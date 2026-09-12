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
import logging
import socket
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, abort

IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------------
# optional dependencies
# --------------------------------------------------------------------------
# The Windows media-session bindings ship under two different distributions:
#   winsdk                            - wheels for Python <= 3.12
#   winrt-Windows.Media.Control[all]  - the successor, wheels for 3.13
# The APIs we use are identical, so accept whichever one is installed.
#
# The [all] extra matters: a bare winrt-Windows.Media.Control pulls in only
# winrt-runtime, leaving out the Windows.Foundation / Windows.Media packages
# its types depend on, and the import then fails at runtime.
MEDIA_FIX = 'pip install --only-binary=:all: "winrt-Windows.Media.Control[all]"'
VOLUME_FIX = "pip install --only-binary=:all: pycaw"

FORCE_MEDIA_KEYS = False  # set by --media-keys

MediaManager = Buffer = DataReader = InputStreamOptions = None
BINDING = None
BINDING_ERRORS = []

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
    except Exception as exc:  # not installed, incomplete install, or not Windows
        BINDING_ERRORS.append(f"{_package}: {type(exc).__name__}: {exc}")

HAVE_WINRT = BINDING is not None

PYCAW_ERROR = None
try:
    from ctypes import POINTER, cast as ctypes_cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    HAVE_PYCAW = True
except Exception as exc:  # pragma: no cover
    HAVE_PYCAW = False
    PYCAW_ERROR = f"{type(exc).__name__}: {exc}"


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
def init_apartment() -> str:
    """Put the calling thread in the COM multi-threaded apartment.

    This matters more than it looks. WinRT hands an async completion straight
    to a thread in the MTA, but for a single-threaded apartment it posts the
    completion to that thread's Windows message queue instead. This thread
    runs an asyncio event loop, not a window message pump, so in an STA
    nothing ever dispatches the completion and every await hangs forever.
    comtypes.CoInitialize(), which this used to call, creates exactly that STA.
    """
    if not IS_WINDOWS:
        return "n/a (not Windows)"

    for module in ("winrt.runtime", "winsdk._winrt", "winsdk.system"):
        try:
            __import__(module, fromlist=["x"]).init_apartment()
            break
        except Exception:
            continue

    # Also the verification: CoInitializeEx reports RPC_E_CHANGED_MODE if the
    # thread is already an STA, and otherwise joins/creates the MTA itself.
    try:
        import ctypes

        COINIT_MULTITHREADED = 0x0
        RPC_E_CHANGED_MODE = 0x80010106
        hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED) & 0xFFFFFFFF
        return "STA - async calls will hang" if hr == RPC_E_CHANGED_MODE else "MTA"
    except Exception as exc:
        return f"unknown ({type(exc).__name__}: {exc})"


class Worker:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.apartment = "?"
        self._ready = threading.Event()
        threading.Thread(target=self._run, name="winrt-worker", daemon=True).start()
        self._ready.wait(5)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.apartment = init_apartment()
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
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            future.cancel()
            raise


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
# Which art key belongs to which track, so a state read does not reopen and
# reread the whole thumbnail every time.
_track_art: dict[tuple, str] = {}


async def _get_manager():
    global _manager
    if _manager is None:
        _manager = await asyncio.wait_for(MediaManager.request_async(), 5)
    return _manager


