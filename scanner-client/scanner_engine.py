import cv2
import imagehash
from PIL import Image
import sqlite3
import json
import os
import easyocr
import re
import numpy as np
import requests

try:
    import torch
except Exception:
    torch = None
from datetime import datetime, timezone
from io import BytesIO
from thefuzz import process, fuzz

# ----------------- Configuration -----------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(SCRIPT_DIR, "mtg_art_hashes.db")
INV_DB_NAME = os.path.join(SCRIPT_DIR, "mtg_inventory.db")
PRICE_FILE = os.path.join(SCRIPT_DIR, "tcg_prices.json")
VIDEO_SOURCE_RAW = os.environ.get("MTG_VIDEO_SOURCE", os.environ.get("VIDEO_SOURCE", "/dev/video9"))
VIDEO_SOURCE = VIDEO_SOURCE_RAW.strip()
if VIDEO_SOURCE.isdigit():
    VIDEO_SOURCE = int(VIDEO_SOURCE)
SERVER_URL = os.environ.get("MTG_SERVER_URL", "").rstrip("/")
SCANNER_API_KEY = os.environ.get("MTG_SCANNER_API_KEY", "")
CF_ACCESS_CLIENT_ID = os.environ.get("MTG_CF_ACCESS_CLIENT_ID", "")
CF_ACCESS_CLIENT_SECRET = os.environ.get("MTG_CF_ACCESS_CLIENT_SECRET", "")
SCANNER_DEVICE = os.environ.get("MTG_SCANNER_DEVICE", "main-computer")
QUEUE_FILE = os.path.join(SCRIPT_DIR, os.environ.get("MTG_SCAN_QUEUE", "scan_queue.jsonl"))
REQUEST_TIMEOUT = float(os.environ.get("MTG_SCAN_TIMEOUT", "8"))
OCR_GPU_SETTING = os.environ.get("MTG_OCR_GPU", "auto").strip().lower()
if OCR_GPU_SETTING in {"0", "false", "no", "off", "cpu"}:
    OCR_GPU = False
elif OCR_GPU_SETTING in {"1", "true", "yes", "on", "gpu"}:
    OCR_GPU = True
else:
    # Auto-detect CUDA or AMD ROCm. PyTorch exposes ROCm through the
    # torch.cuda API as well, so this works for ROCm-enabled builds.
    OCR_GPU = bool(torch is not None and torch.cuda.is_available())

if torch is not None:
    try:
        if getattr(torch.version, "hip", None):
            print(f"[OCR] PyTorch ROCm detected: {torch.version.hip}; GPU available={torch.cuda.is_available()}")
        else:
            print(f"[OCR] PyTorch CUDA available={torch.cuda.is_available()}")
    except Exception as exc:
        print(f"[OCR] Could not query PyTorch GPU status: {exc}")
else:
    print("[OCR] PyTorch is unavailable; EasyOCR will use CPU.")
