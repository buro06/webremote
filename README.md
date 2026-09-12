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

First, ask the environment what it supports:

```
.venv\Scripts\python.exe webremote.py --check
```

It prints the media and volume backends, the import error behind anything
missing, and the pip command that fixes it. Exit code 0 means full features.

### Buttons work, but there is no track info and no volume slider

The optional extras are not installed, so the remote is in media-key mode.
Install them into the existing environment - no rebuild, no compiler:

```
.venv\Scripts\python.exe -m pip install --only-binary=:all: "winrt-Windows.Media.Control[all]" pycaw comtypes
```

On Python 3.12 or older, use `winsdk` in place of the `winrt-` package.

**The `[all]` extra is required.** A bare `winrt-Windows.Media.Control`
depends only on `winrt-runtime`; the `Windows.Foundation` and `Windows.Media`
packages its own types need sit behind that extra, so the import fails and the
remote falls back to media keys with no visible error.

`run.bat` detects this state on startup and offers to install the missing
pieces in place.

### "Microsoft Visual C++ 14.0 or greater is required" / "building wheel failed"

**You do not need Visual Studio.** That error means pip found no prebuilt wheel
for your Python version and fell back to compiling from source. The media
bindings are compiled C++ extensions with a limited range of wheels:

| Package | Wheels for |
| --- | --- |
| `winsdk` | Python 3.8 - 3.12 |
| `winrt-Windows.Media.Control` (successor) | Python 3.9 - 3.13 |

So on Python 3.14 neither one installs without a compiler.

**`run.bat` handles this for you.** It looks for a Python between 3.9 and 3.13,
and if 3.14 is all you have it explains the tradeoff and offers to install
Python 3.13 via winget and build the environment with that instead:

```
  The only Python installed is Python 3.14.0.

    [1]  Install Python 3.13 and use it  -  recommended, full features
    [2]  Continue with Python 3.14.0  -  buttons work, no track info
    [3]  Exit
```

It offers the same choice when an existing `.venv` turns out to be media-key
only, in which case option 1 rebuilds it. To do it by hand instead:

```
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
```

Always pass `--only-binary=:all:` when installing by hand. It makes pip fail
fast with "no matching distribution" instead of trying to invoke a compiler.

Note that `run.bat` installs the extras as three separate pip commands, so an
extra with no wheel for your Python cannot take the others down with it.

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
