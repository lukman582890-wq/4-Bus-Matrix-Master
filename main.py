"""
Entry point for 4-Bus Digital Matrix Mixer (pywebview WebView2 + System Tray + Auto-Start).
Legacy CustomTkinter GUI is preserved in gui_legacy.py.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

import pystray
import webview
from PIL import Image, ImageDraw

from bridge import Api, get_resource_path

logger = logging.getLogger("matrix_mixer")


def create_tray_image() -> Image.Image:
    """Draw a crisp 64x64 4-bar studio mixer icon for the Windows System Tray."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(18, 23, 32, 255), outline=(52, 61, 75, 255), width=3)
    draw.rounded_rectangle((13, 30, 20, 50), radius=3, fill=(56, 214, 232, 255))
    draw.rounded_rectangle((24, 20, 31, 50), radius=3, fill=(56, 214, 232, 255))
    draw.rounded_rectangle((35, 26, 42, 50), radius=3, fill=(255, 176, 32, 255))
    draw.rounded_rectangle((46, 15, 53, 50), radius=3, fill=(61, 220, 151, 255))
    return img


def main() -> int:
    smoke_test = "--smoke-test" in sys.argv
    test_tray = "--test-tray" in sys.argv
    start_minimized = "--minimized" in sys.argv
    api = Api()
    api.set_window_hidden(start_minimized)
    w, h = api.window_size
    html_path = get_resource_path(r"ui\index.html")

    force_exit = [False]
    tray_icon_ref = [None]

    window = webview.create_window(
        title="4-Bus Matrix Master — Digital Matrix Mixer",
        url=html_path,
        js_api=api,
        width=w,
        height=h,
        min_size=(1100, 700),
        background_color="#0b0e13",
        resizable=True,
        hidden=start_minimized,
    )
    api.attach_window(window)

    def show_window() -> None:
        try:
            api.set_window_hidden(False)
            window.show()
            window.restore()
        except Exception as exc:
            logger.warning("show_window warning: %s", exc)

    def hide_to_tray() -> None:
        try:
            api.set_window_hidden(True)
            window.hide()
        except Exception as exc:
            logger.warning("hide_to_tray warning: %s", exc)

    def full_close() -> None:
        force_exit[0] = True
        api.set_window_hidden(True)
        icon = tray_icon_ref[0]
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        api.shutdown()
        try:
            window.destroy()
        except Exception:
            pass

    api.attach_lifecycle_handlers(on_hide=hide_to_tray, on_exit=full_close)

    def start_tray() -> None:
        if smoke_test:
            return
        try:
            def _on_show(icon, item) -> None:
                show_window()

            def _on_toggle_mute(icon, item) -> None:
                api.set_master_mute(not api.engine.master_mute)

            def _on_exit(icon, item) -> None:
                threading.Thread(target=full_close, daemon=True).start()

            menu = pystray.Menu(
                pystray.MenuItem("Show Mixer", _on_show, default=True),
                pystray.MenuItem("Toggle Master Mute", _on_toggle_mute),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit (Full Close)", _on_exit),
            )
            icon = pystray.Icon("4BusMatrixMaster", create_tray_image(), "4-Bus Matrix Master", menu)
            tray_icon_ref[0] = icon
            threading.Thread(target=icon.run, name="SystemTray", daemon=True).start()
        except Exception as exc:
            logger.exception("System tray failed to start: %s", exc)

    def on_loaded() -> None:
        res = api.start_mixer()
        if not res.get("ok"):
            logger.warning("Auto-start mixer warning: %s", res.get("error"))
        api.start_meter_push()
        start_tray()

        if smoke_test:
            def _auto_close() -> None:
                time.sleep(2.0)
                full_close()

            threading.Thread(target=_auto_close, daemon=True).start()
        elif test_tray:
            def _test_tray_flow() -> None:
                time.sleep(1.0)
                api.hide_to_tray()
                time.sleep(0.8)
                show_window()
                time.sleep(0.6)
                api.full_close()

            threading.Thread(target=_test_tray_flow, daemon=True).start()

    def on_resized(width: int, height: int) -> None:
        api.set_window_size(width, height)

    def on_closing():
        if force_exit[0] or smoke_test or test_tray:
            api.shutdown()
            return True
        # Intercept X click asynchronously so WinForms FormClosing never deadlocks WebView2
        def _ask_js() -> None:
            try:
                window.evaluate_js("window.__showCloseDialog && window.__showCloseDialog()")
            except Exception:
                full_close()

        threading.Thread(target=_ask_js, daemon=True).start()
        return False

    window.events.loaded += on_loaded
    window.events.resized += on_resized
    window.events.closing += on_closing

    try:
        webview.start(gui="edgechromium", debug=False)
    finally:
        icon = tray_icon_ref[0]
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        api.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
