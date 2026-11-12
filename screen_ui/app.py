"""Libadwaita Application wrapper for screen-UI."""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio  # noqa: E402

from . import APP_ID, DEFAULT_BAUDRATE
from .window import ScreenUIWindow


class ScreenUIApp(Adw.Application):
    """Main GTK4/Adwaita application class."""

    def __init__(
        self,
        initial_port: Optional[str] = None,
        initial_baudrate: int = DEFAULT_BAUDRATE,
        auto_connect: bool = False,
    ) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self._initial_port = initial_port
        self._initial_baudrate = initial_baudrate
        self._auto_connect = auto_connect
        self._window: Optional[ScreenUIWindow] = None

    def do_activate(self) -> None:
        if self._window is None:
            self._window = ScreenUIWindow(
                app=self,
                initial_port=self._initial_port,
                initial_baudrate=self._initial_baudrate,
                auto_connect=self._auto_connect,
            )
        self._window.present()
