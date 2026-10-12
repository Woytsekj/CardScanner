from __future__ import annotations
import argparse, json, os, sqlite3, time
from datetime import datetime, timezone
from pathlib import Path
import requests

DB_INV = Path(os.environ.get('DATA_DIR', '/data')) / 'mtg_inventory.db'
USER_AGENT = os.environ.get('SCRYFALL_USER_AGENT', 'MTGScannerInventory/1.0 (local inventory)')
TIMEOUT = float(os.environ.get('SCRYFALL_TIMEOUT', '10'))
DELAY = max(0.11, float(os.environ.get('SCRYFALL_REQUEST_DELAY', '0.12')))

def tcg_id(v):
    if v is None or str(v).strip()=='': return None
    try: return str(int(float(v)))
    except: return str(v).strip()

def ensure_cache(c):
    c.execute('''CREATE TABLE IF NOT EXISTS card_price_mapping (
        scryfall_id TEXT PRIMARY KEY,
        tcgplayer_id TEXT,
        finishes_json TEXT,
        card_name TEXT,
        set_code TEXT,
        collector_number TEXT,
        resolved_at TEXT NOT NULL
    )''')
    c.commit()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0, help='0 means all missing mappings')
    args=ap.parse_args()
    if not DB_INV.exists():
        raise SystemExit(f'Inventory DB not found: {DB_INV}')
    conn=sqlite3.connect(DB_INV)
    conn.row_factory=sqlite3.Row
    ensure_cache(conn)
    rows=conn.execute("SELECT id,scryfall_id,name FROM inventory WHERE scryfall_id IS NOT NULL AND TRIM(scryfall_id)<>'' AND (tcgplayer_id IS NULL OR TRIM(tcgplayer_id)='') GROUP BY scryfall_id,id,name ORDER BY id").fetchall()
    if args.limit: rows=rows[:args.limit]
    sess=requests.Session(); sess.headers.update({'User-Agent':USER_AGENT,'Accept':'application/json'})
    print(f'Found {len(rows)} inventory row(s) missing TCGplayer IDs.')
    repaired=0
    for i,row in enumerate(rows,1):
        sid=row['scryfall_id']
        try:
            r=sess.get(f'https://api.scryfall.com/cards/{sid}', timeout=TIMEOUT)
            r.raise_for_status(); data=r.json()
            tid=tcg_id(data.get('tcgplayer_id'))
            finishes=data.get('finishes') if isinstance(data.get('finishes'),list) else []
            now=datetime.now(timezone.utc).isoformat()
            conn.execute('INSERT OR REPLACE INTO card_price_mapping VALUES (?,?,?,?,?,?,?)',(sid,tid,json.dumps(finishes,separators=(',',':')),data.get('name'),data.get('set'),data.get('collector_number'),now))
            if tid:
                cur=conn.execute("UPDATE inventory SET tcgplayer_id=? WHERE scryfall_id=? AND (tcgplayer_id IS NULL OR TRIM(tcgplayer_id)='')",(tid,sid))
                repaired += cur.rowcount
                print(f'[{i}/{len(rows)}] {data.get("name",row["name"])} -> TCGplayer {tid} ({data.get("set")} #{data.get("collector_number")})')
            else:
                print(f'[{i}/{len(rows)}] {row["name"]} -> no TCGplayer mapping in Scryfall')
            conn.commit()
        except Exception as exc:
            print(f'[{i}/{len(rows)}] {row["name"]} -> ERROR: {exc}')
        if i < len(rows): time.sleep(DELAY)
    conn.close(); print(f'Repaired {repaired} inventory row(s).')
if __name__=='__main__': main()
