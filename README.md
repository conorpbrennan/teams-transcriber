# Teams Transcriber

A background **daemon** that automatically records Microsoft Teams meetings to
text. It watches for a Teams meeting with **live captions** turned on, and the
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
2. It polls the Teams tab for live-caption elements.
3. When captions appear → it opens a new file in `transcripts/` and records.
4. When the meeting ends (captions gone, or the tab closes) → it finalizes that
   file and goes back to watching.

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

Just take your Teams meetings in the daemon's Chrome window and **turn on live
captions** (More → Language and speech → Turn on live captions). Recording is
automatic. Transcripts land in `transcripts/transcript_<date>_<time>.txt`.

## Running manually (without the service)

```bash
python3 transcriber.py                 # foreground daemon (auto-launches Chrome)
python3 transcriber.py --no-launch     # attach only to an already-running debug Chrome
python3 transcriber.py --discover      # dump caption-related DOM (troubleshooting)
```

## Requirements

- Google Chrome (or Chromium) — detected automatically
- Python: `requests`, `websocket-client`
- Live captions must be turned on in the meeting (the daemon reads them; it can't
  enable them for you).

## Troubleshooting

If a meeting records nothing, run `python3 transcriber.py --discover` during the
call. It prints the caption-related DOM elements; if Teams has changed its
markup, update the `TEXT_SELECTORS` / `AUTHOR_SELECTORS` lists in
`transcriber.py`.
