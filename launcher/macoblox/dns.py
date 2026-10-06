"""Local DNS forwarder for Roblox only.

The shim sends Roblox's lookups to 127.0.0.1:<port> (MACOBLOX_DNS), and this
forwards them over DNS-over-TLS (or plain UDP for a custom server). Some
Roblox image hosts do not resolve through ISP or system resolvers in some
regions, and plain UDP DNS to public resolvers is often tampered with there;
the rest of the system keeps its own DNS."""

import socket
import ssl
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .i18n import _

PROVIDERS = {
    "quad9": ("9.9.9.9", "dns.quad9.net"),
    "cloudflare": ("1.1.1.1", "cloudflare-dns.com"),
    "google": ("8.8.8.8", "dns.google"),
}


# Idle DNS-over-TLS connections are closed by the servers after a while;
# older pooled ones are dropped instead of being tried first.
POOL_IDLE_SECONDS = 20
CACHE_ENTRIES = 2048
# A lookup gets this long upstream, less than the 2.5 s the shim waits for
# an answer (dns_override.c): a server that cannot be reached is answered
# with SERVFAIL, and the shim falls back to Darling's resolver at once.
UPSTREAM_SECONDS = 2.0
# After a failed lookup, SERVFAIL right away for this long instead of
# making every lookup wait for the dead server again.
DOWN_SECONDS = 30
# Include queued work in this limit, so bursts cannot outlive the shim's
# reply deadline behind an unbounded executor queue.
MAX_INFLIGHT_QUERIES = 16
# getaddrinfo has no portable cancellation/timeout API. Custom host names
# use daemon workers with a shared cap, including lookups from stopped
# forwarders, so an unavailable system resolver cannot accumulate threads.
_HOSTNAME_LOOKUPS = threading.BoundedSemaphore(4)


