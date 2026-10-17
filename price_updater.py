from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

BASE_URL = os.environ.get("TCGCSV_BASE_URL", "https://tcgcsv.com")
CATEGORY_ID = int(os.environ.get("TCGCSV_CATEGORY_ID", "1"))  # Magic: The Gathering
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PRICES_FILE = DATA_DIR / "tcg_prices.json"
META_FILE = DATA_DIR / "price_sync.json"
LAST_SEEN_FILE = DATA_DIR / "tcgcsv_last_updated.txt"
DB_INV = DATA_DIR / "mtg_inventory.db"
DB_ART = DATA_DIR / "mtg_art_hashes.db"
USER_AGENT = os.environ.get("TCGCSV_USER_AGENT", "MTGScannerInventory/1.1")
REQUEST_DELAY = float(os.environ.get("TCGCSV_REQUEST_DELAY", "0.12"))
CHECK_INTERVAL = int(os.environ.get("TCGCSV_CHECK_INTERVAL", "3600"))
TIMEOUT = int(os.environ.get("TCGCSV_TIMEOUT", "30"))

# Price history / alerts
PRICE_HISTORY_DAYS = max(30, int(os.environ.get("PRICE_HISTORY_DAYS", "365")))
PRICE_ALERT_ENABLED = os.environ.get("PRICE_ALERT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
PRICE_ALERT_LOOKBACK_DAYS = max(1, int(os.environ.get("PRICE_ALERT_LOOKBACK_DAYS", "30")))
PRICE_ALERT_COOLDOWN_DAYS = max(0, int(os.environ.get("PRICE_ALERT_COOLDOWN_DAYS", "7")))
PRICE_ALERT_MIN_DOLLARS = max(0.0, float(os.environ.get("PRICE_ALERT_MIN_DOLLARS", "2.00")))
PRICE_ALERT_MIN_PERCENT = max(0.0, float(os.environ.get("PRICE_ALERT_MIN_PERCENT", "25")))
PRICE_ALERT_MIN_OWNED_VALUE_GAIN = max(0.0, float(os.environ.get("PRICE_ALERT_MIN_OWNED_VALUE_GAIN", "10.00")))
PRICE_ALERT_MAX_PER_SYNC = max(1, int(os.environ.get("PRICE_ALERT_MAX_PER_SYNC", "10")))
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "America/New_York").strip() or "America/New_York"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"})


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_INV, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    # WAL mode is initialized by the web app's one-time database migration.
    # Reissuing PRAGMA journal_mode=WAL here can race the web startup migration.
    return conn


