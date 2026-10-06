"""Roblox's embedded web pages, shown in a WebKit window of the launcher.

Roblox's macOS client opens web views for signing in with a password (and
its captcha), purchases and account pages. Darling's WebKit is a stub, so the
shim (web_bridge.m) forwards those views over a Unix socket to this bridge,
which shows them with WebKitGTK. The protocol is JSON, one object per line:
the game sends requests ({"op": ...}), this side events ({"event": ...}).
It is the protocol of spidercraft's Roblox Mac Linux Port
(runtime/browser/host.cpp), adapted with their permission.
"""

import json
import os
import socket
import urllib.parse
from collections import deque

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import core  # noqa: E402
from .i18n import _  # noqa: E402

GUEST_PREFIX = "/Volumes/SystemRoot"  # the host filesystem as the game sees it
MAX_FRAME_BYTES = 1024 * 1024
MAX_QUEUE_BYTES = 2 * MAX_FRAME_BYTES
MAX_PENDING = 128
IO_BYTES_PER_TURN = 64 * 1024
IO_MESSAGES_PER_TURN = 32


def _webkit():
    """WebKit and Soup, imported on first use (an optional dependency)."""
    if "X11" in type(Gdk.Display.get_default()).__name__:
        # WebKit's DMA-BUF renderer fails on X11 with NVIDIA's driver.
        os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    gi.require_version("WebKit", "6.0")
    gi.require_version("Soup", "3.0")
    from gi.repository import Soup, WebKit
    return WebKit, Soup


def _web_url(url):
    """Only plain http(s) pages with a host and no credentials are shown."""
    if not isinstance(url, str) or len(url) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname) and "@" not in parts.netloc
    except ValueError:
        return False


def _client_url(url):
    """roblox:// links belong to the running client, never to a browser."""
    lower = url.lower() if isinstance(url, str) else ""
    return (lower.startswith("roblox:") and len(lower) > 7) or \
        (lower.startswith("roblox-player:") and len(lower) > 14)


def _compatible_user_agent(agent):
    """Roblox's user agent, as WebKitGTK accepts it. Apple's WebKit takes
    Roblox's concatenated products; WebKitGTK's parser rejects the second
    slash and keeps its desktop agent. Pages must also pick their macOS
    bridge, not a Linux fallback, so app-tagged agents get a Mac platform."""
    result = agent or ""
    result = result.replace("Roblox/DarwinRobloxApp/", "Roblox/Darwin RobloxApp/")
    if "RobloxApp/" in result:
        result = result.replace("(X11; Linux x86_64)", "(Macintosh; Intel Mac OS X 10_15_7)")
    return result


def _header_ok(name, value):
    if not name or len(name) > 256 or len(value) > 16384:
        return False
    if any(not (c.isalnum() or c in "!#$%&'*+-.^_`|~") for c in name):
        return False
    if any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in value):
        return False
    return name.lower() not in ("host", "content-length", "connection", "transfer-encoding")


class _Page:
    def __init__(self, page_id, view, generation):
        self.id = page_id
        self.view = view
        self.delegate = False
        self.panel_title = ""
        self.generation = generation
        self.signals = []
        self.handlers = set()
        self.scripts = set()
        self.script_bytes = 0


