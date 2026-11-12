import fcntl
import os
from pathlib import Path
import pty
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from screen_ui.app import ScreenUIApp
from screen_ui.backend import is_screen_installed, list_screen_sessions
from screen_ui.ports import SerialPortInfo
from screen_ui.screenshot import take_screenshot


def _pump_events(seconds: float = 0.15) -> None:
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.01)


def _walk_widgets(root: Gtk.Widget) -> list[Gtk.Widget]:
    out: list[Gtk.Widget] = [root]
    child = root.get_first_child()
    while child is not None:
        out.extend(_walk_widgets(child))
        child = child.get_next_sibling()
    return out


def _find_button(root: Gtk.Widget, *, label: str | None = None, icon: str | None = None, tooltip_sub: str | None = None) -> Gtk.Widget:
    for w in _walk_widgets(root):
        if isinstance(w, Gtk.Button):
            if label is not None and w.get_label() == label:
                return w
            if icon is not None and w.get_icon_name() == icon:
                return w
            if tooltip_sub is not None and tooltip_sub in (w.get_tooltip_text() or ""):
                return w
    raise AssertionError(f"Button not found (label={label!r}, icon={icon!r}, tooltip_sub={tooltip_sub!r})")


class TestAllUIButtons(unittest.TestCase):
    @unittest.skipUnless(is_screen_installed(), "GNU screen not installed")
    def test_every_button_and_control_end_to_end(self) -> None:
        dev_master, dev_slave = pty.openpty()
        dev_path = os.ttyname(dev_slave)

        app = ScreenUIApp(initial_baudrate=115200, auto_connect=False)
        results: dict[str, object] = {}

        def read_dev_nonblocking(wait: float = 0.25) -> bytes:
            import select

            buf = bytearray()
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if select.select([dev_master], [], [], 0.02)[0]:
                    buf.extend(os.read(dev_master, 4096))
                else:
                    _pump_events(0.01)
            return bytes(buf)

        def run_suite(a: ScreenUIApp) -> None:
            win = a._window
            assert win is not None
            try:
                _pump_events(0.25)

                # 1. Default baudrate 115200 & Refresh port button
                self.assertEqual(win.selected_baudrate, 115200)
                refresh_btn = _find_button(win, icon="view-refresh-symbolic")
                refresh_btn.emit("clicked")
                _pump_events(0.1)

                # Inject virtual PTY port via patched list_serial_ports for full live GNU screen testing
                virt_info = SerialPortInfo(device=dev_path, description="Virtual PTY UART", is_usb=True)
                legacy_info = SerialPortInfo(device="/dev/ttyS0", description="ttyS0", is_usb=False)
                with patch(
                    "screen_ui.window.list_serial_ports",
                    side_effect=lambda include_legacy_ttys=False: (
                        [virt_info, legacy_info] if include_legacy_ttys else [virt_info]
                    ),
                ):
                    refresh_btn.emit("clicked")
                    _pump_events(0.1)
                    self.assertEqual(win.selected_port, dev_path)

                    # 2. Auto Baud button (while disconnected)
                    win._auto_baud_btn.emit("clicked")
                    _pump_events(0.4)
                    # Wait for worker thread to finish probing
                    deadline = time.monotonic() + 8.0
                    while win._detecting_baud and time.monotonic() < deadline:
                        _pump_events(0.05)
                    self.assertFalse(win._detecting_baud)
                    self.assertEqual(win.selected_baudrate, 115200)

                    # 3. Connect button -> launches real GNU screen session on virtual PTY
                    win._connect_btn.emit("clicked")
                    _pump_events(0.45)
                    self.assertTrue(win._backend.is_connected)
                    self.assertEqual(win._connect_btn.get_label(), "Disconnect")
                    self.assertFalse(win._port_dropdown.get_sensitive())
                    self.assertFalse(win._baud_dropdown.get_sensitive())
                    self.assertFalse(win._auto_baud_btn.get_sensitive())
                    sess_name = win._backend.session_name
                    self.assertIsNotNone(sess_name)
                    assert sess_name is not None

                    # Feed embedded serial logs from virtual device -> GNU screen -> UI
                    os.write(
                        dev_master,
                        (
                            b"ets Jun  8 2016 00:22:57\r\n"
                            b"I (100) boot: ESP-IDF ready\r\n"
                            b"W (200) mqtt: Retry #1 latency 120ms\r\n"
                            b"E (300) sensor: I2C NACK error at 0x48\r\n"
                            b"WiFi connected IP 192.168.1.50 OK\r\n"
                        ),
                    )
                    _pump_events(0.35)
                    buf_text = win._buffer.get_text(win._buffer.get_start_iter(), win._buffer.get_end_iter(), False)
                    self.assertIn("I2C NACK error", buf_text)
                    self.assertGreaterEqual(win._error_count, 1)
                    self.assertGreaterEqual(win._warn_count, 1)

                    # 4. Pause button + floating Resume pill
                    win._pause_btn.set_active(True)
                    _pump_events(0.05)
                    self.assertTrue(win._paused)
                    self.assertEqual(win._pause_label.get_text(), "Resume")
                    os.write(dev_master, b"W (400) ota: Buffered while paused\r\n")
                    _pump_events(0.25)
                    self.assertGreater(len(win._pause_queue), 0)
                    self.assertTrue(win._paused_banner.get_visible())
                    paused_text = win._buffer.get_text(win._buffer.get_start_iter(), win._buffer.get_end_iter(), False)
                    self.assertNotIn("Buffered while paused", paused_text)

                    resume_pill = _find_button(win._paused_banner, label="Resume")
                    resume_pill.emit("clicked")
                    _pump_events(0.15)
                    self.assertFalse(win._paused)
                    self.assertEqual(len(win._pause_queue), 0)
                    resumed_text = win._buffer.get_text(win._buffer.get_start_iter(), win._buffer.get_end_iter(), False)
                    self.assertIn("Buffered while paused", resumed_text)

                    # 5. Severity filter pills: Errors, Warnings, Info/OK, All
                    win._filter_err_btn.set_active(True)
                    _pump_events(0.05)
                    err_only = win._buffer.get_text(win._buffer.get_start_iter(), win._buffer.get_end_iter(), False)
                    self.assertIn("I2C NACK error", err_only)
                    self.assertNotIn("Retry #1", err_only)

                    win._filter_warn_btn.set_active(True)
                    _pump_events(0.05)
                    warn_text = win._buffer.get_text(win._buffer.get_start_iter(), win._buffer.get_end_iter(), False)
                    self.assertIn("Retry #1", warn_text)
                    self.assertIn("I2C NACK error", warn_text)

                    win._filter_info_btn.set_active(True)
                    _pump_events(0.05)
                    info_text = win._buffer.get_text(win._buffer.get_start_iter(), win._buffer.get_end_iter(), False)
                    self.assertIn("ESP-IDF ready", info_text)
                    self.assertNotIn("I2C NACK error", info_text)

                    win._filter_all_btn.set_active(True)
                    _pump_events(0.05)

                    # 6. Timestamps, Colorize, Wrap, Auto-Scroll, A-/A+, Jump to Bottom
                    ts_btn = _find_button(win, label="Timestamps")
                    ts_btn.set_active(True)
                    _pump_events(0.05)
                    self.assertTrue(win._show_timestamps)
                    ts_btn.set_active(False)

                    color_btn = _find_button(win, label="Colorize")
                    color_btn.set_active(False)
                    _pump_events(0.05)
                    self.assertFalse(win._colorize)
                    color_btn.set_active(True)
                    _pump_events(0.05)
                    self.assertTrue(win._colorize)

                    wrap_btn = _find_button(win, label="Wrap")
                    wrap_btn.set_active(False)
                    self.assertEqual(win._text_view.get_wrap_mode(), Gtk.WrapMode.NONE)
                    wrap_btn.set_active(True)
                    self.assertEqual(win._text_view.get_wrap_mode(), Gtk.WrapMode.WORD_CHAR)

                    a_minus = _find_button(win, label="A-")
                    a_plus = _find_button(win, label="A+")
                    a_plus.emit("clicked")
                    self.assertEqual(win._font_size, 12)
                    a_minus.emit("clicked")
                    self.assertEqual(win._font_size, 11)

                    win._autoscroll_btn.set_active(False)
                    self.assertFalse(win._auto_scroll)
                    win._jump_bottom_btn.emit("clicked")
                    _pump_events(0.05)
                    self.assertTrue(win._auto_scroll)

                    # 7. Search toggle + search entry + Next/Prev
                    win._search_toggle.set_active(True)
                    _pump_events(0.05)
                    self.assertTrue(win._search_bar.get_search_mode())
                    win._search_entry.set_text("sensor")
                    _pump_events(0.25)
                    self.assertIn("1 match", win._search_count_lbl.get_text())
                    next_btn = _find_button(win._search_bar, icon="go-down-symbolic")
                    prev_btn = _find_button(win._search_bar, icon="go-up-symbolic")
                    next_btn.emit("clicked")
                    prev_btn.emit("clicked")
                    win._search_toggle.set_active(False)
                    _pump_events(0.05)

                    # 8. Copy buttons (Smart, Selection, All, Errors & Warnings)
                    copy_btn = _find_button(win, icon="edit-copy-symbolic")
                    copy_btn.emit("clicked")
                    _pump_events(0.05)

                    it_s = win._buffer.get_start_iter()
                    it_e = win._buffer.get_iter_at_offset(10)
                    win._buffer.select_range(it_s, it_e)
                    win._copy_smart()
                    win._copy_errors_warnings()
                    _pump_events(0.05)

                    # 9. Screenshot capture (Window & Terminal Pane -> temp dir)
                    with tempfile.TemporaryDirectory() as tmpdir:
                        p1 = take_screenshot(win, output_path=Path(tmpdir) / "win.png", copy_to_clipboard=True)
                        p2 = take_screenshot(win._scrolled, output_path=Path(tmpdir) / "term.png", copy_to_clipboard=True)
                        self.assertIsNotNone(p1)
                        self.assertIsNotNone(p2)
                        assert p1 is not None and p2 is not None
                        self.assertTrue(p1.exists() and p1.stat().st_size > 1000)
                        self.assertTrue(p2.exists() and p2.stat().st_size > 500)

                    # 10. Quick control chips (Ctrl+C, Ctrl+D, Ctrl+Z, Esc, Break, Reset)
                    read_dev_nonblocking(0.05)  # drain
                    for chip_lbl, expected in [
                        ("Ctrl+C", b"\x03"),
                        ("Ctrl+D", b"\x04"),
                        ("Ctrl+Z", b"\x1a"),
                        ("Esc", b"\x1b"),
                    ]:
                        chip = _find_button(win, label=chip_lbl)
                        chip.emit("clicked")
                        got = read_dev_nonblocking(0.45)
                        self.assertIn(expected, got, f"{chip_lbl} did not transmit {expected!r}; got {got!r}")

                    break_chip = _find_button(win, label="Break")
                    break_chip.emit("clicked")
                    reset_chip = _find_button(win, label="Reset")
                    reset_chip.emit("clicked")  # Safe ENOTTY on PTY, no crash

                    # 11. Command entry + EOL dropdown + HEX toggle + Send button + History
                    win._eol_dropdown.set_selected(1)  # \n
                    win._cmd_entry.set_text("help")
                    send_btn = _find_button(win, label="Send")
                    send_btn.emit("clicked")
                    got_cmd = read_dev_nonblocking(0.25)
                    self.assertIn(b"help\n", got_cmd)
                    self.assertEqual(win._cmd_history[-1], "help")

                    win._hex_toggle.set_active(True)
                    win._cmd_entry.set_text("AA 55 01")
                    send_btn.emit("clicked")
                    got_hex = read_dev_nonblocking(0.25)
                    self.assertIn(b"\xaa\x55\x01", got_hex)

                    # Invalid HEX should not crash or clear entry
                    win._cmd_entry.set_text("ZZ")
                    send_btn.emit("clicked")
                    self.assertEqual(win._cmd_entry.get_text(), "ZZ")
                    win._hex_toggle.set_active(False)
                    win._cmd_entry.set_text("")

                    # 12. Direct Keys toggle
                    direct_btn = _find_button(win, label="Direct Keys")
                    direct_btn.set_active(True)
                    self.assertTrue(win._direct_keys)
                    win._on_terminal_key_pressed(None, Gdk.KEY_x, 0, Gdk.ModifierType(0))  # type: ignore[arg-type]
                    got_key = read_dev_nonblocking(0.25)
                    self.assertIn(b"x", got_key)
                    direct_btn.set_active(False)

                    # 13. Screen popover controls: Populate, Toggle Log, Detach, Reattach, Clear, Kill, CheckButtons
                    win._populate_screen_popover(win._screen_popover)
                    pop_btns = [w for w in _walk_widgets(win._screen_pop_box) if isinstance(w, Gtk.Button)]
                    pop_labels = [b.get_label() for b in pop_btns]
                    self.assertIn("Send Break (Ctrl+A b)", pop_labels)
                    self.assertIn("Detach Session (Ctrl+A d)", pop_labels)
                    self.assertIn("Toggle Screen File Log (Ctrl+A H)", pop_labels)
                    self.assertIn("Clear Screen Session (Ctrl+A C)", pop_labels)
                    self.assertIn("Kill Session (Ctrl+A k)", pop_labels)

                    # Toggle GNU screen file log ON & OFF
                    with tempfile.TemporaryDirectory() as logdir:
                        logf = os.path.join(logdir, "screen_test.log")
                        ok_on, state_on = win._backend.toggle_screen_logfile(logf)
                        self.assertTrue(ok_on and state_on)
                        ok_off, state_off = win._backend.toggle_screen_logfile()
                        self.assertTrue(ok_off and not state_off)

                    # Detach session -> stays alive in `screen -ls`
                    win._on_detach_session()
                    _pump_events(0.25)
                    self.assertFalse(win._backend.is_connected)
                    self.assertEqual(win._status_pill.get_text(), "Detached")
                    detached = [s for s in list_screen_sessions() if s.name == sess_name]
                    self.assertEqual(len(detached), 1)
                    self.assertEqual(detached[0].status, "Detached")

                    # Reattach via Screen popover session button!
                    win._populate_screen_popover(win._screen_popover)
                    reattach_btn = _find_button(win._screen_pop_box, label=detached[0].display_label)
                    reattach_btn.emit("clicked")
                    _pump_events(0.3)
                    self.assertTrue(win._backend.is_connected)

                    # Clear button
                    clear_btn = _find_button(win, label="Clear")
                    clear_btn.emit("clicked")
                    _pump_events(0.05)
                    self.assertEqual(len(win._entries), 0)
                    self.assertEqual(win._error_count, 0)

                    # Kill session button
                    win._on_kill_session()
                    _pump_events(0.4)
                    self.assertFalse(win._backend.is_connected)
                    self.assertFalse(any(s.name == sess_name for s in list_screen_sessions()))
                    # Clear kernel TIOCEXCL left on persistent test PTY slave fd after screen exits
                    fcntl.ioctl(dev_slave, termios.TIOCNXCL)

                    # Show legacy ttyS* ports & Direct pyserial fallback toggles
                    win._populate_screen_popover(win._screen_popover)
                    legacy_chk = next(
                        w for w in _walk_widgets(win._screen_pop_box)
                        if isinstance(w, Gtk.CheckButton) and "ttyS" in (w.get_label() or "")
                    )
                    legacy_chk.set_active(True)
                    _pump_events(0.05)
                    self.assertEqual(len(win._ports), 2)
                    legacy_chk.set_active(False)
                    self.assertEqual(len(win._ports), 1)

                    mode_chk = next(
                        w for w in _walk_widgets(win._screen_pop_box)
                        if isinstance(w, Gtk.CheckButton) and "GNU" in (w.get_label() or "")
                    )
                    mode_chk.set_active(False)
                    self.assertFalse(win._use_screen_backend)
                    win._connect_btn.emit("clicked")
                    _pump_events(0.2)
                    cur_buf = win._buffer.get_text(win._buffer.get_start_iter(), win._buffer.get_end_iter(), False)
                    self.assertTrue(win._backend.is_connected, f"Direct serial failed; buf={cur_buf!r}")
                    self.assertEqual(win._backend.mode, "serial")
                    win._connect_btn.emit("clicked")  # Disconnect
                    _pump_events(0.2)
                    self.assertFalse(win._backend.is_connected)
                    mode_chk.set_active(True)

                results["ok"] = True
            except BaseException as exc:
                results["error"] = exc
            finally:
                if win._backend.is_connected:
                    win._backend.disconnect(kill_session=True)
                a.quit()

        app.connect_after("activate", lambda a: GLib.idle_add(lambda: (run_suite(a), False)[1]))
        try:
            app.run(None)
        finally:
            for fd in (dev_master, dev_slave):
                try:
                    os.close(fd)
                except OSError:
                    pass

        if "error" in results:
            raise results["error"]  # type: ignore[misc]
        self.assertTrue(results.get("ok"))


if __name__ == "__main__":
    unittest.main()
