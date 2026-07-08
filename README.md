# Teams Transcriber

A background **daemon** that automatically records Microsoft Teams meetings to
text. It watches for a Teams meeting, **turns live captions on for you**, and the
moment captions appear it starts saving them to a per-meeting transcript file as:

```
[14:03:21] Austin Brennan: Hello can you see my captions?
```

Fully automated: no screen coordinates, no OCR, no manual start/stop. Captions
are read straight from the page DOM via the Chrome DevTools Protocol, so the text
is accurate, and an in-page MutationObserver captures every line so nothing is
lost as captions scroll away.

## How it works

1. The daemon keeps a debugging-enabled Chrome running (a dedicated profile, so
   your Teams login persists between sessions).
2. It polls **every** Teams tab for a meeting, so a call opened in a new tab (e.g.
   from a calendar link) is picked up wherever it lands.
3. When it detects a meeting it **auto-enables live captions** (drives the menu:
   More → Language and speech → Show live captions) — no manual step needed.
4. When captions appear → it opens a new file in `transcripts/` and records.
5. When the meeting ends (captions gone, or the tab closes) → it finalizes that
   file and goes back to watching.

## Auto-enable & on-screen status

Captions are turned on automatically, and the daemon shows a brief banner at the
top of the meeting so you get confirmation without watching the logs:

- 🟢 **"Meeting detected — transcriber is watching"** when it sees the call.
- 🔴 **"Recording captions to transcript"** once recording has started.

Each banner clears itself after 5 seconds. To disable the auto-enable behaviour
and turn captions on yourself, run with `--no-auto-captions`.

## Install (run once)

```bash
cd ~/dev/teams_transcriber
./install_service.sh
```

This installs a `systemd --user` service that starts on login and restarts if it
crashes. The first time it runs it opens a Chrome window at teams.live.com —
**log into Teams there once**; the login persists for all future meetings.

Manage it:

```bash
systemctl --user status teams-transcriber     # is it running?
journalctl --user -u teams-transcriber -f      # live logs / detection events
systemctl --user stop teams-transcriber        # stop
systemctl --user disable --now teams-transcriber  # uninstall from startup
```

## Using it

Just take your Teams meetings in the daemon's Chrome window — that's it. Captions
are enabled and recording starts automatically; watch for the on-screen banners
to confirm. Transcripts land in `transcripts/transcript_<date>_<time>.txt`.

## Running manually (without the service)

```bash
python3 transcriber.py                    # foreground daemon (auto-launches Chrome)
python3 transcriber.py --no-launch        # attach only to an already-running debug Chrome
python3 transcriber.py --no-auto-captions # don't auto-enable captions; turn them on yourself
python3 transcriber.py --discover         # dump caption/control DOM (troubleshooting)
```

## Requirements

- Google Chrome (or Chromium) — detected automatically
- Python: `requests`, `websocket-client`
- Live captions (the daemon turns them on automatically; pass
  `--no-auto-captions` to enable them yourself instead).

## Troubleshooting

If a meeting records nothing, run `python3 transcriber.py --discover` during the
call. It prints the caption-related DOM **and** the meeting toolbar buttons. If
Teams has changed its markup, update the `TEXT_SELECTORS` / `AUTHOR_SELECTORS`
lists (caption capture) or the auto-enable selectors in `INJECT_JS`
(`closed-captions-button-off`, the "Language and speech" menu item) in
`transcriber.py`.
