"""Main GTK4 / Libadwaita application window for screen-UI."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import subprocess
import threading
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import APP_NAME, DEFAULT_BAUDRATE, __version__
from .backend import (
    ScreenBackend,
    ScreenSessionInfo,
    is_screen_installed,
    list_screen_sessions,
    pulse_dtr_rts_reset,
)
from .highlighter import (
    PALETTE,
    AnsiStreamProcessor,
    ParsedEvent,
    Severity,
    StyledSpan,
    enhance_line,
)
from .ports import (
    COMMON_BAUDRATES,
    SerialPortInfo,
    auto_detect_port,
    detect_baudrate,
    list_serial_ports,
)
from .screenshot import default_screenshot_dir, take_screenshot

MAX_LOG_ENTRIES = 50000

APP_CSS = """
window, .main-window {
    background-color: #0d1117;
}

.toolbar-surface {
    background-color: #161b22;
    border-bottom: 1px solid #30363d;
    padding: 6px 10px;
}

.footer-surface {
    background-color: #161b22;
    border-top: 1px solid #30363d;
    padding: 6px 10px;
}

.terminal-frame {
    background-color: #0d1117;
}

textview.terminal-view {
    background-color: #0d1117;
    color: #e6edf3;
    font-family: "JetBrains Mono", "Fira Code", "Ubuntu Sans Mono", "DejaVu Sans Mono", monospace;
    padding: 10px 14px;
    caret-color: #58a6ff;
}

textview.terminal-view text {
    background-color: #0d1117;
    color: #e6edf3;
}

.status-pill {
    border-radius: 999px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 600;
    background-color: #21262d;
    color: #8b949e;
    border: 1px solid #30363d;
}

.status-pill.connected {
    background-color: rgba(46, 160, 67, 0.18);
    color: #4ade80;
    border-color: rgba(74, 222, 128, 0.45);
}

.status-pill.paused {
    background-color: rgba(245, 158, 11, 0.18);
    color: #fbbf24;
    border-color: rgba(251, 191, 36, 0.45);
}

.status-pill.error {
    background-color: rgba(248, 81, 73, 0.18);
    color: #ff6b7a;
    border-color: rgba(255, 107, 122, 0.45);
}

.filter-pill {
    border-radius: 999px;
    padding: 2px 10px;
    min-height: 26px;
    font-size: 12px;
    font-weight: 600;
}

.filter-err:checked {
    background-color: rgba(248, 81, 73, 0.25);
    color: #ff6b7a;
    border: 1px solid rgba(255, 107, 122, 0.5);
}

.filter-warn:checked {
    background-color: rgba(245, 158, 11, 0.25);
    color: #ffb84d;
    border: 1px solid rgba(255, 184, 77, 0.5);
}

.filter-ok:checked {
    background-color: rgba(46, 160, 67, 0.25);
    color: #4ade80;
    border: 1px solid rgba(74, 222, 128, 0.5);
}

.overlay-banner {
    background-color: rgba(22, 27, 34, 0.94);
    border: 1px solid #30363d;
    border-radius: 999px;
    padding: 5px 12px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
}

.overlay-banner.paused-banner {
    border-color: rgba(251, 191, 36, 0.6);
}

.quick-chip {
    min-height: 26px;
    padding: 2px 8px;
    font-size: 11px;
    font-family: monospace;
    border-radius: 6px;
}

.stat-label {
    font-size: 11px;
    color: #8b949e;
    font-family: monospace;
}

