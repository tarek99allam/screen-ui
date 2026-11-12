"""CLI entry point for `screen-UI` / `screen-ui`."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import DEFAULT_BAUDRATE, __version__
from .backend import list_screen_sessions
from .ports import auto_detect_port, list_serial_ports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="screen-UI",
        description="Modern GTK4/Libadwaita GUI wrapper for GNU screen and embedded serial debugging.",
    )
    parser.add_argument(
        "port",
        nargs="?",
        help="Optional serial device path (e.g. /dev/ttyUSB0). Auto-detects if omitted.",
    )
    parser.add_argument(
        "baudrate",
        nargs="?",
        type=int,
        help=f"Optional baudrate (default: {DEFAULT_BAUDRATE}).",
    )
    parser.add_argument(
        "-b",
        "--baud",
        type=int,
        dest="baud_flag",
        help=f"Baudrate (default: {DEFAULT_BAUDRATE}).",
    )
    parser.add_argument(
        "-c",
        "--connect",
        action="store_true",
        help="Auto-connect immediately on startup (also implied when port is passed).",
    )
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="List detected serial ports and active GNU screen sessions, then exit.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"screen-UI {__version__}",
    )
    return parser


def _print_inventory() -> int:
    ports = list_serial_ports(include_legacy_ttys=False)
    print("Detected Serial Ports:")
    if not ports:
        print("  (none)")
    else:
        best = auto_detect_port(ports)
        for p in ports:
            mark = " *" if best and p.device == best.device else "  "
            print(f"{mark} {p.display_label}")

    sessions = list_screen_sessions()
    print("\nActive GNU screen Sessions:")
    if not sessions:
        print("  (none)")
    else:
        for s in sessions:
            print(f"   {s.full_id} ({s.status})")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        return _print_inventory()

    baud = args.baud_flag or args.baudrate or DEFAULT_BAUDRATE
    port = args.port
    auto_connect = bool(args.connect or port is not None)

    from .app import ScreenUIApp

    app = ScreenUIApp(
        initial_port=port,
        initial_baudrate=baud,
        auto_connect=auto_connect,
    )
    return app.run(None)


if __name__ == "__main__":
    sys.exit(main())
