# ⚠️ This is vibe coded AF. Use at your own risk.

This client was built with heavy AI-assisted (“vibe-coded”) development. It may contain bugs, misread cards, mishandle camera devices, or fail on some hardware/software combinations. **Use it at your own risk.** Verify scan results before changing your inventory, and back up your server database before bulk operations.

# MTG Scanner Client v38

A standalone desktop scanner client for the [CardScanner MTG inventory server](https://github.com/Woytsekj/CardScanner). It captures card images, detects card boundaries, uses OCR/image matching to identify cards, and communicates with the server through its dedicated scanner API.

The client is kept separate from the web application so it can be installed and updated independently.

## What is included

- `Scanner.py` — desktop scanning interface.
- `scanner_engine.py` — card detection, image processing, OCR, and matching logic.
- `start_scanner.fish` — launcher; optionally starts an Android phone camera stream through `scrcpy`.
- `setup_phone_camera.fish` — helper for setting up the phone camera / loopback device.
- `camera_probe.py` — checks whether a video device is delivering frames.
- `check_ocr_gpu.fish` — checks whether the current PyTorch environment can use CUDA/ROCm.
- `scanner.env.fish.example` — example local configuration.
- `scanner_requirements.txt` — Python dependencies.
- `README_GPU.md` — additional notes for OCR GPU acceleration.

## Compatibility notes

The supplied v38 build is aimed at Linux systems using the Fish shell. The launcher expects `fish`; phone-camera mode also expects `scrcpy` and a working `v4l2loopback` video device. Other operating systems and camera arrangements may require changes.

The scanner was described as based on the v36 detector path, with a conservative rotated-rectangle fallback for rounded card corners when normal quadrilateral detection fails. Results will depend on lighting, focus, card sleeves, camera angle, and the card itself. No recognition result should be treated as guaranteed.

## Requirements

- Python 3.10+ recommended; dependency compatibility depends on the installed OS/Python version.
- Fish shell.
- Packages listed in `scanner_requirements.txt` (includes OpenCV, ImageHash, Pillow, EasyOCR, NumPy, TheFuzz, Requests, and PySide6).
- Network access to the MTG Scanner server.
- A configured scanner API key.
- For phone-camera mode: Android phone, `scrcpy`, `v4l2loopback`, and a usable V4L2 device.
- Optional GPU acceleration: a compatible PyTorch build and matching GPU runtime. CPU mode may be used if GPU acceleration is unavailable.

## Installation (Linux / Fish)

From the repository root:

```bash
cd scanner-client
```

Install system tools using your distribution's package manager. Package names and kernel module setup differ by distribution. For phone-camera mode, install `scrcpy`, V4L2 utilities, and a `v4l2loopback` module compatible with your running kernel.

Create a virtual environment and install Python dependencies:

```bash
python -m venv .venv
source .venv/bin/activate.fish
python -m pip install --upgrade pip
python -m pip install -r scanner_requirements.txt
```

If your system's Fish activation script is unavailable, activate the environment using the shell-specific method for your environment, or run `.venv/bin/python -m pip ...` directly.

## Configure the server connection

Create a local config from the example:

```bash
cp scanner.env.fish.example scanner.env.fish
```

Edit `scanner.env.fish` and set at least:

```fish
set -gx MTG_SERVER_URL "http://YOUR-SERVER-IP:5000"
set -gx MTG_SCANNER_API_KEY 'YOUR_LONG_RANDOM_SCANNER_API_KEY'
set -gx MTG_SCANNER_DEVICE "your-computer-name"
set -gx MTG_OCR_GPU "auto"
```

Use the actual server URL that is reachable from the scanner computer. For LAN-only access, use your server's LAN address. For remote access, use your secured endpoint and configure any required Cloudflare Access service-token values supported by the client.

**Important:** `MTG_SCANNER_API_KEY` must match `SCANNER_API_KEY` configured on the server. Keep `scanner.env.fish` private; it contains a credential. Never commit it. The example file should contain placeholders only.

The example also includes optional camera settings and local fallback owner names. Adjust those to match your environment. The server must be running and reachable before scanning can sync.

## Camera setup

### Android phone camera

1. Connect/configure the phone and computer for `scrcpy` as required by your setup.
2. Run the helper script if the loopback camera device is not ready:

   ```fish
   ./setup_phone_camera.fish
   ```

3. Confirm that the configured V4L2 device (default example: `/dev/video9`) exists and provides frames.
4. Configure `MTG_VIDEO_SOURCE`, `MTG_V4L2_DEVICE`, `MTG_SCRCPY_CAMERA`, camera-facing, orientation, and zoom values in `scanner.env.fish` as needed.

The launcher attempts to load `v4l2loopback` if the expected device is missing, then starts `scrcpy` and checks that frames are available. Kernel module installation/loading may require administrator privileges and matching kernel headers.

### Other camera sources

Set the client configuration to the correct video source for your setup and ensure the device is readable by your user. The exact source configuration depends on the scanner code and your camera driver.

## Launch

From `scanner-client/`, run:

```fish
./start_scanner.fish
```

The launcher sources `scanner.env.fish` if present, chooses the available project/active Python interpreter, checks for core dependencies, and (when phone-camera mode is enabled) starts the camera stream before launching `Scanner.py`.

If the script is not executable:

```bash
chmod +x start_scanner.fish setup_phone_camera.fish check_ocr_gpu.fish
```

You can also run the GUI directly with the Python interpreter from the virtual environment:

```bash
.venv/bin/python Scanner.py
```

Direct launch does not perform the same phone-camera startup/cleanup steps as the launcher.

## OCR GPU acceleration

GPU acceleration is optional. The client attempts to detect supported CUDA or AMD ROCm/PyTorch availability, but the correct setup depends on the GPU, OS, Python version, and PyTorch build. Do not install random GPU wheels or assume a GPU is being used just because the computer has one.

Run:

```fish
./check_ocr_gpu.fish
```

Review [`README_GPU.md`](README_GPU.md) for additional guidance. If the GPU runtime is not working, configure `MTG_OCR_GPU` to force CPU mode if supported by the current client, then verify performance and results.

## Troubleshooting

- **Server connection fails:** verify `MTG_SERVER_URL`, server reachability, Docker service status, and that the client API key matches the server's scanner API key.
- **Unauthorized API requests:** check the key carefully and confirm any required Cloudflare Access service-token configuration.
- **Camera device missing:** check `ls -l /dev/video*`, `v4l2-ctl --list-devices`, `lsmod | grep v4l2loopback`, and `scrcpy_camera.log`.
- **Phone stream starts but no frames arrive:** check the `scrcpy` log, camera permissions, V4L2 device number, and `v4l2loopback` compatibility with the running kernel.
- **OCR is slow:** verify which Python interpreter the launcher reports, whether the correct dependencies are installed in that environment, and whether GPU acceleration is actually available.
- **Poor card recognition:** improve focus and avoid glare if possible; keep the card flat and fully inside the camera view. Review the result before saving it.
- **Missing Python module:** activate the intended virtual environment and run `python -m pip install -r scanner_requirements.txt` using that environment's Python.

## Safety and data handling

- The scanner sends requests to the configured server. Only point it at a server you control or are authorized to use.
- Treat the scanner API key and any Cloudflare Access service-token credentials as secrets.
- Verify recognized card, set, finish, owner, and quantity before accepting a scan or batch operation.
- Back up the server's inventory database before testing new builds or doing large imports.
- Avoid posting logs/configuration publicly if they contain credentials, private server addresses, or personal data.

## Relationship to the main project

The main Flask server and Docker setup are in the repository root. The server owns the inventory database, accounts, price history, deck data, and web interface. The client is a separate application that sends scan-related requests to the server's dedicated API.

For server setup, deployment, security, database backups, and web features, see the repository's top-level `README.md`.

## Version notes

**v38:** Based on the v36 detector path for the physical foam-tray setup. Adds a conservative rounded-corner rotated-rectangle fallback when normal quadrilateral detection fails, plus a guard for missing card crops. The project retains its existing OCR, ROCm/CUDA detection, Cloudflare-related configuration, manual-add, and keyboard workflows.

This is an unofficial community project and is not affiliated with or endorsed by Wizards of the Coast. Magic: The Gathering is a trademark of Wizards of the Coast LLC.