class WebBridge:
    """One socket for the game, one window with a stack of pages."""

    def __init__(self, launcher):
        self.WebKit, self.Soup = _webkit()
        self.launcher = launcher
        self.user_agent = self.WebKit.Settings.new().get_user_agent()
        # Unix socket paths are limited to 104 bytes, prefix included.
        name = f"macoblox-web-{os.getpid()}.sock"
        candidates = [os.path.join(d, name) for d in (GLib.get_user_runtime_dir(), "/tmp") if d]
        fitting = [p for p in candidates if len(GUEST_PREFIX + p) < 104]
        if not fitting:
            raise RuntimeError("socket path too long")
        self.path = fitting[0]
        self.guest_path = GUEST_PREFIX + self.path
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.setblocking(False)
        self.listener.bind(self.path)
        os.chmod(self.path, 0o600)
        self.listener.listen(4)
        self.listen_watch = GLib.io_add_watch(self.listener.fileno(), GLib.PRIORITY_DEFAULT,
                                              GLib.IOCondition.IN, self._accept)
        self.peer = None
        self.peer_watch = 0
        self.write_watch = 0
        self.received = bytearray()
        self.outgoing = deque()
        self.outgoing_bytes = 0
        self.incoming_idle = 0
        self.flush_idle = 0
        self.read_closed = False
        self.generation = 0
        self.stopped = False
        self.async_serial = 0
        self.pending_async = {}
        self.pages = {}
        self.decisions = {}
        self.decision_id = 0
        self.session = None
        self.current = None
        self.window = None

    # ---------------------------------------------------------------- socket
    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self._drop_peer()
        if self.listen_watch:
            GLib.source_remove(self.listen_watch)
            self.listen_watch = 0
        self.listener.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass
        if self.window:
            self.window.destroy()
            self.window = None
        self.session = None

    def _accept(self, _fd, _condition):
        if self.stopped:
            return False
        try:
            client, _address = self.listener.accept()
        except OSError:
            return True
        if self.peer:
            client.close()  # one game at a time
            return True
        client.setblocking(False)
        self.generation += 1
        self.peer = client
        self.received.clear()
        self.read_closed = False
        self.peer_watch = GLib.io_add_watch(client.fileno(), GLib.PRIORITY_DEFAULT,
                                            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                                            self._incoming)
        return True

    def _drop_peer(self):
        self.generation += 1
        if self.incoming_idle:
            GLib.source_remove(self.incoming_idle)
            self.incoming_idle = 0
        if self.flush_idle:
            GLib.source_remove(self.flush_idle)
            self.flush_idle = 0
        if self.peer_watch:
            GLib.source_remove(self.peer_watch)
            self.peer_watch = 0
        if self.write_watch:
            GLib.source_remove(self.write_watch)
            self.write_watch = 0
        if self.peer:
            self.peer.close()
            self.peer = None
        self.received.clear()
        self.outgoing.clear()
        self.outgoing_bytes = 0
        self.read_closed = False
        for number in list(self.decisions):
            self._resolve_decision(number, False)
        for token, (cancellable, _page, _generation) in list(self.pending_async.items()):
            self.pending_async.pop(token, None)
            cancellable.cancel()
        for page in list(self.pages.values()):
            self._close_page(page)
        self.current = None
        if self.window:
            self.window.set_visible(False)

    def _incoming(self, _fd, _condition):
        if self.peer is None:
            return False
        generation = self.generation
        remaining = IO_BYTES_PER_TURN
        try:
            while remaining > 0:
                chunk = self.peer.recv(min(8192, remaining))
                if not chunk:
                    self.read_closed = True
                    break
                self.received += chunk
                remaining -= len(chunk)
                if len(self.received) > MAX_QUEUE_BYTES:
                    self._drop_peer()
                    return False
        except BlockingIOError:
            pass
        except OSError:
            self.read_closed = True
        more = self._process_incoming(generation)
        if generation != self.generation or not self.peer:
            return False
        if not more and self.read_closed:
            self._drop_peer()
            return False
        # Rearm only through an idle turn. A continuously readable descriptor
        # at the default priority must still give GTK's redraw sources a turn.
        self.peer_watch = 0
        self._schedule_incoming(generation)
        return False

    def _process_incoming(self, generation):
        for _index in range(IO_MESSAGES_PER_TURN):
            end = self.received.find(b"\n")
            if end < 0:
                if len(self.received) > MAX_FRAME_BYTES:
                    self._drop_peer()
                return False
            if end + 1 > MAX_FRAME_BYTES:
                self._drop_peer()
                return False
            line = self.received[:end]
            del self.received[:end + 1]
            try:
                message = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(message, dict):
                try:
                    self._handle(message)
                except Exception as error:  # one bad request must not end the bridge
                    print("Embedded web page request failed:", type(error).__name__)
                    self._reply_error(message.get("request"), "The embedded browser request failed")
            if generation != self.generation or not self.peer:
                return False
        return b"\n" in self.received

    def _schedule_incoming(self, generation):
        if not self.incoming_idle:
            self.incoming_idle = GLib.idle_add(self._continue_incoming, generation)

    def _continue_incoming(self, generation):
        self.incoming_idle = 0
        if generation != self.generation or not self.peer:
            return False
        if self._process_incoming(generation):
            self._schedule_incoming(generation)
        elif generation == self.generation and self.read_closed:
            self._drop_peer()
        elif generation == self.generation and self.peer and not self.peer_watch:
            self.peer_watch = GLib.io_add_watch(
                self.peer.fileno(), GLib.PRIORITY_DEFAULT,
                GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR, self._incoming)
        return False

    def send(self, message, generation=None):
        if not self.peer or (generation is not None and generation != self.generation):
            return
        try:
            frame = json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        except (TypeError, ValueError, RecursionError):
            self._drop_peer()
            return
        if (len(frame) > MAX_FRAME_BYTES or len(self.outgoing) >= MAX_PENDING
                or self.outgoing_bytes + len(frame) > MAX_QUEUE_BYTES):
            # Closing the stream lets the guest finish pending callbacks with
            # an error. Silently dropping a reply leaves them waiting forever.
            self._drop_peer()
            return
        self.outgoing.append(frame)
        self.outgoing_bytes += len(frame)
        if not self.write_watch and not self.flush_idle:
            self.write_watch = GLib.io_add_watch(self.peer.fileno(), GLib.PRIORITY_DEFAULT,
                                                 GLib.IOCondition.OUT, self._flush)

    def _flush(self, _fd, _condition):
        remaining = IO_BYTES_PER_TURN
        messages = IO_MESSAGES_PER_TURN
        while self.outgoing and self.peer and remaining > 0 and messages > 0:
            try:
                sent = self.peer.send(self.outgoing[0][:remaining])
            except BlockingIOError:
                return True
            except OSError:
                self._drop_peer()
                return False
            if sent == 0:
                self._drop_peer()
                return False
            self.outgoing[0] = self.outgoing[0][sent:]
            self.outgoing_bytes -= sent
            remaining -= sent
            if not self.outgoing[0]:
                self.outgoing.popleft()
                messages -= 1
        if self.outgoing and self.peer:
            self.write_watch = 0
            self.flush_idle = GLib.idle_add(self._continue_flush, self.generation)
            return False
        self.write_watch = 0
        return False

    def _continue_flush(self, generation):
        self.flush_idle = 0
        if generation == self.generation and self.peer and self.outgoing and not self.write_watch:
            self.write_watch = GLib.io_add_watch(self.peer.fileno(), GLib.PRIORITY_DEFAULT,
                                               GLib.IOCondition.OUT, self._flush)
        return False

    def _event(self, page, kind, **fields):
        if page and not self._active_page(page):
            return
        message = {"view": page.id if page else 0, "event": kind}
        message.update(fields)
        self.send(message)

    def _active_page(self, page):
        return (self.peer is not None and page.view is not None
                and page.generation == self.generation and self.pages.get(page.id) is page)

    def _begin_async(self, number, page=None):
        if not self.peer or (page and not self._active_page(page)):
            return None
        if len(self.pending_async) >= MAX_PENDING:
            self._reply_error(number, "Too many pending browser requests", page.id if page else 0)
            return None
        self.async_serial += 1
        token = self.async_serial
        cancellable = Gio.Cancellable.new()
        self.pending_async[token] = (cancellable, page, self.generation)
        return token, cancellable

    def _finish_async(self, token, reply):
        job = self.pending_async.pop(token, None)
        if not job:
            return
        _cancellable, page, generation = job
        if not page or self._active_page(page):
            self.send(reply, generation)

    def _abort_async(self, token):
        job = self.pending_async.pop(token, None)
        if job:
            job[0].cancel()

    def _reply_error(self, number, reason, page_id=0):
        if isinstance(number, int) and not isinstance(number, bool):
            self.send({"view": page_id, "event": "reply", "request": number, "error": reason})

    # ---------------------------------------------------------------- window
    def _build_window(self):
        WebKit = self.WebKit
        self.window = Adw.Window(title="Roblox", default_width=1100, default_height=800, hide_on_close=True)
        self.window.connect("close-request", self._closed)
        self.title = Adw.WindowTitle(title="Roblox")
        header = Adw.HeaderBar(title_widget=self.title)
        self.back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Back"))
        self.forward = Gtk.Button(icon_name="go-next-symbolic", tooltip_text=_("Forward"))
        self.reload = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Reload"))
        self.back.connect("clicked", lambda *_args: self.current and self.current.view.go_back())
        self.forward.connect("clicked", lambda *_args: self.current and self.current.view.go_forward())
        self.reload.connect("clicked", lambda *_args: self.current and self.current.view.reload())
        for button in (self.back, self.forward, self.reload):
            header.pack_start(button)
        self.done = Gtk.Button(label=_("Back to Roblox"))
        self.done.add_css_class("suggested-action")
        self.done.connect("clicked", lambda *_args: self._return_to_game())
        header.pack_end(self.done)
        self.stack = Gtk.Stack()
        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        self.window.set_content(view)
        data = core.WEB_DATA_DIR
        cache = core.CACHE_DIR / "web"
        data.mkdir(parents=True, exist_ok=True)
        self.session = WebKit.NetworkSession.new(str(data), str(cache))
        self.session.get_cookie_manager().set_persistent_storage(
            str(data / "cookies.sqlite"), WebKit.CookiePersistentStorage.SQLITE)

    def _controls(self):
        page = self.current
        panel = bool(page and page.panel_title)
        for button in (self.back, self.forward, self.reload):
            button.set_visible(page is not None and not panel)
        self.done.set_label(_("Close") if panel else _("Back to Roblox"))
        if page:
            self.back.set_sensitive(page.view.can_go_back())
            self.forward.set_sensitive(page.view.can_go_forward())
            title = page.panel_title if panel else (page.view.get_title() or "")
            self.title.set_title(title or "Roblox")
            uri = page.view.get_uri() if not panel else None
            self.title.set_subtitle((urllib.parse.urlsplit(uri).hostname or "") if uri else "")
        else:
            self.title.set_title("Roblox")
            self.title.set_subtitle("")

    def _closed(self, _window):
        self._return_to_game()
        return True  # hidden, not destroyed

    def _return_to_game(self):
        if self.current:
            self._event(self.current, "closed")
        self.current = None
        if self.window:
            self.window.set_visible(False)

    # ---------------------------------------------------------------- pages
    def _page(self, page_id):
        if page_id in self.pages:
            return self.pages[page_id]
        if len(self.pages) >= 32:
            return None
        WebKit = self.WebKit
        if not self.window:
            self._build_window()
        manager = WebKit.UserContentManager()
        view = WebKit.WebView(network_session=self.session, user_content_manager=manager)
        page = _Page(page_id, view, self.generation)
        self.pages[page_id] = page
        self.stack.add_named(view, str(page_id))
        for signal, callback in (("load-changed", self._load_changed),
                                 ("load-failed", self._load_failed),
                                 ("web-process-terminated", self._terminated),
                                 ("notify::title", self._state), ("notify::uri", self._state),
                                 ("decide-policy", self._policy)):
            page.signals.append((view, view.connect(signal, callback, page)))
        return page

    def _close_page(self, page):
        if self.pages.get(page.id) is not page:
            return
        self.pages.pop(page.id)
        if self.current is page:
            self.current = None
            if self.window:
                self.window.set_visible(False)
        for number, (_decision, owner, _source) in list(self.decisions.items()):
            if owner is page:
                self._resolve_decision(number, False)
        for token, (cancellable, owner, _generation) in list(self.pending_async.items()):
            if owner is page:
                self.pending_async.pop(token, None)
                cancellable.cancel()
        view = page.view
        page.view = None  # invalidate callbacks before stopping the load
        for source, handler in page.signals:
            source.disconnect(handler)
        page.signals.clear()
        if view:
            manager = view.get_user_content_manager()
            for name in page.handlers:
                manager.unregister_script_message_handler(name, None)
            manager.remove_all_scripts()
            view.stop_loading()
            self.stack.remove(view)
        page.handlers.clear()
        page.scripts.clear()
        page.script_bytes = 0

    def _state(self, view, _pspec, page):
        if not self._active_page(page):
            return
        self._event(page, "state", url=view.get_uri() or "", title=view.get_title() or "",
                    back=view.can_go_back(), forward=view.can_go_forward(), loading=view.is_loading())
        if self.current is page:
            self._controls()

    def _load_changed(self, view, event, page):
        self._event(page, "load", stage=int(event))
        self._state(view, None, page)

    def _load_failed(self, _view, _event, _uri, _error, page):
        # Never the URL: sign-in flows may put transient secrets in it.
        self._event(page, "error", message="The embedded page could not be loaded.")
        return False

    def _terminated(self, _view, _reason, page):
        self._event(page, "error", message="The embedded browser process stopped unexpectedly.")

    def _policy(self, view, decision, kind, page):
        if not self._active_page(page):
            decision.ignore()
            return True
        WebKit = self.WebKit
        if kind == WebKit.PolicyDecisionType.RESPONSE:
            return False
        action = decision.get_navigation_action()
        url = action.get_request().get_uri()
        if _client_url(url):
            # Server IDs, follow-user IDs and authentication tickets belong to
            # the running client's URL handler, never to a browser.
            self._event(page, "launch-url", url=url)
            decision.ignore()
            return True
        if not _web_url(url) and url != "about:blank":
            decision.ignore()
            return True
        if kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            view.load_uri(url)
            return True
        if not page.delegate:
            return False
        if len(self.decisions) >= MAX_PENDING:
            decision.ignore()
            self._event(page, "error", message="Too many pending page navigations.")
            return True
        self.decision_id += 1
        number = self.decision_id
        source = GLib.timeout_add_seconds(15, self._expire_decision, number)
        self.decisions[number] = (decision, page, source)
        self._event(page, "navigation", decision=number, url=url, type=int(action.get_navigation_type()))
        return True

    def _expire_decision(self, number):
        entry = self.decisions.pop(number, None)
        if entry:
            entry[0].ignore()
        return False

    def _resolve_decision(self, number, allow):
        entry = self.decisions.pop(number, None)
        if entry:
            decision, page, source = entry
            GLib.source_remove(source)
            decision.use() if allow and self._active_page(page) else decision.ignore()

    def _message(self, _manager, value, page, name):
        if not self._active_page(page):
            return
        text = value.to_json(0)
        body = None
        if text:
            try:
                body = json.loads(text)
            except (ValueError, RecursionError):
                body = None
        self._event(page, "message", name=name, body=body)

    # ---------------------------------------------------------------- requests
    def _handle(self, message):
        op = message.get("op")
        page_id = message.get("view", 0)
        if op == "return-to-game":
            self._return_to_game()
            return
        if op == "attach":
            return  # the game's window: the browser stays a window of its own
        if op == "policy":
            number = message.get("decision")
            if isinstance(number, int) and not isinstance(number, bool):
                self._resolve_decision(number, bool(message.get("allow")))
            return
        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0 or page_id > 1000000:
            self._reply_error(message.get("request"), "Invalid embedded page")
            return
        if op == "close":
            page = self.pages.get(page_id)
            if page:
                self._close_page(page)
            return
        if op == "cookies-get":
            if not self.session:
                self._build_window()
            self._cookies_get(message.get("request"))
            return
        elif op == "cookie-set":
            if not self.session:
                self._build_window()
            self._cookie_set(message.get("request"), message.get("cookie"))
            return
        if op not in ("load", "user-agent", "eval", "back", "forward", "reload", "stop", "handler", "script"):
            self._reply_error(message.get("request"), "Unknown embedded browser operation")
            return
        if op in ("eval", "back", "forward", "reload", "stop") and page_id not in self.pages:
            self._reply_error(message.get("request"), "The embedded page was closed", page_id)
            if message.get("request") is None:
                self.send({"view": page_id, "event": "error", "message": "The embedded page was closed."})
            return
        page = self._page(page_id)
        if not page:
            self._reply_error(message.get("request"), "Too many embedded pages", page_id)
            self.send({"view": page_id, "event": "error", "message": "Too many embedded pages."})
            return
        if op == "load":
            self._load(page, message)
        elif op == "user-agent":
            self._set_user_agent(page, message.get("agent"))
        elif op == "eval":
            self._eval(page, message.get("request"), message.get("script"))
        elif op == "back":
            page.view.go_back()
        elif op == "forward":
            page.view.go_forward()
        elif op == "reload":
            page.view.reload()
        elif op == "stop":
            page.view.stop_loading()
        elif op == "handler":
            self._handler(page, message.get("name"))
        elif op == "script":
            self._script(page, message)

    def _set_user_agent(self, page, agent):
        valid = isinstance(agent, str) and agent and all(32 <= ord(c) <= 126 for c in agent)
        page.view.get_settings().set_user_agent(_compatible_user_agent(agent) if valid else None)

    def _load(self, page, message):
        url = message.get("url")
        if not _web_url(url):
            self._event(page, "error", message="The embedded page URL is invalid.")
            return
        page.delegate = bool(message.get("delegate"))
        page.panel_title = message.get("panelTitle") if isinstance(message.get("panelTitle"), str) else ""
        self._set_user_agent(page, message.get("agent"))
        self.current = page
        self.stack.set_visible_child(page.view)
        self._controls()
        request = self.WebKit.URIRequest.new(url)
        headers = message.get("headers")
        if isinstance(headers, dict):
            fields = request.get_http_headers()
            for name, value in list(headers.items())[:128]:
                if isinstance(name, str) and isinstance(value, str) and _header_ok(name, value):
                    fields.replace(name, value)
        page.view.load_request(request)
        if self._active_page(page):
            self.window.present()
            page.view.grab_focus()

    def _eval(self, page, number, script):
        if not isinstance(script, str):
            self._reply_error(number, "Invalid JavaScript request", page.id)
            return
        job = self._begin_async(number, page)
        if not job:
            return
        token, cancellable = job

        def done(view, result):
            reply = {"view": page.id, "event": "reply", "request": number}
            try:
                value = view.evaluate_javascript_finish(result)
                text = value.to_json(0) if value else None
                if text:
                    reply["value"] = json.loads(text)
            except (GLib.Error, ValueError, RecursionError):
                reply["error"] = "JavaScript evaluation failed"
            self._finish_async(token, reply)

        try:
            page.view.evaluate_javascript(script, -1, None, None, cancellable, done)
        except Exception:
            self._abort_async(token)
            raise

    def _handler(self, page, name):
        if not isinstance(name, str) or not name or len(name) > 128:
            return
        manager = page.view.get_user_content_manager()
        if name in page.handlers:
            return
        if len(page.handlers) >= MAX_PENDING:
            self._event(page, "error", message="Too many page message handlers.")
            return
        if manager.register_script_message_handler(name, None):
            page.handlers.add(name)
            page.signals.append((manager, manager.connect(f"script-message-received::{name}",
                                                        self._message, page, name)))

    def _script(self, page, message):
        WebKit = self.WebKit
        source = message.get("script")
        if not isinstance(source, str):
            return
        script_id = message.get("scriptId")
        if not isinstance(script_id, int) or isinstance(script_id, bool):
            script_id = None
        key = (script_id, source, bool(message.get("mainOnly")), bool(message.get("atEnd")))
        if key in page.scripts:
            return  # loadRequest replays the same configuration on each load
        size = len(source.encode("utf-8"))
        if len(page.scripts) >= MAX_PENDING or page.script_bytes + size > MAX_QUEUE_BYTES:
            self._event(page, "error", message="Too many injected page scripts.")
            return
        script = WebKit.UserScript.new(
            source,
            WebKit.UserContentInjectedFrames.TOP_FRAME if message.get("mainOnly") else WebKit.UserContentInjectedFrames.ALL_FRAMES,
            WebKit.UserScriptInjectionTime.END if message.get("atEnd") else WebKit.UserScriptInjectionTime.START,
            None, None)
        page.view.get_user_content_manager().add_script(script)
        page.scripts.add(key)
        page.script_bytes += size

    def _cookies_get(self, number):
        manager = self.session.get_cookie_manager()
        job = self._begin_async(number)
        if not job:
            return
        token, cancellable = job

        def done(manager, result):
            reply = {"view": 0, "event": "reply", "request": number}
            try:
                cookies = manager.get_all_cookies_finish(result)
                values = []
                for c in cookies:
                    value = {"name": c.get_name(), "value": c.get_value(), "domain": c.get_domain(),
                             "path": c.get_path(), "secure": c.get_secure(), "httpOnly": c.get_http_only()}
                    expires = c.get_expires()
                    if expires:
                        value["expires"] = expires.to_unix()
                    values.append(value)
                reply["value"] = values
            except GLib.Error:
                reply["error"] = "Could not read browser cookies"
            self._finish_async(token, reply)

        try:
            manager.get_all_cookies(cancellable, done)
        except Exception:
            self._abort_async(token)
            raise

    def _cookie_set(self, number, cookie):
        if not isinstance(cookie, dict):
            self._reply_error(number, "Invalid browser cookie")
            return
        name, value, domain = cookie.get("name"), cookie.get("value"), cookie.get("domain")
        path = cookie.get("path") or "/"
        if not all(isinstance(v, str) for v in (name, value, domain, path)) or not name or not domain:
            self._reply_error(number, "Invalid browser cookie")
            return
        soup_cookie = self.Soup.Cookie.new(name, value, domain, path, -1)
        soup_cookie.set_secure(bool(cookie.get("secure")))
        soup_cookie.set_http_only(bool(cookie.get("httpOnly")))
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and not isinstance(expires, bool):
            date = GLib.DateTime.new_from_unix_utc(int(expires))
            if date:
                soup_cookie.set_expires(date)
        job = self._begin_async(number)
        if not job:
            return
        token, cancellable = job

        def done(manager, result):
            reply = {"view": 0, "event": "reply", "request": number}
            try:
                manager.add_cookie_finish(result)
            except GLib.Error:
                reply["error"] = "Could not write browser cookie"
            self._finish_async(token, reply)

        try:
            self.session.get_cookie_manager().add_cookie(soup_cookie, cancellable, done)
        except Exception:
            self._abort_async(token)
            raise