def normalize_price(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def get_json(path: str):
    url = path if path.startswith("http") else f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_last_updated() -> str:
    url = f"{BASE_URL.rstrip('/')}/last-updated.txt"
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text.strip()


def build_price_map(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Normalize TCGCSV rows into TCGplayer ID -> normal/foil prices."""
    price_map: dict[str, dict[str, Any]] = {}
    for item in items:
        pid_raw = item.get("productId")
        if not pid_raw:
            continue
        try:
            pid = str(int(float(pid_raw)))
        except (TypeError, ValueError):
            continue
        entry = price_map.setdefault(pid, {"normal": None, "foil": None})
        subtype = str(item.get("subTypeName") or "").lower()
        variant = "foil" if ("foil" in subtype or "etched" in subtype) else "normal"
        value = item.get("marketPrice")
        if value is None:
            value = item.get("midPrice")
        if value is None:
            value = item.get("lowPrice")
        if value is not None and entry[variant] is None:
            entry[variant] = value
    return price_map


def load_saved_marker() -> str:
    try:
        return LAST_SEEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_history_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tcgplayer_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            source_updated TEXT NOT NULL,
            normal_price REAL,
            foil_price REAL,
            UNIQUE(tcgplayer_id, source_updated)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS price_alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tcgplayer_id TEXT NOT NULL,
            finish TEXT NOT NULL,
            alerted_at TEXT NOT NULL,
            old_price REAL NOT NULL,
            new_price REAL NOT NULL,
            percent_change REAL NOT NULL,
            owned_quantity INTEGER NOT NULL DEFAULT 0,
            owned_value_gain REAL NOT NULL DEFAULT 0.0
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_history_tcg_time ON price_history(tcgplayer_id, captured_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_alert_tcg_finish_time ON price_alert_log(tcgplayer_id, finish, alerted_at)")
    conn.commit()


def owned_products(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return owned TCGplayer products, resolving IDs from every supported source."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS card_price_mapping (
            scryfall_id TEXT PRIMARY KEY,
            tcgplayer_id TEXT,
            finishes_json TEXT,
            card_name TEXT,
            set_code TEXT,
            collector_number TEXT,
            resolved_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_card_price_mapping_tcg "
        "ON card_price_mapping(tcgplayer_id)"
    )

    # Provide the alias even if the optional art database is missing/unavailable.
    join_sql = (
        "LEFT JOIN (SELECT NULL AS scryfall_id, NULL AS tcgplayer_id) a ON 1=0"
    )
    if DB_ART.exists():
        attached = False
        try:
            conn.execute("ATTACH DATABASE ? AS art_db", (str(DB_ART),))
            attached = True
            conn.execute("SELECT 1 FROM art_db.card_art LIMIT 1").fetchone()
            join_sql = "LEFT JOIN art_db.card_art a ON i.scryfall_id = a.scryfall_id"
        except sqlite3.Error:
            if attached:
                try:
                    conn.execute("DETACH DATABASE art_db")
                except sqlite3.Error:
                    pass

    # Mapping-table IDs must remain joined regardless of whether the art DB exists.
    join_sql += "\nLEFT JOIN card_price_mapping m ON m.scryfall_id = i.scryfall_id"
    tcg_id_sql = (
        "COALESCE("
        "NULLIF(TRIM(i.tcgplayer_id), ''), "
        "NULLIF(TRIM(a.tcgplayer_id), ''), "
        "NULLIF(TRIM(m.tcgplayer_id), '')"
        ")"
    )
    rows = conn.execute(
        f"""SELECT {tcg_id_sql} AS tcgplayer_id,
                  MAX(i.scryfall_id) AS scryfall_id,
                  MAX(i.name) AS name,
                  MAX(i.set_code) AS set_code,
                  MAX(i.collector_number) AS collector_number,
                  SUM(CASE WHEN IFNULL(NULLIF(i.finish,''),'normal')='normal' THEN IFNULL(i.quantity,1) ELSE 0 END) AS normal_qty,
                  SUM(CASE WHEN IFNULL(NULLIF(i.finish,''),'normal')='foil' THEN IFNULL(i.quantity,1) ELSE 0 END) AS foil_qty
           FROM inventory i
           {join_sql}
           WHERE {tcg_id_sql} IS NOT NULL AND TRIM({tcg_id_sql}) <> ''
           GROUP BY {tcg_id_sql}"""
    ).fetchall()
    return [dict(r) for r in rows]


def scryfall_image_url(scryfall_id: str | None) -> str | None:
    if not scryfall_id:
        return None
    sid = str(scryfall_id).strip().lower()
    if len(sid) < 2:
        return None
    return f"https://cards.scryfall.io/normal/front/{sid[0]}/{sid[1]}/{sid}.jpg"


def last_history_point(conn: sqlite3.Connection, tcg_id: str, finish: str, cutoff: str) -> float | None:
    column = "foil_price" if finish == "foil" else "normal_price"
    row = conn.execute(
        f"""SELECT {column} AS price
            FROM price_history
            WHERE tcgplayer_id = ? AND captured_at <= ? AND {column} IS NOT NULL
            ORDER BY captured_at DESC LIMIT 1""",
        (tcg_id, cutoff),
    ).fetchone()
    return normalize_price(row["price"]) if row else None


def recently_alerted(conn: sqlite3.Connection, tcg_id: str, finish: str, cutoff: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM price_alert_log
           WHERE tcgplayer_id = ? AND finish = ? AND alerted_at >= ?
           ORDER BY alerted_at DESC LIMIT 1""",
        (tcg_id, finish, cutoff),
    ).fetchone()
    return row is not None


def send_discord_alert(card: dict[str, Any], finish: str, old_price: float, new_price: float, percent: float,
                       owned_qty: int, owned_gain: float) -> tuple[bool, str]:
    if not DISCORD_WEBHOOK_URL:
        return False, "DISCORD_WEBHOOK_URL is not configured"
    image = scryfall_image_url(card.get("scryfall_id"))
    description = (
        f"**${old_price:,.2f} → ${new_price:,.2f}** (+{percent:.1f}%)\n"
        f"You own **{owned_qty:,} {finish}** copies · owned-value gain **+${owned_gain:,.2f}**"
    )
    payload: dict[str, Any] = {
        "embeds": [{
            "title": f"📈 Price Alert — {card.get('name') or 'Unknown card'} ({finish.title()})",
            "description": description,
            "fields": [
                {"name": "Set", "value": str(card.get("set_code") or "—"), "inline": True},
                {"name": "Collector #", "value": str(card.get("collector_number") or "—"), "inline": True},
                {"name": "30-day reference", "value": f"${old_price:,.2f}", "inline": True},
                {"name": "Current", "value": f"${new_price:,.2f}", "inline": True},
            ],
            "footer": {"text": "MTG Scanner Inventory"},
        }]
    }
    if image:
        payload["embeds"][0]["thumbnail"] = {"url": image}
    try:
        response = session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        if response.status_code == 429:
            return False, "Discord webhook rate-limited the request"
        response.raise_for_status()
        return True, "sent"
    except requests.RequestException as exc:
        return False, str(exc)


def record_history_and_alerts(price_map: dict[str, dict[str, Any]], source_updated: str) -> tuple[int, int]:
    if not DB_INV.exists():
        print("[history] inventory database not found; skipping price history")
        return 0, 0
    conn = connect_db()
    ensure_history_tables(conn)
    now = datetime.now(timezone.utc)
    captured_at = now.isoformat()
    cutoff_retention = (now - timedelta(days=PRICE_HISTORY_DAYS)).isoformat()
    cutoff_alert = (now - timedelta(days=PRICE_ALERT_LOOKBACK_DAYS)).isoformat()
    cooldown_cutoff = (now - timedelta(days=PRICE_ALERT_COOLDOWN_DAYS)).isoformat()
    products = owned_products(conn)
    history_rows = 0
    alerts_sent = 0

    for card in products:
        tcg_id = str(card["tcgplayer_id"]).strip()
        prices = price_map.get(tcg_id, {})
        normal = normalize_price(prices.get("normal"))
        foil = normalize_price(prices.get("foil"))
        if normal is None and foil is None:
            continue
        for finish, current in (("normal", normal), ("foil", foil)):
            if current is None:
                continue
            # Check the old price before inserting today's point so the comparison is genuinely historical.
            old = last_history_point(conn, tcg_id, finish, cutoff_alert)
            cur = conn.execute(
                """INSERT OR IGNORE INTO price_history
                   (tcgplayer_id,captured_at,source_updated,normal_price,foil_price)
                   VALUES (?,?,?,?,?)""",
                (tcg_id, captured_at, source_updated, normal, foil),
            )
            if cur.rowcount > 0:
                history_rows += 1

            if not PRICE_ALERT_ENABLED or not DISCORD_WEBHOOK_URL or old is None or old <= 0:
                continue
            delta = current - old
            if delta <= 0:
                continue
            percent = (delta / old) * 100.0
            owned_qty = int(card["foil_qty"] if finish == "foil" else card["normal_qty"] or 0)
            owned_gain = delta * owned_qty
            threshold_ok = delta >= PRICE_ALERT_MIN_DOLLARS or percent >= PRICE_ALERT_MIN_PERCENT
            owned_ok = PRICE_ALERT_MIN_OWNED_VALUE_GAIN <= 0 or owned_gain >= PRICE_ALERT_MIN_OWNED_VALUE_GAIN
            if not threshold_ok or not owned_ok:
                continue
            if PRICE_ALERT_COOLDOWN_DAYS > 0 and recently_alerted(conn, tcg_id, finish, cooldown_cutoff):
                continue
            if alerts_sent >= PRICE_ALERT_MAX_PER_SYNC:
                continue
            ok, msg = send_discord_alert(card, finish, old, current, percent, owned_qty, owned_gain)
            if ok:
                conn.execute(
                    """INSERT INTO price_alert_log
                       (tcgplayer_id,finish,alerted_at,old_price,new_price,percent_change,owned_quantity,owned_value_gain)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (tcg_id, finish, captured_at, old, current, percent, owned_qty, owned_gain),
                )
                alerts_sent += 1
                print(f"[alert] {card.get('name')} ({finish}) {old:.2f} -> {current:.2f}, +{percent:.1f}%")
            else:
                print(f"[alert] failed for {card.get('name')} ({finish}): {msg}")

    conn.execute("DELETE FROM price_history WHERE captured_at < ?", (cutoff_retention,))
    # Keep alert logs smaller than the price-history retention window.
    conn.execute("DELETE FROM price_alert_log WHERE alerted_at < ?", (cutoff_retention,))
    conn.commit()
    conn.close()
    return history_rows, alerts_sent


def sync_prices(force: bool = False) -> bool:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    marker = get_last_updated()
    previous = load_saved_marker()
    # If the current price snapshot is missing, rebuild it even when the
    # last-seen TCGCSV marker matches. This prevents a fresh/recreated
    # data directory from getting stuck with no prices.
    snapshot_missing = not PRICES_FILE.exists()
    if not force and not snapshot_missing and previous and marker == previous:
        # A card can become trackable even if TCGCSV has not published new prices.
        # Reuse the saved snapshot to record the first history point for newly
        # mapped cards rather than skipping history capture entirely.
        try:
            saved_snapshot = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
            saved_prices = saved_snapshot.get("results") if isinstance(saved_snapshot, dict) else None
            if not isinstance(saved_prices, list):
                raise ValueError("saved snapshot does not contain a results list")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"[warn] Saved price snapshot cannot be used ({exc}); rebuilding TCGCSV build {marker}.")
        else:
            history_rows, alerts_sent = record_history_and_alerts(build_price_map(saved_prices), marker)
            try:
                meta = json.loads(META_FILE.read_text(encoding="utf-8"))
                if not isinstance(meta, dict):
                    meta = {}
            except (OSError, json.JSONDecodeError):
                meta = {}
            meta["history_rows_recorded"] = history_rows
            meta["alerts_sent"] = alerts_sent
            meta["history_checked_at"] = datetime.now(timezone.utc).isoformat()
            META_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            print(f"No TCGCSV update detected ({marker}); recorded {history_rows:,} new history point(s) from the saved snapshot.")
            return False
    if snapshot_missing and previous and marker == previous:
        print(f"Price snapshot is missing; rebuilding TCGCSV build {marker}.")

    groups_payload = get_json(f"/tcgplayer/{CATEGORY_ID}/groups")
    groups = groups_payload.get("results", [])
    all_prices = []
    synced_groups = 0
    failures = []

    print(f"TCGCSV build {marker}: syncing {len(groups)} MTG groups")
    for idx, group in enumerate(groups, start=1):
        group_id = group.get("groupId")
        if not group_id:
            continue
        try:
            payload = get_json(f"/tcgplayer/{CATEGORY_ID}/{group_id}/prices")
            for item in payload.get("results", []):
                if item.get("productId"):
                    all_prices.append(item)
            synced_groups += 1
        except requests.RequestException as exc:
            failures.append({"groupId": group_id, "error": str(exc)})
            print(f"[warn] group {group_id} failed: {exc}")
        time.sleep(REQUEST_DELAY)
        if idx % 25 == 0:
            print(f"  {idx}/{len(groups)} groups processed")

    meta = {
        "source": "TCGCSV",
        "category_id": CATEGORY_ID,
        "source_updated": marker,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "groups": len(groups),
        "groups_synced": synced_groups,
        "price_rows": len(all_prices),
        "failures": failures,
        "price_history_days": PRICE_HISTORY_DAYS,
        "discord_alerts_enabled": PRICE_ALERT_ENABLED and bool(DISCORD_WEBHOOK_URL),
    }

    if failures:
        META_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"Sync incomplete: {len(failures)} group(s) failed. Existing price snapshot was kept; next run will retry build {marker}.")
        return False

    # Build a quick productId -> normal/foil map for history capture.
    price_map = build_price_map(all_prices)

    tmp = PRICES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"success": True, "sourceUpdated": marker, "results": all_prices}, separators=(",", ":")), encoding="utf-8")
    tmp.replace(PRICES_FILE)
    history_rows, alerts_sent = record_history_and_alerts(price_map, marker)
    meta["history_rows_recorded"] = history_rows
    meta["alerts_sent"] = alerts_sent
    META_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    LAST_SEEN_FILE.write_text(marker + "\n", encoding="utf-8")
    print(f"Wrote {len(all_prices):,} price rows to {PRICES_FILE}; recorded {history_rows:,} history points; sent {alerts_sent} Discord alert(s)")
    return True


def run_daemon():
    while True:
        try:
            sync_prices(force=False)
        except Exception as exc:
            print(f"[error] sync failed: {exc}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Check/sync once and exit")
    parser.add_argument("--force", action="store_true", help="Sync even if TCGCSV timestamp is unchanged")
    args = parser.parse_args()

    if args.once or args.force:
        sync_prices(force=args.force)
    else:
        run_daemon()