SCRYFALL_USER_AGENT = os.environ.get("MTG_SCRYFALL_USER_AGENT", f"MTGScannerClient/2.0 ({SCANNER_DEVICE})")
SCRYFALL_CACHE_FILE = os.path.join(SCRIPT_DIR, "scryfall_print_cache.json")
SCRYFALL_LOOKUP_ENABLED = os.environ.get("MTG_SCRYFALL_LOOKUP_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
SCRYFALL_TIMEOUT = float(os.environ.get("MTG_SCRYFALL_TIMEOUT", "8"))

cards_by_name = {}
all_card_names = []
all_hashes_flat = []
tcg_prices = {}

print(f"Initializing EasyOCR reader (GPU={OCR_GPU})...")
try:
    reader = easyocr.Reader(["en"], gpu=OCR_GPU)
except Exception as exc:
    print(f"[!] EasyOCR GPU initialization failed ({exc}); falling back to CPU.")
    OCR_GPU = False
    reader = easyocr.Reader(["en"], gpu=False)


def open_camera():
    """Open the configured camera via V4L2. Paths like /dev/video9 avoid index enumeration."""
    return cv2.VideoCapture(VIDEO_SOURCE, cv2.CAP_V4L2)


def describe_camera_source():
    return str(VIDEO_SOURCE)


def normalized_finish(is_foil_mode: bool) -> str:
    return "foil" if is_foil_mode else "normal"


# ----------------- CV Math Helpers -----------------
def order_points(pts):
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def _normalize_collector_number(value: str) -> str:
    """Normalize collector-number text for comparison.

    Examples:
      219       -> 219
      0219      -> 219
      219/281   -> 219
      AFR-219   -> 219
      A-219     -> A-219  (kept distinct from the normal printed 219)
    """
    value = str(value or "").strip().lower()
    if not value:
        return ""
    value = value.replace(" ", "")
    if "/" in value:
        value = value.split("/", 1)[0]
    # Scryfall list exports can store SET-123; strip a three-letter set prefix.
    m = re.fullmatch(r"[a-z]{3}-(\d{1,4}[a-z]?)", value)
    if m:
        value = m.group(1)
    if re.fullmatch(r"\d+[a-z]?", value):
        if value[0].isdigit():
            prefix = re.match(r"0+", value)
            if prefix:
                value = value[len(prefix.group(0)):] or "0"
    return value


def parse_collector_number(words, raw_text=""):
    """Extract the *printed* collector number, including forms like 219/281.

    OCR frequently separates the slash and denominator into three tokens
    (``219``, ``/``, ``281``), so we inspect both the original OCR text and
    the token list.  The numerator is the collector number; the denominator
    is the set's total card count and is intentionally ignored.
    """
    text = str(raw_text or "")
    compact = re.sub(r"[^0-9a-z/ -]", " ", text.lower())

    # Most reliable: OCR kept the fraction together or with spaces around '/'.
    m = re.search(r"\b(\d{1,4})\s*/\s*\d{1,4}\b", compact)
    if m:
        return _normalize_collector_number(m.group(1))

    # Next: OCR separated slash into a token: 219 / 281.
    toks = [re.sub(r"[^0-9a-z/]", "", str(w).lower()) for w in words]
    toks = [t for t in toks if t]
    for i in range(len(toks) - 2):
        if re.fullmatch(r"\d{1,4}", toks[i]) and toks[i+1] == "/" and re.fullmatch(r"\d{1,4}", toks[i+2]):
            return _normalize_collector_number(toks[i])

    # Finally accept a standalone number. Prefer 3-4 digit values because
    # those are much more likely to be a collector number than mana/text OCR.
    candidates = []
    for token in toks:
        if re.fullmatch(r"\d{1,4}[a-z]?", token):
            candidates.append(token)
    if not candidates:
        return None
    candidates.sort(key=lambda x: (len(re.sub(r"[^0-9]", "", x)) >= 3,
                                   len(x)), reverse=True)
    return _normalize_collector_number(candidates[0])


def _collector_match_rank(stored: str, target: str) -> int:
    """Return a preference score for matching a printed collector number.

    Exact numeric collector numbers win. Prefixed forms such as A-219 are a
    lower-priority match because a camera OCR of a normal printed card usually
    means the plain printed number 219.
    """
    stored = str(stored or "").strip().lower()
    target = _normalize_collector_number(target)
    if not stored or not target:
        return -1
    compact = stored.replace(" ", "")
    if compact == target:
        return 100
    # SET-219 is equivalent when the OCR separately identified that SET.
    m = re.fullmatch(r"[a-z]{3}-(\d{1,4}[a-z]?)", compact)
    if m and _normalize_collector_number(m.group(1)) == target:
        return 90
    # A-219 / similar promotional prefixes are intentionally lower priority.
    m = re.fullmatch(r"[a-z]+-(\\d{1,4}[a-z]?)", compact)
    if m and _normalize_collector_number(m.group(1)) == target:
        return 60
    return -1


# ----------------- Local Inventory Fallback -----------------
def init_inventory_db():
    conn = sqlite3.connect(INV_DB_NAME)
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS inventory
           (id INTEGER PRIMARY KEY AUTOINCREMENT,
            scryfall_id TEXT,
            name TEXT,
            set_code TEXT,
            rarity TEXT,
            price TEXT,
            owner TEXT DEFAULT 'Me',
            quantity INTEGER DEFAULT 1,
            scan_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            finish TEXT DEFAULT 'normal',
            collector_number TEXT,
            tcgplayer_id TEXT)"""
    )
    for ddl in (
        "ALTER TABLE inventory ADD COLUMN quantity INTEGER DEFAULT 1",
        "ALTER TABLE inventory ADD COLUMN owner TEXT DEFAULT 'Me'",
        "ALTER TABLE inventory ADD COLUMN finish TEXT DEFAULT 'normal'",
        "ALTER TABLE inventory ADD COLUMN collector_number TEXT",
        "ALTER TABLE inventory ADD COLUMN tcgplayer_id TEXT",
    ):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass
    c.execute("UPDATE inventory SET finish='normal' WHERE finish IS NULL OR TRIM(finish)=''")
    conn.commit()
    conn.close()


def save_to_local_inventory(match, price, owner, finish, quantity=1):
    init_inventory_db()
    conn = sqlite3.connect(INV_DB_NAME)
    c = conn.cursor()
    c.execute(
        """SELECT id, quantity FROM inventory
           WHERE scryfall_id = ? AND owner = ? AND IFNULL(NULLIF(finish,''),'normal') = ?
           ORDER BY id LIMIT 1""",
        (match["id"], owner, finish),
    )
    existing = c.fetchone()
    if existing:
        quantity = max(1, int(quantity))
        new_qty = existing[1] + quantity
        c.execute(
            "UPDATE inventory SET quantity=?, scan_time=CURRENT_TIMESTAMP, price=? WHERE id=?",
            (new_qty, str(price), existing[0]),
        )
        print(f"[UPDATED LOCAL] {match['name']} {finish} qty increased to {new_qty} for {owner}.")
    else:
        c.execute(
            """INSERT INTO inventory
               (scryfall_id, name, set_code, rarity, price, owner, quantity, finish, collector_number, tcgplayer_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                match["id"], match["name"], match["set"], match["rarity"], str(price), owner, max(1, int(quantity)), finish,
                match.get("collector_number"), match.get("tcg_id"),
            ),
        )
        new_qty = 1
        print(f"[SAVED LOCAL] {match['name']} [{match['set']}] {finish} for {owner}.")
    conn.commit()
    conn.close()
    return {"success": True, "quantity": new_qty, "mode": "local"}


# ----------------- Remote Scanner API -----------------
def queue_scan(payload):
    with open(QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, separators=(",", ":")) + "\n")


def api_headers():
    """Build headers for scanner API requests, with optional Cloudflare Access service-token auth."""
    headers = {
        "X-Scanner-API-Key": SCANNER_API_KEY,
        "User-Agent": f"MTGScanner/{SCANNER_DEVICE}",
    }
    if CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
        headers["CF-Access-Client-Id"] = CF_ACCESS_CLIENT_ID
        headers["CF-Access-Client-Secret"] = CF_ACCESS_CLIENT_SECRET
    return headers


