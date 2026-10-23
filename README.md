# PiTV

PiTV turns a Raspberry Pi connected to a television into a local TV-control and entertainment system. It provides an on-screen menu, HDMI-CEC control, optional games and AirPlay mirroring, an optional HomeKit/Apple Home television accessory through Homebridge, and separate admin and guest web portals.

> **Development branch:** `experimental`. These instructions target that branch. Review changes before deploying them to a TV you rely on.

## Contents

- [What PiTV does](#what-pitv-does)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install and update](#install-and-update)
- [First-run setup and security](#first-run-setup-and-security)
- [Command reference](#command-reference)
- [Troubleshooting and fixes](#troubleshooting-and-fixes)
- [FAQ](#faq)
- [Backups and removing an SD card](#backups-and-removing-an-sd-card)

## What PiTV does

- **TV control:** power and input selection using HDMI-CEC, where supported by the TV and HDMI path.
- **On-screen menu:** screensavers, optional games and AirPlay mirroring.
- **HomeKit integration (optional):** the `homebridge-pitv-tv` plugin exposes a Television accessory with power, input selection and remote-key navigation, plus switches for Guest Mode, Kill Switch and Joke Mode.
- **Admin web portal:** port `80`; administrator login, TV controls, guest management and Guest Mode.
- **Guest web portal:** port `8080`; gated by Guest Mode, with guest sign-in, a TV-displayed connection code, D-pad and player PIN.
- **Local command line:** the `screen` command wrapper.
- **Optional controllers:** Bluetooth gamepad support and virtual keyboard input for console games.


## How it works

### Menu and TV

`tv_menu.py` runs as `pitv-menu.service` on the Pi's local text console (`tty1`). It owns the menu, CEC interaction and local command listener. The `screen` CLI, portals and Homebridge send it permitted commands.

HDMI-CEC can request TV power changes and select an HDMI input. An *Active Source* message can ask a TV to switch inputs; it does **not** emulate every button on a Fire TV Stick or other HDMI device.


### Web portals and Guest Mode

The same Python file, `guest-portal.py`, runs in two modes:

- `pitv-admin.service` runs it with `--admin` on port `80`.
- `pitv-guest.service` runs it without that flag on port `8080`.

Admin sign-in uses a username and password. Guest sign-in is gated by **Guest Mode** and normally has two steps: the guest's password, followed by a short-lived connection code shown on the television. The second step helps demonstrate that the guest can see the TV. Guests get permitted TV controls, not admin controls.

The portal checks the client's IP address against an optional allowlist. If no networks are configured, any client that can route to the Pi may reach the portal. That does not automatically mean it is reachable from the public internet; router rules, IPv6, VPNs and tunnels matter too.

### Device data and secrets

Device-specific configuration belongs on the Pi and must not be committed to GitHub. Most private records live under `~/.pitv/`, including portal accounts, player PINs, pairing information and signing secrets. The shared Homebridge control key is normally at `/etc/pitv/control.key`. Runtime files and logs are stored under `/tmp/`.

**Never publish passwords, NFC login links, player PINs, emergency codes, session cookies, Homebridge configuration or the full control key.**

## Requirements

### Core system

- A Raspberry Pi running Raspberry Pi OS or a compatible Debian-based system with `systemd` and `apt`.
- An HDMI-connected TV that supports the CEC functions you intend to use.
- Network access for installation and updates.
- A terminal on the Pi or SSH access.
- `git` to download the repository.

The Raspberry Pi Zero 2 W is the primary target. Performance and package availability vary by Pi model, OS release and TV.

### Optional features

- **Apple Home:** a working Homebridge installation. The deploy script tries to install/update the plugin if `hb-service` is available.
- **Gamepad:** a supported USB or Bluetooth controller. Run `setup-input.sh` once and reboot to enable virtual keyboard injection for games.
- **Games:** game executables must be installed if they are not already available.
- **AirPlay mirroring:** UxPlay, GStreamer plugins and Avahi/mDNS. `deploy.sh` checks/installs UxPlay and GStreamer prerequisites; if the required UxPlay version is missing, it may build it from source and needs internet access.

## Install and update

### 1. Install Git

On a fresh Raspberry Pi OS installation:

```bash
sudo apt update
sudo apt install -y git
```

### 2. Download the development branch

Run as your normal Pi login user, not from an unrelated root shell:

```bash
git clone --branch experimental --single-branch https://github.com/CharlieNR/PiTV.git PiTV
cd ~/PiTV
```

If you cloned into a different directory, change into that directory instead.

### 3. Deploy PiTV

```bash
chmod +x deploy.sh setup-input.sh screen *.sh *.py
./deploy.sh
```

The deployment script copies scripts and service templates to `/opt/pitv/`, configures systemd services for the current Linux user, creates/configures the signed-control key and group, and restarts the menu and portals. It may also install/update UxPlay and GStreamer packages. It ends by asking whether to reboot; answering capital `Y` accepts that prompt.

The script uses `sudo` for operations requiring it. Running it from the correct normal account matters because PiTV keeps private configuration under that account's `~/.pitv/` directory.

### 4. Enable controller input (optional)

If the menu responds to a controller but games do not, run the one-time setup and reboot:

```bash
cd ~/PiTV
./setup-input.sh
sudo reboot
```

This loads the `uinput` module, creates a udev rule and adds the PiTV user to the `input` group. The reboot is required for the new group membership to apply to the service.

### 5. Check services

```bash
systemctl --no-pager --full status pitv-menu.service
systemctl --no-pager --full status pitv-admin.service
systemctl --no-pager --full status pitv-guest.service
```

The admin portal is normally `http://<pi-ip-address>/` (port 80); the guest portal is `http://<pi-ip-address>:8080/`. Replace the placeholder with the Pi's actual LAN address. The guest portal shows a gate page while Guest Mode is off.

Check whether the web ports are listening:

```bash
sudo ss -ltnp | grep -E ':(80|8080)([[:space:]]|$)' || echo "Web ports are not listening"
```

A service reported as active does not, by itself, prove that a port is listening; check both the service and the port.

### 6. Update an existing checkout

```bash
cd ~/PiTV
git status --short
git pull --ff-only origin experimental
./deploy.sh
```

If the pull refuses because you have uncommitted edits, stop and preserve them first. Do not blindly discard local changes with `git reset --hard`. Back up edited files outside the repository or use `git stash push` to set local edits aside before pulling. Review the stash later rather than immediately applying old code over updated fixes.

## First-run setup and security

### Create an admin login

Create an administrator account for the port-80 portal:

```bash
screen admin password set <username> <password>
```

The username may be up to 40 characters and cannot contain a vertical bar, tab or newline. The admin password must be at least eight characters. Substitute your own values; do not type the angle brackets literally. Quote a password if it contains shell-special characters.

The command passes the password as an argument and may leave it in shell history. Use a unique password and do not share terminal history.

### Change the emergency code

The emergency code can clear the local keypad lock and full lockdown. The original shipped code is known from the public repository/history, so set a personal six-digit code before relying on PiTV:

```bash
screen emergency status
screen emergency set <your-new-six-digit-code>
screen emergency status
```

Choose a code different from every player's PIN. Do not put it in this repository or reuse the placeholder literally.

### Check command authentication

```bash
screen control status
```

This reports the key path, permissions, fingerprint and whether the detected Homebridge user appears able to read it. A fingerprint is an identifier, not the secret itself.

If the HomeKit TV tile stops responding, run this status command and inspect the logs. Do **not** rotate the key as a first troubleshooting step: after rotation, Homebridge must use the matching new key.

### Restrict the web portals (optional, recommended where practical)

```bash
screen network status
```

With an empty allowlist, every client address that can route to the Pi can try to reach the portals; normal password/session checks still apply. To restrict access, add your **actual home-LAN CIDR**, for example:

```bash
screen network allow 192.168.50.0/24
screen network status
```

That example is correct only if your LAN really uses the `192.168.50.0/24` subnet. Check your router/network settings first. If your phone or laptop is on another VLAN/subnet, allow that network too or you may lock yourself out.

The allowlist is an application-level portal filter, not a general firewall and not a router port-forwarding setting. `screen network clear` empties the allowlist and therefore **removes** this restriction.

## Command reference

Run these commands as the PiTV Linux user, normally from SSH or a local terminal. Most do not need `sudo`. PiTV installs its own `screen` command wrapper; it is not the usual GNU terminal-multiplexer command.

### Navigation and television

| Command | What it does |
|---|---|
| `screen up` / `screen down` | Move through menu items, or send navigation to the current target. |
| `screen left` / `screen right` | Move left/right. |
| `screen enter` | Select the highlighted item. |
| `screen back` | Go back or delete, depending on the current view. |
| `screen clear` | Clear/leave the current PiTV view. |
| `screen on` | Request TV power-on. |
| `screen off` | Request TV standby/off. |
| `screen status` | Query the TV's CEC power state; it can report unknown if the TV does not answer. |
| `screen change` | Toggle between PiTV and Sky based on the last detected input. If the input is unknown, it defaults to PiTV. |
| `screen change pi` | Switch to PiTV (configured as HDMI 2 / physical address `20:00`). |
| `screen change sky` | Switch to Sky (configured as HDMI 1 / physical address `10:00`). |

CEC behaviour depends on the TV, receiver/soundbar, HDMI wiring and CEC settings. Input selection does not provide universal remote-button control for every HDMI device.

### Screensavers, mirroring and games

| Command | What it does |
|---|---|
| `screen fire [seconds]` | Run the fire screensaver. |
| `screen clock [seconds]` | Run the clock screensaver. |
| `screen matrix [seconds]` | Run the Matrix-style screensaver. |
| `screen music` | Start the AirPlay/mirroring receiver and status screen. |
| `screen mirror` | Alias for `screen music`. |
| `screen game <name>` | Start a game if its executable is installed; games require a player PIN. |
| `screen controller` | Scan for a Bluetooth controller for 60 seconds; put the controller in pairing mode first. |

The duration argument is optional. Game names include `snake`, `tetris`, `invaders`, `breakout`, `shooter`, `tictactoe`, `tyrian`, `moonbuggy`, `boulderdash`, `2048`, `sudoku`, `minesweeper`, `curseofwar`, `nethack`, `crawl`, `dopewars` and `supertux`. Some names have aliases. Keyboard-heavy games are easier with a keyboard.

Optional game packages can be installed on a compatible Debian/Raspberry Pi OS release with:

```bash
sudo apt install -y vitetris ninvaders lbreakouthd chromium-bsu opentyrian moon-buggy cavezofphear supertux 2048 nudoku freesweep curseofwar nethack-console crawl dopewars
```

Package availability varies by OS release. Some packages, including `opentyrian` and `chromium-bsu`, may require enabling the appropriate `contrib`/`non-free` repository components. The command cannot install packages that are unavailable in your configured repositories.

### Player PINs

| Command | What it does |
|---|---|
| `screen pin assign <name>` | Create a PIN for a person or show their existing PIN. |
| `screen pin assign <name> custom <six-digit-pin>` | Set a specific unused six-digit PIN. |
| `screen pin list` | List all player PINs. Treat the output as private. |
| `screen pin remove <name>` | Remove a player's PIN. |
| `screen pin revoke <name>` | Alias for removing the PIN. |
| `screen pin rename <old-name> <new-name>` | Rename a PIN holder while keeping their PIN. |
| `screen pin guest rotate all` | Generate fresh player PINs for all guest accounts. |

Player PINs are not web-portal passwords. The list command prints PINs, so do not share its output publicly.

### Emergency code and system

| Command | What it does |
|---|---|
| `screen version` | Show the installed PiTV version. |
| `sudo screen restart "reason"` | Reboot the Pi. The reason is written for the boot-report email. This command intentionally requires root; without root it exits. |
| `screen unlock authorise <six-digit-code>` | Send emergency-code authorisation to clear full lockdown. |
| `screen emergency status` | Check whether the emergency code is still the public default. |
| `screen emergency set <six-digit-code>` | Set a new six-digit emergency code; it takes effect immediately. |
| `screen joke on` | Enable Joke Mode: the TV goes to standby after a random 10–50 minutes, then the cycle repeats. |
| `screen joke off` | Disable Joke Mode. |
| `screen joke status` | Show whether Joke Mode is enabled. |

Three incorrect game PIN attempts trigger the game keypad lock; repeated failed emergency-code attempts can trigger full lockdown. The emergency code is the recovery path.

### Sky Q control

Sky control is off by default. PiTV sends Sky remote-button commands over HDMI-CEC only after it is enabled. Sky's **Control other devices** setting must also be enabled.

| Command | What it does |
|---|---|
| `screen sky status` | Show whether Sky control is enabled, the logical address, current input and navigation routing. |
| `screen sky on` | Enable Sky control using the default logical address. |
| `screen sky on 3` | Enable with logical address 3; use the detected address if yours differs. |
| `screen sky on 3 always` | Always route navigation keys to Sky, even when the TV input is not identified as Sky. |
| `screen sky off` | Disable Sky control (default). |
| `screen sky <button>` | Send a supported button: `guide`, `info`, `sky`, `back`, directional keys, `ok`, playback buttons, channel up/down, colours, or digits 0–9. |
| `screen sky power on` | Request that the Sky box wakes. |
| `screen sky power off` | Request Sky standby. |

### Access control: signed-command key

| Command | What it does |
|---|---|
| `screen control status` | Inspect key path, file permissions, fingerprint and detected Homebridge-user access. |
| `screen control key show` | Print the full secret. Use only when you specifically need to copy it; treat it like a password. |
| `screen control key rotate` | Replace the shared key. Then make sure Homebridge uses the same new key and restart Homebridge. |

**If the key is missing:** the deployment script normally creates `/etc/pitv/control.key`. If the command listener has no readable key, it fails closed and refuses signed-channel commands; it does not accept unsigned messages as a fallback. Homebridge also logs an error and won't send commands if it cannot load a key. The status command helps diagnose this.

### Access control: web-portal IP allowlist

| Command | What it does |
|---|---|
| `screen network status` | Show permitted networks. An empty list means no IP restriction. |
| `screen network allow <cidr>` | Add one allowed IP network. Repeat for additional networks. |
| `screen network clear` | Remove all allowlist entries, restoring the unrestricted-by-IP default. |

This filters which client IP addresses may reach the portals. It is not an operating-system firewall. Normal login authentication still applies, and the client must be in at least one allowed network when an allowlist exists.

### Guest web portal

The guest portal runs on port `8080` and works only while **Guest Mode is ON**. Guest passwords must be at least six characters.

| Command | What it does |
|---|---|
| `screen guest password set <name> <password>` | Create/update a guest; prints their player PIN and NFC login link. Keep both private. |
| `screen guest list` | List guests, player PINs and NFC links. Protect the output. |
| `screen guest remove <name>` | Delete a guest and their associated player PIN. |
| `screen guest kick all` | Immediately sign out every guest by rotating the guest-session key; admin sessions are unaffected. |
| `screen guest code` | Show Guest Mode and connection-code status. |
| `screen guest code show` | Ask the TV to display the connection code for 30 seconds. |
| `screen guest lock status` | Show whether guest controls are paused. |
| `screen guest lock on` | Pause guest controls without signing guests out. |
| `screen guest lock off` | Resume guest controls. |

The connection code is short-lived and rotates. It is not the guest's account password. NFC links are bearer credentials: anyone holding one may be able to sign in as that guest, so treat links like passwords.

### Admin web portal

The admin portal runs on port `80` and is not gated by Guest Mode.

| Command | What it does |
|---|---|
| `screen admin password set <username> <password>` | Create/update an administrator account; password must be at least eight characters. |
| `screen admin list` | List administrator usernames. |
| `screen admin remove <username>` | Remove an administrator account. |

### SSH keyboard remote

| Command | What it does |
|---|---|
| `screen remote on` | Run the interactive keyboard forwarder; press `Esc` to exit. |
| `screen remote off` | Stop a background `remote.py` session if present. |

It can send menu navigation and keyboard input into supported games; it does not replace a second physical controller for every two-player game.

### Logs

| Command | What it does |
|---|---|
| `screen log` | Show the last 30 lines of `/tmp/pitv.log`. |
| `screen log tail` | Follow the log live; press `Ctrl+C` to stop. |
| `screen log show` | Print the full current log file. |
| `screen log clear` | Clear the log on disk and in memory. |
| `screen log copy` | Save a timestamped copy under the current user's home directory. |

Other diagnostics:

```bash
sudo journalctl -u pitv-admin.service -u pitv-guest.service --since "15 minutes ago" --no-pager
sudo journalctl -u pitv-menu.service --since "15 minutes ago" --no-pager
sudo ss -ltnp
```

## Troubleshooting and fixes

### Login goes white or says the request's origin did not match

**Cause:** some browsers or embedded web views submit a form with the HTTP header `Origin: null`. The original origin check treated that literal value as a normal website origin and rejected the login before checking the username or password. A `GET /login` could also return an empty 404, and redirects previously had no HTML fallback.

**Repository fix:** the current `experimental` branch handles `Origin: null` the same way as a missing Origin header, while normal non-null Origin values still have to match the request's Host. The portal also returns a useful response for a rejected non-null Origin, redirects `GET /login` to `/`, and includes an HTML fallback in redirects. This fixes the blank-page compatibility issue without disabling the normal check for real Origin values.

Update and deploy the repository's fix:

```bash
cd ~/PiTV
git status --short
git pull --ff-only origin experimental
./deploy.sh
```

If Git reports local changes that prevent the pull, preserve them first; do not blindly discard them. Back up edited files outside the repository or stash local edits before pulling. Then deploy the updated branch.

Reload `http://<pi-ip-address>/`. If it still fails, collect the recent admin log:

```bash
sudo journalctl -u pitv-admin.service --since "15 minutes ago" --no-pager
```

Look for `Rejected cross-origin POST`, `Origin=`, `Host=`, Python tracebacks or service restarts. Do not post passwords, session cookies or control keys.

### Admin or guest portal does not load

```bash
sudo systemctl --no-pager --full status pitv-admin.service pitv-guest.service
sudo ss -ltnp | grep -E ':(80|8080)([[:space:]]|$)'
```

If a service is not running, inspect its journal before restarting:

```bash
sudo journalctl -u pitv-admin.service -u pitv-guest.service --since "15 minutes ago" --no-pager
sudo systemctl restart pitv-admin.service pitv-guest.service
```

An active service status does not prove a port is listening; check both. A connection error immediately after restarting may be transient, so test again once restart has completed.

### Login says “Wrong login”

- **Admin portal:** create or check an administrator with `screen admin password set <username> <password>`. A guest account is not an admin account.
- **Guest portal:** Guest Mode must be on. The guest enters their own password, then the short-lived connection code shown on the TV.
- After repeated failures, login throttling can delay attempts. Read the page's message and wait instead of repeatedly submitting.

### Home app TV tile does nothing

Run:

```bash
screen control status
screen log
```

Common causes are Homebridge not being able to read `/etc/pitv/control.key`, a manually configured key that differs from the Pi's key, or `pitv-menu.service` not running. If you deliberately rotate the key, ensure Homebridge uses the matching new key and restart it. Never share the output of `screen control key show`.

### Controller works in the menu but not in games

```bash
cd ~/PiTV
./setup-input.sh
sudo reboot
cat /tmp/pitv-mapper.log
```

The one-time script configures device permissions; rebooting is required for the group change to apply to the service.

### HDMI-CEC power or input switching does not work

- Check that the TV and HDMI path support CEC and that CEC is enabled in the TV settings.
- Try `screen status`, `screen change pi` and `screen change sky`.
- Inspect `sudo journalctl -u pitv-menu.service --since "15 minutes ago" --no-pager`.
- Receivers/soundbars between the Pi and TV can affect CEC behaviour.

An input-selection message does not provide universal control of a Fire TV Stick or another streaming device's menu.

### Guest portal shows a gate page

This is expected when Guest Mode is off. Enable Guest Mode through the Home app switch or admin portal. Guest sign-in cannot complete while the mode is off.

### `screen` says the FIFO is missing

```bash
sudo systemctl --no-pager --full status pitv-menu.service
sudo journalctl -u pitv-menu.service --since "15 minutes ago" --no-pager
```

The CLI talks to the running menu through `/tmp/tv_menu.fifo`. If the menu service is stopped or has failed, the FIFO may be missing.

### A network allowlist locks out a phone or laptop

Run `screen network status` from SSH or the Pi's local terminal. Add the device's actual subnet with `screen network allow <cidr>`. If you intentionally want to remove all portal IP restrictions, `screen network clear` empties the list; that allows any address able to route to the Pi to reach the portals again. Do not do this to solve an unrelated login error.

### Is the portal reachable from the internet?

`screen network status` reports PiTV's IP allowlist. It does not report router, IPv6, VPN or tunnel configuration. PiTV does not create router port-forwarding rules or public tunnels as part of installation.

Check those separately if used: router port-forwarding/UPnP, IPv6 firewall rules, reverse proxies, Cloudflare Tunnel and Tailscale Funnel. For example, if Tailscale is installed, inspect `tailscale funnel status`. A process listening on `0.0.0.0:80` or `:8080` is listening on network interfaces, but that alone does not make it publicly reachable through a router.

## FAQ

### What is `screen control status` for?

It diagnoses the key used to authenticate signed commands, especially commands from Homebridge. It reports the key path, permissions, fingerprint and detected Homebridge user's apparent file access. It is not a list of web-portal logins or a scan of internet exposure.

### How could anything send a command without the key?

A local process can technically send a UDP datagram to `127.0.0.1:8129`, but the receiver checks the signature. An unsigned message or a message signed with the wrong key is rejected. Homebridge's commands are signed with the shared key. Other permitted local routes, such as the CLI FIFO and authenticated portal actions, are separate paths; the control key is not an Internet-facing login password.

### What if the key is missing or Homebridge cannot read it?

`deploy.sh` normally creates `/etc/pitv/control.key`. If the command listener has no readable key, it refuses signed-channel commands instead of accepting unsigned messages. The Homebridge plugin also logs an error and does not send commands if it cannot load a key. Run `screen control status` and inspect logs; missing key access makes that integration fail closed.

### Does PiTV expose its ports to the internet?

PiTV does not create router port forwards. Its command listener binds to `127.0.0.1:8129`, so ordinary direct LAN connections cannot connect to that listener. The web portals listen on the Pi's network interfaces and use the optional IP allowlist.

Without router forwarding or a tunnel, inbound IPv4 access from outside a typical home network usually cannot reach the web portals. Still check IPv6 firewall policy, UPnP, VPNs and public tunnels such as Tailscale Funnel or Cloudflare Tunnel if you use them. Do not infer internet exposure from a listening port alone.

### Does `screen network clear` block access?

No. It clears the IP allowlist and returns to the default where any client that can route to the Pi can reach the portals. Use `screen network allow <cidr>` to restrict portal access. Neither command changes router settings or the operating-system firewall.

### Why does guest sign-in have two steps?

The first step proves the guest knows a guest password. The second requires a short-lived code displayed on the TV, which adds proof they can see the television. Guest Mode controls whether the guest portal is available.

### Can PiTV fully control an Amazon Fire TV Stick?

Not currently. PiTV supports CEC power/input selection and explicit Sky Q remote buttons. Switching the TV to the Fire Stick's HDMI input may work, but that is not the same as sending the Fire Stick's navigation buttons. This repository does not currently include a dedicated Fire TV control implementation.

## Backups and removing an SD card

Do not erase an old SD card immediately after moving to a new one. First cold-boot the new card more than once and verify TV power/input, the menu, admin login, guest login, Homebridge, and any games or mirroring you rely on.

Then make a secure offline backup of private data that may exist only on the old card, including relevant `~/.pitv/` contents, `/etc/pitv/control.key`, Homebridge configuration and custom changes. These files contain credentials and must not be uploaded to GitHub.

The source code can be cloned again; device-only configuration and secret keys cannot necessarily be reconstructed from it. Keep the old card untouched as a rollback copy until the new card is stable and you have verified the backup.

## Project files

- `tv_menu.py` — menu, CEC handling, games and signed-command listener.
- `screen` — command-line interface.
- `guest-portal.py` — admin and guest web portals.
- `guest-admin.py` and `pin-admin.py` — account and PIN management.
- `pitv_secrets.py` — password hashing, emergency-code handling and signed-command authentication.
- `pitv-config.py` — control-key and portal-network settings.
- `homebridge-pitv-tv/` — optional Homebridge plugin.
- `deploy.sh` — deployment, systemd services and shared control-key setup.
- `setup-input.sh` — one-time controller key-injection permissions.
