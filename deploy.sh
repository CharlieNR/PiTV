#!/bin/bash
# Exit on any error
set -e

# If deployment fails after stopping the PiTV services, make a best effort to
# bring them back up instead of leaving the TV menu and portals stopped.
PITV_SERVICES_STOPPED=0
pitv_deploy_exit() {
    local rc=$?
    trap - EXIT
    if [[ "$rc" -ne 0 && "$PITV_SERVICES_STOPPED" -eq 1 ]]; then
        echo "Deployment failed; attempting to restart the PiTV services..."
        sudo systemctl start pitv-menu.service pitv-guest.service pitv-admin.service 2>/dev/null || true
    fi
    exit "$rc"
}
trap pitv_deploy_exit EXIT

echo "Deploying Pi TV updates..."

# ── UxPlay version / iOS compatibility ─────────────────────────────
# UxPlay 1.73.7 contains the upstream fix for changed iOS 27 TEARDOWN
# behaviour. Older versions can connect briefly and then drop the mirror.
UXPLAY_VERSION="1.73.7"
UXPLAY_BIN="/usr/local/bin/uxplay"

echo "Checking PiTV UxPlay version..."
INSTALLED_UXPLAY=""
if [ -x "$UXPLAY_BIN" ]; then
    INSTALLED_UXPLAY="$("$UXPLAY_BIN" -h 2>&1 | sed -n "s/^UxPlay \\([0-9][0-9.]*\\).*/\\1/p" | head -1 || true)"
fi

if [ "$INSTALLED_UXPLAY" != "$UXPLAY_VERSION" ]; then
    echo "Installing UxPlay $UXPLAY_VERSION from upstream..."
    sudo apt-get update -qq
    sudo apt-get install -y build-essential cmake pkg-config git libssl-dev libplist-dev libavahi-compat-libdnssd-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav >/dev/null
    rm -rf /tmp/uxplay-build
    git clone --depth 1 --branch "v$UXPLAY_VERSION" https://github.com/FDH2/UxPlay.git /tmp/uxplay-build
    cd /tmp/uxplay-build
    cmake -DNO_X11_DEPS=ON .
    make -j2
    sudo make install
    cd - >/dev/null
    rm -rf /tmp/uxplay-build
    INSTALLED_UXPLAY="$("$UXPLAY_BIN" -h 2>&1 | sed -n "s/^UxPlay \\([0-9][0-9.]*\\).*/\\1/p" | head -1 || true)"
    if [ "$INSTALLED_UXPLAY" != "$UXPLAY_VERSION" ]; then
        echo "ERROR: UxPlay installation did not report version $UXPLAY_VERSION."
        echo "Installed report: ${INSTALLED_UXPLAY:-unknown}"
        exit 1
    fi
    echo "UxPlay $INSTALLED_UXPLAY installed."
else
    echo "UxPlay $INSTALLED_UXPLAY already installed."
fi

# ── AirPlay hardware decoding prerequisites ─────────────────────────
# Give the Zero 2 W enough GPU memory for the Broadcom video decoder.
# UxPlay's Raspberry Pi testing found 128 MB worked well on Zero 2 W.
CONFIG="/boot/firmware/config.txt"
if [ -f "$CONFIG" ]; then
    if grep -qE '^[[:space:]]*gpu_mem=' "$CONFIG"; then
        sudo sed -i -E 's/^[[:space:]]*gpu_mem=.*/gpu_mem=128/' "$CONFIG"
    else
        printf '\n# PiTV: GPU memory for AirPlay H.264 hardware decoding\ngpu_mem=128\n'             | sudo tee -a "$CONFIG" >/dev/null
    fi
    echo "GPU memory configured to 128 MB (reboot required to apply)."
fi

# Pi Zero 2 W screen mirroring is dramatically faster with the Broadcom
# H.264 decoder. UxPlay uses GStreamer's v4l2h264dec/v4l2convert for this.
echo "Checking PiTV AirPlay framebuffer + hardware-decoding support..."
if command -v apt-get >/dev/null 2>&1; then
    if sudo apt-get update -qq && sudo apt-get install -y         gstreamer1.0-tools         gstreamer1.0-plugins-good         gstreamer1.0-plugins-bad         gstreamer1.0-libav >/dev/null; then
        echo "GStreamer video plugins ready."
    else
        echo "Warning: could not update/install GStreamer video plugins (including fbdevsink)."
        echo "Mirroring will fall back to software decoding if necessary."
    fi
