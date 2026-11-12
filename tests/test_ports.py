import unittest

from screen_ui.ports import SerialPortInfo, auto_detect_port, score_serial_sample


class TestPorts(unittest.TestCase):
    def test_auto_detect_prefers_usb_over_ttys(self) -> None:
        ports = [
            SerialPortInfo(device="/dev/ttyS0", description="ttyS0", is_usb=False),
            SerialPortInfo(device="/dev/ttyACM0", description="ESP32-S3", vid=0x303A, pid=0x1001, is_usb=True),
            SerialPortInfo(device="/dev/ttyUSB0", description="FT232R USB UART", vid=0x0403, pid=0x6001, is_usb=True),
        ]
        picked = auto_detect_port(ports)
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.device, "/dev/ttyUSB0")

    def test_score_serial_sample_distinguishes_text_vs_garbage(self) -> None:
        good = b"I (120) boot: ESP-IDF v5.2\r\nOK\r\n"
        bad = bytes([0x00, 0x01, 0xFE, 0xFF, 0x03, 0x81, 0x00, 0x99, 0x02, 0x7F] * 4)
        self.assertGreater(score_serial_sample(good), 0.9)
        self.assertLess(score_serial_sample(bad), 0.3)
        self.assertEqual(score_serial_sample(b""), 0.0)


if __name__ == "__main__":
    unittest.main()
