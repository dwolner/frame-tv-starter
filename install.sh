#!/bin/bash
# Frame TV starter kit installer (macOS).
#   ./install.sh                       install / update
#   ./install.sh --add-phone-key 'ssh-ed25519 AAAA… Shortcuts on iPhone'
#   ./install.sh --uninstall
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="$HOME/.config/frame-tv"
BIN="$HOME/.local/bin"
LABEL="local.frame-tv.check"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST" "$BIN/frame" "$BIN/frame-ssh"
  echo "Removed the background job and commands. Settings/photos cache left in $CONF."
  echo "Remove any 'frame-ssh' lines from ~/.ssh/authorized_keys by hand."
  exit 0
fi

if [ "${1:-}" = "--add-phone-key" ]; then
  key="${2:-}"
  case "$key" in ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-*) ;; *) echo "Paste the whole public key line in quotes (starts with ssh-ed25519)."; exit 1 ;; esac
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  touch "$HOME/.ssh/authorized_keys" && chmod 600 "$HOME/.ssh/authorized_keys"
  if grep -qF "$(echo "$key" | awk '{print $2}')" "$HOME/.ssh/authorized_keys"; then echo "That key is already added."; exit 0; fi
  cp "$HOME/.ssh/authorized_keys" "$HOME/.ssh/authorized_keys.bak-$(date +%Y%m%d%H%M%S)"
  echo "command=\"$BIN/frame-ssh\",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty $key" >> "$HOME/.ssh/authorized_keys"
  echo "Added. That phone can now run only the TV commands over SSH."
  exit 0
fi

[ "$(uname)" = "Darwin" ] || { echo "This kit is for macOS."; exit 1; }
# Must be Apple's Python: a downloaded Python (uv, pyenv, python.org) gets blocked by
# macOS Local Network privacy and every TV call fails with "No route to host".
PY=/usr/bin/python3
"$PY" -c 'import sys; assert sys.version_info >= (3, 9)' 2>/dev/null || {
  echo "Apple's Python isn't installed yet. Run:  xcode-select --install   then re-run ./install.sh"; exit 1; }

echo "→ Python environment"
"$PY" -m venv "$HERE/.venv"
"$HERE/.venv/bin/pip" install -q --upgrade pip
"$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"

echo "→ Commands in $BIN"
mkdir -p "$BIN" "$CONF"
printf '#!/bin/sh\nexec "%s/.venv/bin/python" "%s/frame.py" "$@"\n' "$HERE" "$HERE" > "$BIN/frame"
cp "$HERE/frame-ssh" "$BIN/frame-ssh"
chmod +x "$BIN/frame" "$BIN/frame-ssh"

if [ ! -f "$CONF/config.json" ]; then
  cp "$HERE/config.example.json" "$CONF/config.json"
  echo "→ Created $CONF/config.json — edit it (photo source, alerts)"
fi

echo "→ Background check every 30 minutes ($LABEL)"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$HERE/.venv/bin/python</string>
    <string>$HERE/frame.py</string>
    <string>check</string>
  </array>
  <key>StartInterval</key><integer>1800</integer>
  <key>StandardOutPath</key><string>$CONF/check.out</string>
  <key>StandardErrorPath</key><string>$CONF/check.err</string>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

case ":$PATH:" in *":$BIN:"*) ;; *) echo "→ Add to your shell:  export PATH=\"\$HOME/.local/bin:\$PATH\"";; esac
echo
if [ -f "$CONF/token" ]; then
  echo "Done. Already paired — try:   frame status"
else
  echo "Done. Next: turn the TV on, then run   frame pair <TV IP address>"
fi
