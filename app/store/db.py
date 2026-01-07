import os, sqlite3, json
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Dict, Any
import pytz

EST = pytz.timezone("America/New_York")

DB_PATH = os.getenv("DB_PATH", "farewatch.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  origin TEXT NOT NULL,
  destination TEXT NOT NULL,
  depart_date TEXT NOT NULL, -- YYYY-MM-DD
  cabin TEXT NOT NULL DEFAULT 'ECONOMY',
  adults INTEGER NOT NULL DEFAULT 1,
  currency TEXT NOT NULL DEFAULT 'USD',
  baseline_price_cents INTEGER,
  drop_threshold_pct INTEGER DEFAULT 15,
  value_percentile INTEGER DEFAULT 20,
  created_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fare_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watch_id INTEGER NOT NULL,
  seen_utc TEXT NOT NULL,
  provider TEXT NOT NULL,
  price_cents INTEGER NOT NULL,
  currency TEXT NOT NULL,
  offer_json TEXT NOT NULL,
  FOREIGN KEY (watch_id) REFERENCES watches(id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_watch_time
ON fare_snapshots (watch_id, seen_utc);

CREATE UNIQUE INDEX IF NOT EXISTS uq_watch_route_date
ON watches (origin, destination, depart_date, cabin, adults, currency);

CREATE TABLE IF NOT EXISTS global_min_alerts (
  origin TEXT NOT NULL,
  destination TEXT NOT NULL,
  last_price_cents INTEGER NOT NULL,
  last_sent_utc TEXT NOT NULL,
  PRIMARY KEY (origin, destination)
);

CREATE TABLE IF NOT EXISTS watch_subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watch_id INTEGER NOT NULL,
  email TEXT NOT NULL,
  created_utc TEXT NOT NULL,
  last_emailed_cents INTEGER,
  last_emailed_seen_utc TEXT,
  UNIQUE(watch_id, email)
);
"""

@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with connect() as c:
        c.executescript(SCHEMA)

        # ---- migrations for older DBs ----
        # Ensure watch_subscriptions has the columns our code uses
        existing_cols = {
            row["name"]
            for row in c.execute("PRAGMA table_info(watch_subscriptions)").fetchall()
        }

        if "created_utc" not in existing_cols:
            c.execute("ALTER TABLE watch_subscriptions ADD COLUMN created_utc TEXT;")
            # backfill if old created_at existed
            if "created_at" in existing_cols:
                c.execute("""
                    UPDATE watch_subscriptions
                    SET created_utc = COALESCE(created_utc, created_at)
                """)

        if "last_emailed_cents" not in existing_cols:
            c.execute("ALTER TABLE watch_subscriptions ADD COLUMN last_emailed_cents INTEGER;")

        if "last_emailed_seen_utc" not in existing_cols:
            c.execute("ALTER TABLE watch_subscriptions ADD COLUMN last_emailed_seen_utc TEXT;")

def get_watch_id(origin, destination, depart_date, cabin="ECONOMY", adults=1, currency="USD"):
    with connect() as c:
        r = c.execute(
            """SELECT id FROM watches
               WHERE origin=? AND destination=? AND depart_date=? AND cabin=? AND adults=? AND currency=?""",
            (origin, destination, depart_date, cabin, adults, currency)
        ).fetchone()
        return r["id"] if r else None

def ensure_watch(
    origin: str,
    destination: str,
    depart_date: str,
    cabin: str,
    adults: int,
    currency: str,
) -> int:
    with connect() as c:
        c.row_factory = sqlite3.Row

        row = c.execute(
            """
            SELECT id
            FROM watches
            WHERE origin = ?
              AND destination = ?
              AND depart_date = ?
              AND cabin = ?
              AND adults = ?
              AND currency = ?
            """,
            (origin, destination, depart_date, cabin, adults, currency),
        ).fetchone()

        if row:
            return row["id"]

        cur = c.execute(
            """
            INSERT INTO watches
              (origin, destination, depart_date, cabin, adults, currency, created_utc)
            VALUES
              (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
            (origin, destination, depart_date, cabin, adults, currency),
        )
        return cur.lastrowid

def add_watch(origin, destination, depart_date,
              cabin="ECONOMY", adults=1, currency="USD",
              baseline_price_cents=None, drop_threshold_pct=15, value_percentile=20) -> int:
    with connect() as c:
        cur = c.execute(
            """INSERT INTO watches
               (origin,destination,depart_date,cabin,adults,currency,
                baseline_price_cents,drop_threshold_pct,value_percentile,created_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (origin, destination, depart_date, cabin, adults, currency,
             baseline_price_cents, drop_threshold_pct, value_percentile,
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        return cur.lastrowid

def list_watches():
    with connect() as c:
        rows = c.execute(
            """
            SELECT id,
                   origin,
                   destination,
                   depart_date,
                   cabin,
                   adults,
                   currency,
            FROM watches
            ORDER BY depart_date ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]
    

def append_snapshot(watch_id:int, provider:str, price_total:str, currency:str, offer_json:str):
    cents = int(round(float(price_total) * 100))
    with connect() as c:
        c.execute(
            """INSERT INTO fare_snapshots
               (watch_id, seen_utc, provider, price_cents, currency, offer_json)
               VALUES (?,?,?,?,?,?)""",
            (watch_id, datetime.utcnow().isoformat(timespec="seconds"),
             provider, cents, currency, offer_json),
        )

def latest_snapshot(watch_id:int) -> Optional[Dict[str,Any]]:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM fare_snapshots WHERE watch_id=? ORDER BY seen_utc DESC LIMIT 1",
            (watch_id,)
        ).fetchone()
        return dict(row) if row else None

