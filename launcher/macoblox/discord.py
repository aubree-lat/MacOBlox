"""Discord Rich Presence for MacOBlox over Discord IPC Unix sockets."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import struct
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger("macoblox.discord")

CLIENT_ID = "1468188794309050523"

CACHE_FILE = (
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    / "MacOBlox"
    / "game_cache.json"
)

_GAME_CACHE: dict[int, dict] = {}
_CACHE_LOCK = threading.RLock()
_CACHE_LIMIT = 512
_CACHE_FILE_LIMIT = 2 * 1024 * 1024
_HTTP_BODY_LIMIT = 1024 * 1024


def _cache_entry(place_id: int, value) -> dict | None:
    if place_id <= 0 or not isinstance(value, dict):
        return None
    universe_id = value.get("universe_id")
    if not isinstance(universe_id, int) or isinstance(universe_id, bool) or universe_id <= 0:
        return None
    name, creator, icon = value.get("name"), value.get("creator", ""), value.get("icon_url")
    if not isinstance(name, str) or not name or len(name) > 1024:
        return None
    if not isinstance(creator, str) or len(creator) > 1024:
        return None
    if icon is not None and (not isinstance(icon, str) or len(icon) > 4096):
        return None
    return {"place_id": place_id, "universe_id": universe_id, "name": name,
            "creator": creator, "icon_url": icon}


def _cached_game_info(place_id: int) -> dict | None:
    with _CACHE_LOCK:
        value = _GAME_CACHE.pop(place_id, None)
        if value is None:
            return None
        _GAME_CACHE[place_id] = value
        return dict(value)


def _load_cache():
    try:
        with CACHE_FILE.open("rb") as source:
            body = source.read(_CACHE_FILE_LIMIT + 1)
        if len(body) > _CACHE_FILE_LIMIT:
            return
        raw = json.loads(body)
        if not isinstance(raw, dict):
            return
        loaded = {}
        for key, value in raw.items():
            try:
                place_id = int(key)
            except (ValueError, TypeError):
                continue
            entry = _cache_entry(place_id, value)
            if entry:
                loaded[place_id] = entry
                if len(loaded) > _CACHE_LIMIT:
                    loaded.pop(next(iter(loaded)))
        with _CACHE_LOCK:
            _GAME_CACHE.clear()
            _GAME_CACHE.update(loaded)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.debug("Failed to load game cache: %s", e)


def _save_cache():
    # Serialize writers and replace the complete file atomically. A failed
    # write must leave the previous cache readable.
    with _CACHE_LOCK:
        temporary = None
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            entries = [(key, (json.dumps(str(key)) + ":" +
                             json.dumps(value, ensure_ascii=False, separators=(",", ":"))).encode("utf-8"))
                       for key, value in _GAME_CACHE.items()]
            size = 2 + sum(len(body) for _, body in entries) + max(0, len(entries) - 1)
            first = 0
            while size > _CACHE_FILE_LIMIT and first < len(entries):
                key, body = entries[first]
                _GAME_CACHE.pop(key, None)
                size -= len(body) + (1 if first + 1 < len(entries) else 0)
                first += 1
            body = b"{" + b",".join(body for _, body in entries[first:]) + b"}"
            with tempfile.NamedTemporaryFile(mode="wb", dir=CACHE_FILE.parent,
                                             prefix=".game_cache-", delete=False) as output:
                temporary = output.name
                output.write(body)
            os.replace(temporary, CACHE_FILE)
            temporary = None
        except Exception as e:
            log.debug("Failed to save game cache: %s", e)
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def _store_game_info(place_id: int, info: dict):
    entry = _cache_entry(place_id, info)
    if entry is None:
        return
    with _CACHE_LOCK:
        _GAME_CACHE.pop(place_id, None)
        _GAME_CACHE[place_id] = entry
        while len(_GAME_CACHE) > _CACHE_LIMIT:
            _GAME_CACHE.pop(next(iter(_GAME_CACHE)))
        _save_cache()


def _public_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read(_HTTP_BODY_LIMIT + 1)
    if len(body) > _HTTP_BODY_LIMIT:
        raise ValueError("Public game metadata response exceeded size limit")
    return json.loads(body)


_load_cache()


def fetch_game_info(place_id: int, universe_id: int | None = None, *, cancelled=None) -> dict | None:
    """Fetch experience title, creator, and icon from public Roblox APIs."""
    cached = _cached_game_info(place_id)
    if cached:
        return cached
    def stopped():
        return cancelled is not None and cancelled()

    try:
        if stopped():
            return None
        if not universe_id:
            data = _public_json(f"https://apis.roblox.com/universes/v1/places/{place_id}/universe")
            universe_id = data.get("universeId")

        if stopped() or not universe_id:
            return None

        data = _public_json(f"https://games.roblox.com/v1/games?universeIds={universe_id}")
        entries = data.get("data", [])
        if stopped() or not entries:
            return None
        entry = entries[0]
        name = entry.get("name", "Roblox")
        creator = entry.get("creator", {}).get("name", "")

        icon_url = None
        try:
            if stopped():
                return None
            idata = _public_json(
                f"https://thumbnails.roblox.com/v1/games/icons?universeIds={universe_id}&returnPolicy=PlaceHolder&size=512x512&format=Png&isCircular=false")
            ientries = idata.get("data", [])
            if ientries and ientries[0].get("imageUrl"):
                icon_url = ientries[0]["imageUrl"]
        except Exception:
            pass

        info = {
            "place_id": place_id,
            "universe_id": universe_id,
            "name": name,
            "creator": creator,
            "icon_url": icon_url,
        }
        if stopped():
            return None
        _store_game_info(place_id, info)
        return _cache_entry(place_id, info)
    except Exception as e:
        log.debug("Failed to fetch game info for place %s: %s", place_id, e)
        return None


class GameActivityTracker:
    """Track a launch with one log reader and one latest-join metadata worker.

    on_change(info, generation) runs on a worker; UI consumers must dispatch to
    their main thread and check is_current_generation before applying it.
    """

    _READ_SIZE = 64 * 1024
    _READS_PER_TURN = 4
    _LINE_LIMIT = 256 * 1024

    def __init__(self, log_path: Path, on_change, *, autostart=True):
        self.log_path = log_path
        self.on_change = on_change
        self.running = True
        self.current_place_id: int | None = None
        self.current_universe_id: int | None = None
        self._condition = threading.Condition(threading.RLock())
        self._stopped = threading.Event()
        self._generation = 0
        self._pending_join = None
        self._started = False
        self._resolve_thread = threading.Thread(target=self._resolve_worker,
                                                name="macoblox-game-metadata", daemon=True)
        self.thread = threading.Thread(target=self._run, name="macoblox-game-log", daemon=True)
        if autostart:
            self.start()

    def start(self):
        with self._condition:
            if self._started or not self.running:
                return
            self._started = True
        try:
            self._resolve_thread.start()
            self.thread.start()
        except RuntimeError:
            self.stop()
            raise

    def stop(self):
        with self._condition:
            self.running = False
            self._generation += 1
            self._pending_join = None
            self._stopped.set()
            self._condition.notify_all()

    def is_current_generation(self, generation):
        with self._condition:
            return self.running and self._generation == generation

    def _emit_locked(self, info):
        if self.running:
            try:
                self.on_change(dict(info) if info else None, self._generation)
            except Exception as error:
                log.debug("Game activity callback failed: %s", error)

    def _join(self, place_id, universe_id):
        cached = _cached_game_info(place_id)
        with self._condition:
            if not self.running or self.current_place_id == place_id:
                return
            self.current_place_id = place_id
            self.current_universe_id = universe_id
            self._generation += 1
            self._pending_join = None
            if cached:
                self._emit_locked(cached)
            else:
                self._emit_locked({"place_id": place_id, "name": "Roblox", "loading": True})
                self._pending_join = (self._generation, place_id, universe_id)
                self._condition.notify_all()

    def _leave(self):
        with self._condition:
            if not self.running or self.current_place_id is None:
                return
            self.current_place_id = None
            self.current_universe_id = None
            self._generation += 1
            self._pending_join = None
            self._condition.notify_all()
            self._emit_locked(None)

    def _resolve_worker(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: not self.running or self._pending_join is not None)
                if not self.running:
                    return
                generation, place_id, universe_id = self._pending_join
                self._pending_join = None
            for attempt in range(6):
                if not self.is_current_generation(generation):
                    break
                info = fetch_game_info(place_id, universe_id,
                                       cancelled=lambda: not self.is_current_generation(generation))
                with self._condition:
                    if not self.running or self._generation != generation:
                        break
                    if info:
                        # Any queued loading callback now describes an older
                        # state, even though the experience itself is unchanged.
                        self._generation += 1
                        self._emit_locked(info)
                        break
                    if attempt < 5:
                        self._condition.wait_for(
                            lambda: not self.running or self._generation != generation,
                            timeout=1.0 + attempt * 0.5)

    def _run(self):
        join_re = re.compile(
            r"!\s*Joining game\s+['\"][^'\"]*['\"]\s+place\s+(\d+)|"
            r"GameJoinUtil::joinGamePost.*BODY:.*[\"']placeId[\"']:\s*(\d+)|"
            r"status code:.*[\"']PlaceId[\"']:\s*(\d+)|"
            r"Report game_join_loadtime:.*placeid:(\d+)",
            re.IGNORECASE
        )
        uid_re = re.compile(
            r"[\"']UniverseId[\"']:\s*(\d+)|"
            r"universeid:(\d+)",
            re.IGNORECASE
        )
        leave_re = re.compile(
            r"leaveUGCGame|returnToLuaApp|stage:LuaApp|destroyCaptureModeDataModelIfExists|Destroying MegaReplicator",
            re.IGNORECASE
        )

        last_pos = 0
        universe_id = None
        file_identity = None
        fragment = b""
        discarding_line = False

        def handle_line(raw_line):
            nonlocal universe_id
            line = raw_line.decode("utf-8", errors="replace")
            um = uid_re.search(line)
            if um:
                universe_id = next(int(group) for group in um.groups() if group)
            jm = join_re.search(line)
            if jm:
                place_id = next(int(group) for group in jm.groups() if group)
                if place_id:
                    self._join(place_id, universe_id)
            elif leave_re.search(line):
                self._leave()
                universe_id = None

        while not self._stopped.is_set():
            try:
                with open(self.log_path, "rb") as f:
                    status = os.fstat(f.fileno())
                    identity = (status.st_dev, status.st_ino)
                    if file_identity != identity or status.st_size < last_pos:
                        if file_identity is not None:
                            self._leave()
                        file_identity = identity
                        last_pos = 0
                        universe_id = None
                        fragment = b""
                        discarding_line = False
                    f.seek(last_pos)
                    for _ in range(self._READS_PER_TURN):
                        if self._stopped.is_set():
                            return
                        chunk = f.read(self._READ_SIZE)
                        if not chunk:
                            break
                        last_pos += len(chunk)
                        if discarding_line:
                            end = chunk.find(b"\n")
                            if end < 0:
                                continue
                            chunk = chunk[end + 1:]
                            discarding_line = False
                        pieces = (fragment + chunk).split(b"\n")
                        fragment = pieces.pop()
                        for raw_line in pieces:
                            if self._stopped.is_set():
                                return
                            if len(raw_line) <= self._LINE_LIMIT:
                                handle_line(raw_line)
                        if len(fragment) > self._LINE_LIMIT:
                            # Consume an oversized line once, then wait for its
                            # delimiter instead of rereading it every poll.
                            fragment = b""
                            discarding_line = True
            except FileNotFoundError:
                pass
            except Exception as e:
                log.debug("Log tracker error: %s", e)
            self._stopped.wait(0.5)


class DiscordRPC:
    """Serialize IPC transactions and coalesce queued presence updates.

    close() is terminal and nonblocking; the sole presence worker clears and
    closes the transport. Synchronous methods also share its transaction lock.
    """

    _TIMEOUT = 2.0
    _FRAME_LIMIT = 1024 * 1024
    _NO_PENDING = object()
    # Activities are keyed by the launcher's PID across connections. Serialize
    # old/new transports so a late close cannot clear a replacement's update.
    _TRANSPORT_LOCK = threading.Lock()
    _OWNER_LOCK = threading.Lock()
    _OWNERS = {}

    def __init__(self, client_id: str = CLIENT_ID):
        self.client_id = client_id
        self.sock: socket.socket | None = None
        self._connected = False
        self._closed = False
        self._condition = threading.Condition()
        self._io_lock = self._TRANSPORT_LOCK
        self._owner_token = object()
        self._pending = self._NO_PENDING
        self._worker = threading.Thread(target=self._presence_worker,
                                        name="macoblox-discord-ipc", daemon=True)
        try:
            self._worker.start()
        except RuntimeError:
            self._closed = True
            raise
        # The worker has no pending activity yet. Publish replacement ownership
        # only after it exists, preserving the old close on startup failure.
        with self._OWNER_LOCK:
            self._OWNERS[self.client_id] = self._owner_token

    @property
    def connected(self):
        with self._condition:
            connected = self._connected and not self._closed
        return connected and self._is_owner()

    @property
    def closed(self):
        return self._is_closed()

    def _is_owner(self):
        with self._OWNER_LOCK:
            return self._OWNERS.get(self.client_id) is self._owner_token

    def _is_closed(self):
        with self._condition:
            return self._closed

    def _find_socket(self) -> list[str]:
        candidates = []
        uid = os.getuid()
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        for base in (runtime, os.environ.get("TMPDIR", "/tmp"), "/tmp"):
            if not base or not os.path.isdir(base):
                continue
            for i in range(10):
                path = os.path.join(base, f"discord-ipc-{i}")
                if os.path.exists(path):
                    candidates.append(path)
        return list(dict.fromkeys(candidates))

    def _read_exact(self, s: socket.socket, length: int, deadline: float) -> bytes:
        buf = bytearray()
        while len(buf) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Discord IPC response timed out")
            s.settimeout(remaining)
            chunk = s.recv(length - len(buf))
            if not chunk:
                raise ConnectionError("Discord IPC response ended early")
            buf.extend(chunk)
        return bytes(buf)

    def _send_frame(self, s: socket.socket, opcode: int, payload: bytes, deadline: float):
        if len(payload) > self._FRAME_LIMIT:
            raise ValueError("Discord IPC request exceeded size limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Discord IPC transaction timed out")
        s.settimeout(remaining)
        s.sendall(struct.pack("<II", opcode, len(payload)) + payload)

    def _response(self, s: socket.socket, deadline: float, *, nonce=None, ready=False):
        # Dispatches and ping frames may arrive between command replies. Keep
        # the entire transaction bounded, including peers sending fragments.
        for _ in range(32):
            opcode, length = struct.unpack("<II", self._read_exact(s, 8, deadline))
            if length > self._FRAME_LIMIT:
                raise ValueError("Discord IPC response exceeded size limit")
            body = self._read_exact(s, length, deadline)
            if opcode == 3:
                self._send_frame(s, 4, body, deadline)
                continue
            if opcode == 4:
                continue
            if opcode != 1:
                raise ConnectionError("Discord IPC closed or returned an invalid opcode")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("Discord IPC response was not an object")
            if ready:
                if data.get("cmd") == "DISPATCH" and data.get("evt") == "READY":
                    return
                if data.get("evt") == "ERROR":
                    raise ConnectionError("Discord rejected IPC handshake")
            elif data.get("nonce") == nonce:
                if data.get("cmd") != "SET_ACTIVITY" or data.get("evt") == "ERROR":
                    raise ConnectionError("Discord rejected presence update")
                return
        raise ConnectionError("Discord IPC response did not match request")

    def _disconnect_locked(self):
        sock, self.sock = self.sock, None
        with self._condition:
            self._connected = False
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def _connect_locked(self) -> bool:
        if self._is_closed() or not self._is_owner():
            return False
        if self._connected and self.sock:
            return True
        for path in self._find_socket():
            if self._is_closed() or not self._is_owner():
                return False
            s: socket.socket | None = None
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                deadline = time.monotonic() + self._TIMEOUT
                s.settimeout(self._TIMEOUT)
                s.connect(path)
                payload = json.dumps({"v": 1, "client_id": self.client_id}).encode("utf-8")
                self._send_frame(s, 0, payload, deadline)
                self._response(s, deadline, ready=True)
                with self._condition:
                    if not self._closed and self._is_owner():
                        self.sock = s
                        self._connected = True
                        log.info("Connected to Discord IPC on %s", path)
                        return True
                s.close()
            except Exception as e:
                log.debug("Failed connecting to %s: %s", path, e)
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
        return False

    def connect(self) -> bool:
        with self._io_lock:
            return self._connect_locked()

    @staticmethod
    def _activity(
        details: str = "Playing Roblox",
        state: str | None = None,
        start_time: float | None = None,
        large_image: str = "macoblox",
        large_text: str = "Mac O’ Blox",
        small_image: str | None = None,
        small_text: str | None = None,
    ) -> dict:
        assets: dict = {
            "large_image": large_image,
            "large_text": large_text,
        }
        if small_image:
            assets["small_image"] = small_image
            if small_text:
                assets["small_text"] = small_text
        activity: dict = {
            "details": details,
            "assets": assets,
        }
        if state:
            activity["state"] = state
        if start_time:
            activity["timestamps"] = {"start": int(start_time)}
        return activity

    def _set_activity_locked(self, activity, *, closing=False) -> bool:
        if not self._is_owner():
            return False
        if not closing:
            if self._is_closed() or not self._connect_locked() or self._is_closed():
                return False
        elif not self._connected or self.sock is None:
            return False
        message = {
            "cmd": "SET_ACTIVITY",
            "args": {
                "pid": os.getpid(),
                "activity": activity,
            },
            "nonce": str(uuid.uuid4()),
        }
        try:
            payload = json.dumps(message).encode("utf-8")
            assert self.sock is not None
            deadline = time.monotonic() + self._TIMEOUT
            self._send_frame(self.sock, 1, payload, deadline)
            self._response(self.sock, deadline, nonce=message["nonce"])
            return True
        except Exception as e:
            log.debug("Failed to send presence: %s", e)
            self._disconnect_locked()
            return False

    def update_presence(self, *args, **kwargs) -> bool:
        """Send synchronously; queue_presence is the nonblocking UI API."""
        activity = self._activity(*args, **kwargs)
        with self._io_lock:
            return self._set_activity_locked(activity)

    def queue_presence(self, **kwargs) -> bool:
        activity = self._activity(**kwargs)
        if not self._is_owner():
            return False
        with self._condition:
            if self._closed:
                return False
            self._pending = activity
            self._condition.notify()
            return True

    def clear_presence(self) -> bool:
        with self._io_lock:
            if self._is_closed() or not self._connected:
                return False
            return self._set_activity_locked(None)

    def _presence_worker(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._pending is not self._NO_PENDING)
                    if self._closed:
                        break
                    activity, self._pending = self._pending, self._NO_PENDING
                with self._io_lock:
                    self._set_activity_locked(activity)
        except Exception as error:
            log.debug("Discord IPC worker stopped: %s", error)
            with self._condition:
                self._closed = True
                self._pending = self._NO_PENDING
        finally:
            with self._io_lock:
                try:
                    self._set_activity_locked(None, closing=True)
                finally:
                    self._disconnect_locked()
            with self._OWNER_LOCK:
                if self._OWNERS.get(self.client_id) is self._owner_token:
                    self._OWNERS.pop(self.client_id, None)

    def close(self):
        with self._condition:
            self._closed = True
            self._pending = self._NO_PENDING
            self._condition.notify_all()
