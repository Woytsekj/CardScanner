from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
import hmac
import requests
import threading
import time

from flask import Flask, g, jsonify, render_template, request, send_file, send_from_directory, redirect, url_for, session, flash
from functools import wraps
from secrets import token_urlsafe
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlparse
from uuid import uuid4
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from flask.sessions import SecureCookieSessionInterface

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_INV = DATA_DIR / "mtg_inventory.db"
DB_ART = DATA_DIR / "mtg_art_hashes.db"
PRICES_FILE = DATA_DIR / "tcg_prices.json"
PRICE_META_FILE = DATA_DIR / "price_sync.json"
SCANNER_API_KEY = os.environ.get("SCANNER_API_KEY", "").strip()
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()
REQUIRE_LOGIN = os.environ.get("REQUIRE_LOGIN", "1").strip().lower() not in {"0", "false", "no", "off"}
PUBLIC_HTTPS = os.environ.get("PUBLIC_HTTPS", "1").strip().lower() not in {"0", "false", "no", "off"}
SESSION_LIFETIME_HOURS = max(1, int(os.environ.get("SESSION_LIFETIME_HOURS", "12")))
PUBLIC_HOSTNAME = os.environ.get("PUBLIC_HOSTNAME", "cards.stinkywar.com").strip().lower()
TRUSTED_HOSTS = {h.strip().lower() for h in os.environ.get("TRUSTED_HOSTS", "").split(",") if h.strip()}
LOCAL_HOSTS = {h.strip().lower() for h in os.environ.get("LOCAL_HOSTS", "127.0.0.1,localhost,192.168.4.27").split(",") if h.strip()}
TRUSTED_HOSTS.update(LOCAL_HOSTS)
if PUBLIC_HOSTNAME:
    TRUSTED_HOSTS.add(PUBLIC_HOSTNAME)
