"""GTK 4 / libadwaita interface of the Mac O’ Blox launcher."""

import json
import os
import re
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402
from pathlib import Path  # noqa: E402

from . import __version__, author, core, discord, dns, i18n, mods, studio, uri as uri_handoff  # noqa: E402
from .i18n import _  # noqa: E402
from .setup import SetupWizard  # noqa: E402

APP_ID = "wtf.aubree.MacOBlox"

# Common fast flags. Roblox only honours flags on its client allowlist, so
# some of these may have no effect in a given client version.
PRESETS = [
    {"title": "FPS limit", "subtitle": "FramerateCap + DFIntTaskSchedulerTargetFps",
     "flag": "DFIntTaskSchedulerTargetFps", "kind": "fps", "default": 144, "min": 30, "max": 1000},
    {"title": "Graphics quality", "subtitle": "DFIntDebugFRMQualityLevelOverride, 1–21",
     "flag": "DFIntDebugFRMQualityLevelOverride", "kind": "number", "default": 10, "min": 1, "max": 21},
    {"title": "MSAA", "subtitle": "FIntDebugForceMSAASamples: 0, 1, 2, 4, 8",
     "flag": "FIntDebugForceMSAASamples", "kind": "number", "default": 4, "min": 0, "max": 8},
    {"title": "No shadows", "subtitle": "FIntRenderShadowIntensity = 0",
     "flag": "FIntRenderShadowIntensity", "kind": "fixed", "value": 0},
    {"title": "No grass", "subtitle": "FIntFRMMinGrassDistance / FIntFRMMaxGrassDistance = 0",
     "flag": ["FIntFRMMinGrassDistance", "FIntFRMMaxGrassDistance"], "kind": "fixed", "value": 0},
    {"title": "Low quality terrain", "subtitle": "FIntTerrainArraySliceSize = 0",
     "flag": "FIntTerrainArraySliceSize", "kind": "fixed", "value": 0},
    {"title": "Texture quality override", "subtitle": "DFIntTextureQualityOverride: 3, 16x AF, No mip skipping",
     "flag": "DFIntTextureQualityOverride", "kind": "number", "default": 3, "min": 0, "max": 3,
     # The override is ignored unless this is set too.
     "also": {
         "DFFlagTextureQualityOverrideEnabled": True,
         "FIntDebugTextureManagerSkipMips": -1,
         "DFIntTextureCompositorLowResFactor": 1,
         "DFFlagTextureCompositorHighQualityEnabled": True,
         "FIntDebugForceAnisotropy": 16,
     }},
    {"title": "Anisotropic filtering (16x)", "subtitle": "FIntDebugForceAnisotropy: 16 (sharp textures at angles)",
     "flag": "FIntDebugForceAnisotropy", "kind": "number", "default": 16, "min": 1, "max": 16},
    {"title": "Force maximum texture resolution", "subtitle": "FIntDebugTextureManagerSkipMips: -1 (never downscale mips)",
     "flag": "FIntDebugTextureManagerSkipMips", "kind": "number", "default": -1, "min": -1, "max": 0},
]

DNS_CHOICES = [
    ("system", "System (Darling default)"),
    ("quad9", "Quad9 (9.9.9.9, encrypted)"),
    ("cloudflare", "Cloudflare (1.1.1.1, encrypted)"),
    ("google", "Google (8.8.8.8, encrypted)"),
    ("custom", "Custom"),
]


def _toast(overlay, text):
    toast = Adw.Toast.new(text)
    # Error texts contain <, > and & (compiler output, paths); as markup they
    # would turn the toast empty.
    toast.set_use_markup(False)
    overlay.add_toast(toast)


def _button_row(title):
    """A clickable row; Adw.ButtonRow needs libadwaita 1.6 (Ubuntu 24.04 has 1.5)."""
    if hasattr(Adw, "ButtonRow"):
        return Adw.ButtonRow(title=title)
    return Adw.ActionRow(title=title, activatable=True)


def _error_dialog(window, heading, details):
    """Shows the whole error text, selectable and with a copy button, so
    people can send it. Also kept in ~/.cache/macoblox/last-error.txt."""
    try:
        core.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (core.CACHE_DIR / "last-error.txt").write_text(f"Mac O’ Blox {__version__}\n{heading}\n\n{details}\n")
    except OSError:
        pass
    dialog = Adw.AlertDialog(heading=heading)
    view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                        top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
    view.get_buffer().set_text(details)
    scroller = Gtk.ScrolledWindow(child=view, min_content_height=160, max_content_height=360,
                                  propagate_natural_height=True)
    scroller.add_css_class("card")
    dialog.set_extra_child(scroller)
    dialog.add_response("copy", _("Copy"))
    dialog.add_response("close", _("Close"))
    dialog.set_default_response("close")

    def response(_dialog, result):
        if result == "copy":
            window.get_clipboard().set(f"Mac O’ Blox {__version__}\n{heading}\n\n{details}")

    dialog.connect("response", response)
    dialog.present(window)


class GameLogsView(Gtk.Box):
    """Live streaming log viewer for Roblox Player with syntax highlighting and search."""

    READ_BYTES = 64 * 1024
    LINES_PER_UPDATE = 200
    LINE_BYTES = 8192
    VIEW_CHARS = 1024 * 1024
    SEARCH_MATCHES = 2048

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.window = window
        self.last_pos = 0
        self.line_count = 0
        self.current_log_path = None
        self.auto_scroll = True
        self.matches = []
        self.current_match_idx = -1
        self._pending = bytearray()
        self._discard_line = False
        self._log_identity = None
        self._drain_id = 0
        self._search_refresh_id = 0
        self._search_limited = False
        self._search_query = ""

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_start(16)
        bar.set_margin_end(16)
        bar.set_margin_top(8)

        self.title_label = Gtk.Label(label=_("Game Logs"), css_classes=["heading"])
        bar.append(self.title_label)

        self.status_label = Gtk.Label(css_classes=["dim-label", "caption"], margin_start=8)
        bar.append(self.status_label)

        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)

        search_btn = Gtk.Button(icon_name="edit-find-symbolic")
        search_btn.add_css_class("flat")
        search_btn.set_tooltip_text(_("Search in logs (Ctrl+F)"))
        search_btn.connect("clicked", lambda *_args: self.toggle_search())
        bar.append(search_btn)

        self.scroll_btn = Gtk.ToggleButton(icon_name="go-bottom-symbolic")
        self.scroll_btn.add_css_class("flat")
        self.scroll_btn.set_tooltip_text(_("Auto-scroll"))
        self.scroll_btn.set_active(True)
        self.scroll_btn.connect("toggled", self._on_scroll_toggled)
        bar.append(self.scroll_btn)

        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        copy_btn.add_css_class("flat")
        copy_btn.set_tooltip_text(_("Copy logs"))
        copy_btn.connect("clicked", self._on_copy_clicked)
        bar.append(copy_btn)

        self.open_btn = Gtk.Button(icon_name="document-edit-symbolic")
        self.open_btn.add_css_class("flat")
        self.open_btn.set_tooltip_text(_("Open in text editor"))
        self.open_btn.connect("clicked", lambda *_args: window.open_external_log(self.current_log_path))
        bar.append(self.open_btn)

        clear_btn = Gtk.Button(icon_name="edit-clear-symbolic")
        clear_btn.add_css_class("flat")
        clear_btn.set_tooltip_text(_("Clear view"))
        clear_btn.connect("clicked", self._on_clear_clicked)
        bar.append(clear_btn)

        self.append(bar)

        # Search revealer bar
        self.search_revealer = Gtk.Revealer(
            reveal_child=False,
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN
        )
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_box.set_margin_start(16)
        search_box.set_margin_end(16)
        search_box.set_margin_top(2)
        search_box.set_margin_bottom(2)

        self.search_entry = Gtk.SearchEntry(hexpand=True)
        self.search_entry.set_placeholder_text(_("Search in logs (Ctrl+F)…"))
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", lambda *_args: self._find_next())
        self.search_entry.connect("stop-search", lambda *_args: self.close_search())

        self.search_count_label = Gtk.Label(css_classes=["dim-label", "caption"], margin_start=6, margin_end=6)

        prev_btn = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("Previous match"))
        prev_btn.add_css_class("flat")
        prev_btn.connect("clicked", lambda *_args: self._find_prev())

        next_btn = Gtk.Button(icon_name="go-down-symbolic", tooltip_text=_("Next match"))
        next_btn.add_css_class("flat")
        next_btn.connect("clicked", lambda *_args: self._find_next())

        close_btn = Gtk.Button(icon_name="window-close-symbolic", tooltip_text=_("Close search"))
        close_btn.add_css_class("flat")
        close_btn.connect("clicked", lambda *_args: self.close_search())

        search_box.append(self.search_entry)
        search_box.append(self.search_count_label)
        search_box.append(prev_btn)
        search_box.append(next_btn)
        search_box.append(close_btn)

        self.search_revealer.set_child(search_box)
        self.append(self.search_revealer)

        self.scrolled = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.scrolled.set_margin_start(16)
        self.scrolled.set_margin_end(16)
        self.scrolled.set_margin_bottom(12)
        self.scrolled.add_css_class("card")

        self.text_view = Gtk.TextView()
        self.text_view.set_monospace(True)
        self.text_view.set_editable(False)
        self.text_view.set_cursor_visible(False)
        self.text_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.text_view.set_left_margin(12)
        self.text_view.set_right_margin(12)
        self.text_view.set_top_margin(12)
        self.text_view.set_bottom_margin(12)
        self.buffer = self.text_view.get_buffer()
        self._end_mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        self._search_anchor = self.buffer.create_mark(None, self.buffer.get_start_iter(), True)

        self.tag_ln = self.buffer.create_tag("log_ln", foreground="#6e6e73")
        self.tag_time = self.buffer.create_tag("log_time", foreground="#77767b")
        self.tag_err = self.buffer.create_tag("log_err", foreground="#ed333b", weight=Pango.Weight.BOLD)
        self.tag_warn = self.buffer.create_tag("log_warn", foreground="#e5a50a", weight=Pango.Weight.SEMIBOLD)
        self.tag_info = self.buffer.create_tag("log_info", foreground="#3584e4")
        self.tag_success = self.buffer.create_tag("log_success", foreground="#33d17a", weight=Pango.Weight.BOLD)
        self.tag_debug = self.buffer.create_tag("log_debug", foreground="#7f848e")
        self.tag_macoblox = self.buffer.create_tag("log_macoblox", foreground="#c061cb", weight=Pango.Weight.BOLD)
        self.tag_match = self.buffer.create_tag("search_match", background="#2a5c9a", foreground="#ffffff")
        self.tag_current = self.buffer.create_tag("search_current", background="#f6d32d", foreground="#000000")
        self._ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}[^\s,]*)(.*)$")

        self.scrolled.set_child(self.text_view)
        self.append(self.scrolled)

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

    def _on_key_pressed(self, _controller, keyval, _keycode, state):
        if (state & Gdk.ModifierType.CONTROL_MASK) and keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.toggle_search()
            return True
        if keyval == Gdk.KEY_Escape and self.search_revealer.get_reveal_child():
            self.close_search()
            return True
        return False

    def toggle_search(self):
        if self.search_revealer.get_reveal_child():
            self.close_search()
        else:
            self.open_search()

    def open_search(self):
        self.search_revealer.set_reveal_child(True)
        self.search_entry.grab_focus()
        if self.search_entry.get_text():
            self._on_search_changed(self.search_entry)

    def close_search(self):
        if self._search_refresh_id:
            GLib.source_remove(self._search_refresh_id)
            self._search_refresh_id = 0
        self.search_revealer.set_reveal_child(False)
        self.buffer.remove_tag(self.tag_match, self.buffer.get_start_iter(), self.buffer.get_end_iter())
        self.buffer.remove_tag(self.tag_current, self.buffer.get_start_iter(), self.buffer.get_end_iter())
        self.search_count_label.set_text("")
        self.matches = []
        self.current_match_idx = -1
        self._search_query = ""
        self.text_view.grab_focus()

    def _on_search_changed(self, entry):
        if self._search_refresh_id:
            GLib.source_remove(self._search_refresh_id)
            self._search_refresh_id = 0
        self._rebuild_search(entry.get_text().strip())

    def _rebuild_search(self, query, preserve=False):
        self._search_limited = False
        anchor = self.buffer.get_iter_at_mark(self._search_anchor).get_offset()
        self._search_query = query
        self.buffer.remove_tag(self.tag_match, self.buffer.get_start_iter(), self.buffer.get_end_iter())
        self.buffer.remove_tag(self.tag_current, self.buffer.get_start_iter(), self.buffer.get_end_iter())
        if not query:
            self.search_count_label.set_text("")
            self.matches = []
            self.current_match_idx = -1
            return

        matches = []
        it = self.buffer.get_start_iter()
        while True:
            res = it.forward_search(query, Gtk.TextSearchFlags.CASE_INSENSITIVE, None)
            if not res:
                break
            if len(matches) == self.SEARCH_MATCHES:
                self._search_limited = True
                break
            s, e = res
            self.buffer.apply_tag(self.tag_match, s, e)
            matches.append(s.get_offset())
            it = e

        self.matches = matches
        if matches:
            self.current_match_idx = (
                min(range(len(matches)), key=lambda i: abs(matches[i] - anchor))
                if preserve else 0
            )
            self._highlight_current_match(query, scroll=not preserve)
        else:
            self.current_match_idx = -1
            self.search_count_label.set_text(_("No matches"))

    def _highlight_current_match(self, query=None, scroll=True):
        if not self.matches or self.current_match_idx < 0:
            return
        if query is None:
            query = self.search_entry.get_text().strip()
        self.buffer.remove_tag(self.tag_current, self.buffer.get_start_iter(), self.buffer.get_end_iter())
        offset = self.matches[self.current_match_idx]
        s = self.buffer.get_iter_at_offset(offset)
        e = self.buffer.get_iter_at_offset(offset + len(query))
        self.buffer.apply_tag(self.tag_current, s, e)
        self.buffer.move_mark(self._search_anchor, s)
        if scroll:
            self.text_view.scroll_to_iter(s, 0.2, False, 0.0, 0.5)
        suffix = "+" if self._search_limited else ""
        self.search_count_label.set_text(f"{self.current_match_idx + 1} / {len(self.matches)}{suffix}")

    def _find_next(self):
        if not self.matches:
            return
        self.current_match_idx = (self.current_match_idx + 1) % len(self.matches)
        self._highlight_current_match()

    def _find_prev(self):
        if not self.matches:
            return
        self.current_match_idx = (self.current_match_idx - 1) % len(self.matches)
        self._highlight_current_match()

    def _insert_highlighted_text(self, text: str):
        # The reader budgets LF-delimited rows. Other Unicode/control
        # separators must not turn one row into thousands of GTK insertions.
        lines = text.split("\n")
        if lines and not lines[-1]:
            lines.pop()
        for line in lines:
            line = line.rstrip("\r")
            self.line_count += 1
            ll = line.lower()
            if "[macoblox]" in ll:
                tag = self.tag_macoblox
            elif any(k in ll for k in ("error", "crash", "fatal", "sigsegv", "exception", "abort")):
                tag = self.tag_err
            elif any(k in ll for k in ("warning", "warn")):
                tag = self.tag_warn
            elif any(k in ll for k in ("! joining game", "game join succeeded", "entered play session")):
                tag = self.tag_success
            elif "info" in ll:
                tag = self.tag_info
            elif any(k in ll for k in ("debug", "flog", "dflog")):
                tag = self.tag_debug
            else:
                tag = None

            end = self.buffer.get_end_iter()
            # Gutter line number prefix
            self.buffer.insert_with_tags(end, f"{self.line_count:5d} │ ", self.tag_ln)

            m = self._ts_re.match(line)
            end = self.buffer.get_end_iter()
            if m:
                self.buffer.insert_with_tags(end, m.group(1), self.tag_time)
                end = self.buffer.get_end_iter()
                rest = m.group(2) + "\n"
                if tag:
                    self.buffer.insert_with_tags(end, rest, tag)
                else:
                    self.buffer.insert(end, rest)
            else:
                if tag:
                    self.buffer.insert_with_tags(end, line + "\n", tag)
                else:
                    self.buffer.insert(end, line + "\n")

    def reset(self, log_path=None):
        if self._drain_id:
            GLib.source_remove(self._drain_id)
            self._drain_id = 0
        self._pending.clear()
        self._discard_line = False
        self._log_identity = None
        self.current_log_path = log_path
        self.last_pos = 0
        self.line_count = 0
        self.buffer.set_text("")
        self.close_search()
        if log_path:
            self.status_label.set_text(log_path.name)
        else:
            self.status_label.set_text("")

    def update(self):
        log_path = None
        if self.window.session and self.window.session.log_path:
            log_path = self.window.session.log_path
        elif self.window.last_log:
            log_path = self.window.last_log

        if not log_path or not log_path.exists():
            return

        if self.current_log_path != log_path:
            self.reset(log_path)

        try:
            with open(log_path, "rb") as f:
                info = os.fstat(f.fileno())
                identity = (info.st_dev, info.st_ino)
                if ((self._log_identity is not None and self._log_identity != identity)
                        or info.st_size < self.last_pos):
                    self.reset(log_path)
                self._log_identity = identity
                f.seek(self.last_pos)
                chunk = f.read(max(0, self.READ_BYTES - len(self._pending)))
                self.last_pos = f.tell()
                self._pending.extend(chunk)
                eof = self.last_pos >= info.st_size
            inserted = False
            for _ in range(self.LINES_PER_UPDATE):
                newline = self._pending.find(b"\n")
                if self._discard_line:
                    if newline < 0:
                        self._pending.clear()
                        break
                    del self._pending[:newline + 1]
                    self._discard_line = False
                    continue
                shortened = newline > self.LINE_BYTES or (newline < 0 and len(self._pending) > self.LINE_BYTES)
                if newline >= 0:
                    line = bytes(self._pending[:min(newline, self.LINE_BYTES)])
                    del self._pending[:newline + 1]
                elif shortened:
                    line = bytes(self._pending[:self.LINE_BYTES])
                    self._pending.clear()
                    self._discard_line = True
                elif eof and self.window.session is None and self._pending:
                    line = bytes(self._pending)
                    self._pending.clear()
                else:
                    break
                text = line.decode("utf-8", errors="replace")
                if shortened:
                    text += " … [open the log file for the full line]"
                # Joining line fragments before decoding preserves split UTF-8
                # characters and avoids inventing a new line for each write.
                self._insert_highlighted_text(text + "\n")
                inserted = True
            if inserted:
                self.status_label.set_text(f"{log_path.name} ({self.line_count} l.)")
                lines = self.buffer.get_line_count()
                if lines > 5000:
                    trim = self.buffer.get_start_iter()
                    trim.forward_lines(lines - 4000)
                    self.buffer.delete(self.buffer.get_start_iter(), trim)
                chars = self.buffer.get_char_count()
                if chars > self.VIEW_CHARS:
                    trim = self.buffer.get_iter_at_offset(chars - self.VIEW_CHARS * 3 // 4)
                    trim.forward_line()
                    self.buffer.delete(self.buffer.get_start_iter(), trim)
                if self.search_revealer.get_reveal_child() and self.search_entry.get_text().strip():
                    # Offsets may have moved after trimming. Invalidate them
                    # immediately, then refresh once per bounded UI interval.
                    self.matches = []
                    self.current_match_idx = -1
                    self.buffer.remove_tag(self.tag_current, self.buffer.get_start_iter(), self.buffer.get_end_iter())
                    if not self._search_refresh_id:
                        self._search_refresh_id = GLib.timeout_add(100, self._refresh_search)
                if self.auto_scroll:
                    self.text_view.scroll_to_mark(self._end_mark, 0.0, False, 0.0, 1.0)
            backlog = (not eof or b"\n" in self._pending or len(self._pending) > self.LINE_BYTES)
            if backlog and not self._drain_id:
                self._drain_id = GLib.idle_add(self._drain_log)
        except Exception:
            pass

    def _drain_log(self):
        self._drain_id = 0
        if self.get_mapped():
            self.update()
        return False

    def _refresh_search(self):
        self._search_refresh_id = 0
        if self.search_revealer.get_reveal_child():
            query = self.search_entry.get_text().strip()
            self._rebuild_search(query, preserve=query == self._search_query)
        return False

    def _on_scroll_toggled(self, btn):
        self.auto_scroll = btn.get_active()
        if self.auto_scroll:
            self.text_view.scroll_to_mark(self._end_mark, 0.0, False, 0.0, 1.0)

    def _on_copy_clicked(self, _btn):
        raw = self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)
        clean = re.sub(r"^\s*\d+\s*│\s*", "", raw, flags=re.MULTILINE)
        if clean:
            Gdk.Display.get_default().get_clipboard().set(clean)
            _toast(self.window.toasts, _("Logs copied to clipboard"))

    def _on_clear_clicked(self, _btn):
        if self._drain_id:
            GLib.source_remove(self._drain_id)
            self._drain_id = 0
        self._pending.clear()
        self._discard_line = False
        if self.current_log_path:
            try:
                self.last_pos = self.current_log_path.stat().st_size
            except OSError:
                pass
        self.buffer.set_text("")
        self.line_count = 0
        self.close_search()


