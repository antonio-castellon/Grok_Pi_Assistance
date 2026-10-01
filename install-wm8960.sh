#!/bin/bash
# Stage the WM8960 HAT. Run this only after the board is seated on the 40-pin header.
set -euo pipefail

CONFIG=/boot/firmware/config.txt
if [[ ! -f "$CONFIG" ]]; then
  CONFIG=/boot/config.txt
fi

if ! grep -q '^dtparam=i2c_arm=on' "$CONFIG"; then
  sudo sed -i 's/^#dtparam=i2c_arm=on/dtparam=i2c_arm=on/' "$CONFIG"
fi
if ! grep -q '^dtparam=i2c_arm=on' "$CONFIG"; then
  echo 'dtparam=i2c_arm=on' | sudo tee -a "$CONFIG" >/dev/null
fi

if ! grep -q '^dtparam=i2s=on' "$CONFIG"; then
  sudo sed -i 's/^#dtparam=i2s=on/dtparam=i2s=on/' "$CONFIG"
fi
if ! grep -q '^dtparam=i2s=on' "$CONFIG"; then
  echo 'dtparam=i2s=on' | sudo tee -a "$CONFIG" >/dev/null
fi

if ! grep -q '^dtoverlay=wm8960-soundcard' "$CONFIG"; then
  echo 'dtoverlay=wm8960-soundcard' | sudo tee -a "$CONFIG" >/dev/null
fi

echo "WM8960 overlay is in $CONFIG."
echo "Reboot once the HAT is powered on the header: sudo reboot"
