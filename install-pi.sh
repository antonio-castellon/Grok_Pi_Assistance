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
CONFIG_DIR="$APP_HOME/.config/grok-assistant"

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

boot_config() {
  local config="/boot/firmware/config.txt"
  if [[ ! -f "$config" ]]; then
    config="/boot/config.txt"
  fi
  if [[ ! -f "$config" ]]; then
    echo "No boot config.txt found. Add the HAT overlays by hand." >&2
    return
  fi
  local changed=0
  local line extra=""
  for line in \
    "dtparam=i2c_arm=on" \
    "dtparam=i2s=on" \
    "dtparam=spi=on" \
    "dtoverlay=wm8960-soundcard" \
    "dtoverlay=piscreen,drm,rotate=180"
  do
    if grep -q "^${line}$" "$config"; then
      continue
    fi
    if grep -q "^#${line}$" "$config"; then
      sudo sed -i "s/^#${line}$/${line}/" "$config"
      changed=1
      continue
    fi
    extra="${extra}${line}"$'\n'
    changed=1
  done
  if [[ -n "$extra" ]]; then
    printf '\n# grok-assistant HAT\n[all]\n%s' "$extra" | sudo tee -a "$config" >/dev/null
  fi
  if [[ "$changed" -eq 1 ]]; then
    echo "Boot overlays written to $config. Reboot once so the HAT and the panel come up."
    touch "$APP_ROOT/.reboot-needed"
  else
    echo "Boot overlays already present in $config"
  fi
}

boot_config

echo "Python environment and speech models"
chmod +x "$APP_ROOT/setup.sh" "$APP_ROOT/hat-bringup.sh" "$APP_ROOT/install-wm8960.sh"
"$APP_ROOT/setup.sh"

if [[ ! -x "$APP_HOME/.grok/bin/grok" ]]; then
  echo "Installing the Grok CLI"
  curl -fsSL https://x.ai/cli/install.sh | bash || echo "Grok CLI install failed. Run: curl -fsSL https://x.ai/cli/install.sh | bash"
fi

mkdir -p "$CONFIG_DIR/bin"
cat > "$CONFIG_DIR/sudo-askpass" <<EOF
#!/bin/bash
cat "$CONFIG_DIR/sudo.pass"
EOF
cat > "$CONFIG_DIR/bin/sudo" <<EOF
#!/bin/bash
export SUDO_ASKPASS="$CONFIG_DIR/sudo-askpass"
exec /usr/bin/sudo -A "\$@"
EOF
chmod 700 "$CONFIG_DIR/bin" "$CONFIG_DIR/sudo-askpass" "$CONFIG_DIR/bin/sudo"
if [[ -f "$CONFIG_DIR/sudo.pass" ]]; then
  echo "Leaving the existing sudo password file in place"
elif [[ -t 0 ]]; then
  echo "Voice shutdown runs sudo. The password is stored only in $CONFIG_DIR/sudo.pass (mode 600)."
  echo "Press Enter to skip. You can create that file later."
  pass=""
  read -r -s -p "sudo password: " pass
  echo
  if [[ -n "$pass" ]]; then
    printf '%s\n' "$pass" > "$CONFIG_DIR/sudo.pass"
    chmod 600 "$CONFIG_DIR/sudo.pass"
  else
    echo "Skipped the sudo password. Voice shutdown stays unavailable until that file exists."
  fi
  unset pass
else
  echo "No terminal, so no sudo password was stored. Voice shutdown needs $CONFIG_DIR/sudo.pass later."
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

install_unit hat-bringup.service "[Unit]
Description=Unmute WM8960 and record HAT status
After=sound.target multi-user.target
Wants=sound.target

[Service]
Type=oneshot
User=root
ExecStart=$APP_ROOT/hat-bringup.sh
RemainAfterExit=yes

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
sudo systemctl enable hat-bringup.service wifi-boot.service
sudo systemctl disable grok-assistant.service || true

echo
echo "Installed in $APP_ROOT for $APP_USER."
echo "Sign in to Grok once, on this machine: $APP_HOME/.grok/bin/grok"
echo "Cloud answers need that login. Local commands do not."
if [[ -f "$APP_ROOT/.reboot-needed" ]]; then
  rm -f "$APP_ROOT/.reboot-needed"
  echo "Reboot now so the sound HAT and the touch panel load: sudo reboot"
else
  echo "Start it with: sudo systemctl start wifi-boot"
  echo "The assistant itself stays disabled at boot. wifi-boot starts it after the network is up."
fi
echo "The first listening line appears about 18 seconds after the assistant starts."