def history_min_median(watch_id:int) -> Optional[Dict[str,int]]:
    with connect() as c:
        rows = c.execute(
            "SELECT price_cents, seen_utc FROM fare_snapshots WHERE watch_id=? ORDER BY seen_utc ASC",
            (watch_id,)
        ).fetchall()
    if not rows:
        return None
    prices = [r["price_cents"] for r in rows]
    prices_sorted = sorted(prices)
    n = len(prices_sorted)
    median = prices_sorted[n//2] if n % 2 else (prices_sorted[n//2 - 1] + prices_sorted[n//2]) // 2
    latest_cents = rows[-1]["price_cents"]
    return {"min_cents": prices_sorted[0], "median_cents": median, "n": n, "latest_cents": latest_cents}

def history_for_watch(watch_id: int):
    """
    Return full price history for a watch_id as a list of dicts:
    [{seen_utc, price_cents, currency}, ...] ordered by time.
    """
    with connect() as c:
        rows = c.execute(
            "SELECT seen_utc, price_cents, currency "
            "FROM fare_snapshots WHERE watch_id=? ORDER BY seen_utc ASC",
            (watch_id,)
        ).fetchall()
    return [dict(r) for r in rows]

def get_global_min_for_window(origin: str, destination: str, start_date: str, end_date: str) -> Optional[Dict]:
    """
    Find the cheapest price across all watches for origin/destination
    whose depart_date is between start_date and end_date (YYYY-MM-DD).
    Returns a dict with watch_id, depart_date, min_cents.
    """
    with connect() as c:
        row = c.execute("""
            SELECT w.id AS watch_id,
                   w.depart_date,
                   MIN(s.price_cents) AS min_cents
            FROM watches w
            JOIN fare_snapshots s ON s.watch_id = w.id
            WHERE w.origin = ? AND w.destination = ?
              AND w.depart_date BETWEEN ? AND ?
            GROUP BY w.id
            ORDER BY min_cents ASC
            LIMIT 1
        """, (origin, destination, start_date, end_date)).fetchone()
    return dict(row) if row else None

def get_last_global_alert(origin: str, destination: str) -> Optional[int]:
    """
    Return the last_price_cents we emailed for this route, or None if never.
    """
    with connect() as c:
        row = c.execute(
            "SELECT last_price_cents FROM global_min_alerts WHERE origin=? AND destination=?",
            (origin, destination)
        ).fetchone()
    return row["last_price_cents"] if row else None

def upsert_global_alert(origin: str, destination: str, price_cents: int):
    """
    Insert or update the last global min alert for this route.
    """
    now = datetime.now(EST).strftime('%Y-%m-%d %H:%M')
    with connect() as c:
        c.execute("""
            INSERT INTO global_min_alerts (origin, destination, last_price_cents, last_sent_utc)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(origin, destination) DO UPDATE SET
                last_price_cents = excluded.last_price_cents,
                last_sent_utc = excluded.last_sent_utc
        """, (origin, destination, price_cents, now))

def update_last_emailed_for_watch(watch_id: int, cents: int, seen_utc: str) -> None:
    with connect() as c:
        c.execute(
            """
            UPDATE watches
            SET last_emailed_cents = ?, last_emailed_seen_utc = ?
            WHERE id = ?
            """,
            (cents, seen_utc, watch_id),
        )
        c.commit()

def delete_watch_by_id(watch_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        c.commit()

def ensure_subscription(watch_id: int, email: str) -> int:
    """
    Get or create a subscription row for (watch_id, email).
    Returns subscription id.
    """
    with connect() as c:
        c.row_factory = sqlite3.Row

        row = c.execute(
            """
            SELECT id
            FROM watch_subscriptions
            WHERE watch_id = ? AND email = ?
            """,
            (watch_id, email),
        ).fetchone()

        if row:
            return row["id"]

        cur = c.execute(
            """
            INSERT INTO watch_subscriptions
              (watch_id, email, created_utc)
            VALUES
              (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
            (watch_id, email),
        )
        return cur.lastrowid
    
def update_subscription_last_emailed(
    subscription_id: int,
    last_emailed_cents: int,
    seen_utc: str,
) -> None:
    with connect() as c:
        c.execute(
            """
            UPDATE watch_subscriptions
            SET last_emailed_cents = ?, last_emailed_seen_utc = ?
            WHERE id = ?
            """,
            (last_emailed_cents, seen_utc, subscription_id),
        )
def get_subscriptions_for_watch(watch_id: int):
    with connect() as c:
        rows = c.execute(
            """
            SELECT id, email, last_emailed_cents, last_emailed_seen_utc
            FROM watch_subscriptions
            WHERE watch_id = ?
            ORDER BY id ASC
            """,
            (watch_id,),
        ).fetchall()
    return [dict(r) for r in rows]

def count_subscriptions_for_watch(watch_id: int) -> int:
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM watch_subscriptions WHERE watch_id=?",
            (watch_id,),
        ).fetchone()
    return int(row["n"])

def list_watches_with_stats() -> list[dict]:
    """
    Returns watches + subscriber_count + min/median/latest/n (from fare_snapshots)
    """
    with connect() as c:
        rows = c.execute(
            """
            SELECT
                w.id,
                w.origin,
                w.destination,
                w.depart_date,
                w.cabin,
                w.adults,
                w.currency,
                w.created_utc,

                (SELECT COUNT(*) FROM watch_subscriptions s WHERE s.watch_id = w.id) AS subscriber_count,

                (SELECT COUNT(*) FROM fare_snapshots fs WHERE fs.watch_id = w.id) AS n,
                (SELECT MIN(fs.price_cents) FROM fare_snapshots fs WHERE fs.watch_id = w.id) AS min_cents,
                (SELECT fs2.price_cents
                   FROM fare_snapshots fs2
                  WHERE fs2.watch_id = w.id
               ORDER BY fs2.seen_utc DESC
                  LIMIT 1) AS latest_cents
            FROM watches w
            ORDER BY w.depart_date ASC, w.id DESC
            """
        ).fetchall()

    # median requires python or sqlite window funcs; if you already have history_min_median(wid), use that in API per watch.
    return [dict(r) for r in rows]

    
def delete_subscription(watch_id: int, email: str):
    with connect() as c:
        c.execute(
            """
            DELETE FROM subscriptions
            WHERE watch_id = ? AND email = ?
            """,
            (watch_id, email),
        )
