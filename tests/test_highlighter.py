import unittest

from screen_ui.highlighter import AnsiStreamProcessor, PALETTE, Severity, classify_line, enhance_line


class TestHighlighter(unittest.TestCase):
    def test_classify_embedded_severities(self) -> None:
        self.assertEqual(classify_line("E (1234) wifi: auth failed"), Severity.ERROR)
        self.assertEqual(classify_line("[E][sensor:042]: I2C NACK error"), Severity.ERROR)
        self.assertEqual(classify_line("Guru Meditation Error: Core  1 panic'ed"), Severity.ERROR)
        self.assertEqual(classify_line("Backtrace: 0x400d1a2b:0x3ffb1230"), Severity.ERROR)
        self.assertEqual(classify_line("<err> bt_hci: command timeout"), Severity.ERROR)

        self.assertEqual(classify_line("W (99) mqtt: retrying connection"), Severity.WARN)
        self.assertEqual(classify_line("[W][ota]: low memory warning"), Severity.WARN)
        self.assertEqual(classify_line("<wrn> fs: ring buffer overflow"), Severity.WARN)

        self.assertEqual(classify_line("ets Jun  8 2016 00:22:57"), Severity.BOOT)
        self.assertEqual(classify_line("rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)"), Severity.BOOT)
        self.assertEqual(classify_line("Starting kernel ..."), Severity.BOOT)

        self.assertEqual(classify_line("WiFi connected, IP acquired"), Severity.SUCCESS)
        self.assertEqual(classify_line("AT+CGMR -> OK"), Severity.SUCCESS)

        self.assertEqual(classify_line("I (421) app_main: Heap free 210432"), Severity.INFO)
        self.assertEqual(classify_line("[D][spi]: transfer complete"), Severity.DEBUG)
        self.assertEqual(classify_line("plain serial telemetry 42"), Severity.NORMAL)

    def test_ansi_stream_strips_screen_osc_and_preserves_sgr(self) -> None:
        proc = AnsiStreamProcessor(colorize=True)
        raw = (
            b"\x1b]0;/etc/screenrc: unknown command\x07"
            b"\x1b[?1049h\x1b[1;40r\x1b[H\x1b[2J"
            b"\x1b[32mI (100) boot: ready\x1b[0m\r\n"
        )
        events = proc.feed(raw)
        clears = [e for e in events if e.kind == "clear"]
        appends = [e for e in events if e.kind == "append"]
        self.assertEqual(len(clears), 1)
        self.assertEqual(len(appends), 1)
        self.assertEqual(appends[0].text, "I (100) boot: ready\n")
        self.assertTrue(any(s.fg == "#4ade80" for s in appends[0].spans))

    def test_partial_chunks_and_carriage_return(self) -> None:
        proc = AnsiStreamProcessor(colorize=True)
        ev1 = proc.feed(b"Progress 10%")
        self.assertEqual(ev1[0].kind, "append")
        self.assertEqual(ev1[0].text, "Progress 10%")

        ev2 = proc.feed(b"\rProgress 100%\r\n")
        self.assertEqual(len(ev2), 1)
        self.assertEqual(ev2[0].kind, "replace_line")
        self.assertEqual(ev2[0].text, "Progress 100%\n")

    def test_inline_token_highlighting(self) -> None:
        sev, spans = enhance_line("[wifi] IP 192.168.1.10 MAC AA:BB:CC:DD:EE:FF addr 0x3ffb1234", colorize=True)
        self.assertEqual(sev, Severity.NORMAL)
        fgs = {s.fg for s in spans}
        self.assertIn(PALETTE["tag_fg"], fgs)
        self.assertIn(PALETTE["ip_mac_fg"], fgs)
        self.assertIn(PALETTE["hex_fg"], fgs)


if __name__ == "__main__":
    unittest.main()
