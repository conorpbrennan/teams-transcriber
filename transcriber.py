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
  scan();
  window.__capInit = true;
  return 'init';
})()
"""

STATUS_JS = ("JSON.stringify({n:(window.__capLog||[]).length,"
             "present:(typeof window.__capPresent==='function'?window.__capPresent():false)})")
LOG_JS = "JSON.stringify(window.__capLog || [])"

DISCOVER_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('[data-tid],[aria-label],[class]').forEach(el => {
    const sig = [el.getAttribute('data-tid'), el.getAttribute('aria-label'),
                 el.className && el.className.toString()].join(' ');
    if (/caption/i.test(sig)) {
      out.push({ tag: el.tagName, sig: sig.trim().slice(0, 120),
                 text: (el.innerText || '').trim().slice(0, 60) });
    }
  });
  return JSON.stringify(out.slice(0, 25));
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


def launch_chrome():
    """Start a debugging-enabled Chrome on a dedicated profile (login persists)."""
    binary = chrome_binary()
    if not binary:
        log("[error] no Chrome/Chromium binary found")
        return False
    os.makedirs(CHROME_PROFILE, exist_ok=True)
    log("launching Chrome with remote debugging...")
    subprocess.Popen(
        [binary, f"--remote-debugging-port={DEBUG_PORT}",
         f"--user-data-dir={CHROME_PROFILE}", "--no-first-run",
         "--no-default-browser-check", TEAMS_URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    for _ in range(40):
        if chrome_alive():
            return True
        time.sleep(0.5)
    return False


def find_teams_target():
    try:
        targets = requests.get(f"{CDP_HTTP}/json", timeout=5).json()
    except requests.RequestException:
        return None
    for t in targets:
        if t.get("type") == "page" and any(h in t.get("url", "") for h in TEAMS_HINTS):
            return t.get("webSocketDebuggerUrl")
    return None


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


def daemon(interval, launch):
    log(f"transcriber daemon started (poll {interval}s). Watching for Teams captions.")
    cdp = None
    recording = False
    transcript = None
    last_active = 0.0

    while True:
        try:
            if not chrome_alive():
                if recording and transcript:
                    log(f"Chrome closed; finalized {transcript.out_path}")
                recording, transcript, cdp = False, None, None
                if launch:
                    launch_chrome()
                else:
                    time.sleep(3)
                    continue

            if cdp is None:
                url = find_teams_target()
                if not url:
                    time.sleep(2)
                    continue
                cdp = CDP(url)
                cdp.evaluate(INJECT_JS)
                log("attached to Teams tab.")

            cdp.evaluate(INJECT_JS)  # idempotent; re-arms after navigation
            status = json.loads(cdp.evaluate(STATUS_JS))
            present = status.get("present", False)
            now = time.monotonic()

            if not recording and present:
                path = new_transcript_path()
                cdp.evaluate("window.__capReset && window.__capReset()")
                transcript = Transcript(path)
                recording, last_active = True, now
                log(f"captions detected -> recording to {path}")

            if recording:
                entries = json.loads(cdp.evaluate(LOG_JS))
                before = transcript.finalized
                transcript.ingest(entries)
                if present or transcript.finalized > before:
                    last_active = now
                if not present and now - last_active > END_GRACE:
                    transcript.ingest(entries, flush_last=True)
                    log(f"meeting ended -> saved {transcript.out_path}")
                    recording, transcript = False, None

        except (websocket.WebSocketException, RuntimeError, OSError,
                json.JSONDecodeError) as e:
            log(f"[info] tab dropped ({type(e).__name__}); will reattach")
            if cdp:
                cdp.close()
            cdp = None
            if recording and transcript:
                # Page is gone; the meeting is effectively over.
                log(f"finalized {transcript.out_path}")
                recording, transcript = False, None

        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description="Automated Teams caption transcriber daemon.")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between DOM polls (default 2)")
    ap.add_argument("--no-launch", action="store_true",
                    help="do not auto-launch Chrome; only attach if it is already running")
    ap.add_argument("--discover", action="store_true",
                    help="print caption-related DOM elements once and exit")
    args = ap.parse_args()

    if args.discover:
        url = find_teams_target()
        if not url:
            print("No Teams tab found.", file=sys.stderr)
            sys.exit(1)
        cdp = CDP(url)
        cdp.evaluate(INJECT_JS)
        print(cdp.evaluate(DISCOVER_JS))
        cdp.close()
        return

    try:
        daemon(args.interval, launch=not args.no_launch)
    except KeyboardInterrupt:
        log("daemon stopped.")


if __name__ == "__main__":
    main()
