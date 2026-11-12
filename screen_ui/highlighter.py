"""ANSI/VT stream parser and embedded firmware log syntax highlighter."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Optional


class Severity(str, Enum):
    ERROR = "error"
    WARN = "warn"
    SUCCESS = "success"
    BOOT = "boot"
    INFO = "info"
    DEBUG = "debug"
    NORMAL = "normal"


@dataclass(frozen=True)
class StyledSpan:
    """A styled slice within a rendered line."""

    start: int
    end: int
    fg: Optional[str] = None
    bg: Optional[str] = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False


@dataclass
class ParsedEvent:
    """Event emitted by `AnsiStreamProcessor.feed()`."""

    kind: str  # "append", "replace_line", "clear", "newline"
    text: str = ""
    spans: list[StyledSpan] = field(default_factory=list)
    severity: Severity = Severity.NORMAL


# Modern high-contrast GitHub-Dark / Tokyo-Night inspired embedded palette
PALETTE = {
    "fg_default": "#e6edf3",
    "fg_muted": "#8b949e",
    "error_fg": "#ff6b7a",
    "error_bg": "#331419",
    "warn_fg": "#ffb84d",
    "warn_bg": "#2e210e",
    "success_fg": "#4ade80",
    "boot_fg": "#c084fc",
    "info_fg": "#38bdf8",
    "debug_fg": "#94a3b8",
    "timestamp_fg": "#7aa2f7",
    "tag_fg": "#7dcfff",
    "hex_fg": "#d8b4fe",
    "ip_mac_fg": "#2dd4bf",
    "string_fg": "#a3e635",
    "number_fg": "#fdba74",
    "tx_fg": "#60a5fa",
}

# Standard ANSI 16-color table (tuned for dark background readability)
ANSI_16_FG = {
    30: "#484f58",
    31: "#ff6b7a",
    32: "#4ade80",
    33: "#fbbf24",
    34: "#60a5fa",
    35: "#c084fc",
    36: "#22d3ee",
    37: "#e6edf3",
    90: "#8b949e",
    91: "#ff8593",
    92: "#6ee7b7",
    93: "#fde047",
    94: "#93c5fd",
    95: "#d8b4fe",
    96: "#67e8f9",
    97: "#ffffff",
}

ANSI_16_BG = {
    40: "#161b22",
    41: "#5c1a22",
    42: "#163d26",
    43: "#4d380e",
    44: "#172d4d",
    45: "#3b1d54",
    46: "#133d45",
    47: "#484f58",
    100: "#30363d",
    101: "#7f1d1d",
    102: "#14532d",
    103: "#713f12",
    104: "#1e3a8a",
    105: "#581c87",
    106: "#164e63",
    107: "#6e7681",
}


def _ansi256_to_hex(n: int) -> str:
    """Convert xterm 256-color index to #RRGGBB."""
    if n < 0:
        n = 0
    elif n > 255:
        n = 255

    std16 = [
        "#484f58", "#ff6b7a", "#4ade80", "#fbbf24",
        "#60a5fa", "#c084fc", "#22d3ee", "#e6edf3",
        "#8b949e", "#ff8593", "#6ee7b7", "#fde047",
        "#93c5fd", "#d8b4fe", "#67e8f9", "#ffffff",
    ]
    if n < 16:
        return std16[n]
    if n < 232:
        idx = n - 16
        steps = [0, 95, 135, 175, 215, 255]
        r = steps[(idx // 36) % 6]
        g = steps[(idx // 6) % 6]
        b = steps[idx % 6]
        return f"#{r:02x}{g:02x}{b:02x}"
    gray = 8 + (n - 232) * 10
    return f"#{gray:02x}{gray:02x}{gray:02x}"


# --- Embedded Firmware / OS Log Intelligence Patterns ---

_ERROR_RE = re.compile(
    r"(?:"
    r"(?:^|\s)\[?E\]?(?:\s|:|\()|"
    r"<err>|"
    r"\b(?:ERROR|ERR|FATAL|CRITICAL|CRIT|PANIC|FAIL(?:ED|URE)?|EXCEPTION|TRACEBACK|"
    r"ASSERT(?:ION)?\s*FAILED|SEGFAULT|SEGMENTATION\s+FAULT|HARDFAULT|BUS\s+ERROR|"
    r"CORE\s+DUMPED|ABORT(?:ED)?|GURU\s+MEDITATION\s+ERROR|BACKTRACE|WATCHDOG|"
    r"TASK_WDT|IWDT|OOM|OUT\s+OF\s+MEMORY|TIMED?\s*OUT|CRC\s+ERROR|NACK|"
    r"PERMISSION\s+DENIED|ERRNO\s*=?\s*-?\d+)\b"
    r")",
    re.IGNORECASE,
)

_WARN_RE = re.compile(
    r"(?:"
    r"(?:^|\s)\[?W\]?(?:\s|:|\()|"
    r"<wrn>|"
    r"\b(?:WARN(?:ING)?|DEPRECATED|RETRY(?:ING)?|OVERFLOW|UNDERRUN|"
    r"DROPPED|SKIPPED|BROWN-?OUT|UNSTABLE|LOW\s+MEMORY|NOT\s+FOUND)\b"
    r")",
    re.IGNORECASE,
)

_SUCCESS_RE = re.compile(
    r"\b(?:OK|PASS(?:ED)?|SUCCESS(?:FUL(?:LY)?)?|READY|CONNECTED|INITIALIZED|"
    r"BOOTED|DONE|VERIFIED|WIFI\s+CONNECTED|IP\s+ACQUIRED|MQTT\s+CONNECTED|"
    r"OTA\s+COMPLETE)\b",
    re.IGNORECASE,
)

_BOOT_RE = re.compile(
    r"(?:"
    r"ets\s+(?:Jun|Jul|May|Mar|Jan|Feb|Apr|Aug|Sep|Oct|Nov|Dec)\b|"
    r"\b(?:rst:0x[0-9a-fA-F]+|boot:0x[0-9a-fA-F]+|U-Boot|Starting\s+kernel|"
    r"Linux\s+version|ESP-IDF|ESPHome|Zephyr|MicroPython|FreeRTOS)\b"
    r")",
    re.IGNORECASE,
)

_INFO_RE = re.compile(
    r"(?:^|\s)(?:\[I\]|<inf>|I\s+\(\d+\)|\bINFO\b)",
    re.IGNORECASE,
)

_DEBUG_RE = re.compile(
    r"(?:^|\s)(?:\[[DV]\]|<(?:dbg|trc)>|[DV]\s+\(\d+\)|\b(?:DEBUG|TRACE|VERBOSE)\b)",
    re.IGNORECASE,
)

# Inline token highlight rules (applied in order of priority)
_INLINE_RULES: list[tuple[re.Pattern[str], dict[str, object]]] = [
    # Timestamps: [12:34:56.789], [  12.345678], (12345) at start
    (
        re.compile(r"^\[\d{2}:\d{2}:\d{2}(?:\.\d+)?\]|^\[\s*\d+\.\d+\]|(?<=\s)\(\d{2,}\)"),
        {"fg": PALETTE["timestamp_fg"]},
    ),
    # IPv4 & MAC addresses
    (
        re.compile(
            r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"
            r"|\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"
        ),
        {"fg": PALETTE["ip_mac_fg"], "bold": True},
    ),
    # Hex numbers & memory addresses
    (
        re.compile(r"\b0x[0-9A-Fa-f]+\b"),
        {"fg": PALETTE["hex_fg"]},
    ),
    # Bracketed component tags like [wifi], [esp_netif], [I][app:042]
    (
        re.compile(r"\[[A-Za-z0-9_.:-]{2,28}\]"),
        {"fg": PALETTE["tag_fg"]},
    ),
    # Double-quoted strings
    (
        re.compile(r'"[^"\n]{1,120}"'),
        {"fg": PALETTE["string_fg"]},
    ),
    # Values with embedded units (3.3V, 115200bps, 42ms, -65dBm, 98%)
    (
        re.compile(
            r"(?<![A-Za-z0-9_])-?\d+(?:\.\d+)?(?:ms|us|ns|s|Hz|kHz|MHz|GHz|bps|kbps|Mbps|dBm|mV|V|mA|uA|A|KB|MB|GB|bytes|%)\b"
        ),
        {"fg": PALETTE["number_fg"]},
    ),
]


def classify_line(text: str) -> Severity:
    """Determine embedded log severity for a stripped line of text."""
    stripped = text.strip()
    if not stripped:
        return Severity.NORMAL
    if _ERROR_RE.search(stripped):
        return Severity.ERROR
    if _WARN_RE.search(stripped):
        return Severity.WARN
    if _BOOT_RE.search(stripped):
        return Severity.BOOT
    if _SUCCESS_RE.search(stripped):
        return Severity.SUCCESS
    if _INFO_RE.search(stripped):
        return Severity.INFO
    if _DEBUG_RE.search(stripped):
        return Severity.DEBUG
    return Severity.NORMAL


def enhance_line(
    text: str,
    ansi_spans: Optional[list[StyledSpan]] = None,
    colorize: bool = True,
) -> tuple[Severity, list[StyledSpan]]:
    """Combine ANSI SGR spans with smart embedded-log token & severity highlighting."""
    severity = classify_line(text)
    spans: list[StyledSpan] = list(ansi_spans or [])

    if not colorize or not text:
        return severity, spans

    has_ansi = any(s.fg or s.bg for s in spans)

    # Apply line-level severity color when no explicit ANSI colors are present,
    # and always apply subtle row background tint on ERROR / WARN lines.
    if severity == Severity.ERROR:
        spans.insert(0, StyledSpan(0, len(text), fg=None if has_ansi else PALETTE["error_fg"], bg=PALETTE["error_bg"], bold=not has_ansi))
    elif severity == Severity.WARN:
        spans.insert(0, StyledSpan(0, len(text), fg=None if has_ansi else PALETTE["warn_fg"], bg=PALETTE["warn_bg"], bold=not has_ansi))
    elif not has_ansi:
        sev_fg = {
            Severity.SUCCESS: PALETTE["success_fg"],
            Severity.BOOT: PALETTE["boot_fg"],
            Severity.INFO: PALETTE["info_fg"],
            Severity.DEBUG: PALETTE["debug_fg"],
        }.get(severity)
        if sev_fg:
            spans.append(StyledSpan(0, len(text), fg=sev_fg, bold=(severity in (Severity.SUCCESS, Severity.BOOT))))

    # Apply inline token highlights on top of plain/info/normal regions
    if not has_ansi and severity not in (Severity.ERROR, Severity.WARN):
        for regex, style in _INLINE_RULES:
            for m in regex.finditer(text):
                spans.append(
                    StyledSpan(
                        start=m.start(),
                        end=m.end(),
                        fg=style.get("fg"),  # type: ignore[arg-type]
                        bold=bool(style.get("bold", False)),
                    )
                )

    # Even on ERROR/WARN lines, highlight exact keywords & hex/IPs for fast scanning
    if severity in (Severity.ERROR, Severity.WARN) and not has_ansi:
        kw_re = _ERROR_RE if severity == Severity.ERROR else _WARN_RE
        kw_fg = PALETTE["error_fg"] if severity == Severity.ERROR else PALETTE["warn_fg"]
        for m in kw_re.finditer(text):
            spans.append(StyledSpan(m.start(), m.end(), fg=kw_fg, bold=True))
        for m in _INLINE_RULES[2][0].finditer(text):  # hex addresses in crash dumps!
            spans.append(StyledSpan(m.start(), m.end(), fg=PALETTE["hex_fg"], bold=True))

    return severity, spans


class AnsiStreamProcessor:
    """Stateful VT/ANSI parser converting raw PTY byte chunks into clean line events."""

    # Matches CSI (\x1b[...), OSC (\x1b]...BEL|ST), 2-byte ESC, or charset sequences
    _ESC_TOKEN_RE = re.compile(
        r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
        r"|\x1b\[[0-9;?=!<>]*[A-Za-z@~]"      # CSI
        r"|\x1b[()][A-Za-z0-9]"               # Charset
        r"|\x1b[P_^X].*?\x1b\\"               # DCS / PM / APC / SOS
        r"|\x1b[@-Z\\-_=>]"                   # Single ESC commands
    )

    def __init__(self, colorize: bool = True) -> None:
        self.colorize = colorize
        self._pending_raw = ""
        self._cur_line = ""
        self._cur_ansi_spans: list[StyledSpan] = []
        self._fg: Optional[str] = None
        self._bg: Optional[str] = None
        self._bold: bool = False
        self._dim: bool = False
        self._italic: bool = False
        self._underline: bool = False
        self._line_dirty_replace: bool = False

    def reset_style(self) -> None:
        self._fg = None
        self._bg = None
        self._bold = False
        self._dim = False
        self._italic = False
        self._underline = False

    def _apply_sgr(self, params_str: str) -> None:
        if not params_str:
            self.reset_style()
            return
        parts = [p for p in params_str.split(";")]
        codes: list[int] = []
        for p in parts:
            if p == "":
                codes.append(0)
            elif p.isdigit():
                codes.append(int(p))
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                self.reset_style()
            elif c == 1:
                self._bold = True
            elif c == 2:
                self._dim = True
            elif c == 3:
                self._italic = True
            elif c == 4:
                self._underline = True
            elif c == 22:
                self._bold = False
                self._dim = False
            elif c == 23:
                self._italic = False
            elif c == 24:
                self._underline = False
            elif c == 39:
                self._fg = None
            elif c == 49:
                self._bg = None
            elif c in ANSI_16_FG:
                self._fg = ANSI_16_FG[c]
            elif c in ANSI_16_BG:
                self._bg = ANSI_16_BG[c]
            elif c in (38, 48) and i + 1 < len(codes):
                is_fg = c == 38
                mode = codes[i + 1]
                if mode == 5 and i + 2 < len(codes):
                    color = _ansi256_to_hex(codes[i + 2])
                    if is_fg:
                        self._fg = color
                    else:
                        self._bg = color
                    i += 2
                elif mode == 2 and i + 4 < len(codes):
                    r, g, b = codes[i + 2], codes[i + 3], codes[i + 4]
                    color = f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"
                    if is_fg:
                        self._fg = color
                    else:
                        self._bg = color
                    i += 4
            i += 1

    def _emit_plain(self, chunk: str) -> None:
        if not chunk:
            return
        start = len(self._cur_line)
        self._cur_line += chunk
        end = len(self._cur_line)
        if self._fg or self._bg or self._bold or self._dim or self._italic or self._underline:
            self._cur_ansi_spans.append(
                StyledSpan(
                    start=start,
                    end=end,
                    fg=self._fg,
                    bg=self._bg,
                    bold=self._bold,
                    dim=self._dim,
                    italic=self._italic,
                    underline=self._underline,
                )
            )

    def _flush_line(self, events: list[ParsedEvent], with_newline: bool) -> None:
        text = self._cur_line
        severity, spans = enhance_line(text, self._cur_ansi_spans, colorize=self.colorize)
        kind = "replace_line" if self._line_dirty_replace else "append"
        events.append(ParsedEvent(kind=kind, text=text + ("\n" if with_newline else ""), spans=spans, severity=severity))
        if with_newline:
            self._cur_line = ""
            self._cur_ansi_spans = []
            self._line_dirty_replace = False
        else:
            self._line_dirty_replace = True

    def feed(self, raw_bytes: bytes) -> list[ParsedEvent]:
        """Feed raw bytes from PTY/serial and return rendered line events."""
        text = self._pending_raw + raw_bytes.decode("utf-8", errors="replace")
        self._pending_raw = ""

        # Hold back incomplete trailing ESC sequence across read boundaries
        last_esc = text.rfind("\x1b")
        if last_esc != -1 and len(text) - last_esc < 64:
            tail = text[last_esc:]
            if not self._ESC_TOKEN_RE.match(tail):
                self._pending_raw = tail
                text = text[:last_esc]

        events: list[ParsedEvent] = []
        pos = 0
        for m in self._ESC_TOKEN_RE.finditer(text):
            if m.start() > pos:
                self._process_text_segment(text[pos:m.start()], events)
            seq = m.group(0)
            if seq.startswith("\x1b["):
                final = seq[-1]
                body = seq[2:-1]
                if final == "m":
                    self._apply_sgr(body)
                elif final == "J" and body in ("2", "3"):
                    self._cur_line = ""
                    self._cur_ansi_spans = []
                    self._line_dirty_replace = False
                    events.append(ParsedEvent(kind="clear"))
                elif final == "K":
                    # Erase to end of line; on carriage-returned progress lines, clear remainder
                    pass
            pos = m.end()

        if pos < len(text):
            self._process_text_segment(text[pos:], events)

        # Flush partial line without newline so interactive prompts appear immediately!
        if self._cur_line:
            self._flush_line(events, with_newline=False)

        return events

    def _process_text_segment(self, seg: str, events: list[ParsedEvent]) -> None:
        i = 0
        n = len(seg)
        while i < n:
            ch = seg[i]
            if ch == "\r":
                if i + 1 < n and seg[i + 1] == "\n":
                    self._flush_line(events, with_newline=True)
                    i += 2
                    continue
                # Standalone CR: rewind current line (e.g. progress bars)
                self._cur_line = ""
                self._cur_ansi_spans = []
                self._line_dirty_replace = True
                i += 1
            elif ch == "\n":
                self._flush_line(events, with_newline=True)
                i += 1
            elif ch == "\b":
                if self._cur_line:
                    self._cur_line = self._cur_line[:-1]
                    self._cur_ansi_spans = [
                        s for s in self._cur_ansi_spans if s.start < len(self._cur_line)
                    ]
                i += 1
            elif ch == "\x07":  # BEL
                i += 1
            elif ord(ch) < 0x20 and ch not in ("\t",):
                i += 1
            else:
                # Consume run of normal characters
                j = i + 1
                while j < n and seg[j] not in "\r\n\b\x07" and (ord(seg[j]) >= 0x20 or seg[j] == "\t"):
                    j += 1
                self._emit_plain(seg[i:j])
                i = j
