# ⚠️ This is vibe coded AF. Use at your own risk.

This client was built with heavy AI-assisted (“vibe-coded”) development. It may contain bugs, misread cards, mishandle camera devices, or fail on some hardware/software combinations. **Use it at your own risk.** Verify scan results before changing your inventory, and back up your server database before bulk operations.

# MTG Scanner Client v39

A standalone desktop scanner client for the [CardScanner MTG inventory server](https://github.com/Woytsekj/CardScanner). It captures card images, detects card boundaries, uses OCR/image matching to identify cards, and communicates with the server through its dedicated scanner API.

The client is kept separate from the web application so it can be installed and updated independently.

## What is included

- `Scanner.py` — desktop scanning interface.
- `scanner_engine.py` — card detection, image processing, OCR, and matching logic.
- `start_scanner.fish` — desktop launcher; uses the desktop's default webcam by default. The legacy phone-to-PC stream is optional.
- `setup_phone_camera.fish` — helper for the optional legacy phone camera / loopback device.
- `mobile-app/` — installable Android-friendly PWA; uses the phone's own camera, zoom controls, continuous capture workflow, and OCR-assisted set/collector-aware printing search. It does not require a USB/scrcpy connection and is not a native APK.
- `camera_probe.py` — checks whether a video device is delivering frames.
- `check_ocr_gpu.fish` — checks whether the current PyTorch environment can use CUDA/ROCm.
- `scanner.env.fish.example` — example local configuration.
- `scanner_requirements.txt` — Python dependencies.
- `README_GPU.md` — additional notes for OCR GPU acceleration.

## Compatibility notes

The supplied build is aimed at Linux systems using the Fish shell. The desktop launcher expects `fish` and uses camera source `0` by default when no source is configured. The legacy phone-camera bridge is opt-in (`MTG_SCRCPY_CAMERA=1`) and requires `scrcpy` plus a working `v4l2loopback` device. Android users can instead install the mobile PWA from the HTTPS CardScanner site, with no phone-to-PC camera connection.

The desktop scanner is based on the v36 detector path, with a conservative rotated-rectangle fallback for rounded card corners when normal quadrilateral detection fails. The mobile PWA intentionally uses a lighter camera + OCR + exact-printing-confirmation workflow rather than running the desktop image-hash matching engine in a phone browser. Results will depend on lighting, focus, card sleeves, camera angle, and the card itself. No recognition result should be treated as guaranteed.

## Requirements

- Python 3.10+ recommended; dependency compatibility depends on the installed OS/Python version.
- Fish shell.
- Packages listed in `scanner_requirements.txt` (includes OpenCV, ImageHash, Pillow, EasyOCR, NumPy, TheFuzz, Requests, and PySide6).
- Network access to the MTG Scanner server.
- A configured scanner API key.
- For legacy phone-camera bridge mode only: Android phone, `scrcpy`, `v4l2loopback`, and a usable V4L2 device.
- For mobile PWA: deploy the full repository behind HTTPS and Cloudflare Access. The current Flask app serves the PWA and its authenticated routes; setup is documented in [`SERVER_INTEGRATION.md`](SERVER_INTEGRATION.md).
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
set -gx MTG_VIDEO_SOURCE "0"
set -gx MTG_SCRCPY_CAMERA "0"
```

Use the actual server URL that is reachable from the scanner computer. For LAN-only access, use your server's LAN address. For remote access, use your secured endpoint and configure any required Cloudflare Access service-token values supported by the client.

**Important:** `MTG_SCANNER_API_KEY` must match `SCANNER_API_KEY` configured on the server. Keep `scanner.env.fish` private; it contains a credential. Never commit it. The example file should contain placeholders only.

The example also includes optional camera settings and local fallback owner names. The server must be running and reachable before scanning can sync. The desktop UI requests people/account mappings and storage locations from `/api/scanner/options`; the updated main Flask app provides that endpoint and persists location/vending fields.

## Camera setup

### Android camera (recommended: mobile PWA)

1. Deploy the full repository and put the hostname/path behind a Cloudflare Access policy limited to you and invited friends.
2. On the phone, open `https://YOUR-CARDS-HOST/scanner/mobile/`, sign in through Cloudflare Access and CardScanner, then use the browser menu's **Install app** or **Add to Home screen** option.
3. Allow camera access, use the zoom slider/buttons to frame the card, and leave Continuous scan enabled to keep the camera running between cards. Capture prompts OCR and searches printings; verify the set code and collector number before saving. Queries can include a collector number and set code (for example `Forest 304 C21`) or use the optional Set code filter. No cable, `scrcpy`, loopback device, or PC camera stream is needed.
4. Review the OCR name, choose the exact Scryfall printing, select the account person/location, set normal/foil and vending, and save.

The PWA uses session-authenticated routes served by the main Flask application and an injected per-session CSRF token. It intentionally does not embed the scanner API key or a Cloudflare service-token secret.

### Optional legacy phone-to-PC camera bridge

If you still want the desktop PySide6 scanner to use the phone as its camera, explicitly set `MTG_SCRCPY_CAMERA=1`, configure `MTG_VIDEO_SOURCE` and `MTG_V4L2_DEVICE` (example `/dev/video9` / `9`), and run `./setup_phone_camera.fish` if needed. The launcher attempts to start `scrcpy` and checks that frames are available. Kernel module setup may require administrator privileges and matching kernel headers.

### Desktop camera sources

By default, `start_scanner.fish` uses camera index `0` when `MTG_VIDEO_SOURCE` is not set. Set `MTG_VIDEO_SOURCE` to a device path such as `/dev/video2` if needed and ensure the device is readable by your user.

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
- **Camera device missing:** for a local camera, check `ls -l /dev/video*` and `v4l2-ctl --list-devices`; for optional scrcpy mode also check `lsmod | grep v4l2loopback` and `scrcpy_camera.log`.
- **Mobile page says API unavailable:** confirm the updated Flask container was rebuilt with `scanner-client/mobile-app/`, then check `/api/scanner/mobile/options` while signed in and verify Cloudflare Access protects the hostname or both the PWA path and `/api/scanner/mobile/*`.
- **Mobile title OCR is wrong:** the OCR output is only a suggestion. Correct the name manually and confirm the exact set and collector number before saving.
- **Phone stream starts but no frames arrive:** check the `scrcpy` log, camera permissions, V4L2 device number, and `v4l2loopback` compatibility with the running kernel.
- **OCR is slow:** verify which Python interpreter the launcher reports, whether the correct dependencies are installed in that environment, and whether GPU acceleration is actually available.
- **Poor card recognition:** improve focus and avoid glare if possible; keep the card flat and fully inside the camera view. Review the result before saving it.
- **Missing Python module:** activate the intended virtual environment and run `python -m pip install -r scanner_requirements.txt` using that environment's Python.

## Safety and data handling

- The scanner sends requests to the configured server. Only point it at a server you control or are authorized to use.
- Treat the desktop scanner API key and any Cloudflare Access service-token credentials as secrets. Never place them in the PWA, browser storage, or a shared APK.
- When server validation rejects a scan that was already queued while offline, the payload is preserved in `scan_queue_rejected.jsonl` for review rather than retrying forever or blocking later queued scans.
- Verify recognized card, set, finish, owner, and quantity before accepting a scan or batch operation.
- Back up the server's inventory database before testing new builds or doing large imports.
- Avoid posting logs/configuration publicly if they contain credentials, private server addresses, or personal data.

## Relationship to the main project

The main Flask server and Docker setup are in the repository root. The server owns the inventory database, accounts, price history, deck data, and web interface. The client is a separate application that sends scan-related requests to the server's dedicated API.

For server setup, deployment, security, database backups, and web features, see the repository's top-level `README.md`.

### Account, location, and vending behavior

The desktop scan confirmation now includes an account-aware person selector, an independent storage-location selector, and a **Mark for vending** checkbox. When the server offers `/api/scanner/options`, the client displays the account associated with each person and preserves person/account IDs; it falls back to the legacy `/api/people` endpoint when needed. Vending is not inferred from the storage location, including `free`.

The updated server validates person/account mappings, persists physical location separately from vending status, and stores scans under the canonical active person. Non-admin mobile sessions can scan only into their linked person’s collection. Client-side controls are not a security boundary. Read `SERVER_INTEGRATION.md` before deploying these features to your friends.

## Version notes

**v39 full-repository update:** Adds account-aware person options, location and vending fields, stable scan IDs for retry safety, and an Android-friendly installable PWA at `/scanner/mobile/`. The main Flask server supplies the PWA and routes, validates session-scoped person access and CSRF, and persists vending independently from location. The PWA does not contain shared API keys or Cloudflare service-token secrets.

**v39.1 mobile scanner update:** Adds controllable hardware zoom where supported and a digital-crop fallback, continuous capture that keeps the camera active between cards, automatic OCR-assisted printing search in continuous mode, and desktop-style queries that support optional collector numbers and set codes.

This is an unofficial community project and is not affiliated with or endorsed by Wizards of the Coast. Magic: The Gathering is a trademark of Wizards of the Coast LLC.


### Mobile scanner v39.2

Mobile printing discovery uses the server local `data/mtg_art_hashes.db` catalog and local TCGCSV price snapshot; it does not query the Scryfall search API. Candidate card thumbnails are still loaded from Scryfall image CDN URLs. Ensure the local art/catalog DB is installed and current on the server.


### Mobile scanner v39.3

Title OCR now detects the card boundary first and crops the title bar relative to the card, rather than assuming the card fills the viewport. Low-confidence recognition tries a binarized alternate crop; if card-boundary detection fails, it samples several vertically distributed strips. The page reports OCR confidence and detection score to support local camera tuning.


### Mobile batch-scanning workflow (v39.8)

When a printing is selected, the mobile scanner shows a fixed **Save & next** action above the Android navigation area. It stays available while you scroll to review results or inventory options. After a successful save in continuous mode, the page clears the selected printing, keeps the camera running, and scrolls the capture preview back into view. Person, location, and finish choices are retained for batch work; quantity returns to 1 and the vending checkbox resets after each save.


### Batch scanning vending mode (v39.8.1)

The vending checkbox is a session/batch setting and is preserved after each successful card save. The fixed mobile save dock contains a matching “Mark all cards in this batch for vending” checkbox, so the mode can be changed without scrolling. Turn it off for cards being allocated to decks; turn it on for a bulk/pulled-card batch intended for vending. Quantity still resets to 1 after each successful save. The setting is not persisted across a browser reload, to avoid accidentally carrying a vending batch into a later deck-scanning session.


### Mobile v39.9 — diagonal card handling

The mobile OCR estimates in-plane card skew from a small, downscaled edge-orientation histogram. When a diagonal card is detected, it rotates the image onto an expanded canvas before card-boundary detection and OCR. The correction is accepted only when the card-boundary score supports it; straight cards keep the existing fast path. The prediction banner reports the applied correction angle for local tuning.


### Mobile scanner v39.11: OCR sanity check

Footer set/collector OCR is corroborated against an independently read title before quick-confirm is offered. A conflicting footer is ignored; unreadable titles keep candidate confirmation manual. See `CHANGES_scanner-mobile_v39.11.md`.
