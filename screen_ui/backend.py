"""GNU screen PTY session backend, session manager, and hardware serial helpers."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
from typing import Callable, Optional

import serial

_SCREEN_LS_RE = re.compile(
    r"^\s*(\d+)\.([^\s]+)\s+\(([^)]+)\)\s+\((Attached|Detached)\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScreenSessionInfo:
    """Represents a running GNU screen session from `screen -ls`."""

    pid: int
    name: str
    timestamp: str
    status: str

    @property
    def full_id(self) -> str:
        return f"{self.pid}.{self.name}"

    @property
    def display_label(self) -> str:
        return f"{self.name} (PID {self.pid} • {self.status})"


def is_screen_installed() -> bool:
    return shutil.which("screen") is not None


def list_screen_sessions() -> list[ScreenSessionInfo]:
    """Parse active GNU screen sessions from `screen -ls`."""
    if not is_screen_installed():
        return []
    try:
        res = subprocess.run(
            ["screen", "-ls"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    sessions: list[ScreenSessionInfo] = []
    combined = (res.stdout or "") + "\n" + (res.stderr or "")
    for line in combined.splitlines():
        m = _SCREEN_LS_RE.match(line)
        if m:
            sessions.append(
                ScreenSessionInfo(
                    pid=int(m.group(1)),
                    name=m.group(2),
                    timestamp=m.group(3).strip(),
                    status=m.group(4).capitalize(),
                )
            )
    return sessions


def sanitize_session_name(port: str) -> str:
    """Create a safe GNU screen session name from a device path."""
    base = os.path.basename(port) or "serial"
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", base)
    return f"screen-ui-{clean}"


def pulse_dtr_rts_reset(port: str, hold_ms: int = 100) -> tuple[bool, str]:
    """Pulse RTS/DTR on a serial port to reset ESP32/Arduino/STM32 targets."""
    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
        return False, f"Reset failed on {port}: {exc}"

    try:
        buf = bytearray(4)
        fcntl.ioctl(fd, termios.TIOCMGET, buf, True)
        status = struct.unpack("I", buf)[0]
        # Pull RTS high (EN low on ESP32 auto-reset) while keeping DTR low
        rts_on = (status | termios.TIOCM_RTS) & ~termios.TIOCM_DTR
        fcntl.ioctl(fd, termios.TIOCMSET, struct.pack("I", rts_on))
        time.sleep(max(0.02, hold_ms / 1000.0))
        # Release RTS & DTR
        released = status & ~(termios.TIOCM_RTS | termios.TIOCM_DTR)
        fcntl.ioctl(fd, termios.TIOCMSET, struct.pack("I", released))
        return True, f"Sent RTS/DTR reset pulse to {port}"
    except OSError as exc:
        return False, f"TIOCMSET reset failed: {exc}"
    finally:
        os.close(fd)


class ScreenBackend:
    """Manages an active serial connection via GNU `screen` in a PTY (or direct pyserial)."""

    def __init__(
        self,
        on_data: Callable[[bytes], None],
        on_exit: Callable[[str], None],
    ) -> None:
        self._on_data = on_data
        self._on_exit = on_exit

        self._lock = threading.Lock()
        self._running = False
        self._reader_thread: Optional[threading.Thread] = None

        # GNU screen PTY state
        self._pty_master: Optional[int] = None
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._screenrc_path: Optional[str] = None
        self.session_name: Optional[str] = None
        self.port: Optional[str] = None
        self.baudrate: Optional[int] = None
        self.mode: str = "screen"  # "screen" or "serial"
        self.screen_logging: bool = False

        # Direct pyserial fallback state
        self._serial: Optional[serial.Serial] = None

    @property
    def is_connected(self) -> bool:
        return self._running

    def _cleanup_screenrc(self) -> None:
        if self._screenrc_path:
            try:
                os.unlink(self._screenrc_path)
            except OSError:
                pass
            self._screenrc_path = None

    def _create_screenrc(self) -> str:
        fd, path = tempfile.mkstemp(prefix="screen_ui_", suffix=".rc")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(
                "startup_message off\n"
                "vbell off\n"
                "defscrollback 50000\n"
                "altscreen off\n"
                "defutf8 on\n"
                "term xterm-256color\n"
                "termcapinfo xterm* ti@:te@\n"
            )
        self._screenrc_path = path
        return path

    def connect_screen(
        self,
        port: str,
        baudrate: int,
        session_name: Optional[str] = None,
        rows: int = 48,
        cols: int = 240,
    ) -> None:
        """Start a new GNU `screen <port> <baudrate>` session inside a PTY."""
        if not is_screen_installed():
            raise RuntimeError("GNU `screen` is not installed.")

        self.disconnect(kill_session=False)

        name = session_name or sanitize_session_name(port)
        # Ensure unique session name if an old session with the exact name exists
        existing = {s.name for s in list_screen_sessions()}
        if name in existing:
            name = f"{name}-{int(time.time()) % 10000}"

        rc_path = self._create_screenrc()
        master_fd, slave_fd = pty.openpty()
        try:
            fcntl.ioctl(
                slave_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["SYSSCREENRC"] = "/dev/null"

        try:
            proc = subprocess.Popen(
                ["screen", "-U", "-c", rc_path, "-S", name, port, str(baudrate)],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                env=env,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            self._cleanup_screenrc()
            raise

        os.close(slave_fd)

        with self._lock:
            self.mode = "screen"
            self.port = port
            self.baudrate = baudrate
            self.session_name = name
            self.screen_logging = False
            self._pty_master = master_fd
            self._proc = proc
            self._running = True

        self._reader_thread = threading.Thread(
            target=self._pty_read_loop,
            daemon=True,
            name="screen-ui-pty-reader",
        )
        self._reader_thread.start()

    def attach_screen_session(
        self,
        session_id: str,
        rows: int = 48,
        cols: int = 240,
    ) -> None:
        """Attach to an existing GNU `screen` session (detached or shared)."""
        if not is_screen_installed():
            raise RuntimeError("GNU `screen` is not installed.")

        self.disconnect(kill_session=False)

        master_fd, slave_fd = pty.openpty()
        try:
            fcntl.ioctl(
                slave_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"

        try:
            proc = subprocess.Popen(
                ["screen", "-x", session_id],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                env=env,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise

        os.close(slave_fd)

        with self._lock:
            self.mode = "screen"
            self.port = session_id
            self.baudrate = None
            self.session_name = session_id
            self.screen_logging = False
            self._pty_master = master_fd
            self._proc = proc
            self._running = True

        self._reader_thread = threading.Thread(
            target=self._pty_read_loop,
            daemon=True,
            name="screen-ui-pty-reader",
        )
        self._reader_thread.start()

    def connect_direct_serial(self, port: str, baudrate: int) -> None:
        """Fallback direct pyserial connection without GNU screen."""
        self.disconnect(kill_session=False)
        ser = serial.Serial(port=port, baudrate=baudrate, timeout=0.05)
        with self._lock:
            self.mode = "serial"
            self.port = port
            self.baudrate = baudrate
            self.session_name = None
            self._serial = ser
            self._running = True

        self._reader_thread = threading.Thread(
            target=self._serial_read_loop,
            daemon=True,
            name="screen-ui-serial-reader",
        )
        self._reader_thread.start()

    def _pty_read_loop(self) -> None:
        reason = "Disconnected"
        while self._running:
            fd = self._pty_master
            if fd is None:
                break
            try:
                ready, _, _ = select.select([fd], [], [], 0.15)
            except (OSError, ValueError):
                break

            if ready:
                try:
                    data = os.read(fd, 8192)
                except OSError:
                    reason = "Screen session closed"
                    break
                if not data:
                    reason = "Screen session ended (EOF)"
                    break
                self._on_data(data)

            proc = self._proc
            if proc is not None and proc.poll() is not None:
                # Drain any remaining bytes in PTY buffer
                try:
                    while select.select([fd], [], [], 0.02)[0]:
                        tail = os.read(fd, 8192)
                        if not tail:
                            break
                        self._on_data(tail)
                except OSError:
                    pass
                reason = f"Screen exited (code {proc.returncode})"
                break

        was_running = self._running
        self._close_handles()
        if was_running:
            self._on_exit(reason)

    def _serial_read_loop(self) -> None:
        reason = "Disconnected"
        while self._running:
            ser = self._serial
            if ser is None or not ser.is_open:
                break
            try:
                waiting = ser.in_waiting
                data = ser.read(waiting or 1)
                if data:
                    self._on_data(data)
            except serial.SerialException as exc:
                reason = f"Serial error: {exc}"
                break
            except OSError as exc:
                reason = f"I/O error: {exc}"
                break

        was_running = self._running
        self._close_handles()
        if was_running:
            self._on_exit(reason)

    def _close_handles(self) -> None:
        with self._lock:
            self._running = False
            if self._pty_master is not None:
                try:
                    os.close(self._pty_master)
                except OSError:
                    pass
                self._pty_master = None
            if self._serial is not None:
                try:
                    if self._serial.is_open:
                        self._serial.close()
                except OSError:
                    pass
                self._serial = None
            self._proc = None
        self._cleanup_screenrc()

    def disconnect(self, kill_session: bool = True) -> None:
        """Close the active connection. If `kill_session` is False, detaches cleanly."""
        with self._lock:
            was_running = self._running
            self._running = False
            mode = self.mode
            session = self.session_name
            proc = self._proc

        if was_running and mode == "screen" and session:
            cmd = "quit" if kill_session else "detach"
            try:
                subprocess.run(
                    ["screen", "-S", session, "-X", cmd],
                    capture_output=True,
                    timeout=1.5,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            if proc is not None and proc.poll() is None:
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except OSError:
                        pass

        self._close_handles()

    def send_bytes(self, data: bytes) -> bool:
        if not self._running or not data:
            return False
        if self.mode == "screen" and self._pty_master is not None:
            try:
                os.write(self._pty_master, data)
                return True
            except OSError:
                return False
        if self.mode == "serial" and self._serial is not None:
            try:
                self._serial.write(data)
                self._serial.flush()
                return True
            except (serial.SerialException, OSError):
                return False
        return False

    def send_text(self, text: str) -> bool:
        return self.send_bytes(text.encode("utf-8", errors="replace"))

    def resize(self, rows: int, cols: int) -> None:
        if self.mode == "screen" and self._pty_master is not None:
            try:
                fcntl.ioctl(
                    self._pty_master,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", max(10, rows), max(40, cols), 0, 0),
                )
            except OSError:
                pass

    def _run_screen_cmd(self, *args: str) -> bool:
        if self.mode != "screen" or not self.session_name:
            return False
        try:
            res = subprocess.run(
                ["screen", "-S", self.session_name, "-X", *args],
                capture_output=True,
                timeout=2.0,
            )
            return res.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def send_break(self) -> bool:
        if self.mode == "screen":
            return self._run_screen_cmd("break")
        if self.mode == "serial" and self._serial is not None:
            try:
                self._serial.send_break(duration=0.25)
                return True
            except (serial.SerialException, OSError):
                return False
        return False

    def clear_screen_session(self) -> bool:
        return self._run_screen_cmd("clear")

    def toggle_screen_logfile(self, logfile_path: Optional[str] = None) -> tuple[bool, bool]:
        """Toggle GNU screen's built-in session file logging (`Ctrl+A H`)."""
        if self.mode != "screen" or not self.session_name:
            return False, False
        if not self.screen_logging:
            if logfile_path:
                self._run_screen_cmd("logfile", logfile_path)
            ok = self._run_screen_cmd("log", "on")
            if ok:
                self.screen_logging = True
            return ok, self.screen_logging
        ok = self._run_screen_cmd("log", "off")
        if ok:
            self.screen_logging = False
        return ok, self.screen_logging
