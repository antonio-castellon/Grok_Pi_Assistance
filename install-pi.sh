#!/bin/bash
# Install the voice assistant on a clean Raspberry Pi OS.
# Run it as the normal desktop user, from this directory:
#   ./install-pi.sh
# Do not run it as root. It asks for sudo when a system change is required.
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
APP_USER="${SUDO_USER:-$USER}"
if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run ./install-pi.sh as the Pi user, not as root." >&2
  exit 1
fi
APP_HOME="$HOME"

if [[ ! -f /etc/os-release ]] || ! grep -qiE 'debian|raspbian|ubuntu' /etc/os-release; then
  echo "This installer expects Raspberry Pi OS or another Debian system." >&2
  exit 1
fi

echo "Installing packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 \
  python3-venv \
  python3-pip \
  alsa-utils \
  mpv \
  curl \
  ca-certificates \
  bzip2 \
  network-manager \
  console-setup \
  console-setup-linux \
  espeak-ng

echo "Adding $APP_USER to audio, video, input, and netdev"
sudo usermod -aG audio,video,input,netdev "$APP_USER"

echo "Python environment and speech models"
echo "Screen and audio stay as the operating system configured them."
chmod +x "$APP_ROOT/setup.sh"
"$APP_ROOT/setup.sh"

if [[ ! -x "$APP_HOME/.grok/bin/grok" ]]; then
  echo "Installing the Grok CLI"
  curl -fsSL https://x.ai/cli/install.sh | bash || echo "Grok CLI install failed. Install it from the Grok CLI site and sign in on this machine."
fi

install_unit() {
  local name="$1"
  local body="$2"
  local tmp
  tmp="$(mktemp)"
  printf '%s\n' "$body" > "$tmp"
  sudo cp "$tmp" "/etc/systemd/system/$name"
  rm -f "$tmp"
}

install_unit grok-assistant.service "[Unit]
Description=Grok voice assistant
After=sound.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
SupplementaryGroups=audio video input
WorkingDirectory=$APP_ROOT
Environment=HOME=$APP_HOME
Environment=XDG_CONFIG_HOME=$APP_HOME/.config
Environment=PATH=$APP_HOME/.grok/bin:$APP_HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$APP_ROOT/.venv/bin/python $APP_ROOT/assistant.py run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target"

install_unit wifi-boot.service "[Unit]
Description=WiFi setup, then the Grok voice assistant
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
SupplementaryGroups=video input netdev
WorkingDirectory=$APP_ROOT
Environment=HOME=$APP_HOME
Environment=XDG_CONFIG_HOME=$APP_HOME/.config
ExecStart=/usr/bin/python3 $APP_ROOT/wifi-boot.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target"

sudo systemctl daemon-reload
sudo systemctl enable wifi-boot.service
sudo systemctl disable grok-assistant.service || true

echo
echo "Installed in $APP_ROOT for $APP_USER."
echo "HDMI, the screen, and the sound devices were not changed."
echo "Playback and capture use the ALSA default devices. The picture uses fb0."
echo "Sign in to Grok once, on this machine, outside this repository: $APP_HOME/.grok/bin/grok"
echo "Do not put a token or a password in this project."
echo "Start it with: sudo systemctl start wifi-boot"
echo "The assistant itself stays disabled at boot. wifi-boot starts it after the network is up."
echo "The first listening line appears about 18 seconds after the assistant starts."
