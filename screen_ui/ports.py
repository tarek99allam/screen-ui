"""Serial port discovery, USB prioritization, and baudrate auto-detection."""

from __future__ import annotations

from dataclasses import dataclass
import glob
import os
import re
import time
from typing import Callable, Iterable, Optional

import serial
import serial.tools.list_ports

from . import DEFAULT_BAUDRATE

COMMON_BAUDRATES: list[int] = [
    9600,
    19200,
    38400,
    57600,
    74880,
    115200,
    230400,
    460800,
    921600,
    1000000,
    1500000,
    2000000,
]

# Ordered by likelihood on modern embedded targets (default 115200 first).
AUTO_BAUD_CANDIDATES: list[int] = [
    115200,
    9600,
    921600,
    74880,
    460800,
    230400,
    57600,
    38400,
    19200,
]

_NUM_RE = re.compile(r"(\d+)$")
_TOKEN_HINT_RE = re.compile(
    rb"(?:\b(?:OK|ERROR|WARN|INFO|DEBUG|BOOT|READY|ESP|WIFI|UART|CPU|rst|boot)\b|\r\n|\x1b\[)"
)


@dataclass(frozen=True)
class SerialPortInfo:
    """Metadata describing a discovered serial port."""

    device: str
    description: str
    manufacturer: str = ""
    product: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial_number: str = ""
    subsystem: str = ""
    is_usb: bool = False

    @property
    def short_name(self) -> str:
        return os.path.basename(self.device)

    @property
    def display_label(self) -> str:
        hw = self.product or (
            self.description if self.description and self.description != "n/a" else ""
        )
        if self.vid is not None and self.pid is not None:
            vid_pid = f"{self.vid:04x}:{self.pid:04x}"
            if hw:
                return f"{self.device} — {hw} ({vid_pid})"
            return f"{self.device} ({vid_pid})"
        if hw:
            return f"{self.device} — {hw}"
        return self.device


def _port_sort_key(info: SerialPortInfo) -> tuple[int, int, str, int]:
    """Rank ttyUSB/ttyACM USB adapters first, then virtual/RFCOMM, then ttyS*."""
    dev = info.device
    if "/ttyUSB" in dev:
        tier = 0
    elif "/ttyACM" in dev:
        tier = 1
    elif info.is_usb:
        tier = 2
    elif "/rfcomm" in dev or "/pts/" in dev:
        tier = 3
    else:
        tier = 4

    match = _NUM_RE.search(dev)
    num = int(match.group(1)) if match else 9999
    return (tier, 0 if info.is_usb else 1, re.sub(r"\d+$", "", dev), num)


def _is_dummy_ttys(port_obj: object) -> bool:
    device = getattr(port_obj, "device", "") or ""
    hwid = getattr(port_obj, "hwid", "") or ""
    desc = getattr(port_obj, "description", "") or ""
    subsystem = getattr(port_obj, "subsystem", "") or ""
    return (
        "/ttyS" in device
        and hwid in ("", "n/a")
        and desc in ("", "n/a")
        and subsystem == "serial-base"
    )


def list_serial_ports(include_legacy_ttys: bool = False) -> list[SerialPortInfo]:
    """Return discovered serial ports sorted with USB embedded adapters first."""
    results: list[SerialPortInfo] = []
    seen: set[str] = set()

    for p in serial.tools.list_ports.comports():
        if not include_legacy_ttys and _is_dummy_ttys(p):
            continue
        is_usb = bool(
            p.vid is not None
            or (p.subsystem and "usb" in p.subsystem.lower())
            or "/ttyUSB" in p.device
            or "/ttyACM" in p.device
        )
        info = SerialPortInfo(
            device=p.device,
            description=p.description or "",
            manufacturer=p.manufacturer or "",
            product=p.product or "",
            vid=p.vid,
            pid=p.pid,
            serial_number=p.serial_number or "",
            subsystem=p.subsystem or "",
            is_usb=is_usb,
        )
        results.append(info)
        seen.add(p.device)

    # Also surface /dev/ttyUSB* or /dev/ttyACM* symlinks if udev missed metadata
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/rfcomm*"):
        for path in sorted(glob.glob(pattern)):
            if path not in seen:
                results.append(
                    SerialPortInfo(
                        device=path,
                        description="USB Serial Device",
                        is_usb=("/rfcomm" not in path),
                    )
                )
                seen.add(path)

    results.sort(key=_port_sort_key)
    return results


