"""Native GTK4 widget/window PNG screenshot capture and clipboard copy."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")

from gi.repository import Gdk, Graphene, Gtk  # noqa: E402


def default_screenshot_dir() -> Path:
    xdg_pics = os.environ.get("XDG_PICTURES_DIR")
    base = Path(xdg_pics) if xdg_pics else (Path.home() / "Pictures")
    target = base / "screen-ui"
    target.mkdir(parents=True, exist_ok=True)
    return target


def capture_widget_texture(widget: Gtk.Widget) -> Optional[Gdk.Texture]:
    """Render any realized GTK4 widget (window or terminal pane) to a Gdk.Texture."""
    width = widget.get_width()
    height = widget.get_height()
    if width <= 0 or height <= 0:
        return None

    native = widget.get_native()
    if native is None:
        return None
    renderer = native.get_renderer()
    if renderer is None:
        return None

    paintable = Gtk.WidgetPaintable.new(widget)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, float(width), float(height))
    node = snapshot.to_node()
    if node is None:
        return None

    rect = Graphene.Rect().init(0.0, 0.0, float(width), float(height))
    return renderer.render_texture(node, rect)


def take_screenshot(
    widget: Gtk.Widget,
    prefix: str = "screen-ui",
    copy_to_clipboard: bool = True,
    output_path: Optional[Path] = None,
) -> Optional[Path]:
    """Capture `widget` as a PNG file and optionally copy image to clipboard."""
    texture = capture_widget_texture(widget)
    if texture is None:
        return None

    if output_path is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_path = default_screenshot_dir() / f"{prefix}-{stamp}.png"
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    texture.save_to_png(str(output_path))

    if copy_to_clipboard:
        try:
            clipboard = widget.get_clipboard()
            clipboard.set(texture)
        except Exception:
            pass

    return output_path
