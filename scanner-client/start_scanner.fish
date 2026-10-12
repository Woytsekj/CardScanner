#!/usr/bin/env fish

# AMD ROCm/MIOpen: favor fast solver selection for interactive OCR.
# This reduces first-use/solver-search latency; inference quality is unchanged.
set -gx MIOPEN_FIND_MODE FAST
set -gx MIOPEN_ENABLE_LOGGING 0
# MTG Scanner launcher.
# Phone mode: scrcpy -> v4l2loopback -> /dev/video9.
# Uses the project's virtualenv Python when available, so helper scripts and
# Scanner.py use the same installed dependencies.

set SCRIPT_DIR (dirname (realpath (status filename)))
if test -f "$SCRIPT_DIR/scanner.env.fish"
    source "$SCRIPT_DIR/scanner.env.fish"
end
cd "$SCRIPT_DIR"

# Pick one Python interpreter and use it for EVERYTHING.
# 1. Active virtualenv
# 2. Project-local .venv
# 3. python from PATH
set PYTHON_BIN ""
if set -q VIRTUAL_ENV; and test -x "$VIRTUAL_ENV/bin/python"
    set PYTHON_BIN "$VIRTUAL_ENV/bin/python"
else if test -x "$SCRIPT_DIR/.venv/bin/python"
    set PYTHON_BIN "$SCRIPT_DIR/.venv/bin/python"
else if type -q python
    set PYTHON_BIN (command -v python)
else if type -q python3
    set PYTHON_BIN (command -v python3)
end

if test -z "$PYTHON_BIN"
    echo "[ERROR] No Python interpreter found."
    exit 1
end

echo "[PYTHON] Using: $PYTHON_BIN"

# Fail early with a useful message instead of repeatedly launching a helper
# with the wrong system Python.
$PYTHON_BIN -c 'import cv2, numpy' >/dev/null 2>&1
if test $status -ne 0
    echo "[ERROR] OpenCV is not installed in the Python environment above."
    echo "[ERROR] Install scanner dependencies with:"
    echo "        $PYTHON_BIN -m pip install -r scanner_requirements.txt"
    exit 1
end

set SCRCPY_PID ""
set SCANNER_STATUS 0

if not set -q MTG_SCRCPY_CAMERA
    set MTG_SCRCPY_CAMERA "1"
end
if not set -q MTG_V4L2_DEVICE
    set MTG_V4L2_DEVICE "9"
end
set V4L2_DEVICE "/dev/video$MTG_V4L2_DEVICE"

function cleanup
    if test -n "$SCRCPY_PID"
        if kill -0 $SCRCPY_PID 2>/dev/null
            kill $SCRCPY_PID 2>/dev/null
        end
        wait $SCRCPY_PID 2>/dev/null
    end
end
trap cleanup EXIT INT TERM

if test "$MTG_SCRCPY_CAMERA" = "1"
    if not type -q scrcpy
        echo "[ERROR] scrcpy is not installed or not on PATH."
        exit 1
    end

    if not test -e "$V4L2_DEVICE"
        echo "[PHONE] $V4L2_DEVICE does not exist; attempting to load v4l2loopback..."
        if not type -q sudo
            echo "[ERROR] sudo is required to load v4l2loopback."
            exit 1
        end
        sudo modprobe v4l2loopback video_nr=$MTG_V4L2_DEVICE card_label="MTG Scanner Phone" exclusive_caps=1
        if test $status -ne 0
            echo "[ERROR] Could not load v4l2loopback."
            echo "        Run ./setup_phone_camera.fish first."
            exit 1
        end
        sleep 1
    end

    if not test -e "$V4L2_DEVICE"
        echo "[ERROR] $V4L2_DEVICE still does not exist."
        exit 1
    end

    set camera_facing "back"
    if set -q MTG_SCRCPY_CAMERA_FACING
        set camera_facing "$MTG_SCRCPY_CAMERA_FACING"
    end
    set capture_orientation "@90"
    if set -q MTG_SCRCPY_CAPTURE_ORIENTATION
        set capture_orientation "$MTG_SCRCPY_CAPTURE_ORIENTATION"
    end
    set camera_zoom "1.5"
    if set -q MTG_SCRCPY_CAMERA_ZOOM
        set camera_zoom "$MTG_SCRCPY_CAMERA_ZOOM"
    end

    echo "[PHONE] Starting Android camera through scrcpy -> $V4L2_DEVICE"
    echo "[PHONE] Camera facing: $camera_facing"
    echo "[PHONE] Capture orientation: $capture_orientation"
    echo "[PHONE] Camera zoom: $camera_zoom"

    command scrcpy \
        --video-source=camera \
        --camera-facing="$camera_facing" \
        --capture-orientation="$capture_orientation" \
        --camera-zoom="$camera_zoom" \
        --v4l2-sink="$V4L2_DEVICE" \
        --no-video-playback \
        --no-audio >"$SCRIPT_DIR/scrcpy_camera.log" 2>&1 &
    set SCRCPY_PID $last_pid

    # With exclusive_caps=1 the loopback may initially advertise OUTPUT only.
    # Once scrcpy becomes the producer it should switch to CAPTURE.
    set READY 0
    for i in (seq 1 24)
        sleep 0.5
        if $PYTHON_BIN "$SCRIPT_DIR/camera_probe.py" "$V4L2_DEVICE"
            set READY 1
            break
        end
        if not kill -0 $SCRCPY_PID 2>/dev/null
            echo "[ERROR] scrcpy exited before the phone camera became available."
            echo "----- scrcpy_camera.log -----"
            cat "$SCRIPT_DIR/scrcpy_camera.log"
            echo "-----------------------------"
            exit 1
        end
    end

    if test $READY -ne 1
        echo "[ERROR] $V4L2_DEVICE exists, but no video frame became available."
        echo ""
        echo "Diagnostics:"
        if type -q v4l2-ctl
            echo "  v4l2-ctl --all -d $V4L2_DEVICE"
        end
        echo "  ls -l $V4L2_DEVICE"
        echo "  lsmod | grep v4l2loopback"
        echo "  cat $SCRIPT_DIR/scrcpy_camera.log"
        exit 1
    end
    echo "[PHONE] Camera stream ready."
end

$PYTHON_BIN "$SCRIPT_DIR/Scanner.py"
set SCANNER_STATUS $status
exit $SCANNER_STATUS