def get_remote_prices(tcg_id):
    """Get current prices from the central server, with useful diagnostics."""
    if not SERVER_URL or not SCANNER_API_KEY or not tcg_id:
        return None
    try:
        url = f"{SERVER_URL}/api/price/{tcg_id}"
        response = requests.get(
            url,
            headers=api_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("success"):
            prices = {"normal": data.get("normal"), "foil": data.get("foil")}
            if prices["normal"] is not None or prices["foil"] is not None:
                print(f"[REMOTE PRICE] TCGplayer {tcg_id}: normal={prices['normal']} foil={prices['foil']} source={data.get('source_updated','unknown')}")
                return prices
            print(f"[REMOTE PRICE] Server has no price for TCGplayer {tcg_id} (snapshot={data.get('snapshot_exists')}, source={data.get('source_updated')}).")
            return None
        print(f"[REMOTE PRICE] Server returned an error for TCGplayer {tcg_id}: {data}")
    except requests.RequestException as exc:
        print(f"[REMOTE PRICE] Request failed for TCGplayer {tcg_id}: {exc}")
    except Exception as exc:
        print(f"[REMOTE PRICE] Unexpected error for TCGplayer {tcg_id}: {exc}")
    return None


def _request_scryfall_card(sid):
    headers = {
        "User-Agent": os.environ.get(
            "MTG_SCRYFALL_USER_AGENT",
            f"MTGScannerClient/1.0 ({SCANNER_DEVICE})",
        ),
        "Accept": "application/json",
    }
    response = requests.get(
        f"https://api.scryfall.com/cards/{sid}",
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def get_remote_card_pricing(match):
    """Resolve a missing TCGplayer mapping and retrieve price data.

    Preferred path is the central server's card endpoint. For compatibility with
    older servers that only expose /api/price/<tcg_id>, a 404 falls back to a
    single Scryfall lookup on the scanner, followed by the legacy price endpoint.
    """
    if not SERVER_URL or not SCANNER_API_KEY or not match or not match.get("id"):
        return None
    sid = str(match.get("id")).strip()
    if not sid:
        return None
    headers = api_headers()

    try:
        response = requests.get(
            f"{SERVER_URL}/api/price/card/{sid}",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 404:
            response.raise_for_status()
            data = response.json()
            if not data.get("success"):
                print(f"[REMOTE CARD] Server returned an error for {sid}: {data}")
                return None
            tcg_id = data.get("tcgplayer_id")
            if tcg_id:
                match["tcg_id"] = tcg_id
            match["server_finishes"] = data.get("finishes") or []
            print(
                f"[REMOTE CARD] {match.get('name')} · {match.get('set')} #{match.get('collector_number','?')} "
                f"· resolved TCGplayer ID={tcg_id or 'MISSING'} · normal={data.get('normal')} foil={data.get('foil')} "
                f"· finishes={data.get('finishes') or []}"
            )
            return {
                "normal": data.get("normal"),
                "foil": data.get("foil"),
                "finishes": data.get("finishes") or [],
                "tcgplayer_id": tcg_id,
                "source_updated": data.get("source_updated"),
            }

        print(f"[REMOTE CARD] Server does not support /api/price/card/{sid}; using compatibility lookup.")
        sf = _request_scryfall_card(sid)
        tcg_id = sf.get("tcgplayer_id")
        finishes = sf.get("finishes") if isinstance(sf.get("finishes"), list) else []
        match["server_finishes"] = finishes
        if not tcg_id:
            print(f"[REMOTE CARD] Scryfall has no TCGplayer ID for {sid}.")
            return {"normal": None, "foil": None, "finishes": finishes, "tcgplayer_id": None, "source_updated": None}

        tcg_id = str(tcg_id)
        match["tcg_id"] = tcg_id
        price_response = requests.get(
            f"{SERVER_URL}/api/price/{tcg_id}",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        price_response.raise_for_status()
        price_data = price_response.json()
        if not price_data.get("success"):
            print(f"[REMOTE PRICE] Legacy server endpoint rejected TCGplayer {tcg_id}: {price_data}")
            return None
        print(
            f"[REMOTE CARD] Compatibility lookup resolved {match.get('name')} · "
            f"TCGplayer ID={tcg_id} · normal={price_data.get('normal')} foil={price_data.get('foil')} "
            f"· finishes={finishes}"
        )
        return {
            "normal": price_data.get("normal"),
            "foil": price_data.get("foil"),
            "finishes": finishes,
            "tcgplayer_id": tcg_id,
            "source_updated": price_data.get("source_updated"),
        }
    except requests.RequestException as exc:
        print(f"[REMOTE CARD] Request failed for {sid}: {exc}")
    except Exception as exc:
        print(f"[REMOTE CARD] Unexpected error for {sid}: {exc}")
    return None


def post_remote_scan(payload):
    if not SERVER_URL or not SCANNER_API_KEY:
        raise RuntimeError("Remote server URL/API key not configured")
    response = requests.post(
        f"{SERVER_URL}/api/scan",
        json=payload,
        headers=api_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(data.get("error", "Server rejected scan"))
    return data


def flush_pending_queue():
    if not SERVER_URL or not SCANNER_API_KEY or not os.path.exists(QUEUE_FILE):
        return 0
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError:
        return 0
    if not lines:
        return 0

    remaining = []
    sent = 0
    for idx, line in enumerate(lines):
        try:
            payload = json.loads(line)
            post_remote_scan(payload)
            sent += 1
        except Exception as exc:
            print(f"[QUEUE] Could not upload pending scan yet: {exc}")
            remaining.extend(lines[idx:])
            break
    try:
        if remaining:
            with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(remaining) + "\n")
        else:
            os.remove(QUEUE_FILE)
    except OSError:
        pass
    if sent:
        print(f"[QUEUE] Uploaded {sent} pending scan(s) to the server.")
    return sent


def save_scan(match, price, owner, finish, quantity=1):
    payload = {
        "scryfall_id": match["id"],
        "name": match["name"],
        "set_code": match["set"],
        "rarity": match["rarity"],
        "collector_number": match.get("collector_number"),
        "tcgplayer_id": match.get("tcg_id"),
        "price": str(price),
        "owner": owner,
        "quantity": max(1, int(quantity)),
        "finish": finish,
        "device": SCANNER_DEVICE,
        "scan_time": datetime.now(timezone.utc).isoformat(),
    }

    if SERVER_URL and SCANNER_API_KEY:
        try:
            result = post_remote_scan(payload)
            flush_pending_queue()
            result["mode"] = "server"
            return result
        except Exception as exc:
            print(f"[REMOTE] Server unavailable: {exc}")
            try:
                queue_scan(payload)
            except OSError as queue_exc:
                print(f"[!] Could not queue scan locally: {queue_exc}")
            return {"success": True, "mode": "queued", "error": str(exc), "quantity": max(1, int(quantity))}

    return save_to_local_inventory(match, price, owner, finish, quantity=quantity)


# ----------------- Data Loading -----------------
def load_local_data():
    global cards_by_name, all_card_names, all_hashes_flat, tcg_prices

    if not os.path.exists(DB_NAME):
        print(f"Error: Could not find database at {DB_NAME}. Run build_db.py first.")
        return False

    print(f"Loading hashes from {DB_NAME} into memory...")
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    try:
        c.execute("SELECT scryfall_id, name, set_code, rarity, tcgplayer_id, phash, collector_number FROM card_art")
        rows = c.fetchall()
        has_cn = True
    except sqlite3.OperationalError:
        print("[!] WARNING: 'collector_number' column missing in mtg_art_hashes.db. Run update_collector_numbers.py first!")
        c.execute("SELECT scryfall_id, name, set_code, rarity, tcgplayer_id, phash FROM card_art")
        rows = c.fetchall()
        has_cn = False

    for row in rows:
        if has_cn:
            scryfall_id, name, set_code, rarity, tcg_id, phash_str, col_num = row
        else:
            scryfall_id, name, set_code, rarity, tcg_id, phash_str = row
            col_num = None
        try:
            phash = imagehash.hex_to_hash(phash_str)
        except Exception:
            continue
        entry = {
            "id": scryfall_id,
            "name": name,
            "set": str(set_code).upper(),
            "rarity": str(rarity).capitalize(),
            "tcg_id": tcg_id,
            "hash": phash,
            "collector_number": str(col_num) if col_num else None,
        }
        norm_name = str(name).lower().strip()
        cards_by_name.setdefault(norm_name, []).append(entry)
        all_hashes_flat.append(entry)

    all_card_names = list(cards_by_name.keys())
    conn.close()
    print(f"Indexed {len(all_hashes_flat)} total card printings across {len(all_card_names)} unique names.")

    if os.path.exists(PRICE_FILE):
        print("Loading TCGplayer offline prices...")
        try:
            with open(PRICE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("results", []):
                pid_raw = item.get("productId")
                if not pid_raw:
                    continue
                try:
                    pid = str(int(float(pid_raw)))
                except (TypeError, ValueError):
                    continue
                tcg_prices.setdefault(pid, {"normal": None, "foil": None})
                subtype = str(item.get("subTypeName", "")).lower()
                price = item.get("marketPrice") or item.get("midPrice") or item.get("lowPrice")
                if price is None:
                    continue
                if "foil" in subtype or "etched" in subtype:
                    if tcg_prices[pid]["foil"] is None:
                        tcg_prices[pid]["foil"] = price
                else:
                    if tcg_prices[pid]["normal"] is None:
                        tcg_prices[pid]["normal"] = price
            print(f"Loaded {len(tcg_prices)} offline prices.")
        except Exception as exc:
            print(f"[!] Could not load {PRICE_FILE}: {exc}")
    else:
        print("Note: tcg_prices.json not found; prices will show as N/A.")
    return True


# ----------------- Hybrid Identification -----------------
def draw_wrapped_text(frame, text, origin, font_scale, color, thickness=3, max_width=None, line_gap=1.25):
    """Draw readable status text, wrapping long messages instead of shrinking the font."""
    if not text:
        return
    if max_width is None:
        max_width = frame.shape[1] - origin[0] - 40

    font = cv2.FONT_HERSHEY_SIMPLEX
    words = str(text).split()
    lines = []
    current = ""

    for word in words:
        trial = word if not current else f"{current} {word}"
        width = cv2.getTextSize(trial, font, font_scale, thickness)[0][0]
        if current and width > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)

    x, y = origin
    line_height = max(1, int(cv2.getTextSize("Ag", font, font_scale, thickness)[0][1] * line_gap))
    for line in lines:
        cv2.putText(frame, line, (x, y), font, font_scale, color, thickness, cv2.LINE_AA)
        y += line_height
    return y



def _normalize_ocr_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower()).strip()


def _read_best_title(bgr_card_crop):
    """Fast title OCR with targeted fallbacks.

    The previous scanner always ran three full EasyOCR passes over the title.
    On ROCm that was unnecessarily expensive. We now accept a strong first
    pass immediately and only try the grayscale/Otsu variants when the first
    result is genuinely ambiguous.
    """
    h, w = bgr_card_crop.shape[:2]
    crop = bgr_card_crop[int(h*0.01):int(h*0.17), int(w*0.01):int(w*0.99)]

    def score_variant(variant):
        try:
            text = _normalize_ocr_text(" ".join(reader.readtext(variant, detail=0)))
        except Exception:
            return ("", 0.0, "")
        if not text:
            return ("", 0.0, "")
        matches = process.extract(text, all_card_names, scorer=fuzz.WRatio, limit=4)
        if not matches:
            return ("", 0.0, text)
        return (matches[0][0], float(matches[0][1]), text)

    best = score_variant(crop)
    # Most good scans land above this threshold on the first pass. Do not pay
    # for additional GPU OCR work when the name is already clear.
    if best[1] >= 90.0:
        return best

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    normalized = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    candidate = score_variant(normalized)
    if candidate[1] > best[1]:
        best = candidate
    if best[1] >= 86.0 or not gray.size:
        return best

    otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    candidate = score_variant(otsu)
    if candidate[1] > best[1]:
        best = candidate
    return best


def _read_scryfall_cache() -> dict:
    try:
        with open(SCRYFALL_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_scryfall_cache(cache: dict) -> None:
    try:
        with open(SCRYFALL_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
    except OSError as exc:
        print(f"[SCRYFALL] Could not write cache: {exc}")


def _scryfall_exact_name_prints(name: str) -> list[dict]:
    if not SCRYFALL_LOOKUP_ENABLED or not name:
        return []
    key = _normalize_ocr_text(name)
    cache = _read_scryfall_cache()
    cached = cache.get(key)
    if isinstance(cached, list):
        return cached
    headers = {"User-Agent": SCRYFALL_USER_AGENT, "Accept": "application/json"}
    try:
        response = requests.get(
            "https://api.scryfall.com/cards/search",
            params={"q": f'name:"{key}"', "unique": "prints", "order": "released"},
            headers=headers,
            timeout=SCRYFALL_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        prints = []
        for card in data.get("data", []):
            prints.append({
                "id": card.get("id"),
                "name": card.get("name"),
                "set": str(card.get("set") or "").upper(),
                "collector_number": str(card.get("collector_number") or ""),
                "tcg_id": card.get("tcgplayer_id"),
                "finishes": card.get("finishes") if isinstance(card.get("finishes"), list) else [],
                "image_url": ((card.get("image_uris") or {}).get("normal") if isinstance(card.get("image_uris"), dict) else None),
            })
        cache[key] = prints
        _write_scryfall_cache(cache)
        return prints
    except Exception as exc:
        print(f"[SCRYFALL] Name lookup failed for '{name}': {exc}")
        return []


def _scryfall_image_hash(url: str):
    cache = _read_scryfall_cache()
    # Image cache is stored separately to avoid mixing list payloads.
    image_cache_path = SCRYFALL_CACHE_FILE + ".images"
    try:
        with open(image_cache_path, "r", encoding="utf-8") as f:
            image_cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        image_cache = {}
    if url in image_cache:
        try:
            return imagehash.hex_to_hash(image_cache[url])
        except Exception:
            pass
    try:
        r = requests.get(url, headers={"User-Agent": SCRYFALL_USER_AGENT}, timeout=SCRYFALL_TIMEOUT)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB")
        ph = imagehash.phash(img)
        image_cache[url] = str(ph)
        with open(image_cache_path, "w", encoding="utf-8") as f:
            json.dump(image_cache, f, separators=(",", ":"))
        return ph
    except Exception:
        return None


def resolve_remote_by_name_set_number(bgr_card_crop, scanned_name: str, detected_sets: list[str], detected_collector_num: str | None):
    """Last-resort exact-name resolution through Scryfall.

    A result is only returned when set/collector or image evidence gives us a
    sufficiently specific printing. This prevents a generic card-name match
    from silently picking the wrong reprint.
    """
    prints = _scryfall_exact_name_prints(scanned_name)
    if not prints:
        return None
    normalized_sets = {str(x).lower() for x in detected_sets}
    target_num = str(detected_collector_num or "").lstrip("0").lower()

    def cn_norm(c):
        return str(c.get("collector_number") or "").lstrip("0").lower()

    # Strongest: exact set + collector number.
    exact = [c for c in prints if normalized_sets and str(c.get("set", "")).lower() in normalized_sets and target_num and cn_norm(c) == target_num]
    if len(exact) == 1:
        return exact[0]
    if exact:
        prints = exact

    # If a set code alone yields one printing, it is sufficiently specific.
    if normalized_sets:
        by_set = [c for c in prints if str(c.get("set", "")).lower() in normalized_sets]
        if len(by_set) == 1:
            return by_set[0]
        if by_set:
            prints = by_set

    # A collector number can also be unique for a card name.
    if target_num:
        by_num = [c for c in prints if cn_norm(c) == target_num]
        if len(by_num) == 1:
            return by_num[0]
        if by_num:
            prints = by_num

    if len(prints) == 1:
        return prints[0]

    # Multiple exact-name printings: use image evidence if available.
    h, w = bgr_card_crop.shape[:2]
    region = bgr_card_crop[int(h*0.14):int(h*0.82), int(w*0.08):int(w*0.92)]
    if region.size:
        scan_hash = imagehash.phash(Image.fromarray(cv2.cvtColor(region, cv2.COLOR_BGR2RGB)))
        scored = []
        for card in prints[:20]:
            url = card.get("image_url")
            if not url:
                continue
            ref_hash = _scryfall_image_hash(url)
            if ref_hash is not None:
                scored.append((scan_hash - ref_hash, card))
        if scored:
            scored.sort(key=lambda x: x[0])
            best_dist, best_card = scored[0]
            second_dist = scored[1][0] if len(scored) > 1 else 999
            # Require a useful margin when image matching among reprints.
            if best_dist <= 18 and (second_dist - best_dist >= 2 or len(scored) == 1):
                return best_card
    return None


def _guess_artist_from_metadata(raw_text: str, words: list[str]) -> str | None:
    """Best-effort extraction of the printed artist from the bottom metadata line."""
    text = _normalize_ocr_text(raw_text).replace("/", " ")
    if not text:
        return None
    tokens = text.split()
    # Language codes commonly precede the artist on the bottom line.
    language_tokens = {"en", "de", "fr", "es", "it", "pt", "ja", "jp", "ko", "ru", "cs", "ph", "cn", "zhs", "zht"}
    start = None
    for i, tok in enumerate(tokens):
        if tok in language_tokens:
            start = i + 1
    if start is None:
        return None
    stop_words = {
        "wizards", "coast", "inc", "inc.", "llc", "hasbro", "copyright", "tm", "©",
    }
    artist_tokens = []
    for tok in tokens[start:]:
        if tok in stop_words or re.search(r"\d", tok):
            break
        if tok in {"the"} and not artist_tokens:
            continue
        artist_tokens.append(tok)
        if len(artist_tokens) >= 5:
            break
    if not artist_tokens:
        return None
    artist = " ".join(artist_tokens).strip(" -")
    return artist or None


def _read_metadata_fallback(bgr_card_crop):
    """A second, targeted OCR pass for tiny collector/set/artist text.

    Only called for ambiguous title matches when the first metadata pass failed
    to produce a useful set or collector number. The crop is enlarged and
    contrast-normalized so basic lands and older cards have a better chance of
    yielding the printed metadata without making every scan slower.
    """
    h, w = bgr_card_crop.shape[:2]
    y1, y2 = int(h * 0.89), int(h * 0.999)
    x1, x2 = int(w * 0.03), int(w * 0.99)
    crop = bgr_card_crop[y1:y2, x1:x2]
    if crop.size == 0:
        return "", None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    big = cv2.resize(clahe, None, fx=5.0, fy=5.0, interpolation=cv2.INTER_CUBIC)
    try:
        results = reader.readtext(big, detail=0)
        raw = " ".join(results).lower()
    except Exception as exc:
        print(f"[OCR META] fallback failed: {exc}")
        return "", None
    clean = re.sub(r"[^a-z0-9/ -]", " ", raw)
    clean = re.sub(r"\s+", " ", clean).strip()
    words = clean.split()
    artist = _guess_artist_from_metadata(clean, words)
    print(f"[*] Metadata OCR fallback saw: '{clean}'" + (f" · artist='{artist}'" if artist else ""))
    return clean, artist


def _scryfall_name_artist_prints(name: str, artist: str) -> list[dict]:
    """Small cached Scryfall query used only when local metadata is ambiguous."""
    name = _normalize_ocr_text(name)
    artist = " ".join(str(artist or "").split())
    if not SCRYFALL_LOOKUP_ENABLED or not name or not artist:
        return []
    cache_key = f"artist::{name}::{artist.casefold()}"
    cache = _read_scryfall_cache()
    cached = cache.get(cache_key)
    if isinstance(cached, list):
        return cached
    headers = {"User-Agent": SCRYFALL_USER_AGENT, "Accept": "application/json"}
    try:
        response = requests.get(
            "https://api.scryfall.com/cards/search",
            params={"q": f'name:"{name}" artist:"{artist}"', "unique": "prints", "order": "released"},
            headers=headers,
            timeout=SCRYFALL_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        prints = []
        for card in data.get("data", []):
            prints.append({
                "id": card.get("id"),
                "name": card.get("name"),
                "set": str(card.get("set") or "").upper(),
                "collector_number": str(card.get("collector_number") or ""),
                "tcg_id": card.get("tcgplayer_id"),
                "artist": card.get("artist") or "",
                "finishes": card.get("finishes") if isinstance(card.get("finishes"), list) else [],
                "image_url": ((card.get("image_uris") or {}).get("normal") if isinstance(card.get("image_uris"), dict) else None),
            })
        cache[cache_key] = prints
        _write_scryfall_cache(cache)
        return prints
    except Exception as exc:
        print(f"[SCRYFALL] Name+artist lookup failed for '{name}' / '{artist}': {exc}")
        return []

def identify_card_hybrid(bgr_card_crop, is_foil_mode):
    """Identify a card using title OCR first, then set/collector, then image hashes.

    Important safety rule: never allow a Set/Collector OCR hit to override a strong
    card-name OCR match with a different card name. This prevents collisions such as
    Genesis Ultimatum (SPG #106) being replaced by another card that happens to be
    TDM #106.
    """
    h, w = bgr_card_crop.shape[:2]

    title_score = 0.0
    candidate_printings = []
    scanned_name = ""
    best_name, title_score, best_ocr = _read_best_title(bgr_card_crop)
    scanned_name = best_ocr or _normalize_ocr_text(best_name)
    if scanned_name:
        if best_ocr and best_ocr != scanned_name and title_score >= 90:
            scanned_name = best_ocr
        if best_name and title_score >= 74:
            # Gather a small family of near-equal title matches so a slightly
            # misspelled OCR string does not eliminate the correct printing.
            near = process.extract(scanned_name, all_card_names, scorer=fuzz.WRatio, limit=8)
            threshold = max(72.0, title_score - 10.0)
            for item in near:
                name_choice, score = item[0], float(item[1])
                if score >= threshold:
                    candidate_printings.extend(cards_by_name.get(name_choice, []))
            if not candidate_printings:
                candidate_printings = list(cards_by_name.get(best_name, []))
            print(f"[OCR] '{scanned_name}' -> '{best_name}' ({title_score:.1f}%)")
        else:
            print(f"[OCR] '{scanned_name}' best candidate '{best_name}' only ({title_score:.1f}%), using broader matching.")

    # Read the lower edge for set / rarity / collector information only when
    # the title could refer to multiple printings. This removes a full EasyOCR
    # pass for uniquely named cards while preserving the metadata needed for
    # common names such as Forest, Island, Plains, etc.
    raw_bottom_text = ""
    clean_bottom_text = ""
    words = []
    detected_sets = []
    detected_collector_num = None
    detected_artist = None
    unique_sets = {str(c["set"]).lower() for c in all_hashes_flat}

    if len(candidate_printings) > 1 or title_score < 93.0:
        bottom_y1, bottom_y2 = int(h * 0.87), int(h * 0.998)
        bottom_x1, bottom_x2 = int(w * 0.01), int(w * 0.99)
        bottom_crop = bgr_card_crop[bottom_y1:bottom_y2, bottom_x1:bottom_x2]
        bh, bw = bottom_crop.shape[:2]
        if bw > 0 and bh > 0:
            scale = max(1.0, 700.0 / bw)
            big_bottom = cv2.resize(bottom_crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        else:
            big_bottom = bottom_crop
        bottom_results = reader.readtext(big_bottom, detail=0)
        raw_bottom_text = " ".join(bottom_results).lower()
        clean_bottom_text = re.sub(r"[^a-z0-9/ -]", " ", raw_bottom_text)
        clean_bottom_text = re.sub(r"\s+", " ", clean_bottom_text).strip()
        words = clean_bottom_text.split()
        print(f"[*] Bottom OCR saw: '{clean_bottom_text}'")

        for word in words:
            if word in unique_sets and word not in detected_sets:
                detected_sets.append(word)
        detected_collector_num = parse_collector_number(words, raw_bottom_text)
        detected_artist = _guess_artist_from_metadata(clean_bottom_text, words)

        # Basic lands and a few promo/secret-lair layouts can defeat the first
        # tiny-text pass. Only pay for the enhanced OCR fallback when we still
        # have an ambiguous common name and lack useful set/collector metadata.
        if len(candidate_printings) > 8 and (not detected_sets or not detected_collector_num):
            meta_text, meta_artist = _read_metadata_fallback(bgr_card_crop)
            if meta_text:
                raw_bottom_text = f"{raw_bottom_text} {meta_text}".strip()
                clean_bottom_text = meta_text
                words = clean_bottom_text.split()
                detected_collector_num = parse_collector_number(words, clean_bottom_text) or detected_collector_num
                if meta_artist:
                    detected_artist = meta_artist
                for word in words:
                    if word in unique_sets and word not in detected_sets:
                        detected_sets.append(word)

        if detected_artist:
            print(f"[*] Detected artist: '{detected_artist}'")

    candidate_sets = {str(c["set"]).lower() for c in candidate_printings}
    # SPG cards are frequently described by the printed TDM set line plus the
    # words 'Special Guest'. Treat that phrase as evidence for SPG.
    if {"special", "guest"}.issubset(set(words)) and "spg" in candidate_sets and "spg" not in detected_sets:
        detected_sets.append("spg")

    detected_rarities = []
    if "m" in words or "mythic" in words:
        detected_rarities.append("Mythic")
    if "r" in words or "rare" in words:
        detected_rarities.append("Rare")
    if "u" in words or "uncommon" in words:
        detected_rarities.append("Uncommon")
    if "c" in words or "common" in words:
        detected_rarities.append("Common")

    original_title_candidates = list(candidate_printings)

    if detected_sets:
        print(f"[*] Extracted Set Code(s): {detected_sets}")

    # 1) Strongest disambiguation: exact collector number inside the OCR name candidates.
    #    This is intentionally done BEFORE any global set/collector lookup.
    if candidate_printings and detected_collector_num:
        number_matches = []
        for c in candidate_printings:
            rank = _collector_match_rank(c.get("collector_number"), detected_collector_num)
            if rank >= 0:
                number_matches.append((rank, c))

        if detected_sets:
            allowed_sets = {str(x).lower() for x in detected_sets}
            number_matches = [(rank, c) for rank, c in number_matches if str(c["set"]).lower() in allowed_sets]

        if number_matches:
            best_rank = max(rank for rank, _ in number_matches)
            best = [c for rank, c in number_matches if rank == best_rank]
            if len(best) == 1:
                print(f"[LOCAL SUCCESS] Exact Name/Set/Collector: {best[0]['name']} [{best[0]['set']} #{detected_collector_num}]")
                return best[0]

    # 2) Narrow title candidates by any set codes that were successfully read.
    if candidate_printings and detected_sets:
        set_pool = [c for c in candidate_printings if str(c["set"]).lower() in set(detected_sets)]
        if set_pool:
            candidate_printings = set_pool

    # 3) If we have a title match, perform exact Set/Collector ONLY inside those
    #    title candidates. Never use a different card merely because its set/number matches.
    if candidate_printings and detected_sets and detected_collector_num:
        target_num = detected_collector_num.lstrip("0") or "0"
        exact_matches = [
            c for c in candidate_printings
            if str(c["set"]).lower() in set(detected_sets)
            and c.get("collector_number")
            and str(c["collector_number"]).lower().lstrip("0") == target_num.lower()
        ]
        if len(exact_matches) == 1:
            print(f"[LOCAL SUCCESS] Exact Name/Set/Collector: {exact_matches[0]['name']} [{exact_matches[0]['set']} #{detected_collector_num}]")
            return exact_matches[0]

    global_pool = all_hashes_flat
    if detected_sets:
        global_pool = [c for c in global_pool if str(c["set"]).lower() in set(detected_sets)]
        if detected_rarities:
            rarity_pool = [c for c in global_pool if c["rarity"] in detected_rarities]
            if rarity_pool:
                global_pool = rarity_pool

    # If OCR title was uncertain, an exact global Set/Collector match can still help,
    # but only when its card name agrees strongly with the title OCR. This blocks the
    # previous false positive where Genesis Ultimatum became Equilibrium Adept.
    if detected_sets and detected_collector_num and (not original_title_candidates or title_score < 78):
        target_set = detected_sets[0]
        target_num = detected_collector_num.lstrip("0") or "0"
        ranked = []
        for c in all_hashes_flat:
            if str(c["set"]).lower() != target_set.lower():
                continue
            rank = _collector_match_rank(c.get("collector_number"), detected_collector_num)
            if rank >= 0:
                name_score = fuzz.WRatio(scanned_name, str(c["name"]).lower()) if scanned_name else 0
                ranked.append((rank, name_score, c))
        if ranked:
            ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
            best_rank, best_name_score, best_card = ranked[0]
            tied = [x for x in ranked if x[0] == best_rank and x[1] == best_name_score]
            if (not scanned_name or best_name_score >= 82) and (len(tied) == 1 or best_rank >= 100):
                print(f"[LOCAL SUCCESS] Verified Set/Collector: {best_card['name']} [{target_set} #{detected_collector_num}]")
                return best_card
            print(f"[SAFE MATCH] Could not uniquely verify Set/Collector {target_set} #{detected_collector_num} against OCR '{scanned_name}'.")

    # For highly ambiguous common names, artist metadata is much more useful
    # than a loose image-hash winner. If OCR found an artist but not enough
    # local metadata to uniquely identify a printing, use one cached Scryfall
    # name+artist query as a guarded resolver.
    if detected_artist and (not detected_sets or not detected_collector_num) and scanned_name:
        remote_artist_prints = _scryfall_name_artist_prints(scanned_name, detected_artist)
        if detected_sets:
            remote_artist_prints = [c for c in remote_artist_prints if str(c.get("set", "")).lower() in set(detected_sets)] or remote_artist_prints
        if detected_collector_num:
            target_num = detected_collector_num.lstrip("0") or "0"
            remote_exact_num = [c for c in remote_artist_prints if str(c.get("collector_number", "")).lstrip("0") == target_num]
            if len(remote_exact_num) == 1:
                print(f"[REMOTE MATCH] Name+artist+collector resolved: {remote_exact_num[0].get('name')} [{remote_exact_num[0].get('set')} #{remote_exact_num[0].get('collector_number')}]")
                return remote_exact_num[0]
        if len(remote_artist_prints) == 1:
            print(f"[REMOTE MATCH] Name+artist resolved: {remote_artist_prints[0].get('name')} [{remote_artist_prints[0].get('set')} #{remote_artist_prints[0].get('collector_number')}]")
            return remote_artist_prints[0]

    pool_to_search = candidate_printings if candidate_printings else global_pool
    if not pool_to_search:
        pool_to_search = all_hashes_flat
    if len(pool_to_search) == 1:
        return pool_to_search[0]

    # Consistency-weighted image matching. The older matcher used the minimum
    # distance across four hashes, so one accidentally similar crop could beat
    # the correct printing. Here a good candidate must be consistently close
    # across three art-focused crops. This is both faster and less prone to
    # false positives between visually similar reprints.
    regions = [
        (int(h * 0.14), int(h * 0.58), int(w * 0.08), int(w * 0.92)),
        (int(h * 0.18), int(h * 0.72), int(w * 0.10), int(w * 0.90)),
        (int(h * 0.10), int(h * 0.52), int(w * 0.05), int(w * 0.95)),
    ]
    hashes = [
        imagehash.phash(Image.fromarray(cv2.cvtColor(bgr_card_crop[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)))
        for y1, y2, x1, x2 in regions
    ]

    max_dist = 26 if candidate_printings and title_score >= 88 else (24 if candidate_printings else 8)
    valid_matches = []
    for item in pool_to_search:
        distances = sorted(hh - item["hash"] for hh in hashes)
        # Weight the best match, but require the other crops to agree.
        combined = (distances[0] * 0.50) + (distances[1] * 0.30) + (distances[2] * 0.20)
        if distances[0] <= max_dist and combined <= max_dist:
            valid_matches.append((combined, distances, item))
    if not valid_matches:
        # Local image hashes can be stale or missing for some printings.
        # Fall back to an exact-name Scryfall lookup rather than guessing a
        # different card by set/collector alone.
        remote = resolve_remote_by_name_set_number(
            bgr_card_crop, scanned_name, detected_sets, detected_collector_num
        ) if scanned_name and title_score >= 90 else None
        if remote:
            print(f"[REMOTE MATCH] Exact-name Scryfall resolution: {remote.get('name')} [{str(remote.get('set') or '').upper()} #{remote.get('collector_number','?')}]" )
            return remote
        return None

    valid_matches.sort(key=lambda x: (x[0], -fuzz.WRatio(scanned_name, str(x[2]["name"]).lower()) if scanned_name else 0))
    best_score = valid_matches[0][0]
    tied = [m[2] for m in valid_matches if m[0] == best_score]

    # Prefer a name-consistent tie before price/finish heuristics.
    if scanned_name and len(tied) > 1:
        tied.sort(key=lambda x: fuzz.WRatio(scanned_name, str(x["name"]).lower()), reverse=True)
        best_name_score = fuzz.WRatio(scanned_name, str(tied[0]["name"]).lower())
        name_ties = [c for c in tied if fuzz.WRatio(scanned_name, str(c["name"]).lower()) == best_name_score]
        tied = name_ties or tied

    if len(tied) > 1:
        for candidate in tied:
            tcg_raw = candidate.get("tcg_id")
            tid = None
            if tcg_raw:
                try:
                    tid = str(int(float(tcg_raw)))
                except (TypeError, ValueError):
                    pass
            p = tcg_prices.get(tid, {})
            if is_foil_mode and p.get("foil") is not None:
                return candidate
            if not is_foil_mode and p.get("normal") is not None:
                return candidate
    return tied[0]