fi

if command -v modprobe >/dev/null 2>&1; then
    sudo depmod -a 2>/dev/null || true
    if sudo modprobe bcm2835-codec 2>/dev/null; then
        echo "Broadcom bcm2835-codec module loaded."
    else
        echo "Warning: bcm2835-codec module is unavailable."
        echo "Mirroring will fall back to software decoding."
    fi

    # Keep the codec module loaded after reboot as well.
    echo "bcm2835-codec" | sudo tee /etc/modules-load.d/pitv-bcm2835-codec.conf >/dev/null
fi

if command -v gst-inspect-1.0 >/dev/null 2>&1; then
    if gst-inspect-1.0 v4l2h264dec >/dev/null 2>&1; then
        echo "V4L2 H.264 hardware decoder is available."
    else
        echo "V4L2 H.264 hardware decoder is still unavailable."
    fi
fi

# Imposter generates a genuine QR PNG and writes its pixels directly to /dev/fb0.
# This avoids fbi's virtual-terminal/console handling; no terminal-character QR fallback is used.
if ! python3 -c 'import pitv_framebuffer; import pitv_tvui; import pitv_artwork; import cairosvg; import qrcode; from qrcode.constants import ERROR_CORRECT_M; from PIL import Image, ImageDraw, ImageFont' >/dev/null 2>&1; then
    echo "Installing Python QR/Pillow dependencies for direct framebuffer rendering..."
    sudo apt-get install -y python3-qrcode python3-pil python3-cairosvg fonts-dejavu-core
fi
if ! python3 -c 'import pitv_framebuffer; import pitv_tvui; import pitv_artwork; import cairosvg; import qrcode; from qrcode.constants import ERROR_CORRECT_M; from PIL import Image, ImageDraw, ImageFont' >/dev/null 2>&1; then
    echo "ERROR: PiTV image UI, framebuffer, QR, or Pillow dependencies are unavailable."
    exit 1
fi

# The on-TV Controllers page uses BlueZ's standard bluetoothctl CLI and
# PiTV's existing evdev listener. Install Debian/Raspberry Pi OS packages only;
# do not alter the system Python environment with pip.
NEED_BT_PACKAGES=0
command -v bluetoothctl >/dev/null 2>&1 || NEED_BT_PACKAGES=1
python3 -c 'import evdev' >/dev/null 2>&1 || NEED_BT_PACKAGES=1
if [ "$NEED_BT_PACKAGES" -eq 1 ]; then
    echo "Installing Bluetooth and controller input dependencies (bluez, python3-evdev)..."
    sudo apt-get update -qq
    sudo apt-get install -y bluez python3-evdev
fi
if ! command -v bluetoothctl >/dev/null 2>&1; then
    echo "ERROR: bluetoothctl is still unavailable after installing bluez."
    exit 1
fi
if ! python3 -c 'import evdev' >/dev/null 2>&1; then
    echo "WARNING: python3-evdev is unavailable; Bluetooth pairing may work, but controller events will not be read."
fi

# Fire art uses cacafire from Debian's caca-utils package.
# Install it on first deployment so the Fire option cannot silently exit to the menu.
if ! command -v cacafire >/dev/null 2>&1; then
    echo "Installing the PiTV Fire artwork dependency (caca-utils)..."
    if sudo apt-get update -qq && sudo apt-get install -y caca-utils; then
        echo "Fire artwork dependency ready."
    else
        echo "WARNING: caca-utils could not be installed. Fire will show an error until it is installed."
    fi
fi

# Make sure all scripts are executable
chmod +x *.sh *.py 2>/dev/null || true

# Ensure destination directory exists
sudo mkdir -p /opt/pitv

# Safely copy scripts, configs, and service files (skipping hidden folders like .git)
sudo cp -f *.sh *.py *.service *.c /opt/pitv/ 2>/dev/null || true
sudo cp -f .gitignore VERSION /opt/pitv/ 2>/dev/null || true