.pause-btn-active {
    background-color: rgba(245, 158, 11, 0.25);
    color: #fbbf24;
    border: 1px solid rgba(251, 191, 36, 0.55);
}
"""


@dataclass
class LogEntry:
    """Stored log line for non-destructive filtering and export."""

    text: str
    spans: list[StyledSpan]
    severity: Severity
    timestamp_prefix: str = ""
    is_tx: bool = False


class ScreenUIWindow(Adw.ApplicationWindow):
    """Main window combining GNU screen session management with smart log UI."""

    def __init__(
        self,
        app: Adw.Application,
        initial_port: Optional[str] = None,
        initial_baudrate: int = DEFAULT_BAUDRATE,
        auto_connect: bool = False,
    ) -> None:
        super().__init__(application=app)
        self.set_title(APP_NAME)
        self.set_default_size(1220, 780)
        self.add_css_class("main-window")

        self._initial_port = initial_port
        self._initial_baudrate = initial_baudrate
        self._auto_connect = auto_connect

        # State
        self._ports: list[SerialPortInfo] = []
        self._custom_baudrates: list[int] = list(COMMON_BAUDRATES)
        self._include_legacy_ttys = False
        self._use_screen_backend = is_screen_installed()
        self._paused = False
        self._auto_scroll = True
        self._show_timestamps = False
        self._colorize = True
        self._direct_keys = False
        self._wrap_lines = True
        self._font_size = 11
        self._active_filter: str = "all"  # all | error | warn | info
        self._search_term = ""

        self._rx_bytes = 0
        self._tx_bytes = 0
        self._error_count = 0
        self._warn_count = 0
        self._info_count = 0

        self._entries: deque[LogEntry] = deque(maxlen=MAX_LOG_ENTRIES)
        self._pause_queue: list[ParsedEvent] = []
        self._cmd_history: list[str] = []
        self._cmd_history_idx = -1
        self._last_incomplete_range: Optional[tuple[int, int]] = None
        self._has_incomplete_line = False
        self._updating_scroll = False
        self._detecting_baud = False

        self._stream = AnsiStreamProcessor(colorize=True)
        self._backend = ScreenBackend(
            on_data=self._on_backend_data_thread,
            on_exit=self._on_backend_exit_thread,
        )
        self._tag_cache: dict[tuple[Optional[str], Optional[str], bool, bool, bool, bool], Gtk.TextTag] = {}
        self._search_tag: Optional[Gtk.TextTag] = None
        self._font_css_provider = Gtk.CssProvider()

        self._apply_css()
        self._build_ui()
        self._setup_shortcuts()
        self._refresh_ports(select_port=self._initial_port, initial=True)

        # Select initial baudrate (default 115200)
        self._select_baudrate(self._initial_baudrate)

        # Poll for USB serial hotplug every 1.5s while disconnected
        GLib.timeout_add(1500, self._poll_ports_timer)

        self.connect("close-request", self._on_close_request)

        if self._auto_connect:
            GLib.idle_add(self._auto_connect_on_start)

    def _apply_css(self) -> None:
        style_mgr = Adw.StyleManager.get_default()
        style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        display = Gdk.Display.get_default()
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):
            provider.load_from_string(APP_CSS)
            self._font_css_provider.load_from_string(f"textview.terminal-view {{ font-size: {self._font_size}pt; }}")
        else:
            provider.load_from_data(APP_CSS.encode("utf-8"))
            self._font_css_provider.load_from_data(
                f"textview.terminal-view {{ font-size: {self._font_size}pt; }}".encode("utf-8")
            )
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        Gtk.StyleContext.add_provider_for_display(
            display,
            self._font_css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
        )

    # ------------------------------------------------------------------ UI Build

    def _build_ui(self) -> None:
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._toast_overlay.set_child(root_box)

        # ---- Header Bar ----
        header = Adw.HeaderBar()
        root_box.append(header)

        # Left title & live status badge
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        app_lbl = Gtk.Label(label="screen-UI")
        app_lbl.add_css_class("heading")
        title_box.append(app_lbl)

        self._status_pill = Gtk.Label(label="Disconnected")
        self._status_pill.add_css_class("status-pill")
        title_box.append(self._status_pill)
        header.set_title_widget(title_box)

        # Left controls: Port + Refresh + Baud + Auto-Baud + Connect
        conn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.pack_start(conn_box)

        self._port_dropdown = Gtk.DropDown.new_from_strings(["No ports found"])
        self._port_dropdown.set_tooltip_text("Select serial port (auto-detects USB/ACM)")
        self._port_dropdown.set_size_request(230, -1)
        conn_box.append(self._port_dropdown)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh & auto-detect serial ports")
        refresh_btn.connect("clicked", lambda *_: self._refresh_ports(auto_pick=True))
        conn_box.append(refresh_btn)

        self._baud_dropdown = Gtk.DropDown.new_from_strings([str(b) for b in self._custom_baudrates])
        self._baud_dropdown.set_tooltip_text("Baudrate (default 115200)")
        conn_box.append(self._baud_dropdown)

        self._auto_baud_btn = Gtk.Button(label="Auto")
        self._auto_baud_btn.set_tooltip_text("Auto-detect active baudrate (falls back to 115200)")
        self._auto_baud_btn.connect("clicked", self._on_auto_baud_clicked)
        conn_box.append(self._auto_baud_btn)

        self._connect_btn = Gtk.Button(label="Connect")
        self._connect_btn.add_css_class("suggested-action")
        self._connect_btn.connect("clicked", self._on_connect_clicked)
        conn_box.append(self._connect_btn)

        # Right controls: Pause, Screenshot, Copy, Screen Control Popover
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.pack_end(actions_box)

        self._pause_btn = Gtk.ToggleButton()
        pause_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._pause_icon = Gtk.Image.new_from_icon_name("media-playback-pause-symbolic")
        self._pause_label = Gtk.Label(label="Pause")
        pause_content.append(self._pause_icon)
        pause_content.append(self._pause_label)
        self._pause_btn.set_child(pause_content)
        self._pause_btn.set_tooltip_text("Pause live output while buffering incoming data (Ctrl+P)")
        self._pause_btn.connect("toggled", self._on_pause_toggled)
        actions_box.append(self._pause_btn)

        # Screenshot split action
        shot_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        shot_box.add_css_class("linked")
        shot_btn = Gtk.Button(icon_name="camera-photo-symbolic")
        shot_btn.set_tooltip_text("Take Screenshot PNG & copy to clipboard (Ctrl+Shift+S)")
        shot_btn.connect("clicked", lambda *_: self._take_screenshot(terminal_only=False))
        shot_box.append(shot_btn)

        shot_menu_btn = Gtk.MenuButton(icon_name="pan-down-symbolic")
        shot_menu_btn.set_tooltip_text("Screenshot options")
        shot_menu_btn.set_popover(self._build_screenshot_popover())
        shot_box.append(shot_menu_btn)
        actions_box.append(shot_box)

        # Copy split action
        copy_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        copy_box.add_css_class("linked")
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        copy_btn.set_tooltip_text("Copy selected text (or all output if none selected)")
        copy_btn.connect("clicked", lambda *_: self._copy_smart())
        copy_box.append(copy_btn)

        copy_menu_btn = Gtk.MenuButton(icon_name="pan-down-symbolic")
        copy_menu_btn.set_tooltip_text("Copy & Export options")
        copy_menu_btn.set_popover(self._build_copy_popover())
        copy_box.append(copy_menu_btn)
        actions_box.append(copy_box)

        # GNU Screen Controls Popover
        self._screen_menu_btn = Gtk.MenuButton(label="Screen")
        self._screen_menu_btn.set_tooltip_text("GNU screen session controls & hardware tools")
        self._screen_popover = self._build_screen_popover()
        self._screen_menu_btn.set_popover(self._screen_popover)
        actions_box.append(self._screen_menu_btn)

        # ---- Secondary Embedded Intelligence & View Toolbar ----
        sub_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sub_bar.add_css_class("toolbar-surface")
        root_box.append(sub_bar)

        # Severity Filter Pills
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        sub_bar.append(filter_box)

        self._filter_all_btn = Gtk.ToggleButton(label="All")
        self._filter_all_btn.set_active(True)
        self._filter_all_btn.add_css_class("filter-pill")
        self._filter_all_btn.connect("toggled", self._on_filter_btn_toggled, "all")
        filter_box.append(self._filter_all_btn)

        self._filter_err_btn = Gtk.ToggleButton(label="Errors (0)")
        self._filter_err_btn.add_css_class("filter-pill")
        self._filter_err_btn.add_css_class("filter-err")
        self._filter_err_btn.connect("toggled", self._on_filter_btn_toggled, "error")
        filter_box.append(self._filter_err_btn)

        self._filter_warn_btn = Gtk.ToggleButton(label="Warnings (0)")
        self._filter_warn_btn.add_css_class("filter-pill")
        self._filter_warn_btn.add_css_class("filter-warn")
        self._filter_warn_btn.connect("toggled", self._on_filter_btn_toggled, "warn")
        filter_box.append(self._filter_warn_btn)

        self._filter_info_btn = Gtk.ToggleButton(label="Info/OK (0)")
        self._filter_info_btn.add_css_class("filter-pill")
        self._filter_info_btn.add_css_class("filter-ok")
        self._filter_info_btn.connect("toggled", self._on_filter_btn_toggled, "info")
        filter_box.append(self._filter_info_btn)

        sep1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sub_bar.append(sep1)

        # Toggles
        self._autoscroll_btn = Gtk.ToggleButton(label="Auto-Scroll")
        self._autoscroll_btn.set_active(True)
        self._autoscroll_btn.add_css_class("filter-pill")
        self._autoscroll_btn.connect("toggled", self._on_autoscroll_toggled)
        sub_bar.append(self._autoscroll_btn)

        ts_btn = Gtk.ToggleButton(label="Timestamps")
        ts_btn.add_css_class("filter-pill")
        ts_btn.connect("toggled", self._on_timestamps_toggled)
        sub_bar.append(ts_btn)

        color_btn = Gtk.ToggleButton(label="Colorize")
        color_btn.set_active(True)
        color_btn.add_css_class("filter-pill")
        color_btn.set_tooltip_text("Highlight embedded errors, warnings, hex, IPs, and tags")
        color_btn.connect("toggled", self._on_colorize_toggled)
        sub_bar.append(color_btn)

        direct_btn = Gtk.ToggleButton(label="Direct Keys")
        direct_btn.add_css_class("filter-pill")
        direct_btn.set_tooltip_text("Send keystrokes typed in terminal view straight to screen")
        direct_btn.connect("toggled", self._on_direct_keys_toggled)
        sub_bar.append(direct_btn)

        wrap_btn = Gtk.ToggleButton(label="Wrap")
        wrap_btn.set_active(True)
        wrap_btn.add_css_class("filter-pill")
        wrap_btn.connect("toggled", self._on_wrap_toggled)
        sub_bar.append(wrap_btn)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        sub_bar.append(spacer)

        # Font zoom & Search & Clear
        zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        zoom_box.add_css_class("linked")
        z_out = Gtk.Button(label="A-")
        z_out.set_tooltip_text("Decrease terminal font size")
        z_out.connect("clicked", lambda *_: self._change_font_size(-1))
        z_in = Gtk.Button(label="A+")
        z_in.set_tooltip_text("Increase terminal font size")
        z_in.connect("clicked", lambda *_: self._change_font_size(1))
        zoom_box.append(z_out)
        zoom_box.append(z_in)
        sub_bar.append(zoom_box)

        self._search_toggle = Gtk.ToggleButton(icon_name="system-search-symbolic")
        self._search_toggle.set_tooltip_text("Search output (Ctrl+F)")
        self._search_toggle.connect("toggled", self._on_search_toggled)
        sub_bar.append(self._search_toggle)

        clear_btn = Gtk.Button(icon_name="edit-clear-all-symbolic", label="Clear")
        clear_btn.set_tooltip_text("Clear terminal output")
        clear_btn.connect("clicked", lambda *_: self._clear_output())
        sub_bar.append(clear_btn)

        # ---- Search Bar ----
        self._search_bar = Gtk.SearchBar()
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search serial logs (error, 0x..., IP, tag)...")
        self._search_entry.set_size_request(320, -1)
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("activate", lambda *_: self._find_next(forward=True))
        search_box.append(self._search_entry)

        prev_btn = Gtk.Button(icon_name="go-up-symbolic")
        prev_btn.set_tooltip_text("Previous match")
        prev_btn.connect("clicked", lambda *_: self._find_next(forward=False))
        next_btn = Gtk.Button(icon_name="go-down-symbolic")
        next_btn.set_tooltip_text("Next match")
        next_btn.connect("clicked", lambda *_: self._find_next(forward=True))
        self._search_count_lbl = Gtk.Label(label="")
        self._search_count_lbl.add_css_class("stat-label")
        search_box.append(prev_btn)
        search_box.append(next_btn)
        search_box.append(self._search_count_lbl)

        self._search_bar.set_child(search_box)
        self._search_bar.connect_entry(self._search_entry)
        self._search_bar.set_show_close_button(True)
        self._search_bar.connect("notify::search-mode-enabled", self._on_search_bar_mode)
        root_box.append(self._search_bar)

        # ---- Main Terminal Output Area with Floating Overlay Pills ----
        self._terminal_overlay = Gtk.Overlay()
        self._terminal_overlay.set_vexpand(True)
        self._terminal_overlay.set_hexpand(True)
        self._terminal_overlay.add_css_class("terminal-frame")
        root_box.append(self._terminal_overlay)

        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_vexpand(True)
        self._scrolled.set_hexpand(True)
        self._scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._terminal_overlay.set_child(self._scrolled)

        self._text_view = Gtk.TextView()
        self._text_view.set_editable(False)
        self._text_view.set_cursor_visible(False)
        self._text_view.set_monospace(True)
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._text_view.add_css_class("terminal-view")
        self._apply_font_size()
        self._scrolled.set_child(self._text_view)
        self._buffer = self._text_view.get_buffer()

        # Scroll listener to auto-unlock scroll if user scrolls up
        vadj = self._scrolled.get_vadjustment()
        vadj.connect("value-changed", self._on_scroll_changed)

        # Key forwarding controller for Direct Keys mode
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_terminal_key_pressed)
        self._text_view.add_controller(key_ctrl)

        # Right-click context menu on terminal
        right_click = Gtk.GestureClick(button=3)
        right_click.connect("pressed", self._on_terminal_right_click)
        self._text_view.add_controller(right_click)

        # Floating Paused Banner
        self._paused_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._paused_banner.add_css_class("overlay-banner")
        self._paused_banner.add_css_class("paused-banner")
        self._paused_banner.set_halign(Gtk.Align.CENTER)
        self._paused_banner.set_valign(Gtk.Align.START)
        self._paused_banner.set_margin_top(12)
        self._paused_banner.set_visible(False)

        self._paused_banner_lbl = Gtk.Label(label="Paused • 0 buffered lines")
        resume_pill = Gtk.Button(label="Resume")
        resume_pill.add_css_class("suggested-action")
        resume_pill.add_css_class("pill")
        resume_pill.connect("clicked", lambda *_: self._pause_btn.set_active(False))
        self._paused_banner.append(self._paused_banner_lbl)
        self._paused_banner.append(resume_pill)
        self._terminal_overlay.add_overlay(self._paused_banner)

        # Floating Jump-to-Bottom Pill
        self._jump_bottom_btn = Gtk.Button(label="↓ Jump to Bottom")
        self._jump_bottom_btn.add_css_class("overlay-banner")
        self._jump_bottom_btn.set_halign(Gtk.Align.END)
        self._jump_bottom_btn.set_valign(Gtk.Align.END)
        self._jump_bottom_btn.set_margin_end(18)
        self._jump_bottom_btn.set_margin_bottom(14)
        self._jump_bottom_btn.set_visible(False)
        self._jump_bottom_btn.connect("clicked", lambda *_: self._jump_to_bottom())
        self._terminal_overlay.add_overlay(self._jump_bottom_btn)

        # ---- Bottom Command & Quick Control Bar ----
        bottom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bottom_box.add_css_class("footer-surface")
        root_box.append(bottom_box)

        input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bottom_box.append(input_row)

        # Quick embedded control chips
        for label, payload, tip in [
            ("Ctrl+C", b"\x03", "Send SIGINT (0x03)"),
            ("Ctrl+D", b"\x04", "Send EOF / Soft Reset (0x04)"),
            ("Ctrl+Z", b"\x1a", "Send SIGTSTP (0x1A)"),
            ("Esc", b"\x1b", "Send Escape (0x1B)"),
        ]:
            chip = Gtk.Button(label=label)
            chip.add_css_class("quick-chip")
            chip.set_tooltip_text(tip)
            chip.connect("clicked", lambda _b, p=payload: self._send_raw_bytes(p))
            input_row.append(chip)

        break_chip = Gtk.Button(label="Break")
        break_chip.add_css_class("quick-chip")
        break_chip.set_tooltip_text("Send serial BREAK via GNU screen (Ctrl+A b)")
        break_chip.connect("clicked", lambda *_: self._on_send_break())
        input_row.append(break_chip)

        reset_chip = Gtk.Button(label="Reset")
        reset_chip.add_css_class("quick-chip")
        reset_chip.set_tooltip_text("Pulse RTS/DTR to hardware-reset ESP32/Arduino/STM32")
        reset_chip.connect("clicked", lambda *_: self._on_hardware_reset())
        input_row.append(reset_chip)

        # Command Entry
        self._cmd_entry = Gtk.Entry()
        self._cmd_entry.set_hexpand(True)
        self._cmd_entry.set_placeholder_text("Send command to serial target (Up/Down for history)...")
        self._cmd_entry.connect("activate", lambda *_: self._on_send_command())
        cmd_key = Gtk.EventControllerKey()
        cmd_key.connect("key-pressed", self._on_cmd_entry_key)
        self._cmd_entry.add_controller(cmd_key)
        input_row.append(self._cmd_entry)

        self._hex_toggle = Gtk.ToggleButton(label="HEX")
        self._hex_toggle.add_css_class("quick-chip")
        self._hex_toggle.set_tooltip_text("Interpret input as space-separated hex bytes (e.g. AA 55 01 0D)")
        input_row.append(self._hex_toggle)

        self._eol_dropdown = Gtk.DropDown.new_from_strings(["\\r\\n", "\\n", "\\r", "None"])
        self._eol_dropdown.set_selected(0)
        self._eol_dropdown.set_tooltip_text("Line ending appended on Send")
        input_row.append(self._eol_dropdown)

        send_btn = Gtk.Button(label="Send")
        send_btn.add_css_class("suggested-action")
        send_btn.connect("clicked", lambda *_: self._on_send_command())
        input_row.append(send_btn)

        # Footer Status & Stats
        stat_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bottom_box.append(stat_row)

        self._backend_stat_lbl = Gtk.Label(label="Backend: GNU screen ready")
        self._backend_stat_lbl.add_css_class("stat-label")
        stat_row.append(self._backend_stat_lbl)

        stat_spacer = Gtk.Box()
        stat_spacer.set_hexpand(True)
        stat_row.append(stat_spacer)

        self._counters_lbl = Gtk.Label(label="Lines: 0  |  RX: 0 B  |  TX: 0 B")
        self._counters_lbl.add_css_class("stat-label")
        stat_row.append(self._counters_lbl)

        # Welcome banner in terminal
        self._append_system_line(
            f"screen-UI v{__version__} ready — select a port (auto-detected) and baudrate (default {DEFAULT_BAUDRATE}), then click Connect.\n",
            Severity.BOOT,
        )

    def _build_screenshot_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        for label, cb in [
            ("Capture Full Window (PNG + Clipboard)", lambda: self._take_screenshot(False, pop)),
            ("Capture Terminal View Only (PNG + Clipboard)", lambda: self._take_screenshot(True, pop)),
            ("Open Screenshots Folder", lambda: self._open_screenshots_dir(pop)),
        ]:
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.set_halign(Gtk.Align.FILL)
            btn.connect("clicked", lambda _b, f=cb: f())
            box.append(btn)

        pop.set_child(box)
        return pop

    def _build_copy_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        for label, cb in [
            ("Copy Selection", lambda: self._copy_selection(pop)),
            ("Copy All Visible Output", lambda: self._copy_all(pop)),
            ("Copy Errors & Warnings Only", lambda: self._copy_errors_warnings(pop)),
            ("Save Log to File...", lambda: self._save_log_dialog(pop)),
        ]:
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b, f=cb: f())
            box.append(btn)

        pop.set_child(box)
        return pop

    def _build_screen_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        self._screen_pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._screen_pop_box.set_margin_top(8)
        self._screen_pop_box.set_margin_bottom(8)
        self._screen_pop_box.set_margin_start(8)
        self._screen_pop_box.set_margin_end(8)
        self._screen_pop_box.set_size_request(280, -1)
        pop.set_child(self._screen_pop_box)
        pop.connect("show", self._populate_screen_popover)
        return pop

    def _populate_screen_popover(self, _pop: Gtk.Popover) -> None:
        box = self._screen_pop_box
        while child := box.get_first_child():
            box.remove(child)

        hdr = Gtk.Label(label="GNU screen Controls")
        hdr.add_css_class("heading")
        hdr.set_halign(Gtk.Align.START)
        box.append(hdr)

        # Screen action buttons
        actions = [
            ("Send Break (Ctrl+A b)", self._on_send_break, self._backend.is_connected),
            (
                "Detach Session (Ctrl+A d)",
                self._on_detach_session,
                self._backend.is_connected and self._backend.mode == "screen",
            ),
            (
                "Toggle Screen File Log (Ctrl+A H)",
                self._on_toggle_screen_log,
                self._backend.is_connected and self._backend.mode == "screen",
            ),
            (
                "Clear Screen Session (Ctrl+A C)",
                self._on_clear_screen_session,
                self._backend.is_connected and self._backend.mode == "screen",
            ),
            ("Kill Session (Ctrl+A k)", self._on_kill_session, self._backend.is_connected),
            ("Hardware Reset (RTS/DTR)", self._on_hardware_reset, True),
        ]

        for label, cb, enabled in actions:
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.set_sensitive(enabled)
            btn.connect("clicked", lambda _b, f=cb: (self._screen_popover.popdown(), f()))
            box.append(btn)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Backend mode switch
        mode_check = Gtk.CheckButton(label="Use GNU `screen` backend")
        mode_check.set_active(self._use_screen_backend)
        mode_check.set_sensitive(is_screen_installed())
        mode_check.connect("toggled", self._on_backend_mode_toggled)
        box.append(mode_check)

        legacy_check = Gtk.CheckButton(label="Show legacy /dev/ttyS* ports")
        legacy_check.set_active(self._include_legacy_ttys)
        legacy_check.connect("toggled", self._on_legacy_ports_toggled)
        box.append(legacy_check)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        sess_hdr = Gtk.Label(label="Attach Running `screen` Sessions")
        sess_hdr.add_css_class("heading")
        sess_hdr.set_halign(Gtk.Align.START)
        box.append(sess_hdr)

        sessions: list[ScreenSessionInfo] = list_screen_sessions()
        if not sessions:
            empty_lbl = Gtk.Label(label="No active screen sessions found")
            empty_lbl.add_css_class("dim-label")
            empty_lbl.set_halign(Gtk.Align.START)
            box.append(empty_lbl)
        else:
            for sess in sessions:
                sbtn = Gtk.Button(label=sess.display_label)
                sbtn.add_css_class("flat")
                sbtn.connect(
                    "clicked",
                    lambda _b, sid=sess.full_id: (
                        self._screen_popover.popdown(),
                        self._attach_existing_session(sid),
                    ),
                )
                box.append(sbtn)

    def _setup_shortcuts(self) -> None:
        ctrl = Gtk.EventControllerKey()
        ctrl.connect("key-pressed", self._on_window_key_pressed)
        self.add_controller(ctrl)

    def _on_window_key_pressed(
        self, _ctrl: Gtk.EventControllerKey, keyval: int, _keycode: int, state: Gdk.ModifierType
    ) -> bool:
        mod_ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        mod_shift = bool(state & Gdk.ModifierType.SHIFT_MASK)

        if mod_ctrl and keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self._search_toggle.set_active(not self._search_toggle.get_active())
            return True
        if mod_ctrl and keyval in (Gdk.KEY_p, Gdk.KEY_P):
            self._pause_btn.set_active(not self._pause_btn.get_active())
            return True
        if mod_ctrl and mod_shift and keyval in (Gdk.KEY_S, Gdk.KEY_s):
            self._take_screenshot(terminal_only=False)
            return True
        if mod_ctrl and mod_shift and keyval in (Gdk.KEY_C, Gdk.KEY_c):
            self._copy_smart()
            return True
        if mod_ctrl and keyval in (Gdk.KEY_l, Gdk.KEY_L):
            self._clear_output()
            return True
        return False

    # ------------------------------------------------------------------ Ports & Baudrate

    def _refresh_ports(
        self,
        select_port: Optional[str] = None,
        auto_pick: bool = False,
        initial: bool = False,
    ) -> None:
        prev_selected = self.selected_port
        ports = list_serial_ports(include_legacy_ttys=self._include_legacy_ttys)
        self._ports = ports

        if not ports:
            self._port_dropdown.set_model(Gtk.StringList.new(["No serial ports detected"]))
            self._port_dropdown.set_selected(0)
            if not initial:
                self._toast("No USB serial devices found")
            return

        labels = [p.display_label for p in ports]
        self._port_dropdown.set_model(Gtk.StringList.new(labels))

        target_dev = select_port
        if target_dev is None and not auto_pick and prev_selected:
            target_dev = prev_selected
        if target_dev is None or auto_pick:
            best = auto_detect_port(ports)
            if best:
                target_dev = best.device

        chosen_idx = 0
        if target_dev:
            for idx, p in enumerate(ports):
                if p.device == target_dev:
                    chosen_idx = idx
                    break
        self._port_dropdown.set_selected(chosen_idx)

        if auto_pick and ports:
            self._toast(f"Detected port: {ports[chosen_idx].device}")

    def _poll_ports_timer(self) -> bool:
        if not self._backend.is_connected and not self._detecting_baud:
            new_ports = list_serial_ports(include_legacy_ttys=self._include_legacy_ttys)
            old_devs = [p.device for p in self._ports]
            new_devs = [p.device for p in new_ports]
            if new_devs != old_devs:
                # Auto-select newly plugged USB device
                added = [d for d in new_devs if d not in old_devs]
                self._refresh_ports(select_port=added[0] if added else None)
                if added:
                    self._toast(f"USB serial device detected: {added[0]}")
        return True

    @property
    def selected_port(self) -> Optional[str]:
        idx = self._port_dropdown.get_selected()
        if 0 <= idx < len(self._ports):
            return self._ports[idx].device
        return None

    @property
    def selected_baudrate(self) -> int:
        idx = self._baud_dropdown.get_selected()
        if 0 <= idx < len(self._custom_baudrates):
            return self._custom_baudrates[idx]
        return DEFAULT_BAUDRATE

    def _select_baudrate(self, baud: int) -> None:
        if baud not in self._custom_baudrates:
            self._custom_baudrates.append(baud)
            self._custom_baudrates.sort()
            self._baud_dropdown.set_model(
                Gtk.StringList.new([str(b) for b in self._custom_baudrates])
            )
        self._baud_dropdown.set_selected(self._custom_baudrates.index(baud))

    def _on_auto_baud_clicked(self, _btn: Gtk.Button) -> None:
        if self._backend.is_connected:
            self._toast("Disconnect first to probe baudrate")
            return
        if self._detecting_baud:
            return
        port = self.selected_port
        if not port:
            self._toast("No serial port selected")
            return

        self._detecting_baud = True
        self._auto_baud_btn.set_sensitive(False)
        self._auto_baud_btn.set_label("Probing...")

        def worker() -> None:
            baud, live, msg = detect_baudrate(
                port=port,
                default_baud=DEFAULT_BAUDRATE,
                progress_cb=lambda b: GLib.idle_add(
                    self._auto_baud_btn.set_label, f"{b}..."
                ),
            )

            def finish() -> bool:
                self._detecting_baud = False
                self._auto_baud_btn.set_sensitive(True)
                self._auto_baud_btn.set_label("Auto")
                self._select_baudrate(baud)
                self._toast(msg)
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ Connection

    def _auto_connect_on_start(self) -> bool:
        if self.selected_port and not self._backend.is_connected:
            self._connect_now()
        return False

    def _on_connect_clicked(self, _btn: Gtk.Button) -> None:
        if self._backend.is_connected:
            self._backend.disconnect(kill_session=True)
            self._set_disconnected_ui("Disconnected")
        else:
            self._connect_now()

    def _connect_now(self) -> None:
        port = self.selected_port
        if not port:
            self._toast("No serial port available")
            return
        baud = self.selected_baudrate

        try:
            if self._use_screen_backend and is_screen_installed():
                self._backend.connect_screen(port=port, baudrate=baud)
                backend_desc = f"GNU screen ({self._backend.session_name})"
            else:
                self._backend.connect_direct_serial(port=port, baudrate=baud)
                backend_desc = "Direct pyserial"
        except Exception as exc:
            self._toast(f"Connection failed: {exc}")
            self._append_system_line(f"[ERROR] Failed to open {port} @ {baud}: {exc}\n", Severity.ERROR)
            return

        self._set_connected_ui(port, baud, backend_desc)
        self._append_system_line(
            f"Connected via {backend_desc} to {port} @ {baud} baud\n",
            Severity.SUCCESS,
        )

    def _attach_existing_session(self, session_id: str) -> None:
        try:
            self._backend.attach_screen_session(session_id)
        except Exception as exc:
            self._toast(f"Attach failed: {exc}")
            return
        self._set_connected_ui(session_id, None, f"GNU screen ({session_id})")
        self._append_system_line(f"Attached to existing GNU screen session {session_id}\n", Severity.SUCCESS)

    def _set_connected_ui(self, target: str, baud: Optional[int], backend_desc: str) -> None:
        self._connect_btn.set_label("Disconnect")
        self._connect_btn.remove_css_class("suggested-action")
        self._connect_btn.add_css_class("destructive-action")
        self._port_dropdown.set_sensitive(False)
        self._baud_dropdown.set_sensitive(False)
        self._auto_baud_btn.set_sensitive(False)

        label = f"Connected • {target}" + (f" @ {baud}" if baud else "")
        self._status_pill.set_text(label)
        self._status_pill.remove_css_class("error")
        self._status_pill.remove_css_class("paused")
        self._status_pill.add_css_class("connected")
        self._backend_stat_lbl.set_text(f"Backend: {backend_desc}")

    def _set_disconnected_ui(self, reason: str) -> None:
        self._connect_btn.set_label("Connect")
        self._connect_btn.remove_css_class("destructive-action")
        self._connect_btn.add_css_class("suggested-action")
        self._port_dropdown.set_sensitive(True)
        self._baud_dropdown.set_sensitive(True)
        self._auto_baud_btn.set_sensitive(True)

        self._status_pill.set_text(reason)
        self._status_pill.remove_css_class("connected")
        self._status_pill.remove_css_class("paused")
        self._backend_stat_lbl.set_text("Backend: Idle")

    # ------------------------------------------------------------------ Stream Rendering & Pause

    def _on_backend_data_thread(self, data: bytes) -> None:
        GLib.idle_add(self._handle_incoming_bytes, data)

    def _on_backend_exit_thread(self, reason: str) -> None:
        GLib.idle_add(self._handle_backend_exit, reason)

    def _handle_backend_exit(self, reason: str) -> bool:
        self._set_disconnected_ui("Disconnected")
        self._append_system_line(f"[{reason}]\n", Severity.WARN)
        self._toast(reason)
        return False

    def _handle_incoming_bytes(self, data: bytes) -> bool:
        self._rx_bytes += len(data)
        events = self._stream.feed(data)
        if not events:
            self._update_counters()
            return False

        if self._paused:
            self._pause_queue.extend(events)
            self._update_pause_banner()
            self._update_counters()
            return False

        self._apply_events(events)
        self._update_counters()
        return False

    def _apply_events(self, events: list[ParsedEvent]) -> None:
        for ev in events:
            if ev.kind == "clear":
                self._clear_output(keep_history=False)
                continue

            ts = datetime.now().strftime("[%H:%M:%S.%f")[:-3] + "] " if self._show_timestamps else ""

            if ev.kind == "replace_line" and self._has_incomplete_line and self._entries:
                # Update last stored entry
                self._entries[-1] = LogEntry(
                    text=ev.text,
                    spans=ev.spans,
                    severity=ev.severity,
                    timestamp_prefix=ts or self._entries[-1].timestamp_prefix,
                )
                self._replace_last_line_in_buffer(self._entries[-1])
                self._has_incomplete_line = not ev.text.endswith("\n")
                if ev.text.endswith("\n"):
                    self._tally_severity(ev.severity)
                continue

            entry = LogEntry(
                text=ev.text,
                spans=ev.spans,
                severity=ev.severity,
                timestamp_prefix=ts,
            )
            self._entries.append(entry)
            self._has_incomplete_line = not ev.text.endswith("\n")

            if ev.text.endswith("\n"):
                self._tally_severity(ev.severity)

            if self._entry_matches_filter(entry):
                self._insert_entry_to_buffer(entry)

        if self._auto_scroll and not self._paused:
            self._scroll_to_end()

    def _tally_severity(self, sev: Severity) -> None:
        if sev == Severity.ERROR:
            self._error_count += 1
        elif sev == Severity.WARN:
            self._warn_count += 1
        elif sev in (Severity.SUCCESS, Severity.INFO, Severity.BOOT):
            self._info_count += 1

    def _entry_matches_filter(self, entry: LogEntry) -> bool:
        if self._active_filter == "all":
            return True
        if self._active_filter == "error":
            return entry.severity == Severity.ERROR
        if self._active_filter == "warn":
            return entry.severity in (Severity.WARN, Severity.ERROR)
        if self._active_filter == "info":
            return entry.severity in (Severity.SUCCESS, Severity.INFO, Severity.BOOT)
        return True

    def _get_or_create_tag(self, span: StyledSpan) -> Gtk.TextTag:
        key = (span.fg, span.bg, span.bold, span.dim, span.italic, span.underline)
        tag = self._tag_cache.get(key)
        if tag is not None:
            return tag

        tag = self._buffer.create_tag(None)
        if span.fg:
            tag.set_property("foreground", span.fg)
        if span.bg:
            tag.set_property("background", span.bg)
        if span.bold:
            tag.set_property("weight", Pango.Weight.BOLD)
        if span.dim:
            tag.set_property("foreground", PALETTE["fg_muted"])
        if span.italic:
            tag.set_property("style", Pango.Style.ITALIC)
        if span.underline:
            tag.set_property("underline", Pango.Underline.SINGLE)
        self._tag_cache[key] = tag
        return tag

    def _insert_entry_to_buffer(self, entry: LogEntry) -> None:
        end_iter = self._buffer.get_end_iter()
        line_start_offset = end_iter.get_offset()

        prefix = entry.timestamp_prefix if self._show_timestamps else ""
        full_text = prefix + entry.text
        self._buffer.insert(end_iter, full_text)

        if prefix and self._colorize:
            p_start = self._buffer.get_iter_at_offset(line_start_offset)
            p_end = self._buffer.get_iter_at_offset(line_start_offset + len(prefix))
            ts_tag = self._get_or_create_tag(
                StyledSpan(0, len(prefix), fg=PALETTE["timestamp_fg"])
            )
            self._buffer.apply_tag(ts_tag, p_start, p_end)

        text_base = line_start_offset + len(prefix)
        if self._colorize:
            spans = entry.spans
            if not spans and not entry.is_tx:
                _, spans = enhance_line(entry.text.rstrip("\n"), colorize=True)
            for sp in spans:
                s_off = text_base + max(0, sp.start)
                e_off = text_base + min(len(entry.text), sp.end)
                if e_off > s_off:
                    it_s = self._buffer.get_iter_at_offset(s_off)
                    it_e = self._buffer.get_iter_at_offset(e_off)
                    self._buffer.apply_tag(self._get_or_create_tag(sp), it_s, it_e)

        self._last_incomplete_range = (line_start_offset, line_start_offset + len(full_text))

        # Keep buffer bounded smoothly
        if self._buffer.get_line_count() > MAX_LOG_ENTRIES:
            start = self._buffer.get_start_iter()
            cut = self._buffer.get_iter_at_line(1000)[1]
            self._buffer.delete(start, cut)

    def _replace_last_line_in_buffer(self, entry: LogEntry) -> None:
        if not self._entry_matches_filter(entry):
            return
        if self._last_incomplete_range is not None:
            s_off, _ = self._last_incomplete_range
            it_s = self._buffer.get_iter_at_offset(s_off)
            it_e = self._buffer.get_end_iter()
            self._buffer.delete(it_s, it_e)
        self._insert_entry_to_buffer(entry)

    def _append_system_line(self, text: str, severity: Severity = Severity.INFO) -> None:
        fg = {
            Severity.ERROR: PALETTE["error_fg"],
            Severity.WARN: PALETTE["warn_fg"],
            Severity.SUCCESS: PALETTE["success_fg"],
            Severity.BOOT: PALETTE["boot_fg"],
            Severity.INFO: PALETTE["info_fg"],
        }.get(severity, PALETTE["fg_muted"])
        entry = LogEntry(
            text=text,
            spans=[StyledSpan(0, len(text), fg=fg, bold=True)],
            severity=severity,
            timestamp_prefix=datetime.now().strftime("[%H:%M:%S] "),
        )
        self._entries.append(entry)
        self._tally_severity(severity)
        if self._entry_matches_filter(entry):
            self._insert_entry_to_buffer(entry)
            if self._auto_scroll and not self._paused:
                self._scroll_to_end()
        self._update_counters()

    def _rebuild_buffer_from_entries(self) -> None:
        self._buffer.set_text("")
        self._last_incomplete_range = None
        for entry in self._entries:
            if self._entry_matches_filter(entry):
                self._insert_entry_to_buffer(entry)
        if self._search_term:
            self._apply_search_highlights()
        if self._auto_scroll and not self._paused:
            self._scroll_to_end()

    # ------------------------------------------------------------------ Pause / Scroll

    def _on_pause_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._paused = btn.get_active()
        if self._paused:
            self._pause_btn.add_css_class("pause-btn-active")
            self._pause_icon.set_from_icon_name("media-playback-start-symbolic")
            self._pause_label.set_text("Resume")
            self._status_pill.add_css_class("paused")
            self._update_pause_banner()
        else:
            self._pause_btn.remove_css_class("pause-btn-active")
            self._pause_icon.set_from_icon_name("media-playback-pause-symbolic")
            self._pause_label.set_text("Pause")
            self._status_pill.remove_css_class("paused")
            self._paused_banner.set_visible(False)
            if self._pause_queue:
                queued = self._pause_queue
                self._pause_queue = []
                self._apply_events(queued)
                self._update_counters()

    def _update_pause_banner(self) -> None:
        if not self._paused:
            self._paused_banner.set_visible(False)
            return
        lines = sum(1 for e in self._pause_queue if e.text.endswith("\n"))
        self._paused_banner_lbl.set_text(f"⏸ Paused • {lines} buffered lines")
        self._paused_banner.set_visible(True)

    def _scroll_to_end(self) -> None:
        def do_scroll() -> bool:
            self._updating_scroll = True
            vadj = self._scrolled.get_vadjustment()
            vadj.set_value(max(0.0, vadj.get_upper() - vadj.get_page_size()))
            self._updating_scroll = False
            self._jump_bottom_btn.set_visible(False)
            return False

        GLib.idle_add(do_scroll)

    def _jump_to_bottom(self) -> None:
        self._auto_scroll = True
        self._autoscroll_btn.set_active(True)
        self._scroll_to_end()

    def _on_scroll_changed(self, vadj: Gtk.Adjustment) -> None:
        if self._updating_scroll:
            return
        at_bottom = (vadj.get_value() + vadj.get_page_size()) >= (vadj.get_upper() - 24.0)
        self._jump_bottom_btn.set_visible(not at_bottom)

    # ------------------------------------------------------------------ Toolbar Handlers

    def _on_filter_btn_toggled(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if not btn.get_active():
            # Prevent unselecting all; keep active filter
            if self._active_filter == mode:
                btn.set_active(True)
            return
        self._active_filter = mode
        for b, m in [
            (self._filter_all_btn, "all"),
            (self._filter_err_btn, "error"),
            (self._filter_warn_btn, "warn"),
            (self._filter_info_btn, "info"),
        ]:
            if m != mode and b.get_active():
                b.set_active(False)
        self._rebuild_buffer_from_entries()

    def _on_autoscroll_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._auto_scroll = btn.get_active()
        if self._auto_scroll:
            self._scroll_to_end()

    def _on_timestamps_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._show_timestamps = btn.get_active()
        self._rebuild_buffer_from_entries()

    def _on_colorize_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._colorize = btn.get_active()
        self._stream.colorize = self._colorize
        self._rebuild_buffer_from_entries()
        self._toast(f"Embedded color enhancement {'enabled' if self._colorize else 'disabled'}")

    def _on_direct_keys_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._direct_keys = btn.get_active()
        if self._direct_keys:
            self._text_view.grab_focus()
            self._toast("Direct Keys ON — typing in terminal forwards directly to screen")

    def _on_wrap_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._wrap_lines = btn.get_active()
        self._text_view.set_wrap_mode(
            Gtk.WrapMode.WORD_CHAR if self._wrap_lines else Gtk.WrapMode.NONE
        )

    def _change_font_size(self, delta: int) -> None:
        self._font_size = max(8, min(24, self._font_size + delta))
        self._apply_font_size()

    def _apply_font_size(self) -> None:
        css = f"textview.terminal-view {{ font-size: {self._font_size}pt; }}"
        if hasattr(self._font_css_provider, "load_from_string"):
            self._font_css_provider.load_from_string(css)
        else:
            self._font_css_provider.load_from_data(css.encode("utf-8"))

    def _clear_output(self, keep_history: bool = False) -> None:
        self._buffer.set_text("")
        self._last_incomplete_range = None
        self._has_incomplete_line = False
        if not keep_history:
            self._entries.clear()
            self._error_count = 0
            self._warn_count = 0
            self._info_count = 0
            self._update_counters()

    def _update_counters(self) -> None:
        self._filter_err_btn.set_label(f"Errors ({self._error_count})")
        self._filter_warn_btn.set_label(f"Warnings ({self._warn_count})")
        self._filter_info_btn.set_label(f"Info/OK ({self._info_count})")
        self._counters_lbl.set_text(
            f"Lines: {len(self._entries)}  |  RX: {self._rx_bytes:,} B  |  TX: {self._tx_bytes:,} B"
        )

    # ------------------------------------------------------------------ Search

    def _on_search_toggled(self, btn: Gtk.ToggleButton) -> None:
        active = btn.get_active()
        self._search_bar.set_search_mode(active)
        if active:
            self._search_entry.grab_focus()

    def _on_search_bar_mode(self, bar: Gtk.SearchBar, _pspec: object) -> None:
        if not bar.get_search_mode() and self._search_toggle.get_active():
            self._search_toggle.set_active(False)
            self._clear_search_highlights()

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_term = entry.get_text().strip()
        self._apply_search_highlights()

    def _clear_search_highlights(self) -> None:
        if self._search_tag is not None:
            self._buffer.remove_tag(
                self._search_tag,
                self._buffer.get_start_iter(),
                self._buffer.get_end_iter(),
            )
        self._search_count_lbl.set_text("")

    def _apply_search_highlights(self) -> None:
        self._clear_search_highlights()
        if not self._search_term:
            return
        if self._search_tag is None:
            self._search_tag = self._buffer.create_tag(
                "search_hit", background="#713f12", foreground="#fef08a"
            )
        count = 0
        it = self._buffer.get_start_iter()
        flags = Gtk.TextSearchFlags.CASE_INSENSITIVE
        while True:
            found = it.forward_search(self._search_term, flags, None)
            if not found:
                break
            m_start, m_end = found
            self._buffer.apply_tag(self._search_tag, m_start, m_end)
            count += 1
            it = m_end
        self._search_count_lbl.set_text(f"{count} match{'es' if count != 1 else ''}")

    def _find_next(self, forward: bool = True) -> None:
        if not self._search_term:
            return
        flags = Gtk.TextSearchFlags.CASE_INSENSITIVE
        insert_mark = self._buffer.get_insert()
        it = self._buffer.get_iter_at_mark(insert_mark)
        if forward:
            found = it.forward_search(self._search_term, flags, None)
            if not found:
                found = self._buffer.get_start_iter().forward_search(self._search_term, flags, None)
        else:
            found = it.backward_search(self._search_term, flags, None)
            if not found:
                found = self._buffer.get_end_iter().backward_search(self._search_term, flags, None)
        if found:
            m_start, m_end = found
            self._buffer.select_range(m_start, m_end if forward else m_start)
            self._text_view.scroll_to_iter(m_start, 0.15, False, 0.0, 0.5)

    # ------------------------------------------------------------------ Copy, Save, Screenshot

    def _copy_smart(self) -> None:
        if self._buffer.get_has_selection():
            self._copy_selection()
        else:
            self._copy_all()

    def _copy_selection(self, pop: Optional[Gtk.Popover] = None) -> None:
        if pop:
            pop.popdown()
        bounds = self._buffer.get_selection_bounds()
        if not bounds:
            self._toast("No text selected")
            return
        text = self._buffer.get_text(bounds[0], bounds[1], False)
        self.get_clipboard().set(text)
        self._toast(f"Copied selection ({len(text)} chars)")

    def _copy_all(self, pop: Optional[Gtk.Popover] = None) -> None:
        if pop:
            pop.popdown()
        text = self._buffer.get_text(
            self._buffer.get_start_iter(), self._buffer.get_end_iter(), False
        )
        if not text:
            self._toast("Terminal buffer is empty")
            return
        self.get_clipboard().set(text)
        self._toast(f"Copied all output ({text.count(chr(10))} lines)")

    def _copy_errors_warnings(self, pop: Optional[Gtk.Popover] = None) -> None:
        if pop:
            pop.popdown()
        lines = [
            (e.timestamp_prefix if self._show_timestamps else "") + e.text
            for e in self._entries
            if e.severity in (Severity.ERROR, Severity.WARN)
        ]
        if not lines:
            self._toast("No errors or warnings recorded yet")
            return
        self.get_clipboard().set("".join(lines))
        self._toast(f"Copied {len(lines)} error/warning lines")

    def _save_log_dialog(self, pop: Optional[Gtk.Popover] = None) -> None:
        if pop:
            pop.popdown()
        dialog = Gtk.FileDialog(title="Save Serial Output Log")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dialog.set_initial_name(f"screen-ui-{stamp}.log")
        dialog.save(self, None, self._on_save_log_done)

    def _on_save_log_done(self, dialog: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
        try:
            file = dialog.save_finish(res)
        except GLib.Error:
            return
        path = file.get_path()
        if not path:
            return
        text = self._buffer.get_text(
            self._buffer.get_start_iter(), self._buffer.get_end_iter(), False
        )
        Path(path).write_text(text, encoding="utf-8")
        self._toast(f"Saved log to {Path(path).name}")

    def _take_screenshot(
        self, terminal_only: bool = False, pop: Optional[Gtk.Popover] = None
    ) -> None:
        if pop:
            pop.popdown()
        target = self._scrolled if terminal_only else self
        prefix = "screen-ui-terminal" if terminal_only else "screen-ui"
        out_path = take_screenshot(target, prefix=prefix, copy_to_clipboard=True)
        if out_path is None:
            self._toast("Screenshot failed — window not realized yet")
            return

        toast = Adw.Toast.new(f"Screenshot saved & copied: {out_path.name}")
        toast.set_button_label("Open")
        toast.connect(
            "button-clicked",
            lambda *_: subprocess.Popen(["xdg-open", str(out_path)]),
        )
        self._toast_overlay.add_toast(toast)

    def _open_screenshots_dir(self, pop: Optional[Gtk.Popover] = None) -> None:
        if pop:
            pop.popdown()
        subprocess.Popen(["xdg-open", str(default_screenshot_dir())])

    # ------------------------------------------------------------------ Context Menu & Keys

    def _on_terminal_right_click(
        self, _gesture: Gtk.GestureClick, _n_press: int, x: float, y: float
    ) -> None:
        pop = Gtk.Popover()
        pop.set_parent(self._text_view)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        pop.set_pointing_to(rect)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        for label, cb in [
            ("Copy Selection", lambda: self._copy_selection(pop)),
            ("Copy All Output", lambda: self._copy_all(pop)),
            ("Copy Errors & Warnings", lambda: self._copy_errors_warnings(pop)),
            ("Screenshot Terminal", lambda: self._take_screenshot(True, pop)),
            ("Clear Output", lambda: (pop.popdown(), self._clear_output())),
        ]:
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.connect("clicked", lambda _btn, f=cb: f())
            box.append(b)

        pop.set_child(box)
        pop.popup()

    def _on_terminal_key_pressed(
        self, _ctrl: Gtk.EventControllerKey, keyval: int, _keycode: int, state: Gdk.ModifierType
    ) -> bool:
        mod_ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        # Allow Ctrl+C to copy when text is selected
        if mod_ctrl and keyval in (Gdk.KEY_c, Gdk.KEY_C) and self._buffer.get_has_selection():
            return False

        if not self._direct_keys or not self._backend.is_connected:
            return False

        special_map = {
            Gdk.KEY_Return: b"\r",
            Gdk.KEY_KP_Enter: b"\r",
            Gdk.KEY_BackSpace: b"\x08",
            Gdk.KEY_Tab: b"\t",
            Gdk.KEY_Escape: b"\x1b",
            Gdk.KEY_Up: b"\x1b[A",
            Gdk.KEY_Down: b"\x1b[B",
            Gdk.KEY_Right: b"\x1b[C",
            Gdk.KEY_Left: b"\x1b[D",
        }
        if keyval in special_map:
            self._send_raw_bytes(special_map[keyval])
            return True

        if mod_ctrl and Gdk.KEY_a <= keyval <= Gdk.KEY_z:
            self._send_raw_bytes(bytes([keyval - Gdk.KEY_a + 1]))
            return True
        if mod_ctrl and Gdk.KEY_A <= keyval <= Gdk.KEY_Z:
            self._send_raw_bytes(bytes([keyval - Gdk.KEY_A + 1]))
            return True

        uni = Gdk.keyval_to_unicode(keyval)
        if uni and uni >= 32:
            self._send_raw_bytes(chr(uni).encode("utf-8"))
            return True
        return False

    # ------------------------------------------------------------------ Command Sending & Screen Actions

    def _send_raw_bytes(self, payload: bytes) -> None:
        if not self._backend.is_connected:
            self._toast("Not connected")
            return
        if self._backend.send_bytes(payload):
            self._tx_bytes += len(payload)
            self._update_counters()
        else:
            self._toast("Failed to write to serial session")

    def _on_send_command(self) -> None:
        text = self._cmd_entry.get_text()
        if not self._backend.is_connected:
            self._toast("Connect to a serial port first")
            return

        if self._hex_toggle.get_active():
            cleaned = text.replace("0x", "").replace(",", " ").strip()
            try:
                payload = bytes.fromhex(cleaned)
            except ValueError:
                self._toast("Invalid HEX bytes (example: AA 55 01 0D)")
                return
        else:
            eol_map = {0: "\r\n", 1: "\n", 2: "\r", 3: ""}
            eol = eol_map.get(self._eol_dropdown.get_selected(), "\r\n")
            payload = (text + eol).encode("utf-8")

        if self._backend.send_bytes(payload):
            self._tx_bytes += len(payload)
            if text:
                if not self._cmd_history or self._cmd_history[-1] != text:
                    self._cmd_history.append(text)
            self._cmd_history_idx = -1
            self._cmd_entry.set_text("")
            self._update_counters()

    def _on_cmd_entry_key(
        self, _ctrl: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType
    ) -> bool:
        if not self._cmd_history:
            return False
        if keyval == Gdk.KEY_Up:
            if self._cmd_history_idx == -1:
                self._cmd_history_idx = len(self._cmd_history) - 1
            elif self._cmd_history_idx > 0:
                self._cmd_history_idx -= 1
            self._cmd_entry.set_text(self._cmd_history[self._cmd_history_idx])
            self._cmd_entry.set_position(-1)
            return True
        if keyval == Gdk.KEY_Down:
            if self._cmd_history_idx == -1:
                return False
            if self._cmd_history_idx < len(self._cmd_history) - 1:
                self._cmd_history_idx += 1
                self._cmd_entry.set_text(self._cmd_history[self._cmd_history_idx])
                self._cmd_entry.set_position(-1)
            else:
                self._cmd_history_idx = -1
                self._cmd_entry.set_text("")
            return True
        return False

    def _on_send_break(self) -> None:
        if self._backend.send_break():
            self._toast("Sent BREAK signal")
        else:
            self._toast("Not connected")

    def _on_detach_session(self) -> None:
        name = self._backend.session_name
        self._backend.disconnect(kill_session=False)
        self._set_disconnected_ui("Detached")
        self._append_system_line(
            f"Detached GNU screen session '{name}' (still running in background — reattach anytime)\n",
            Severity.INFO,
        )
        self._toast(f"Detached '{name}'")

    def _on_kill_session(self) -> None:
        self._backend.disconnect(kill_session=True)
        self._set_disconnected_ui("Disconnected")
        self._toast("Killed screen session")

    def _on_toggle_screen_log(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = str(default_screenshot_dir().parent / f"screen-ui-{stamp}.log")
        ok, active = self._backend.toggle_screen_logfile(log_path)
        if ok:
            self._toast(
                f"Screen file logging ON ({log_path})" if active else "Screen file logging OFF"
            )
        else:
            self._toast("Failed to toggle screen logging")

    def _on_clear_screen_session(self) -> None:
        self._backend.clear_screen_session()
        self._clear_output()

    def _on_hardware_reset(self) -> None:
        port = self.selected_port or self._backend.port
        if not port or not port.startswith("/dev/"):
            self._toast("Select a serial port to reset")
            return
        ok, msg = pulse_dtr_rts_reset(port)
        self._toast(msg)
        if ok:
            self._append_system_line(f"[{msg}]\n", Severity.BOOT)

    def _on_backend_mode_toggled(self, chk: Gtk.CheckButton) -> None:
        self._use_screen_backend = chk.get_active()

    def _on_legacy_ports_toggled(self, chk: Gtk.CheckButton) -> None:
        self._include_legacy_ttys = chk.get_active()
        self._refresh_ports()

    def _on_close_request(self, _win: Gtk.Window) -> bool:
        if self._backend.is_connected:
            self._backend.disconnect(kill_session=True)
        return False

    def _toast(self, message: str) -> None:
        self._toast_overlay.add_toast(Adw.Toast.new(message))
