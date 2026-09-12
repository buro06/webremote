# webremote

Control media playing on a Windows PC from your phone's browser.

## What it does

- Play / pause, previous / next track, seek
- System master volume + mute
- Shows the current track: title, artist, album art, and which app is playing it
- Live connection indicator (green = talking to the PC, red = not)
- One mobile-first page, no build step, no JS dependencies

Transport control and now-playing data come from the **Windows Global System
Media Transport Controls** session — the same thing behind the volume-key
overlay — so it works with Spotify, YouTube/anything in Chrome or Edge, VLC,
Groove, iTunes, and most other players. Volume uses the system endpoint volume
via `pycaw`.

## Setup (on the Windows PC)

Double-click `run.bat` — it creates a virtual environment, installs
dependencies, and starts the server.

Or manually:

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python webremote.py
```

It prints two URLs:

```
  local : http://127.0.0.1:8765/
  phone : http://192.168.1.42:8765/
```

Open the `phone` one on any device on the same Wi-Fi.

### Options

```
python webremote.py --port 9000     # different port
python webremote.py --host 127.0.0.1  # local only
python webremote.py --token         # generate a token; URL becomes .../?t=XXXX
python webremote.py --token mysecret
```

### First-run notes

- Windows Firewall will ask to allow Python on private networks the first time.
  Allow it, or the phone can't reach the server.
- Add the page to your phone's home screen for a full-screen app-like remote.

## Troubleshooting

### "Microsoft Visual C++ 14.0 or greater is required" / "building wheel failed"

**You do not need Visual Studio.** That error means pip found no prebuilt wheel
for your Python version and fell back to compiling from source. The media
bindings are compiled C++ extensions with a limited range of wheels:

| Package | Wheels for |
| --- | --- |
| `winsdk` | Python 3.8 - 3.12 |
| `winrt-Windows.Media.Control` (successor) | Python 3.9 - 3.13 |

So on Python 3.14 neither one installs without a compiler. Pick one:

1. **Install Python 3.13** (or 3.12) alongside your current one and build the
   venv with it - this gets you full track info:
   ```
   py -3.13 -m venv .venv
   .venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
   ```
2. **Skip the extras.** The remote works without them, just in media-key mode:
   buttons control whatever is playing, but no title/artist/art/seek.
   ```
   pip install flask
   python webremote.py
   ```

Always add `--only-binary=:all:` to pip installs here. It makes pip fail fast
with "no matching distribution" instead of trying to invoke a compiler.
`run.bat` does this for you and falls back to a core-only install.

## Degraded modes

Everything is optional except Flask:

| Missing | Effect |
| --- | --- |
| `winsdk` / `winrt-*` | No track info; buttons fall back to virtual media keys |
| `pycaw` | Volume slider disabled; mute/up/down fall back to media keys |
| Not on Windows | Server runs and serves the page, but controls do nothing |

## Security

There is no authentication by default — anyone on your network who finds the
port can pause your music. That's usually fine on a home LAN. Use `--token` if
you want a shared secret in the URL, and don't port-forward this to the
internet.

## API

| Method | Path | Body |
| --- | --- | --- |
| GET | `/api/state` | — |
| POST | `/api/command` | `{"action": "playpause\|play\|pause\|next\|prev\|stop"}` |
| POST | `/api/seek` | `{"position": 42.0}` (seconds) |
| POST | `/api/volume` | `{"level": 40}` or `{"delta": -5}` or `{"mute": null}` (null toggles) |
| GET | `/api/art/<key>` | album art JPEG, key from `/api/state` |
