#!/bin/bash
# Record whether the WM8960 and the MHS35 came up, and unmute the codec.
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec > "$ROOT/hat-status.log" 2>&1
date
# One quarter-turn from the current panel orientation (90 -> 180).
# The overlay is read only at boot, so apply it and reboot once.
CONFIG=/boot/firmware/config.txt
if grep -q '^dtoverlay=piscreen,drm,rotate=90$' "$CONFIG"; then
  sed -i 's/^dtoverlay=piscreen,drm,rotate=90$/dtoverlay=piscreen,drm,rotate=180/' "$CONFIG"
  echo "display rotation set to 180; rebooting to apply"
  reboot
  exit 0
fi
echo "=== sound cards ==="
cat /proc/asound/cards
echo "=== capture ==="
arecord -l
echo "=== drm ==="
for f in /sys/class/drm/card*-*/status; do
  printf '%s ' "$f"
  cat "$f" 2>/dev/null
done
echo "=== framebuffers ==="
ls -l /dev/fb* 2>&1
echo "=== input ==="
awk 'BEGIN{RS=""} /Name|Handlers/{print}' /proc/bus/input/devices
echo "=== kernel ==="
dmesg | grep -Ei 'wm8960|ads7846|ili9486|piscreen|rpi-lcd|spi0|mipi|panel|i2s' | tail -60

card=$(sed -n 's/^.*\[\(wm8960[^]]*\)\].*/\1/p' /proc/asound/cards | head -1)
if [[ -z "$card" ]]; then
  echo "WM8960 card not found"
  exit 0
fi
echo "=== unmuting $card ==="
# Playback path. Missing control names are ignored.
for control in \
  "Left Output Mixer PCM" \
  "Right Output Mixer PCM"
do
  amixer -c "$card" sset "$control" on || true
done
# Keep the microphone out of the speakers. Those bypass paths are sidetone.
for control in \
  "Left Output Mixer Boost Bypass" \
  "Right Output Mixer Boost Bypass" \
  "Left Output Mixer LINPUT3" \
  "Right Output Mixer RINPUT3"
do
  amixer -c "$card" sset "$control" off || true
done
amixer -c "$card" sset Speaker 120 || true
amixer -c "$card" sset Headphone 120 || true
playback=220
owner="$(stat -c '%U' "$ROOT")"
owner_home="$(getent passwd "$owner" | cut -d: -f6)"
if [[ -z "$owner_home" ]]; then
  owner_home="/home/$owner"
fi
saved="$owner_home/.config/grok-assistant/volume"
if [[ -f "$saved" ]]; then
  read -r playback < "$saved"
fi
if [[ "$playback" =~ ^[0-9]+$ ]] && (( playback <= 255 )); then
  amixer -c "$card" sset Playback "$playback" || true
else
  amixer -c "$card" sset Playback 220 || true
fi
# Microphone path on the HAT mic jack.
# Modest mic gain. The extra boost switch was off, so "Hey Grok" never got loud
# enough. Full gain rails the input, so stay below that.
amixer -c "$card" sset "Left Input Mixer Boost" on || true
amixer -c "$card" sset "Right Input Mixer Boost" on || true
amixer -c "$card" sset "Left Boost Mixer LINPUT1" on || true
amixer -c "$card" sset "Left Boost Mixer LINPUT3" on || true
amixer -c "$card" sset "Right Boost Mixer RINPUT1" on || true
amixer -c "$card" sset "Right Boost Mixer RINPUT2" on || true
amixer -c "$card" sset "Left Input Boost Mixer LINPUT1" 2 || true
amixer -c "$card" sset "Right Input Boost Mixer RINPUT1" 2 || true
amixer -c "$card" sset Capture 52 || true
amixer -c "$card" sset "ADC PCM" 192 || true
alsactl store || true
echo "=== mixer ==="
amixer -c "$card" scontents