LOGIN_MAX_ATTEMPTS = max(3, int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5")))
LOGIN_LOCKOUT_MINUTES = max(1, int(os.environ.get("LOGIN_LOCKOUT_MINUTES", "15")))
LOGIN_WINDOW_MINUTES = max(LOGIN_LOCKOUT_MINUTES, int(os.environ.get("LOGIN_WINDOW_MINUTES", "15")))
VENDOR_USERNAME = os.environ.get("VENDOR_USERNAME", "").strip()
VENDOR_INITIAL_PASSWORD = os.environ.get("VENDOR_PASSWORD", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_INITIAL_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "America/New_York").strip() or "America/New_York"
SCRYFALL_USER_AGENT = os.environ.get("SCRYFALL_USER_AGENT", "MTGScannerInventory/1.0 (local inventory)").strip()
SCRYFALL_TIMEOUT = float(os.environ.get("SCRYFALL_TIMEOUT", "10"))
SCRYFALL_BULK_TIMEOUT = float(os.environ.get("SCRYFALL_BULK_TIMEOUT", "120"))
SCRYFALL_BULK_META_FILE = DATA_DIR / "scryfall_bulk_sync.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
PRICE_ALERT_ENABLED = os.environ.get("PRICE_ALERT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
PRICE_ALERT_LOOKBACK_DAYS = max(1, int(os.environ.get("PRICE_ALERT_LOOKBACK_DAYS", "30")))
PRICE_ALERT_COOLDOWN_DAYS = max(0, int(os.environ.get("PRICE_ALERT_COOLDOWN_DAYS", "7")))
PRICE_ALERT_MIN_DOLLARS = max(0.0, float(os.environ.get("PRICE_ALERT_MIN_DOLLARS", "2.00")))
PRICE_ALERT_MIN_PERCENT = max(0.0, float(os.environ.get("PRICE_ALERT_MIN_PERCENT", "25")))
PRICE_ALERT_MIN_OWNED_VALUE_GAIN = max(0.0, float(os.environ.get("PRICE_ALERT_MIN_OWNED_VALUE_GAIN", "10.00")))
PRICE_HISTORY_DAYS = max(30, int(os.environ.get("PRICE_HISTORY_DAYS", "365")))

# Whitelisted inventory sort expressions. These are mapped server-side so request
# parameters can never become raw SQL.
SORTS = {
    "date": "i.scan_time",
    "name": "i.name COLLATE NOCASE",
    "set": "COALESCE(i.set_code, '') COLLATE NOCASE",
    "number": "COALESCE(i.collector_number, a.collector_number) COLLATE NOCASE",
    "cmc": "COALESCE(cm.mana_value, -1)",
    "quantity": "IFNULL(i.quantity, 1)",
    "finish": "IFNULL(NULLIF(i.finish, ''), 'normal') COLLATE NOCASE",
    "owner": "IFNULL(i.owner, 'Me') COLLATE NOCASE",
    "scanned": "CAST(i.price AS REAL)",
    # Price-based sorts are whitelisted here, but calculated after the query
    # because the correct value depends on the row's Normal/Foil finish.
    "normal": "i.scan_time",
    "foil": "i.scan_time",
    "price": "i.scan_time",
    "value": "i.scan_time",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.secret_key = SESSION_SECRET
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False  # overridden per request below
app.config["SESSION_COOKIE_NAME"] = "mtg_inventory_session"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=SESSION_LIFETIME_HOURS)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

class LocalAwareSessionInterface(SecureCookieSessionInterface):
    """Use Secure cookies on HTTPS/public requests, but allow HTTP LAN access.

    The cookie remains HttpOnly + SameSite=Lax in both modes. Because the cookie
    is host-only, a local-IP cookie is not reused by the public hostname.
    """
    def get_cookie_secure(self, app):
        return bool(request.is_secure)

app.session_interface = LocalAwareSessionInterface()
# Trust forwarded client/protocol information from the local Cloudflare connector,
# but do not trust X-Forwarded-Host so the host allow-list cannot be bypassed.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


def _display_tz() -> ZoneInfo:
    try:
        return ZoneInfo(DISPLAY_TIMEZONE)
    except ZoneInfoNotFoundError:
        app.logger.warning("Unknown DISPLAY_TIMEZONE=%s; falling back to UTC", DISPLAY_TIMEZONE)
        return ZoneInfo("UTC")


def parse_stored_datetime(value: Any) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                pass
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_display_tz())


def format_local_datetime(value: Any) -> str:
    dt = parse_stored_datetime(value)
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


@app.template_filter("localdt")
def localdt_filter(value: Any) -> str:
    return format_local_datetime(value)



def connect_inventory() -> sqlite3.Connection:
    """Open an inventory connection without changing database-wide journal mode.

    journal_mode is persistent for the database and should be initialized once
    during startup, not repeatedly by concurrent request workers.
    """
    conn = sqlite3.connect(DB_INV, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def enable_inventory_wal(conn: sqlite3.Connection) -> None:
    """Enable WAL once during startup, retrying brief locks from other containers."""
    current = conn.execute("PRAGMA journal_mode").fetchone()
    if current and str(current[0]).lower() == "wal":
        return

    for attempt in range(6):
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode and str(mode[0]).lower() == "wal":
                return
            raise sqlite3.OperationalError(
                f"Could not enable SQLite WAL mode (reported mode: {mode[0] if mode else 'unknown'})"
            )
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 5:
                raise
            time.sleep(0.25 * (attempt + 1))


def normalize_finish(value: Any) -> str:
    value = str(value or "normal").strip().lower()
    return "foil" if value in {"foil", "etched", "foil etched", "etched foil"} else "normal"


def _normalize_tcgplayer_id(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        clean = str(value).strip()
        return clean or None


def ensure_card_mapping_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS card_price_mapping (
            scryfall_id TEXT PRIMARY KEY,
            tcgplayer_id TEXT,
            finishes_json TEXT,
            card_name TEXT,
            set_code TEXT,
            collector_number TEXT,
            resolved_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_card_price_mapping_tcg ON card_price_mapping(tcgplayer_id)")
    conn.commit()


def _art_db_tcgplayer_id(scryfall_id: str) -> str | None:
    if not DB_ART.exists():
        return None
    try:
        conn = sqlite3.connect(DB_ART)
        row = conn.execute("SELECT tcgplayer_id FROM card_art WHERE scryfall_id = ? LIMIT 1", (scryfall_id,)).fetchone()
        conn.close()
        return _normalize_tcgplayer_id(row[0]) if row else None
    except sqlite3.Error:
        return None


def _upsert_card_metadata(conn: sqlite3.Connection, scryfall_id: str, data: dict[str, Any]) -> None:
    colors = data.get("colors") if isinstance(data.get("colors"), list) else []
    identity = data.get("color_identity") if isinstance(data.get("color_identity"), list) else []
    legalities = data.get("legalities") if isinstance(data.get("legalities"), dict) else {}
    colors_text = "".join(str(x).upper() for x in colors)
    conn.execute(
        """INSERT INTO card_metadata
           (scryfall_id,name,type_line,colors_json,colors_text,color_identity_json,mana_value,mana_cost,oracle_text,oracle_id,legalities_json,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(scryfall_id) DO UPDATE SET
             name=excluded.name, type_line=excluded.type_line, colors_json=excluded.colors_json,
             colors_text=excluded.colors_text, color_identity_json=excluded.color_identity_json,
             mana_value=excluded.mana_value, mana_cost=excluded.mana_cost, oracle_text=excluded.oracle_text,
             oracle_id=excluded.oracle_id, legalities_json=excluded.legalities_json, updated_at=excluded.updated_at""",
        (scryfall_id, data.get("name"), data.get("type_line"), json.dumps(colors, separators=(",", ":")),
         colors_text, json.dumps(identity, separators=(",", ":")), data.get("mana_value"), data.get("mana_cost"),
         data.get("oracle_text"), data.get("oracle_id"), json.dumps(legalities, separators=(",", ":")),
         datetime.now(timezone.utc).isoformat()),
    )
    printed_name = str(data.get("printed_name") or "").strip()
    canonical_name = str(data.get("name") or "").strip()
    if printed_name and canonical_name and printed_name.casefold() != canonical_name.casefold():
        conn.execute("INSERT OR IGNORE INTO card_aliases(scryfall_id, alias) VALUES (?, ?)", (scryfall_id, printed_name))


def resolve_scryfall_mapping(scryfall_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    sid = str(scryfall_id or "").strip()
    empty = {"scryfall_id": sid, "tcgplayer_id": None, "finishes": [], "name": None, "set": None, "collector_number": None}
    if not sid:
        return empty
    own_conn = False
    if conn is None:
        conn = connect_inventory(); own_conn = True
    try:
        ensure_card_mapping_table(conn)
        cached = conn.execute("SELECT tcgplayer_id, finishes_json, card_name, set_code, collector_number FROM card_price_mapping WHERE scryfall_id=?", (sid,)).fetchone()
        if cached:
            try: finishes = json.loads(cached[1] or "[]")
            except json.JSONDecodeError: finishes = []
            return {"scryfall_id": sid, "tcgplayer_id": _normalize_tcgplayer_id(cached[0]), "finishes": finishes if isinstance(finishes,list) else [], "name": cached[2], "set": cached[3], "collector_number": cached[4]}
        local_tcg = _art_db_tcgplayer_id(sid)
        if local_tcg:
            conn.execute("INSERT OR REPLACE INTO card_price_mapping(scryfall_id,tcgplayer_id,finishes_json,card_name,set_code,collector_number,resolved_at) VALUES(?,?,?,?,?,?,?)", (sid,local_tcg,"[]",None,None,None,datetime.now(timezone.utc).isoformat()))
            conn.commit()
            return {"scryfall_id":sid,"tcgplayer_id":local_tcg,"finishes":[],"name":None,"set":None,"collector_number":None}
        response = requests.get(f"https://api.scryfall.com/cards/{sid}", headers={"User-Agent": SCRYFALL_USER_AGENT, "Accept":"application/json"}, timeout=SCRYFALL_TIMEOUT)
        response.raise_for_status(); data=response.json()
        _upsert_card_metadata(conn,sid,data)
        tcg_id=_normalize_tcgplayer_id(data.get("tcgplayer_id")); finishes=data.get("finishes") if isinstance(data.get("finishes"),list) else []
        result={"scryfall_id":sid,"tcgplayer_id":tcg_id,"finishes":finishes,"name":data.get("name"),"set":data.get("set"),"collector_number":data.get("collector_number")}
        conn.execute("INSERT OR REPLACE INTO card_price_mapping(scryfall_id,tcgplayer_id,finishes_json,card_name,set_code,collector_number,resolved_at) VALUES(?,?,?,?,?,?,?)", (sid,tcg_id,json.dumps(finishes,separators=(",",":")),data.get("name"),data.get("set"),data.get("collector_number"),datetime.now(timezone.utc).isoformat()))
        conn.commit(); return result
    except Exception as exc:
        app.logger.warning("Scryfall mapping lookup failed for %s: %s", sid, exc); return empty
    finally:
        if own_conn: conn.close()


_AUTH_READY = False


def ensure_people_and_auth(conn: sqlite3.Connection, force: bool = False) -> None:
    """Create/upgrade auth + people tables. Runs once per process (not per request)."""
    global _AUTH_READY
    if _AUTH_READY and not force:
        return
    _ensure_people_and_auth(conn)
    _AUTH_READY = True


def _ensure_people_and_auth(conn: sqlite3.Connection) -> None:
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS people (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL COLLATE NOCASE UNIQUE, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 1,
        role TEXT NOT NULL DEFAULT 'admin',
        is_superadmin INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_login_at TEXT,
        person_id INTEGER REFERENCES people(id) ON DELETE SET NULL
    )""")
    for ddl in (
        "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'",
        "ALTER TABLE users ADD COLUMN is_superadmin INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN last_login_at TEXT",
        "ALTER TABLE users ADD COLUMN person_id INTEGER REFERENCES people(id) ON DELETE SET NULL",
    ):
        try: c.execute(ddl)
        except sqlite3.OperationalError: pass
    # Each person can be the default collection for at most one web account.
    # A partial index preserves the ability to leave service/vendor accounts unlinked.
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_person_id_unique ON users(person_id) WHERE person_id IS NOT NULL")
    c.execute("UPDATE users SET role='admin' WHERE is_admin=1 AND (role IS NULL OR role='')")
    c.execute("UPDATE users SET role='vendor' WHERE is_admin=0 AND (role IS NULL OR role='')")
    c.execute("UPDATE users SET is_superadmin=1, is_admin=1, role='superadmin' WHERE is_superadmin=1")
    c.execute("""CREATE TABLE IF NOT EXISTS login_attempts (key TEXT PRIMARY KEY, first_attempt REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")

    # Seed the people table once. Do not re-create deleted people on later requests.
    bootstrapped = c.execute("SELECT value FROM app_state WHERE key='people_bootstrapped'").fetchone()
    if not bootstrapped:
        try: existing=[r[0] for r in c.execute("SELECT DISTINCT IFNULL(owner,'Me') FROM inventory WHERE TRIM(IFNULL(owner,''))<>''").fetchall()]
        except sqlite3.Error: existing=[]
        for name in existing + ["Me", "Trade Binder", "Friend"]:
            clean=str(name).strip()
            if clean: c.execute("INSERT OR IGNORE INTO people(name,active) VALUES(?,1)",(clean[:100],))
        c.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('people_bootstrapped','1')")

    user_count=c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count==0 and ADMIN_INITIAL_PASSWORD:
        c.execute("INSERT INTO users(username,password_hash,is_admin,role,is_superadmin,active) VALUES(?,?,?,?,?,1)",(ADMIN_USERNAME,generate_password_hash(ADMIN_INITIAL_PASSWORD),1,"superadmin",1))

    # If an older database has admins but no superadmin, promote the configured
    # ADMIN_USERNAME, otherwise the oldest active admin. This establishes the
    # main/highest-power account without changing other users.
    super_count=c.execute("SELECT COUNT(*) FROM users WHERE is_superadmin=1 AND active=1").fetchone()[0]
    if super_count == 0:
        candidate=c.execute("SELECT username FROM users WHERE active=1 AND is_admin=1 AND username=? ORDER BY rowid LIMIT 1", (ADMIN_USERNAME,)).fetchone()
        if not candidate:
            candidate=c.execute("SELECT username FROM users WHERE active=1 AND is_admin=1 ORDER BY rowid LIMIT 1").fetchone()
        if candidate:
            c.execute("UPDATE users SET is_superadmin=1, is_admin=1, role='superadmin' WHERE username=?", (candidate[0],))

    if VENDOR_USERNAME and VENDOR_INITIAL_PASSWORD:
        exists=c.execute("SELECT username FROM users WHERE username=?",(VENDOR_USERNAME,)).fetchone()
        if not exists:
            c.execute("INSERT INTO users(username,password_hash,is_admin,role,is_superadmin,active) VALUES(?,?,?,?,?,1)",(VENDOR_USERNAME,generate_password_hash(VENDOR_INITIAL_PASSWORD),0,"vendor",0))
    conn.commit()


def active_people(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM people WHERE active=1 ORDER BY name COLLATE NOCASE").fetchall()]

def env_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}



def safe_next_url(value: str) -> str:
    value = str(value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return ""
    return value



def ensure_csrf_token() -> str:
    token = str(session.get("csrf_token") or "").strip()
    if not token:
        token = token_urlsafe(32); session["csrf_token"] = token
    return token


def csrf_valid() -> bool:
    expected = str(session.get("csrf_token") or "")
    supplied = request.headers.get("X-CSRFToken") or request.form.get("csrf_token") or ""
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def client_ip() -> str:
    return str(request.remote_addr or "unknown")[:100]


def login_rate_key(username: str) -> str:
    return f"{client_ip()}|{username.strip().lower()[:150]}"


def check_login_rate_limits(username: str) -> tuple[bool, str]:
    now=time.time(); keys=[f"ip:{client_ip()}", f"user:{login_rate_key(username)}"]
    conn=connect_inventory()
    try:
        for key in keys:
            row=conn.execute("SELECT blocked_until FROM login_attempts WHERE key=?",(key,)).fetchone()
            if row and float(row[0] or 0)>now: return False,"Too many failed login attempts. Please wait and try again."
    finally: conn.close()
    return True,""


def record_failed_login(username: str) -> None:
    now=time.time(); conn=connect_inventory()
    try:
        for key in (f"ip:{client_ip()}", f"user:{login_rate_key(username)}"):
            row=conn.execute("SELECT first_attempt,attempts FROM login_attempts WHERE key=?",(key,)).fetchone()
            if not row or now-float(row[0])>LOGIN_WINDOW_MINUTES*60: first,attempts=now,1
            else: first,attempts=float(row[0]),int(row[1])+1
            blocked=now+LOGIN_LOCKOUT_MINUTES*60 if attempts>=LOGIN_MAX_ATTEMPTS else 0
            conn.execute("INSERT INTO login_attempts(key,first_attempt,attempts,blocked_until) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET first_attempt=excluded.first_attempt,attempts=excluded.attempts,blocked_until=excluded.blocked_until",(key,first,attempts,blocked))
        conn.execute("DELETE FROM login_attempts WHERE first_attempt<?",(now-86400,)); conn.commit()
    finally: conn.close()


def clear_login_failures(username: str) -> None:
    conn=connect_inventory(); conn.execute("DELETE FROM login_attempts WHERE key IN (?,?)",(f"ip:{client_ip()}", f"user:{login_rate_key(username)}")); conn.commit(); conn.close()

def current_user() -> sqlite3.Row | None:
    """Resolve the logged-in user once per request (cached on flask.g)."""
    username = str(session.get("username") or "").strip()
    cached = getattr(g, "_current_user", None)
    if cached is not None and cached[0] == username:
        return cached[1]
    user = _load_current_user(username)
    g._current_user = (username, user)
    return user


def _load_current_user(username: str) -> sqlite3.Row | None:
    if not username:
        return None
    try:
        conn = connect_inventory()
        ensure_people_and_auth(conn)
        user = conn.execute(
            """SELECT u.username, u.password_hash, u.is_admin, u.role, u.is_superadmin,
                      u.active, u.created_at, u.last_login_at, u.person_id,
                      p.name AS person_name
               FROM users u LEFT JOIN people p ON p.id = u.person_id
               WHERE u.username = ?""",
            (username,),
        ).fetchone()
        conn.close()
    except Exception:
        return None
    if not user or not int(user["active"]):
        session.clear()
        return None
    is_admin = bool(int(user["is_admin"]) or str(user["role"]).lower() in {"admin", "superadmin"} or int(user["is_superadmin"]))
    session["is_admin"] = is_admin
    session["is_superadmin"] = bool(int(user["is_superadmin"]) or str(user["role"]).lower() == "superadmin")
    return user


def current_admin() -> str | None:
    user = current_user()
    return str(user["username"]) if user and (int(user["is_admin"]) or int(user["is_superadmin"]) or str(user["role"]).lower() in {"admin", "superadmin"}) else None


def is_superadmin_user(user: sqlite3.Row | None) -> bool:
    return bool(user and (int(user["is_superadmin"]) or str(user["role"]).lower() == "superadmin"))


def superadmin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            if request.path.startswith("/api/") or request.method != "GET":
                return jsonify({"success": False, "error": "Login required"}), 401
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        if not is_superadmin_user(user):
            if request.path.startswith("/api/") or request.method != "GET":
                return jsonify({"success": False, "error": "Superadmin access required"}), 403
            return render_template("login.html", next_url="", error="Superadmin access required."), 403
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            if request.path.startswith("/api/") or request.method != "GET":
                return jsonify({"success": False, "error": "Login required"}), 401
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        if not int(user["is_admin"]) and str(user["role"]).lower() != "admin":
            if request.path.startswith("/api/") or request.method != "GET":
                return jsonify({"success": False, "error": "Admin access required"}), 403
            return render_template("login.html", next_url="", error="Administrator access required."), 403
        return view(*args, **kwargs)
    return wrapped


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "error": "Login required"}), 401
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        return view(*args, **kwargs)
    return wrapped


def vendor_or_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            if request.method != "GET":
                return jsonify({"success": False, "error": "Login required"}), 401
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        if int(user["is_admin"]) or str(user["role"]).lower() == "vendor":
            return view(*args, **kwargs)
        return render_template("login.html", next_url="", error="Vendor access required."), 403
    return wrapped


@app.context_processor
def inject_security_context():
    user = current_user()
    return {
        "csrf_token": ensure_csrf_token(),
        "current_user": user,
    }


def scanner_api_path_allowed(path: str) -> bool:
    """Only these low-scope endpoints may be accessed with the scanner API key.

    Browser/admin APIs remain login-protected even if someone obtains the scanner key.
    """
    return (
        path == "/api/health"
        or path == "/api/version"
        or path == "/api/people"
        or path.startswith("/api/price/")
        or path == "/api/scan"
        or path == "/api/scan/batch"
    )


@app.before_request
def security_gate():
    # Host allow-list. For the public deployment, use the exact Cloudflare hostname.
    if TRUSTED_HOSTS:
        host = str(request.host.split(":", 1)[0]).lower()
        if host not in TRUSTED_HOSTS:
            return "Bad Request", 400

    path = request.path
    if (
        path == "/api/health"
        or path.startswith("/static/")
        or request.endpoint in {"login", "login_post"}
    ):
        pass
    elif path.startswith("/api/"):
        # Browser APIs require a logged-in user. Only the explicitly listed scanner
        # endpoints can use the separate scanner API key. This keeps the scanner
        # credential from becoming a backdoor into admin-only endpoints.
        user = current_user()
        api_key_ok = (not user) and scanner_api_path_allowed(path) and check_api_key()
        if not user and not api_key_ok:
            return jsonify({"success": False, "error": "Authentication required"}), 401
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and user and not csrf_valid():
            return jsonify({"success": False, "error": "CSRF validation failed"}), 400
    elif REQUIRE_LOGIN and not current_user():
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not path.startswith("/api/") and request.endpoint != "login_post":
        if current_user() and not csrf_valid():
            return "CSRF validation failed", 400

    # Vendor accounts are deliberately restricted to vending and logout.
    user = current_user()
    if user and str(user["role"]).lower() == "vendor" and not int(user["is_admin"]):
        allowed = {"vending_view", "vending_export_csv", "vending_labels", "logout", "login", "login_post", "static"}
        if request.endpoint not in allowed and not path.startswith("/static/"):
            return render_template("login.html", next_url="", error="This account only has access to the Vending area."), 403


@app.after_request
def security_headers(response):
    # Inventory data is private; don't leave authenticated pages in shared/browser caches.
    if session.get("username"):
        response.headers.setdefault("Cache-Control", "private, no-store")
        response.headers.setdefault("Pragma", "no-cache")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    response.headers.setdefault("Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://cards.scryfall.io https://*.scryfall.com; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-src 'none'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    if PUBLIC_HTTPS and request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def migrate_db() -> None:
    conn = connect_inventory()
    # Gunicorn is started with --preload so this startup migration runs once in
    # the master process before workers fork. Avoid changing journal mode from
    # every connection, which can race and fail with "database is locked".
    enable_inventory_wal(conn)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scryfall_id TEXT,
            name TEXT NOT NULL,
            set_code TEXT,
            rarity TEXT,
            price TEXT DEFAULT '0.00',
            scan_time TEXT DEFAULT CURRENT_TIMESTAMP,
            quantity INTEGER DEFAULT 1,
            owner TEXT DEFAULT 'Me',
            finish TEXT DEFAULT 'normal',
            collector_number TEXT,
            tcgplayer_id TEXT
        )
        """
    )
    migrations = (
        ("quantity", "ALTER TABLE inventory ADD COLUMN quantity INTEGER DEFAULT 1"),
        ("owner", "ALTER TABLE inventory ADD COLUMN owner TEXT DEFAULT 'Me'"),
        ("finish", "ALTER TABLE inventory ADD COLUMN finish TEXT DEFAULT 'normal'"),
        ("collector_number", "ALTER TABLE inventory ADD COLUMN collector_number TEXT"),
        ("tcgplayer_id", "ALTER TABLE inventory ADD COLUMN tcgplayer_id TEXT"),
    )
    for _column, ddl in migrations:
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass

    c.execute("UPDATE inventory SET quantity = 1 WHERE quantity IS NULL OR quantity < 1")
    c.execute("UPDATE inventory SET owner = 'Me' WHERE owner IS NULL OR TRIM(owner) = ''")
    c.execute("UPDATE inventory SET finish = 'normal' WHERE finish IS NULL OR TRIM(finish) = ''")
    c.execute("UPDATE inventory SET finish = lower(finish)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_name ON inventory(name COLLATE NOCASE)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_owner ON inventory(owner COLLATE NOCASE)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_scryfall ON inventory(scryfall_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_finish ON inventory(finish)")
    c.execute(
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
    c.execute(
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
    c.execute("CREATE INDEX IF NOT EXISTS idx_price_history_tcg_time ON price_history(tcgplayer_id, captured_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_price_alert_tcg_finish_time ON price_alert_log(tcgplayer_id, finish, alerted_at)")
    ensure_card_mapping_table(conn)
    c.execute(
        """CREATE TABLE IF NOT EXISTS card_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scryfall_id TEXT NOT NULL,
            alias TEXT NOT NULL COLLATE NOCASE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(scryfall_id, alias)
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_card_aliases_scryfall ON card_aliases(scryfall_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_card_aliases_alias ON card_aliases(alias COLLATE NOCASE)")
    try:
        c.execute("ALTER TABLE inventory ADD COLUMN location TEXT DEFAULT 'Free'")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE inventory ADD COLUMN deck_id INTEGER")
    except sqlite3.OperationalError:
        pass
    c.execute("UPDATE inventory SET location='Free' WHERE location IS NULL OR TRIM(location)=''")

    # Vending eligibility is independent from physical storage and deck assignment.
    # Only backfill when this column is first introduced; never re-enable rows that
    # an administrator has intentionally opted out of vending on later restarts.
    existing_columns = {row[1] for row in c.execute("PRAGMA table_info(inventory)").fetchall()}
    if "for_vending" not in existing_columns:
        c.execute("ALTER TABLE inventory ADD COLUMN for_vending INTEGER NOT NULL DEFAULT 0")
        c.execute("UPDATE inventory SET for_vending=1 WHERE LOWER(TRIM(COALESCE(location,'Free')))='free'")
        c.execute("UPDATE inventory SET for_vending=0 WHERE deck_id IS NOT NULL")
    c.execute("UPDATE inventory SET for_vending=0 WHERE for_vending IS NULL")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_location ON inventory(location COLLATE NOCASE)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_deck_id ON inventory(deck_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_for_vending ON inventory(for_vending)")
    c.execute("""CREATE TABLE IF NOT EXISTS card_metadata (
        scryfall_id TEXT PRIMARY KEY, name TEXT, type_line TEXT, colors_json TEXT,
        colors_text TEXT, color_identity_json TEXT, mana_value REAL, mana_cost TEXT,
        oracle_text TEXT, oracle_id TEXT, legalities_json TEXT, updated_at TEXT NOT NULL
    )""")
    for _column, ddl in (
        ("oracle_id", "ALTER TABLE card_metadata ADD COLUMN oracle_id TEXT"),
        ("legalities_json", "ALTER TABLE card_metadata ADD COLUMN legalities_json TEXT"),
    ):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_card_metadata_oracle_id ON card_metadata(oracle_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_card_metadata_type ON card_metadata(type_line COLLATE NOCASE)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_card_metadata_colors ON card_metadata(colors_text COLLATE NOCASE)")
    c.execute("""CREATE TABLE IF NOT EXISTS decklists (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual',
        source_url TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        tags_json TEXT NOT NULL DEFAULT '[]', person_name TEXT,
        cover_filename TEXT, cover_scryfall_id TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS deck_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT, deck_id INTEGER NOT NULL, name TEXT NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 1, scryfall_id TEXT, set_code TEXT,
        collector_number TEXT, category TEXT, finish TEXT DEFAULT 'normal', tcgplayer_id TEXT,
        role_tags_json TEXT NOT NULL DEFAULT '[]',
        FOREIGN KEY(deck_id) REFERENCES decklists(id) ON DELETE CASCADE
    )""")
    for _column, ddl in (
        ("finish", "ALTER TABLE deck_cards ADD COLUMN finish TEXT DEFAULT 'normal'"),
        ("tcgplayer_id", "ALTER TABLE deck_cards ADD COLUMN tcgplayer_id TEXT"),
        ("role_tags_json", "ALTER TABLE deck_cards ADD COLUMN role_tags_json TEXT NOT NULL DEFAULT '[]'"),
    ):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass
    for _column, ddl in (
        ("tags_json", "ALTER TABLE decklists ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'"),
        ("person_name", "ALTER TABLE decklists ADD COLUMN person_name TEXT"),
        ("cover_filename", "ALTER TABLE decklists ADD COLUMN cover_filename TEXT"),
        ("cover_scryfall_id", "ALTER TABLE decklists ADD COLUMN cover_scryfall_id TEXT"),
    ):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass
    c.execute("UPDATE deck_cards SET finish='normal' WHERE finish IS NULL OR TRIM(finish)=''")
    c.execute("CREATE INDEX IF NOT EXISTS idx_decklists_person ON decklists(person_name COLLATE NOCASE)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_deck_cards_deck ON deck_cards(deck_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_deck_cards_name ON deck_cards(name COLLATE NOCASE)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_deck_cards_scryfall ON deck_cards(scryfall_id)")
    # One-time compatibility migration for the old schema, where a deck name in
    # `location` implicitly meant membership in that deck. Never repeat this on
    # subsequent starts: storage locations are now independent of deck membership.
    c.execute("CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    migrated = c.execute("SELECT 1 FROM app_migrations WHERE name='deck_location_to_deck_id_v1'").fetchone()
    if not migrated:
        try:
            c.execute(
                """UPDATE inventory SET deck_id=(SELECT d.id FROM decklists d
                       WHERE LOWER(TRIM(d.name))=LOWER(TRIM(inventory.location))
                       ORDER BY d.updated_at DESC LIMIT 1)
                   WHERE deck_id IS NULL AND LOWER(COALESCE(location,'Free')) <> 'free'
                     AND EXISTS (SELECT 1 FROM decklists d WHERE LOWER(TRIM(d.name))=LOWER(TRIM(inventory.location)))"""
            )
        except sqlite3.Error:
            pass
        c.execute("INSERT OR IGNORE INTO app_migrations(name, applied_at) VALUES('deck_location_to_deck_id_v1', ?)", (datetime.now(timezone.utc).isoformat(),))

    ensure_people_and_auth(conn)
    conn.commit()
    conn.close()


def attach_art_join(conn: sqlite3.Connection) -> str:
    art_join = (
        "LEFT JOIN (SELECT NULL AS scryfall_id, "
        "NULL AS tcgplayer_id, NULL AS collector_number) a ON 1=0"
    )

    if DB_ART.exists():
        try:
            conn.execute(
                "ATTACH DATABASE ? AS art_db",
                (str(DB_ART),),
            )
            conn.execute(
                "SELECT 1 FROM art_db.card_art LIMIT 1"
            ).fetchone()

            art_join = (
                "LEFT JOIN art_db.card_art a "
                "ON i.scryfall_id = a.scryfall_id"
            )
        except sqlite3.Error:
            try:
                conn.execute("DETACH DATABASE art_db")
            except sqlite3.Error:
                pass

    mapping_join = (
        "LEFT JOIN card_price_mapping m "
        "ON m.scryfall_id = i.scryfall_id"
    )

    return f"{art_join}\n{mapping_join}"


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_qty(value: Any, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


_PRICE_CACHE: dict[str, Any] = {"key": None, "prices": {}}
_PRICE_CACHE_LOCK = threading.Lock()


def get_tcg_prices() -> dict[str, dict[str, Any]]:
    """Return parsed TCGplayer prices, re-reading the file only when it changes.

    The snapshot is large; parsing it on every call made each page load slow.
    The cache is keyed on (mtime_ns, size), and the updater writes the file
    atomically, so a changed key means a complete new file.
    """
    try:
        st = PRICES_FILE.stat()
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    with _PRICE_CACHE_LOCK:
        if _PRICE_CACHE["key"] == key:
            return _PRICE_CACHE["prices"]
        prices = _load_tcg_prices_from_disk()
        # Do not cache a failed parse, so the next request retries.
        if prices or st.st_size == 0:
            _PRICE_CACHE["key"] = key
            _PRICE_CACHE["prices"] = prices
        return prices


def _load_tcg_prices_from_disk() -> dict[str, dict[str, Any]]:
    prices: dict[str, dict[str, Any]] = {}
    try:
        data = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        app.logger.warning("Could not load %s: %s", PRICES_FILE, exc)
        return prices

    for item in data.get("results", []):
        pid_raw = item.get("productId")
        if not pid_raw:
            continue
        try:
            pid = str(int(float(pid_raw)))
        except (TypeError, ValueError):
            continue
        entry = prices.setdefault(pid, {"normal": None, "foil": None})
        subtype = str(item.get("subTypeName") or "").lower()
        price = item.get("marketPrice")
        if price is None:
            price = item.get("midPrice")
        if price is None:
            price = item.get("lowPrice")
        if price is None:
            continue
        variant = "foil" if ("foil" in subtype or "etched" in subtype) else "normal"
        if entry[variant] is None:
            entry[variant] = price
    return prices


def get_price_meta() -> dict[str, Any]:
    if not PRICE_META_FILE.exists():
        return {}
    try:
        return json.loads(PRICE_META_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def scryfall_image_url(scryfall_id: str | None) -> str | None:
    if not scryfall_id:
        return None
    sid = str(scryfall_id).strip().lower()
    if len(sid) < 2:
        return None
    return f"https://cards.scryfall.io/normal/front/{sid[0]}/{sid[1]}/{sid}.jpg"


def card_value(item: dict[str, Any], prices: dict[str, dict[str, Any]]) -> tuple[float, float, float]:
    tcg_id = str(item.get("tcgplayer_id")) if item.get("tcgplayer_id") else None
    current = prices.get(tcg_id, {}) if tcg_id else {}
    normal = current.get("normal")
    foil = current.get("foil")
    scanned = to_float(item.get("scanned_price"))
    finish = normalize_finish(item.get("finish"))
    unit = foil if finish == "foil" else normal
    if unit is None:
        unit = scanned
    unit = to_float(unit, scanned)
    return to_float(normal, 0.0), to_float(foil, 0.0), unit


def parse_scryfall_search(search: str) -> tuple[list[str], list[Any], str | None, str | None]:
    """Parse a useful local subset of Scryfall query syntax into parameterized SQL.

    Supported card queries include free-text names/collector numbers, set:, cn:,
    name:, t:/type:, o:/oracle:, c:, r:/rarity:, is:foil/nonfoil, and common
    collection helpers owner:, loc:/location:, deck:, and vending:true/false.
    order:set/name/collector/usd and direction:asc/desc can also be used.
    Unsupported qualifiers fall back to ordinary text search rather than SQL.
    """
    where: list[str] = []
    args: list[Any] = []
    order_override: str | None = None
    direction_override: str | None = None
    token_pattern = re.compile(
        r'-?[A-Za-z][A-Za-z0-9_]*(?:>=|<=|!=|=|:|>|<)"(?:\\.|[^"\\])*"'
        r'|-?[A-Za-z][A-Za-z0-9_]*(?:>=|<=|!=|=|:|>|<)\S+'
        r'|-(?:"(?:\\.|[^"\\])*"|\S+)'
        r'|"(?:\\.|[^"\\])*"|\S+'
    )
    tokens = token_pattern.findall(search or "")
    rarity_rank = {"common": 1, "uncommon": 2, "rare": 3, "mythic": 4, "mythic rare": 4}

    def append(expr: str, values: list[Any], negated: bool) -> None:
        where.append(f"NOT ({expr})" if negated else f"({expr})")
        args.extend(values)

    for raw in tokens:
        negated = raw.startswith("-")
        token = raw[1:] if negated else raw
        field = None
        operator = None
        value = None
        for op in (">=", "<=", "!=", ":", "=", ">", "<"):
            if op in token:
                possible_field, possible_value = token.split(op, 1)
                if possible_field and possible_value and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", possible_field):
                    field, operator, value = possible_field.lower(), op, possible_value
                    break
        if value is not None:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
            if field == "order":
                order_map = {"set": "set", "name": "name", "collector": "number", "number": "number", "usd": "price", "price": "price", "cmc": "cmc", "date": "date", "released": "date"}
                order_override = order_map.get(value.lower(), order_override)
                continue
            if field in {"direction", "dir"}:
                if value.lower() in {"asc", "desc"}:
                    direction_override = value.lower()
                continue
            if field in {"set", "s"}:
                pattern = value.replace("*", "%")
                if "%" in pattern or "_" in pattern:
                    append("COALESCE(i.set_code, '') LIKE ? COLLATE NOCASE", [pattern], negated)
                else:
                    append("COALESCE(i.set_code, '') = ? COLLATE NOCASE", [value.upper()], negated)
                continue
            if field in {"cn", "collector", "collector_number"}:
                col = "COALESCE(CAST(COALESCE(i.collector_number, a.collector_number) AS TEXT), '')"
                pattern = value.replace("*", "%")
                if "%" in pattern or "_" in pattern:
                    append(f"{col} LIKE ? COLLATE NOCASE", [pattern], negated)
                else:
                    append(f"{col} = ? COLLATE NOCASE", [value], negated)
                continue
            if field in {"name", "card"}:
                append("i.name LIKE ? COLLATE NOCASE", [f"%{value}%"], negated)
                continue
            if field in {"t", "type"}:
                parts = [part for part in re.split(r"\s+", value) if part]
                expr = " AND ".join("COALESCE(cm.type_line, '') LIKE ? COLLATE NOCASE" for _ in parts) or "1=1"
                append(expr, [f"%{part}%" for part in parts], negated)
                continue
            if field in {"o", "oracle"}:
                append("COALESCE(cm.oracle_text, '') LIKE ? COLLATE NOCASE", [f"%{value.replace('*', '%')}%" if "*" not in value else f"%{value.replace('*', '%')}"], negated)
                continue
            if field in {"c", "color", "colors"}:
                color_value = value.lower().replace(" ", "")
                if color_value in {"colorless", "c"}:
                    expr, vals = "COALESCE(cm.colors_text, '') = ''", []
                elif color_value in {"multicolor", "multi"}:
                    expr, vals = "length(COALESCE(cm.colors_text, '')) > 1", []
                else:
                    symbols = [ch.upper() for ch in color_value if ch.upper() in "WUBRG"]
                    if not symbols:
                        expr, vals = "1=0", []
                    elif operator in {"=", "!="}:
                        expr, vals = "UPPER(COALESCE(cm.colors_text, '')) = ?", ["".join(ch for ch in "WUBRG" if ch in symbols)]
                    else:
                        expr = " AND ".join("UPPER(COALESCE(cm.colors_text, '')) LIKE ?" for _ in symbols)
                        vals = [f"%{ch}%" for ch in symbols]
                append(expr, vals, negated or operator == "!=")
                continue
            if field in {"r", "rarity"}:
                rank_expr = "CASE LOWER(COALESCE(i.rarity,'')) WHEN 'common' THEN 1 WHEN 'uncommon' THEN 2 WHEN 'rare' THEN 3 WHEN 'mythic' THEN 4 WHEN 'mythic rare' THEN 4 ELSE 0 END"
                rarity = rarity_rank.get(value.lower())
                if operator in {">=", "<=", ">", "<", "=", "!="} and rarity is not None:
                    sql_op = "=" if operator == ":" else operator
                    append(f"{rank_expr} {sql_op} ?", [rarity], negated)
                elif operator in {":", "="}:
                    append("LOWER(COALESCE(i.rarity,'')) = ?", [value.lower()], negated)
                else:
                    append("1=0", [], negated)
                continue
            if field == "is":
                flag = value.lower()
                if flag in {"foil", "etched"}:
                    append("LOWER(COALESCE(i.finish,'normal')) IN ('foil','etched','etched foil','foil etched')", [], negated)
                elif flag in {"nonfoil", "normal"}:
                    append("LOWER(COALESCE(i.finish,'normal')) NOT IN ('foil','etched','etched foil','foil etched')", [], negated)
                else:
                    # Unknown is: qualifiers fall back to text search, rather than falsely matching everything.
                    needle = f"%{token}%"
                    append("(i.name LIKE ? COLLATE NOCASE OR COALESCE(i.collector_number, a.collector_number) LIKE ? COLLATE NOCASE)", [needle, needle], negated)
                continue
            if field == "owner":
                append("COALESCE(i.owner,'Me') = ? COLLATE NOCASE", [value], negated or operator == "!=")
                continue
            if field in {"loc", "location"}:
                append("COALESCE(i.location,'Free') = ? COLLATE NOCASE", [value], negated or operator == "!=")
                continue
            if field == "deck":
                if value.lower() in {"none", "unassigned", "free"}:
                    append("i.deck_id IS NOT NULL", [], not (negated or operator == "!="))
                else:
                    append("i.deck_id = (SELECT d.id FROM decklists d WHERE d.name = ? COLLATE NOCASE ORDER BY d.updated_at DESC LIMIT 1)", [value], negated or operator == "!=")
                continue
            if field == "vending":
                want = value.lower() in {"1", "true", "yes", "on", "available"}
                append("COALESCE(i.for_vending,0) = ?", [1 if want else 0], negated or operator == "!=")
                continue
            # Unknown key:value terms remain searchable as text.
            needle = f"%{token}%"
            append("(i.name LIKE ? COLLATE NOCASE OR COALESCE(i.collector_number, a.collector_number) LIKE ? COLLATE NOCASE OR COALESCE(i.set_code,'') LIKE ? COLLATE NOCASE)", [needle, needle, needle], negated)
            continue

        # Plain words/quoted phrases search card name, collector number, and aliases.
        plain = token.strip('"')
        if not plain:
            continue
        needle = f"%{plain}%"
        append(
            "(i.name LIKE ? COLLATE NOCASE OR COALESCE(i.collector_number, a.collector_number) LIKE ? COLLATE NOCASE OR EXISTS (SELECT 1 FROM card_aliases ca WHERE ca.scryfall_id = i.scryfall_id AND ca.alias LIKE ? COLLATE NOCASE))",
            [needle, needle, needle], negated,
        )
    return where, args, order_override, direction_override


def inventory_rows(search: str, owner_filter: str, finish_filter: str, sort_key: str, direction: str, type_filter: str = "", color_filter: str = "", location_filter: str = "", vending_filter: str = ""):
    prices = get_tcg_prices()
    conn = connect_inventory()
    join_sql = attach_art_join(conn)

    where, args, query_order, query_direction = parse_scryfall_search(search)
    if query_order:
        sort_key = query_order
    if query_direction:
        direction = query_direction
    if owner_filter:
        where.append("IFNULL(i.owner, 'Me') = ?")
        args.append(owner_filter)
    if finish_filter:
        where.append("IFNULL(NULLIF(i.finish, ''), 'normal') = ?")
        args.append(finish_filter)
    if type_filter:
        where.append("COALESCE(cm.type_line, '') LIKE ? COLLATE NOCASE")
        args.append(f"%{type_filter}%")
    if color_filter == "multicolor":
        where.append("length(COALESCE(cm.colors_text, '')) > 1")
    elif color_filter == "colorless":
        where.append("COALESCE(cm.colors_text, '') = ''")
    elif color_filter:
        where.append("COALESCE(cm.colors_text, '') LIKE ? COLLATE NOCASE")
        args.append(f"%{color_filter}%")
    if location_filter:
        where.append("COALESCE(i.location, 'Free') = ? COLLATE NOCASE")
        args.append(location_filter)
    if vending_filter == "yes":
        where.append("COALESCE(i.for_vending,0) = 1")
    elif vending_filter == "no":
        where.append("COALESCE(i.for_vending,0) = 0")
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    sort_key = sort_key if sort_key in SORTS else "date"
    direction_sql = "ASC" if direction == "asc" else "DESC"
    price_sort = sort_key in {"normal", "foil", "price", "value"}
    sql_sort = SORTS[sort_key] if not price_sort else "i.scan_time"
    if sort_key == "set":
        order_sql = (
            f"COALESCE(i.set_code, '') COLLATE NOCASE {direction_sql}, "
            f"CAST(COALESCE(i.collector_number, a.collector_number, '0') AS INTEGER) {direction_sql}, "
            f"COALESCE(CAST(COALESCE(i.collector_number, a.collector_number) AS TEXT), '') COLLATE NOCASE {direction_sql}"
        )
    else:
        order_sql = f"{sql_sort} {direction_sql}"

    query = f"""
        SELECT i.id, i.scryfall_id, i.name, i.set_code, i.rarity,
               i.price AS scanned_price, i.scan_time,
               IFNULL(i.quantity, 1) AS quantity, IFNULL(i.owner, 'Me') AS owner,
               IFNULL(NULLIF(i.finish, ''), 'normal') AS finish,
               COALESCE(i.collector_number, a.collector_number) AS collector_number,
               COALESCE(
                    NULLIF(TRIM(i.tcgplayer_id), ''),
                    NULLIF(TRIM(a.tcgplayer_id), ''),
                    NULLIF(TRIM(m.tcgplayer_id), '')
                 ) AS tcgplayer_id,
               COALESCE(i.location, 'Free') AS location,
               i.deck_id, COALESCE(i.for_vending, 0) AS for_vending,
               cm.type_line, cm.colors_text, cm.mana_value, cm.oracle_text, cm.oracle_id, cm.legalities_json, cm.updated_at AS metadata_updated_at,
               COALESCE((SELECT GROUP_CONCAT(ca.alias, ' · ') FROM card_aliases ca WHERE ca.scryfall_id = i.scryfall_id), '') AS alternate_names
        FROM inventory i
        {join_sql}
        LEFT JOIN card_metadata cm ON cm.scryfall_id = i.scryfall_id
        {where_sql}
        ORDER BY {order_sql}, i.id ASC
    """
    rows = conn.execute(query, args).fetchall()

    all_count_rows = conn.execute(
        """
        SELECT scryfall_id, IFNULL(owner, 'Me') owner,
               IFNULL(NULLIF(finish, ''), 'normal') finish,
               SUM(IFNULL(quantity,1)) qty
        FROM inventory
        GROUP BY scryfall_id, owner, finish
        """
    ).fetchall()
    conn.close()

    owner_finish: dict[tuple[str, str], dict[str, int]] = {}
    card_finish: dict[str, dict[str, int]] = {}
    for r in all_count_rows:
        sid = r["scryfall_id"] or ""
        owner = r["owner"] or "Me"
        finish = normalize_finish(r["finish"])
        qty = int(r["qty"] or 0)
        owner_finish.setdefault((sid, owner), {"normal": 0, "foil": 0})[finish] += qty
        card_finish.setdefault(sid, {"normal": 0, "foil": 0})[finish] += qty

    inventory: list[dict[str, Any]] = []
    owner_totals: dict[str, float] = {}
    owner_counts: dict[str, dict[str, int]] = {}
    grand_total = 0.0
    finish_totals = {"normal": 0, "foil": 0}

    for row in rows:
        item = dict(row)
        item["finish"] = normalize_finish(item.get("finish"))
        item["quantity"] = parse_qty(item.get("quantity"))
        item["for_vending"] = bool(int(item.get("for_vending") or 0))
        normal_price, foil_price, unit_value = card_value(item, prices)
        item["curr_normal"] = normal_price if normal_price else None
        item["curr_foil"] = foil_price if foil_price else None
        item["calculated_unit_price"] = unit_value
        item["calculated_total_value"] = unit_value * item["quantity"]
        item["image_url"] = scryfall_image_url(item.get("scryfall_id"))
        item["alternate_names"] = str(item.get("alternate_names") or "")
        item["location"] = str(item.get("location") or "Free")
        item["type_line"] = str(item.get("type_line") or "")
        item["colors_text"] = str(item.get("colors_text") or "")
        item["oracle_text"] = str(item.get("oracle_text") or "")
        item["oracle_id"] = str(item.get("oracle_id") or "")
        item["legalities_json"] = str(item.get("legalities_json") or "{}")
        item["metadata_available"] = bool(item["oracle_text"] or item["type_line"] or item["legalities_json"] not in ("", "{}"))

        owner = item["owner"] or "Me"
        owner_totals[owner] = owner_totals.get(owner, 0.0) + item["calculated_total_value"]
        counts = owner_finish.get((item.get("scryfall_id") or "", owner), {"normal": 0, "foil": 0})
        card_counts = card_finish.get(item.get("scryfall_id") or "", {"normal": 0, "foil": 0})
        item["owner_normal_qty"] = counts["normal"]
        item["owner_foil_qty"] = counts["foil"]
        item["card_normal_qty"] = card_counts["normal"]
        item["card_foil_qty"] = card_counts["foil"]
        owner_counts.setdefault(owner, {"normal": 0, "foil": 0})
        owner_counts[owner][item["finish"]] += item["quantity"]

        finish_totals[item["finish"]] += item["quantity"]
        grand_total += item["calculated_total_value"]
        inventory.append(item)

    if sort_key in {"normal", "foil", "price", "value"}:
        key_name = {"normal": "curr_normal", "foil": "curr_foil", "price": "calculated_unit_price", "value": "calculated_total_value"}[sort_key]
        inventory.sort(key=lambda x: to_float(x.get(key_name)), reverse=(direction_sql == "DESC"))

    return inventory, owner_totals, owner_counts, grand_total, finish_totals, len(inventory)


def check_api_key() -> bool:
    if not SCANNER_API_KEY:
        return False
    supplied = request.headers.get("X-Scanner-API-Key", "")
    return hmac.compare_digest(supplied, SCANNER_API_KEY)


def insert_or_increment_scan(payload: dict[str, Any]) -> dict[str, Any]:
    scryfall_id = str(payload.get("scryfall_id") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not scryfall_id or not name:
        raise ValueError("scryfall_id and name are required")

    owner = str(payload.get("owner") or "Me").strip()[:100] or "Me"
    finish = normalize_finish(payload.get("finish"))

    owner_conn = connect_inventory()
    ensure_people_and_auth(owner_conn)
    valid_owner = owner_conn.execute("SELECT 1 FROM people WHERE name = ? AND active=1", (owner,)).fetchone()
    owner_conn.close()
    if not valid_owner:
        raise ValueError(f"Owner '{owner}' is not an active person on the server")
    qty = parse_qty(payload.get("quantity"))
    price = str(payload.get("price") if payload.get("price") is not None else "0.00")
    set_code = str(payload.get("set_code") or "").upper()
    rarity = str(payload.get("rarity") or "Common")
    collector_number = str(payload.get("collector_number") or "").strip() or None
    tcgplayer_id = str(payload.get("tcgplayer_id") or "").strip() or None
    location = str(payload.get("location") or "Free").strip()[:120] or "Free"
    scan_time = str(payload.get("scan_time") or datetime.now(timezone.utc).isoformat())
    # Optional client-generated id. If a scan was saved but the response was lost,
    # the scanner's retry carries the same id and is acknowledged without
    # incrementing the quantity a second time.
    scan_id = str(payload.get("scan_id") or "").strip()[:64] or None

    conn = connect_inventory()
    if scan_id:
        conn.execute("CREATE TABLE IF NOT EXISTS scan_log (scan_id TEXT PRIMARY KEY, row_id INTEGER, quantity INTEGER, created_at TEXT NOT NULL)")
        seen = conn.execute("SELECT row_id, quantity FROM scan_log WHERE scan_id=?", (scan_id,)).fetchone()
        if seen:
            conn.close()
            return {"success": True, "id": seen["row_id"], "quantity": seen["quantity"], "owner": owner,
                    "finish": finish, "name": name, "duplicate": True}
    if not tcgplayer_id and scryfall_id:
        mapping = resolve_scryfall_mapping(scryfall_id, conn)
        tcgplayer_id = mapping.get("tcgplayer_id")
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT id, quantity FROM inventory
            WHERE scryfall_id = ? AND owner = ? AND IFNULL(NULLIF(finish,''),'normal') = ?
              AND COALESCE(location,'Free') = ? AND deck_id IS NULL
              AND COALESCE(for_vending,0) = 0
            ORDER BY id LIMIT 1
            """,
            (scryfall_id, owner, finish, location),
        ).fetchone()
        if existing:
            new_qty = int(existing["quantity"] or 0) + qty
            conn.execute(
                """
                UPDATE inventory
                SET quantity = ?, scan_time = ?, price = ?,
                    collector_number = COALESCE(?, collector_number),
                    tcgplayer_id = COALESCE(?, tcgplayer_id)
                WHERE id = ?
                """,
                (new_qty, scan_time, price, collector_number, tcgplayer_id, existing["id"]),
            )
            row_id = existing["id"]
        else:
            cur = conn.execute(
                """
                INSERT INTO inventory
                    (scryfall_id, name, set_code, rarity, price, owner, quantity, scan_time, finish, collector_number, tcgplayer_id, location, for_vending)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (scryfall_id, name, set_code, rarity, price, owner, qty, scan_time, finish, collector_number, tcgplayer_id, location, 0),
            )
            row_id = cur.lastrowid
            new_qty = qty
        if scan_id:
            conn.execute("INSERT OR IGNORE INTO scan_log(scan_id,row_id,quantity,created_at) VALUES(?,?,?,?)",
                         (scan_id, row_id, new_qty, datetime.now(timezone.utc).isoformat()))
            conn.execute("DELETE FROM scan_log WHERE created_at < ?", ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "success": True,
        "id": row_id,
        "quantity": new_qty,
        "owner": owner,
        "finish": finish,
        "name": name,
    }


@app.get("/login")
def login():
    user = current_user()
    if user:
        if int(user["is_admin"]) or str(user["role"]).lower() == "admin":
            return redirect(url_for("index"))
        return redirect(url_for("vending_view"))
    return render_template("login.html", next_url=safe_next_url(request.args.get("next", "")), error=request.args.get("error", ""))


@app.post("/login")
def login_post():
    username = str(request.form.get("username") or "").strip()[:150]
    password = str(request.form.get("password") or "")
    next_url = safe_next_url(request.form.get("next", ""))
    if not csrf_valid():
        flash("Your login page expired. Please try again.", "error")
        return redirect(url_for("login", next=next_url))
    allowed, rate_msg = check_login_rate_limits(username)
    if not allowed:
        flash("Too many failed login attempts. Please wait and try again.", "error")
        return redirect(url_for("login", next=next_url))

    conn = connect_inventory()
    ensure_people_and_auth(conn)
    user = conn.execute("SELECT username, password_hash, is_admin, role, is_superadmin, active FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not int(user["active"]) or not check_password_hash(user["password_hash"], password):
        conn.close()
        record_failed_login(username)
        flash("Invalid username or password.", "error")
        return redirect(url_for("login", next=next_url))
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE users SET last_login_at=? WHERE username=?", (now_iso, user["username"]))
    conn.commit(); conn.close()
    clear_login_failures(username)
    session.clear()
    session.permanent = True
    session["username"] = user["username"]
    session["is_admin"] = bool(int(user["is_admin"]) or int(user["is_superadmin"]) or str(user["role"]).lower() in {"admin", "superadmin"})
    session["is_superadmin"] = bool(int(user["is_superadmin"]) or str(user["role"]).lower() == "superadmin")
    session["csrf_token"] = token_urlsafe(32)
    if int(user["is_admin"]) or int(user["is_superadmin"]) or str(user["role"]).lower() in {"admin", "superadmin"}:
        return redirect(next_url if next_url else url_for("index"))
    return redirect(next_url if next_url and next_url.startswith("/vending") else url_for("vending_view"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/admin")
@admin_required
def admin_page():
    conn = connect_inventory()
    ensure_people_and_auth(conn)
    people = conn.execute("""SELECT p.id, p.name, p.active, p.created_at,
               COALESCE((SELECT SUM(IFNULL(i.quantity,1)) FROM inventory i WHERE IFNULL(i.owner, 'Me') = p.name),0) AS card_count
               FROM people p ORDER BY p.active DESC, p.name COLLATE NOCASE""").fetchall()
    users = conn.execute(
        """SELECT u.username, u.is_admin, u.role, u.is_superadmin, u.active,
                  u.created_at, u.last_login_at, u.person_id, p.name AS person_name
           FROM users u LEFT JOIN people p ON p.id = u.person_id
           ORDER BY u.is_superadmin DESC, u.username COLLATE NOCASE"""
    ).fetchall()
    conn.close()
    return render_template(
        "admin.html",
        people=people,
        users=users,
        account_people=[p for p in people if p["active"]],
        admin_user=current_admin(),
        admin_is_superadmin=is_superadmin_user(current_user()),
        message=request.args.get("message", ""),
        display_timezone=DISPLAY_TIMEZONE,
        price_history_days=PRICE_HISTORY_DAYS,
        discord_configured=bool(DISCORD_WEBHOOK_URL),
        price_alert_enabled=PRICE_ALERT_ENABLED,
        alert_lookback_days=PRICE_ALERT_LOOKBACK_DAYS,
        alert_cooldown_days=PRICE_ALERT_COOLDOWN_DAYS,
        alert_min_dollars=PRICE_ALERT_MIN_DOLLARS,
        alert_min_percent=PRICE_ALERT_MIN_PERCENT,
        alert_min_owned_gain=PRICE_ALERT_MIN_OWNED_VALUE_GAIN,
        scryfall_bulk_meta=get_scryfall_bulk_meta(),
    )


def _like_pattern(value: str) -> str:
    """Return a simple contains pattern while preventing wildcard characters from acting as wildcards."""
    safe = value.replace("%", "").replace("_", "")
    return f"%{safe}%"


def _prefix_pattern(value: str) -> str:
    """Return a simple prefix pattern while preventing wildcard characters from acting as wildcards."""
    safe = value.replace("%", "").replace("_", "")
    return f"{safe}%"


def build_inventory_search_index(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a compact client-side autocomplete index from all owned printings.

    Keeping this index in the rendered page makes main-page predictive search
    independent of a second AJAX request. Alternate names are included.
    """
    merged: dict[str, dict[str, Any]] = {}
    for item in rows:
        sid = str(item.get("scryfall_id") or "").strip()
        if not sid:
            continue
        entry = merged.get(sid)
        if entry is None:
            entry = {
                "scryfall_id": sid,
                "name": item.get("name") or "",
                "set_code": item.get("set_code") or "",
                "collector_number": item.get("collector_number") or "",
                "quantity": 0,
                "alternate_names": [],
                "image_url": item.get("image_url"),
            }
            merged[sid] = entry
        entry["quantity"] += int(item.get("quantity") or 0)
        for alias in str(item.get("alternate_names") or "").split(" · "):
            alias = alias.strip()
            if alias and alias.casefold() not in {a.casefold() for a in entry["alternate_names"]}:
                entry["alternate_names"].append(alias)
        if not entry.get("image_url") and item.get("image_url"):
            entry["image_url"] = item.get("image_url")
    return list(merged.values())

def _catalog_search_results(q: str, limit: int = 12) -> list[dict[str, Any]]:
    if not q or not DB_ART.exists():
        return []

    contains = _like_pattern(q)
    prefix = _prefix_pattern(q)
    limit = max(1, min(int(limit), 40))
    try:
        conn = sqlite3.connect(DB_ART)
        conn.row_factory = sqlite3.Row
        cols = "scryfall_id, name, set_code, rarity, tcgplayer_id, collector_number"
        rows = conn.execute(
            f"""SELECT {cols},
                       CASE
                         WHEN name LIKE ? COLLATE NOCASE THEN 0
                         WHEN set_code LIKE ? COLLATE NOCASE THEN 1
                         WHEN collector_number LIKE ? COLLATE NOCASE THEN 2
                         ELSE 3
                       END AS match_rank
                FROM card_art
                WHERE name LIKE ? COLLATE NOCASE
                   OR set_code LIKE ? COLLATE NOCASE
                   OR collector_number LIKE ? COLLATE NOCASE
                ORDER BY match_rank, name COLLATE NOCASE, set_code COLLATE NOCASE, collector_number COLLATE NOCASE
                LIMIT ?""",
            (prefix, prefix, prefix, contains, contains, contains, limit),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Card catalog search failed: {exc}") from exc

    return [
        {
            "scryfall_id": row["scryfall_id"],
            "name": row["name"],
            "set_code": row["set_code"],
            "rarity": row["rarity"],
            "tcgplayer_id": row["tcgplayer_id"],
            "collector_number": row["collector_number"],
            "image_url": scryfall_image_url(row["scryfall_id"]),
        }
        for row in rows
    ]


@app.get("/admin/catalog_search")
@admin_required
def catalog_search():
    """Search the local card catalog (mtg_art_hashes.db) for manual inventory adds."""
    q = str(request.args.get("q") or "").strip()[:120]
    try:
        results = _catalog_search_results(q, limit=40)
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 500
    return jsonify({"success": True, "results": results})


@app.get("/admin/catalog_suggest")
@admin_required
def catalog_suggest():
    """Small live-autocomplete search for the manual-add catalog box."""
    q = str(request.args.get("q") or "").strip()[:80]
    if len(q) < 2:
        return jsonify({"success": True, "results": []})
    try:
        results = _catalog_search_results(q, limit=10)
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 500
    return jsonify({"success": True, "results": results})


@app.get("/search_suggest")
def search_suggest():
    """Autocomplete suggestions from cards in inventory, including alternate names."""
    q = str(request.args.get("q") or "").strip()[:80]
    if len(q) < 2:
        return jsonify({"success": True, "results": []})

    contains = _like_pattern(q)
    prefix = _prefix_pattern(q)
    conn = connect_inventory()
    join_sql = attach_art_join(conn)
    try:
        rows = conn.execute(
            f"""SELECT i.scryfall_id, i.name, i.set_code,
                      COALESCE(i.collector_number, a.collector_number) AS collector_number,
                      SUM(COALESCE(i.quantity,1)) AS total_quantity,
                      COALESCE((SELECT GROUP_CONCAT(ca.alias, ' · ') FROM card_aliases ca WHERE ca.scryfall_id = i.scryfall_id), '') AS alternate_names,
                      (SELECT ca.alias FROM card_aliases ca WHERE ca.scryfall_id = i.scryfall_id AND ca.alias LIKE ? COLLATE NOCASE ORDER BY CASE WHEN ca.alias LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END, ca.alias COLLATE NOCASE LIMIT 1) AS matched_alias,
                      CASE
                        WHEN i.name LIKE ? COLLATE NOCASE THEN 0
                        WHEN EXISTS (SELECT 1 FROM card_aliases ca2 WHERE ca2.scryfall_id=i.scryfall_id AND ca2.alias LIKE ? COLLATE NOCASE) THEN 1
                        WHEN COALESCE(i.collector_number, a.collector_number) LIKE ? COLLATE NOCASE THEN 2
                        ELSE 3
                      END AS match_rank
               FROM inventory i
               {join_sql}
               WHERE i.name LIKE ? COLLATE NOCASE
                  OR COALESCE(i.collector_number, a.collector_number) LIKE ? COLLATE NOCASE
                  OR EXISTS (SELECT 1 FROM card_aliases ca3 WHERE ca3.scryfall_id=i.scryfall_id AND ca3.alias LIKE ? COLLATE NOCASE)
               GROUP BY i.scryfall_id, i.name, i.set_code,
                        COALESCE(i.collector_number, a.collector_number)
               ORDER BY match_rank, i.name COLLATE NOCASE, i.set_code COLLATE NOCASE,
                        COALESCE(i.collector_number, a.collector_number) COLLATE NOCASE
               LIMIT 12""",
            (contains, prefix, prefix, prefix, prefix, contains, contains, contains),
        ).fetchall()
    finally:
        conn.close()

    return jsonify({
        "success": True,
        "results": [
            {
                "scryfall_id": row["scryfall_id"],
                "name": row["name"],
                "set_code": row["set_code"],
                "collector_number": row["collector_number"],
                "quantity": int(row["total_quantity"] or 0),
                "alternate_names": row["alternate_names"] or "",
                "matched_alias": row["matched_alias"],
                "image_url": scryfall_image_url(row["scryfall_id"]),
            }
            for row in rows
        ],
    })


@app.get("/admin/aliases")
@admin_required
def list_aliases():
    scryfall_id = str(request.args.get("scryfall_id") or "").strip()
    if not scryfall_id:
        return jsonify({"success": True, "aliases": []})
    conn = connect_inventory()
    rows = conn.execute(
        "SELECT id, alias, created_at FROM card_aliases WHERE scryfall_id = ? ORDER BY alias COLLATE NOCASE",
        (scryfall_id,),
    ).fetchall()
    conn.close()
    return jsonify({"success": True, "aliases": [dict(r) for r in rows]})


@app.post("/admin/aliases")
@admin_required
def add_alias():
    scryfall_id = str(request.form.get("scryfall_id") or "").strip()[:80]
    alias = " ".join(str(request.form.get("alias") or "").strip().split())[:200]
    if not scryfall_id or not alias:
        return redirect(url_for("admin_page", message="Choose a card and enter an alternate name."))
    conn = connect_inventory()
    try:
        # Require the card to exist in inventory so aliases cannot become orphaned.
        exists = conn.execute("SELECT 1 FROM inventory WHERE scryfall_id = ? LIMIT 1", (scryfall_id,)).fetchone()
        if not exists:
            return redirect(url_for("admin_page", message="That card is not in your inventory."))
        conn.execute("INSERT OR IGNORE INTO card_aliases(scryfall_id, alias) VALUES (?,?)", (scryfall_id, alias))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin_page", message=f'Alternate name added: {alias}'))


@app.delete("/admin/aliases/<int:alias_id>")
@admin_required
def delete_alias(alias_id: int):
    conn = connect_inventory()
    cur = conn.execute("DELETE FROM card_aliases WHERE id = ?", (alias_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": cur.rowcount > 0})


@app.post("/admin/manual_add")
@admin_required
def manual_add():
    """Add a card directly to inventory, optionally using metadata from the local catalog."""
    name = str(request.form.get("name") or "").strip()[:200]
    if not name:
        return redirect(url_for("admin_page", message="Card name is required."))

    owner = str(request.form.get("owner") or "").strip()[:100]
    finish = normalize_finish(request.form.get("finish"))
    qty_raw = request.form.get("quantity", "1")
    try:
        qty = int(qty_raw)
    except (TypeError, ValueError):
        return redirect(url_for("admin_page", message="Quantity must be a whole number."))
    if qty < 1:
        return redirect(url_for("admin_page", message="Quantity must be at least 1."))

    scryfall_id = str(request.form.get("scryfall_id") or "").strip()
    set_code = str(request.form.get("set_code") or "").strip().upper()[:20]
    collector_number = str(request.form.get("collector_number") or "").strip()[:40] or None
    rarity = str(request.form.get("rarity") or "Common").strip()[:40]
    tcgplayer_id = str(request.form.get("tcgplayer_id") or "").strip()[:40] or None
    price = str(request.form.get("price") or "0.00").strip()[:32]
    location = str(request.form.get("location") or "Free").strip()[:120] or "Free"

    conn = connect_inventory()
    ensure_people_and_auth(conn)
    valid_owner = conn.execute("SELECT 1 FROM people WHERE name = ? AND active=1", (owner,)).fetchone()
    if not valid_owner:
        conn.close()
        return redirect(url_for("admin_page", message="Choose an active owner."))

    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = None
        if scryfall_id:
            existing = conn.execute(
                """SELECT id, quantity FROM inventory
                   WHERE scryfall_id = ? AND owner = ? AND IFNULL(NULLIF(finish,''),'normal') = ?
                     AND COALESCE(location,'Free') = ? AND deck_id IS NULL
                     AND COALESCE(for_vending,0) = 0
                   ORDER BY id LIMIT 1""",
                (scryfall_id, owner, finish, location),
            ).fetchone()
        if existing:
            new_qty = int(existing["quantity"] or 0) + qty
            conn.execute(
                """UPDATE inventory SET quantity=?, price=?, set_code=COALESCE(NULLIF(?,''),set_code),
                   rarity=COALESCE(NULLIF(?,''),rarity), collector_number=COALESCE(?,collector_number),
                   tcgplayer_id=COALESCE(?,tcgplayer_id), scan_time=? WHERE id=?""",
                (new_qty, price, set_code, rarity, collector_number, tcgplayer_id, datetime.now(timezone.utc).isoformat(), existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO inventory
                   (scryfall_id,name,set_code,rarity,price,owner,quantity,scan_time,finish,collector_number,tcgplayer_id,location,for_vending)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (scryfall_id or None, name, set_code, rarity, price, owner, qty,
                 datetime.now(timezone.utc).isoformat(), finish, collector_number, tcgplayer_id, location, 0),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        return redirect(url_for("admin_page", message=f"Manual add failed: {exc}"))
    conn.close()
    return redirect(url_for("admin_page", message=f"Added {qty} × {name} ({finish})."))


@app.post("/admin/people")
@admin_required
def add_person():
    name = str(request.form.get("name") or "").strip()[:100]
    if not name:
        return redirect(url_for("admin_page"))
    conn = connect_inventory()
    ensure_people_and_auth(conn)
    try:
        conn.execute("INSERT INTO people(name, active) VALUES (?,1)", (name,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.execute("UPDATE people SET active=1 WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin_page", message=f"Added {name}"))


@app.post("/admin/people/<int:person_id>/toggle")
@admin_required
def toggle_person(person_id: int):
    conn = connect_inventory()
    row = conn.execute("SELECT active FROM people WHERE id = ?", (person_id,)).fetchone()
    if row:
        conn.execute("UPDATE people SET active = ? WHERE id = ?", (0 if row["active"] else 1, person_id))
        conn.commit()
    conn.close()
    return redirect(url_for("admin_page"))


@app.post("/admin/people/<int:person_id>/rename")
@admin_required
def rename_person(person_id: int):
    name = str(request.form.get("name") or "").strip()[:100]
    if name:
        conn = connect_inventory()
        try:
            old = conn.execute("SELECT name FROM people WHERE id = ?", (person_id,)).fetchone()
            conn.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))
            if old:
                conn.execute("UPDATE inventory SET owner = ? WHERE owner = ?", (name, old["name"]))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        finally:
            conn.close()
    return redirect(url_for("admin_page"))


@app.post("/admin/people/<int:person_id>/delete")
@admin_required
def delete_person(person_id: int):
    """Permanently remove a person. Cards may be transferred to another active owner.

    We never silently discard inventory. If the person still owns cards, the form
    must provide another active owner to receive them before the people record is
    deleted. Existing locations/deck assignments and finish quantities are preserved.
    """
    transfer_to = " ".join(str(request.form.get("transfer_to") or "").strip().split())[:100]
    conn = connect_inventory()
    ensure_people_and_auth(conn)
    try:
        person = conn.execute("SELECT id, name FROM people WHERE id = ?", (person_id,)).fetchone()
        if not person:
            return redirect(url_for("admin_page", message="Person not found."))
        name = str(person["name"])
        total_qty = int(conn.execute("SELECT COALESCE(SUM(IFNULL(quantity,1)),0) FROM inventory WHERE owner = ?", (name,)).fetchone()[0] or 0)
        if total_qty > 0:
            if not transfer_to or transfer_to.casefold() == name.casefold():
                return redirect(url_for("admin_page", message=f"{name} still owns {total_qty} card(s). Choose another active owner to transfer them to before deleting."))
            target = conn.execute("SELECT name FROM people WHERE name = ? AND active = 1", (transfer_to,)).fetchone()
            if not target:
                return redirect(url_for("admin_page", message="Choose an active owner for the inventory transfer."))
            target_name = str(target["name"])
            conn.execute("BEGIN IMMEDIATE")
            # Transfer rows one-by-one so exact printing, finish, location, and deck membership stay intact.
            rows = conn.execute("SELECT * FROM inventory WHERE owner = ? ORDER BY id", (name,)).fetchall()
            for row in rows:
                finish = normalize_finish(row["finish"])
                location = str(row["location"] or "Free")
                deck_id = row["deck_id"]
                scryfall_id = row["scryfall_id"]
                existing = conn.execute(
                    """SELECT id, quantity FROM inventory
                       WHERE owner = ? AND IFNULL(scryfall_id,'') = IFNULL(?, '')
                         AND IFNULL(NULLIF(finish,''),'normal') = ?
                         AND COALESCE(location,'Free') = ?
                         AND IFNULL(deck_id,0) = IFNULL(?,0)
                         AND COALESCE(for_vending,0) = COALESCE(?,0)
                       ORDER BY id LIMIT 1""",
                    (target_name, scryfall_id, finish, location, deck_id, row["for_vending"]),
                ).fetchone()
                qty = int(row["quantity"] or 0)
                if existing:
                    conn.execute("UPDATE inventory SET quantity = quantity + ? WHERE id = ?", (qty, existing["id"]))
                    conn.execute("DELETE FROM inventory WHERE id = ?", (row["id"],))
                else:
                    conn.execute("UPDATE inventory SET owner = ? WHERE id = ?", (target_name, row["id"]))
        else:
            conn.execute("BEGIN IMMEDIATE")
        # Deleting a person safely removes the account's default-owner mapping;
        # it never deletes or transfers the web account itself.
        conn.execute("UPDATE users SET person_id = NULL WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
        conn.commit()
        msg = f"Deleted {name} permanently." + (f" Transferred {total_qty} card(s) to {target_name}." if total_qty > 0 else "")
        return redirect(url_for("admin_page", message=msg))
    except Exception as exc:
        conn.rollback()
        return redirect(url_for("admin_page", message=f"Could not delete {name if 'name' in locals() else 'person'}: {exc}"))
    finally:
        conn.close()


@app.post("/admin/users")
@superadmin_required
def add_user():
    username = " ".join(str(request.form.get("username") or "").strip().split())[:150]
    password = str(request.form.get("password") or "")
    role = str(request.form.get("role") or "vendor").strip().lower()
    raw_person_id = str(request.form.get("person_id") or "").strip()
    if not username or len(username) < 2:
        return redirect(url_for("admin_page", message="Username must be at least 2 characters."))
    if len(password) < 12:
        return redirect(url_for("admin_page", message="User password must be at least 12 characters."))
    if role not in {"admin", "vendor"}:
        role = "vendor"
    conn=connect_inventory(); ensure_people_and_auth(conn)
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        conn.close(); return redirect(url_for("admin_page", message="That username already exists."))
    person_id = None
    if raw_person_id:
        try:
            person_id = int(raw_person_id)
        except ValueError:
            conn.close(); return redirect(url_for("admin_page", message="Invalid person assignment."))
        person = conn.execute("SELECT id, name, active FROM people WHERE id=?", (person_id,)).fetchone()
        if not person or not int(person["active"]):
            conn.close(); return redirect(url_for("admin_page", message="Choose an active person for this account."))
        if conn.execute("SELECT username FROM users WHERE person_id=?", (person_id,)).fetchone():
            conn.close(); return redirect(url_for("admin_page", message="That person is already assigned to another account."))
    conn.execute("INSERT INTO users(username,password_hash,is_admin,role,is_superadmin,active,person_id) VALUES (?,?,?,?,?,1,?)", (username,generate_password_hash(password),1 if role=="admin" else 0,role,0,person_id))
    conn.commit(); conn.close()
    assignment_note = "" if person_id is None else " and linked to the selected person"
    return redirect(url_for("admin_page", message=f"Created {role} user {username}{assignment_note}."))


@app.post("/admin/users/<username>/person")
@superadmin_required
def assign_user_person(username: str):
    """Associate a web account with its default collection person, or clear the link."""
    username = str(username)[:150]
    raw_person_id = str(request.form.get("person_id") or "").strip()
    person_id: int | None = None
    if raw_person_id:
        try:
            person_id = int(raw_person_id)
        except ValueError:
            return redirect(url_for("admin_page", message="Invalid person assignment."))

    conn = connect_inventory()
    ensure_people_and_auth(conn)
    try:
        user = conn.execute("SELECT username FROM users WHERE username=?", (username,)).fetchone()
        if not user:
            return redirect(url_for("admin_page", message="Web user not found."))
        if person_id is not None:
            person = conn.execute("SELECT id,name,active FROM people WHERE id=?", (person_id,)).fetchone()
            if not person or not int(person["active"]):
                return redirect(url_for("admin_page", message="Choose an active person."))
            other = conn.execute(
                "SELECT username FROM users WHERE person_id=? AND username<>? COLLATE NOCASE",
                (person_id, username),
            ).fetchone()
            if other:
                return redirect(url_for("admin_page", message=f"{person['name']} is already linked to {other['username']}. Unlink that account first."))
        conn.execute("UPDATE users SET person_id=? WHERE username=?", (person_id, username))
        conn.commit()
        person_name = conn.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()[0] if person_id is not None else None
        message = f"Linked {username} to {person_name}." if person_name else f"Removed the person assignment for {username}."
        return redirect(url_for("admin_page", message=message))
    except sqlite3.IntegrityError:
        conn.rollback()
        return redirect(url_for("admin_page", message="That person is already linked to another account."))
    finally:
        conn.close()


@app.post("/admin/users/<username>/toggle")
@superadmin_required
def toggle_user(username):
    username = str(username)[:150]
    current = current_user()
    if current and str(current["username"]).casefold() == username.casefold():
        return redirect(url_for("admin_page", message="You cannot deactivate your own account."))
    conn=connect_inventory(); ensure_people_and_auth(conn)
    user=conn.execute("SELECT username,active,is_admin,is_superadmin,role FROM users WHERE username=?",(username,)).fetchone()
    if not user:
        conn.close(); return redirect(url_for("admin_page", message="User not found."))
    if (int(user["is_admin"]) or int(user["is_superadmin"])) and int(user["active"]):
        active_admins=conn.execute("SELECT COUNT(*) FROM users WHERE (is_admin=1 OR is_superadmin=1) AND active=1").fetchone()[0]
        if active_admins <= 1:
            conn.close(); return redirect(url_for("admin_page", message="At least one active administrator must remain."))
    conn.execute("UPDATE users SET active=? WHERE username=?", (0 if int(user["active"]) else 1, username))
    conn.commit(); conn.close()
    return redirect(url_for("admin_page", message=f"{'Activated' if not int(user['active']) else 'Deactivated'} {username}."))


@app.post("/admin/users/<username>/password")
@superadmin_required
def admin_reset_user_password(username):
    password=str(request.form.get("password") or "")
    if len(password)<12:
        return redirect(url_for("admin_page", message="Password must be at least 12 characters."))
    conn=connect_inventory(); cur=conn.execute("UPDATE users SET password_hash=? WHERE username=?", (generate_password_hash(password), str(username)[:150])); conn.commit(); changed=cur.rowcount; conn.close()
    return redirect(url_for("admin_page", message=("Password reset." if changed else "User not found.")))


@app.post("/admin/users/<username>/rename")
@superadmin_required
def rename_user(username):
    old_username = str(username)[:150]
    new_username = " ".join(str(request.form.get("new_username") or "").strip().split())[:150]
    if len(new_username) < 2:
        return redirect(url_for("admin_page", message="Username must be at least 2 characters."))
    conn=connect_inventory(); ensure_people_and_auth(conn)
    try:
        user = conn.execute("SELECT username FROM users WHERE username=?", (old_username,)).fetchone()
        if not user:
            return redirect(url_for("admin_page", message="User not found."))
        existing = conn.execute("SELECT username FROM users WHERE username=? AND username<>?", (new_username, old_username)).fetchone()
        if existing:
            return redirect(url_for("admin_page", message="That username already exists."))
        logged_in_username = str(session.get("username") or "")
        is_self_rename = bool(logged_in_username) and logged_in_username.casefold() == old_username.casefold()
        conn.execute("UPDATE users SET username=? WHERE username=?", (new_username, old_username))
        conn.commit()
        if is_self_rename:
            session["username"] = new_username
        return redirect(url_for("admin_page", message=f"Renamed {old_username} to {new_username}."))
    except sqlite3.IntegrityError:
        conn.rollback()
        return redirect(url_for("admin_page", message="That username already exists."))
    finally:
        conn.close()


@app.post("/admin/users/<username>/role")
@superadmin_required
def change_user_role(username):
    username = str(username)[:150]
    role = str(request.form.get("role") or "vendor").strip().lower()
    if role not in {"admin", "vendor"}:
        role = "vendor"
    conn=connect_inventory(); ensure_people_and_auth(conn)
    try:
        user=conn.execute("SELECT username,is_admin,is_superadmin,active FROM users WHERE username=?",(username,)).fetchone()
        if not user:
            return redirect(url_for("admin_page", message="User not found."))
        if int(user["is_superadmin"]):
            return redirect(url_for("admin_page", message="The superadmin account cannot be changed to a normal role."))
        if int(user["is_admin"]) and role == "vendor":
            active_admins=conn.execute("SELECT COUNT(*) FROM users WHERE (is_admin=1 OR is_superadmin=1) AND active=1").fetchone()[0]
            if active_admins <= 1:
                return redirect(url_for("admin_page", message="At least one active administrator must remain."))
        is_admin = 1 if role == "admin" else 0
        conn.execute("UPDATE users SET role=?, is_admin=? WHERE username=?", (role,is_admin,username))
        conn.commit()
        return redirect(url_for("admin_page", message=f"Updated {username} to {role}."))
    finally:
        conn.close()


@app.post("/admin/users/<username>/delete")
@superadmin_required
def delete_user(username):
    username = str(username)[:150]
    current = current_user()
    if current and str(current["username"]).casefold() == username.casefold():
        return redirect(url_for("admin_page", message="You cannot delete your own account."))
    conn=connect_inventory(); ensure_people_and_auth(conn)
    try:
        user=conn.execute("SELECT username,is_admin,is_superadmin,active FROM users WHERE username=?",(username,)).fetchone()
        if not user:
            return redirect(url_for("admin_page", message="User not found."))
        if int(user["is_superadmin"]):
            active_superadmins=conn.execute("SELECT COUNT(*) FROM users WHERE is_superadmin=1 AND active=1").fetchone()[0]
            if active_superadmins <= 1:
                return redirect(url_for("admin_page", message="You cannot delete the last superadmin account."))
        if int(user["is_admin"]) or int(user["is_superadmin"]):
            active_admins=conn.execute("SELECT COUNT(*) FROM users WHERE (is_admin=1 OR is_superadmin=1) AND active=1").fetchone()[0]
            if int(user["active"]) and active_admins <= 1:
                return redirect(url_for("admin_page", message="You cannot delete the last active administrator."))
        conn.execute("DELETE FROM users WHERE username=?", (username,))
        conn.commit()
        return redirect(url_for("admin_page", message=f"Deleted web user {username} permanently."))
    finally:
        conn.close()


@app.post("/admin/password")
@admin_required
def change_password():
    password = str(request.form.get("password") or "")
    if len(password) < 12:
        return redirect(url_for("admin_page", message="Password must be at least 12 characters."))
    conn = connect_inventory()
    conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (generate_password_hash(password), current_admin()))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_page", message="Password updated."))


@app.post("/admin/set_counts")
@admin_required
def set_counts():
    data = request.get_json(silent=True) or {}
    scryfall_id = str(data.get("scryfall_id") or "").strip()
    owner = str(data.get("owner") or "").strip()[:100]
    try:
        normal_qty = max(0, int(data.get("normal", 0)))
        foil_qty = max(0, int(data.get("foil", 0)))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Counts must be whole numbers."}), 400
    if not scryfall_id or not owner:
        return jsonify({"success": False, "error": "scryfall_id and owner are required"}), 400
    conn = connect_inventory()
    ensure_people_and_auth(conn)
    rows = conn.execute("SELECT id, finish FROM inventory WHERE scryfall_id = ? AND owner = ?", (scryfall_id, owner)).fetchall()
    # Preserve card metadata from any existing row. Create missing finish rows only if needed.
    for finish, qty in (("normal", normal_qty), ("foil", foil_qty)):
        row = next((r for r in rows if normalize_finish(r["finish"]) == finish), None)
        if row:
            if qty == 0:
                conn.execute("DELETE FROM inventory WHERE id = ?", (row["id"],))
            else:
                conn.execute("UPDATE inventory SET quantity = ? WHERE id = ?", (qty, row["id"]))
        elif qty > 0:
            source = conn.execute("SELECT name,set_code,rarity,price,collector_number,tcgplayer_id,location,deck_id,for_vending FROM inventory WHERE scryfall_id = ? AND owner = ? LIMIT 1", (scryfall_id, owner)).fetchone()
            if source:
                conn.execute("""INSERT INTO inventory(scryfall_id,name,set_code,rarity,price,owner,quantity,finish,collector_number,tcgplayer_id,location,deck_id,for_vending) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (scryfall_id, source["name"], source["set_code"], source["rarity"], source["price"], owner, qty, finish, source["collector_number"], source["tcgplayer_id"], source["location"] or "Free", source["deck_id"], source["for_vending"]))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/")
@admin_required
def index():
    search = request.args.get("q", "").strip()
    owner_explicit = "owner" in request.args
    owner_filter = request.args.get("owner", "").strip()
    # A linked account starts in its own collection. Selecting "All owners"
    # sends owner= explicitly, which preserves that choice across form submits.
    if not owner_explicit:
        user = current_user()
        owner_filter = str(user["person_name"] or "").strip() if user else ""
    finish_filter = request.args.get("finish", "").strip().lower()
    type_filter = request.args.get("type", "").strip()
    color_filter = request.args.get("color", "").strip().lower()
    location_filter = request.args.get("location", "").strip()
    vending_filter = request.args.get("vending", "").strip().lower()
    if vending_filter not in {"", "yes", "no"}:
        vending_filter = ""
    if finish_filter not in {"", "normal", "foil"}:
        finish_filter = ""
    sort_key = request.args.get("sort", "date")
    direction = request.args.get("dir", "desc")
    _, _, query_order, query_direction = parse_scryfall_search(search)
    if query_order:
        sort_key = query_order
    if query_direction:
        direction = query_direction
    if sort_key not in SORTS:
        sort_key = "date"
    if direction not in {"asc", "desc"}:
        direction = "desc"
    view = request.args.get("view", "list")
    if view not in {"list", "grid"}:
        view = "list"

    inventory, visible_owner_totals, visible_owner_counts, visible_total, visible_finish_totals, result_count = inventory_rows(
        search, owner_filter, finish_filter, sort_key, direction, type_filter, color_filter, location_filter, vending_filter
    )
    if not (search or owner_filter or finish_filter or type_filter or color_filter or location_filter or vending_filter):
        # Nothing is filtered, so the result above already IS the full inventory.
        all_inventory, owner_totals, owner_counts, grand_total, global_finish_totals = (
            inventory, visible_owner_totals, visible_owner_counts, visible_total, visible_finish_totals
        )
    else:
        all_inventory, owner_totals, owner_counts, grand_total, global_finish_totals, _ = inventory_rows("", "", "", "date", "desc")

    conn = connect_inventory()
    owners = active_people(conn)
    decks = conn.execute("SELECT id, name FROM decklists ORDER BY name COLLATE NOCASE").fetchall()
    total_rows = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    total_cards = conn.execute("SELECT COALESCE(SUM(IFNULL(quantity,1)),0) FROM inventory").fetchone()[0]
    locations = [r[0] for r in conn.execute("SELECT DISTINCT COALESCE(location, 'Free') FROM inventory ORDER BY COALESCE(location, 'Free') COLLATE NOCASE").fetchall()]
    if "Free" not in locations:
        locations.insert(0, "Free")
    conn.close()
    # Keep client-side autocomplete scoped to the selected/default owner.
    # all_inventory is intentionally unfiltered for collection-wide summary
    # totals, but using it for autocomplete exposes other owners' card names
    # even when a linked account defaults to its own collection.
    autocomplete_rows, *_ = inventory_rows("", owner_filter, "", "date", "desc")
    inventory_search_index = build_inventory_search_index(autocomplete_rows)

    return render_template(
        "index.html",
        inventory=inventory,
        owner_totals=owner_totals,
        owner_counts=owner_counts,
        grand_total=grand_total,
        visible_total=visible_total,
        visible_owner_totals=visible_owner_totals,
        visible_owner_counts=visible_owner_counts,
        global_finish_totals=global_finish_totals,
        visible_finish_totals=visible_finish_totals,
        owners=owners,
        selected_owner=owner_filter,
        owner_explicit=owner_explicit,
        selected_finish=finish_filter,
        selected_type=type_filter,
        selected_color=color_filter,
        selected_location=location_filter,
        selected_vending=vending_filter,
        locations=locations,
        decks=decks,
        search=search,
        sort_key=sort_key,
        direction=direction,
        view=view,
        result_count=result_count,
        total_rows=total_rows,
        total_cards=total_cards,
        price_meta=get_price_meta(),
        price_history_days=PRICE_HISTORY_DAYS,
        scanner_api_enabled=bool(SCANNER_API_KEY),
        admin_user=current_admin(),
        admin_is_superadmin=is_superadmin_user(current_user()),
        inventory_search_index=inventory_search_index,
    )


@app.post("/update")
@admin_required
def update_inventory():
    data = request.get_json(silent=True) or {}
    try:
        item_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid id"}), 400
    field = data.get("field")
    value = data.get("value")
    if field not in {"quantity", "owner", "finish", "location", "for_vending", "deck_id"}:
        return jsonify({"success": False, "error": "Invalid field"}), 400
    if field == "quantity":
        value = parse_qty(value)
    elif field == "owner":
        value = str(value or "Me").strip()[:100] or "Me"
        conn_check = connect_inventory()
        ensure_people_and_auth(conn_check)
        valid = conn_check.execute("SELECT 1 FROM people WHERE name = ? AND active=1", (value,)).fetchone()
        conn_check.close()
        if not valid:
            return jsonify({"success": False, "error": "Owner is not active"}), 400
    elif field == "finish":
        value = normalize_finish(value)
    elif field == "location":
        value = str(value or "Free").strip()[:120] or "Free"
    elif field == "for_vending":
        if value in (True, 1, "1", "true", "yes", "on"):
            value = 1
        elif value in (False, 0, "0", "false", "no", "off"):
            value = 0
        else:
            return jsonify({"success": False, "error": "Vending flag must be on or off."}), 400
    elif field == "deck_id":
        if value in (None, "", "none", "unassigned"):
            value = None
        else:
            try:
                value = int(value)
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "Invalid deck."}), 400

    conn = connect_inventory()
    try:
        if field == "owner":
            updated = conn.execute("UPDATE inventory SET owner=? WHERE id=?", (value, item_id)).rowcount
        elif field == "deck_id":
            if value is not None and not conn.execute("SELECT 1 FROM decklists WHERE id=?", (value,)).fetchone():
                return jsonify({"success": False, "error": "Deck not found."}), 404
            if value is None:
                updated = conn.execute("UPDATE inventory SET deck_id=NULL WHERE id=?", (item_id,)).rowcount
            else:
                updated = conn.execute("UPDATE inventory SET deck_id=?, for_vending=0 WHERE id=?", (value, item_id)).rowcount
        elif field == "for_vending":
            row = conn.execute("SELECT deck_id FROM inventory WHERE id=?", (item_id,)).fetchone()
            if not row:
                return jsonify({"success": False, "error": "Row not found"}), 404
            if value and row["deck_id"] is not None:
                return jsonify({"success": False, "error": "Cards assigned to a deck cannot be marked for vending. Unassign them first."}), 400
            updated = conn.execute("UPDATE inventory SET for_vending=? WHERE id=?", (value, item_id)).rowcount
        else:
            updated = conn.execute(f"UPDATE inventory SET {field}=? WHERE id=?", (value, item_id)).rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not updated:
        return jsonify({"success": False, "error": "Row not found"}), 404
    return jsonify({"success": True})


@app.post("/admin/inventory/bulk-update")
@admin_required
def bulk_update_inventory():
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids")
    if not isinstance(raw_ids, list):
        return jsonify({"success": False, "error": "Select one or more inventory rows."}), 400
    ids: list[int] = []
    seen: set[int] = set()
    for raw_id in raw_ids:
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if item_id > 0 and item_id not in seen:
            seen.add(item_id)
            ids.append(item_id)
    if not ids:
        return jsonify({"success": False, "error": "Select one or more inventory rows."}), 400
    if len(ids) > 5000:
        return jsonify({"success": False, "error": "Select at most 5,000 rows per bulk operation."}), 400

    assignments: list[str] = []
    values: list[Any] = []
    owner_supplied = "owner" in data and data.get("owner") not in (None, "", "unchanged")
    owner_value = " ".join(str(data.get("owner") or "").split())[:100] if owner_supplied else None
    vending_value = None
    if data.get("for_vending", "unchanged") not in (None, "", "unchanged"):
        raw_vending = data.get("for_vending")
        if raw_vending in (True, 1, "1", "true", "yes", "on"):
            vending_value = 1
        elif raw_vending in (False, 0, "0", "false", "no", "off"):
            vending_value = 0
        else:
            return jsonify({"success": False, "error": "Choose Mark for vending or Remove from vending."}), 400

    location_supplied = "location" in data and data.get("location") not in (None, "")
    location_value = str(data.get("location") or "").strip()[:120] if location_supplied else None
    deck_supplied = "deck_id" in data and data.get("deck_id") not in (None, "", "unchanged")
    deck_value: int | None = None
    if deck_supplied:
        raw_deck = data.get("deck_id")
        if raw_deck not in ("none", "unassigned", "null"):
            try:
                deck_value = int(raw_deck)
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "Invalid deck selection."}), 400

    if not any((owner_supplied, vending_value is not None, location_supplied, deck_supplied)):
        return jsonify({"success": False, "error": "Choose at least one bulk change."}), 400
    if owner_supplied:
        conn = connect_inventory()
        try:
            valid_owner = conn.execute("SELECT 1 FROM people WHERE name=? COLLATE NOCASE AND active=1", (owner_value,)).fetchone()
        finally:
            conn.close()
        if not valid_owner:
            return jsonify({"success": False, "error": "Choose an active owner from the people list."}), 400
    if deck_supplied and deck_value is not None and vending_value == 1:
        return jsonify({"success": False, "error": "A card cannot be assigned to a deck and marked for vending in the same operation."}), 400

    conn = connect_inventory()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if deck_supplied and deck_value is not None and not conn.execute("SELECT 1 FROM decklists WHERE id=?", (deck_value,)).fetchone():
            conn.rollback()
            return jsonify({"success": False, "error": "Selected deck does not exist."}), 400
        if vending_value == 1 and not (deck_supplied and deck_value is None):
            placeholders = ",".join("?" for _ in ids)
            assigned_count = conn.execute(
                f"SELECT COUNT(*) FROM inventory WHERE id IN ({placeholders}) AND deck_id IS NOT NULL",
                ids,
            ).fetchone()[0]
            if assigned_count:
                conn.rollback()
                return jsonify({"success": False, "error": f"{assigned_count} selected row(s) are assigned to decks. Unassign them before marking them for vending."}), 400
        if owner_supplied:
            assignments.append("owner=?")
            values.append(owner_value)
        if location_supplied:
            assignments.append("location=?")
            values.append(location_value or "Free")
        if deck_supplied:
            assignments.append("deck_id=?")
            values.append(deck_value)
        if deck_supplied and deck_value is not None:
            # Assigning to a deck always removes sale eligibility.
            assignments.append("for_vending=0")
        elif vending_value is not None:
            assignments.append("for_vending=?")
            values.append(vending_value)
        placeholders = ",".join("?" for _ in ids)
        cur = conn.execute(f"UPDATE inventory SET {', '.join(assignments)} WHERE id IN ({placeholders})", values + ids)
        conn.commit()
        updated = cur.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify({"success": True, "updated": updated, "selected": len(ids)})


@app.post("/admin/move_quantity")
@admin_required
def move_quantity():
    data = request.get_json(silent=True) or {}
    try:
        item_id = int(data.get("id"))
        move_qty = int(data.get("quantity"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid item or quantity."}), 400
    destination = " ".join(str(data.get("destination") or "").strip().split())[:120] or "Free"
    if move_qty < 1:
        return jsonify({"success": False, "error": "Quantity must be at least 1."}), 400

    conn = connect_inventory()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM inventory WHERE id = ?", (item_id,)).fetchone()
        if not row:
            conn.rollback()
            return jsonify({"success": False, "error": "Inventory row not found."}), 404
        source_qty = int(row["quantity"] or 0)
        source_location = str(row["location"] or "Free")
        if move_qty > source_qty:
            conn.rollback()
            return jsonify({"success": False, "error": f"You only have {source_qty} copy/copies in {source_location}."}), 400
        if destination.casefold() == source_location.casefold():
            conn.rollback()
            return jsonify({"success": True, "message": "Destination is the current location."})

        # Find an existing compatible destination row so split quantities stay tidy.
        target = None
        if row["scryfall_id"]:
            target = conn.execute(
                """SELECT id, quantity FROM inventory
                   WHERE scryfall_id = ? AND owner = ? AND IFNULL(NULLIF(finish,''),'normal') = ?
                     AND COALESCE(location,'Free') = ? AND IFNULL(deck_id,0)=IFNULL(?,0)
                     AND COALESCE(for_vending,0)=COALESCE(?,0) AND id <> ? LIMIT 1""",
                (row["scryfall_id"], row["owner"], normalize_finish(row["finish"]), destination, row["deck_id"], row["for_vending"], item_id),
            ).fetchone()
        else:
            target = conn.execute(
                """SELECT id, quantity FROM inventory
                   WHERE scryfall_id IS NULL AND name = ? COLLATE NOCASE AND set_code = ? COLLATE NOCASE
                     AND COALESCE(collector_number,'') = COALESCE(?, '') AND owner = ?
                     AND IFNULL(NULLIF(finish,''),'normal') = ? AND COALESCE(location,'Free') = ?
                     AND IFNULL(deck_id,0)=IFNULL(?,0) AND COALESCE(for_vending,0)=COALESCE(?,0) AND id <> ? LIMIT 1""",
                (row["name"], row["set_code"], row["collector_number"], row["owner"], normalize_finish(row["finish"]), destination, row["deck_id"], row["for_vending"], item_id),
            ).fetchone()

        if move_qty == source_qty:
            conn.execute("UPDATE inventory SET location = ? WHERE id = ?", (destination, item_id))
        else:
            conn.execute("UPDATE inventory SET quantity = quantity - ? WHERE id = ?", (move_qty, item_id))
            if target:
                conn.execute("UPDATE inventory SET quantity = quantity + ? WHERE id = ?", (move_qty, target["id"]))
            else:
                conn.execute(
                    """INSERT INTO inventory
                       (scryfall_id,name,set_code,rarity,price,owner,quantity,scan_time,finish,collector_number,tcgplayer_id,location,deck_id,for_vending)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row["scryfall_id"], row["name"], row["set_code"], row["rarity"], row["price"], row["owner"], move_qty,
                     row["scan_time"], normalize_finish(row["finish"]), row["collector_number"], row["tcgplayer_id"], destination, row["deck_id"], row["for_vending"]),
                )
        conn.commit()
        return jsonify({"success": True, "message": f"Moved {move_qty} card(s) to {destination}."})
    except Exception as exc:
        conn.rollback()
        return jsonify({"success": False, "error": str(exc)}), 500
    finally:
        conn.close()


@app.delete("/delete/<int:item_id>")
@admin_required
def delete_item(item_id: int):
    conn = connect_inventory()
    cur = conn.execute("DELETE FROM inventory WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": cur.rowcount > 0})


@app.post("/import_csv")
@admin_required
def import_csv():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400
    file = request.files["file"]
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        return jsonify({"success": False, "error": "Must be a CSV file"}), 400
    raw = file.stream.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text, newline=None))
    if not reader.fieldnames:
        return jsonify({"success": False, "error": "CSV has no header row"}), 400

    def get(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
        lowered = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
        for key in keys:
            value = lowered.get(key.lower())
            if value not in (None, ""):
                return value
        return default

    headers = {str(h).strip().lower() for h in reader.fieldnames if h}
    is_manabox = bool(headers & {"manabox id", "binder name", "binder type", "purchase price currency"}) or (
        "set code" in headers and "foil" in headers
    )
    owner_override = str(request.form.get("owner") or "").strip()[:100]
    location_override = str(request.form.get("location") or "").strip()[:120]

    def manabox_finish(value: Any) -> str:
        v = str(value or "").strip().lower()
        if v in {"true", "yes", "1", "y"}:
            return "foil"
        return normalize_finish(v or "normal")

    conn = connect_inventory()
    ensure_people_and_auth(conn)
    known_owners = {n.casefold() for n in active_people(conn)}
    unknown_owners: set[str] = set()
    count = 0
    skipped = 0
    for row in reader:
        name = get(row, "name", "card name")
        if not name:
            skipped += 1
            continue
        scryfall_id = get(row, "scryfall_id", "scryfall id", "uuid", default="")
        set_code = str(get(row, "set_code", "set code", "set", default="")).upper()
        rarity = str(get(row, "rarity", default="Common")).strip()
        if rarity.islower():
            rarity = rarity.title()  # ManaBox uses lowercase (common, mythic...)
        price = get(row, "price", "scanned price", "scanned_price", "purchase price", default="0.00")
        owner = owner_override or str(get(row, "owner", default="Me") or "Me").strip()[:100] or "Me"
        if owner.casefold() not in known_owners:
            unknown_owners.add(owner)
            skipped += 1
            continue
        qty = parse_qty(get(row, "quantity", "qty", default=1))
        finish = manabox_finish(get(row, "finish", "printing", "treatment", "foil", default="normal"))
        scan_time = get(row, "scan_date", "scan_time", "scan time", "date", "added", default=datetime.now(timezone.utc).isoformat())
        collector_number = get(row, "collector_number", "collector number", "card number", "number", default="")
        tcgplayer_id = get(row, "tcgplayer_id", "tcgplayer id", "tcgplayer product id", "product id", default="")
        location = location_override or str(get(row, "location", "where", "binder name", default="Free") or "Free").strip()[:120] or "Free"
        raw_vending = get(row, "for_vending", "for vending", "vending")
        if raw_vending is None:
            # Storage location no longer implies sale eligibility. Legacy Free rows
            # are backfilled once by migrate_db(); new imports opt in explicitly.
            for_vending = 0
        else:
            for_vending = int(str(raw_vending).strip().lower() in {"1", "true", "yes", "y", "on", "available"})
        conn.execute(
            """
            INSERT INTO inventory
                (scryfall_id, name, set_code, rarity, price, owner, quantity, scan_time, finish, collector_number, tcgplayer_id, location, for_vending)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(scryfall_id), str(name), set_code, str(rarity), str(price), owner, qty, str(scan_time), finish,
             str(collector_number).strip() or None, str(tcgplayer_id).strip() or None, location, for_vending),
        )
        count += 1
    conn.commit()
    conn.close()
    result = {"success": True, "count": count, "skipped": skipped, "format": "manabox" if is_manabox else "standard"}
    if unknown_owners:
        result["unknown_owners"] = sorted(unknown_owners)
        result["note"] = "Rows for unknown owners were skipped. Add those people in Admin, then re-import."
    return jsonify(result)


@app.get("/export_csv")
@admin_required
def export_csv():
    inventory, _, _, _, _, _ = inventory_rows("", "", "", "date", "desc")
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow([
        "id", "scryfall_id", "name", "set_code", "rarity", "scanned_price", "finish", "quantity", "owner",
        "collector_number", "tcgplayer_id", "location", "for_vending", "current_normal", "current_foil", "current_unit_value",
        "current_total_value", "scan_time",
    ])
    for item in inventory:
        writer.writerow([
            item.get("id"), item.get("scryfall_id"), item.get("name"), item.get("set_code"), item.get("rarity"),
            item.get("scanned_price"), item.get("finish"), item.get("quantity"), item.get("owner"),
            item.get("collector_number"), item.get("tcgplayer_id"), item.get("location"), int(bool(item.get("for_vending"))), item.get("curr_normal"), item.get("curr_foil"),
            item.get("calculated_unit_price"), item.get("calculated_total_value"), item.get("scan_time"),
        ])
    return send_file(
        io.BytesIO(out.getvalue().encode("utf-8")), mimetype="text/csv", as_attachment=True,
        download_name="mtg_inventory_export.csv",
    )


@app.get("/export_manabox_csv")
@admin_required
def export_manabox_csv():
    """Export in ManaBox's collection CSV format (Collection > menu > Import)."""
    owner = request.args.get("owner", "").strip()
    inventory, _, _, _, _, _ = inventory_rows("", owner, "", "date", "desc")
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow([
        "Name", "Set code", "Collector number", "Foil", "Rarity", "Quantity", "Scryfall ID",
        "Purchase price", "Purchase price currency", "Condition", "Language", "Misprint", "Altered",
    ])
    for item in inventory:
        writer.writerow([
            item.get("name") or "",
            str(item.get("set_code") or "").lower(),
            item.get("collector_number") or "",
            "foil" if normalize_finish(item.get("finish")) == "foil" else "normal",
            str(item.get("rarity") or "").lower(),
            item.get("quantity") or 1,
            item.get("scryfall_id") or "",
            item.get("scanned_price") or "",
            "USD",
            "near_mint",
            "en",
            "false",
            "false",
        ])
    suffix = f"_{owner.replace(' ', '_')}" if owner else ""
    return send_file(
        io.BytesIO(out.getvalue().encode("utf-8")), mimetype="text/csv", as_attachment=True,
        download_name=f"manabox_export{suffix}.csv",
    )


@app.get("/api/people")
def api_people():
    if not SCANNER_API_KEY or not check_api_key():
        return jsonify({"success": False, "error": "Invalid scanner API key"}), 401
    conn = connect_inventory()
    ensure_people_and_auth(conn)
    people = active_people(conn)
    conn.close()
    return jsonify({"success": True, "people": people})


APP_VERSION = "36.0"


@app.get("/api/health")
def api_health():
    return jsonify({
        "success": True,
        "service": "mtg-scanner-inventory",
        "version": APP_VERSION,
        "scanner_api_enabled": bool(SCANNER_API_KEY),
        "card_price_endpoint": True,
    })


@app.get("/api/version")
def api_version():
    return jsonify({"success": True, "version": APP_VERSION, "card_price_endpoint": True})


@app.get("/api/price/card/<scryfall_id>")
def api_price_by_card(scryfall_id: str):
    """Resolve a card by Scryfall ID, repair missing TCGplayer mappings, and return current prices."""
    if not SCANNER_API_KEY:
        return jsonify({"success": False, "error": "SCANNER_API_KEY is not configured on the server"}), 503
    if not check_api_key():
        return jsonify({"success": False, "error": "Invalid scanner API key"}), 401
    conn = connect_inventory()
    try:
        mapping = resolve_scryfall_mapping(scryfall_id, conn)
    finally:
        conn.close()
    tcg_id = mapping.get("tcgplayer_id")
    prices = get_tcg_prices().get(str(tcg_id), {"normal": None, "foil": None}) if tcg_id else {"normal": None, "foil": None}
    meta = get_price_meta()
    normal = prices.get("normal")
    foil = prices.get("foil")
    return jsonify({
        "success": True,
        "scryfall_id": str(scryfall_id),
        "tcgplayer_id": tcg_id,
        "normal": normal,
        "foil": foil,
        "finishes": mapping.get("finishes", []),
        "name": mapping.get("name"),
        "set": mapping.get("set"),
        "collector_number": mapping.get("collector_number"),
        "source_updated": meta.get("source_updated"),
        "snapshot_exists": PRICES_FILE.exists(),
        "mapping_repaired": bool(tcg_id),
    })


@app.get("/api/price/<tcg_id>")
def api_price(tcg_id: str):
    if not SCANNER_API_KEY:
        return jsonify({"success": False, "error": "SCANNER_API_KEY is not configured on the server"}), 503
    if not check_api_key():
        return jsonify({"success": False, "error": "Invalid scanner API key"}), 401

    key = str(tcg_id).strip()
    prices = get_tcg_prices().get(key, {"normal": None, "foil": None})
    meta = get_price_meta()
    normal = prices.get("normal")
    foil = prices.get("foil")

    # If the live snapshot has no entry but history exists, return the newest
    # historical point so the scanner can still display a known price.
    if normal is None and foil is None and DB_INV.exists():
        try:
            conn = connect_inventory()
            row = conn.execute(
                "SELECT normal_price, foil_price FROM price_history WHERE tcgplayer_id = ? ORDER BY captured_at DESC LIMIT 1",
                (key,),
            ).fetchone()
            conn.close()
            if row:
                normal = row[0]
                foil = row[1]
        except sqlite3.Error:
            pass

    return jsonify({
        "success": True,
        "tcgplayer_id": key,
        "normal": normal,
        "foil": foil,
        "source_updated": meta.get("source_updated"),
        "snapshot_exists": PRICES_FILE.exists(),
    })


@app.post("/api/scan")
def api_scan():
    if not SCANNER_API_KEY:
        return jsonify({"success": False, "error": "SCANNER_API_KEY is not configured on the server"}), 503
    if not check_api_key():
        return jsonify({"success": False, "error": "Invalid scanner API key"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        result = insert_or_increment_scan(payload)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Remote scan failed")
        return jsonify({"success": False, "error": "Server error while saving scan"}), 500
    return jsonify(result)


@app.post("/api/scan/batch")
def api_scan_batch():
    if not SCANNER_API_KEY:
        return jsonify({"success": False, "error": "SCANNER_API_KEY is not configured on the server"}), 503
    if not check_api_key():
        return jsonify({"success": False, "error": "Invalid scanner API key"}), 401
    payload = request.get_json(silent=True) or {}
    scans = payload.get("scans") or []
    if not isinstance(scans, list):
        return jsonify({"success": False, "error": "scans must be a list"}), 400
    results = []
    for scan in scans:
        try:
            results.append(insert_or_increment_scan(scan))
        except ValueError as exc:
            results.append({"success": False, "error": str(exc)})
        except Exception:
            app.logger.exception("Batch scan item failed")
            results.append({"success": False, "error": "Server error while saving scan"})
    return jsonify({"success": all(r.get("success") for r in results), "results": results})


@app.get("/api/price-history/<tcg_id>")
@admin_required
def api_price_history(tcg_id: str):
    tcg_id = str(tcg_id).strip()
    if not tcg_id:
        return jsonify({"success": False, "error": "Missing TCGplayer ID"}), 400
    try:
        days = int(request.args.get("days", PRICE_HISTORY_DAYS))
    except (TypeError, ValueError):
        days = 90
    days = max(3, min(days, PRICE_HISTORY_DAYS))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    conn = connect_inventory()
    join_sql = attach_art_join(conn)
    owned = conn.execute(
        f"""SELECT COUNT(*) FROM inventory i
            {join_sql}
            WHERE COALESCE(
                NULLIF(TRIM(i.tcgplayer_id), ''),
                NULLIF(TRIM(a.tcgplayer_id), ''),
                NULLIF(TRIM(m.tcgplayer_id), '')
            ) = ?""",
        (tcg_id,),
    ).fetchone()[0]
    if not owned:
        conn.close()
        return jsonify({"success": False, "error": "That card is not in the inventory"}), 404
    rows = conn.execute(
        """SELECT captured_at, normal_price, foil_price, source_updated
           FROM price_history
           WHERE tcgplayer_id = ? AND captured_at >= ?
           ORDER BY captured_at ASC""",
        (tcg_id, since),
    ).fetchall()
    conn.close()
    return jsonify({
        "success": True,
        "tcgplayer_id": tcg_id,
        "days": days,
        "timezone": DISPLAY_TIMEZONE,
        "points": [
            {
                "captured_at": r["captured_at"],
                "normal": r["normal_price"],
                "foil": r["foil_price"],
                "source_updated": r["source_updated"],
            }
            for r in rows
        ],
    })


@app.post("/admin/test_discord")
@admin_required
def test_discord():
    ok, msg = send_discord_message({
        "embeds": [{
            "title": "MTG Scanner Inventory — Discord test",
            "description": "Your Discord webhook is working.",
            "fields": [
                {"name": "Price history", "value": f"{PRICE_HISTORY_DAYS} days retained", "inline": True},
                {"name": "Display timezone", "value": DISPLAY_TIMEZONE, "inline": True},
            ],
            "footer": {"text": "MTG Scanner Inventory"},
        }]
    })
    return redirect(url_for("admin_page", message=("Discord test sent." if ok else f"Discord test failed: {msg}")))



# ---------------------------------------------------------------------------
# Decklists, card metadata filters, locations, and vending view
# ---------------------------------------------------------------------------

def _norm_card_name(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _looks_like_uuid(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", str(value or "").strip()))


def _extract_archidekt_id(value: str) -> str | None:
    match = re.search(r"archidekt\.com/decks/(\d+)", value or "", re.I)
    return match.group(1) if match else None


def _extract_moxfield_id(value: str) -> str | None:
    match = re.search(r"moxfield\.com/decks/([A-Za-z0-9_-]+)", value or "", re.I)
    return match.group(1) if match else None


def _clean_tag_list(values: Any, *, limit: int = 30) -> list[str]:
    """Normalize imported/user-entered tags while preserving display casing."""
    if values is None:
        items: list[Any] = []
    elif isinstance(values, str):
        items = re.split(r"[,;|]", values)
    elif isinstance(values, dict):
        if any(k in values for k in ("name", "label", "value")):
            items = [values.get("name") or values.get("label") or values.get("value")]
        else:
            items = [key for key, enabled in values.items() if enabled]
    elif isinstance(values, (list, tuple, set)):
        items = list(values)
    else:
        items = [values]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            item = item.get("name") or item.get("label") or item.get("tag") or item.get("value")
        if item is None:
            continue
        tag = " ".join(str(item).replace("\x00", "").split()).strip().lstrip("#").strip()
        if not tag:
            continue
        tag = tag[:48]
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= limit:
            break
    return out


def _deck_tags_from_payload(payload: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for field in ("tags", "deckTags", "deck_tags", "deckTagsList", "customTags", "custom_tags"):
        value = payload.get(field)
        if value:
            tags.extend(_clean_tag_list(value))
    return _clean_tag_list(tags)


def _entry_role_tags(entry: dict[str, Any], categories: Any = None) -> list[str]:
    tags: list[str] = []
    if categories:
        tags.extend(_clean_tag_list(categories))
    for field in ("tags", "customTags", "custom_tags", "roleTags", "role_tags", "categories", "categoryTags"):
        value = entry.get(field)
        if value:
            tags.extend(_clean_tag_list(value))
    return _clean_tag_list(tags)


def fetch_archidekt_deck(deck_id: str) -> dict[str, Any]:
    url = f"https://archidekt.com/api/decks/{deck_id}/"
    headers = {"User-Agent": "MTGScannerInventory/1.0 (personal inventory)", "Accept": "application/json"}
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
        raise ValueError("Archidekt returned an unexpected deck format.")
    return payload


def fetch_moxfield_deck(deck_id: str) -> dict[str, Any]:
    """Fetch a public Moxfield deck. Private decks still require an exported file."""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36 MTGScannerInventory/1.0",
        "Accept": "application/json",
        "Referer": "https://www.moxfield.com/",
        "Origin": "https://www.moxfield.com",
    }
    failures: list[str] = []
    for version in ("v3", "v2"):
        url = f"https://api2.moxfield.com/{version}/decks/all/{deck_id}"
        try:
            response = requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and any(k in payload for k in ("mainboard", "commanders", "boards", "cards")):
                return payload
            failures.append(f"{version} returned an unexpected deck format")
        except (requests.RequestException, ValueError) as exc:
            failures.append(f"{version}: {exc}")
    raise ValueError("Could not fetch this Moxfield deck. Make sure it is public; for private decks, upload a Moxfield JSON/CSV export. " + "; ".join(failures))


def normalize_archidekt_cards(payload: dict[str, Any], include_extra: bool = False) -> list[dict[str, Any]]:
    category_map = {
        str(c.get("name")): c.get("includedInDeck") is not False
        for c in (payload.get("categories") or []) if isinstance(c, dict) and c.get("name")
    }
    raw: list[dict[str, Any]] = []
    excluded = {"sideboard", "maybeboard", "maybe", "considering"}
    for entry in payload.get("cards", []):
        if not isinstance(entry, dict):
            continue
        card_obj = entry.get("card") or {}
        oracle = card_obj.get("oracleCard") or card_obj.get("oracle_card") or {}
        cats = [str(c) for c in (entry.get("categories") or []) if c is not None]
        category = str(entry.get("category") or (cats[0] if cats else "Mainboard"))
        if not include_extra:
            if category.casefold() in excluded or any(c.casefold() in excluded for c in cats):
                continue
            if category_map and cats and not any(category_map.get(c, True) for c in cats):
                continue
        name = oracle.get("name") or card_obj.get("displayName") or card_obj.get("name") or entry.get("name")
        if not name:
            continue
        try:
            qty = max(1, int(entry.get("quantity", entry.get("qty", 1))))
        except (TypeError, ValueError):
            qty = 1
        edition = card_obj.get("edition") if isinstance(card_obj.get("edition"), dict) else {}
        uid = card_obj.get("uid") or card_obj.get("scryfall_id") or card_obj.get("scryfallId")
        sid = str(uid).strip() if _looks_like_uuid(uid) else None
        set_code = (
            edition.get("editionCode") or edition.get("edition_code") or edition.get("setCode") or edition.get("set_code")
            or card_obj.get("setCode") or card_obj.get("set_code")
        )
        collector_number = card_obj.get("collectorNumber") or card_obj.get("collector_number") or card_obj.get("cardNumber")
        finish_raw = (
            entry.get("finish") or entry.get("printing") or entry.get("treatment")
            or card_obj.get("finish") or card_obj.get("printing") or card_obj.get("treatment")
        )
        finish = normalize_finish(finish_raw)
        tcgplayer_id = card_obj.get("tcgplayerId") or card_obj.get("tcgplayer_id") or card_obj.get("tcgplayer")
        role_tags = _entry_role_tags(entry, cats)
        raw.append({
            "name": str(name), "quantity": qty, "scryfall_id": sid,
            "set_code": str(set_code).upper() if set_code else None,
            "collector_number": str(collector_number) if collector_number else None,
            "category": category, "finish": finish,
            "tcgplayer_id": _normalize_tcgplayer_id(tcgplayer_id), "role_tags": role_tags,
        })
    merged: dict[tuple[str, str | None, str, tuple[str, ...]], dict[str, Any]] = {}
    for card in raw:
        key = (_norm_card_name(card["name"]), card.get("scryfall_id"), card.get("category") or "Mainboard", tuple(t.casefold() for t in card.get("role_tags", [])))
        if key not in merged:
            merged[key] = dict(card)
        else:
            merged[key]["quantity"] += card["quantity"]
    return list(merged.values())


def normalize_moxfield_deck(payload: dict[str, Any], include_extra: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalize a public Moxfield API payload while retaining card tags/categories."""
    boards = payload.get("boards") if isinstance(payload.get("boards"), dict) else {}
    board_specs = (
        ("commanders", "Commander"), ("mainboard", "Mainboard"), ("sideboard", "Sideboard"),
        ("maybeboard", "Maybeboard"), ("considering", "Considering"), ("companions", "Companion"),
    )
    excluded = {"sideboard", "maybeboard", "considering"}
    raw: list[dict[str, Any]] = []
    for board_key, section_name in board_specs:
        board = payload.get(board_key)
        if not board:
            board = boards.get(board_key) or boards.get(board_key.title())
        if not board:
            continue
        if not include_extra and board_key in excluded:
            continue
        entries = list(board.values()) if isinstance(board, dict) else list(board) if isinstance(board, list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            card_obj = entry.get("card") if isinstance(entry.get("card"), dict) else entry
            name = card_obj.get("name") or card_obj.get("displayName") or entry.get("name")
            if not name:
                continue
            try:
                qty = max(1, int(entry.get("quantity", entry.get("qty", entry.get("count", 1)))))
            except (TypeError, ValueError):
                qty = 1
            explicit_section = str(entry.get("board") or entry.get("zone") or entry.get("section") or "").strip()
            category_value = str(entry.get("category") or "").strip()
            structural_categories = {"mainboard", "deck", "commander", "commanders", "sideboard", "maybeboard", "considering", "companion"}
            if explicit_section:
                category = explicit_section[:100]
            elif category_value.casefold() in structural_categories:
                category = category_value[:100]
            else:
                category = section_name
            sid_raw = card_obj.get("scryfall_id") or card_obj.get("scryfallId") or entry.get("scryfall_id") or entry.get("scryfallId")
            sid = str(sid_raw).strip() if _looks_like_uuid(sid_raw) else None
            set_code = card_obj.get("set") or card_obj.get("set_code") or card_obj.get("setCode") or entry.get("set") or entry.get("set_code")
            collector_number = card_obj.get("cn") or card_obj.get("collector_number") or card_obj.get("collectorNumber") or entry.get("cn") or entry.get("collector_number")
            is_foil = entry.get("isFoil") or entry.get("is_foil") or card_obj.get("isFoil")
            finish = "foil" if is_foil else normalize_finish(entry.get("finish") or entry.get("printing") or card_obj.get("finish"))
            tcg_id = card_obj.get("tcgplayerId") or card_obj.get("tcgplayer_id") or entry.get("tcgplayerId")
            role_tags = _entry_role_tags(entry)
            # Some exports expose user tags one level below the card object.
            role_tags = _clean_tag_list(role_tags + _entry_role_tags(card_obj))
            if category.casefold() not in {"mainboard", "sideboard", "maybeboard", "considering", "commander", "companion"}:
                role_tags = _clean_tag_list(role_tags + [category])
            raw.append({
                "name": str(name), "quantity": qty, "scryfall_id": sid,
                "set_code": str(set_code).upper() if set_code else None,
                "collector_number": str(collector_number) if collector_number else None,
                "category": category, "finish": finish,
                "tcgplayer_id": _normalize_tcgplayer_id(tcg_id),
                "role_tags": role_tags,
            })
    # Some clients provide a flat cards array instead of board dictionaries.
    if not raw and isinstance(payload.get("cards"), list):
        for entry in payload["cards"]:
            if not isinstance(entry, dict):
                continue
            section = str(entry.get("board") or entry.get("zone") or "Mainboard")
            if not include_extra and section.casefold() in excluded:
                continue
            card = entry.get("card") if isinstance(entry.get("card"), dict) else entry
            name = card.get("name") or entry.get("name")
            if not name:
                continue
            role_tags = _clean_tag_list(_entry_role_tags(entry) + _entry_role_tags(card))
            try: qty = max(1, int(entry.get("quantity", entry.get("qty", 1)) or 1))
            except (TypeError, ValueError): qty = 1
            sid_raw = card.get("scryfall_id") or card.get("scryfallId")
            raw.append({"name": str(name), "quantity": qty, "scryfall_id": str(sid_raw) if _looks_like_uuid(sid_raw) else None,
                        "set_code": card.get("set") or card.get("set_code"), "collector_number": card.get("cn") or card.get("collector_number"),
                        "category": section, "finish": "foil" if entry.get("isFoil") else normalize_finish(entry.get("finish")),
                        "tcgplayer_id": _normalize_tcgplayer_id(card.get("tcgplayerId")), "role_tags": role_tags})
    merged: dict[tuple[str, str | None, str, tuple[str, ...]], dict[str, Any]] = {}
    for card in raw:
        key = (_norm_card_name(card["name"]), card.get("scryfall_id"), card.get("category") or "Mainboard", tuple(t.casefold() for t in card.get("role_tags", [])))
        if key not in merged:
            merged[key] = dict(card)
        else:
            merged[key]["quantity"] += card["quantity"]
    return list(merged.values()), _deck_tags_from_payload(payload)


def parse_text_decklist(text: str) -> list[dict[str, Any]]:
    """Parse common deck text, including Moxfield-style #Category sections."""
    section = "Mainboard"
    active_tags: list[str] = []
    merged: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    structural = {"commander", "commanders", "sideboard", "maybeboard", "mainboard", "deck", "considering", "companion"}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip().rstrip(":").strip()
            if heading.casefold() in structural:
                section = heading.title() if heading.casefold() != "mainboard" else "Mainboard"
                active_tags = []
            elif heading:
                # Moxfield-style tagged sections: preserve the heading on each card.
                active_tags = _clean_tag_list(re.split(r"[,;|]", heading))
            continue
        low = line.casefold().rstrip(":")
        if low in structural:
            section = line.rstrip(":").title()
            if low == "mainboard": section = "Mainboard"
            active_tags = []
            continue
        match = re.match(r"^(\d+)\s*x?\s+(.+?)\s*$", line, re.I)
        if not match:
            continue
        qty = max(1, int(match.group(1)))
        name = re.sub(r"\s+\([A-Za-z0-9]{2,8}\)\s+[0-9]+[A-Za-z*]*$", "", match.group(2).strip()).strip()
        role_tags = _clean_tag_list(active_tags)
        key = (_norm_card_name(name), section, tuple(t.casefold() for t in role_tags))
        if key not in merged:
            merged[key] = {"name": name, "quantity": qty, "scryfall_id": None, "set_code": None,
                           "collector_number": None, "category": section, "role_tags": role_tags}
        else:
            merged[key]["quantity"] += qty
    return list(merged.values())


def parse_deck_csv(text: str, include_extra: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse CSV deck/collection exports and preserve tag/category columns when present."""
    reader = csv.DictReader(io.StringIO(text, newline=None))
    if not reader.fieldnames:
        raise ValueError("CSV deck file has no header row.")
    def get(row: dict[str, Any], *keys: str) -> Any:
        lowered = {str(k).strip().casefold(): v for k, v in row.items() if k is not None}
        for key in keys:
            value = lowered.get(key.casefold())
            if value not in (None, ""):
                return value
        return None
    cards: list[dict[str, Any]] = []
    deck_tags: list[str] = []
    for row in reader:
        name = get(row, "name", "card name", "simple name", "product name")
        if not name:
            continue
        section_raw = str(get(row, "board", "zone", "section", "deck section") or "Mainboard").strip()
        category_raw = str(get(row, "category", "categories") or "").strip()
        structural = {"mainboard", "deck", "commander", "commanders", "sideboard", "maybeboard", "considering", "companion"}
        section = category_raw if category_raw.casefold() in structural else section_raw
        if not include_extra and section.casefold() in {"sideboard", "maybeboard", "considering"}:
            continue
        if get(row, "deck tags", "deck_tags", "deck tag"):
            deck_tags.extend(_clean_tag_list(get(row, "deck tags", "deck_tags", "deck tag")))
        role_values: list[str] = []
        for col in ("tags", "tag", "custom tags", "custom_tags", "categories", "card categories", "role tags", "role_tags"):
            if get(row, col): role_values.extend(_clean_tag_list(get(row, col)))
        if category_raw and category_raw.casefold() not in structural:
            role_values.extend(_clean_tag_list(category_raw))
        roles = _clean_tag_list(role_values)
        try: qty = max(1, int(float(get(row, "quantity", "qty", "count", "amount") or 1)))
        except (TypeError, ValueError): qty = 1
        finish_value = get(row, "finish", "printing", "treatment")
        if str(get(row, "foil") or "").casefold() in {"true", "yes", "1", "foil", "y"}:
            finish_value = "foil"
        sid_raw = get(row, "scryfall id", "scryfall_id", "scryfall uuid")
        card = {
            "name": str(name).strip(), "quantity": qty,
            "scryfall_id": str(sid_raw).strip() if _looks_like_uuid(sid_raw) else None,
            "set_code": str(get(row, "set code", "set_code", "set") or "").strip().upper() or None,
            "collector_number": str(get(row, "collector number", "collector_number", "collector #", "cn") or "").strip() or None,
            "category": section or "Mainboard", "finish": normalize_finish(finish_value),
            "tcgplayer_id": _normalize_tcgplayer_id(get(row, "tcgplayer id", "tcgplayer_id")), "role_tags": roles,
        }
        cards.append(card)
    return cards, _clean_tag_list(deck_tags)


def deck_cover_directory() -> Path:
    path = DATA_DIR / "deck_covers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _remove_deck_cover_file(filename: str | None) -> None:
    if filename and re.fullmatch(r"[0-9a-f]{32}\.(?:jpg|jpeg|png|webp)", filename):
        try: (deck_cover_directory() / filename).unlink()
        except OSError: pass


def deck_cover_url(deck: Any) -> str | None:
    filename = deck.get("cover_filename") if hasattr(deck, "get") else None
    sid = deck.get("cover_scryfall_id") if hasattr(deck, "get") else None
    if filename:
        return url_for("deck_cover_file", filename=filename)
    if sid:
        return scryfall_image_url(str(sid))
    return None

def resolve_local_printing(name: str, scryfall_id: str|None=None, set_code: str|None=None, collector_number: str|None=None) -> dict[str,Any]:
    """Resolve an exact printing against the local card-art catalog.

    If set/collector information is supplied it takes precedence over a stale
    Scryfall ID, which is important when editing an imported deck to a different
    printing (for example changing only the collector number).
    """
    fallback = {
        "scryfall_id": str(scryfall_id).strip() if _looks_like_uuid(scryfall_id) else None,
        "name": name,
        "set_code": set_code,
        "collector_number": collector_number,
        "rarity": "Common",
        "tcgplayer_id": None,
    }
    if not DB_ART.exists():
        return fallback
    try:
        conn=sqlite3.connect(DB_ART)
        conn.row_factory=sqlite3.Row
        row=None

        # Exact UUID is authoritative only when the user did not also specify
        # printing fields that intentionally select a different printing.
        if _looks_like_uuid(scryfall_id) and not (set_code or collector_number):
            row=conn.execute(
                "SELECT scryfall_id,name,set_code,rarity,tcgplayer_id,collector_number FROM card_art WHERE scryfall_id=? LIMIT 1",
                (str(scryfall_id).strip(),),
            ).fetchone()

        if row is None:
            clauses=["name = ? COLLATE NOCASE"]
            args=[name]
            if set_code:
                clauses.append("set_code = ? COLLATE NOCASE")
                args.append(str(set_code).strip())
            if collector_number:
                clauses.append("collector_number = ?")
                args.append(str(collector_number).strip())
            rows=conn.execute(
                f"SELECT scryfall_id,name,set_code,rarity,tcgplayer_id,collector_number FROM card_art WHERE {' AND '.join(clauses)} ORDER BY set_code, collector_number LIMIT 12",
                args,
            ).fetchall()
            if len(rows)==1:
                row=rows[0]

        conn.close()
        return dict(row) if row else fallback
    except sqlite3.Error:
        return fallback


def save_deck(
    name: str, source: str, source_url: str | None, cards: list[dict[str, Any]],
    *, deck_tags: list[str] | None = None, person_name: str | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn = connect_inventory()
    conn.execute(
        "INSERT INTO decklists(name,source,source_url,created_at,updated_at,tags_json,person_name) VALUES (?,?,?,?,?,?,?)",
        (name, source, source_url, now, now, json.dumps(_clean_tag_list(deck_tags or []), ensure_ascii=False), person_name or None),
    )
    deck_id = int(conn.execute("SELECT last_insert_rowid() FROM decklists").fetchone()[0])
    conn.executemany(
        """INSERT INTO deck_cards
           (deck_id,name,quantity,scryfall_id,set_code,collector_number,category,finish,tcgplayer_id,role_tags_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [(deck_id, c["name"], int(c["quantity"]), c.get("scryfall_id"), c.get("set_code"),
          c.get("collector_number"), c.get("category"), normalize_finish(c.get("finish")),
          c.get("tcgplayer_id"), json.dumps(_clean_tag_list(c.get("role_tags") or []), ensure_ascii=False)) for c in cards],
    )
    conn.commit()
    conn.close()
    return deck_id


def deck_comparison(deck_id: int, owner: str, fuzzy: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conn = connect_inventory()
    deck = conn.execute("SELECT name FROM decklists WHERE id=?", (deck_id,)).fetchone()
    if not deck:
        conn.close()
        return [], {"required": 0, "owned": 0, "assigned": 0, "free_available": 0, "missing": 0, "complete": False, "fuzzy": fuzzy}
    cards = conn.execute(
        """SELECT dc.id,dc.name,dc.quantity,dc.scryfall_id,dc.set_code,dc.collector_number,
                  dc.category,dc.finish,dc.tcgplayer_id,dc.role_tags_json,cm.oracle_id AS card_oracle_id
           FROM deck_cards dc LEFT JOIN card_metadata cm ON cm.scryfall_id=dc.scryfall_id
           WHERE dc.deck_id=?
           ORDER BY CASE WHEN LOWER(COALESCE(dc.category,''))='commander' THEN 0 ELSE 1 END,
                    dc.category, dc.name COLLATE NOCASE, dc.id""",
        (deck_id,),
    ).fetchall()
    inv = conn.execute(
        """SELECT i.scryfall_id,i.name,i.set_code,i.collector_number,i.finish,
                  i.deck_id, COALESCE(i.for_vending,0) AS for_vending,
                  SUM(IFNULL(i.quantity,1)) AS qty,cm.oracle_id AS oracle_id
           FROM inventory i LEFT JOIN card_metadata cm ON cm.scryfall_id=i.scryfall_id
           WHERE i.owner=? AND (i.deck_id=? OR i.deck_id IS NULL)
           GROUP BY i.scryfall_id,i.name,i.set_code,i.collector_number,i.finish,
                    i.deck_id,COALESCE(i.for_vending,0),cm.oracle_id""",
        (owner, deck_id),
    ).fetchall()
    conn.close()

    free_exact: dict[tuple[str, str], int] = {}
    assigned_exact: dict[tuple[str, str], int] = {}
    free_name: dict[tuple[str, str], int] = {}
    assigned_name: dict[tuple[str, str], int] = {}
    free_fuzzy: dict[str, int] = {}
    assigned_fuzzy: dict[str, int] = {}
    for r in inv:
        finish = normalize_finish(r["finish"])
        qty = int(r["qty"] or 0)
        sid = str(r["scryfall_id"] or "").strip()
        is_assigned = r["deck_id"] is not None and int(r["deck_id"]) == deck_id
        exact = assigned_exact if is_assigned else free_exact
        by_name = assigned_name if is_assigned else free_name
        fuzzy_map = assigned_fuzzy if is_assigned else free_fuzzy
        if sid:
            exact[(sid, finish)] = exact.get((sid, finish), 0) + qty
        name_key = (_norm_card_name(r["name"]), finish)
        by_name[name_key] = by_name.get(name_key, 0) + qty
        fuzzy_key = str(r["oracle_id"] or "").strip() or _norm_card_name(r["name"])
        fuzzy_map[fuzzy_key] = fuzzy_map.get(fuzzy_key, 0) + qty

    out = []
    req = have = assigned_total = free_total = missing = 0
    for r in cards:
        required = int(r["quantity"] or 0)
        finish = normalize_finish(r["finish"])
        sid = str(r["scryfall_id"] or "").strip()
        if fuzzy:
            fuzzy_key = str(r["card_oracle_id"] or "").strip() or _norm_card_name(r["name"])
            already = assigned_fuzzy.get(fuzzy_key, 0)
            free_av = free_fuzzy.get(fuzzy_key, 0)
            assigned = min(required, already)
            free_used = min(max(0, required - assigned), free_av)
            h = assigned + free_used
            m = max(0, required - h)
            assigned_fuzzy[fuzzy_key] = max(0, already - assigned)
            free_fuzzy[fuzzy_key] = max(0, free_av - free_used)
        else:
            key = (sid, finish) if sid else None
            already = assigned_exact.get(key, 0) if key else assigned_name.get((_norm_card_name(r["name"]), finish), 0)
            free_av = free_exact.get(key, 0) if key else free_name.get((_norm_card_name(r["name"]), finish), 0)
            assigned = min(required, already)
            free_used = min(max(0, required - assigned), free_av)
            h = assigned + free_used
            m = max(0, required - h)
            if key:
                assigned_exact[key] = max(0, assigned_exact.get(key, 0) - assigned)
                free_exact[key] = max(0, free_exact.get(key, 0) - free_used)
            nk = (_norm_card_name(r["name"]), finish)
            assigned_name[nk] = max(0, assigned_name.get(nk, 0) - assigned)
            free_name[nk] = max(0, free_name.get(nk, 0) - free_used)
        req += required
        have += h
        assigned_total += assigned
        free_total += free_av
        missing += m
        try:
            role_tags = _clean_tag_list(json.loads(r["role_tags_json"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            role_tags = []
        out.append({"id": r["id"], "name": r["name"], "category": r["category"] or "Mainboard", "role_tags": role_tags,
                    "required": required, "owned": h, "assigned": assigned, "free_available": free_av,
                    "missing": m, "scryfall_id": r["scryfall_id"], "set_code": r["set_code"],
                    "collector_number": r["collector_number"], "finish": finish, "tcgplayer_id": r["tcgplayer_id"],
                    "image_url": scryfall_image_url(r["scryfall_id"]), "fuzzy": fuzzy})
    return out, {"required": req, "owned": have, "assigned": assigned_total, "free_available": free_total,
                 "missing": missing, "complete": missing == 0, "fuzzy": fuzzy}


@app.get("/decks")
@admin_required
def decks_page():
    query = str(request.args.get("q") or "").strip().casefold()
    selected_tag = str(request.args.get("tag") or "").strip()
    selected_person = str(request.args.get("person") or "").strip()
    conn = connect_inventory()
    rows = conn.execute(
        "SELECT d.*,COALESCE(SUM(dc.quantity),0) card_count FROM decklists d "
        "LEFT JOIN deck_cards dc ON dc.deck_id=d.id GROUP BY d.id ORDER BY d.updated_at DESC, d.name COLLATE NOCASE"
    ).fetchall()
    owners = active_people(conn)
    locations = [r[0] for r in conn.execute("SELECT DISTINCT COALESCE(location,'Free') FROM inventory ORDER BY COALESCE(location,'Free') COLLATE NOCASE").fetchall()]
    conn.close()
    decks: list[dict[str, Any]] = []
    all_tags: dict[str, str] = {}
    for row in rows:
        deck = dict(row)
        try:
            deck["tags"] = _clean_tag_list(json.loads(deck.get("tags_json") or "[]"))
        except (TypeError, json.JSONDecodeError):
            deck["tags"] = []
        for tag in deck["tags"]:
            all_tags.setdefault(tag.casefold(), tag)
        deck["cover_url"] = deck_cover_url(deck)
        deck["person_name"] = str(deck.get("person_name") or "").strip()
        if query and query not in " ".join([deck.get("name") or "", deck.get("source") or "", deck["person_name"], " ".join(deck["tags"])]).casefold():
            continue
        if selected_tag and selected_tag.casefold() not in {t.casefold() for t in deck["tags"]}:
            continue
        if selected_person and selected_person.casefold() != (deck["person_name"] or "Unassigned").casefold():
            continue
        decks.append(deck)
    current_user_row = current_user()
    default_person = str(current_user_row["person_name"] or "").strip() if current_user_row else ""
    if not default_person and owners:
        default_person = owners[0]
    person_options = sorted({str(d.get("person_name") or "Unassigned") for d in (dict(r) for r in rows)}, key=str.casefold)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for deck in decks:
        group_name = deck.get("person_name") or "Unassigned"
        grouped.setdefault(group_name, []).append(deck)
    deck_groups = sorted(grouped.items(), key=lambda item: (item[0] == "Unassigned", item[0].casefold()))
    return render_template(
        "decks.html", decks=decks, deck_groups=deck_groups, owners=owners, locations=locations,
        message=request.args.get("message", ""), q=request.args.get("q", ""), selected_tag=selected_tag,
        selected_person=selected_person, tag_options=sorted(all_tags.values(), key=str.casefold),
        person_options=person_options, default_person=default_person,
    )


@app.post("/decks/import")
@admin_required
def decks_import():
    source_url = str(request.form.get("source_url") or "").strip()
    pasted = request.form.get("deck_text") or ""
    deck_name = str(request.form.get("deck_name") or "").strip()[:200]
    include_extra = bool(request.form.get("include_extra"))
    add_inventory = bool(request.form.get("add_inventory"))
    store_free = bool(request.form.get("store_free"))
    owner = str(request.form.get("owner") or "").strip()[:100] or "Me"
    location = str(request.form.get("location") or "").strip()[:120]
    source_file = request.files.get("deck_file")
    imported_tags: list[str] = []
    uploaded_payload: dict[str, Any] | None = None
    try:
        if source_url:
            archidekt_id = _extract_archidekt_id(source_url)
            moxfield_id = _extract_moxfield_id(source_url)
            if archidekt_id:
                payload = fetch_archidekt_deck(archidekt_id)
                cards = normalize_archidekt_cards(payload, include_extra)
                imported_tags = _deck_tags_from_payload(payload)
                deck_name = deck_name or str(payload.get("name") or f"Archidekt Deck {archidekt_id}")
                source = "archidekt"
            elif moxfield_id:
                payload = fetch_moxfield_deck(moxfield_id)
                cards, imported_tags = normalize_moxfield_deck(payload, include_extra)
                deck_name = deck_name or str(payload.get("name") or payload.get("title") or f"Moxfield Deck {moxfield_id}")
                source = "moxfield"
            else:
                raise ValueError("Enter a valid Archidekt or Moxfield deck URL. Only public decks can be fetched by URL.")
        elif source_file and source_file.filename:
            filename = source_file.filename.lower()
            raw_data = source_file.read()
            try:
                file_text = raw_data.decode("utf-8-sig")
            except UnicodeDecodeError:
                file_text = raw_data.decode("latin-1")
            if filename.endswith(".json"):
                try:
                    uploaded_payload = json.loads(file_text)
                except json.JSONDecodeError as exc:
                    raise ValueError("Uploaded JSON could not be parsed.") from exc
                if not isinstance(uploaded_payload, dict):
                    raise ValueError("Uploaded JSON must contain a deck object.")
                if any(k in uploaded_payload for k in ("mainboard", "commanders", "boards")):
                    cards, imported_tags = normalize_moxfield_deck(uploaded_payload, include_extra)
                    source = "moxfield-json"
                    deck_name = deck_name or str(uploaded_payload.get("name") or uploaded_payload.get("title") or "Imported Moxfield Deck")
                elif isinstance(uploaded_payload.get("cards"), list):
                    cards = normalize_archidekt_cards(uploaded_payload, include_extra)
                    imported_tags = _deck_tags_from_payload(uploaded_payload)
                    source = "archidekt-json"
                    deck_name = deck_name or str(uploaded_payload.get("name") or "Imported Archidekt Deck")
                else:
                    raise ValueError("Unrecognized deck JSON. Expected an Archidekt or Moxfield deck export.")
            elif filename.endswith(".csv"):
                cards, imported_tags = parse_deck_csv(file_text, include_extra)
                source = "deck-csv"
                deck_name = deck_name or Path(source_file.filename).stem[:200] or "Imported Deck"
            elif filename.endswith(".txt"):
                cards = parse_text_decklist(file_text)
                source = "text-file"
                deck_name = deck_name or Path(source_file.filename).stem[:200] or "Imported Deck"
            else:
                raise ValueError("Upload a .json, .csv, or .txt deck export.")
        else:
            cards = parse_text_decklist(pasted)
            source = "text"
            deck_name = deck_name or "Imported Deck"
        if not cards:
            raise ValueError("No cards were found in the decklist.")
        deck_tags = _clean_tag_list(imported_tags + _clean_tag_list(request.form.get("deck_tags") or ""))
        deck_person = str(request.form.get("deck_person") or "").strip()[:100]
        conn = connect_inventory()
        people = set(active_people(conn))
        conn.close()
        if deck_person and deck_person not in people:
            raise ValueError(f"Deck for person '{deck_person}' is not an active person.")
        if not deck_person:
            user = current_user()
            mapped = str(user["person_name"] or "").strip() if user else ""
            deck_person = mapped if mapped in people else (owner if owner in people else "")
        resolved_cards = []
        for card in cards:
            resolved = resolve_local_printing(card["name"], card.get("scryfall_id"), card.get("set_code"), card.get("collector_number"))
            merged = dict(card)
            for key, value in resolved.items():
                if value is not None and value != "":
                    merged[key] = value
            merged["role_tags"] = _clean_tag_list(card.get("role_tags") or [])
            resolved_cards.append(merged)
        cards = resolved_cards
        deck_db_id = save_deck(deck_name, source, source_url or None, cards, deck_tags=deck_tags, person_name=deck_person or None)
        if add_inventory:
            target_location = location or "Free"
            target_deck_id = None if store_free else deck_db_id
            conn = connect_inventory()
            ensure_people_and_auth(conn)
            if not conn.execute("SELECT 1 FROM people WHERE name=? AND active=1", (owner,)).fetchone():
                conn.close()
                raise ValueError(f"Owner '{owner}' is not active")
            now_iso = datetime.now(timezone.utc).isoformat()
            price_snapshot = get_tcg_prices()
            for card in cards:
                sid = card.get("scryfall_id")
                finish = normalize_finish(card.get("finish"))
                existing = None
                if sid:
                    existing = conn.execute(
                        """SELECT id,price,tcgplayer_id FROM inventory WHERE scryfall_id=? AND owner=?
                           AND IFNULL(NULLIF(finish,''),'normal')=? AND COALESCE(location,'Free')=?
                           AND IFNULL(deck_id,0)=IFNULL(?,0) AND COALESCE(for_vending,0)=0 LIMIT 1""",
                        (sid, owner, finish, target_location, target_deck_id),
                    ).fetchone()
                tcgplayer_id = _normalize_tcgplayer_id(card.get("tcgplayer_id"))
                if existing and not tcgplayer_id:
                    tcgplayer_id = _normalize_tcgplayer_id(existing["tcgplayer_id"])
                current_prices = price_snapshot.get(tcgplayer_id, {}) if tcgplayer_id else {}
                snapshot_price = current_prices.get(finish)
                if snapshot_price is not None:
                    snapshot_price = f"{to_float(snapshot_price):.2f}"
                if existing:
                    conn.execute(
                        """UPDATE inventory SET quantity=quantity+?,scan_time=?,price=COALESCE(?,price),
                           collector_number=COALESCE(?,collector_number),tcgplayer_id=COALESCE(?,tcgplayer_id),deck_id=?,
                           for_vending=CASE WHEN ? IS NOT NULL THEN 0 ELSE for_vending END WHERE id=?""",
                        (int(card["quantity"]), now_iso, snapshot_price, card.get("collector_number"), tcgplayer_id, target_deck_id, target_deck_id, existing["id"]),
                    )
                else:
                    conn.execute(
                        """INSERT INTO inventory(scryfall_id,name,set_code,rarity,price,owner,quantity,scan_time,finish,collector_number,tcgplayer_id,location,deck_id,for_vending)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sid, card["name"], card.get("set_code") or "", card.get("rarity") or "Common", snapshot_price or "0.00", owner, int(card["quantity"]), now_iso, finish, card.get("collector_number"), tcgplayer_id, target_location, target_deck_id, 0),
                    )
            conn.commit()
            conn.close()
        return redirect(url_for("deck_view", deck_id=deck_db_id, owner=deck_person or owner, message=f"Imported {deck_name} with {len(deck_tags)} deck tag(s)."))
    except Exception as exc:
        return redirect(url_for("decks_page", message=f"Deck import failed: {exc}"))

@app.post("/decks/<int:deck_id>/settings")
@admin_required
def update_deck_settings(deck_id: int):
    name = " ".join(str(request.form.get("name") or "").split())[:200]
    if not name:
        return redirect(url_for("deck_view", deck_id=deck_id, message="Deck name cannot be empty."))
    tags = _clean_tag_list(request.form.get("deck_tags") or "")
    person_name = str(request.form.get("person_name") or "").strip()[:100]
    conn = connect_inventory()
    if not conn.execute("SELECT 1 FROM decklists WHERE id=?", (deck_id,)).fetchone():
        conn.close()
        return redirect(url_for("decks_page", message="Deck not found."))
    if person_name and not conn.execute("SELECT 1 FROM people WHERE name=? COLLATE NOCASE AND active=1", (person_name,)).fetchone():
        conn.close()
        return redirect(url_for("deck_view", deck_id=deck_id, message="Select an active person for this deck."))
    conn.execute(
        "UPDATE decklists SET name=?, tags_json=?, person_name=?, updated_at=? WHERE id=?",
        (name, json.dumps(tags, ensure_ascii=False), person_name or None, datetime.now(timezone.utc).isoformat(), deck_id),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("deck_view", deck_id=deck_id, message="Deck settings saved."))


@app.get("/deck-covers/<filename>")
@admin_required
def deck_cover_file(filename: str):
    # Only app-generated names are accepted; never serve arbitrary DATA_DIR files.
    if not re.fullmatch(r"[0-9a-f]{32}\.(?:jpg|jpeg|png|webp)", filename):
        return "Not found", 404
    return send_from_directory(deck_cover_directory(), filename, conditional=True, max_age=3600)


def _valid_cover_upload(upload) -> tuple[bytes, str]:
    if not upload or not upload.filename:
        raise ValueError("Choose an image file to upload.")
    ext = Path(upload.filename).suffix.lower()
    allowed = {".jpg": "jpg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp"}
    if ext not in allowed:
        raise ValueError("Cover image must be JPG, PNG, or WebP.")
    raw = upload.read(5 * 1024 * 1024 + 1)
    if not raw:
        raise ValueError("The selected cover image is empty.")
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("Cover images must be 5 MB or smaller.")
    valid = (
        (ext in {".jpg", ".jpeg"} and raw.startswith(b"\xff\xd8\xff"))
        or (ext == ".png" and raw.startswith(b"\x89PNG\r\n\x1a\n"))
        or (ext == ".webp" and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")
    )
    if not valid:
        raise ValueError("The file contents do not match a supported image format.")
    return raw, allowed[ext]


@app.post("/decks/<int:deck_id>/cover")
@admin_required
def update_deck_cover(deck_id: int):
    mode = str(request.form.get("cover_mode") or "none").strip().lower()
    conn = connect_inventory()
    deck = conn.execute("SELECT cover_filename FROM decklists WHERE id=?", (deck_id,)).fetchone()
    if not deck:
        conn.close()
        return redirect(url_for("decks_page", message="Deck not found."))
    old_filename = deck["cover_filename"]
    if mode == "keep":
        conn.close()
        return redirect(url_for("deck_view", deck_id=deck_id, message="Current cover kept."))
    new_filename = None
    cover_scryfall_id = None
    try:
        if mode == "upload":
            raw, ext = _valid_cover_upload(request.files.get("cover_image"))
            new_filename = f"{uuid4().hex}.{ext}"
            cover_path = deck_cover_directory() / new_filename
            cover_path.write_bytes(raw)
        elif mode == "card":
            cover_scryfall_id = str(request.form.get("cover_scryfall_id") or "").strip()
            row = conn.execute(
                "SELECT 1 FROM deck_cards WHERE deck_id=? AND scryfall_id=? LIMIT 1",
                (deck_id, cover_scryfall_id),
            ).fetchone()
            if not cover_scryfall_id or not row:
                raise ValueError("Choose a card printing that is actually in this deck.")
        elif mode != "none":
            raise ValueError("Choose uploaded image, card image, or no cover.")
        conn.execute(
            "UPDATE decklists SET cover_filename=?, cover_scryfall_id=?, updated_at=? WHERE id=?",
            (new_filename, cover_scryfall_id, datetime.now(timezone.utc).isoformat(), deck_id),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if new_filename:
            try: (deck_cover_directory() / new_filename).unlink()
            except OSError: pass
        conn.close()
        return redirect(url_for("deck_view", deck_id=deck_id, message=f"Could not update cover: {exc}"))
    conn.close()
    if old_filename and old_filename != new_filename:
        _remove_deck_cover_file(old_filename)
    return redirect(url_for("deck_view", deck_id=deck_id, message="Deck cover updated."))


@app.get("/decks/<int:deck_id>")
@admin_required
def deck_view(deck_id:int):
    owner = str(request.args.get("owner") or "").strip()
    fuzzy = request.args.get("fuzzy", "0").strip().lower() in {"1", "true", "yes", "on"}
    conn = connect_inventory()
    deck_row = conn.execute("SELECT * FROM decklists WHERE id=?", (deck_id,)).fetchone()
    owners = active_people(conn)
    deck_cards_rows = conn.execute(
        "SELECT * FROM deck_cards WHERE deck_id=? ORDER BY CASE WHEN LOWER(COALESCE(category,''))='commander' THEN 0 ELSE 1 END, category, name COLLATE NOCASE, id",
        (deck_id,),
    ).fetchall()
    conn.close()
    if not deck_row:
        return redirect(url_for("decks_page", message="Deck not found."))
    deck = dict(deck_row)
    try: deck["tags"] = _clean_tag_list(json.loads(deck.get("tags_json") or "[]"))
    except (TypeError, json.JSONDecodeError): deck["tags"] = []
    deck["cover_url"] = deck_cover_url(deck)
    deck_cards: list[dict[str, Any]] = []
    for row in deck_cards_rows:
        card = dict(row)
        try: card["role_tags"] = _clean_tag_list(json.loads(card.get("role_tags_json") or "[]"))
        except (TypeError, json.JSONDecodeError): card["role_tags"] = []
        deck_cards.append(card)
    cover_cards = [card for card in deck_cards if card.get("scryfall_id")]
    if "owner" not in request.args:
        if deck.get("person_name"):
            owner = str(deck["person_name"])
        else:
            user = current_user()
            mapped_person = str(user["person_name"] or "").strip() if user else ""
            owner = mapped_person if mapped_person else (owners[0] if owners else "")
    comparison, stats = deck_comparison(deck_id, owner, fuzzy=fuzzy)
    return render_template(
        "deck_view.html", deck=deck, deck_cards=deck_cards, comparison=comparison, stats=stats,
        owners=owners, selected_owner=owner, fuzzy=fuzzy, cover_cards=cover_cards,
        message=request.args.get("message", ""),
    )


@app.post("/decks/<int:deck_id>/cards/<int:card_id>/edit")
@admin_required
def edit_deck_card(deck_id:int, card_id:int):
    name=" ".join(str(request.form.get("name") or "").strip().split())
    if not name:
        return redirect(url_for("deck_view",deck_id=deck_id,message="Card name cannot be empty."))
    try:
        qty=max(1,int(request.form.get("quantity") or 1))
    except (TypeError,ValueError):
        qty=1
    set_code=str(request.form.get("set_code") or "").strip().upper()[:20] or None
    collector=str(request.form.get("collector_number") or "").strip()[:30] or None
    category=str(request.form.get("category") or "Mainboard").strip()[:100] or "Mainboard"
    role_tags = _clean_tag_list(request.form.get("role_tags") or "")
    finish=normalize_finish(request.form.get("finish"))
    scryfall_id=str(request.form.get("scryfall_id") or "").strip() or None
    tcgplayer_id=_normalize_tcgplayer_id(request.form.get("tcgplayer_id"))

    # Re-resolve the exact printing when a set/collector or Scryfall ID was supplied.
    if set_code or collector or scryfall_id:
        resolved=resolve_local_printing(name,scryfall_id,set_code,collector)
        if resolved.get("scryfall_id"):
            scryfall_id=resolved.get("scryfall_id")
            name=resolved.get("name") or name
            set_code=resolved.get("set_code") or set_code
            collector=resolved.get("collector_number") or collector
            tcgplayer_id=resolved.get("tcgplayer_id") or tcgplayer_id

    conn=connect_inventory()
    if not conn.execute("SELECT 1 FROM decklists WHERE id=?",(deck_id,)).fetchone():
        conn.close(); return redirect(url_for("decks_page",message="Deck not found."))
    conn.execute(
        """UPDATE deck_cards SET name=?,quantity=?,scryfall_id=?,set_code=?,collector_number=?,category=?,finish=?,tcgplayer_id=?,role_tags_json=?
           WHERE id=? AND deck_id=?""",
        (name,qty,scryfall_id,set_code,collector,category,finish,tcgplayer_id,json.dumps(role_tags, ensure_ascii=False),card_id,deck_id),
    )
    conn.execute("UPDATE decklists SET updated_at=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),deck_id))
    conn.commit(); conn.close()
    return redirect(url_for("deck_view",deck_id=deck_id,message=f"Updated {name}."))


@app.post("/decks/<int:deck_id>/cards/add")
@admin_required
def add_deck_card(deck_id:int):
    name=" ".join(str(request.form.get("name") or "").strip().split())
    if not name:
        return redirect(url_for("deck_view",deck_id=deck_id,message="Card name cannot be empty."))
    try: qty=max(1,int(request.form.get("quantity") or 1))
    except (TypeError,ValueError): qty=1
    set_code=str(request.form.get("set_code") or "").strip().upper()[:20] or None
    collector=str(request.form.get("collector_number") or "").strip()[:30] or None
    category=str(request.form.get("category") or "Mainboard").strip()[:100] or "Mainboard"
    role_tags = _clean_tag_list(request.form.get("role_tags") or "")
    finish=normalize_finish(request.form.get("finish"))
    tcgplayer_id=_normalize_tcgplayer_id(request.form.get("tcgplayer_id"))
    scryfall_id=str(request.form.get("scryfall_id") or "").strip() or None
    if set_code or collector or scryfall_id:
        resolved=resolve_local_printing(name,scryfall_id,set_code,collector)
        if resolved.get("scryfall_id"):
            scryfall_id=resolved.get("scryfall_id"); name=resolved.get("name") or name; set_code=resolved.get("set_code") or set_code; collector=resolved.get("collector_number") or collector; tcgplayer_id=resolved.get("tcgplayer_id") or tcgplayer_id
    conn=connect_inventory()
    if not conn.execute("SELECT 1 FROM decklists WHERE id=?",(deck_id,)).fetchone():
        conn.close(); return redirect(url_for("decks_page",message="Deck not found."))
    conn.execute("INSERT INTO deck_cards(deck_id,name,quantity,scryfall_id,set_code,collector_number,category,finish,tcgplayer_id,role_tags_json) VALUES (?,?,?,?,?,?,?,?,?,?)",(deck_id,name,qty,scryfall_id,set_code,collector,category,finish,tcgplayer_id,json.dumps(role_tags, ensure_ascii=False)))
    conn.execute("UPDATE decklists SET updated_at=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),deck_id)); conn.commit(); conn.close()
    return redirect(url_for("deck_view",deck_id=deck_id,message=f"Added {name} to the deck."))


@app.post("/decks/<int:deck_id>/cards/<int:card_id>/delete")
@admin_required
def delete_deck_card(deck_id:int, card_id:int):
    conn=connect_inventory()
    cur=conn.execute("DELETE FROM deck_cards WHERE id=? AND deck_id=?",(card_id,deck_id))
    if cur.rowcount:
        conn.execute("UPDATE decklists SET updated_at=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),deck_id))
    conn.commit(); conn.close()
    return redirect(url_for("deck_view",deck_id=deck_id,message=("Removed card from deck." if cur.rowcount else "Deck card not found.")))


def _deck_available_free_rows(conn: sqlite3.Connection, deck_card: sqlite3.Row, owner: str, fuzzy: bool = False):
    finish=normalize_finish(deck_card["finish"]); sid=str(deck_card["scryfall_id"] or "").strip()
    if fuzzy:
        oracle_id=None
        if sid:
            r=conn.execute("SELECT oracle_id FROM card_metadata WHERE scryfall_id=?",(sid,)).fetchone()
            oracle_id=str(r[0] or "").strip() if r else None
        if oracle_id:
            return conn.execute("""SELECT i.* FROM inventory i LEFT JOIN card_metadata cm ON cm.scryfall_id=i.scryfall_id WHERE i.owner=? AND i.deck_id IS NULL AND cm.oracle_id=? ORDER BY i.id""",(owner,oracle_id)).fetchall()
        return conn.execute("SELECT * FROM inventory WHERE owner=? AND deck_id IS NULL AND name=? COLLATE NOCASE ORDER BY id",(owner,deck_card["name"])).fetchall()
    if sid:
        rows=conn.execute("""SELECT * FROM inventory WHERE owner=? AND deck_id IS NULL AND scryfall_id=? AND IFNULL(NULLIF(finish,''),'normal')=? ORDER BY id""",(owner,sid,finish)).fetchall()
        if rows: return rows
    return conn.execute("""SELECT * FROM inventory WHERE owner=? AND deck_id IS NULL AND name=? COLLATE NOCASE AND IFNULL(NULLIF(finish,''),'normal')=? ORDER BY id""",(owner,deck_card["name"],finish)).fetchall()


@app.post("/decks/<int:deck_id>/assign-free")
@admin_required
def assign_free_to_deck(deck_id:int):
    owner=str(request.form.get("owner") or "").strip()
    fuzzy=str(request.form.get("fuzzy") or "0").strip().lower() in {"1","true","yes","on"}
    conn=connect_inventory()
    deck=conn.execute("SELECT * FROM decklists WHERE id=?",(deck_id,)).fetchone()
    owners=active_people(conn)
    if not deck:
        conn.close(); return redirect(url_for("decks_page",message="Deck not found."))
    if owner not in owners:
        owner=owners[0] if owners else "Me"
    moved=0; requested=0
    cards=conn.execute("SELECT * FROM deck_cards WHERE deck_id=? ORDER BY id",(deck_id,)).fetchall()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for dc in cards:
            finish=normalize_finish(dc["finish"])
            sid=str(dc["scryfall_id"] or "").strip()
            if fuzzy:
                oracle_row=conn.execute("SELECT oracle_id FROM card_metadata WHERE scryfall_id=?",(sid,)).fetchone() if sid else None
                oracle_id=str(oracle_row[0] or "").strip() if oracle_row else None
                if oracle_id:
                    row=conn.execute(
                        """SELECT COALESCE(SUM(i.quantity),0) q FROM inventory i LEFT JOIN card_metadata cm ON cm.scryfall_id=i.scryfall_id
                           WHERE i.owner=? AND i.deck_id=? AND cm.oracle_id=?""",
                        (owner,deck_id,oracle_id),
                    ).fetchone()
                else:
                    row=conn.execute(
                        """SELECT COALESCE(SUM(quantity),0) q FROM inventory WHERE owner=? AND deck_id=? AND name=? COLLATE NOCASE""",
                        (owner,deck_id,dc["name"]),
                    ).fetchone()
            elif sid:
                row=conn.execute(
                    """SELECT COALESCE(SUM(quantity),0) q FROM inventory WHERE owner=? AND deck_id=?
                       AND scryfall_id=? AND IFNULL(NULLIF(finish,''),'normal')=?""",
                    (owner,deck_id,sid,finish),
                ).fetchone()
            else:
                row=conn.execute(
                    """SELECT COALESCE(SUM(quantity),0) q FROM inventory WHERE owner=? AND deck_id=?
                       AND name=? COLLATE NOCASE AND IFNULL(NULLIF(finish,''),'normal')=?""",
                    (owner,deck_id,dc["name"],finish),
                ).fetchone()
            already=int(row["q"] or 0)
            need=max(0,int(dc["quantity"] or 0)-already)
            requested+=need
            if not need: continue
            for inv in _deck_available_free_rows(conn,dc,owner,fuzzy=fuzzy):
                if need<=0: break
                source_qty=int(inv["quantity"] or 0)
                if source_qty<=0: continue
                take=min(need,source_qty)
                if take==source_qty:
                    conn.execute("UPDATE inventory SET deck_id=?, for_vending=0 WHERE id=?",(deck_id,inv["id"]))
                else:
                    conn.execute("UPDATE inventory SET quantity=quantity-? WHERE id=?",(take,inv["id"]))
                    target=None
                    if sid:
                        target=conn.execute(
                            """SELECT id FROM inventory WHERE owner=? AND deck_id=? AND COALESCE(location,'Free')=? AND scryfall_id=?
                               AND IFNULL(NULLIF(finish,''),'normal')=? AND COALESCE(for_vending,0)=0 LIMIT 1""",(owner,deck_id,inv["location"],sid,finish)
                        ).fetchone()
                    else:
                        target=conn.execute(
                            """SELECT id FROM inventory WHERE owner=? AND deck_id=? AND COALESCE(location,'Free')=? AND name=? COLLATE NOCASE
                               AND IFNULL(NULLIF(finish,''),'normal')=? AND COALESCE(for_vending,0)=0 LIMIT 1""",(owner,deck_id,inv["location"],dc["name"],finish)
                        ).fetchone()
                    if target:
                        conn.execute("UPDATE inventory SET quantity=quantity+? WHERE id=?",(take,target["id"]))
                    else:
                        conn.execute(
                            """INSERT INTO inventory(scryfall_id,name,set_code,rarity,price,owner,quantity,scan_time,finish,collector_number,tcgplayer_id,location,deck_id,for_vending)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                            (inv["scryfall_id"],inv["name"],inv["set_code"],inv["rarity"],inv["price"],owner,take,inv["scan_time"],finish,inv["collector_number"],inv["tcgplayer_id"],inv["location"] or "Free",deck_id),
                        )
                moved+=take; need-=take
        conn.commit()
        message=f"Assigned {moved} card(s) from the unassigned pool to this deck. Physical storage locations were preserved."
        if moved < requested:
            message += f" {requested-moved} still need unassigned copies matching the deck."
    except Exception as exc:
        conn.rollback(); message=f"Could not assign cards: {exc}"
    finally:
        conn.close()
    return redirect(url_for("deck_view",deck_id=deck_id,owner=owner,fuzzy="1" if fuzzy else "0",message=message))


@app.get("/decks/<int:deck_id>/missing.csv")
def deck_missing_csv(deck_id:int):
    owner=str(request.args.get("owner") or "Me").strip(); fuzzy=request.args.get("fuzzy","0").strip().lower() in {"1","true","yes","on"}; comparison,_=deck_comparison(deck_id,owner,fuzzy=fuzzy); out=io.StringIO(newline=""); w=csv.writer(out); w.writerow(["name","missing","required","owned","set_code","collector_number"])
    for c in comparison:
        if c["missing"]: w.writerow([c["name"],c["missing"],c["required"],c["owned"],c.get("set_code") or "",c.get("collector_number") or ""])
    return send_file(io.BytesIO(out.getvalue().encode()),mimetype="text/csv",as_attachment=True,download_name=f"deck_{deck_id}_missing.csv")


def refresh_scryfall_metadata(scryfall_id: str, conn: sqlite3.Connection) -> bool:
    sid = str(scryfall_id or "").strip()
    if not _looks_like_uuid(sid):
        return False
    try:
        response = requests.get(
            f"https://api.scryfall.com/cards/{sid}",
            headers={"User-Agent": SCRYFALL_USER_AGENT, "Accept": "application/json"},
            timeout=SCRYFALL_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return False
        _upsert_card_metadata(conn, sid, data)
        tcg_id = _normalize_tcgplayer_id(data.get("tcgplayer_id"))
        finishes = data.get("finishes") if isinstance(data.get("finishes"), list) else []
        conn.execute(
            "INSERT OR REPLACE INTO card_price_mapping(scryfall_id,tcgplayer_id,finishes_json,card_name,set_code,collector_number,resolved_at) VALUES(?,?,?,?,?,?,?)",
            (sid, tcg_id, json.dumps(finishes, separators=(",",":")), data.get("name"), data.get("set"), data.get("collector_number"), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return True
    except Exception as exc:
        app.logger.warning("Metadata refresh failed for %s: %s", sid, exc)
        return False


def get_scryfall_bulk_meta() -> dict[str, Any]:
    if not SCRYFALL_BULK_META_FILE.exists(): return {}
    try: return json.loads(SCRYFALL_BULK_META_FILE.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): return {}


def get_scryfall_bulk_manifest() -> dict[str, Any]:
    response=requests.get("https://api.scryfall.com/bulk-data",headers={"User-Agent":SCRYFALL_USER_AGENT,"Accept":"application/json"},timeout=SCRYFALL_TIMEOUT)
    response.raise_for_status(); payload=response.json()
    for item in payload.get("data",[]):
        if item.get("type")=="default_cards": return item
    raise RuntimeError("Scryfall bulk-data manifest did not contain default_cards.")


def refresh_scryfall_bulk_metadata(force: bool=False) -> dict[str,Any]:
    manifest=get_scryfall_bulk_manifest(); remote_version=str(manifest.get("updated_at") or "").strip()
    cached=get_scryfall_bulk_meta()
    if not force and remote_version and cached.get("updated_at")==remote_version:
        return {"success":True,"skipped":True,"updated":0,"needed":0,"found":0,"updated_at":remote_version}
    conn=connect_inventory()
    needed={str(r[0]).strip() for r in conn.execute("SELECT DISTINCT scryfall_id FROM inventory WHERE scryfall_id IS NOT NULL AND TRIM(scryfall_id)<>''").fetchall()}
    conn.close()
    if not needed: return {"success":True,"skipped":False,"updated":0,"needed":0,"found":0,"updated_at":remote_version}
    uri=manifest.get("jsonl_download_uri") or manifest.get("download_uri")
    if not uri: raise RuntimeError("Scryfall bulk manifest has no download URI for default_cards.")
    temp=DATA_DIR/"scryfall_default_cards.tmp"
    found=0
    try:
        with requests.get(uri,headers={"User-Agent":SCRYFALL_USER_AGENT,"Accept":"application/json"},timeout=SCRYFALL_BULK_TIMEOUT,stream=True) as response:
            response.raise_for_status()
            with temp.open("wb") as fh:
                for chunk in response.iter_content(1024*1024):
                    if chunk: fh.write(chunk)
        import gzip
        opener=gzip.open if temp.open("rb").read(2)==b"\x1f\x8b" else open
        conn=connect_inventory()
        with opener(temp,"rt",encoding="utf-8") as fh:
            for raw in fh:
                line=raw.strip()
                if not line or line in {"[","]",","}: continue
                if line.endswith(","): line=line[:-1]
                try: card=json.loads(line)
                except json.JSONDecodeError: continue
                if not isinstance(card,dict): continue
                sid=str(card.get("id") or "").strip()
                if sid not in needed: continue
                _upsert_card_metadata(conn,sid,card)
                tcg_id=_normalize_tcgplayer_id(card.get("tcgplayer_id")); finishes=card.get("finishes") if isinstance(card.get("finishes"),list) else []
                conn.execute("INSERT OR REPLACE INTO card_price_mapping(scryfall_id,tcgplayer_id,finishes_json,card_name,set_code,collector_number,resolved_at) VALUES(?,?,?,?,?,?,?)",(sid,tcg_id,json.dumps(finishes,separators=(",",":")),card.get("name"),card.get("set"),card.get("collector_number"),datetime.now(timezone.utc).isoformat()))
                found+=1
                if found>=len(needed): break
        conn.commit(); conn.close()
        SCRYFALL_BULK_META_FILE.write_text(json.dumps({"updated_at":remote_version,"synced_at":datetime.now(timezone.utc).isoformat(),"needed":len(needed),"found":found,"bulk_name":manifest.get("name")},indent=2),encoding="utf-8")
        return {"success":True,"skipped":False,"updated":found,"needed":len(needed),"found":found,"updated_at":remote_version}
    finally:
        try: temp.unlink()
        except FileNotFoundError: pass


@app.post("/admin/refresh_scryfall_bulk")
@admin_required
def admin_refresh_scryfall_bulk():
    try:
        result=refresh_scryfall_bulk_metadata(force=True)
        return redirect(url_for("admin_page",message=f"Scryfall metadata refreshed: {result.get('found',0)} of {result.get('needed',0)} owned printings updated."))
    except Exception as exc:
        return redirect(url_for("admin_page",message=f"Scryfall bulk refresh failed: {exc}"))


@app.get("/api/card-info/<scryfall_id>")
@admin_required
def api_card_info(scryfall_id:str):
    conn=connect_inventory(); row=conn.execute("SELECT scryfall_id,name,type_line,colors_text,mana_value,mana_cost,oracle_text,oracle_id,legalities_json,updated_at FROM card_metadata WHERE scryfall_id=?",(scryfall_id.strip(),)).fetchone(); conn.close()
    if not row: return jsonify({"success":False,"error":"No cached Scryfall metadata for this card. Use Admin → Download/Refresh Scryfall Bulk Metadata."}),404
    try: legalities=json.loads(row["legalities_json"] or "{}")
    except json.JSONDecodeError: legalities={}
    return jsonify({"success":True,"card":{"scryfall_id":row["scryfall_id"],"name":row["name"],"type_line":row["type_line"],"colors":row["colors_text"] or "","mana_value":row["mana_value"],"mana_cost":row["mana_cost"],"oracle_text":row["oracle_text"] or "","oracle_id":row["oracle_id"],"legalities":legalities,"updated_at":row["updated_at"]}})


@app.post("/admin/enrich_metadata")
@admin_required
def enrich_metadata():
    try:
        limit=max(1,min(1000,int(request.form.get("limit") or 500)))
    except (TypeError,ValueError):
        limit=500
    conn=connect_inventory()
    ids=[r[0] for r in conn.execute(
        "SELECT DISTINCT scryfall_id FROM inventory WHERE scryfall_id IS NOT NULL AND scryfall_id != '' AND scryfall_id NOT IN (SELECT scryfall_id FROM card_metadata) LIMIT ?",
        (limit,),
    ).fetchall()]
    conn.close()
    ok=0
    for sid in ids:
        c=connect_inventory()
        if refresh_scryfall_metadata(sid, c):
            ok += 1
        c.close()
        time.sleep(0.12)
    return redirect(url_for("admin_page",message=f"Metadata enrichment finished: {ok} of {len(ids)} cards updated."))


@app.post("/decks/<int:deck_id>/delete")
@admin_required
def delete_deck(deck_id: int):
    conn = connect_inventory()
    try:
        conn.execute("BEGIN IMMEDIATE")
        deck = conn.execute("SELECT id,name,cover_filename FROM decklists WHERE id=?", (deck_id,)).fetchone()
        if not deck:
            conn.rollback()
            return redirect(url_for("decks_page", message="Deck not found."))
        rows = conn.execute("SELECT id,quantity FROM inventory WHERE deck_id=?", (deck_id,)).fetchall()
        moved_qty = sum(max(0, int(row["quantity"] or 0)) for row in rows)
        # Unassign copies, preserving physical location, quantity, finish, owner, and sale flag.
        conn.execute("UPDATE inventory SET deck_id=NULL WHERE deck_id=?", (deck_id,))
        conn.execute("DELETE FROM deck_cards WHERE deck_id=?", (deck_id,))
        cur = conn.execute("DELETE FROM decklists WHERE id=?", (deck_id,))
        conn.commit()
        if cur.rowcount and deck["cover_filename"]:
            _remove_deck_cover_file(deck["cover_filename"])
        message = f"Deck deleted. Unassigned {moved_qty} inventory copies; physical storage locations were preserved." if cur.rowcount else "Deck not found."
        return redirect(url_for("decks_page", message=message))
    except Exception as exc:
        conn.rollback()
        return redirect(url_for("decks_page", message=f"Could not delete deck: {exc}"))
    finally:
        conn.close()


@app.get("/vending/export.csv")
@vendor_or_admin_required
def vending_export_csv():
    owner=str(request.args.get("owner") or "").strip()
    q=str(request.args.get("q") or "").strip()
    try:
        min_value=max(0.0,float(request.args.get("min_value") or 0))
    except ValueError:
        min_value=0.0
    inventory,*_=inventory_rows(q,owner,"","price","desc","","","","yes")
    inventory=[i for i in inventory if i["calculated_total_value"]>=min_value] if min_value else inventory
    out=io.StringIO(newline="")
    w=csv.writer(out)
    w.writerow(["name","set_code","collector_number","rarity","finish","owner","quantity","location","unit_price","total_value","tcgplayer_id"])
    for i in inventory:
        w.writerow([i["name"],i.get("set_code") or "",i.get("collector_number") or "",i.get("rarity") or "",i.get("finish") or "",i.get("owner") or "",i.get("quantity") or 0,i.get("location") or "Free",f"{i.get('calculated_unit_price',0):.2f}",f"{i.get('calculated_total_value',0):.2f}",i.get("tcgplayer_id") or ""])
    return send_file(io.BytesIO(out.getvalue().encode("utf-8")),mimetype="text/csv",as_attachment=True,download_name="mtg_vending_inventory.csv")


@app.get("/vending")
@vendor_or_admin_required
def vending_view():
    # Vending is a shared sale-eligible view across owners by default. The owner
    # filter remains available when an admin/vendor wants to narrow the list.
    owner = str(request.args.get("owner") or "").strip()
    q=str(request.args.get("q") or "").strip()
    try: min_value=max(0.0,float(request.args.get("min_value") or 0))
    except ValueError: min_value=0.0

    # Vending eligibility is independent of physical location and deck assignment.
    autocomplete_rows,*_=inventory_rows("",owner,"","price","desc","","","","yes")
    search_index=build_inventory_search_index(autocomplete_rows)

    inventory,*_=inventory_rows(q,owner,"","price","desc","","","","yes")
    inventory=[i for i in inventory if i["calculated_total_value"]>=min_value] if min_value else inventory
    conn=connect_inventory()
    owners=active_people(conn)
    decks=conn.execute("SELECT id,name FROM decklists ORDER BY name COLLATE NOCASE").fetchall()
    locations=[row[0] for row in conn.execute("SELECT DISTINCT COALESCE(location,'Free') FROM inventory ORDER BY COALESCE(location,'Free') COLLATE NOCASE").fetchall()]
    conn.close()
    return render_template("vending.html",inventory=inventory,owners=owners,decks=decks,locations=locations,admin_user=current_admin(),selected_owner=owner,search=q,min_value=min_value,result_count=len(inventory),inventory_search_index=search_index)


@app.get("/vending/labels")
@vendor_or_admin_required
def vending_labels():
    """Render a printer-friendly label sheet for rows explicitly marked for vending."""
    raw_ids = str(request.args.get("ids") or "")
    ordered_ids=[]
    seen=set()
    for token in raw_ids.split(","):
        token=token.strip()
        if not token.isdigit():
            continue
        item_id=int(token)
        if item_id>0 and item_id not in seen:
            seen.add(item_id); ordered_ids.append(item_id)
    if not ordered_ids:
        return redirect(url_for("vending_view"))

    template = str(request.args.get("template") or "5160").strip()
    if template not in {"5160","5164"}:
        template="5160"
    repeat_mode = str(request.args.get("repeat") or "one").strip().lower()
    if repeat_mode not in {"one","qty"}:
        repeat_mode="one"
    try:
        start_label=max(1, min(30 if template=="5160" else 6, int(request.args.get("start") or 1)))
    except ValueError:
        start_label=1

    inventory, *_ = inventory_rows("", "", "", "price", "desc", "", "", "", "yes")
    by_id={int(i["id"]): i for i in inventory}
    labels=[]
    skipped=[]
    for item_id in ordered_ids:
        item=by_id.get(item_id)
        if not item:
            skipped.append(item_id); continue
        count=item["quantity"] if repeat_mode=="qty" else 1
        count=max(1,min(int(count),100))
        for _ in range(count):
            labels.append({
                "name": item["name"],
                "set_code": item.get("set_code") or "",
                "collector_number": item.get("collector_number") or "",
                "finish": item.get("finish") or "normal",
                "owner": item.get("owner") or "",
                "price": float(item.get("calculated_unit_price") or 0),
                "quantity": int(item.get("quantity") or 0),
            })

    per_sheet=30 if template=="5160" else 6
    # Only the first sheet honors the user's starting position; following sheets are full.
    labels=[None]*(start_label-1)+labels
    sheets=[labels[i:i+per_sheet] for i in range(0,len(labels),per_sheet)]
    if not sheets: sheets=[[]]
    for sheet in sheets:
        sheet.extend([None]*(per_sheet-len(sheet)))

    return render_template("vending_labels.html", sheets=sheets, template=template, repeat_mode=repeat_mode, start_label=start_label, selected_count=len(ordered_ids), printed_label_count=sum(1 for x in labels if x is not None), skipped=skipped)


@app.post("/refresh_prices")
@admin_required
def refresh_prices():
    return jsonify({
        "success": False,
        "message": "Price refresh is handled by the updater container. Run `docker compose run --rm updater python price_updater.py --once` for an immediate sync.",
    }), 409


if REQUIRE_LOGIN and (not SESSION_SECRET or len(SESSION_SECRET) < 32 or SESSION_SECRET in {"change-this-session-secret", "replace-with-a-long-random-session-secret"}):
    raise RuntimeError("SECURITY: Set SESSION_SECRET to a random value of at least 32 characters before starting the public server.")
if REQUIRE_LOGIN and (not SCANNER_API_KEY or len(SCANNER_API_KEY) < 32 or SCANNER_API_KEY in {"change-this-scanner-key", "replace-with-a-long-random-scanner-key"}):
    raise RuntimeError("SECURITY: Set SCANNER_API_KEY to a random value of at least 32 characters before public scanner use.")
if not ADMIN_INITIAL_PASSWORD:
    app.logger.info("ADMIN_PASSWORD is empty. Existing admin users will be preserved; set it only for first-time bootstrap.")
migrate_db()

if __name__ == "__main__":
    app.run(
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
    )
