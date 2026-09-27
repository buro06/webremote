"""
webremote - control whatever is playing on this Windows PC from a browser.

    python webremote.py              serve on http://<this-pc>:8765
    python webremote.py --token      also require a random access token in the URL
    python webremote.py --check      report which features work here, then exit
    python webremote.py --demo       simulated player, to try the page on any OS

Playback control, track info and seeking use the Windows media session API
(the one behind the volume-key overlay), so it works with Spotify, browsers,
VLC, Media Player and anything else that reports what it is playing. Volume is
the system master volume, via pycaw.

Both are optional. Without them the remote falls back to virtual media keys:
the buttons still work, but there is no track info, seek bar or volume slider.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import inspect
import json
import queue
import secrets
import socket
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

IS_WINDOWS = sys.platform == "win32"
HERE = Path(__file__).resolve().parent

TICK = 0.25  # seconds between change checks while a page is open
FULL_REFRESH = 3.0  # re-read everything at least this often, changed or not
HEARTBEAT = 5.0  # longest silence on an open page's stream; its online light relies on it


# --------------------------------------------------------------------------
# optional dependencies
# --------------------------------------------------------------------------
if IS_WINDOWS:
    # comtypes (under pycaw) initialises COM on import. Make that the
    # multithreaded apartment too, so no thread ends up in an STA by accident.
    sys.coinit_flags = 0

SessionManager = Buffer = DataReader = InputStreamOptions = None
MEDIA_BINDING = None
MEDIA_ERRORS: list[str] = []

# pywinrt ("winrt") is the maintained binding; winsdk is its predecessor and
# exposes the same API, so either will do.
for _package in ("winrt", "winsdk"):
    try:
        _control = importlib.import_module(f"{_package}.windows.media.control")
        _streams = importlib.import_module(f"{_package}.windows.storage.streams")
        SessionManager = _control.GlobalSystemMediaTransportControlsSessionManager
        Buffer = _streams.Buffer
        DataReader = _streams.DataReader
        InputStreamOptions = _streams.InputStreamOptions
        MEDIA_BINDING = _package
        break
    except Exception as exc:
        MEDIA_ERRORS.append(f"{_package}: {type(exc).__name__}: {exc}")

VOLUME_ERROR = None
try:
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
except Exception as exc:
    VOLUME_ERROR = f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# virtual media keys - the fallback for everything
# --------------------------------------------------------------------------
VK = {"playpause": 0xB3, "next": 0xB0, "prev": 0xB1, "mute": 0xAD, "voldown": 0xAE, "volup": 0xAF}


def tap_key(name: str) -> bool:
    if not IS_WINDOWS:
        return False
    import ctypes

    ctypes.windll.user32.keybd_event(VK[name], 0, 0, 0)
    ctypes.windll.user32.keybd_event(VK[name], 0, 2, 0)  # KEYEVENTF_KEYUP
    return True


# --------------------------------------------------------------------------
# the worker thread
#
# Every WinRT and COM object is created and used on this one thread. The HTTP
# server answers from many threads, and these objects are apartment-bound, so
# requests hand their work over here instead of touching them directly.
# --------------------------------------------------------------------------
def join_mta() -> str:
    """Put the calling thread in the COM multithreaded apartment.

    WinRT delivers async completions straight to MTA threads, but posts them to
    an STA thread's window message queue. This thread runs an asyncio loop, not
    a message pump, so in an STA every awaited WinRT call would hang forever.
    """
    if not IS_WINDOWS:
        return "n/a"
    import ctypes

    hr = ctypes.windll.ole32.CoInitializeEx(None, 0) & 0xFFFFFFFF  # COINIT_MULTITHREADED
    return "STA - media calls will hang" if hr == 0x80010106 else "MTA"  # RPC_E_CHANGED_MODE


class Worker:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.apartment = "?"
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.apartment = join_mta()
            ready.set()
            self.loop.run_forever()

        threading.Thread(target=run, name="media-worker", daemon=True).start()
        ready.wait(5)

    def call(self, fn, *args, timeout: float = 5):
        """Run fn (plain or async) on the worker and wait for its result."""

        async def runner():
            result = fn(*args)
            return await result if inspect.isawaitable(result) else result

        future = asyncio.run_coroutine_threadsafe(runner(), self.loop)
        try:
            return future.result(timeout)
        except FutureTimeout:
            future.cancel()
            raise

    def spawn(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)


# --------------------------------------------------------------------------
# album art
# --------------------------------------------------------------------------
class ArtCache:
    """Recent cover images, served to the page by content hash."""

    LIMIT = 16

    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, str]] = {}
        self._lock = threading.Lock()

    def put(self, data: bytes | None) -> str | None:
        if not data:
            return None
        key = hashlib.sha1(data).hexdigest()[:16]
        with self._lock:
            if key not in self._items:
                if len(self._items) >= self.LIMIT:
                    self._items.pop(next(iter(self._items)))
                self._items[key] = (data, sniff_image_type(data))
        return key

    def get(self, key: str) -> tuple[bytes, str] | None:
        with self._lock:
            return self._items.get(key)


def sniff_image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"<svg"):
        return "image/svg+xml"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


art = ArtCache()


# --------------------------------------------------------------------------
# Windows media session
# --------------------------------------------------------------------------
STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}
PLAYING, PAUSED = 4, 5

KNOWN_APPS = {
    "spotify": "Spotify",
    "chrome": "Chrome",
    "msedge": "Edge",
    "firefox": "Firefox",
    "opera": "Opera",
    "brave": "Brave",
    "vlc": "VLC",
    "zunemusic": "Media Player",
    "zunevideo": "Films & TV",
    "itunes": "iTunes",
    "applemusic": "Apple Music",
    "foobar2000": "foobar2000",
    "musicbee": "MusicBee",
    "tidal": "TIDAL",
    "deezer": "Deezer",
    "plex": "Plex",
}


def app_name(aumid: str | None) -> str | None:
    """A readable name from an app user model id like 'Spotify.exe' or
    'Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic'."""
    if not aumid:
        return None
    lowered = aumid.lower()
    for needle, label in KNOWN_APPS.items():
        if needle in lowered:
            return label
    # Firefox registers under a hash of its install folder.
    if len(aumid) == 16 and all(c in "0123456789ABCDEF" for c in aumid):
        return "Firefox"
    name = aumid.split("!")[-1].replace("\\", "/").split("/")[-1]
    return name[:-4] if name.lower().endswith(".exe") else name


def empty_state(backend: str) -> dict:
    return {
        "backend": backend,
        "active": False,
        "app": None,
        "status": "closed",
        "title": None,
        "artist": None,
        "album": None,
        "art": None,
        "position": None,
        "duration": None,
        "can": {"play": True, "pause": True, "next": True, "prev": True, "seek": False},
    }


class WindowsMedia:
    """Reads and drives the media session Windows considers current.

    Changes are found by polling cheap synchronous reads, deliberately not by
    subscribing to WinRT events: those handlers arrive on Windows' own threads
    and need the GIL, which the worker may hold while blocked in a call to the
    same session, and the two then wait on each other.
    """

    backend = "session"

    def __init__(self) -> None:
        self._manager = None
        self._track_art: dict[tuple, str] = {}

    async def _session(self):
        if self._manager is None:
            self._manager = await asyncio.wait_for(SessionManager.request_async(), 5)
        manager = self._manager

        # Windows promotes whichever app last played to "current", which is
        # nearly always right and matches the volume overlay. Only when that
        # one has closed or stopped, look for another that is still going.
        # (Preferring *any* playing session lets a stale one that never
        # reported its pause hijack the remote.)
        current = manager.get_current_session()
        if current is not None and _status(current) not in (0, 3):
            return current

        best, best_rank = current, _rank(current)
        for session in manager.get_sessions():
            if _rank(session) > best_rank:
                best, best_rank = session, _rank(session)
        return best

    async def fingerprint(self) -> tuple:
        """The parts of the state that are cheap to read and show a change."""
        session = await self._session()
        if session is None:
            return (None,)
        info = session.get_playback_info()
        c = info.controls
        timeline = session.get_timeline_properties()
        return (
            session.source_app_user_model_id,
            int(info.playback_status),
            c.is_play_enabled, c.is_pause_enabled, c.is_next_enabled,
            c.is_previous_enabled, c.is_playback_position_enabled,
            # A new track or a seek shows up as a new length or a fresh update.
            timeline.end_time.total_seconds(),
            _timestamp(timeline.last_updated_time),
        )

    async def state(self) -> dict:
        state = empty_state(self.backend)
        session = await self._session()
        if session is None:
            return state

        state["active"] = True
        state["app"] = app_name(session.source_app_user_model_id)

        info = session.get_playback_info()
        state["status"] = STATUS.get(int(info.playback_status), "unknown")
        c = info.controls
        state["can"] = {
            "play": bool(c.is_play_enabled),
            "pause": bool(c.is_pause_enabled),
            "next": bool(c.is_next_enabled),
            "prev": bool(c.is_previous_enabled),
            "seek": bool(c.is_playback_position_enabled),
        }

        try:
            props = await asyncio.wait_for(session.try_get_media_properties_async(), 3)
            state["title"] = props.title or None
            state["artist"] = props.artist or props.album_artist or None
            state["album"] = props.album_title or None
            state["art"] = await self._art_for(state, props)
        except Exception:
            pass

        try:
            timeline = session.get_timeline_properties()
            start = timeline.start_time.total_seconds()
            duration = timeline.end_time.total_seconds() - start
        except Exception:
            duration = 0
        if duration > 0:
            position = timeline.position.total_seconds() - start
            if state["status"] == "playing":
                # Apps report position only now and then; carry it forward to
                # now. An update older than the whole track is stale rather
                # than merely old, so leave that one alone.
                elapsed = time.time() - _timestamp(timeline.last_updated_time)
                if 0 < elapsed <= duration:
                    position += elapsed
            state["position"] = round(max(0.0, min(position, duration)), 2)
            state["duration"] = round(duration, 2)
        else:
            state["can"]["seek"] = False
        return state

    async def _art_for(self, state: dict, props) -> str | None:
        track = (state["app"], state["title"], state["artist"], state["album"])
        if track in self._track_art:
            return self._track_art[track]
        key = art.put(await _read_thumbnail(props))
        if key is not None:  # remember only a found cover, so a late one still turns up
            if len(self._track_art) > 32:
                self._track_art.clear()
            self._track_art[track] = key
        return key

    async def command(self, action: str) -> str | None:
        """Returns how the command was delivered, or None if it could not be."""
        try:
            session = await self._session()
        except Exception:
            session = None

        if session is not None:
            if action == "playpause":
                # More apps honour an explicit play or pause than the toggle,
                # so ask for the state we want and keep the toggle as backup.
                if _status(session) == PLAYING:
                    attempts = [session.try_pause_async, session.try_toggle_play_pause_async]
                else:
                    attempts = [session.try_play_async, session.try_toggle_play_pause_async]
            else:
                attempts = [{"next": session.try_skip_next_async,
                             "prev": session.try_skip_previous_async}[action]]
            for attempt in attempts:
                try:
                    if await asyncio.wait_for(attempt(), 3):
                        return "session"
                except Exception:
                    pass

        return "key" if tap_key(action) else None

    async def seek(self, seconds: float) -> bool:
        session = await self._session()
        if session is None:
            return False
        start = session.get_timeline_properties().start_time.total_seconds()
        ticks = int((start + max(0.0, seconds)) * 10_000_000)  # 100 ns units
        return bool(await asyncio.wait_for(session.try_change_playback_position_async(ticks), 3))

    async def sessions(self) -> list[dict]:
        """Every registered session, for --check. When a button does nothing
        in some app, this shows what that app claims to support."""
        await self._session()
        current = self._manager.get_current_session()
        current_id = current.source_app_user_model_id if current else None
        rows = []
        for session in self._manager.get_sessions():
            c = session.get_playback_info().controls
            rows.append({
                "app": session.source_app_user_model_id,
                "current": session.source_app_user_model_id == current_id,
                "status": STATUS.get(_status(session), "?"),
                "supports": [name for name, ok in (
                    ("play", c.is_play_enabled), ("pause", c.is_pause_enabled),
                    ("next", c.is_next_enabled), ("prev", c.is_previous_enabled),
                    ("seek", c.is_playback_position_enabled)) if ok],
            })
        return rows


def _status(session) -> int:
    try:
        return int(session.get_playback_info().playback_status)
    except Exception:
        return -1


def _rank(session) -> int:
    """Playing beats paused beats everything else."""
    if session is None:
        return -1
    return {PLAYING: 3, PAUSED: 2}.get(_status(session), 1)


def _timestamp(value) -> float:
    try:
        return value.timestamp()
    except Exception:
        return 0.0


async def _read_thumbnail(props) -> bytes | None:
    ref = getattr(props, "thumbnail", None)
    if ref is None:
        return None
    try:
        stream = await asyncio.wait_for(ref.open_read_async(), 3)
        size = int(stream.size)
        if not 0 < size < 10_000_000:
            return None
        try:
            buf = await asyncio.wait_for(
                stream.read_async(Buffer(size), size, InputStreamOptions.READ_AHEAD), 3)
        finally:
            stream.close()
    except Exception:
        return None

    # How bytes come out of an IBuffer differs between binding versions.
    try:
        return bytes(memoryview(buf))  # pywinrt 2+: buffers support the buffer protocol
    except TypeError:
        pass
    reader = DataReader.from_buffer(buf)
    try:
        out = bytearray(buf.length)  # pywinrt: fills a caller-supplied buffer
        reader.read_bytes(out)
        return bytes(out)
    except TypeError:
        return bytes(reader.read_bytes(buf.length))  # winsdk: returns the bytes


class MediaKeysOnly:
    """No media session binding: send keys and report nothing."""

    backend = "keys" if IS_WINDOWS else "none"

    async def fingerprint(self) -> tuple:
        return ()

    async def state(self) -> dict:
        return empty_state(self.backend)

    async def command(self, action: str) -> str | None:
        return "key" if tap_key(action) else None

    async def seek(self, seconds: float) -> bool:
        return False


# --------------------------------------------------------------------------
# system volume
# --------------------------------------------------------------------------
class WindowsVolume:
    """Master volume of the default output device - the tray slider, not a
    per-app volume."""

    REFRESH = 3.0  # re-acquire the device this often, to follow a switch to headphones

    def __init__(self) -> None:
        self._endpoint = None
        self._acquired = 0.0

    def _device(self):
        if self._endpoint is None or time.monotonic() - self._acquired > self.REFRESH:
            speakers = AudioUtilities.GetSpeakers()
            if hasattr(speakers, "EndpointVolume"):  # pycaw 2023+
                self._endpoint = speakers.EndpointVolume
            else:  # older pycaw hands back the raw IMMDevice
                iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                self._endpoint = cast(iface, POINTER(IAudioEndpointVolume))
            self._acquired = time.monotonic()
        return self._endpoint

    def get(self) -> dict:
        try:
            device = self._device()
            return {"available": True,
                    "level": round(device.GetMasterVolumeLevelScalar() * 100),
                    "muted": bool(device.GetMute())}
        except Exception as exc:
            self._endpoint = None
            return {"available": False, "level": None, "muted": None,
                    "error": f"{type(exc).__name__}: {exc}"}

    def set(self, level: float) -> bool:
        device = self._device()
        device.SetMasterVolumeLevelScalar(max(0.0, min(100.0, level)) / 100, None)
        if level > 0 and device.GetMute():
            device.SetMute(False, None)  # dragging up from silence should be heard
        return True

    def nudge(self, delta: float) -> bool:
        return self.set(self.get()["level"] + delta)

    def mute(self, muted: bool | None) -> bool:
        device = self._device()
        device.SetMute(not device.GetMute() if muted is None else bool(muted), None)
        return True


class VolumeKeysOnly:
    """No pycaw: volume keys work, but there is no level to read or set."""

    def get(self) -> dict:
        return {"available": False, "level": None, "muted": None, "keys": IS_WINDOWS,
                "error": VOLUME_ERROR}

    def set(self, level: float) -> bool:
        return False

    def nudge(self, delta: float) -> bool:
        key = "volup" if delta > 0 else "voldown"
        return all(tap_key(key) for _ in range(max(1, round(abs(delta) / 2))))  # ~2% a press

    def mute(self, muted: bool | None) -> bool:
        return tap_key("mute")  # toggle only; the current state is unknown


# --------------------------------------------------------------------------
# demo backend - a pretend player, for trying the page on any machine
# --------------------------------------------------------------------------
class DemoMedia:
    backend = "demo"
    TRACKS = [
        ("Northern Lights", "Aurora Quartet", "Night Drive", 214, ("#1e3a8a", "#22d3ee")),
        ("Paper Boats", "The Tidewater", "Low Tide", 187, ("#7c2d12", "#fbbf24")),
        ("Glasshouse", "Mira Vale", "Greenroom Sessions", 251, ("#14532d", "#a3e635")),
    ]

    def __init__(self) -> None:
        self.index = 0
        self.playing = True
        self.offset = 0.0
        self.since = time.monotonic()
        self.covers = [art.put(self._cover(*t[4], t[0])) for t in self.TRACKS]

    @staticmethod
    def _cover(dark: str, light: str, title: str) -> bytes:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">'
                f'<defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="{dark}"/>'
                f'<stop offset="1" stop-color="{light}"/></linearGradient></defs>'
                f'<rect width="300" height="300" fill="url(#g)"/>'
                f'<circle cx="210" cy="95" r="48" fill="#fff" opacity=".18"/>'
                f'<text x="24" y="270" font-family="sans-serif" font-size="26" '
                f'font-weight="700" fill="#fff">{title}</text></svg>').encode()

    def _position(self) -> float:
        pos = self.offset + (time.monotonic() - self.since if self.playing else 0.0)
        duration = self.TRACKS[self.index][3]
        if pos >= duration:  # roll on to the next track
            self._load((self.index + 1) % len(self.TRACKS))
            return 0.0
        return pos

    def _load(self, index: int, offset: float = 0.0) -> None:
        self.index, self.offset, self.since = index, offset, time.monotonic()

    async def fingerprint(self) -> tuple:
        self._position()
        return (self.index, self.playing, round(self.offset, 2))

    async def state(self) -> dict:
        title, artist, album, duration, _ = self.TRACKS[self.index]
        state = empty_state(self.backend)
        state.update(active=True, app="Demo Player", title=title, artist=artist, album=album,
                     art=self.covers[self.index], status="playing" if self.playing else "paused",
                     position=round(self._position(), 2), duration=duration)
        state["can"]["seek"] = True
        return state

    async def command(self, action: str) -> str:
        if action == "playpause":
            self._load(self.index, self._position())
            self.playing = not self.playing
        elif action == "next":
            self._load((self.index + 1) % len(self.TRACKS))
        elif action == "prev":
            # Like most players: restart the track, unless it has only just begun.
            back = self._position() < 3
            self._load((self.index - back) % len(self.TRACKS))
        return "demo"

    async def seek(self, seconds: float) -> bool:
        self._load(self.index, max(0.0, min(seconds, self.TRACKS[self.index][3] - 1)))
        return True


class DemoVolume:
    def __init__(self) -> None:
        self.level, self.muted = 40, False

    def get(self) -> dict:
        return {"available": True, "level": self.level, "muted": self.muted}

    def set(self, level: float) -> bool:
        self.level = round(max(0.0, min(100.0, level)))
        self.muted = self.muted and self.level == 0
        return True

    def nudge(self, delta: float) -> bool:
        return self.set(self.level + delta)

    def mute(self, muted: bool | None) -> bool:
        self.muted = (not self.muted) if muted is None else bool(muted)
        return True


# --------------------------------------------------------------------------
# live updates
# --------------------------------------------------------------------------
class Remote:
    """Owns the backends and pushes state to every open page.

    While at least one page is connected, a loop on the worker compares a cheap
    fingerprint every TICK and sends a full snapshot when it moves, so pages
    hear about a pause or a track change within a quarter second. With no page
    open it does nothing.
    """

    def __init__(self, media, volume, worker: Worker) -> None:
        self.media, self.volume, self.worker = media, volume, worker
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._latest: str | None = None
        self._wake: asyncio.Event | None = None
        self._force = False
        worker.spawn(self._watch())

    # -- called from HTTP threads ---------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=8)
        with self._lock:
            self._clients.add(q)
            if self._latest:
                q.put_nowait(self._latest)
        self.refresh()
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(q)

    def refresh(self) -> None:
        """Push a fresh snapshot now rather than at the next change."""
        self._force = True
        if self._wake is not None:
            self.worker.loop.call_soon_threadsafe(self._wake.set)

    def run(self, fn, *args, timeout: float = 5):
        return self.worker.call(fn, *args, timeout=timeout)

    # -- on the worker ----------------------------------------------------
    async def snapshot(self) -> dict:
        try:
            state = await asyncio.wait_for(self.media.state(), 5)
        except Exception as exc:
            # A wedged media session must not take the page down with it.
            state = empty_state(self.media.backend)
            state["error"] = f"Media session not responding ({type(exc).__name__})"
        state["volume"] = self.volume.get()
        return state

    async def _fingerprint(self) -> tuple | None:
        try:
            media = await asyncio.wait_for(self.media.fingerprint(), 2)
        except Exception:
            media = None
        volume = self.volume.get()
        return media, volume["level"], volume["muted"]

    def _publish(self, payload: str) -> None:
        with self._lock:
            self._latest = payload
            for q in self._clients:
                if q.full():  # a page that stopped reading only needs the newest
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                q.put_nowait(payload)

    async def _watch(self) -> None:
        self._wake = asyncio.Event()
        last, last_full = None, 0.0
        while True:
            if not self._clients:
                last = None
                await self._sleep(1.0)
                continue
            now = await self._fingerprint()
            force, self._force = self._force, False
            if force or now != last or time.monotonic() - last_full > FULL_REFRESH:
                self._publish(json.dumps(await self.snapshot()))
                last, last_full = now, time.monotonic()
            await self._sleep(TICK)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()


# --------------------------------------------------------------------------
# web server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "webremote"
    protocol_version = "HTTP/1.1"
    remote: Remote  # set in main()
    token: str | None = None
    verbose = False

    # -- plumbing ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _allowed(self, query: dict) -> bool:
        if not self.token:
            return True
        supplied = (query.get("t") or [None])[0] or self.headers.get("X-Token")
        return bool(supplied) and secrets.compare_digest(supplied, self.token)

    def log_message(self, fmt: str, *args) -> None:
        if self.verbose:
            super().log_message(fmt, *args)

    def log_error(self, fmt: str, *args) -> None:
        super().log_message(fmt, *args)

    # -- routing ----------------------------------------------------------
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if not self._allowed(parse_qs(url.query)):
            return self._send(401, b"Missing or wrong access token.", "text/plain")

        if url.path == "/":
            page = (HERE / "static" / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")
        if url.path == "/api/events":
            return self._events()
        if url.path == "/api/state":
            try:
                return self._json(self.remote.run(self.remote.snapshot, timeout=8))
            except FutureTimeout:
                return self._json({"error": "timed out"}, 504)
        if url.path.startswith("/api/art/"):
            found = art.get(url.path.rsplit("/", 1)[-1])
            if found is None:
                return self._send(404, b"", "text/plain")
            return self._send(200, found[0], found[1], cache="max-age=86400, immutable")
        self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        body = self._body()  # read it regardless, or it corrupts the next request on this connection
        if not self._allowed(parse_qs(url.query)):
            return self._json({"ok": False, "error": "unauthorised"}, 401)
        remote = self.remote
        try:
            if url.path == "/api/command":
                action = body.get("action")
                if action not in ("playpause", "next", "prev"):
                    return self._json({"ok": False, "error": "unknown action"}, 400)
                via = remote.run(remote.media.command, action)
                self._reply(via is not None, via=via)
            elif url.path == "/api/seek":
                position = float(body["position"])
                self._reply(remote.run(remote.media.seek, position))
            elif url.path == "/api/volume":
                v = remote.volume
                if "mute" in body:
                    ok = remote.run(v.mute, body["mute"])
                elif "delta" in body:
                    ok = remote.run(v.nudge, float(body["delta"]))
                elif "level" in body:
                    ok = remote.run(v.set, float(body["level"]))
                else:
                    return self._json({"ok": False, "error": "nothing to do"}, 400)
                self._reply(ok, volume=remote.run(v.get))
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except (KeyError, TypeError, ValueError):
            self._json({"ok": False, "error": "bad request"}, 400)
        except FutureTimeout:
            self._json({"ok": False, "error": "the media session did not answer in time"}, 504)
        except Exception as exc:
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def _reply(self, ok, **extra) -> None:
        self.remote.refresh()
        self._json({"ok": bool(ok), **extra})

    def _events(self) -> None:
        """Server-sent events: one JSON snapshot per change, for as long as
        the page stays open."""
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        q = self.remote.subscribe()
        try:
            self.wfile.write(b"retry: 1500\n\n")
            self.wfile.flush()
            while True:
                try:
                    self.wfile.write(f"data: {q.get(timeout=HEARTBEAT)}\n\n".encode())
                except queue.Empty:
                    # A named event, not an SSE comment: the page never sees
                    # comments, and it needs to hear something regularly to
                    # know the link is still alive.
                    self.wfile.write(b"event: ping\ndata: {}\n\n")
                self.wfile.flush()
        except OSError:  # the page went away
            pass
        finally:
            self.remote.unsubscribe(q)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def lan_address() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))  # no packet is sent; this just picks a route
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def saved_token() -> str:
    """A generated token, reused on every run so a bookmarked URL keeps
    working after a restart. Delete the file to get a new one."""
    path = HERE / ".webremote-token"
    try:
        token = path.read_text().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(6)
    try:
        path.write_text(token + "\n")
    except OSError:
        pass  # still works, just not stable across restarts
    return token


def build(demo: bool, worker: Worker):
    if demo:
        return DemoMedia(), DemoVolume()
    media = WindowsMedia() if MEDIA_BINDING else MediaKeysOnly()
    volume = WindowsVolume() if VOLUME_ERROR is None else VolumeKeysOnly()
    return media, volume


def describe(media, volume) -> list[str]:
    if isinstance(media, DemoMedia):
        return ["media : demo player (nothing on this PC is controlled)"]
    lines = []
    if isinstance(media, WindowsMedia):
        lines.append(f"media : Windows media session (via {MEDIA_BINDING})")
    else:
        lines.append("media : media keys only - no track info or seeking"
                     if IS_WINDOWS else "media : unavailable - not Windows (try --demo)")
        lines += [f"        tried {err}" for err in MEDIA_ERRORS]
    if isinstance(volume, WindowsVolume):
        lines.append("volume: system master volume (via pycaw)")
    else:
        lines.append("volume: volume keys only - no slider" if IS_WINDOWS else "volume: unavailable")
        lines.append(f"        tried pycaw: {VOLUME_ERROR}")
    return lines


def check(remote: Remote) -> int:
    """Exercise every backend for real; an import succeeding proves little."""
    print(f"python: {sys.version.split()[0]} on {sys.platform}, worker in {remote.worker.apartment}")
    for line in describe(remote.media, remote.volume):
        print(line)
    ok = isinstance(remote.media, WindowsMedia) and isinstance(remote.volume, WindowsVolume)

    if isinstance(remote.media, WindowsMedia):
        try:
            rows = remote.run(remote.media.sessions, timeout=10)
            print(f"        {len(rows)} session(s) registered")
            for row in rows:
                mark = "*" if row["current"] else " "
                print(f"      {mark} {row['app']}  [{row['status']}]  supports: "
                      f"{', '.join(row['supports']) or 'nothing'}")
            state = remote.run(remote.media.state, timeout=10)
            if state["active"]:
                print(f"        showing: {state['app']} - {state['title'] or 'untitled'} "
                      f"[{state['status']}], seek {'yes' if state['can']['seek'] else 'no'}")
        except Exception as exc:
            ok = False
            print(f"        FAILED: {type(exc).__name__}: {exc}")

    if isinstance(remote.volume, WindowsVolume):
        vol = remote.run(remote.volume.get)
        if vol["available"]:
            print(f"        reading: {vol['level']}%{' (muted)' if vol['muted'] else ''}")
        else:
            ok = False
            print(f"        FAILED: {vol['error']}")
    return 0 if ok else 1


def main() -> int:
    """Exit codes, which run.bat relies on: 0 = stopped on purpose (Ctrl+C),
    2 = bad arguments or port in use (retrying won't help), anything else = a
    fault worth restarting after."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    parser.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    parser.add_argument("--token", nargs="?", const="", metavar="TOKEN",
                        help="require ?t=TOKEN in the URL; give the flag alone to generate "
                             "one (kept in .webremote-token, so bookmarks survive restarts)")
    parser.add_argument("--demo", action="store_true", help="use a simulated player")
    parser.add_argument("--check", action="store_true",
                        help="report what works here and exit (0 = everything)")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args()

    worker = Worker()
    remote = Remote(*build(args.demo, worker), worker)
    if args.check:
        return check(remote)

    if args.token == "":
        args.token = saved_token()
    Handler.remote, Handler.token, Handler.verbose = remote, args.token, args.verbose

    try:
        server = Server((args.host, args.port), Handler)
    except OSError as exc:
        print(f"Cannot listen on port {args.port}: {exc}. Try --port with another number.")
        return 2  # like argparse's bad-arguments exit: run.bat does not retry these

    suffix = f"/?t={args.token}" if args.token else "/"
    print("webremote")
    for line in describe(remote.media, remote.volume):
        print(f"  {line}")
    print()
    print(f"  on this PC : http://127.0.0.1:{args.port}{suffix}")
    print(f"  on a phone : http://{lan_address()}:{args.port}{suffix}")
    print()
    print("  Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
