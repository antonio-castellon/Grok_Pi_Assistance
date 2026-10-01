# Grok Pi Assistance

Spanish voice assistant for a Raspberry Pi. It listens and speaks on the sound devices Raspberry Pi OS already selected, and it draws on the framebuffer the OS assigned (`fb0`). Exact local orders run on the Pi. Questions go to Grok in the cloud. There is no on-device Grok model.

The behavior of the running program is in [PI_ASSISTANCE_GROK.md](PI_ASSISTANCE_GROK.md). [ASSISTANCE_GROK.md](ASSISTANCE_GROK.md) is the desktop tray clone, not the Pi.

## Clean install

On a fresh Raspberry Pi OS:

```bash
git clone https://github.com/antonio-castellon/Grok_Pi_Assistance.git
cd Grok_Pi_Assistance
./install-pi.sh
```

Run that as the normal user, not as root. The script:

- installs Python, ALSA utilities, mpv, NetworkManager, the console fonts, and espeak-ng
- puts the user in the `audio`, `video`, `input`, and `netdev` groups
- leaves HDMI, the screen, and the sound devices as Raspberry Pi OS configured them
- creates `.venv` and downloads the speech models (about 1.5 GB)
- installs the Grok CLI if `~/.grok/bin/grok` is missing
- installs `wifi-boot` and `grok-assistant` for this user
- enables the Wi-Fi boot service only

`grok-assistant` stays disabled at boot. `wifi-boot` starts it after the network answers. If there is no network, `fb0` shows a touch menu to join Wi-Fi.

The picture is `fb0`. Playback and the microphone are the ALSA `default` devices. Sign in on the machine, outside this repository:

```bash
~/.grok/bin/grok
```

Cloud questions need that login. Wakes, volume, music, and voice enrollment do not. Do not commit a token.

`setup.sh` alone refreshes the virtualenv and the models. `install-pi.sh` is the full machine setup.

## What is not in this repository

Speech models, the virtualenv, and `bin/` are downloaded or built on the Pi. They are listed in `.gitignore`.

These stay on the device and must not be committed:

- any password, sudo secret, or API token
- `~/.config/grok-assistant/config.json` when it holds a spoken administrator key
- `~/.config/grok-assistant/speakers.json` (voice prints)
- session files under `~/.config/grok-assistant/` and `~/.grok/sessions/`

The `config.json` in this repository has an empty `admin_key`. Administrator mode stays closed until a key is set on the machine.

Nemotron is not installed. On this Pi it was too slow to be a menu choice.
