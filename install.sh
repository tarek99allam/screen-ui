#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"

mkdir -p "${BIN_DIR}" "${APP_DIR}"

for cmd in screen-UI screen-ui; do
    cat > "${BIN_DIR}/${cmd}" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="${REPO_DIR}\${PYTHONPATH:+:\$PYTHONPATH}"
exec /usr/bin/python3 -m screen_ui.cli "\$@"
EOF
    chmod +x "${BIN_DIR}/${cmd}"
done

cat > "${APP_DIR}/screen-ui.desktop" <<EOF
[Desktop Entry]
Name=screen-UI
Comment=Modern GTK4 GUI for GNU screen and embedded serial debugging
Exec=${BIN_DIR}/screen-UI
Icon=utilities-terminal
Terminal=false
Type=Application
Categories=Development;Utility;Electronics;
Keywords=serial;screen;uart;ttyUSB;embedded;
EOF

echo "Installed launchers:"
echo "  - ${BIN_DIR}/screen-UI"
echo "  - ${BIN_DIR}/screen-ui"
echo "  - ${APP_DIR}/screen-ui.desktop"
echo ""
echo "Make sure ~/.local/bin is in your PATH, then run: screen-UI"