# Install the bundled offline Lucide SVGs required by V2. The renderer creates
# persistent PNG caches in the PiTV user's ~/.cache/pitv directory.
if [ -d assets/icons ]; then
    sudo mkdir -p /opt/pitv/assets
    sudo rm -rf /opt/pitv/assets/icons
    sudo cp -rf assets/icons /opt/pitv/assets/
    sudo chown -R root:root /opt/pitv/assets
    sudo find /opt/pitv/assets -type d -exec chmod 755 {} \;
    sudo find /opt/pitv/assets -type f -exec chmod 644 {} \;
fi

# A previous interrupted deployment can leave /opt/pitv or a newly copied
# script unreadable to the normal PiTV account. Make the code readable/traversable
# before running state initialisation as that account.
sudo chown root:root /opt/pitv
sudo chmod 755 /opt/pitv
sudo chown root:root /opt/pitv/*.py /opt/pitv/*.sh /opt/pitv/*.service
sudo chmod 755 /opt/pitv/*.py /opt/pitv/*.sh

# Build the native RGB565 framebuffer converter. This turns pixel packing into
# one compiled-code pass instead of millions of slow ImageMath pixel operations.
# Build to a temporary file so a failed compile never deletes a working helper.
if [ -f /opt/pitv/pitv_rgb565.c ] && command -v cc >/dev/null 2>&1; then
    if sudo cc -O3 -fPIC -shared -Wall -Wextra \
        -o /opt/pitv/pitv_rgb565.so.new /opt/pitv/pitv_rgb565.c; then
        sudo mv -f /opt/pitv/pitv_rgb565.so.new /opt/pitv/pitv_rgb565.so
        sudo chown root:root /opt/pitv/pitv_rgb565.so
        sudo chmod 755 /opt/pitv/pitv_rgb565.so
        echo "Native RGB565 framebuffer converter ready."
    else
        sudo rm -f /opt/pitv/pitv_rgb565.so.new
        echo "WARNING: native RGB565 converter could not be compiled; using safe Python fallback."
    fi
else
    echo "WARNING: C compiler unavailable; using safe Python fallback for framebuffer conversion."
fi

# Copy the Homebridge TV plugin folder AND reinstall it into Homebridge.
# npm copies the plugin at install time, so refreshing /opt/pitv alone doesn't
# update the running plugin — Homebridge must reinstall it from that path.
if [ -d homebridge-pitv-tv ]; then
    sudo cp -rf homebridge-pitv-tv /opt/pitv/
    if command -v hb-service >/dev/null 2>&1; then
        echo "Reinstalling Homebridge PiTV TV plugin..."
        sudo hb-service add /opt/pitv/homebridge-pitv-tv 2>/dev/null || true
        sudo hb-service restart 2>/dev/null || true
    fi
fi

# Install the `screen` CLI (no extension, so not caught by the globs above)
if [ -f screen ]; then
    sudo cp -f screen /usr/local/bin/screen
    sudo chmod +x /usr/local/bin/screen
fi

# ── Central persistent state ─────────────────────────────────────────
# All durable PiTV application data lives in ~/.pitv/pitv-state.json:
# EMERGENCY_PIN, CONTROL_KEY, portal secrets, guest/admin accounts, player
# PINs, pairing state, Sky/network settings and persistent lockout state.
# ensure_state imports older split files only when the central file is absent;
# if it already exists, it is left untouched.
PITV_USER="${SUDO_USER:-$(id -un)}"
if ! id "$PITV_USER" >/dev/null 2>&1; then
    echo "ERROR: install user '$PITV_USER' does not exist."
    exit 1
fi
PITV_GROUP="$(id -gn "$PITV_USER")"
# fbi needs access to the Linux framebuffer device; on Raspberry Pi OS this is
# normally owned by root:video. Add the PiTV service user to that group.
if getent group video >/dev/null 2>&1; then
    sudo usermod -aG video "$PITV_USER"
else
    echo "WARNING: the video group is missing; the Imposter PNG viewer may not access /dev/fb0."
fi
PITV_HOME="$(getent passwd "$PITV_USER" | cut -d: -f6)"
if [ -z "$PITV_HOME" ] || [ ! -d "$PITV_HOME" ]; then
    echo "ERROR: could not resolve home directory for '$PITV_USER'."
    exit 1
fi
echo "Configuring PiTV services for user '$PITV_USER' (home: $PITV_HOME)."

HB_USER="$(sed -n 's/^User=//p' /etc/systemd/system/homebridge.service \
                                /lib/systemd/system/homebridge.service 2>/dev/null | head -1)"
[ -z "$HB_USER" ] && id homebridge >/dev/null 2>&1 && HB_USER="homebridge"

sudo mkdir -p /etc/pitv
getent group pitv >/dev/null || sudo groupadd pitv

# Install a fixed-action maintenance helper. The TV UI can request only
# approved updates, a PiTV menu restart, or a device reboot; it cannot pass
# arbitrary shell commands through sudo. The helper and deployment runner are
# installed root-owned outside the user-writable checkout.
if [ -f pitv-maintenance.py ]; then
    sudo install -D -o root -g root -m 0755 pitv-maintenance.py /usr/local/sbin/pitv-maintenance
else
    echo "This branch predates the TV maintenance helper; keeping the already-installed helper."
fi
sudo install -D -o root -g root -m 0755 deploy.sh /usr/local/lib/pitv/deploy-runner.sh
MAINT_CONFIG="$(/usr/bin/python3 -c 'import json,sys; print(json.dumps({"user":sys.argv[1],"home":sys.argv[2],"repo":sys.argv[2]+"/PiTV"}))' "$PITV_USER" "$PITV_HOME")"
printf '%s\n' "$MAINT_CONFIG" | sudo tee /etc/pitv/maintenance.json >/dev/null
sudo chown root:root /etc/pitv/maintenance.json
sudo chmod 644 /etc/pitv/maintenance.json
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/pitv-maintenance *\n' "$PITV_USER" \
    | sudo tee /etc/sudoers.d/pitv-maintenance >/dev/null
sudo chown root:root /etc/sudoers.d/pitv-maintenance
sudo chmod 440 /etc/sudoers.d/pitv-maintenance
if ! sudo visudo -cf /etc/sudoers.d/pitv-maintenance >/dev/null; then
    echo "ERROR: restricted PiTV maintenance sudoers rule failed validation."
    exit 1
fi
# Preserve access to the old group-readable key for the one-time migration.
sudo usermod -aG pitv "$PITV_USER" 2>/dev/null || true

# Stop the current services before migrating and removing their old live files.
sudo systemctl stop pitv-menu.service pitv-guest.service pitv-admin.service 2>/dev/null || true
PITV_SERVICES_STOPPED=1

# Populate the persistent icon PNG cache while deployment is in progress.
# This prevents a first-use CairoSVG rasterisation from blocking TV rendering.
echo "Preparing the PiTV V2 icon cache..."
if ! (
    cd /opt/pitv
    sudo -u "$PITV_USER" -H /usr/bin/env PYTHONPATH=/opt/pitv \
        /usr/bin/python3 -c 'import pitv_artwork; print(f"Prepared {pitv_artwork.warm_icon_cache()} V2 icons.")'
); then
    echo "WARNING: V2 icon prewarming failed; deployment will continue."
fi

STATE_FILE="$PITV_HOME/.pitv/pitv-state.json"
if sudo -u "$PITV_USER" -H test -f "$STATE_FILE"; then
    echo "Central state file already exists; deploy.sh will not modify or recreate it."
else
    STATE_RESULT="$(sudo -u "$PITV_USER" -H /usr/bin/env PITV_STATE_FILE="$STATE_FILE" \
        /usr/bin/python3 /opt/pitv/pitv_state.py ensure)"
    case "$STATE_RESULT" in
        CREATED) echo "Created central state file and imported existing PiTV accounts/settings." ;;
        EXISTS)  echo "Central state file was created by another process; left it untouched." ;;
        *)       echo "ERROR: central state initialisation returned: $STATE_RESULT"; exit 1 ;;
    esac
fi
echo "Persistent state: $STATE_FILE"

# Verify the control key exists in central state before preparing Homebridge.
# If an existing state file is incomplete, preserve the old key and fail safely.
if ! sudo -u "$PITV_USER" -H /usr/bin/env PITV_STATE_FILE="$STATE_FILE" \
    /usr/bin/python3 /opt/pitv/pitv_state.py get-control-key >/dev/null 2>&1; then
    echo "ERROR: central state file has no readable CONTROL_KEY."
    echo "Existing control key (if any) has been retained at /etc/pitv/control.key."
    exit 1
fi
# Homebridge needs the control key to sign local commands, but must not be able
# to read password hashes, account tokens or other private state. Give it one
# root-owned, no-argument helper which returns only CONTROL_KEY.
if [ -n "$HB_USER" ] && id "$HB_USER" >/dev/null 2>&1; then
    sudo tee /usr/local/bin/pitv-read-control-key >/dev/null <<EOF
#!/bin/sh
PITV_STATE_FILE="$PITV_HOME/.pitv/pitv-state.json" exec /usr/bin/python3 /opt/pitv/pitv_state.py get-control-key
EOF
    sudo chown root:root /usr/local/bin/pitv-read-control-key
    sudo chmod 755 /usr/local/bin/pitv-read-control-key
    printf '%s ALL=(root) NOPASSWD: /usr/local/bin/pitv-read-control-key ""\n' "$HB_USER" \
        | sudo tee /etc/sudoers.d/pitv-control-key >/dev/null
    sudo chown root:root /etc/sudoers.d/pitv-control-key
    sudo chmod 440 /etc/sudoers.d/pitv-control-key
    sudo visudo -cf /etc/sudoers.d/pitv-control-key >/dev/null
    sudo rm -f /etc/pitv/control.key
    echo "Installed restricted control-key access for Homebridge user '$HB_USER'."
    if command -v hb-service >/dev/null 2>&1; then
        sudo hb-service restart 2>/dev/null || true
    fi
else
    echo "WARNING: Homebridge user was not found; the old system control key, if present,"
    echo "has been retained for recovery. Run deploy again after Homebridge is installed."
fi

# Reload systemd services
echo "Reloading systemd service..."
# Render service templates for this installation's user/group/home. The
# checked-in files contain placeholders so no specific Linux account is needed.
for service in pitv-menu.service pitv-guest.service pitv-admin.service; do
    sudo sed \
        -e "s|@PITV_USER@|$PITV_USER|g" \
        -e "s|@PITV_GROUP@|$PITV_GROUP|g" \
        -e "s|@PITV_HOME@|$PITV_HOME|g" \
        "$service" | sudo tee "/etc/systemd/system/$service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl restart pitv-menu.service
sudo systemctl enable pitv-guest.service pitv-admin.service 2>/dev/null || true
sudo systemctl restart pitv-guest.service
sudo systemctl restart pitv-admin.service
PITV_SERVICES_STOPPED=0

echo "----------------------------------------"
echo "Deploy complete! All files synced to /opt/pitv/"
echo "----------------------------------------"

# The emergency (master) code used to be hardcoded in files that are in git, so
# say so plainly until this Pi has one of its own.
if ! sudo -u "$PITV_USER" -H /usr/bin/env PITV_STATE_FILE="$STATE_FILE" \
    /usr/bin/python3 /opt/pitv/pitv_state.py check-emergency-status >/dev/null 2>&1; then
    echo
    echo "!! The emergency code on this Pi is still the one published in git."
    echo "!! Set your own now:  screen emergency set <6 digits>"
    echo
fi

# Keep the original interactive reboot choice for manual deployments, but
# never try to display a prompt when the trusted maintenance runner is detached.
if [ "${PITV_SKIP_REBOOT_PROMPT:-0}" = "1" ]; then
    echo "Reboot prompt skipped (detached maintenance update)."
else
    read -p "Do you want to reboot the Raspberry Pi now? [Y/n]: " answer
    if [ "$answer" = "Y" ]; then
        echo "Rebooting Pi..."
        sudo reboot
    else
        echo "Reboot skipped."
    fi
fi