def auto_detect_port(ports: Optional[Iterable[SerialPortInfo]] = None) -> Optional[SerialPortInfo]:
    """Select the highest-priority active serial port (USB/ACM first)."""
    candidates = list(ports) if ports is not None else list_serial_ports(include_legacy_ttys=False)
    if not candidates:
        return None
    candidates.sort(key=_port_sort_key)
    return candidates[0]


def score_serial_sample(data: bytes) -> float:
    """Heuristic score in [0.0, 1.0+] indicating valid decoded serial text.

    Returns 0.0 for empty input. Clean printable ASCII/UTF-8 text with newlines
    scores high (~0.85 - 1.2); baudrate-mismatched framing noise scores low (< 0.4).
    """
    if not data:
        return 0.0

    n = len(data)
    printable = 0
    control_bad = 0
    newlines = 0

    for b in data:
        if 0x20 <= b <= 0x7E:
            printable += 1
        elif b in (0x09, 0x0A, 0x0D, 0x1B):  # tab, LF, CR, ESC
            printable += 1
            if b in (0x0A, 0x0D):
                newlines += 1
        elif b == 0x00 or b < 0x07 or (0x0E <= b < 0x20 and b != 0x1B) or b == 0x7F:
            control_bad += 1

    ratio = printable / n
    bad_ratio = control_bad / n

    # Penalize framing errors heavily (lots of high bytes / NULs / control chars)
    score = ratio - (bad_ratio * 1.5)

    if newlines > 0:
        score += min(0.12, newlines / max(1, n) * 2.0)

    if _TOKEN_HINT_RE.search(data):
        score += 0.15

    # Penalize ultra-short 1-2 byte random glitches unless pure ASCII
    if n < 3 and ratio < 1.0:
        score -= 0.4

    return max(0.0, score)


def detect_baudrate(
    port: str,
    candidates: Optional[list[int]] = None,
    sample_seconds: float = 0.32,
    send_probe: bool = True,
    default_baud: int = DEFAULT_BAUDRATE,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> tuple[int, bool, str]:
    """Probe candidate baudrates on `port` and return (baudrate, detected_live, message).

    If the target sends no data during probing, safely falls back to `default_baud` (115200).
    """
    probe_list = list(candidates or AUTO_BAUD_CANDIDATES)
    if default_baud not in probe_list:
        probe_list.insert(0, default_baud)

    best_baud = default_baud
    best_score = 0.0
    any_bytes = False

    for baud in probe_list:
        if progress_cb is not None:
            progress_cb(baud)
        try:
            with serial.Serial(
                port=port,
                baudrate=baud,
                timeout=0.05,
                write_timeout=0.1,
            ) as ser:
                ser.reset_input_buffer()
                if send_probe:
                    try:
                        ser.write(b"\r\n")
                        ser.flush()
                    except serial.SerialException:
                        pass

                buf = bytearray()
                deadline = time.monotonic() + sample_seconds
                while time.monotonic() < deadline:
                    waiting = ser.in_waiting
                    chunk = ser.read(waiting or 64)
                    if chunk:
                        buf.extend(chunk)
                        if len(buf) >= 512:
                            break
                    else:
                        time.sleep(0.02)

                if buf:
                    any_bytes = True
                    sc = score_serial_sample(bytes(buf))
                    if sc > best_score:
                        best_score = sc
                        best_baud = baud
                    # Strong immediate match at default or common baud
                    if sc >= 0.92 and len(buf) >= 8:
                        return (
                            baud,
                            True,
                            f"Detected {baud} baud ({len(buf)} bytes, confidence {min(100, int(sc * 90))}%)",
                        )
        except serial.SerialException as exc:
            return (
                default_baud,
                False,
                f"Cannot probe {port}: {exc}. Keeping {default_baud}.",
            )

    if any_bytes and best_score >= 0.65:
        return (
            best_baud,
            True,
            f"Detected {best_baud} baud (score {best_score:.2f})",
        )

    if any_bytes:
        return (
            default_baud,
            False,
            f"Signal unclear (best score {best_score:.2f}); using default {default_baud}",
        )

    return (
        default_baud,
        False,
        f"No active TX on {port}; using default {default_baud}",
    )
