# screen-UI

A modern **GTK4 / Libadwaita** graphical interface for **GNU `screen`** (`screen /dev/ttyUSB0 115200`) and embedded serial debugging on Linux.

Built for firmware, ESP32/ESPHome, Zephyr, STM32, U-Boot, and embedded Linux developers who love the reliability of `screen`, but want a crisp UI with live error/warning intelligence, pause & buffering, scrollable copyable output, baudrate auto-detection, and one-click screenshots.

---

## Features

- **Real GNU `screen` Session Backend**
  - Launches `screen /dev/ttyUSB0 115200` inside a managed PTY (`screen -U -S screen-ui-...`) with clean VT/ANSI stream parsing.
  - Full `screen` control from the UI: **Send Break** (`Ctrl+A b`), **Detach** (`Ctrl+A d`), **Reattach** running `screen -ls` sessions, **Toggle Screen File Log** (`Ctrl+A H`), **Clear** (`Ctrl+A C`), and **Kill** (`Ctrl+A k`).
  - Also includes **RTS/DTR Hardware Reset** pulse for ESP32 / Arduino / STM32 boards and a direct `pyserial` fallback mode.
- **Smart Port & Baudrate Detection**
  - Prioritizes USB/ACM serial adapters (`/dev/ttyUSB*`, `/dev/ttyACM*`) with hardware labels (`FT232R USB UART`, `CP210x`, etc.), filtering out dummy `/dev/ttyS*` noise by default.
  - Live USB hotplug auto-detection while disconnected.
  - Defaults to **`115200`** baud, with an **Auto** baudrate prober that tests common rates (`115200`, `9600`, `921600`, `74880`, `460800`, `230400`, `57600`, `38400`, `19200`) and safely falls back to `115200` when the target is idle.
- **Embedded Log Color Enhancement**
  - Preserves native ANSI colors (`ESP-IDF`, `Zephyr`, `Linux`, etc.) and automatically colorizes plain-text embedded logs:
    - **Errors** (`ERROR`, `FATAL`, `PANIC`, `Guru Meditation Error`, `Backtrace`, `HardFault`, `FAIL`, `Timeout`, `WDT`, `[E]`, `<err>`) in vivid red with subtle row tint.
    - **Warnings** (`WARN`, `WARNING`, `RETRY`, `OVERFLOW`, `BROWNOUT`, `[W]`, `<wrn>`) in warm amber.
    - **Success / Ready / Boot** (`OK`, `PASS`, `CONNECTED`, `ESP-IDF`, `U-Boot`, `Starting kernel`) in emerald green & purple.
    - **Inline Tokens**: timestamps, hex addresses (`0x3ffb...`), IPv4 & MAC addresses, `[component]` tags, quoted strings, and values with units (`3.3V`, `42ms`, `-65dBm`).
  - Live severity filter pills (`All`, `Errors (N)`, `Warnings (N)`, `Info/OK (N)`) and instant search (`Ctrl+F`).
- **Pause, Scroll, Copy & Screenshot**
  - **Pause (`Ctrl+P`)** freezes the view without dropping a single incoming byte (buffers in the background with a live `+N buffered lines` banner).
  - Smooth scrollback up to 50,000 lines with automatic `"↓ Jump to Bottom"` floating pill.
  - One-click **Copy Selection**, **Copy All**, **Copy Errors & Warnings**, or **Save Log**.
  - **Take Screenshot (`Ctrl+Shift+S`)**: renders the full window or terminal pane directly to PNG in `~/Pictures/screen-ui/` and copies the image straight to your clipboard.
- **Terminal Launcher (`screen-UI`)**
  - Launch from any terminal just like `screen`:
    - `screen-UI` (auto-detects port, defaults to `115200`)
    - `screen-UI /dev/ttyUSB0 115200` (auto-connects immediately)
    - `screen-UI --list` (lists detected USB serial ports & running `screen` sessions)

---

## Requirements (Linux / Ubuntu / Debian)

- Python 3.10+
- GNU `screen`
- GTK 4, Libadwaita, PyGObject, and pyserial:

```bash
sudo apt install screen python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-serial
```

---

## Quick Install

```bash
git clone https://github.com/tarek99allam/screen-ui.git
cd screen-ui
./install.sh
```

Then run from any terminal:

```bash
screen-UI
# or connect directly:
screen-UI /dev/ttyUSB0 115200
```

> Make sure your user is in the `dialout` group to access `/dev/ttyUSB*`:
> `sudo usermod -aG dialout $USER` (then log out and back in).

---

## Keyboard Shortcuts

| Shortcut | Action |
| :--- | :--- |
| `Ctrl+P` | Pause / Resume output (with buffering) |
| `Ctrl+Shift+S` | Take PNG screenshot & copy to clipboard |
| `Ctrl+F` | Toggle log search bar |
| `Ctrl+Shift+C` | Smart copy (selection or all output) |
| `Ctrl+L` | Clear terminal output |

---

## License

MIT
