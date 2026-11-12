import os
import pty
import threading
import time
import unittest

from screen_ui.backend import ScreenBackend, is_screen_installed, list_screen_sessions


@unittest.skipUnless(is_screen_installed(), "GNU screen not installed")
class TestScreenBackend(unittest.TestCase):
    def test_screen_pty_session_tx_rx_detach_and_kill(self) -> None:
        dev_master, dev_slave = pty.openpty()
        dev_path = os.ttyname(dev_slave)

        received = bytearray()
        got_data = threading.Event()
        exited = threading.Event()

        def on_data(chunk: bytes) -> None:
            received.extend(chunk)
            if b"HELLO_EMBEDDED" in received:
                got_data.set()

        def on_exit(_reason: str) -> None:
            exited.set()

        backend = ScreenBackend(on_data=on_data, on_exit=on_exit)
        sess_name = f"screen-ui-unittest-{os.getpid()}"

        try:
            backend.connect_screen(port=dev_path, baudrate=115200, session_name=sess_name)
            time.sleep(0.35)

            sessions = list_screen_sessions()
            self.assertTrue(any(s.name == sess_name for s in sessions))

            os.write(dev_master, b"HELLO_EMBEDDED\r\n")
            self.assertTrue(got_data.wait(timeout=3.0), f"Did not receive test output; got={received!r}")

            # TX from UI -> virtual device
            self.assertTrue(backend.send_text("PING\r"))
            time.sleep(0.15)
            echoed = os.read(dev_master, 1024)
            self.assertIn(b"PING", echoed)

            # Detach cleanly (session stays in screen -ls), then reattach, then kill
            backend.disconnect(kill_session=False)
            time.sleep(0.2)
            sessions_after_detach = list_screen_sessions()
            self.assertTrue(any(s.name == sess_name and s.status == "Detached" for s in sessions_after_detach))

            full_id = next(s.full_id for s in sessions_after_detach if s.name == sess_name)
            backend.attach_screen_session(full_id)
            time.sleep(0.25)
            backend.disconnect(kill_session=True)
            time.sleep(0.25)

            sessions_after_kill = list_screen_sessions()
            self.assertFalse(any(s.name == sess_name for s in sessions_after_kill))
        finally:
            backend.disconnect(kill_session=True)
            for fd in (dev_master, dev_slave):
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