class Changes:
    """A counter bumped whenever the playback state moves, so a request can
    wait on it instead of the page polling on a timer.
    """

    def __init__(self) -> None:
        self.version = 0
        self.live = False  # True once the watcher is running
        self.last_wanted = 0.0  # monotonic time a page last asked
        self._cond = threading.Condition()

    def bump(self, *_) -> None:
        with self._cond:
            self.version += 1
            self._cond.notify_all()

    def wait(self, since: int, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self.version == since:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self.version


changes = Changes()
WATCH_INTERVAL = 0.25  # seconds between looks while a page is open
WATCH_IDLE_AFTER = 15  # stop looking this long after the last page request


async def _fingerprint() -> tuple:
    """The cheap, synchronous parts of the state that show a change.

    Deliberately not WinRT event subscriptions. Their handlers arrive on
    Windows' own threads and need the GIL, which the worker holds while it is
    blocked inside a call to the same session - after a pause that stalled the
    worker, and every command queued behind it, until something timed out.
    Reading the state on the worker itself cannot collide that way.
    """
    parts = []
    session = await _current_session()
    if session is not None:
        try:
            info = session.get_playback_info()
            c = info.controls
            parts += [session.source_app_user_model_id, int(info.playback_status),
                      c.is_play_enabled, c.is_pause_enabled, c.is_next_enabled,
                      c.is_previous_enabled, c.is_playback_position_enabled]
        except Exception:
            parts.append("?")
        try:
            # A new track or a seek shows up as a new length or a fresh update.
            timeline = session.get_timeline_properties()
            parts += [timeline.end_time.total_seconds(), timeline.last_updated_time.timestamp()]
        except Exception:
            pass
    volume = read_volume()
    parts += [volume["level"], volume["muted"]]
    return tuple(parts)


async def watch_changes() -> None:
    last, idle = None, True
    while True:
        if time.monotonic() - changes.last_wanted > WATCH_IDLE_AFTER:
            idle = True
            await asyncio.sleep(1)
            continue
        if idle:
            # Nothing was watched meanwhile, so a returning page's version
            # proves nothing: wake it and start comparing from here.
            idle, last = False, None
            changes.bump()
        try:
            now = await asyncio.wait_for(_fingerprint(), 2)
        except Exception:
            now = None
        if last is not None and now != last:
            changes.bump()
        last = now
        await asyncio.sleep(WATCH_INTERVAL)


def start_watching() -> None:
    asyncio.run_coroutine_threadsafe(watch_changes(), worker.loop)
    changes.live = True


def _session_rank(session) -> int:
    """Playing beats paused beats everything else."""
    if session is None:
        return -1
    try:
        return {4: 3, 5: 2}.get(int(session.get_playback_info().playback_status), 1)
    except Exception:
        return 0


async def _current_session():
    """The session worth showing.

    Windows promotes whichever app last played to "current", which is almost
    always the right answer and matches what the volume overlay shows. Only
    when that session has closed or stopped do we scan the other registered
    sessions for one that is playing.
    """
    if not HAVE_WINRT:
        return None
    try:
        manager = await _get_manager()
    except Exception:
        return None

    current = manager.get_current_session()
    # Preferring *any* playing session over the current one lets a stale
    # session that still claims to be playing hijack the display, which showed
    # up as the pause button never flipping back after pausing a browser tab.
    try:
        if current is not None and int(current.get_playback_info().playback_status) not in (
            0,  # closed
            3,  # stopped
        ):
            return current
    except Exception:
        pass

    best, best_rank = current, _session_rank(current)
    try:
        for candidate in manager.get_sessions():
            rank = _session_rank(candidate)
            if rank > best_rank:
                best, best_rank = candidate, rank
    except Exception:
        pass
    return best


async def list_sessions() -> list:
    """Every registered session and what it claims to support.

    Apps vary wildly in how much of the media-session contract they implement,
    so when a button does nothing this is the first thing to look at.
    """
    rows = []
    try:
        manager = await _get_manager()
        current = manager.get_current_session()
        current_id = getattr(current, "source_app_user_model_id", None)
        for session in manager.get_sessions():
            row = {"app": "?", "status": "?", "current": False, "title": None,
                   "controls": {}, "duration": None}
            try:
                row["app"] = session.source_app_user_model_id
                row["current"] = row["app"] == current_id
            except Exception:
                pass
            try:
                info = session.get_playback_info()
                row["status"] = PLAYBACK_STATUS.get(int(info.playback_status), "?")
                c = info.controls
                row["controls"] = {
                    "play": bool(c.is_play_enabled),
                    "pause": bool(c.is_pause_enabled),
                    "next": bool(c.is_next_enabled),
                    "prev": bool(c.is_previous_enabled),
                    "seek": bool(c.is_playback_position_enabled),
                }
            except Exception:
                pass
            try:
                props = await asyncio.wait_for(session.try_get_media_properties_async(), 3)
                row["title"] = props.title or None
            except Exception:
                pass
            try:
                row["duration"] = round(session.get_timeline_properties().end_time.total_seconds(), 1)
            except Exception:
                pass
            rows.append(row)
    except Exception:
        pass
    return rows


async def _session_count() -> int:
    try:
        manager = await _get_manager()
        return len(list(manager.get_sessions()))
    except Exception:
        return 0


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
                _track_art.clear()
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
        props = await asyncio.wait_for(session.try_get_media_properties_async(), 5)
        state["title"] = props.title or None
        state["artist"] = props.artist or None
        state["album"] = props.album_title or None
        track = (state["app"], state["title"], state["artist"], state["album"])
        state["art"] = _track_art.get(track)
        if state["art"] is None:
            # Only a found cover is remembered, so a late one still turns up.
            state["art"] = _cache_art(await _thumbnail_bytes(props))
            if state["art"] is not None:
                if len(_track_art) > 8:
                    _track_art.clear()
                _track_art[track] = state["art"]
    except Exception:
        pass

    try:
        timeline = session.get_timeline_properties()
        position = timeline.position.total_seconds()
        duration = timeline.end_time.total_seconds()
        if state["status"] == "playing":
            # Position only changes on events, so extrapolate to "now". Some
            # players (browsers especially) go a long time between updates; if
            # the last one predates the whole track the reading is stale rather
            # than merely old, so leave it alone instead of running off the end.
            updated = timeline.last_updated_time
            if updated and updated.timestamp() > 0:
                elapsed = max(0.0, time.time() - updated.timestamp())
                if elapsed <= duration:
                    position += elapsed
        if duration > 0:
            state["position"] = round(min(position, duration), 1)
            state["duration"] = round(duration, 1)
    except Exception:
        pass

    return state


async def do_command(action: str) -> str | None:
    """Transport control. Returns which route worked, or None if neither did.

    Prefers the media session, since it addresses one specific app. Media keys
    go to whatever Windows decides, which is a blunter instrument but reaches
    apps whose session ignores commands.
    """
    session = None if FORCE_MEDIA_KEYS else await _current_session()
    if session is not None:
        try:
            # An explicit play/pause is honoured by more apps than the toggle,
            # so aim for the state we want and keep the toggle as a backup.
            if action == "playpause":
                playing = int(session.get_playback_info().playback_status) == 4
                attempts = [session.try_pause_async if playing else session.try_play_async,
                            session.try_toggle_play_pause_async]
            else:
                attempts = [{
                    "play": session.try_play_async,
                    "pause": session.try_pause_async,
                    "next": session.try_skip_next_async,
                    "prev": session.try_skip_previous_async,
                    "stop": session.try_stop_async,
                }[action]]
            for attempt in attempts:
                if await asyncio.wait_for(attempt(), 4):
                    return "session"
        except Exception:
            pass

    key = {"play": "playpause", "pause": "playpause"}.get(action, action)
    return "mediakey" if tap_key(key) else None


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
    """The master volume of the default output device - the Windows volume.

    This is the endpoint volume, the same one the tray slider and the volume
    keys move. It is deliberately not a per-application session volume.
    """
    global _endpoint
    if _endpoint is not None:
        return _endpoint

    speakers = AudioUtilities.GetSpeakers()
    if hasattr(speakers, "EndpointVolume"):
        # pycaw >= 2023 returns an AudioDevice wrapper that activates for us
        _endpoint = speakers.EndpointVolume
    else:
        # older pycaw handed back the raw IMMDevice pointer
        interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        _endpoint = ctypes_cast(interface, POINTER(IAudioEndpointVolume))
    return _endpoint


def read_volume() -> dict:
    if not HAVE_PYCAW:
        return {"available": False, "level": None, "muted": None,
                "error": PYCAW_ERROR or "pycaw not installed"}
    try:
        endpoint = _volume_endpoint()
        return {
            "available": True,
            "level": round(endpoint.GetMasterVolumeLevelScalar() * 100),
            "muted": bool(endpoint.GetMute()),
        }
    except Exception as exc:
        # Installed but not usable - say why instead of just greying the slider.
        return {"available": False, "level": None, "muted": None,
                "error": f"{type(exc).__name__}: {exc}"}


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


TIMEOUT_STATE = {
    "available": False,
    "backend": "timeout",
    "status": "closed",
    "title": None,
    "artist": None,
    "album": None,
    "app": None,
    "position": None,
    "duration": None,
    "art": None,
    "controls": {},
    "error": "The Windows media session is not responding.",
}


HOLD_SECONDS = 10  # the watcher catches changes; this is only a heartbeat


@app.get("/api/state")
@protected
def api_state():
    # ?since=<version> holds the request until the watcher sees a change, so
    # the page hears about it within a quarter second.
    changes.last_wanted = time.monotonic()
    since = request.args.get("since", type=int)
    if since is not None and changes.live:
        changes.wait(since, HOLD_SECONDS)
    version = changes.version  # before reading, so a change mid-read is not lost

    # A wedged media session must not take the whole page down: the phone is
    # still talking to the PC, so answer 200 and say what is wrong.
    try:
        state = worker.call(read_state, timeout=4)
    except FutureTimeout:
        app.logger.warning("media session read timed out (apartment: %s)", worker.apartment)
        state = dict(TIMEOUT_STATE)
    try:
        state["volume"] = worker.call(read_volume, timeout=4)
        state["sessions"] = worker.call(_session_count, timeout=4)
    except FutureTimeout:
        state["volume"] = {"available": False, "level": None, "muted": None}
    state["time"] = time.time()
    state["version"] = version
    state["live"] = changes.live
    return jsonify(state)


@app.post("/api/command")
@protected
def api_command():
    action = (request.get_json(silent=True) or {}).get("action", "")
    if action not in {"playpause", "play", "pause", "next", "prev", "stop"}:
        return jsonify({"ok": False, "error": "unknown action"}), 400
    try:
        via = worker.call(do_command, action)
        return jsonify({"ok": via is not None, "via": via})
    except FutureTimeout:
        app.logger.warning("%s command timed out (worker busy or media session stuck)", action)
        return jsonify({"ok": False, "error": "timed out"})


@app.post("/api/seek")
@protected
def api_seek():
    body = request.get_json(silent=True) or {}
    try:
        position = float(body.get("position"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad position"}), 400
    try:
        return jsonify({"ok": bool(worker.call(do_seek, position))})
    except FutureTimeout:
        return jsonify({"ok": False, "error": "timed out"})


@app.post("/api/volume")
@protected
def api_volume():
    body = request.get_json(silent=True) or {}
    try:
        if "mute" in body:
            ok = worker.call(set_mute, body["mute"])
        elif "delta" in body:
            ok = worker.call(nudge_volume, float(body["delta"]))
        elif "level" in body:
            ok = worker.call(set_volume, float(body["level"]))
        else:
            return jsonify({"ok": False, "error": "nothing to do"}), 400
        return jsonify({"ok": bool(ok), "volume": worker.call(read_volume, timeout=4)})
    except FutureTimeout:
        return jsonify({"ok": False, "error": "timed out"})


@app.get("/api/sessions")
@protected
def api_sessions():
    try:
        return jsonify({"sessions": worker.call(list_sessions, timeout=10)})
    except FutureTimeout:
        return jsonify({"sessions": [], "error": "timed out"})


@app.get("/api/art/<key>")
@protected
def api_art(key):
    with _art_lock:
        data = _art_cache.get(key)
    if not data:
        abort(404)
    return app.response_class(data, mimetype="image/jpeg", headers={"Cache-Control": "max-age=3600"})


class _QuietPolls(logging.Filter):
    """Keep successful state polls out of the console without hiding errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not ("/api/state" in message and " 200 " in message)


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
    parser.add_argument("--media-keys", action="store_true",
                        help="always drive playback with virtual media keys instead of "
                             "the media session; for apps whose session ignores commands")
    parser.add_argument("--check", action="store_true",
                        help="report what this environment supports and exit; "
                             "exit code 0 = full features, 1 = media keys only")
    args = parser.parse_args()

    global FORCE_MEDIA_KEYS
    FORCE_MEDIA_KEYS = args.media_keys

    media_note = f"Windows media session (via {BINDING})" if HAVE_WINRT else \
        "media keys only - no track info (see README: 'No wheel for your Python')"
    volume_note = "system master volume" if HAVE_PYCAW else "media keys only (pycaw not installed)"

    if args.check:
        # Actually call both backends. Importing a package proves nothing about
        # whether it works, which is how the last two faults stayed invisible.
        print(f"  python: {sys.version.split()[0]} on {sys.platform}")
        print(f"  com   : {worker.apartment} apartment")

        print(f"  media : {media_note}")
        for line in BINDING_ERRORS:
            print(f"          tried {line}")
        if not HAVE_WINRT and IS_WINDOWS:
            print(f"          fix:   {MEDIA_FIX}")

        media_ok = HAVE_WINRT
        if HAVE_WINRT:
            try:
                state = worker.call(read_state, timeout=10)
                rows = worker.call(list_sessions, timeout=15)
                print(f"          {len(rows)} media session(s) registered")
                for row in rows:
                    flags = "".join(k[0] if v else "-" for k, v in sorted(row["controls"].items()))
                    marker = "*" if row["current"] else " "
                    title = row["title"] or "no title"
                    print(f'         {marker} {row["app"]}  [{row["status"]}] '
                          f'{flags}  dur={row["duration"]}  "{title}"')
                if rows:
                    print("           * = the session Windows calls current; flags are")
                    print("             next/pause/play/prev/seek, letter = supported")
                if state["available"]:
                    who = state["app"] or "unknown app"
                    what = state["title"] or "no title reported"
                    print(f'          reading: {who} - "{what}" [{state["status"]}]')
                else:
                    print("          no app is currently registered as playing media")
                started = time.monotonic()
                worker.call(_fingerprint, timeout=10)
                took = (time.monotonic() - started) * 1000
                print(f"          change check: {took:.0f} ms (runs every {WATCH_INTERVAL * 1000:.0f} ms)")
            except Exception as exc:
                media_ok = False
                print(f"          FAILED: {type(exc).__name__}: {exc}")

        print(f"  volume: {volume_note}")
        if PYCAW_ERROR:
            print(f"          tried pycaw: {PYCAW_ERROR}")
            if IS_WINDOWS:
                print(f"          fix:   {VOLUME_FIX}")

        volume_ok = False
        try:
            vol = worker.call(read_volume, timeout=10)
            volume_ok = vol["available"]
            if volume_ok:
                print(f"          reading: {vol['level']}%, muted={vol['muted']}")
            elif vol.get("error"):
                print(f"          FAILED: {vol['error']}")
        except Exception as exc:
            print(f"          FAILED: {type(exc).__name__}: {exc}")

        return 0 if (media_ok and volume_ok) else 1

    if args.token == "generate":
        args.token = secrets.token_urlsafe(8)
    app.config["TOKEN"] = args.token

    suffix = f"?t={args.token}" if args.token else ""
    start_watching()

    print("webremote")
    print(f"  media : {media_note}")
    print(f"  volume: {volume_note}")
    print(f"  com   : {worker.apartment} apartment")
    print()
    print(f"  local : http://127.0.0.1:{args.port}/{suffix}")
    print(f"  phone : http://{local_ip()}:{args.port}/{suffix}")
    print()
    print("  Ctrl+C to stop.")
    logging.getLogger("werkzeug").addFilter(_QuietPolls())

    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
