"""
webremote - control media playing on a Windows PC from your phone's browser.

Run on the Windows PC:
    python webremote.py
then open the printed http://<pc-ip>:8765 URL on any device on the same network.

Media info and transport control use the Windows "Global System Media Transport
Controls" session (the same thing that powers the volume-key overlay), so it
works with Spotify, YouTube in Chrome/Edge, VLC, Groove, iTunes, etc.
Volume uses the system master volume via pycaw.

Both have fallbacks to virtual media keys, so the server still does something
useful if the optional dependencies aren't installed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import os
import secrets
import socket
import sys
import threading
import time
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, abort

IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------------
# optional dependencies
# --------------------------------------------------------------------------
# The Windows media-session bindings ship under two different distributions:
#   winsdk                        - wheels for Python <= 3.12
#   winrt-Windows.Media.Control   - the maintained successor, wheels for 3.13
# The APIs we use are identical, so accept whichever one is installed.
MediaManager = Buffer = DataReader = InputStreamOptions = None
BINDING = None

for _package in ("winsdk", "winrt"):
    try:
        _control = __import__(f"{_package}.windows.media.control", fromlist=["x"])
        _streams = __import__(f"{_package}.windows.storage.streams", fromlist=["x"])
        MediaManager = _control.GlobalSystemMediaTransportControlsSessionManager
        Buffer = _streams.Buffer
        DataReader = _streams.DataReader
        InputStreamOptions = _streams.InputStreamOptions
        BINDING = _package
        break
    except Exception:  # not installed, or not on Windows
        continue

HAVE_WINRT = BINDING is not None

try:
    from ctypes import POINTER, cast as ctypes_cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    HAVE_PYCAW = True
except Exception:  # pragma: no cover
    HAVE_PYCAW = False


# --------------------------------------------------------------------------
# virtual key fallback (works on Windows even without winsdk/pycaw)
# --------------------------------------------------------------------------
VK = {
    "playpause": 0xB3,
    "next": 0xB0,
    "prev": 0xB1,
    "stop": 0xB2,
    "mute": 0xAD,
    "voldown": 0xAE,
    "volup": 0xAF,
}
KEYEVENTF_KEYUP = 0x0002


def tap_key(name: str) -> bool:
    """Send a virtual key press/release. Returns True if it was sent."""
    if not IS_WINDOWS or name not in VK:
        return False
    import ctypes

    code = VK[name]
    ctypes.windll.user32.keybd_event(code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)
    return True


# --------------------------------------------------------------------------
# a single worker thread owns every COM/WinRT object
#
# WinRT and COM interfaces are apartment-bound, and Flask serves requests from
# a pool of threads. Funnelling all of it through one thread with one event
# loop keeps things predictable.
# --------------------------------------------------------------------------
class Worker:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        threading.Thread(target=self._run, name="winrt-worker", daemon=True).start()
        self._ready.wait(5)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def call(self, fn, *args, timeout=8, **kwargs):
        """Run fn (sync or async) on the worker thread and wait for the result."""

        async def runner():
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result

        future = asyncio.run_coroutine_threadsafe(runner(), self.loop)
        return future.result(timeout=timeout)


worker = Worker()


# --------------------------------------------------------------------------
# media session
# --------------------------------------------------------------------------
PLAYBACK_STATUS = {
    0: "closed",
    1: "opened",
    2: "changing",
    3: "stopped",
    4: "playing",
    5: "paused",
}

_manager = None
_art_cache: dict[str, bytes] = {}
_art_lock = threading.Lock()


async def _get_manager():
    global _manager
    if _manager is None:
        _manager = await MediaManager.request_async()
    return _manager


async def _current_session():
    if not HAVE_WINRT:
        return None
    try:
        manager = await _get_manager()
        return manager.get_current_session()
    except Exception:
        return None


async def _thumbnail_bytes(props) -> bytes | None:
    ref = getattr(props, "thumbnail", None)
    if ref is None:
        return None
    try:
        stream = await ref.open_read_async()
        size = stream.size
        if not size:
            return None
        buf = Buffer(size)
        await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
        reader = DataReader.from_buffer(buf)
        return bytes(reader.read_bytes(buf.length))
    except Exception:
        return None


def _cache_art(data: bytes | None) -> str | None:
    if not data:
        return None
    key = hashlib.sha1(data).hexdigest()[:16]
    with _art_lock:
        if key not in _art_cache:
            # keep the cache tiny; only recent covers matter
            if len(_art_cache) > 8:
                _art_cache.clear()
            _art_cache[key] = data
    return key


async def read_state() -> dict:
    state = {
        "available": False,
        "backend": "session" if HAVE_WINRT else ("mediakeys" if IS_WINDOWS else "none"),
        "status": "closed",
        "title": None,
        "artist": None,
        "album": None,
        "app": None,
        "position": None,
        "duration": None,
        "art": None,
        "controls": {"play": True, "pause": True, "next": True, "prev": True, "seek": False},
    }

    session = await _current_session()
    if session is None:
        return state

    state["available"] = True
    try:
        state["app"] = session.source_app_user_model_id
    except Exception:
        pass

    try:
        info = session.get_playback_info()
        state["status"] = PLAYBACK_STATUS.get(int(info.playback_status), "unknown")
        controls = info.controls
        state["controls"] = {
            "play": bool(controls.is_play_enabled),
            "pause": bool(controls.is_pause_enabled),
            "next": bool(controls.is_next_enabled),
            "prev": bool(controls.is_previous_enabled),
            "seek": bool(controls.is_playback_position_enabled),
        }
    except Exception:
        pass

    try:
        props = await session.try_get_media_properties_async()
        state["title"] = props.title or None
        state["artist"] = props.artist or None
        state["album"] = props.album_title or None
        state["art"] = _cache_art(await _thumbnail_bytes(props))
    except Exception:
        pass

    try:
        timeline = session.get_timeline_properties()
        position = timeline.position.total_seconds()
        duration = timeline.end_time.total_seconds()
        if state["status"] == "playing":
            # position only updates on events, so extrapolate to "now"
            updated = timeline.last_updated_time
            if updated and updated.timestamp() > 0:
                position += max(0.0, time.time() - updated.timestamp())
        if duration > 0:
            state["position"] = round(min(position, duration), 1)
            state["duration"] = round(duration, 1)
    except Exception:
        pass

    return state


async def do_command(action: str) -> bool:
    """Transport control. Prefers the media session, falls back to media keys."""
    session = await _current_session()
    if session is not None:
        try:
            calls = {
                "playpause": session.try_toggle_play_pause_async,
                "play": session.try_play_async,
                "pause": session.try_pause_async,
                "next": session.try_skip_next_async,
                "prev": session.try_skip_previous_async,
                "stop": session.try_stop_async,
            }
            if action in calls and await calls[action]():
                return True
        except Exception:
            pass

    key = {"play": "playpause", "pause": "playpause"}.get(action, action)
    return tap_key(key)


async def do_seek(seconds: float) -> bool:
    session = await _current_session()
    if session is None:
        return False
    try:
        # WinRT playback position is in 100-nanosecond ticks
        return bool(await session.try_change_playback_position_async(int(seconds * 10_000_000)))
    except Exception:
        return False


# --------------------------------------------------------------------------
# system volume
# --------------------------------------------------------------------------
_endpoint = None


def _volume_endpoint():
    global _endpoint
    if _endpoint is None:
        speakers = AudioUtilities.GetSpeakers()
        interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        _endpoint = ctypes_cast(interface, POINTER(IAudioEndpointVolume))
    return _endpoint


def read_volume() -> dict:
    if not HAVE_PYCAW:
        return {"available": False, "level": None, "muted": None}
    try:
        endpoint = _volume_endpoint()
        return {
            "available": True,
            "level": round(endpoint.GetMasterVolumeLevelScalar() * 100),
            "muted": bool(endpoint.GetMute()),
        }
    except Exception:
        return {"available": False, "level": None, "muted": None}


def set_volume(level: float) -> bool:
    level = max(0.0, min(100.0, float(level)))
    if HAVE_PYCAW:
        try:
            _volume_endpoint().SetMasterVolumeLevelScalar(level / 100.0, None)
            return True
        except Exception:
            pass
    return False


def nudge_volume(delta: float) -> bool:
    if HAVE_PYCAW:
        current = read_volume()
        if current["available"]:
            return set_volume(current["level"] + delta)
    # each media key step is ~2%
    key = "volup" if delta > 0 else "voldown"
    sent = False
    for _ in range(max(1, int(abs(delta) / 2))):
        sent = tap_key(key) or sent
    return sent


def set_mute(muted: bool | None) -> bool:
    if HAVE_PYCAW:
        try:
            endpoint = _volume_endpoint()
            value = (not endpoint.GetMute()) if muted is None else bool(muted)
            endpoint.SetMute(value, None)
            return True
        except Exception:
            pass
    return tap_key("mute")


# --------------------------------------------------------------------------
# web app
# --------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.config["TOKEN"] = None


def protected(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        token = app.config["TOKEN"]
        if token:
            supplied = request.args.get("t") or request.headers.get("X-Token")
            if not supplied or not secrets.compare_digest(supplied, token):
                abort(401)
        return view(*args, **kwargs)

    return wrapper


@app.after_request
def no_store(response):
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/")
@protected
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.get("/api/state")
@protected
def api_state():
    state = worker.call(read_state)
    state["volume"] = worker.call(read_volume)
    state["time"] = time.time()
    return jsonify(state)


@app.post("/api/command")
@protected
def api_command():
    action = (request.get_json(silent=True) or {}).get("action", "")
    if action not in {"playpause", "play", "pause", "next", "prev", "stop"}:
        return jsonify({"ok": False, "error": "unknown action"}), 400
    return jsonify({"ok": bool(worker.call(do_command, action))})


@app.post("/api/seek")
@protected
def api_seek():
    body = request.get_json(silent=True) or {}
    try:
        position = float(body.get("position"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad position"}), 400
    return jsonify({"ok": bool(worker.call(do_seek, position))})


@app.post("/api/volume")
@protected
def api_volume():
    body = request.get_json(silent=True) or {}
    if "mute" in body:
        ok = worker.call(set_mute, body["mute"])
    elif "delta" in body:
        ok = worker.call(nudge_volume, float(body["delta"]))
    elif "level" in body:
        ok = worker.call(set_volume, float(body["level"]))
    else:
        return jsonify({"ok": False, "error": "nothing to do"}), 400
    return jsonify({"ok": bool(ok), "volume": worker.call(read_volume)})


@app.get("/api/art/<key>")
@protected
def api_art(key):
    with _art_lock:
        data = _art_cache.get(key)
    if not data:
        abort(404)
    return app.response_class(data, mimetype="image/jpeg", headers={"Cache-Control": "max-age=3600"})


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all)")
    parser.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    parser.add_argument("--token", nargs="?", const="generate", default=None,
                        help="require ?t=TOKEN; pass the flag alone to generate one")
    parser.add_argument("--check", action="store_true",
                        help="report what this environment supports and exit; "
                             "exit code 0 = full features, 1 = media keys only")
    args = parser.parse_args()

    media_note = f"Windows media session (via {BINDING})" if HAVE_WINRT else \
        "media keys only - no track info (see README: 'No wheel for your Python')"
    volume_note = "system master volume" if HAVE_PYCAW else "media keys only (pycaw not installed)"

    if args.check:
        print(f"  media : {media_note}")
        print(f"  volume: {volume_note}")
        return 0 if HAVE_WINRT else 1

    if args.token == "generate":
        args.token = secrets.token_urlsafe(8)
    app.config["TOKEN"] = args.token

    suffix = f"?t={args.token}" if args.token else ""
    print("webremote")
    print(f"  media : {media_note}")
    print(f"  volume: {volume_note}")
    print()
    print(f"  local : http://127.0.0.1:{args.port}/{suffix}")
    print(f"  phone : http://{local_ip()}:{args.port}/{suffix}")
    print()
    print("  Ctrl+C to stop.")

    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