class PlayPage(Adw.Bin):
    def __init__(self, window):
        super().__init__()
        self.window = window
        toolbar_view = Adw.ToolbarView()

        self.stack = Adw.ViewStack()
        self.top_box = None

        status = Adw.StatusPage()
        status.set_icon_name("macoblox")
        status.set_title("Mac O’ Blox")
        self.status = status

        center_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                             halign=Gtk.Align.CENTER)

        self.log_button = Gtk.Button(label=_("Open last log"))
        self.log_button.add_css_class("flat")
        self.log_button.set_visible(False)
        self.log_button.connect("clicked", lambda *_args: window.open_last_log())
        center_box.append(self.log_button)

        links = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, margin_top=14)
        for title, icon, uri in _links():
            button = Gtk.Button(icon_name=icon, tooltip_text=title)
            button.add_css_class("flat")
            button.add_css_class("circular")
            button.connect("clicked", lambda *_args, u=uri: _open_uri(window, u))
            links.append(button)
        center_box.append(links)

        status.set_child(center_box)
        self.stack.add_titled_with_icon(status, "play", _("Play"), "media-playback-start-symbolic")

        self.logs_view = GameLogsView(window)
        self.stack.add_titled_with_icon(self.logs_view, "logs", _("Logs"), "utilities-terminal-symbolic")

        self.top_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.top_box.set_margin_top(10)
        self.top_box.set_margin_bottom(10)
        self.switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.switcher.set_halign(Gtk.Align.CENTER)
        self.top_box.append(self.switcher)
        self.top_box.set_visible(False)

        toolbar_view.add_top_bar(self.top_box)
        toolbar_view.set_content(self.stack)
        self.stack.connect("notify::visible-child-name", self._on_tab_changed)

        action_bar = Gtk.ActionBar()

        self.playtime_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.playtime_box.set_valign(Gtk.Align.CENTER)
        self.playtime_box.add_css_class("dim-label")
        clock_icon = Gtk.Image(icon_name="preferences-system-time-symbolic")
        self.playtime_label = Gtk.Label()
        self.playtime_box.append(clock_icon)
        self.playtime_box.append(self.playtime_label)
        self.playtime_box.set_tooltip_text(_("Total playtime"))
        action_bar.pack_start(self.playtime_box)

        self.studio_progress = Gtk.ProgressBar(show_text=True, visible=False)
        self.studio_progress.set_size_request(160, -1)
        action_bar.pack_start(self.studio_progress)

        self.play = Gtk.Button(label=_("Play"))
        self.play.add_css_class("suggested-action")
        self.play.add_css_class("pill")
        self.play.set_size_request(140, -1)
        self.play.connect("clicked", lambda *_args: self._on_play_clicked())
        action_bar.pack_end(self.play)

        self.studio = Gtk.Button(label=_("Roblox Studio"))
        self.studio.add_css_class("pill")
        self.studio.set_size_request(130, -1)
        self.studio.connect("clicked", lambda *_args: window.studio_clicked())
        # Studio needs Wine, which the Flatpak does not have yet.
        self.studio.set_visible(not os.path.exists("/.flatpak-info"))
        action_bar.pack_end(self.studio)

        toolbar_view.add_bottom_bar(action_bar)
        self.set_child(toolbar_view)
        self.refresh()

    def _on_tab_changed(self, stack, _pspec):
        if getattr(self, "top_box", None) is None:
            return
        tab = stack.get_visible_child_name()
        if tab == "logs":
            self.logs_view.update()
        elif tab == "play":
            running = self.window.session is not None
            busy = self.window.busy
            active = running or busy == "starting"
            if not active:
                self.top_box.set_visible(False)

    def show_logs(self, log_path=None):
        if not log_path:
            log_path = (self.window.session.log_path if self.window.session else None) or self.window.last_log
        if not log_path or not log_path.exists():
            _toast(self.window.toasts, _("No log found"))
            return
        self.top_box.set_visible(True)
        child = self.switcher.get_first_child()
        idx = 0
        while child:
            if idx == 1:
                child.set_sensitive(True)
                child.set_tooltip_text(_("View game logs"))
            child = child.get_next_sibling()
            idx += 1
        self.logs_view.reset(log_path)
        self.logs_view.update()
        self.stack.set_visible_child_name("logs")

    def _on_play_clicked(self):
        if self.window.session is not None:
            self.window.stop()
        else:
            self.window.play_clicked()

    def refresh_playtime(self):
        show = self.window.settings.get("show_playtime", True)
        self.playtime_box.set_visible(show)
        if show:
            sec = self.window.settings.get("playtime_seconds", 0)
            self.playtime_label.set_text(core.format_playtime(sec))

    def refresh(self):
        running = self.window.session is not None
        busy = self.window.busy
        active = running or busy == "starting"
        viewing_logs = self.stack.get_visible_child_name() == "logs"

        # Show switcher when game is active or currently viewing logs
        self.top_box.set_visible(active or viewing_logs)

        child = self.switcher.get_first_child()
        idx = 0
        while child:
            if idx == 1:
                child.set_sensitive(active or bool(self.window.last_log))
                if not active and not self.window.last_log:
                    child.set_tooltip_text(_("Game is not running"))
                else:
                    child.set_tooltip_text(_("View game logs"))
            child = child.get_next_sibling()
            idx += 1

        if not active and not self.window.last_log and viewing_logs:
            self.stack.set_visible_child_name("play")

        version = core.installed_version()
        parts = [_("Roblox {version}", version=version) if version else _("Roblox not found")]
        parts.append(_("Darling running") if core.darlingserver_running()
                     else _("Darling starts with the game"))
        if version and not running and not core.signed_in():
            parts.append(_("Sign in with Quick Login"))
        self.status.set_description(" · ".join(parts))

        if running:
            self.play.set_label(_("Stop Roblox"))
            self.play.remove_css_class("suggested-action")
            self.play.add_css_class("destructive-action")
            self.play.set_sensitive(True)
        elif busy == "starting":
            self.play.set_label(_("Starting…"))
            self.play.remove_css_class("destructive-action")
            self.play.add_css_class("suggested-action")
            self.play.set_sensitive(False)
        else:
            self.play.set_label(_("Play") if version else _("Install Roblox"))
            self.play.remove_css_class("destructive-action")
            self.play.add_css_class("suggested-action")
            self.play.set_sensitive(not busy)

        self.log_button.set_visible(bool(self.window.last_log) and not running)
        self.refresh_playtime()


