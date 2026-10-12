#!/usr/bin/env fish
# Install/load v4l2loopback and create /dev/video9.
# Detects package manager instead of assuming apt.

set DEVICE_NUM 9
if set -q MTG_V4L2_DEVICE
    set DEVICE_NUM $MTG_V4L2_DEVICE
end
set DEVICE "/dev/video$DEVICE_NUM"

if type -q pacman
    echo "[INFO] Arch-based Linux detected."
    set pkgs dkms v4l2loopback-dkms v4l2loopback-utils v4l-utils
    set krel (uname -r)
    if string match -q '*-zen*' -- $krel
        set pkgs $pkgs linux-zen-headers
    else if string match -q '*-lts*' -- $krel
        set pkgs $pkgs linux-lts-headers
    else if string match -q '*-hardened*' -- $krel
        set pkgs $pkgs linux-hardened-headers
    else
        set pkgs $pkgs linux-headers
    end
    sudo pacman -S --needed $pkgs
    if test $status -ne 0
        echo "[ERROR] pacman could not install the required packages."
        exit 1
    end
else if type -q apt
    echo "[INFO] Debian/Ubuntu-based Linux detected."
    sudo apt update
    sudo apt install -y dkms v4l2loopback-dkms v4l-utils
    if test $status -ne 0
        echo "[ERROR] apt could not install the required packages."
        exit 1
    end
else if type -q dnf
    echo "[INFO] Fedora/RHEL-based Linux detected."
    echo "[ERROR] This script does not guess Fedora's v4l2loopback package source."
    echo "        Install v4l2loopback/akmod-v4l2loopback for your release, then rerun."
    exit 1
else if type -q zypper
    echo "[INFO] openSUSE-based Linux detected."
    echo "[ERROR] Install the matching v4l2loopback kernel module package for your kernel, then rerun."
    exit 1
else
    echo "[ERROR] No supported package manager detected (pacman/apt/dnf/zypper)."
    exit 1
end

if not type -q modprobe
    echo "[ERROR] modprobe is unavailable; install kmod."
    exit 1
end

echo "[INFO] Loading v4l2loopback as $DEVICE..."
sudo modprobe -r v4l2loopback 2>/dev/null
sudo modprobe v4l2loopback video_nr=$DEVICE_NUM card_label="MTG Scanner Phone" exclusive_caps=1
if test $status -ne 0
    echo "[ERROR] modprobe failed."
    exit 1
end
sleep 1

if not test -e "$DEVICE"
    echo "[ERROR] $DEVICE was not created."
    exit 1
end

if type -q v4l2-ctl
    echo "[INFO] Device created. Current V4L2 state:"
    v4l2-ctl --all -d "$DEVICE" | sed -n '1,40p'
end

echo ""
echo "[OK] $DEVICE exists."
echo "     With exclusive_caps=1 it may not say 'Video Capture' until scrcpy is attached."
echo "     Run ./start_scanner.fish to start scrcpy and wait for a real video frame."
