# Grok Pi Assistance

Spanish voice assistant for a Raspberry Pi 4. It listens on a WM8960 HiFi HAT, draws on a 480×320 SPI touch panel, and speaks the reply. Exact local orders run on the Pi. Questions go to Grok in the cloud. There is no on-device Grok model.

The behavior of the running program is in [PI_ASSISTANCE_GROK.md](PI_ASSISTANCE_GROK.md). [ASSISTANCE_GROK.md](ASSISTANCE_GROK.md) is the desktop tray clone, not the Pi.

## Clean install

On a fresh Raspberry Pi OS, with the WM8960 HAT and the SPI panel seated:

```bash
git clone https://github.com/antonio-castellon/Grok_Pi_Assistance.git
cd Grok_Pi_Assistance
./install-pi.sh
```

Run that as the normal user, not as root. The script:

- installs Python, ALSA, mpv, NetworkManager, the console fonts, and espeak-ng
- puts the user in the `audio`, `video`, `input`, and `netdev` groups
- writes the WM8960 and `piscreen` boot overlays (`rotate=180`)
- creates `.venv` and downloads the speech models (about 1.5 GB)
- installs the Grok CLI if `~/.grok/bin/grok` is missing
- installs `hat-bringup`, `wifi-boot`, and `grok-assistant` for this user
- enables the HAT script and the Wi-Fi boot service

`grok-assistant` stays disabled at boot. `wifi-boot` starts it after the network answers. If there is no network, the panel shows a touch menu to join Wi-Fi.

After the first install, reboot once so the overlays load. Then sign in:

```bash
~/.grok/bin/grok
```

The CLI opens a browser login. On a Pi with no browser, set `XAI_API_KEY` before starting the assistant. Cloud questions need that login. Wakes, volume, music, and voice enrollment do not.

`setup.sh` alone refreshes the virtualenv and the models. `install-pi.sh` is the full machine setup.

## What is not in this repository

Speech models, the virtualenv, and `bin/` are downloaded or built on the Pi. They are listed in `.gitignore`.

These stay on the device and must not be committed:

- `~/.config/grok-assistant/sudo.pass` (the sudo password used for voice shutdown)
- `~/.config/grok-assistant/speakers.json` (voice prints)
- session files under `~/.config/grok-assistant/` and `~/.grok/sessions/`

`config.json` contains the spoken administrator key `1515`. That is a local voice passphrase, not the sudo password. Change it on a machine other people can talk to.

Nemotron is not installed. On this Pi it was too slow to be a menu choice.