class FlagsPage(Adw.PreferencesPage):
    def __init__(self, window):
        super().__init__(title=_("Fast flags"), icon_name="preferences-other-symbolic")
        self.window = window
        self.flags = core.load_fast_flags()
        self.preset_flags = set()
        self.preset_setters = {}  # flag -> function(value) that shows it in its row
        self.preset_resetters = []
        self._save_timer = 0

        presets = Adw.PreferencesGroup(
            title=_("Popular"),
            description=_("Roblox only applies flags from its allowlist, some flags may have no effect."))
        for preset in PRESETS:
            presets.add(self._preset_row(preset))
        self.add(presets)

        self.custom = Adw.PreferencesGroup(title=_("Custom flags"))
        add_button = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        add_button.add_css_class("flat")
        add_button.set_tooltip_text(_("Add flag"))
        add_button.connect("clicked", lambda *_args: self._add_custom_row("", ""))
        import_button = Gtk.Button(label=_("Import JSON"), valign=Gtk.Align.CENTER)
        import_button.add_css_class("flat")
        import_button.connect("clicked", lambda *_args: self._import_dialog())
        export_button = Gtk.Button(label=_("Export JSON"), valign=Gtk.Align.CENTER)
        export_button.add_css_class("flat")
        export_button.connect("clicked", lambda *_args: self._export_dialog())
        suffix = Gtk.Box(spacing=6)
        suffix.append(import_button)
        suffix.append(export_button)
        suffix.append(add_button)
        self.custom.set_header_suffix(suffix)
        self.add(self.custom)
        self.custom_rows = []
        for name, value in self.flags.items():
            if name not in self.preset_flags:
                self._add_custom_row(name, core.format_flag_value(value))

        file_group = Adw.PreferencesGroup()
        path_row = Adw.ActionRow(title=_("File"), subtitle=str(core.FAST_FLAGS))
        path_row.set_subtitle_selectable(True)
        file_group.add(path_row)

        reset_flags_btn = _button_row(_("Reset all fast flags"))
        reset_flags_btn.add_css_class("destructive-action")
        reset_flags_btn.connect("activated", lambda *_args: self._reset_dialog())
        file_group.add(reset_flags_btn)

        self.add(file_group)

    # -- presets
    def _preset_row(self, preset):
        names = preset["flag"] if isinstance(preset["flag"], list) else [preset["flag"]]
        self.preset_flags.update(names)
        enabled = all(name in self.flags for name in names)
        if preset["kind"] == "fps":
            row = Adw.SpinRow.new_with_range(preset["min"], preset["max"], 1)
            row.set_title(_(preset["title"]))
            row.set_subtitle(preset["subtitle"])
            cap = self.window.settings.get("framerate_cap", 0)
            current = self.flags.get(names[0], cap if cap > 0 else preset["default"])
            row.set_value(float(current) if str(current).lstrip("-").isdigit() else preset["default"])
            switch = Gtk.Switch(active=cap > 0 or names[0] in self.flags, valign=Gtk.Align.CENTER)
            row.add_suffix(switch)

            def apply(*_args):
                value = int(row.get_value()) if switch.get_active() else 0
                # Written into Roblox's settings when the game starts.
                self.window.set_setting("framerate_cap", value)
                for name in names:
                    if value:
                        self.flags[name] = value
                    else:
                        self.flags.pop(name, None)
                self._save()

            switch.connect("notify::active", apply)
            row.connect("notify::value", lambda *_args: switch.get_active() and apply())

            def set_fps(value):
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return False
                row.set_value(value)
                switch.set_active(True)
                apply()
                return True

            for name in names:
                self.preset_setters[name] = set_fps
            self.preset_resetters.append(lambda: (switch.set_active(False), row.set_value(preset["default"])))
            return row
        if preset["kind"] == "number":
            also = preset.get("also", {})
            self.preset_flags.update(also)
            row = Adw.SpinRow.new_with_range(preset["min"], preset["max"], 1)
            row.set_title(_(preset["title"]))
            row.set_subtitle(preset["subtitle"])
            current = self.flags.get(names[0], preset["default"])
            row.set_value(float(current) if str(current).lstrip("-").isdigit() else preset["default"])
            switch = Gtk.Switch(active=enabled, valign=Gtk.Align.CENTER)
            row.add_suffix(switch)

            def apply(*_args):
                for name in names:
                    if switch.get_active():
                        self.flags[name] = int(row.get_value())
                    else:
                        self.flags.pop(name, None)
                for name, value in also.items():
                    if switch.get_active():
                        self.flags[name] = value
                    else:
                        self.flags.pop(name, None)
                self._save()

            switch.connect("notify::active", apply)
            row.connect("notify::value", lambda *_args: switch.get_active() and apply())

            def set_number(value):
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return False
                row.set_value(value)
                switch.set_active(True)
                apply()
                return True

            for name in names:
                self.preset_setters[name] = set_number
            for name in also:
                # Imported on its own: set along with the value above.
                self.preset_setters[name] = lambda value: True
            self.preset_resetters.append(lambda: (switch.set_active(False), row.set_value(preset["default"])))
            return row
        row = Adw.SwitchRow(title=_(preset["title"]), subtitle=preset["subtitle"], active=enabled)

        def toggle(*_args):
            for name in names:
                if row.get_active():
                    self.flags[name] = preset["value"]
                else:
                    self.flags.pop(name, None)
            self._save()

        row.connect("notify::active", toggle)

        def set_fixed(value, name):
            if value == preset["value"]:
                row.set_active(True)
            else:
                # Not this preset's value: keep it as a plain flag.
                self.flags[name] = value
                self._save()
            return True

        for name in names:
            self.preset_setters[name] = lambda value, n=name: set_fixed(value, n)
        self.preset_resetters.append(lambda: row.set_active(False))
        return row

    # -- custom flags
    def _add_custom_row(self, name, value):
        row = Adw.ExpanderRow(title=name or _("New flag"), subtitle=value)
        name_row = Adw.EntryRow(title=_("Name"))
        name_row.set_text(name)
        value_row = Adw.EntryRow(title=_("Value"))
        value_row.set_text(value)
        remove = Gtk.Button(label=_("Remove"), halign=Gtk.Align.END, margin_top=6, margin_bottom=6,
                            margin_end=12)
        remove.add_css_class("destructive-action")
        row.add_row(name_row)
        row.add_row(value_row)
        holder = Gtk.ListBoxRow(activatable=False, selectable=False)
        holder.set_child(remove)
        row.add_row(holder)
        entry = {"row": row, "name": name_row, "value": value_row}

        def changed(*_args):
            row.set_title(name_row.get_text() or _("New flag"))
            row.set_subtitle(value_row.get_text())
            self._sync_custom()

        name_row.connect("changed", changed)
        value_row.connect("changed", changed)

        def delete(*_args):
            self.custom.remove(row)
            self.custom_rows.remove(entry)
            self._sync_custom()

        remove.connect("clicked", delete)
        self.custom.add(row)
        self.custom_rows.append(entry)
        if not name:
            row.set_expanded(True)

    def _sync_custom(self):
        for name in [n for n in self.flags if n not in self.preset_flags]:
            del self.flags[name]
        for entry in self.custom_rows:
            name = entry["name"].get_text().strip()
            if name and name not in self.preset_flags:
                self.flags[name] = core.parse_flag_value(entry["value"].get_text())
        self._save()

    def _import_dialog(self):
        dialog = Adw.AlertDialog(
            heading=_("Import fast flags"),
            body=_('Paste JSON like {"Flag": value}. Flags are added to the current ones.'))
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.CHAR, monospace=True)
        view.set_size_request(420, 220)
        scroller = Gtk.ScrolledWindow(child=view, min_content_height=220)
        scroller.add_css_class("card")
        dialog.set_extra_child(scroller)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("import", _("Import"))
        dialog.set_response_appearance("import", Adw.ResponseAppearance.SUGGESTED)

        def response(_dialog, result):
            if result != "import":
                return
            buffer = view.get_buffer()
            text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
            try:
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError
            except ValueError:
                _toast(self.window.toasts, _("This is not a JSON object with flags"))
                return
            existing = {entry["name"].get_text(): entry for entry in self.custom_rows}
            imported = 0
            for name, value in data.items():
                if name in self.preset_flags:
                    # Popular flags live in their own rows above.
                    imported += self.preset_setters[name](value)
                    continue
                if name in existing:
                    existing[name]["value"].set_text(core.format_flag_value(value))
                else:
                    self._add_custom_row(name, core.format_flag_value(value))
                imported += 1
            self._sync_custom()
            _toast(self.window.toasts, _("Imported flags: {count}", count=imported))

        dialog.connect("response", response)
        dialog.present(self.window)

    def _export_dialog(self):
        text = json.dumps(self.flags, indent=2)
        dialog = Adw.AlertDialog(
            heading=_("Export fast flags"),
            body=_("Copy flags to clipboard or save to a file."),
        )
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.NONE, monospace=True, editable=False)
        view.set_size_request(420, 220)
        buffer = view.get_buffer()
        buffer.set_text(text)
        scroller = Gtk.ScrolledWindow(child=view, min_content_height=220)
        scroller.add_css_class("card")
        dialog.set_extra_child(scroller)

        dialog.add_response("close", _("Close"))
        dialog.add_response("copy", _("Copy"))
        dialog.add_response("save", _("Save to file…"))
        dialog.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)

        def response(_dialog, result):
            if result == "copy":
                display = Gdk.Display.get_default()
                if display:
                    display.get_clipboard().set(text)
                    _toast(self.window.toasts, _("Flags copied to clipboard"))
            elif result == "save":
                self._save_export_file(text)

        dialog.connect("response", response)
        dialog.present(self.window)

    def _save_export_file(self, text):
        dialog = Gtk.FileChooserNative.new(
            _("Save fast flags"),
            self.window,
            Gtk.FileChooserAction.SAVE,
            _("Save"),
            _("Cancel"),
        )
        dialog.set_current_name("ClientAppSettings.json")
        f = Gtk.FileFilter()
        f.set_name("JSON files (*.json)")
        f.add_pattern("*.json")
        dialog.add_filter(f)

        def on_response(d, res):
            if res == Gtk.ResponseType.ACCEPT:
                file = d.get_file()
                if file:
                    try:
                        Path(file.get_path()).write_text(text, encoding="utf-8")
                        _toast(self.window.toasts, _("Flags saved to {path}", path=Path(file.get_path()).name))
                    except OSError as e:
                        _error_dialog(self.window, _("Could not save flags"), str(e))
            d.destroy()

        dialog.connect("response", on_response)
        dialog.show()

    def _reset_dialog(self):
        dialog = Adw.AlertDialog(
            heading=_("Reset all fast flags?"),
            body=_("All custom flags and presets will be cleared and reset to default."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("reset", _("Reset"))
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)

        def response(_dialog, result):
            if result == "reset":
                self._reset_all_flags()

        dialog.connect("response", response)
        dialog.present(self.window)

    def _reset_all_flags(self):
        self.flags.clear()
        self.window.set_setting("framerate_cap", 0)
        core.save_fast_flags({})

        for reset_func in self.preset_resetters:
            reset_func()

        for entry in list(self.custom_rows):
            self.custom.remove(entry["row"])
        self.custom_rows.clear()

        _toast(self.window.toasts, _("All fast flags have been reset"))

    def _save(self):
        """Save shortly after the last change: typing a value or spinning a
        number would otherwise rewrite the file on every keystroke."""
        if self._save_timer:
            GLib.source_remove(self._save_timer)
        self._save_timer = GLib.timeout_add(300, self._save_now)

    def _save_now(self):
        self._save_timer = 0
        try:
            core.save_fast_flags(self.flags)
        except OSError as error:
            _toast(self.window.toasts, _("Could not save flags: {error}", error=error))
        return False

    def flush(self):
        """Write a pending change now (Roblox reads the file when it starts)."""
        if self._save_timer:
            GLib.source_remove(self._save_timer)
            self._save_now()


class SettingsPage(Adw.Bin):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self._checking_updates = False
        settings = window.settings

        toolbar_view = Adw.ToolbarView()
        self.stack = Adw.ViewStack()

        # 1. Environment page (Interface, Game, DNS, Diagnostics)
        self.env_page = Adw.PreferencesPage()

        launcher_group = Adw.PreferencesGroup(title="Mac O’ Blox")
        self.launcher_version_row = Adw.ActionRow(
            title=_("Launcher version"),
            subtitle=f"v{__version__}"
        )
        self.check_launcher_btn = Gtk.Button(label=_("Check for updates"), valign=Gtk.Align.CENTER)
        self._launcher_update_handler = self.check_launcher_btn.connect(
            "clicked", lambda *_args: self.check_launcher_update()
        )
        self.launcher_version_row.add_suffix(self.check_launcher_btn)
        launcher_group.add(self.launcher_version_row)

        force_update = _button_row(_("Force update"))
        force_update.connect("activated", lambda *_args: self.force_update_launcher())
        launcher_group.add(force_update)

        self.launcher_progress = Gtk.ProgressBar(show_text=True, margin_top=6, margin_bottom=6,
                                                 margin_start=12, margin_end=12, visible=False)
        launcher_group.add(Gtk.ListBoxRow(activatable=False, selectable=False, child=self.launcher_progress))
        self.env_page.add(launcher_group)

        interface = Adw.PreferencesGroup(title=_("Interface"))
        codes = list(i18n.LANGUAGES)
        language = Adw.ComboRow(title=_("Language"),
                                model=Gtk.StringList.new(list(i18n.LANGUAGES.values())))
        language.set_selected(codes.index(i18n.language()))
        language.connect("notify::selected", lambda row, _pspec: window.set_language(
            codes[row.get_selected()]))
        interface.add(language)
        self.env_page.add(interface)

        game = Adw.PreferencesGroup(title=_("Game"))
        renderer_codes = ("opengl", "vulkan")
        renderer = Adw.ComboRow(
            title=_("Renderer"),
            subtitle=_("Applies on next launch. Missing Mesa EGL/Zink packages need administrator authentication."),
            model=Gtk.StringList.new([_("OpenGL"), _("Vulkan (Zink, experimental)")]))
        selected_renderer = settings.get("renderer", "opengl")
        renderer.set_selected(renderer_codes.index(selected_renderer)
                              if selected_renderer in renderer_codes else 0)
        def select_renderer(row, _pspec):
            selected = renderer_codes[row.get_selected()]
            if selected == "opengl":
                window.set_setting("renderer", selected)
                return
            from . import graphics
            if not graphics.missing_vulkan_dependencies():
                window.set_setting("renderer", selected)
                return
            row.set_sensitive(False)
            _toast(window.toasts, _("Installing Vulkan dependencies… Authorize the administrator prompt to continue."))

            def installed(_result, error):
                row.set_sensitive(True)
                if error:
                    row.handler_block(renderer_handler)
                    row.set_selected(renderer_codes.index(window.settings.get("renderer", "opengl")))
                    row.handler_unblock(renderer_handler)
                    _error_dialog(window, _("Could not install Vulkan dependencies"), str(error))
                else:
                    window.set_setting("renderer", "vulkan")
                    _toast(window.toasts, _("Vulkan dependencies installed"))

            self._in_thread(graphics.ensure_vulkan_dependencies, installed)

        renderer_handler = renderer.connect("notify::selected", select_renderer)
        game.add(renderer)

        self._gpu_codes = ["auto"]
        self.gpu_row = Adw.ComboRow(
            title=_("Graphics card"),
            subtitle=_("Detecting graphics cards… Applies on next launch."),
            model=Gtk.StringList.new([_("Automatic")]),
            sensitive=False,
            visible=settings.get("gpu", "auto") != "auto")
        self._gpu_handler = self.gpu_row.connect("notify::selected", self._select_gpu)
        game.add(self.gpu_row)
        from . import gpus
        self._in_thread(gpus.enumerate_gpus, self._gpu_choices_loaded)

        backend_codes = ["x11", "wayland"]
        backend = Adw.ComboRow(
            title=_("Window backend"),
            subtitle=_("Native Wayland is experimental and incomplete. Applies on next launch."),
            model=Gtk.StringList.new([_("X11 / Xwayland"), _("Native Wayland (experimental)")]))
        selected_backend = settings.get("display_backend", "x11")
        backend.set_selected(backend_codes.index(selected_backend) if selected_backend in backend_codes else 0)
        backend.connect("notify::selected", lambda row, _pspec: window.set_setting(
            "display_backend", backend_codes[row.get_selected()]))
        game.add(backend)

        from .display import validated_dpi_scale
        ui_scale = Adw.SpinRow.new_with_range(100, 400, 25)
        ui_scale.set_digits(0)
        ui_scale.set_title(_("Roblox UI scale"))
        ui_scale.set_subtitle(_("100–400%. Keeps the display resolution. Applies on next launch."))
        ui_scale.set_value(validated_dpi_scale(settings.get("dpi_scale", 1.0)) * 100)
        ui_scale.connect("notify::value", lambda row, _pspec: window.set_setting(
            "dpi_scale", validated_dpi_scale(row.get_value() / 100)))
        game.add(ui_scale)

        mangohud = Adw.SwitchRow(
            title=_("MangoHud overlay"),
            subtitle=_("Show FPS, frametimes and CPU/GPU usage. Requires MangoHud; applies on next launch."),
            active=settings.get("mangohud", False))
        mangohud.connect("notify::active", lambda row, _pspec: window.set_setting(
            "mangohud", row.get_active()))
        game.add(mangohud)

        sensitivity = Adw.SpinRow.new_with_range(0.1, 5.0, 0.05)
        sensitivity.set_digits(2)
        sensitivity.set_title(_("Camera sensitivity"))
        sensitivity.set_subtitle(_("Mouse movement multiplier while rotating the camera"))
        sensitivity.set_value(settings["mouse_sensitivity"])
        sensitivity.connect("notify::value", lambda row, _pspec: window.set_setting(
            "mouse_sensitivity", round(row.get_value(), 2)))
        game.add(sensitivity)

        scroll_sens = Adw.SpinRow.new_with_range(0.5, 10.0, 0.5)
        scroll_sens.set_digits(1)
        scroll_sens.set_title(_("Scroll sensitivity"))
        scroll_sens.set_subtitle(_("Mouse wheel scroll speed in menus and interface"))
        scroll_sens.set_value(settings.get("scroll_sensitivity", 1.5))
        scroll_sens.connect("notify::value", lambda row, _pspec: window.set_setting(
            "scroll_sensitivity", round(row.get_value(), 2)))
        game.add(scroll_sens)

        self.raw_mouse_row = Adw.SwitchRow(
            title=_("Raw mouse input"),
            subtitle=_("Camera moves by the mouse's own motion, without pointer acceleration (XInput 2)"),
            active=settings.get("raw_mouse", True),
        )
        self.raw_mouse_row.connect("notify::active", lambda row, _pspec: window.set_setting(
            "raw_mouse", row.get_active()))
        game.add(self.raw_mouse_row)

        menu_bar = Adw.SwitchRow(title=_("Hide the macOS menu bar"),
                                 subtitle=_("The Roblox, Edit, Window… strip at the top of the game window"),
                                 active=settings["hide_menu_bar"])
        menu_bar.connect("notify::active", lambda row, _pspec: window.set_setting(
            "hide_menu_bar", row.get_active()))
        game.add(menu_bar)

        hide_launcher = Adw.SwitchRow(title=_("Hide launcher while playing"),
                                      subtitle=_("Hide the launcher window while the game is running"),
                                      active=settings.get("hide_launcher_on_launch", True))
        hide_launcher.connect("notify::active", lambda row, _pspec: window.set_setting(
            "hide_launcher_on_launch", row.get_active()))
        game.add(hide_launcher)

        reopen = Adw.SwitchRow(title=_("Show the launcher after Roblox exits"),
                               active=settings["show_launcher_after_exit"])
        reopen.connect("notify::active", lambda row, _pspec: window.set_setting(
            "show_launcher_after_exit", row.get_active()))
        game.add(reopen)

        playtime_switch = Adw.SwitchRow(title=_("Show playtime"),
                                        subtitle=_("Show accumulated playtime on the Play page"),
                                        active=settings.get("show_playtime", True))
        playtime_switch.connect("notify::active", lambda row, _pspec: window.set_show_playtime(row.get_active()))
        game.add(playtime_switch)

        self.env_page.add(game)

        # 3. Discord Rich Presence
        discord_group = Adw.PreferencesGroup(title=_("Discord Rich Presence"))

        self.discord_rpc_switch = Adw.SwitchRow(
            title=_("Enable Discord Rich Presence"),
            subtitle=_("Show current game and playtime in your Discord status"),
            active=settings.get("discord_rpc", True),
        )
        self.discord_rpc_switch.connect("notify::active", lambda row, _pspec: window.set_discord_rpc(row.get_active()))
        discord_group.add(self.discord_rpc_switch)

        self.discord_game = Adw.SwitchRow(
            title=_("Show experience name in Discord"),
            subtitle=_("Display the title and creator of the place you are playing"),
            active=settings.get("discord_rpc_game", True),
        )
        self.discord_game.set_sensitive(settings.get("discord_rpc", True))
        self.discord_game.connect("notify::active", lambda row, _pspec: window.set_discord_rpc_option(
            "discord_rpc_game", row.get_active()))
        discord_group.add(self.discord_game)

        self.discord_icon = Adw.SwitchRow(
            title=_("Show experience thumbnail in Discord"),
            subtitle=_("Replace the Mac O’ Blox icon with the game's icon"),
            active=settings.get("discord_rpc_icon", False),
        )
        self.discord_icon.set_sensitive(settings.get("discord_rpc", True))
        self.discord_icon.connect("notify::active", lambda row, _pspec: window.set_discord_rpc_option(
            "discord_rpc_icon", row.get_active()))
        discord_group.add(self.discord_icon)

        self.discord_time = Adw.SwitchRow(
            title=_("Show elapsed time in Discord"),
            subtitle=_("Display how long you have been playing in your status"),
            active=settings.get("discord_rpc_time", True),
        )
        self.discord_time.set_sensitive(settings.get("discord_rpc", True))
        self.discord_time.connect("notify::active", lambda row, _pspec: window.set_discord_rpc_option(
            "discord_rpc_time", row.get_active()))
        discord_group.add(self.discord_time)

        self.env_page.add(discord_group)

        dns_group = Adw.PreferencesGroup(
            title=_("DNS for Roblox"),
            description=_("Only Roblox uses this server, the rest of the system keeps its own DNS. "
                          "Helps when some Roblox images or servers do not load."))
        dns_codes = [code for code, _label in DNS_CHOICES]
        server = Adw.ComboRow(title=_("DNS server"),
                              model=Gtk.StringList.new([_(label) for _code, label in DNS_CHOICES]))
        current = settings.get("dns", "system")
        server.set_selected(dns_codes.index(current) if current in dns_codes else 0)
        custom = Adw.EntryRow(title=_("Custom server"))
        custom.set_text(settings.get("dns_custom", ""))
        custom.set_show_apply_button(True)
        custom.set_tooltip_text(_("IP address, optionally with :port. Plain DNS, not encrypted."))
        custom.set_visible(current == "custom")

        def dns_changed(row, _pspec):
            code = dns_codes[row.get_selected()]
            window.set_setting("dns", code)
            custom.set_visible(code == "custom")

        def custom_applied(row):
            text = row.get_text().strip()
            try:
                dns.parse_server(text)
            except ValueError as error:
                row.add_css_class("error")
                _toast(window.toasts, str(error))
                return
            row.remove_css_class("error")
            window.set_setting("dns_custom", text)

        server.connect("notify::selected", dns_changed)
        custom.connect("apply", custom_applied)
        dns_group.add(server)
        dns_group.add(custom)
        self.env_page.add(dns_group)

        diagnostics = Adw.PreferencesGroup(
            title=_("Diagnostics"),
            description=_("Detailed logs for debugging. They slow the game down, enable only when needed."))
        for key, title in [("diagnostic_signals", "Crash diagnostics"),
                           ("trace_udp", "Network tracing (UDP)"),
                           ("trace_lock", "Mouse lock tracing"),
                           ("trace_events", "Mouse event tracing"),
                           ("trace_gl", "OpenGL tracing"),
                           ("trace_keys", "Keyboard tracing"),
                           ("fps_log", "Frame rate in the log")]:
            row = Adw.SwitchRow(title=_(title), subtitle=core.TRACE_ENV[key], active=settings[key])
            row.connect("notify::active", lambda r, _pspec, k=key: window.set_setting(k, r.get_active()))
            diagnostics.add(row)
        logs = _button_row(_("Open logs folder"))
        logs.connect("activated", lambda *_args: self.open_logs())
        diagnostics.add(logs)
        rebuild = _button_row(_("Rebuild shim"))
        rebuild.connect("activated", lambda *_args: self.rebuild())
        diagnostics.add(rebuild)
        restart = _button_row(_("Restart Darling"))
        restart.connect("activated", lambda *_args: self.restart_darling())
        diagnostics.add(restart)
        self.env_page.add(diagnostics)

        self.stack.add_titled_with_icon(self.env_page, "env", _("Environment"), "preferences-system-symbolic")

        # 2. Roblox page (Download, updates, version, account)
        self.roblox_page = Adw.PreferencesPage()

        roblox = Adw.PreferencesGroup(title="Roblox")
        self.version_row = Adw.ActionRow(title=_("Installed version"),
                                         subtitle=core.installed_version() or _("not found"))
        self.update_button = Gtk.Button(label=_("Check for updates"), valign=Gtk.Align.CENTER)
        self._update_handler = self.update_button.connect("clicked", lambda *_args: self.check_updates())
        self.version_row.add_suffix(self.update_button)
        roblox.add(self.version_row)
        self.channel_row = Adw.ActionRow(
            title=_("Update channel"),
            subtitle=settings.get("roblox_channel") or _("Production (default)"))
        roblox.add(self.channel_row)

        self.auto_update_switch = Adw.SwitchRow(
            title=_("Check for Roblox updates on startup"),
            subtitle=_("Prompt to update if a newer version of Roblox is available"),
            active=settings.get("auto_check_roblox_updates", True),
        )
        self.auto_update_switch.connect("notify::active", lambda row, _pspec: window.set_setting(
            "auto_check_roblox_updates", row.get_active()))
        roblox.add(self.auto_update_switch)

        self.progress = Gtk.ProgressBar(show_text=True, margin_top=6, margin_bottom=6,
                                        margin_start=12, margin_end=12, visible=False)
        progress_row = Gtk.ListBoxRow(activatable=False, selectable=False, child=self.progress)
        roblox.add(progress_row)

        delete_roblox = _button_row(_("Delete Roblox"))
        delete_roblox.add_css_class("destructive-action")
        delete_roblox.connect("activated", lambda *_args: self.delete_roblox())
        roblox.add(delete_roblox)
        self.roblox_page.add(roblox)

        # The startup render throttle patch gets a group of its own: the
        # button applies or reverts it by hand, the switch decides whether
        # launches and updates keep it applied. Installing turns the switch
        # on and removing turns it off, so the two cannot contradict.
        throttle = Adw.PreferencesGroup(title=_("Throttle patch"))
        self.throttle_row = _button_row(_("Throttle patch"))
        self.throttle_row.set_sensitive(False)
        self.throttle_row.connect("activated", lambda *_args: self.toggle_throttle_patch())
        throttle.add(self.throttle_row)

        self.auto_patch_switch = Adw.SwitchRow(
            title=_("Apply the throttle patch automatically"),
            subtitle=_("Patch the client on every launch and after Roblox updates"),
            active=settings.get("auto_patch_throttle", True),
        )
        self.auto_patch_switch.connect("notify::active", lambda row, _pspec: window.set_setting(
            "auto_patch_throttle", row.get_active()))
        throttle.add(self.auto_patch_switch)
        self._in_thread(core.throttle_patch_state, self._throttle_state_done)
        self.roblox_page.add(throttle)

        account = Adw.PreferencesGroup(title=_("Account"))
        logout = _button_row(_("Sign out"))
        logout.add_css_class("destructive-action")
        logout.connect("activated", lambda *_args: self.logout())
        account.add(logout)
        self.roblox_page.add(account)

        self.stack.add_titled_with_icon(self.roblox_page, "roblox", "Roblox", "application-x-executable-symbolic")

        # 3. Fast flags page
        self.flags_page = FlagsPage(window)
        self.stack.add_titled_with_icon(self.flags_page, "flags", _("Fast flags"), "preferences-other-symbolic")

        top_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top_box.set_margin_top(10)
        top_box.set_margin_bottom(10)
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_halign(Gtk.Align.CENTER)
        top_box.append(switcher)
        toolbar_view.add_top_bar(top_box)
        toolbar_view.set_content(self.stack)

        self.set_child(toolbar_view)

    def set_tab(self, tab):
        self.stack.set_visible_child_name(tab)

    def flush(self):
        self.flags_page.flush()

    def refresh_raw_mouse(self):
        if hasattr(self, "raw_mouse_row"):
            self.raw_mouse_row.set_active(self.window.settings.get("raw_mouse", True))

    def _select_gpu(self, row, _pspec):
        selected = row.get_selected()
        if selected < len(self._gpu_codes):
            self.window.set_setting("gpu", self._gpu_codes[selected])

    def _gpu_choices_loaded(self, adapters, error):
        if getattr(self.window, "settings_page", None) is not self:
            return False
        selection = self.window.settings.get("gpu", "auto")
        adapters = adapters or []
        codes = ["auto", *(adapter["id"] for adapter in adapters)]
        labels = [_("Automatic"), *(adapter["label"] for adapter in adapters)]
        if selection not in codes:
            codes.append(selection)
            labels.append(_("Selected graphics card unavailable"))
        self._gpu_codes = codes
        self.gpu_row.handler_block(self._gpu_handler)
        try:
            self.gpu_row.set_model(Gtk.StringList.new(labels))
            self.gpu_row.set_selected(codes.index(selection))
        finally:
            self.gpu_row.handler_unblock(self._gpu_handler)
        self.gpu_row.set_sensitive(True)
        self.gpu_row.set_visible(len(adapters) > 1 or selection != "auto" or error is not None)
        self.gpu_row.set_subtitle(_("Could not detect graphics cards. Select Automatic to use the system default.")
                                  if error else _("Applies on next launch. Automatic keeps your desktop or terminal's GPU selection."))
        return False

    def open_logs(self):
        try:
            core.LOGS.mkdir(parents=True, exist_ok=True)  # none before the first game
            Gio.AppInfo.launch_default_for_uri(core.LOGS.as_uri(), None)
        except (OSError, GLib.Error) as error:
            _toast(self.window.toasts, _("Could not open the logs folder: {error}", error=error))

    def _in_thread(self, work, done):
        def run():
            try:
                result = work()
                GLib.idle_add(done, result, None)
            except Exception as error:  # shown to the user
                GLib.idle_add(done, None, error)
        threading.Thread(target=run, daemon=True).start()

    def _set_update_action(self, label, action):
        self.update_button.set_label(label)
        self.update_button.disconnect(self._update_handler)
        self._update_handler = self.update_button.connect("clicked", lambda *_args: action())

    def check_launcher_update(self):
        self.check_launcher_btn.set_sensitive(False)
        self.check_launcher_btn.set_label(_("Checking…"))

        def done(result, error):
            self.check_launcher_btn.set_sensitive(True)
            if error:
                self.check_launcher_btn.set_label(_("Check for updates"))
                _toast(self.window.toasts, _("Could not check: {error}", error=error))
                return
            has_update, tag, html_url = result
            if has_update:
                self.launcher_version_row.set_subtitle(_("Update {version} available", version=tag))
                self.check_launcher_btn.set_label(_("Update to {version}", version=tag))
                self.check_launcher_btn.disconnect(self._launcher_update_handler)
                self._launcher_update_handler = self.check_launcher_btn.connect(
                    "clicked", lambda *_args: self.force_update_launcher()
                )
                _toast(self.window.toasts, _("Update {version} available", version=tag))
            else:
                self.check_launcher_btn.set_label(_("Check for updates"))
                _toast(self.window.toasts, _("Mac O’ Blox is up to date"))

        self._in_thread(core.check_launcher_update, done)

    def force_update_launcher(self):
        if not self.window.begin("updating_launcher"):
            return
        self.check_launcher_btn.set_sensitive(False)
        self.launcher_progress.set_visible(True)
        self.launcher_progress.set_fraction(0.1)
        self.launcher_progress.set_text(_("Updating…"))

        def progress(fraction, text):
            GLib.idle_add(self.launcher_progress.set_fraction, fraction)
            GLib.idle_add(self.launcher_progress.set_text, text)

        def done(result, error):
            self.window.end()
            self.check_launcher_btn.set_sensitive(True)
            self.launcher_progress.set_visible(False)
            if error:
                _error_dialog(self.window, _("Update failed"), str(error) or repr(error))
                return
            ok, msg = result if result else (False, "")
            if ok:
                _toast(self.window.toasts, msg)
            else:
                _open_uri(self.window, msg)

        self._in_thread(lambda: core.update_launcher(progress), done)

    def check_updates(self, install=False, on_progress=None, on_complete=None, force_download=False):
        """Looks for a newer client; with install=True also installs it
        using the same asynchronous workflow as first-launch setup."""
        if self._checking_updates:
            return False
        if install and not self.window.begin("updating"):
            return False
        self._checking_updates = True
        self.update_button.set_sensitive(False)
        self.update_button.set_label(_("Checking…"))
        channel = self.window.settings.get("roblox_channel", "")
        target = core.required_client_update(self.window.last_log)
        if target and "channel" in target:
            channel = target["channel"]

        def done(result, error):
            self._checking_updates = False
            self.update_button.set_sensitive(True)
            if error:
                self.update_button.set_label(_("Check for updates"))
                if install:
                    self.window.end(resume_pending=False)
                if on_complete:
                    on_complete(error)
                else:
                    _toast(self.window.toasts, _("Could not check: {error}", error=error))
                return
            version, upload = result
            installed = core.installed_version()
            if installed and not core.version_is_newer(version, installed) and not force_download:
                self.update_button.set_label(_("Check for updates"))
                if install:
                    # A previous attempt may have downloaded Roblox but failed
                    # during preparation. Retry preparation without downloading
                    # or replacing that already-current bundle again.
                    self.install_update(upload, on_progress=on_progress, on_complete=on_complete,
                                        _claimed=True, _prepare_only=True)
                else:
                    _toast(self.window.toasts, _("No newer version was reported for this channel"))
                return
            self._set_update_action(_("Update to {version}", version=version),
                                    lambda: self.install_update(upload, expected_version=version, channel=channel))
            if install:
                self.install_update(upload, on_progress=on_progress, on_complete=on_complete,
                                    _claimed=True, expected_version=version, channel=channel)

        self._in_thread(lambda: core.latest_version(channel, required_log=self.window.last_log), done)
        return True

    def install_update(self, upload, on_progress=None, on_complete=None,
                       _claimed=False, _prepare_only=False, expected_version=None, channel=None):
        if not _claimed and not self.window.begin("updating"):
            return False
        self.update_button.set_sensitive(False)
        self.progress.set_visible(True)
        shown = [-1.0, ""]

        def progress(fraction, text):
            # One update per half percent, not two per 64 KiB chunk.
            if fraction - shown[0] < 0.005 and text[:12] == shown[1][:12] and fraction < 1:
                return
            shown[:] = [fraction, text]
            def show_progress():
                self.progress.set_fraction(fraction)
                self.progress.set_text(text)
                if on_progress:
                    on_progress(fraction, text)
            GLib.idle_add(show_progress)

        def done(backup, error):
            if error is None and not _prepare_only and channel is not None:
                self.window.set_setting("roblox_channel", channel)
                self.channel_row.set_subtitle(channel or _("Production (default)"))
            self.window.end(resume_pending=error is None)
            self.update_button.set_sensitive(True)
            self._set_update_action(_("Check for updates"), self.check_updates)
            self.progress.set_visible(False)
            self.version_row.set_subtitle(core.installed_version() or _("not found"))
            self.window.play_page.refresh()
            if on_complete:
                on_complete(error)
            elif error:
                _error_dialog(self.window, _("Update failed"), str(error) or repr(error))
            else:
                _toast(self.window.toasts, _("Roblox updated, the old version is in backups/") if backup
                       else _("Roblox installed"))

        # A fresh client does not start until the shim is rebuilt and the
        # Darling prefix is restarted (stale launchd and daemons from the old
        # client), so both run as part of the same progress bar.
        running = self.window.session is not None

        def work():
            backup = None
            if not _prepare_only:
                backup = core.update_roblox(upload, lambda fraction, text: progress(fraction * 0.93, text),
                                            expected_version=expected_version)
            progress(0.94, _("Rebuilding the shim…"))
            ok, output = core.build_shim()
            if not ok:
                raise RuntimeError(output)
            if not running:
                progress(0.97, _("Restarting Darling…"))
                core.restart_darling()
            progress(1.0, _("Done"))
            return backup

        self._in_thread(work, done)
        return True

    def logout(self):
        dialog = Adw.AlertDialog(
            heading=_("Sign out?"),
            body=_("The saved Roblox session will be deleted, you will need to sign in again next time."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("logout", _("Sign out of Roblox"))
        dialog.set_response_appearance("logout", Adw.ResponseAppearance.DESTRUCTIVE)

        def response(_dialog, result):
            # A running game would write its session back on the next cookie change.
            if result != "logout" or not self.window.begin("signing out"):
                return

            def done(gone, error):
                self.window.end()
                if gone:
                    _toast(self.window.toasts, _("Session deleted"))
                else:
                    _error_dialog(self.window, _("Could not sign out"),
                                  str(error) if error else
                                  _("The saved session is still there. Press Restart Darling in "
                                    "Diagnostics and try again."))

            self._in_thread(core.logout, done)

        dialog.connect("response", response)
        dialog.present(self.window)

    def _throttle_state_done(self, state, _error):
        if not hasattr(self, "throttle_row"):
            return
        row = self.throttle_row
        # Adw.ButtonRow has no subtitle on libadwaita < 1.7; the state goes
        # into the button text there and into the subtitle where supported.
        def say(title, subtitle):
            row.set_title(title)
            if hasattr(row, "set_subtitle"):
                row.set_subtitle(subtitle)
        row.set_sensitive(state != "unsupported")
        if state == "patched":
            say(_("Remove the throttle patch"),
                _("Applied: the menu renders at full speed from the first second"))
            row.add_css_class("destructive-action")
        elif state == "original":
            say(_("Install the throttle patch"),
                _("Not applied: the menu may run at ~3 FPS for the first 10 seconds"))
            row.remove_css_class("destructive-action")
        else:
            say(_("Not available for this Roblox build"), "")

    def toggle_throttle_patch(self):
        if not self.window.begin("patching"):
            return
        remove = core.throttle_patch_state() == "patched"

        def work():
            # The button is the manual path: run the patcher directly, not
            # apply_throttle_patch, which is the automatic path and does
            # nothing while auto_patch_throttle is off.
            return core.remove_throttle_patch() if remove else core.run_throttle_patcher()

        def done(_result, error):
            self.window.end()
            if error:
                _error_dialog(self.window, _("Throttle patch failed"), str(error) or repr(error))
            else:
                # Keep the switch in step with the button: a hand-removed
                # patch must not be re-applied on the next launch, and a
                # hand-installed one is worth keeping that way.
                self.auto_patch_switch.set_active(not remove)
            self._in_thread(core.throttle_patch_state, self._throttle_state_done)

        self._in_thread(work, done)

    def delete_roblox(self):
        if self.window.session:
            _toast(self.window.toasts, _("Close Roblox first"))
            return
        if not core.installed_version():
            _toast(self.window.toasts, _("Roblox is not installed"))
            return
        dialog = Adw.AlertDialog(
            heading=_("Delete Roblox?"),
            body=_("RobloxPlayer.app and all mod modifications will be removed from your computer.")
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def response(_dialog, result):
            if result == "delete":
                core.delete_roblox()
                self.version_row.set_subtitle(_("not found"))
                self.window.play_page.refresh()
                _toast(self.window.toasts, _("Roblox deleted successfully"))

        dialog.connect("response", response)
        dialog.present(self.window)

    def rebuild(self):
        if not self.window.begin("building"):
            return
        _toast(self.window.toasts, _("Building the shim…"))

        def done(result, error):
            self.window.end()
            ok, output = result if result else (False, str(error))
            if ok:
                _toast(self.window.toasts, _("Shim built"))
            else:
                _error_dialog(self.window, _("Could not build the shim"), output)

        self._in_thread(core.build_shim, done)

    def restart_darling(self):
        if not self.window.begin("restarting"):
            return

        def done(_result, error):
            self.window.end()
            if error:
                _toast(self.window.toasts, _("Could not restart Darling: {error}", error=error))
            else:
                _toast(self.window.toasts, _("Darling stopped, it starts with the next game"))

        self._in_thread(core.restart_darling, done)


ABOUT = ("Mac O’ Blox runs the real Roblox client for macOS on Linux through Darling. "
         "It is not made by Roblox and is not affiliated with it.")


def _links():
    """(title, icon, uri) of the project's community pages."""
    links = [("Discord", "macoblox-discord-symbolic", author.DISCORD_URL)]
    if author.GITHUB_URL:
        links.append(("GitHub", "macoblox-github-symbolic", author.GITHUB_URL))
    return links


def _page_sidebar(stack):
    """The sidebar's page list. Adw.ViewSwitcherSidebar needs libadwaita 1.9;
    older ones (Ubuntu 24.04 has 1.5, Debian 13 1.7) get a plain list of the
    same pages."""
    if hasattr(Adw, "ViewSwitcherSidebar"):
        sidebar = Adw.ViewSwitcherSidebar()
        sidebar.set_stack(stack)
        return sidebar
    names = []
    rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
    rows.add_css_class("navigation-sidebar")
    pages = stack.get_pages()
    for index in range(pages.get_n_items()):
        page = pages.get_item(index)
        line = Gtk.Box(spacing=12)
        line.append(Gtk.Image(icon_name=page.get_icon_name()))
        line.append(Gtk.Label(label=page.get_title(), xalign=0))
        rows.append(line)
        names.append(page.get_name())

    def selected(_rows, row):
        if row is not None and stack.get_visible_child_name() != names[row.get_index()]:
            stack.set_visible_child_name(names[row.get_index()])

    def follow(*_args):
        name = stack.get_visible_child_name()
        if name in names:
            row = rows.get_row_at_index(names.index(name))
            if rows.get_selected_row() is not row:
                rows.select_row(row)

    rows.connect("row-selected", selected)
    stack.connect("notify::visible-child-name", follow)
    follow()
    return rows


def _open_uri(window, uri):
    Gtk.UriLauncher.new(uri).launch(window, None, None, None)


class InfoPage(Adw.PreferencesPage):
    def __init__(self, window):
        super().__init__(title=_("Info"), icon_name="help-about-symbolic")
        self.window = window

        about = Adw.PreferencesGroup(title="Mac O’ Blox", description=_(ABOUT))
        self.add(about)

        community = Adw.PreferencesGroup(title=_("Community"))
        for title, icon, uri in _links():
            row = Adw.ActionRow(title=title, activatable=True)
            row.add_prefix(Gtk.Image(icon_name=icon))
            row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
            row.connect("activated", lambda *_args, u=uri: _open_uri(window, u))
            community.add(row)
        self.add(community)

        made_by = Adw.PreferencesGroup(title=_("Authors"))
        self.avatar = Adw.Avatar(size=48, text=author.NAME, show_initials=True)
        profile = Adw.ActionRow(title=author.NAME, activatable=True,
                                subtitle=_("{user} on Roblox", user="@" + author.ROBLOX_USER))
        profile.add_prefix(self.avatar)
        profile.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        profile.connect("activated", lambda *_args: _open_uri(window, author.PROFILE_URL))
        made_by.add(profile)
        maintainer_avatar = Adw.Avatar(size=48, text=author.MAINTAINER, show_initials=True)
        try:
            maintainer_avatar.set_custom_image(
                Gdk.Texture.new_from_filename(str(author.MAINTAINER_AVATAR)))
        except GLib.Error:
            pass
        maintainer = Adw.ActionRow(title=author.MAINTAINER, activatable=True,
                                   subtitle=_("Maintains this version: stability and performance fixes"))
        maintainer.add_prefix(maintainer_avatar)
        maintainer.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        maintainer.connect("activated", lambda *_args: _open_uri(window, author.MAINTAINER_URL))
        made_by.add(maintainer)

        self.ui_contributor_avatar = Adw.Avatar(size=48, text=author.UI_CONTRIBUTOR, show_initials=True)
        ui_contributor = Adw.ActionRow(title=author.UI_CONTRIBUTOR, activatable=True,
                                       subtitle=_("Better UI, Mods"))
        ui_contributor.add_prefix(self.ui_contributor_avatar)
        ui_contributor.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        ui_contributor.connect("activated", lambda *_args: _open_uri(window, author.UI_CONTRIBUTOR_URL))
        made_by.add(ui_contributor)

        claude = Adw.ActionRow(title=_("Assisted with Claude Opus 5.5"), activatable=True,
                               subtitle=_("Anthropic's AI assisted writing the code together with the authors"))
        claude.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        claude.connect("activated", lambda *_args: _open_uri(window, "https://www.anthropic.com/claude"))
        made_by.add(claude)
        self.add(made_by)

        settings = dict(window.settings)
        threading.Thread(target=lambda: GLib.idle_add(self._show_avatar, author.avatar(settings)),
                         daemon=True).start()
        threading.Thread(target=lambda: GLib.idle_add(self._show_tinytosha_avatar, author.tinytosha_avatar()),
                         daemon=True).start()

    def _show_avatar(self, path):
        if path:
            try:
                self.avatar.set_custom_image(Gdk.Texture.new_from_filename(str(path)))
            except GLib.Error:
                pass
        return False

    def _show_tinytosha_avatar(self, path):
        if path:
            try:
                self.ui_contributor_avatar.set_custom_image(Gdk.Texture.new_from_filename(str(path)))
            except GLib.Error:
                pass
        return False


class ModsPage(Adw.Bin):
    def __init__(self, window):
        super().__init__()
        self.window = window
        settings = window.settings

        page = Adw.PreferencesPage()

        # 1. Sound presets
        sounds_group = Adw.PreferencesGroup(title=_("Sound presets"))

        death_choices = [
            ("default", _("Default (Roblox)")),
            ("classic_oof", _("Classic OOF")),
            ("custom", _("Custom sound")),
        ]
        self._death_codes = [c[0] for c in death_choices]
        cur_death = settings.get("mod_death_sound", "default")
        cur_death_idx = self._death_codes.index(cur_death) if cur_death in self._death_codes else 0

        self.death_sound_row = Adw.ComboRow(
            title=_("Death sound"),
            subtitle=_("Choose the sound played when your character resets or dies"),
            model=Gtk.StringList.new([c[1] for c in death_choices]),
            selected=cur_death_idx,
        )
        self.death_sound_row.connect("notify::selected", self._on_death_sound_selected)
        sounds_group.add(self.death_sound_row)

        self.custom_death_row = Adw.ActionRow(
            title=_("Custom death sound file"),
            subtitle=settings.get("mod_custom_death_sound") or _("No file chosen"),
            visible=(cur_death == "custom"),
        )
        custom_death_btn = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        custom_death_btn.connect("clicked", lambda *_args: self._choose_custom_death_sound())
        self.custom_death_row.add_suffix(custom_death_btn)
        sounds_group.add(self.custom_death_row)

        old_sounds_switch = Adw.SwitchRow(
            title=_("Classic movement sounds"),
            subtitle=_("Restores 2006-2014 walking, jumping, getting up, and silent landing sounds"),
            active=settings.get("mod_old_character_sounds", False),
        )
        old_sounds_switch.connect("notify::active", lambda row, _pspec: window.set_setting(
            "mod_old_character_sounds", row.get_active()))
        sounds_group.add(old_sounds_switch)

        page.add(sounds_group)

        # 2. Mouse cursors
        cursors_group = Adw.PreferencesGroup(title=_("Mouse cursors"))

        cursor_choices = [
            ("default", _("Default")),
            ("2006", _("2006 Classic")),
            ("2013", _("2013 Retro")),
            ("dot", _("Black & White Dot")),
            ("purple_cross", _("Purple Cross")),
            ("custom", _("Custom cursor")),
        ]
        self._cursor_codes = [c[0] for c in cursor_choices]
        cur_cursor = settings.get("mod_cursor_type", "default")
        cur_cursor_idx = self._cursor_codes.index(cur_cursor) if cur_cursor in self._cursor_codes else 0

        self.cursor_row = Adw.ComboRow(
            title=_("Cursor style"),
            subtitle=_("Replaces in-game mouse cursors"),
            model=Gtk.StringList.new([c[1] for c in cursor_choices]),
            selected=cur_cursor_idx,
        )
        self.cursor_row.connect("notify::selected", self._on_cursor_selected)
        cursors_group.add(self.cursor_row)

        self.custom_cursor_row = Adw.ActionRow(
            title=_("Custom cursor file or folder"),
            subtitle=settings.get("mod_custom_cursor") or _("No file chosen"),
            visible=(cur_cursor == "custom"),
        )
        custom_cursor_btn = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        custom_cursor_btn.connect("clicked", lambda *_args: self._choose_custom_cursor())
        self.custom_cursor_row.add_suffix(custom_cursor_btn)
        cursors_group.add(self.custom_cursor_row)

        page.add(cursors_group)

        # 3. Typography
        fonts_group = Adw.PreferencesGroup(title=_("Typography"))

        cur_font = settings.get("mod_custom_font", "")
        self.font_row = Adw.ActionRow(
            title=_("Custom font"),
            subtitle=Path(cur_font).name if cur_font else _("No custom font selected"),
        )
        choose_font_btn = Gtk.Button(label=_("Choose font…"), valign=Gtk.Align.CENTER)
        choose_font_btn.connect("clicked", lambda *_args: self._choose_font())
        self.font_row.add_suffix(choose_font_btn)

        clear_font_btn = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        clear_font_btn.add_css_class("flat")
        clear_font_btn.set_tooltip_text(_("Clear font"))
        clear_font_btn.connect("clicked", lambda *_args: self._clear_font())
        self.font_row.add_suffix(clear_font_btn)

        fonts_group.add(self.font_row)
        page.add(fonts_group)

        # 4. User modifications
        mods_group = Adw.PreferencesGroup(title=_("User modifications"))

        custom_mods_switch = Adw.SwitchRow(
            title=_("Enable modifications folder"),
            subtitle=_("Overlay files from modifications/ onto the Roblox client"),
            active=settings.get("enable_custom_mods", True),
        )
        custom_mods_switch.connect("notify::active", lambda row, _pspec: window.set_setting(
            "enable_custom_mods", row.get_active()))
        mods_group.add(custom_mods_switch)

        open_folder_row = Adw.ActionRow(
            title=_("Open modifications folder"),
            subtitle=_("Drop your custom textures, sounds, and models here"),
        )
        open_folder_btn = Gtk.Button(label=_("Open"), valign=Gtk.Align.CENTER)
        open_folder_btn.connect("clicked", lambda *_args: self._open_mods_folder())
        open_folder_row.add_suffix(open_folder_btn)
        mods_group.add(open_folder_row)

        page.add(mods_group)

        # 5. Management
        mgmt_group = Adw.PreferencesGroup(title=_("Management"))

        apply_btn = _button_row(_("Apply mods now"))
        apply_btn.connect("activated", lambda *_args: self._apply_mods_now())
        mgmt_group.add(apply_btn)

        reset_btn = _button_row(_("Reset all mods to default"))
        reset_btn.add_css_class("destructive-action")
        reset_btn.connect("activated", lambda *_args: self._reset_mods())
        mgmt_group.add(reset_btn)

        page.add(mgmt_group)

        self.set_child(page)

    def _on_death_sound_selected(self, row, _pspec):
        code = self._death_codes[row.get_selected()]
        self.window.set_setting("mod_death_sound", code)
        self.custom_death_row.set_visible(code == "custom")

    def _on_cursor_selected(self, row, _pspec):
        code = self._cursor_codes[row.get_selected()]
        self.window.set_setting("mod_cursor_type", code)
        self.custom_cursor_row.set_visible(code == "custom")

    def _choose_custom_death_sound(self):
        dialog = Gtk.FileChooserNative.new(
            _("Select custom death sound (.ogg)"),
            self.window,
            Gtk.FileChooserAction.OPEN,
            _("Select"),
            _("Cancel"),
        )
        f = Gtk.FileFilter()
        f.set_name("Audio files (*.ogg)")
        f.add_pattern("*.ogg")
        dialog.add_filter(f)

        def on_response(d, res):
            if res == Gtk.ResponseType.ACCEPT:
                file = d.get_file()
                if file:
                    path = file.get_path()
                    self.window.set_setting("mod_custom_death_sound", path)
                    self.custom_death_row.set_subtitle(path)
            d.destroy()

        dialog.connect("response", on_response)
        dialog.show()

    def _choose_custom_cursor(self):
        dialog = Gtk.FileChooserNative.new(
            _("Select custom cursor (.png or folder)"),
            self.window,
            Gtk.FileChooserAction.OPEN,
            _("Select"),
            _("Cancel"),
        )
        f = Gtk.FileFilter()
        f.set_name("PNG Images (*.png)")
        f.add_pattern("*.png")
        dialog.add_filter(f)

        def on_response(d, res):
            if res == Gtk.ResponseType.ACCEPT:
                file = d.get_file()
                if file:
                    path = file.get_path()
                    self.window.set_setting("mod_custom_cursor", path)
                    self.custom_cursor_row.set_subtitle(path)
            d.destroy()

        dialog.connect("response", on_response)
        dialog.show()

    def _choose_font(self):
        dialog = Gtk.FileChooserNative.new(
            _("Select font (.ttf, .otf)"),
            self.window,
            Gtk.FileChooserAction.OPEN,
            _("Select"),
            _("Cancel"),
        )
        f = Gtk.FileFilter()
        f.set_name("Font files (*.ttf, *.otf)")
        f.add_pattern("*.ttf")
        f.add_pattern("*.otf")
        dialog.add_filter(f)

        def on_response(d, res):
            if res == Gtk.ResponseType.ACCEPT:
                file = d.get_file()
                if file:
                    path = file.get_path()
                    self.window.set_setting("mod_custom_font", path)
                    self.font_row.set_subtitle(Path(path).name)
            d.destroy()

        dialog.connect("response", on_response)
        dialog.show()

    def _clear_font(self):
        self.window.set_setting("mod_custom_font", "")
        self.font_row.set_subtitle(_("No custom font selected"))

    def _open_mods_folder(self):
        folder = mods.ensure_mods_dir()
        try:
            Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)
        except Exception:
            import subprocess
            subprocess.Popen(["xdg-open", str(folder)])

    def _apply_mods_now(self):
        try:
            mods.apply_mods(self.window.settings)
            _toast(self.window.toasts, _("Mods applied"))
        except Exception as e:
            _error_dialog(self.window, _("Error applying mods"), str(e))

    def _reset_mods(self):
        try:
            mods.restore_all_mods()
            _toast(self.window.toasts, _("All mods have been reset"))
        except Exception as e:
            _error_dialog(self.window, _("Error resetting mods"), str(e))


class LauncherWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Mac O’ Blox")
        self.set_default_size(760, 580)
        self.set_resizable(True)
        self.settings = core.load_settings()
        i18n.set_language(self.settings.get("language", "en"))
        self.session = None
        self.rpc = None
        self.game_tracker = None
        self.current_game_info = None
        # One long operation at a time: starting, updating, building,
        # restarting Darling or signing out. They share the client and Darling.
        self.busy = None
        self.quit_when_idle = False  # the window was closed during an operation
        self.last_log = self._find_last_log()
        self.pending_uri = None
        self.setup_active = False
        setup_action = Gio.SimpleAction.new("setup", None)
        setup_action.connect("activate", lambda *_args: self.show_setup())
        self.add_action(setup_action)
        # MACOBLOX_PAGE opens another tab first (for screenshots).
        self.build(os.environ.get("MACOBLOX_PAGE", "play"))
        if not self.setup_active:
            threading.Thread(target=self._check_startup_update, daemon=True).start()

    def build(self, page):
        """(Re)create the interface, e.g. after the language changes."""
        if getattr(self, "settings_page", None):
            self.settings_page.flush()  # the new page reads the file
        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.play_page = PlayPage(self)
        self.stack.add_titled_with_icon(self.play_page, "play", _("Play"), "media-playback-start-symbolic")
        self.settings_page = SettingsPage(self)
        self.stack.add_titled_with_icon(self.settings_page, "settings", _("Settings"), "emblem-system-symbolic")
        self.mods_page = ModsPage(self)
        self.stack.add_titled_with_icon(self.mods_page, "mods", _("Mods"), "application-x-addon-symbolic")
        self.info_page = InfoPage(self)
        self.stack.add_titled_with_icon(self.info_page, "info", _("Info"), "help-about-symbolic")
        self.flags_page = self.settings_page.flags_page

        if page in ("flags", "env", "roblox"):
            self.stack.set_visible_child_name("settings")
            self.settings_page.set_tab(page)
        else:
            self.stack.set_visible_child_name(page)

        # Official Libadwaita Split View layout
        self.split = Adw.OverlaySplitView()
        self.split.set_min_sidebar_width(200)
        self.split.set_max_sidebar_width(260)
        self.split.set_sidebar_width_fraction(0.28)
        self.split.set_show_sidebar(self.settings.get("show_sidebar", True))

        # Sidebar with the page list
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        sidebar_header.set_title_widget(Gtk.Label(label="Mac O’ Blox", css_classes=["heading"]))
        sidebar_version = Gtk.Label(label=f"v{__version__}", css_classes=["dim-label", "caption"], margin_end=6)
        sidebar_header.pack_end(sidebar_version)
        sidebar_toolbar.add_top_bar(sidebar_header)

        sidebar_toolbar.set_content(_page_sidebar(self.stack))
        self.split.set_sidebar(sidebar_toolbar)

        # Content area
        content_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append(_("Setup guide"), "win.setup")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text=_("Launcher menu")))

        sidebar_toggle = Gtk.Button(icon_name="sidebar-show-symbolic")
        sidebar_toggle.add_css_class("flat")
        sidebar_toggle.set_tooltip_text(_("Toggle sidebar"))
        sidebar_toggle.connect("clicked", lambda *_args: self.toggle_sidebar())
        header.pack_start(sidebar_toggle)

        content_view.add_top_bar(header)
        self.toasts.set_child(self.stack)
        content_view.set_content(self.toasts)
        content_view.set_hexpand(True)
        content_view.set_vexpand(True)
        self.split.set_content(content_view)

        self.setup = SetupWizard(self)
        self.content_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                       transition_duration=150)
        self.content_stack.add_named(self.split, "launcher")
        self.content_stack.add_named(self.setup, "setup")
        self.set_content(self.content_stack)
        if page == "setup" or (not core.installed_version() and not self.settings.get("setup_complete", False)):
            self.show_setup()
        else:
            self.setup_active = False
            self.content_stack.set_visible_child_name("launcher")

    def show_setup(self, page="welcome"):
        if self.session or self.busy:
            _toast(self.toasts, _("Close Roblox first") if self.session else
                   _("Please wait, the launcher is busy"))
            return
        self.setup_active = True
        self.content_stack.set_visible_child_name("setup")
        self.setup.show(page)

    def dismiss_setup(self):
        if self.setup.installing:
            return
        self.setup_active = False
        self.content_stack.set_visible_child_name("launcher")

    def complete_setup(self, launch=False):
        self.set_setting("setup_complete", True)
        self.dismiss_setup()
        self.stack.set_visible_child_name("play")
        self.play_page.refresh()
        if launch:
            self.play_clicked()

    def toggle_sidebar(self):
        show = not self.split.get_show_sidebar()
        self.split.set_show_sidebar(show)
        self.set_setting("show_sidebar", show)

    def _check_startup_update(self):
        try:
            has_update, tag, html_url = core.check_launcher_update()
            if has_update:
                GLib.idle_add(self._show_launcher_update_dialog, tag)
                return
        except Exception:
            pass

        if self.settings.get("auto_check_roblox_updates", True):
            try:
                installed = core.installed_version()
                if installed:
                    channel = self.settings.get("roblox_channel", "")
                    target = core.required_client_update(self.last_log)
                    if target and "channel" in target:
                        channel = target["channel"]
                    latest, upload = core.latest_version(channel, required_log=self.last_log)
                    if core.version_is_newer(latest, installed):
                        GLib.idle_add(self._show_roblox_update_dialog, latest, upload, channel)
            except Exception:
                pass

    def _show_launcher_update_dialog(self, tag):
        dialog = Adw.AlertDialog(
            heading=_("Update available"),
            body=_("A new version of Mac O’ Blox ({version}) is available. Update now?", version=tag)
        )
        dialog.add_response("later", _("Later"))
        dialog.add_response("update", _("Update"))
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)

        def response(_dialog, result):
            if result == "update":
                self.stack.set_visible_child_name("settings")
                self.settings_page.set_tab("env")
                self.settings_page.force_update_launcher()

        dialog.connect("response", response)
        dialog.present(self)

    def _show_roblox_update_dialog(self, latest, upload, channel=None):
        dialog = Adw.AlertDialog(
            heading=_("Roblox update available"),
            body=_("A newer version of Roblox ({version}) is available. Update now?", version=latest),
        )
        dialog.add_response("later", _("Later"))
        dialog.add_response("update", _("Update"))
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)

        def response(_dialog, result):
            if result == "update":
                self.stack.set_visible_child_name("settings")
                self.settings_page.set_tab("roblox")
                self.settings_page.install_update(upload, expected_version=latest, channel=channel)

        dialog.connect("response", response)
        dialog.present(self)

    def begin(self, what):
        """Claim the launcher for one long operation; False, with a message,
        while the game runs or another operation is under way."""
        if self.session or self.busy:
            _toast(self.toasts, _("Close Roblox first") if self.session else
                   _("Please wait, the launcher is busy"))
            return False
        self.busy = what
        self.play_page.refresh()
        return True

    def end(self, resume_pending=True):
        self.busy = None
        if self.quit_when_idle:
            self.get_application().quit()
            return
        self.play_page.refresh()
        if resume_pending and self.pending_uri and core.installed_version() and not self.setup_active:
            pending = self.pending_uri
            GLib.idle_add(self.handle_uri, pending)

    def set_setting(self, key, value):
        self.settings[key] = value
        core.save_settings(self.settings)

    def set_language(self, code):
        if code == i18n.language():
            return
        self.set_setting("language", code)
        i18n.set_language(code)
        # Rebuild after the combo row finished handling its own signal.
        GLib.idle_add(lambda: self.build("settings") and False)

    def _captcha_dialog(self):
        dialog = Adw.AlertDialog(
            heading=_("Roblox closed at the captcha"),
            body=_("Signing up and signing in with a password show a captcha in a built-in browser. "
                   "The launcher shows it in a window of its own when WebKitGTK 6.0 is installed "
                   "(webkitgtk-6.0, gir1.2-webkit-6.0 or webkitgtk6.0). Without it, create the "
                   "account on roblox.com, then sign in with Quick Login: Roblox shows a code, "
                   "enter it on a phone or in a browser where you are already signed in."))
        dialog.add_response("ok", _("OK"))
        dialog.present(self)

    def play_clicked(self):
        if core.installed_version():
            if self.pending_uri:
                self.handle_uri(self.pending_uri)
            else:
                self.launch()
        else:
            self.show_setup("overview")

    def handle_uri(self, browser_uri):
        """Start Roblox with an opaque browser handoff when it is ready."""
        # The activation callback may have been queued just before another
        # browser click arrived.  Prefer the still-pending complete argument
        # so the newest handoff is the one that starts the client.
        browser_uri = uri_handoff.peek_pending() or browser_uri
        self.pending_uri = browser_uri
        if (self.session or self.busy or self.setup_active or
                getattr(self, "_update_required_dialog_active", False)):
            # The pending file remains in place.  A later activation or the
            # end of the current operation will retry this exact argument.
            return
        if not core.installed_version():
            self.show_setup("overview")
            return
        # Keep the file until RobloxSession has started successfully.  If
        # Darling or the shim fails, the browser handoff can still be retried.
        self.launch(browser_uri)

    def studio_clicked(self):
        if studio.running():
            _toast(self.toasts, _("Roblox Studio is already running"))
            return
        if not studio.needs_install():
            self._update_and_start_studio()
            return
        dialog = Adw.AlertDialog(
            heading=_("Install Roblox Studio?"),
            body=_("Studio runs in its Windows version through Wine. Mac O’ Blox downloads Wine, "
                   "DXVK and Studio, about 800 MB."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("install", _("Install"))
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, result: result == "install" and self._update_and_start_studio())
        dialog.present(self)

    def _update_and_start_studio(self):
        """Installs or updates Studio when needed, then starts it."""
        page = self.play_page
        page.studio.set_sensitive(False)
        page.studio.set_label(_("Checking…"))

        def progress(fraction, text):
            GLib.idle_add(page.studio_progress.set_visible, True)
            GLib.idle_add(page.studio_progress.set_fraction, fraction)
            GLib.idle_add(page.studio_progress.set_text, text)

        def work():
            try:
                studio.install(progress)
            except Exception as error:
                if studio.needs_install():
                    raise
                # Offline or Roblox unreachable: start the installed version.
                print("Studio update skipped:", error)
            studio.launch()

        def done(_result, error):
            page.studio.set_sensitive(True)
            page.studio.set_label(_("Roblox Studio"))
            page.studio_progress.set_visible(False)
            if error:
                _error_dialog(self, _("Could not start Roblox Studio"), str(error) or repr(error))
            else:
                _toast(self.toasts, _("Starting Roblox Studio…"))

        def run():
            try:
                work()
                GLib.idle_add(done, None, None)
            except Exception as error:
                GLib.idle_add(done, None, error)

        threading.Thread(target=run, daemon=True).start()

    def launch(self, launch_uri=None):
        if not self.begin("starting"):
            return
        self.settings_page.flush()
        session = core.RobloxSession(dict(self.settings), launch_uri=launch_uri)
        self.web = self._web_bridge()
        if self.web:
            session.web_socket = self.web.guest_path
            session.web_user_agent = self.web.user_agent

        def start():
            try:
                session.start()  # cleans up after itself when it fails
                GLib.idle_add(self._started, session, None)
            except Exception as error:
                GLib.idle_add(self._started, session, error)

        threading.Thread(target=start, daemon=True).start()

    def _web_bridge(self):
        """The browser window for Roblox's embedded pages (sign-in with a
        password, purchases), or None when WebKitGTK is not installed."""
        try:
            from . import web
            return web.WebBridge(self)
        except Exception as error:  # a missing WebKit, a socket that cannot be made
            print("Embedded web pages are not available:", error)
            return None

    def _stop_web(self):
        if getattr(self, "web", None):
            self.web.stop()
            self.web = None

    def set_discord_rpc(self, enabled):
        self.set_setting("discord_rpc", enabled)
        if hasattr(self, "settings_page"):
            if hasattr(self.settings_page, "discord_game"):
                self.settings_page.discord_game.set_sensitive(enabled)
            if hasattr(self.settings_page, "discord_icon"):
                self.settings_page.discord_icon.set_sensitive(enabled)
            if hasattr(self.settings_page, "discord_time"):
                self.settings_page.discord_time.set_sensitive(enabled)
        if not enabled:
            self._stop_rpc()
        elif self.session:
            self._start_rpc()

    def set_discord_rpc_option(self, key, value):
        self.set_setting(key, value)
        if self.session and self.settings.get("discord_rpc", True):
            self._refresh_rpc_presence()

    def set_show_playtime(self, enabled):
        self.set_setting("show_playtime", enabled)
        self.play_page.refresh_playtime()

    def _on_game_activity_change(self, info: dict | None, session, tracker, generation):
        # The log and metadata workers only enqueue; GTK-owned state changes
        # after checking that this callback still belongs to the active launch.
        GLib.idle_add(self._apply_game_activity_change, info, session, tracker, generation)

    def _apply_game_activity_change(self, info, session, tracker, generation):
        if (self.session is not session or self.game_tracker is not tracker or
                not self.settings.get("discord_rpc", True) or
                not tracker.is_current_generation(generation)):
            return False
        self.current_game_info = info
        self._refresh_rpc_presence()
        return False

    def _refresh_rpc_presence(self):
        if not self.session or not self.settings.get("discord_rpc", True):
            return False
        if self.rpc is None or self.rpc.closed:
            try:
                self.rpc = discord.DiscordRPC()
            except RuntimeError as error:
                print("Could not start Discord presence worker:", error)
                return False
        rpc = self.rpc
        start = (
            getattr(self, "game_started_at", time.time())
            if self.settings.get("discord_rpc_time", True)
            else None
        )
        info = getattr(self, "current_game_info", None)

        if info and not info.get("loading") and self.settings.get("discord_rpc_game", True):
            details = info.get("name", _("Playing Roblox"))
            creator = info.get("creator")
            state = _("by {creator}", creator=creator) if creator else _("In Game")
            icon_url = info.get("icon_url")
            use_icon = self.settings.get("discord_rpc_icon", False) and bool(icon_url)
            large_image = icon_url if use_icon else "macoblox"
            large_text = details
            small_image = "macoblox" if use_icon else None
            small_text = "Mac O’ Blox" if use_icon else None
        elif info:
            # Game is active (either fetching details or user hid experience name in settings)
            details = _("Playing Roblox")
            state = _("In Game")
            large_image = "macoblox"
            large_text = "Mac O’ Blox"
            small_image = None
            small_text = None
        else:
            # Menu (not in an experience)
            details = _("In Main Menu")
            state = None
            large_image = "macoblox"
            large_text = "Mac O’ Blox"
            small_image = None
            small_text = None

        rpc.queue_presence(
            details=details,
            state=state,
            start_time=start,
            large_image=large_image,
            large_text=large_text,
            small_image=small_image,
            small_text=small_text,
        )
        return False

    def _start_rpc(self):
        session = self.session
        if not session or not self.settings.get("discord_rpc", True):
            return
        if self.game_tracker is None and session.log_path:
            tracker = discord.GameActivityTracker(
                session.log_path,
                lambda info, generation: self._on_game_activity_change(
                    info, session, tracker, generation),
                autostart=False,
            )
            self.game_tracker = tracker
            try:
                tracker.start()
            except RuntimeError as error:
                self.game_tracker = None
                print("Could not start Discord game tracker:", error)
        self._refresh_rpc_presence()

    def _stop_rpc(self):
        tracker, self.game_tracker = self.game_tracker, None
        if tracker:
            tracker.stop()
        self.current_game_info = None
        if getattr(self, "rpc", None):
            rpc = self.rpc
            self.rpc = None
            rpc.close()

    def _started(self, session, error):
        self.busy = None
        self.quit_when_idle = False  # a game or an error to show: stay
        if error:
            self._stop_web()
            if session and session.launch_uri:
                self.pending_uri = session.launch_uri
            self.set_visible(True)
            self.play_page.refresh()
            _error_dialog(self, _("Could not start Roblox"), str(error) or repr(error))
            return
        self._stop_rpc()
        self.session = session
        if session.launch_uri:
            # A newer click may have replaced pending-uri while Darling was
            # starting; clear only the argument this session consumed.
            uri_handoff.clear_pending(session.launch_uri)
            self.pending_uri = uri_handoff.peek_pending()
        self.last_log = session.log_path
        self.game_started_at = time.time()
        self.last_playtime_save = time.time()
        self.current_game_info = None
        if hasattr(self, "play_page") and hasattr(self.play_page, "logs_view"):
            self.play_page.logs_view.reset(session.log_path)
            self.play_page.logs_view.update()
        self.play_page.refresh()
        if self.settings.get("discord_rpc", True):
            self._start_rpc()
        # Hide once the game window has had time to appear.
        GLib.timeout_add_seconds(3, self._hide_while_playing)
        GLib.timeout_add(1000, self._watch, session)

    def _hide_while_playing(self):
        if self.session and self.settings.get("hide_launcher_on_launch", True):
            self.set_visible(False)
        return False

    def _watch(self, session):
        # A timer belongs to one launch. It must never start polling a new
        # session if the previous one finished between timer turns.
        if self.session is not session:
            return False
        if getattr(self, "_watch_inflight", None) is not None:
            return True
        self._watch_inflight = session

        def poll():
            try:
                status = session.poll()
            except Exception as error:
                print("Watching the game failed:", error)
                try:
                    session.finish()
                    status = -1
                except Exception as cleanup_error:
                    # Keep ownership of this session until cleanup succeeds;
                    # the next timer turn retries without overlapping launches.
                    print("Cleaning up the game failed:", cleanup_error)
                    status = None
            GLib.idle_add(self._watch_result, session, status)

        try:
            threading.Thread(target=poll, daemon=True).start()
        except RuntimeError as error:
            self._watch_inflight = None
            print("Could not start game watcher:", error)
        return True

    def _watch_result(self, session, status):
        """Apply worker results on GTK's thread after polling/cleanup returns."""
        if getattr(self, "_watch_inflight", None) is session:
            self._watch_inflight = None
        if self.session is not session:
            return False
        if status is None:
            # Active game: track playtime
            self.settings["playtime_seconds"] = self.settings.get("playtime_seconds", 0) + 1
            self.play_page.refresh_playtime()
            if hasattr(self, "play_page") and hasattr(self.play_page, "logs_view"):
                if self.play_page.stack.get_visible_child_name() == "logs":
                    self.play_page.logs_view.update()
            if time.time() - getattr(self, "last_playtime_save", 0) > 15:
                self.last_playtime_save = time.time()
                core.save_settings(self.settings)
            # Retry RPC connection if Discord was launched after the game
            if self.settings.get("discord_rpc", True):
                rpc = getattr(self, "rpc", None)
                if (rpc is None or not rpc.connected) and int(self.settings["playtime_seconds"]) % 5 == 0:
                    self._start_rpc()
            return False
        self.session = None
        self._stop_web()
        self._stop_rpc()
        core.save_settings(self.settings)
        self.play_page.refresh()
        # Roblox's macOS updater cannot reliably replace the host bundle through
        # Darling. It exits with success/a quit sentinel, so this is independent
        # of the frontend exit code and must precede pending-URI auto-retries.
        if core.exit_reason(self.last_log) == "update_required":
            target = core.required_client_update(self.last_log)
            if target and "channel" in target:
                self.set_setting("roblox_channel", target["channel"])
                self.settings_page.channel_row.set_subtitle(target["channel"] or _("Production (default)"))
            self.pending_uri = self.pending_uri or uri_handoff.peek_pending() or session.launch_uri
            self.set_visible(True)
            self.present()
            self._roblox_update_required_dialog(target)
            return False
        pending = self.pending_uri or uri_handoff.peek_pending()
        if pending:
            self.pending_uri = pending
            GLib.idle_add(self.handle_uri, pending)
        failed = status not in (0, -1)
        # A failure is always shown, even with the launcher set to stay closed.
        if failed or self.settings.get("show_launcher_after_exit", True):
            self.set_visible(True)
            self.present()
        else:
            self.get_application().quit()
        if failed:
            reason = core.exit_reason(self.last_log)
            if reason == "captcha":
                self._captcha_dialog()
            elif reason == "x11_broken":
                self._x11_broken_dialog()
            else:
                _toast(self.toasts, _("Roblox exited with code {status}", status=status))
        return False

    def _roblox_update_required_dialog(self, target=None):
        self._update_required_dialog_active = True
        body = _("Roblox closed to install a required update. Let Mac O’ Blox download the current client, then try launching again.")
        if target and target.get("upload"):
            if "channel" in target:
                body = _("Roblox requires version {version} on channel {channel}. Mac O’ Blox will download that exact client, then try launching again.",
                         version=target["version"], channel=target["channel"] or _("Production"))
            else:
                body = _("Roblox requires version {version}. Mac O’ Blox will download that exact client, then try launching again.",
                         version=target["version"])
        dialog = Adw.AlertDialog(
            heading=_("Roblox needs an update"),
            body=body)
        dialog.add_response("later", _("Later"))
        dialog.add_response("update", _("Update Roblox"))
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)

        def response(_dialog, result):
            self._update_required_dialog_active = False
            if result == "update":
                self.stack.set_visible_child_name("settings")
                self.settings_page.set_tab("roblox")
                # Gated channels can return 401 to the launcher's anonymous
                # check. The signed-in client already reported its deployment.
                if target and target.get("upload"):
                    self.settings_page.install_update(target["upload"], expected_version=target["version"],
                                                      channel=target.get("channel"))
                else:
                    self.settings_page.check_updates(install=True, force_download=True)

        dialog.connect("response", response)
        dialog.present(self)

    def _x11_broken_dialog(self):
        has_raw = self.settings.get("raw_mouse", True)
        if has_raw:
            dialog = Adw.AlertDialog(
                heading=_("X11 Connection Lost"),
                body=_("The game crashed because the X11 connection was broken. "
                       "This usually happens when raw mouse input overloads the display server with events. "
                       "Would you like to disable raw mouse input?")
            )
            dialog.add_response("cancel", _("Keep Enabled"))
            dialog.add_response("disable", _("Disable Raw Mouse"))
            dialog.set_response_appearance("disable", Adw.ResponseAppearance.SUGGESTED)

            def on_response(_d, result):
                if result == "disable":
                    self.set_setting("raw_mouse", False)
                    if hasattr(self, "settings_page"):
                        self.settings_page.refresh_raw_mouse()
                    _toast(self.toasts, _("Raw mouse input disabled"))

            dialog.connect("response", on_response)
            dialog.present(self)
        else:
            _error_dialog(
                self,
                _("X11 Connection Lost"),
                _("The game crashed because the X11 connection was broken (explicit kill or server shutdown).")
            )

    def stop(self):
        threading.Thread(target=core.stop_roblox, daemon=True).start()

    def _find_last_log(self):
        if not core.LOGS.exists():
            return None
        logs = sorted(core.LOGS.glob("launch-*.log"), key=lambda p: p.stat().st_mtime)
        return logs[-1] if logs else None

    def open_last_log(self):
        log_path = (self.session.log_path if self.session else None) or self.last_log or self._find_last_log()
        if log_path:
            self.last_log = log_path
            if hasattr(self, "play_page") and hasattr(self.play_page, "show_logs"):
                self.play_page.show_logs(log_path)
                return
        self.open_external_log(log_path)

    def open_external_log(self, log_path=None):
        path = log_path or (self.session.log_path if self.session else None) or self.last_log or self._find_last_log()
        if not path or not path.exists():
            _toast(self.toasts, _("No log found"))
            return
        try:
            Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        except (GLib.Error, OSError) as error:
            _toast(self.toasts, _("Could not open the log: {error}", error=error))


class LauncherApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None

    def do_activate(self):
        pending = uri_handoff.peek_pending()
        if not self.window:
            Gtk.Window.set_default_icon_name("macoblox")
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(
                str(core.PROJECT / "launcher" / "icons"))
            self.window = LauncherWindow(self)
            # Keep running while the window is hidden during a game.
            self.hold()
            self.window.connect("close-request", self._close)
        self.window.set_visible(True)
        self.window.present()
        if os.environ.get("MACOBLOX_PAGE"):
            # Screenshots: no focused field.
            GLib.timeout_add(300, lambda: self.window.set_focus(None) and False)
        if pending:
            GLib.idle_add(self.window.handle_uri, pending)

    def _close(self, window):
        window.settings_page.flush()
        if window.session or window.busy:
            # Closing during a game only hides the launcher. Closing during an
            # update, build or start hides it until that is done: quitting
            # would stop the work half way (a half unpacked client).
            window.set_visible(False)
            window.quit_when_idle = window.busy is not None
            return True
        self.release()
        self.quit()
        return False


def main():
    return LauncherApp().run(None)