def parse_server(text):
    """(host, port) of a custom DNS server written as 9.9.9.9, 9.9.9.9:53,
    2620:fe::fe, [2620:fe::fe]:53 or a host name. Raises ValueError with a
    message for the user."""
    text = text.strip()
    host, port = text, "53"
    if text.startswith("["):
        host, _bracket, rest = text[1:].partition("]")
        if rest:
            port = rest[1:] if rest.startswith(":") else ""
    elif text.count(":") == 1:
        host, port = text.split(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536 or any(c.isspace() for c in host):
        raise ValueError(_("Custom DNS server must look like 9.9.9.9, 9.9.9.9:53 or [2620:fe::fe]:53"))
    return host, int(port)


def _skip_name(message, position, limit=None, compressed=True):
    """Skip a wire-format name without following compression pointers."""
    limit = len(message) if limit is None else min(limit, len(message))
    start = position
    while position < limit and position - start < 255:
        length = message[position]
        if length == 0:
            return position + 1
        if length & 0xC0:
            if (not compressed or (length & 0xC0) != 0xC0 or position + 2 > limit
                    or ((length & 0x3F) << 8 | message[position + 1]) >= len(message)):
                return None
            return position + 2
        position += length + 1
    return None


def _question_end(message):
    """Offset just past the uncompressed single question, or None."""
    if len(message) < 12 or message[4:6] != b"\0\1":
        return None
    position = _skip_name(message, 12, compressed=False)
    return position + 4 if position is not None and position + 4 <= len(message) else None


def _servfail(query):
    """A SERVFAIL answer to `query`: its ID, opcode, RD bit and question."""
    flags = struct.unpack(">H", query[2:4])[0]
    end = _question_end(query)
    question = query[12:end] if end and end <= len(query) else b""
    return (query[:2] + struct.pack(">HHHHH", 0x8000 | (flags & 0x7900) | 0x0080 | 2,
                                    1 if question else 0, 0, 0, 0) + question)


def _cache_metadata(response):
    """TTL field positions and a conservative lifetime, or None."""
    if len(response) < 12:
        return None
    flags, questions, answers, authority, additional = struct.unpack(">HHHHH", response[2:12])
    if flags & 0x0200 or (flags & 15) not in (0, 3):
        return None  # Truncated replies and errors are not cache entries.
    records = answers + authority + additional
    if questions > (len(response) - 12) // 5 or records > (len(response) - 12) // 11:
        return None
    offset = 12
    for _question in range(questions):
        offset = _skip_name(response, offset)
        if offset is None or offset + 4 > len(response):
            return None
        offset += 4
    fields, lifetimes, negative_lifetimes = [], [], []
    negative = not answers or (flags & 15) == 3
    for record in range(records):
        offset = _skip_name(response, offset)
        if offset is None or offset + 10 > len(response):
            return None
        kind, _class, ttl, length = struct.unpack(">HHIH", response[offset:offset + 10])
        data_start, data_end = offset + 10, offset + 10 + length
        if data_end > len(response):
            return None
        if kind != 41:  # EDNS OPT's TTL field contains flags, not a lifetime.
            fields.append((offset + 4, ttl))
            lifetimes.append(ttl)
        if kind == 6 and answers <= record < answers + authority:
            # Negative caching is bounded by both the SOA TTL and MINIMUM.
            soa = _skip_name(response, data_start, data_end)
            soa = _skip_name(response, soa, data_end) if soa is not None else None
            if soa is None or soa + 20 != data_end:
                return None
            lifetime = min(ttl, struct.unpack(">I", response[soa + 16:data_end])[0])
            negative_lifetimes.append(lifetime)
            if negative:
                fields[-1] = offset + 4, lifetime
        offset = data_end
    if not lifetimes:
        return None
    if negative:
        if not negative_lifetimes:
            return None
        lifetimes.extend(negative_lifetimes)
    return fields, min(lifetimes)


def _response_matches(query, response):
    end = _question_end(query)
    response_end = _question_end(response)
    if not (end is not None and response_end == end and len(response) >= 12
            and response[:2] == query[:2] and response[2] & 0x80
            and (response[2] & 0x78) == (query[2] & 0x78)):
        return False
    position = 12
    while query[position]:
        length = query[position]
        if response[position] != length:
            return False
        for offset in range(position + 1, position + length + 1):
            wanted, got = query[offset], response[offset]
            wanted = wanted + 32 if 65 <= wanted <= 90 else wanted
            got = got + 32 if 65 <= got <= 90 else got
            if wanted != got:
                return False
        position += length + 1
    # Keep the name terminator, QTYPE and QCLASS exact; only label contents
    # are case insensitive, including when a resolver normalizes spelling.
    return response[position:end] == query[position:end]


def _set_timeout(connection, deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("DNS lookup deadline expired")
    connection.settimeout(left)


def _close_connection(connection):
    # shutdown wakes a thread blocked in recv; close alone may leave its
    # syscall waiting on the underlying open file description.
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        connection.close()
    except OSError:
        pass


class DnsForwarder:
    def __init__(self, provider, custom=""):
        if provider in PROVIDERS:
            self.server, self.tls_name = PROVIDERS[provider]
            self.port = 853
        else:
            self.server, self.port = parse_server(custom)
            self.tls_name = None
        self.cache = {}
        self.cache_lock = threading.Lock()
        self.pool = []
        self.pool_lock = threading.Lock()
        self.active_connections = set()
        self.context = ssl.create_default_context()
        self.pending = threading.BoundedSemaphore(MAX_INFLIGHT_QUERIES)
        self.address_lock = threading.Lock()
        self.address_event = threading.Event()
        self.address_resolving = False
        self.down_until = 0.0  # time.monotonic() until which the server counts as down
        self.upstream_address = None  # resolved custom server, (family, address)
        self.running = True
        self.socket = None
        self.executor = None
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.bind(("127.0.0.1", 0))
            self.socket.settimeout(0.5)
            self.address = "127.0.0.1:%d" % self.socket.getsockname()[1]
            self.executor = ThreadPoolExecutor(max_workers=8)
            threading.Thread(target=self._serve, daemon=True).start()
        except BaseException:
            self.stop()
            raise

    def stop(self):
        with self.pool_lock:
            self.running = False
            connections = self.active_connections | {connection for connection, _used in self.pool}
            self.active_connections.clear()
            self.pool.clear()
        self.address_event.set()
        if self.socket is not None:
            _close_connection(self.socket)
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)
        for connection in connections:
            _close_connection(connection)

    def _serve(self):
        try:
            while self.running:
                try:
                    query, client = self.socket.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if len(query) > 12:
                    deadline = time.monotonic() + UPSTREAM_SECONDS
                    if not self.running:
                        break
                    if _question_end(query) is None:
                        self._reply(_servfail(query), client)
                        continue
                    if not self.pending.acquire(blocking=False):
                        self._reply(_servfail(query), client)
                        continue
                    try:
                        future = self.executor.submit(self._answer, query, client, deadline)
                    except RuntimeError:  # stop() shut the executor down meanwhile
                        self.pending.release()
                        break
                    future.add_done_callback(lambda _future: self.pending.release())
        finally:
            self.socket.close()

    def _reply(self, response, client):
        if not self.running:
            return
        try:
            self.socket.sendto(response, client)
        except OSError:
            pass

    def _answer(self, query, client, deadline):
        if not self.running:
            return
        if time.monotonic() >= deadline:
            self._reply(_servfail(query), client)
            return
        key = query[2:]
        now = time.monotonic()
        with self.cache_lock:
            cached = self.cache.get(key)
        if cached and cached[0] > now:
            _expires, stored_at, original, fields = cached
            response = bytearray(original)
            response[:2] = query[:2]
            age = max(0, now - stored_at)
            for offset, ttl in fields:
                struct.pack_into(">I", response, offset, max(0, int(ttl - age)))
        else:
            response = self._resolve(query, deadline)
            if not response:
                # Not cached: the next lookup tries the server again.
                response = _servfail(query)
            else:
                self._cache(key, response, time.monotonic())
        if time.monotonic() >= deadline:
            response = _servfail(query)
        self._reply(response, client)

    def _cache(self, key, response, now):
        metadata = _cache_metadata(response)
        if metadata is None:
            return
        fields, ttl = metadata
        if ttl <= 0:
            return
        ttl = min(ttl, 600)
        with self.cache_lock:
            if len(self.cache) >= CACHE_ENTRIES:
                self.cache = {k: v for k, v in self.cache.items() if v[0] > now}
                if len(self.cache) >= CACHE_ENTRIES:
                    self.cache.clear()
            self.cache[key] = (now + ttl, now, response, fields)

    def _resolve(self, query, deadline):
        """The server's answer, or None when it cannot be had in time."""
        now = time.monotonic()
        if not self.running or now >= deadline or now < self.down_until:
            return None
        try:
            response = self._resolve_tls(query, deadline) if self.tls_name else self._resolve_udp(query, deadline)
        except (OSError, ValueError, struct.error):
            response = None
        if response is None and self.running:
            self.down_until = time.monotonic() + DOWN_SECONDS
        return response

    def _address(self, deadline):
        with self.address_lock:
            if self.upstream_address is not None:
                return self.upstream_address
            if not self.running or time.monotonic() >= deadline:
                return None
            for family in (socket.AF_INET, socket.AF_INET6):
                try:
                    socket.inet_pton(family, self.server)
                except OSError:
                    continue
                address = (self.server, self.port) if family == socket.AF_INET else (self.server, self.port, 0, 0)
                self.upstream_address = family, address
                return self.upstream_address
            if not self.address_resolving:
                if not _HOSTNAME_LOOKUPS.acquire(blocking=False):
                    return None
                self.address_resolving = True
                self.address_event.clear()
                try:
                    threading.Thread(target=self._lookup_address, daemon=True).start()
                except BaseException as error:
                    self.address_resolving = False
                    self.address_event.set()
                    _HOSTNAME_LOOKUPS.release()
                    if isinstance(error, (OSError, RuntimeError)):
                        return None
                    raise
        self.address_event.wait(max(0, deadline - time.monotonic()))
        with self.address_lock:
            return self.upstream_address if self.running and time.monotonic() < deadline else None

    def _lookup_address(self):
        address = None
        try:
            results = socket.getaddrinfo(self.server, self.port, type=socket.SOCK_DGRAM)
            for family, _kind, _proto, _name, candidate in results:
                if family in (socket.AF_INET, socket.AF_INET6):
                    address = family, candidate
                    break
        except (OSError, UnicodeError):
            pass
        finally:
            with self.address_lock:
                if self.running:
                    self.upstream_address = address
                self.address_resolving = False
                self.address_event.set()
            _HOSTNAME_LOOKUPS.release()

    def _track(self, connection):
        with self.pool_lock:
            if self.running:
                self.active_connections.add(connection)
                return True
        _close_connection(connection)
        return False

    def _discard(self, connection):
        with self.pool_lock:
            self.active_connections.discard(connection)
        _close_connection(connection)

    def _resolve_udp(self, query, deadline):
        address = self._address(deadline)
        if address is None:
            return None
        family, address = address
        upstream = socket.socket(family, socket.SOCK_DGRAM)
        if not self._track(upstream):
            return None
        try:
            try:
                _set_timeout(upstream, deadline)
                upstream.connect(address)  # the kernel drops replies from anyone else
            except OSError:
                return None
            # Two tries of up to a second each, within the deadline.
            for _attempt in range(2):
                if not self.running or time.monotonic() >= deadline:
                    break
                attempt_deadline = min(deadline, time.monotonic() + 1.0)
                try:
                    _set_timeout(upstream, attempt_deadline)
                    upstream.send(query)
                    while True:  # skip stray datagrams that are not the answer
                        _set_timeout(upstream, attempt_deadline)
                        response = upstream.recv(4096)
                        # Same ID and the same question: an answer to this query.
                        if _response_matches(query, response):
                            return response
                except OSError:  # timeout, or the port is closed
                    continue
        finally:
            self._discard(upstream)
        return None

    def _connect(self, deadline):
        address = self._address(deadline)
        if address is None:
            raise OSError("DNS server address unavailable")
        family, address = address
        raw = socket.socket(family, socket.SOCK_STREAM)
        if not self._track(raw):
            raise OSError("DNS forwarder stopped")
        connection = None
        try:
            _set_timeout(raw, deadline)
            raw.connect(address)
            # Keep stop() from closing the raw descriptor during the TLS
            # ownership transfer. The handshake itself runs outside the lock.
            with self.pool_lock:
                if not self.running:
                    raise OSError("DNS forwarder stopped")
                connection = self.context.wrap_socket(
                    raw, server_hostname=self.tls_name, do_handshake_on_connect=False)
                self.active_connections.discard(raw)
                self.active_connections.add(connection)
            _set_timeout(connection, deadline)
            connection.do_handshake()
            return connection
        except BaseException:
            self._discard(connection if connection is not None else raw)
            raise

    @staticmethod
    def _read_exact(connection, length, deadline=None):
        data = bytearray()
        while len(data) < length:
            if deadline is not None:
                _set_timeout(connection, deadline)
            chunk = connection.recv(length - len(data))
            if not chunk:
                raise OSError("connection closed")
            data.extend(chunk)
        return bytes(data)

    def _pooled(self):
        """A pooled connection that has not sat idle for too long, or None."""
        now = time.monotonic()
        with self.pool_lock:
            if not self.running:
                return None
            while self.pool:
                connection, used = self.pool.pop()
                if now - used < POOL_IDLE_SECONDS:
                    self.active_connections.add(connection)
                    return connection
                connection.close()
        return None

    def _release(self, connection):
        with self.pool_lock:
            self.active_connections.discard(connection)
            if self.running and len(self.pool) < 4:
                self.pool.append((connection, time.monotonic()))
                return
        _close_connection(connection)

    def _resolve_tls(self, query, deadline):
        # A pooled connection the server has closed fails at once and does
        # not count: only two new connections are tried.
        fresh = 0
        while fresh < 2 and self.running:
            if time.monotonic() >= deadline:
                break
            connection = self._pooled()
            if connection is None:
                fresh += 1
                try:
                    connection = self._connect(deadline)
                except (OSError, ValueError):
                    continue
            try:
                _set_timeout(connection, deadline)
                connection.sendall(struct.pack(">H", len(query)) + query)
                length = struct.unpack(">H", self._read_exact(connection, 2, deadline))[0]
                response = self._read_exact(connection, length, deadline)
                if not _response_matches(query, response):
                    raise OSError("DNS reply does not match query")
            except (OSError, ValueError, struct.error):
                self._discard(connection)
                continue
            self._release(connection)
            return response
        return None


def resolve_a(host, provider="quad9", timeout=5):
    """IPv4 addresses of `host` from one DNS-over-TLS query, for the
    launcher's own downloads when the system resolver fails."""
    deadline = time.monotonic() + timeout
    server, tls_name = PROVIDERS.get(provider, PROVIDERS["quad9"])
    query = struct.pack(">HHHHHH", 0x4d42, 0x0100, 1, 0, 0, 0)
    for label in host.rstrip(".").split("."):
        label = label.encode("idna")
        if not 0 < len(label) <= 63:
            raise ValueError("Invalid DNS name")
        query += bytes([len(label)]) + label
    if len(query) - 12 + 1 > 255:
        raise ValueError("Invalid DNS name")
    query += b"\0" + struct.pack(">HH", 1, 1)
    context = ssl.create_default_context()
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection = None
    try:
        _set_timeout(raw, deadline)
        raw.connect((server, 853))
        connection = context.wrap_socket(raw, server_hostname=tls_name, do_handshake_on_connect=False)
        _set_timeout(connection, deadline)
        connection.do_handshake()
        _set_timeout(connection, deadline)
        connection.sendall(struct.pack(">H", len(query)) + query)
        length = struct.unpack(">H", DnsForwarder._read_exact(connection, 2, deadline))[0]
        response = DnsForwarder._read_exact(connection, length, deadline)
    finally:
        if connection is not None:
            _close_connection(connection)
        _close_connection(raw)
    if not _response_matches(query, response):
        raise OSError("DNS reply does not match query")
    if response[3] & 15 or response[2] & 2:
        return []

    answers = struct.unpack(">H", response[6:8])[0]
    if answers > (len(response) - 12) // 11:
        raise OSError("Malformed DNS reply")
    offset = _question_end(response)
    addresses = []
    for _answer in range(answers):
        offset = _skip_name(response, offset)
        if offset is None or offset + 10 > len(response):
            raise OSError("Malformed DNS reply")
        kind, _class, _ttl, length = struct.unpack(">HHIH", response[offset:offset + 10])
        offset += 10
        if offset + length > len(response):
            raise OSError("Malformed DNS reply")
        if kind == 1 and length == 4:
            addresses.append(socket.inet_ntoa(response[offset:offset + 4]))
        offset += length
    return addresses
