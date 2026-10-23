# PiTV

Turn a Raspberry Pi connected to your TV into a useful TV hub: control TV power and HDMI inputs, browse screensavers and games, mirror Apple devices with AirPlay, and optionally control it from Apple Home using Homebridge.

PiTV also includes admin and guest web pages. The guest page is handy when friends or family want a simple remote on their phones without needing SSH access.

## Contents

- [Features](#features)
- [Installation](#installation)
- [Getting started](#getting-started)
- [Command reference](#command-reference)
- [Troubleshooting and fixes](#troubleshooting-and-fixes)
- [FAQ](#faq)
- [Moving to a new SD card](#moving-to-a-new-sd-card)

## Features

- **TV controls:** power and HDMI input switching through HDMI-CEC, when supported by your TV and HDMI setup.
- **On-screen menu:** screensavers, optional games and AirPlay mirroring.
- **Apple Home (optional):** a Homebridge TV tile for power, inputs and remote-key navigation, plus Guest Mode, Kill Switch and Joke Mode switches.
- **Admin page:** manage accounts and TV controls from a browser.
- **Guest page:** let friends use a phone-friendly remote when Guest Mode is enabled.
- **Game controllers:** optional Bluetooth controllers and game input support.
- **Imposter party game:** built-in multiplayer word-bluffing game with local QR joining, PIN verification, secret roles, timed voting and animated results.

## Installation

### Requirements

A Raspberry Pi running Raspberry Pi OS or a compatible Debian-based system, a TV connected over HDMI, network access for setup, and terminal or SSH access to the Pi. Homebridge is needed only for Apple Home integration. Some games and AirPlay mirroring need optional packages.

### 1. Install Git

On the Pi:

```bash
sudo apt update
sudo apt install -y git
```

### 2. Download PiTV

To get the newest changes, download the `experimental` branch:

```bash
git clone --branch experimental --single-branch https://github.com/CharlieNR/PiTV.git PiTV
cd ~/PiTV
```

For the more conservative `main` branch, replace `experimental` with `main` in the clone command. If you already have a PiTV checkout, skip cloning and open that directory.

### 3. Install and start PiTV

```bash
chmod +x deploy.sh setup-input.sh screen *.sh *.py
./deploy.sh
```

Follow the prompts. The installer copies PiTV into place, configures and restarts its services, and may install or update the software needed for AirPlay mirroring. It asks whether to reboot when finished; answer `Y` if you want to reboot immediately.

Run the installer as your normal Pi login user, not from an unrelated root shell.

### 4. Optional: set up a game controller

If a controller works in the menu but not in games, run this once and reboot:

```bash
cd ~/PiTV
./setup-input.sh
sudo reboot
```

### 5. Check that PiTV is running

```bash
systemctl --no-pager --full status pitv-menu.service
systemctl --no-pager --full status pitv-admin.service
systemctl --no-pager --full status pitv-guest.service
```

The three services should show as running. The admin page is at `http://<pi-ip-address>/` and the guest page is at `http://<pi-ip-address>:8080/`. Replace `<pi-ip-address>` with the Pi's actual home-network address.

## Getting started

### Admin page

Open `http://<pi-ip-address>/` in a browser. Create an admin login from the Pi terminal:

```bash
screen admin password set <username> <password>
```

Replace the placeholders with your chosen credentials; don't type the angle brackets literally. Admin passwords must be at least eight characters. This command passes the password as an argument, so avoid sharing terminal screenshots or shell history containing credentials.

### Guest page for friends and family

Create a guest account:

```bash
screen guest password set <name> <password>
```

Guest passwords must be at least six characters. The command also prints that guest's player PIN and an NFC sign-in link; keep both private.

Turn **Guest Mode** on from the Apple Home switch (if Homebridge is installed) or the admin page. Guests open `http://<pi-ip-address>:8080/`, enter their password, then follow the page's instructions to enter the short-lived connection code shown on the TV. Guest Mode must be on for guest sign-in to work.

To list or remove guests:

```bash
screen guest list
screen guest remove <name>
```

#### Typing into a PiTV game from a phone

When an external PiTV game is running, the guest and admin pages offer **Use phone keyboard**. Open it and claim access to type text into the game from your phone. Only one signed-in browser session can hold this access at once; competing sessions and other remote/game-controller input are blocked while it is held. Press **Done — release access** when finished. The lease also expires after 90 seconds without typing, and it is cleared when the game ends.

This is intended for games that accept console text input. It is not a way to type into the guest/admin sign-in forms, and it does not trigger Apple's native Control Centre Remote keyboard. When PiTV is waiting for a game PIN, signed-in guest and admin pages show a popup for 10 seconds. Tapping **Enter PIN** claims the on-TV keypad on a first-click-wins basis and opens a phone numpad. Every digit is forwarded immediately to the existing TV keypad; HomeKit remote, controller and web navigation input are ignored while the phone owns that PIN prompt. The lease is released when the keypad closes or advances to the next player's PIN, when the user cancels, or after 90 seconds. The emergency-code lock screen is not broadcast as a player-PIN prompt.

The on-TV PIN keypad can still be navigated with the Apple Remote's arrows, Select and Back controls when no phone has claimed it.

### Imposter party game

Select **IMPOSTER** at the top of the Games Library. Choose the number of players, then enter each player's unique six-digit PIN. The TV displays a QR code that players scan; each player enters their PIN again on their own phone. One person is randomly assigned the imposter and a different player chooses a word. The imposter does not see the word. Everyone else sees it privately on their phone.

The TV tracks each player's turn. After one full round, select **GO ROUND AGAIN** to assign new roles and a new word, or select **VOTE**. Voting lasts 15 seconds; players can change their vote until the timer ends. The TV animates all vote counts from least to most (players with zero votes are hidden), waits five seconds, then reveals the actual imposter. All game state stays on the Pi; no external game service is used.

### Set a personal emergency code

The default emergency unlock code for a fresh setup is **`000000`**. It can clear the local keypad lock and full lockdown. Because this is a public default, replace it with your own six-digit code before relying on the lock:

```bash
screen emergency set <your-six-digit-code>
screen emergency status
```

Choose a code different from every player's PIN. Older installations may still have the previous public default, `159753`, saved on disk; the status command recognises both public defaults so you can replace them.

### Apple Home / Homebridge (optional)

If Homebridge is installed, `deploy.sh` attempts to install or update the PiTV Homebridge plugin. If the TV tile is missing or doesn't respond after an update, check the Homebridge logs and restart Homebridge if needed. The plugin is in the `homebridge-pitv-tv/` folder.

### Updating PiTV

After you have installed a version that includes these commands, update directly from the PiTV terminal:

```bash
screen update
```

This follows the `experimental` branch, which receives newer changes for testing. For the `main` branch instead, run:

```bash
screen update safe
```

The updater makes a private backup of your PiTV codes/accounts/settings before deployment, and backs up the shared Homebridge control key if it exists. These backups are saved under `~/pitv-update-backups/`, outside the Git repository. If it finds uncommitted edits in your PiTV checkout, it preserves them in Git stash rather than letting the update overwrite them. It does not automatically reapply those edits to new code.

**Your saved codes are not normally reset by updating.** PiTV's personal settings live on the Pi, separately from its source files, and deployment updates the programs. Valid saved emergency codes, player PINs, guest accounts and admin accounts are preserved. The updater also backs up the private settings before deploying as an extra recovery option.

For the very first upgrade from an older version that does not yet have `screen update`, back up settings and install the updater once:

```bash
BACKUP="$HOME/pitv-update-backups/bootstrap-$(date +%Y%m%d-%H%M%S)"
umask 077
mkdir -p "$BACKUP"
chmod 700 "$HOME/pitv-update-backups" "$BACKUP"
if [ -d "$HOME/.pitv" ]; then
  cp -a "$HOME/.pitv" "$BACKUP/pitv"
  chmod -R go-rwx "$BACKUP/pitv"
fi
if sudo test -f /etc/pitv/control.key; then
  sudo cp -p /etc/pitv/control.key "$BACKUP/control.key"
  sudo chown "$(id -u):$(id -g)" "$BACKUP/control.key"
  chmod 600 "$BACKUP/control.key"
fi

cd ~/PiTV
git status --short
git stash push --include-untracked -m "Backup local changes before PiTV update"
git pull --ff-only origin experimental
PITV_SKIP_REBOOT_PROMPT=1 ./deploy.sh
```

This keeps a private backup of the existing PiTV data and moves uncommitted repository edits into Git stash before pulling. If there are no local edits, Git may say there was nothing to stash; that is fine. If a pull or update fails, stop and read the error instead of resetting the repository or deleting configuration files.

## Command reference

Run commands on the Pi as your normal PiTV user, usually over SSH or at the Pi's terminal. PiTV installs its own `screen` command; it is not the usual terminal-multiplexer program.

### TV and menu controls

| Command | What it does |
|---|---|
| `screen up` / `screen down` | Move up and down in the menu or current target. |
| `screen left` / `screen right` | Move left and right. |
| `screen enter` | Select the highlighted item. |
| `screen back` | Go back or delete, depending on the current screen. |
| `screen clear` | Leave or clear the current PiTV view. |
| `screen on` | Request TV power-on. |
| `screen off` | Request TV standby/off. |
| `screen status` | Ask the TV for its CEC power status; may report unknown if the TV doesn't answer. |
| `screen change` | Toggle between PiTV and Sky using the last detected input; if unknown, it switches to PiTV. |
| `screen change pi` | Switch to PiTV's configured HDMI input (HDMI 2 by default in this setup). |
| `screen change sky` | Switch to Sky's configured HDMI input (HDMI 1 by default in this setup). |
| `screen version` | Show the installed PiTV version. |

CEC depends on the TV, HDMI wiring and CEC settings. Selecting an input is not the same as controlling every button on a streaming stick.

### Screensavers, AirPlay and games

| Command | What it does |
|---|---|
| `screen fire [seconds]` | Run the fire screensaver. |
| `screen clock [seconds]` | Run the clock screensaver. |
| `screen matrix [seconds]` | Run the Matrix-style screensaver. |
| `screen music` | Start the AirPlay/mirroring receiver and its status screen. |
| `screen mirror` | Alias for `screen music`. |
| `screen game <name>` | Launch a game if its executable is installed. Games require a player PIN. |
| `screen controller` | Scan for a Bluetooth controller for 60 seconds; put it into pairing mode first. |

The duration argument is optional; without it, a screensaver runs until stopped. Game names include `snake`, `tetris`, `invaders`, `breakout`, `shooter`, `tictactoe`, `tyrian`, `moonbuggy`, `boulderdash`, `2048`, `sudoku`, `minesweeper`, `curseofwar`, `nethack`, `crawl` and `dopewars`. Some names have aliases, and a game will not start if its software package is missing.

Optional game packages can be installed on a compatible Raspberry Pi OS release with:

```bash
sudo apt install -y vitetris ninvaders lbreakouthd chromium-bsu opentyrian moon-buggy cavezofphear supertux 2048 nudoku freesweep curseofwar nethack-console crawl dopewars
```

Package availability varies by OS release; some games may need extra repository components or may not be available.

### Player PINs

| Command | What it does |
|---|---|
| `screen pin assign <name>` | Create a PIN for a person, or show their existing PIN. |
| `screen pin assign <name> custom <six-digit-pin>` | Set a specific unused six-digit PIN. |
| `screen pin list` | List all player PINs. Keep the output private. |
| `screen pin remove <name>` | Remove a PIN. |
| `screen pin revoke <name>` | Alias for removing the PIN. |
| `screen pin rename <old-name> <new-name>` | Rename a person while keeping their PIN. |
| `screen pin guest rotate all` | Generate fresh PINs for all guest accounts. |

Player PINs unlock games; they are not the same as admin or guest web passwords.

### Emergency code and locks

| Command | What it does |
|---|---|
| `screen emergency status` | Check whether the emergency code is still one of the public defaults. |
| `screen emergency set <six-digit-code>` | Set a new emergency unlock code. |
| `screen unlock authorise <six-digit-code>` | Send the emergency code to clear full lockdown. |
| `screen joke on` | Enable Joke Mode: the TV goes to standby after a random 10–50 minutes, then the cycle repeats. |
| `screen joke off` | Disable Joke Mode. |
| `screen joke status` | Show whether Joke Mode is enabled. |

After three incorrect game PIN attempts, the game keypad locks. Repeated failed emergency-code attempts can trigger full lockdown. The emergency code is the recovery route, so choose a private code and remember it.

### Guest and admin accounts

| Command | What it does |
|---|---|
| `screen guest password set <name> <password>` | Create or update a guest; prints their player PIN and NFC sign-in link. Keep both private. |
| `screen guest list` | List guests, player PINs and NFC links. Keep the output private. |
| `screen guest remove <name>` | Delete a guest and their associated player PIN. |
| `screen guest code` | Show whether Guest Mode is on and whether a connection code is active. |
| `screen guest code show` | Ask the TV to display the connection code for 30 seconds. |
| `screen guest lock status` | Check whether guest controls are paused. |
| `screen guest lock on` | Pause guest controls without signing guests out. |
| `screen guest lock off` | Resume guest controls. |
| `screen guest kick all` | Sign out every guest immediately. |
| `screen admin list` | List administrator usernames. |
| `screen admin remove <username>` | Remove an administrator account. |

### Update commands

| Command | What it does |
|---|---|
| `screen update` | Back up device settings, then update from `experimental` and deploy it. |
| `screen update safe` | Back up device settings, then update from `main` and deploy it. |

Updates do not intentionally reset your saved codes or accounts. Private settings backups are stored in `~/pitv-update-backups/`; local uncommitted code edits are preserved in Git stash.

### Remote and logs

| Command | What it does |
|---|---|
| `screen remote on` | Start the interactive keyboard forwarder; press Esc to exit. |
| `screen remote off` | Stop a background remote session if present. |
| `screen log` | Show the last 30 lines of the PiTV log. |
| `screen log tail` | Follow the log live; press Ctrl+C to stop. |
| `screen log show` | Print the current full log. |
| `screen log clear` | Clear the log. |
| `screen log copy` | Save a timestamped log copy in your home directory. |

---

<details>
<summary><strong>IF YOU HAVE A SKY Q, CLICK HERE (Experimental, not tested)</strong></summary>

Sky Q control is experimental and has not been confirmed on every setup. It uses HDMI-CEC, so it needs a compatible HDMI path and Sky's **Control other devices** setting enabled.

| Command | What it does |
|---|---|
| `screen sky status` | Show whether Sky control is enabled, the device address and where navigation keys are routed. |
| `screen sky on` | Enable Sky control using the default device address. |
| `screen sky on 3` | Enable with logical address 3, if that matches your Sky box. |
| `screen sky on 3 always` | Route navigation keys to Sky even when the active input isn't recognised as Sky. |
| `screen sky off` | Disable Sky control. |
| `screen sky <button>` | Send a supported button such as `guide`, `info`, `sky`, `back`, direction keys, `ok`, playback controls, channel up/down, colour buttons or digits 0–9. |
| `screen sky power on` | Request that the Sky box wakes. |
| `screen sky power off` | Request Sky standby. |

Start with `screen sky status`. Enable it only when you want to test it; use `screen sky off` to disable it again. Results depend on the Sky model and CEC configuration.

</details>

## Troubleshooting and fixes

### Login page goes white or does not sign in

An older version can show a blank page instead of returning to the login form in some browsers. The compatibility fix is included in the current `experimental` branch.

To install the fix:

```bash
cd ~/PiTV
git status --short
git pull --ff-only origin experimental
./deploy.sh
```

If local edits prevent the pull, back them up first; don't discard them blindly. Then deploy the updated branch and reload `http://<pi-ip-address>/`.

If the page still fails, collect recent admin logs:

```bash
sudo journalctl -u pitv-admin.service --since "15 minutes ago" --no-pager
```

Look for errors or a rejected cross-origin request. Never share passwords, session cookies, NFC links or emergency codes in bug reports.

### The menu appears frozen after leaving Music / AirPlay

An older version could leave the menu waiting for a key after exiting `screen music`, making the display appear frozen. The current code restores the menu's normal input behaviour when Music exits. This fix is in both branches; update and test the menu again after leaving Music.

To exit Music, use the remote's Back/Home control or the keyboard's `Esc`, `q`, or `b` key. If it still appears frozen after updating, collect:

```bash
screen log
tail -n 100 /tmp/uxplay.log
sudo journalctl -u pitv-menu.service --since "15 minutes ago" --no-pager
```

### Admin or guest page won't load

```bash
sudo systemctl --no-pager --full status pitv-admin.service pitv-guest.service
sudo ss -ltnp | grep -E ':(80|8080)([[:space:]]|$)'
```

If a service has stopped, inspect its logs before restarting:

```bash
sudo journalctl -u pitv-admin.service -u pitv-guest.service --since "15 minutes ago" --no-pager
sudo systemctl restart pitv-admin.service pitv-guest.service
```

An "active" service status doesn't guarantee the web port is listening. Check both. A connection error immediately after restarting may be transient, so test again once restart has finished.

### Login says the password is wrong

- **Admin page:** use an admin account, not a guest account. Create/change it with `screen admin password set <username> <password>`.
- **Guest page:** enable Guest Mode, enter the guest password, and then follow the instructions for the connection code shown on the TV.
- After repeated failed attempts, PiTV may temporarily slow or block further attempts. Wait for the page's message instead of repeatedly submitting.

### Apple Home TV tile does nothing

Check Homebridge is running and the PiTV plugin is installed. Start with the Homebridge logs and:

```bash
sudo systemctl --no-pager --full status pitv-menu.service
screen log
```

If you deploy an update and the TV tile doesn't appear updated, restart Homebridge.

### Controller works in the menu but not in games

Run the optional input setup and reboot:

```bash
cd ~/PiTV
./setup-input.sh
sudo reboot
```

After reboot, check the controller mapper log:

```bash
cat /tmp/pitv-mapper.log
```

### TV power or input switching doesn't work

- Check the HDMI cable and enable HDMI-CEC in TV settings.
- Try `screen status`, `screen change pi` and `screen change sky`.
- Check recent menu errors with `sudo journalctl -u pitv-menu.service --since "15 minutes ago" --no-pager`.
- A soundbar or AV receiver between the Pi and TV can affect CEC behaviour.

### Guest page says Guest Mode is off

This is expected while Guest Mode is disabled. Turn it on from the Home app switch (if installed) or admin page before guests sign in.

### The screen command says the menu FIFO is missing

```bash
sudo systemctl --no-pager --full status pitv-menu.service
sudo journalctl -u pitv-menu.service --since "15 minutes ago" --no-pager
```

If the menu service has failed, its command channel may be missing until the service starts successfully.

## FAQ

### Can I control everything on a Fire TV Stick with HDMI-CEC?

Not with PiTV's current controls. HDMI-CEC can request that the TV changes input and may support some power functions, but input selection doesn't send every remote button to the Fire TV Stick. Sky Q controls are a separate experimental feature.

### Do guests need SSH access?

No. Guests use the guest page in a phone browser. Create their account on the Pi first and enable Guest Mode. Keep the generated NFC link private, because anyone who has it may be able to use it to sign in as that guest.

### What is the emergency code for?

It is the recovery code for PiTV's local keypad lock and full lockdown. A fresh setup defaults to `000000`; older setups may still use `159753`. Both are public defaults, so replace yours with `screen emergency set <six-digit-code>`.

### Can I erase the old SD card now?

Not immediately. First cold-boot the new card more than once and check TV control, the menu, admin login, guest login, Homebridge, and any games or mirroring you use. Then make and verify a private backup of configuration and credentials that exist only on the old card. Keep the old card untouched as a rollback copy until the new one is reliable.

## Moving to a new SD card

The source code can be cloned again, but personal settings, accounts, codes and local Homebridge configuration may exist only on the old card. Before repurposing it, securely back up anything you need, verify the new card after several cold boots, and keep the old card as a rollback until you're satisfied. Never upload private configuration or credentials to the repository.

