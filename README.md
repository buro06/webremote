# webremote

Control whatever is playing on a Windows PC from your phone's browser. You get
play/pause, previous/next, a seek bar, system volume and mute.

It works with any app that reports to Windows' media controls (the overlay
that appears when you press a volume key). That includes Spotify, YouTube and
other sites in Chrome, Edge or Firefox, VLC, Media Player, iTunes and more.

## Run it

On the Windows PC, double-click **`run.bat`**. The first run creates a virtual
environment and installs the dependencies. It then prints an address such as:

```
  on a phone : http://192.168.1.20:8765/
```

Open that address on any device on the same network. The first time, Windows
Firewall asks whether to allow Python. Allow it on **private** networks.

Or run it by hand:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python webremote.py
```

| Option | |
|---|---|
| `--port 9000` | listen on another port (default 8765) |
| `--token` | require a random access token, printed as part of the URL |
| `--token mysecret` | require a token you choose |
| `--check` | test each feature on this PC and report what works |
| `--demo` | simulated player, to try the page on any OS without Windows |
| `--verbose` | log every request |

Without `--token`, anyone on your network who finds the port can control
playback and volume.

## What depends on what

The server itself uses only the Python standard library. Both extras are
optional:

| Package | Gives you | Without it |
|---|---|---|
| `winrt-*` (media session) | title, artist, album art, seek bar, commands sent to the right app | buttons send media keys; no track info |
| `pycaw` | volume slider, mute state | − / + buttons send volume keys |

Scrubbing works only when the app supports it. Spotify, Chrome and Edge
usually do. When an app doesn't, the bar still shows progress but has no
handle.

## When something doesn't work

Run `run.bat --check`. It calls each backend for real and lists every media
session Windows knows about, along with what each one supports:

```
media : Windows media session (via winrt)
        2 session(s) registered
      * Spotify.exe  [playing]  supports: play, pause, next, prev, seek
        Chrome  [paused]  supports: play, pause
```

- **No track info.** The `winrt` packages have no build for your Python
  version. Install an older Python (e.g. 3.13), delete `.venv`, and run
  `run.bat` again.
- **A button does nothing in one app.** Check the `supports:` list. If the app
  doesn't list the action, the app doesn't offer it to Windows.
- **The page can't connect.** Check that the phone is on the same network and
  not a guest network. Then check that Windows Firewall allows Python.

## How it works

`webremote.py` runs a small HTTP server. The page receives state over
Server-Sent Events and sends commands as JSON `POST`s:

| Endpoint | |
|---|---|
| `GET /api/events` | stream of state snapshots, one per change |
| `GET /api/state` | the current state, once |
| `POST /api/command` | `{"action": "playpause" \| "next" \| "prev"}` |
| `POST /api/seek` | `{"position": seconds}` |
| `POST /api/volume` | `{"level": 0-100}`, `{"delta": ±n}` or `{"mute": true \| false \| null}` (null toggles) |
| `GET /api/art/<id>` | album art |

All WinRT and COM work happens on one worker thread in the multithreaded COM
apartment. In a single-threaded apartment, awaited WinRT calls never complete
on a thread that runs an asyncio loop. While a page is open, the worker polls
a cheap fingerprint of the playback state every 250 ms. It pushes a full
snapshot when the fingerprint changes and every 3 s regardless. It polls
instead of subscribing to WinRT events because those callbacks can deadlock
against the worker on the GIL. Between snapshots, the page advances the
position itself.
