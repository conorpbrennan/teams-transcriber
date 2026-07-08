#!/usr/bin/env python3
"""Teams live-caption transcriber daemon (fully automated, DOM-based).

Runs continuously in the background. It keeps a debugging-enabled Chrome alive,
watches for a Teams meeting with live captions turned on, and as soon as captions
appear it starts recording them to a per-meeting transcript file as
`[HH:MM:SS] Speaker: text`. When the meeting ends it finalizes that file and goes
back to watching. No screen coordinates, no OCR, no manual start/stop.

Captions are read from the page DOM via the Chrome DevTools Protocol, captured by
an in-page MutationObserver so nothing is lost as lines scroll out of view.

Usage:
  python3 transcriber.py            # run as a foreground daemon
  python3 transcriber.py --discover # print caption-related DOM (debugging)
Install as a background service with install_service.sh.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import requests
import websocket  # websocket-client

CDP_HTTP = "http://localhost:9222"
DEBUG_PORT = 9222
TEAMS_HINTS = ("teams.live.com", "teams.microsoft.com")
TEAMS_URL = "https://teams.live.com/v2/"
CHROME_PROFILE = os.path.expanduser("~/.config/teams-transcriber-chrome")
# Keep recording through brief caption gaps; end the meeting only after captions
# have been absent this long.
END_GRACE = 120.0


# Injected (idempotently) into the Teams page. A MutationObserver accumulates
# every caption into window.__capLog as {speaker, text}, updating a line in place
# while it is still being spoken. Captions are keyed by DOM node, so a line that
# scrolls away and a brand-new line are distinct entries. Also exposes
# __capPresent() and __capReset() for the daemon.
INJECT_JS = r"""
(() => {
  if (window.__capInit) return 'ready';
  const TEXT_SELECTORS = [
    '[data-tid="closed-caption-text"]',
    '[data-tid="closed-caption-v2-text"]',
    '[class*="closed-caption" i] [class*="text" i]',
  ];
  const AUTHOR_SELECTORS = [
    '[data-tid="author"]',
    '[class*="author" i]',
    '[class*="displayName" i]',
    '[class*="senderName" i]',
  ];
  const txt = el => (el && (el.innerText || el.textContent) || '').trim();
  function captionEls() {
    for (const sel of TEXT_SELECTORS) {
      const els = document.querySelectorAll(sel);
      if (els.length) return [...els];
    }
    return [];
  }
  function author(t) {
    let p = t;
    for (let i = 0; i < 6 && p; i++) {
      p = p.parentElement;
      if (!p) break;
      for (const sel of AUTHOR_SELECTORS) {
        const a = p.querySelector(sel);
        if (a && txt(a)) return txt(a);
      }
    }
    return 'Unknown';
  }
  window.__capLog = [];
  const live = new Map();           // caption node -> index in __capLog
  function record(t) {
    const text = txt(t);
    if (!text) return;
    const speaker = author(t);
    if (live.has(t)) {
      window.__capLog[live.get(t)] = { speaker, text };
    } else {
      live.set(t, window.__capLog.length);
      window.__capLog.push({ speaker, text });
    }
  }
  function scan() { captionEls().forEach(record); }
  new MutationObserver(scan).observe(document.body, {
    subtree: true, childList: true, characterData: true,
  });
  window.__capPresent = () => captionEls().length > 0;
  window.__capReset = () => { window.__capLog.length = 0; live.clear(); };

  // A brief on-screen banner the daemon fires at milestones (meeting seen,
  // recording started) so you get visual confirmation without reading logs.
  window.__capToast = (msg, color) => {
    try {
      let el = document.getElementById('__capToast');
      if (!el) { el = document.createElement('div'); el.id = '__capToast';
                 document.documentElement.appendChild(el); }
      el.textContent = msg;
      el.style.cssText = 'position:fixed;top:18px;left:50%;transform:translateX(-50%);'
        + 'z-index:2147483647;padding:12px 22px;border-radius:10px;'
        + 'font:600 15px/1.3 system-ui,-apple-system,Segoe UI,sans-serif;color:#fff;'
        + 'background:' + (color || '#0b7a34') + ';box-shadow:0 6px 24px rgba(0,0,0,.4);'
        + 'pointer-events:none;';
      clearTimeout(el.__t);
      el.__t = setTimeout(() => { el.remove(); }, 5000);
    } catch (e) {}
  };

  // --- auto-enable live captions -------------------------------------------
  // Teams can't be told to start captions via an API, so we drive the meeting
  // toolbar: click the overflow "More" button, then the "Show live captions"
  // item (data-tid="closed-captions-button-off"). Verified against
  // teams.live.com/v2 (Jul 2026); the data-tid is the stable hook.
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const clickables = () => [...document.querySelectorAll(
    'button,[role=menuitem],[role=menuitemcheckbox],[role=button]')];
  const match = re => clickables().find(el => re.test(
    ((el.getAttribute('aria-label') || '') + ' ' + txt(el)).toLowerCase()));
  // The overflow button is labelled exactly "More"; the hang-up "More options"
  // and "More chat options" buttons must not match.
  const moreBtn = () => clickables().find(el =>
    (el.getAttribute('aria-label') || '').trim().toLowerCase() === 'more' ||
    txt(el).toLowerCase() === 'more');
  const captionsOn = () => captionEls().length > 0
    || !!document.querySelector('[data-tid="captions-panel-dismiss-button"]')
    || !!document.querySelector('[data-tid^="closed-captions-settings-menu-trigger"]')
    || !!document.querySelector('[data-tid="closed-captions-button-on"]');
  // In a call iff there is a Leave / hang-up control.
  window.__capMeeting = () => !!match(/\b(leave|hang up)\b/);
  window.__cap = { done: false, inCall: false, tries: 0, busy: false, last: 0, log: [] };
  const note = m => {
    window.__cap.log.push(m);
    if (window.__cap.log.length > 20) window.__cap.log.shift();
  };
  async function enableCaptions() {
    if (window.__autoCapOff) return;
    const st = window.__cap, inCall = window.__capMeeting();
    if (inCall && !st.inCall) { st.done = false; st.tries = 0; }  // new meeting
    st.inCall = inCall;
    if (!inCall || st.done) return;
    if (captionsOn()) { st.done = true; note('captions already on'); return; }
    const now = Date.now();
    if (st.busy || now - st.last < 8000 || st.tries >= 8) return;  // bounded, throttled
    st.busy = true; st.last = now; st.tries++;
    try {
      const more = moreBtn();
      if (!more) { note('meeting "More" button not found'); return; }
      more.click(); await wait(900);                 // open the overflow menu
      // The captions toggle lives in a "Language and speech" submenu (a
      // haspopup=menu item with no data-tid) -- open it first.
      const lang = clickables().find(el =>
        /language and speech|captions and transcript/i.test(
          (el.getAttribute('aria-label') || '') + ' ' + txt(el)));
      if (lang) { lang.click(); await wait(900); }
      const off = document.querySelector('[data-tid="closed-captions-button-off"]')
                || match(/show live captions|turn on live captions/);
      if (off) { off.click(); st.done = true; note('turned live captions on'); }
      else if (captionsOn()) { st.done = true; note('captions already on'); more.click(); }
      else { note('caption toggle not found in menu'); more.click(); }
    } catch (e) { note('error: ' + e.message); }
    finally { st.busy = false; }
  }
  setInterval(enableCaptions, 3000);

  scan();
  window.__capInit = true;
  return 'init';
})()
"""

STATUS_JS = ("JSON.stringify({n:(window.__capLog||[]).length,"
             "present:(typeof window.__capPresent==='function'?window.__capPresent():false),"
             "meeting:(typeof window.__capMeeting==='function'?window.__capMeeting():false),"
             "autolog:((window.__cap&&window.__cap.log)||[])})")
LOG_JS = "JSON.stringify(window.__capLog || [])"

DISCOVER_JS = r"""
(() => {
  const out = { captions: [], controls: [] };
  document.querySelectorAll('[data-tid],[aria-label],[class]').forEach(el => {
    const sig = [el.getAttribute('data-tid'), el.getAttribute('aria-label'),
                 el.className && el.className.toString()].join(' ');
    if (/caption/i.test(sig)) {
      out.captions.push({ tag: el.tagName, sig: sig.trim().slice(0, 120),
                          text: (el.innerText || '').trim().slice(0, 60) });
    }
  });
  // Toolbar/menu buttons, so we can tune the auto-enable selectors.
  document.querySelectorAll('button,[role=menuitem],[role=button]').forEach(el => {
    const label = (el.getAttribute('aria-label') || el.innerText || '').trim();
    if (label) out.controls.push({ tid: el.getAttribute('data-tid') || '',
                                   label: label.slice(0, 50) });
  });
  out.captions = out.captions.slice(0, 25);
  out.controls = out.controls.slice(0, 60);
  return JSON.stringify(out, null, 2);
})()
"""


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


class CDP:
    """Minimal Chrome DevTools Protocol client over a single page websocket."""

    def __init__(self, ws_url):
        # suppress_origin: Chrome's DevTools endpoint returns 403 if an Origin
        # header is present (DNS-rebinding protection).
        self.ws = websocket.create_connection(ws_url, max_size=None, suppress_origin=True)
        self.ws.settimeout(15)
        self._id = 0

    def evaluate(self, expression):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({
            "id": mid, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True,
                       "awaitPromise": True},
        }))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise RuntimeError(msg["error"])
            result = msg["result"]["result"]
            if result.get("subtype") == "error":
                raise RuntimeError(result.get("description", "eval error"))
            return result.get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def chrome_alive():
    try:
        requests.get(f"{CDP_HTTP}/json/version", timeout=3).raise_for_status()
        return True
    except requests.RequestException:
        return False


def chrome_binary():
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = subprocess.run(["bash", "-lc", f"command -v {name}"],
                              capture_output=True, text=True).stdout.strip()
        if path:
            return path
    return None


def display_env():
    """Display vars Chrome needs, pulled live from the systemd --user environment.

    When started at boot, this service's own environment has no DISPLAY/XAUTHORITY
    (GNOME imports them into the user manager only once the graphical session is
    up, which can be after we start). Querying systemd at launch time gets the
    current values and tracks the XAUTHORITY filename, whose suffix changes every
    login. Without these Chrome cannot reach the display and exits silently, so
    the debug port never opens and we loop relaunching forever.
    """
    env = dict(os.environ)
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return env
    wanted = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
    for line in out.splitlines():
        key, _, val = line.partition("=")
        if key in wanted and val:
            env[key] = val
    return env


def launch_chrome():
    """Start a debugging-enabled Chrome on a dedicated profile (login persists)."""
    binary = chrome_binary()
    if not binary:
        log("[error] no Chrome/Chromium binary found")
        return False
    os.makedirs(CHROME_PROFILE, exist_ok=True)
    # Remove stale singleton locks left by a crashed/zombie Chrome. Otherwise a
    # new launch "hands off" to the dead instance and exits immediately, so the
    # debug port never opens and we loop forever relaunching.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.unlink(os.path.join(CHROME_PROFILE, name))
        except FileNotFoundError:
            pass
    log("launching Chrome with remote debugging...")
    subprocess.Popen(
        [binary, f"--remote-debugging-port={DEBUG_PORT}",
         f"--user-data-dir={CHROME_PROFILE}", "--no-first-run",
         "--no-default-browser-check", TEAMS_URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        env=display_env(),
    )
    for _ in range(40):
        if chrome_alive():
            return True
        time.sleep(0.5)
    return False


def list_teams_targets():
    """Every open Teams *page* tab (chat, calendar, and any meeting tabs).

    A meeting launched from a calendar link opens in a new tab, so we must watch
    all of them and record from whichever actually shows captions -- not just the
    first Teams tab that happened to exist when the daemon attached.
    """
    try:
        targets = requests.get(f"{CDP_HTTP}/json", timeout=5).json()
    except requests.RequestException:
        return []
    return [t for t in targets
            if t.get("type") == "page" and any(h in t.get("url", "") for h in TEAMS_HINTS)]


def find_teams_target():
    targets = list_teams_targets()
    return targets[0].get("webSocketDebuggerUrl") if targets else None


class Transcript:
    """Writes finalized caption entries to one meeting file, exactly once each."""

    def __init__(self, out_path):
        self.out_path = out_path
        self.finalized = 0

    def _write(self, speaker, text):
        stamp = datetime.now().strftime("%H:%M:%S")
        with open(self.out_path, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {speaker}: {text}\n")

    def ingest(self, entries, flush_last=False):
        last = len(entries) if flush_last else len(entries) - 1
        while self.finalized < last:
            e = entries[self.finalized]
            text = (e.get("text") or "").strip()
            if text:
                self._write(e.get("speaker") or "Unknown", text)
            self.finalized += 1


def new_transcript_path():
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcripts")
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"transcript_{datetime.now():%Y-%m-%d_%H%M%S}.txt")


def daemon(interval, launch, auto_captions=True):
    log(f"transcriber daemon started (poll {interval}s). Watching all Teams tabs"
        + ("." if auto_captions else "; auto-captions disabled."))
    conns = {}          # target id -> CDP connection
    autolog_seen = {}   # target id -> auto-caption log lines already printed
    meeting_ids = set() # tabs currently in a meeting (for the "detected" banner)
    recording = False
    transcript = None
    rec_id = None       # target id we are recording from
    last_active = 0.0

    def toast(tid, msg, color):
        c = conns.get(tid)
        if not c:
            return
        try:
            c.evaluate(f"window.__capToast && window.__capToast({json.dumps(msg)},"
                       f"{json.dumps(color)})")
        except (websocket.WebSocketException, RuntimeError, OSError):
            pass

    def drop(tid):
        c = conns.pop(tid, None)
        if c:
            c.close()
        autolog_seen.pop(tid, None)
        meeting_ids.discard(tid)

    def stop_recording(reason):
        nonlocal recording, transcript, rec_id
        if transcript:
            log(f"{reason}: saved {transcript.out_path}")
        recording, transcript, rec_id = False, None, None

    while True:
        try:
            if not chrome_alive():
                if recording:
                    stop_recording("Chrome closed")
                for tid in list(conns):
                    drop(tid)
                if launch:
                    launch_chrome()
                else:
                    time.sleep(3)
                continue

            targets = {t["id"]: t for t in list_teams_targets()}
            for tid in list(conns):          # forget tabs that were closed
                if tid not in targets:
                    if tid == rec_id:
                        stop_recording("recording tab closed")
                    drop(tid)

            # Inject into / poll every Teams tab; collect which ones show captions.
            present_ids = []
            for tid, t in targets.items():
                try:
                    cdp = conns.get(tid)
                    if cdp is None:
                        cdp = CDP(t["webSocketDebuggerUrl"])
                        conns[tid] = cdp
                        if not auto_captions:
                            cdp.evaluate("window.__autoCapOff = true")
                        log(f"attached to Teams tab: {t.get('url', '')[:70]}")
                    cdp.evaluate(INJECT_JS)   # idempotent; re-arms after navigation
                    status = json.loads(cdp.evaluate(STATUS_JS))
                except (websocket.WebSocketException, RuntimeError, OSError,
                        json.JSONDecodeError):
                    if tid == rec_id:
                        stop_recording("recording tab lost")
                    drop(tid)
                    continue
                autolog = status.get("autolog", [])
                for line in autolog[autolog_seen.get(tid, 0):]:
                    log(f"[auto-captions] {line}")
                autolog_seen[tid] = len(autolog)
                if status.get("meeting") and tid not in meeting_ids:
                    meeting_ids.add(tid)
                    log("meeting detected on a Teams tab")
                    toast(tid, "✓ Meeting detected — transcriber is watching",
                          "#0b7a34")
                elif not status.get("meeting"):
                    meeting_ids.discard(tid)
                if status.get("present"):
                    present_ids.append(tid)

            now = time.monotonic()

            if not recording and present_ids:
                rec_id = present_ids[0]
                path = new_transcript_path()
                conns[rec_id].evaluate("window.__capReset && window.__capReset()")
                transcript = Transcript(path)
                recording, last_active = True, now
                log(f"captions detected -> recording to {path}")
                toast(rec_id, "● Recording captions to transcript", "#b00020")

            if recording and rec_id in conns:
                try:
                    entries = json.loads(conns[rec_id].evaluate(LOG_JS))
                except (websocket.WebSocketException, RuntimeError, OSError,
                        json.JSONDecodeError):
                    stop_recording("recording tab lost")
                    entries = None
                if entries is not None:
                    present = rec_id in present_ids
                    before = transcript.finalized
                    transcript.ingest(entries)
                    if present or transcript.finalized > before:
                        last_active = now
                    if not present and now - last_active > END_GRACE:
                        transcript.ingest(entries, flush_last=True)
                        stop_recording("meeting ended")

        except requests.RequestException:
            pass  # transient Chrome hiccup; retry next poll

        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description="Automated Teams caption transcriber daemon.")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between DOM polls (default 2)")
    ap.add_argument("--no-launch", action="store_true",
                    help="do not auto-launch Chrome; only attach if it is already running")
    ap.add_argument("--no-auto-captions", action="store_true",
                    help="do not try to turn live captions on automatically")
    ap.add_argument("--discover", action="store_true",
                    help="print caption/control DOM from the active meeting tab and exit")
    args = ap.parse_args()

    if args.discover:
        targets = list_teams_targets()
        if not targets:
            print("No Teams tab found.", file=sys.stderr)
            sys.exit(1)
        # Prefer a tab that is in a meeting / already showing captions.
        chosen = None
        for t in targets:
            cdp = CDP(t["webSocketDebuggerUrl"])
            cdp.evaluate(INJECT_JS)
            status = json.loads(cdp.evaluate(STATUS_JS))
            if status.get("present") or status.get("meeting"):
                chosen = cdp
                break
            cdp.close()
        if chosen is None:
            chosen = CDP(targets[0]["webSocketDebuggerUrl"])
            chosen.evaluate(INJECT_JS)
        print(chosen.evaluate(DISCOVER_JS))
        chosen.close()
        return

    try:
        daemon(args.interval, launch=not args.no_launch,
               auto_captions=not args.no_auto_captions)
    except KeyboardInterrupt:
        log("daemon stopped.")


if __name__ == "__main__":
    main()
