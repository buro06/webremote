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
python webremote.py --media-keys    # drive playback with media keys only
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

### TimeoutError on /api/state, or the page shows "not responding"

The COM apartment is wrong. `webremote.py --check` prints it:

```
  com   : MTA apartment
```

`MTA` is correct. If it says `STA - async calls will hang`, something in the
process put the worker thread in a single-threaded apartment before we did.
WinRT posts async completions to an STA thread's Windows message queue, and
that thread runs an asyncio loop rather than a message pump, so every await
hangs until it times out. Report it - there is no user-side workaround.

### "winrt-windows-... does not provide the extra 'all'" during install

Harmless. Those warnings come from transitive `winrt-*` packages that have no
`all` extra of their own; the packages still install correctly.

### A button does nothing in one particular app

Run `--check` and look at the session table it prints:

```
  media : Windows media session (via winrt)
          2 media session(s) registered
        * firefox.exe  [playing] n-p-s  dur=0.0  "Song Title"
          Spotify.exe  [paused]  nppps  dur=214.0  "Other Song"
           * = the session Windows calls current; flags are
             next/pause/play/prev/seek, letter = supported
```

Each flag is the first letter of a control the app says it supports, or `-`
if it does not. An app advertising `-` for pause has told Windows it will not
honour a pause command, and the remote cannot make it.

When the session route is refused, try the blunt instrument:

```
python webremote.py --media-keys
```

That skips the media session for playback and sends virtual media keys
instead, which Windows routes by its own rules. Track info still comes from
the session. Firefox in particular needs `media.hardwaremediakeys.enabled`
set to true in `about:config` for either route to reach it.

### Track info appears for one app but not another

An app only appears if it registers with the Windows media session. Spotify,
Chrome and Edge do this reliably; Firefox needs `media.hardwaremediakeys.enabled`
set to true in `about:config`; some players never register at all and can only
be driven by media keys.

The remote prefers whichever session is actually playing over the one Windows
calls "current", so a paused Spotify no longer masks a playing browser tab.
`--check` reports how many sessions are registered and which one it is reading.

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

## Updates

While a page is open, the server checks the playback status, controls,
timeline and volume every quarter second. The page asks
`/api/state?since=<version>`, which the server holds open until that check
sees a change (or ten seconds pass), so play/pause, track, seek and volume
changes show up within about a quarter second. The play/pause icon also flips
the moment you tap it. `--check` prints how long one check takes.

This deliberately does not subscribe to WinRT media-session events. Their
handlers run on Windows' own threads and need Python's GIL, which the worker
holds while it is blocked in a call to the same session; that stalled every
command after a pause.

The checking stops 15 seconds after the last page request, and the page makes
no requests while the tab is hidden or the phone is asleep. It refreshes
immediately on becoming visible again. Successful state requests are filtered
out of the server's console so they cannot bury real errors; every other
request still logs, and a command that times out logs a warning.

## API

| Method | Path | Body |
| --- | --- | --- |
| GET | `/api/state` | — ; `?since=<version>` waits up to 10 s for a change |
| POST | `/api/command` | `{"action": "playpause\|play\|pause\|next\|prev\|stop"}`, replies with `via` = which route worked |
| POST | `/api/seek` | `{"position": 42.0}` (seconds) |
| POST | `/api/volume` | `{"level": 40}` or `{"delta": -5}` or `{"mute": null}` (null toggles) |
| GET | `/api/sessions` | every registered media session and its advertised controls |
| GET | `/api/art/<key>` | album art JPEG, key from `/api/state` |
