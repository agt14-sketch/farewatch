import os
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Set DATABASE_URL in environment variables (Render Postgres).")


@contextmanager
def connect():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """
    Create tables in Postgres.
    Note: No PRAGMA. Use normal Postgres DDL.
    """
    with connect() as conn:
        with conn.cursor() as c:
            c.execute("""
            CREATE TABLE IF NOT EXISTS watches (
              id SERIAL PRIMARY KEY,
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
            """)

            c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_watch_route_date
            ON watches (origin, destination, depart_date, cabin, adults, currency);
            """)

            c.execute("""
            CREATE TABLE IF NOT EXISTS fare_snapshots (
              id SERIAL PRIMARY KEY,
              watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
              seen_utc TEXT NOT NULL,
              provider TEXT NOT NULL,
              price_cents INTEGER NOT NULL,
              currency TEXT NOT NULL,
              offer_json TEXT NOT NULL
            );
            """)

            c.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_watch_time
            ON fare_snapshots (watch_id, seen_utc);
            """)

            c.execute("""
            CREATE TABLE IF NOT EXISTS global_min_alerts (
              origin TEXT NOT NULL,
              destination TEXT NOT NULL,
              last_price_cents INTEGER NOT NULL,
              last_sent_utc TEXT NOT NULL,
              PRIMARY KEY (origin, destination)
            );
            """)

            c.execute("""
            CREATE TABLE IF NOT EXISTS watch_subscriptions (
              id SERIAL PRIMARY KEY,
              watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
              email TEXT NOT NULL,
              created_utc TEXT NOT NULL,
              last_emailed_cents INTEGER,
              last_emailed_seen_utc TEXT,
              UNIQUE(watch_id, email)
            );
            """)

            c.execute("""
            CREATE TABLE IF NOT EXISTS onboarding_email_queue (
              id SERIAL PRIMARY KEY,
              email TEXT NOT NULL,
              watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
              created_utc TEXT NOT NULL,
              sent_utc TEXT,
              UNIQUE(email, watch_id)
            );
            """)


def ensure_watch(origin: str, destination: str, depart_date: str, cabin: str, adults: int, currency: str) -> int:
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute(
                """
                SELECT id FROM watches
                WHERE origin=%s AND destination=%s AND depart_date=%s
                  AND cabin=%s AND adults=%s AND currency=%s
                """,
                (origin, destination, depart_date, cabin, adults, currency),
            )
            row = c.fetchone()
            if row:
                return int(row["id"])

            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            c.execute(
                """
                INSERT INTO watches (origin, destination, depart_date, cabin, adults, currency, created_utc)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (origin, destination, depart_date, cabin, adults, currency, now),
            )
            return int(c.fetchone()["id"])


def list_watches() -> List[dict]:
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("""
                SELECT id, origin, destination, depart_date, cabin, adults, currency,
                       baseline_price_cents, drop_threshold_pct, value_percentile, created_utc
                FROM watches
                ORDER BY depart_date ASC
            """)
            return list(c.fetchall())


def append_snapshot(watch_id: int, provider: str, price_total: float, currency: str, offer_json: str):
    cents = int(round(float(price_total) * 100))
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with connect() as conn:
        with conn.cursor() as c:
            c.execute(
                """
                INSERT INTO fare_snapshots (watch_id, seen_utc, provider, price_cents, currency, offer_json)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (watch_id, now, provider, cents, currency, offer_json),
            )


def latest_snapshot(watch_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute(
                """
                SELECT * FROM fare_snapshots
                WHERE watch_id=%s
                ORDER BY seen_utc DESC
                LIMIT 1
                """,
                (watch_id,),
            )
            row = c.fetchone()
            return dict(row) if row else None


def history_min_median(watch_id: int) -> Optional[Dict[str, int]]:
    """
    Cheap & safe: compute in Python (OK for small histories).
    """
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute(
                "SELECT price_cents, seen_utc FROM fare_snapshots WHERE watch_id=%s ORDER BY seen_utc ASC",
                (watch_id,),
            )
            rows = c.fetchall()

    if not rows:
        return None

    prices = [int(r["price_cents"]) for r in rows]
    prices_sorted = sorted(prices)
    n = len(prices_sorted)
    median = prices_sorted[n // 2] if n % 2 else (prices_sorted[n // 2 - 1] + prices_sorted[n // 2]) // 2
    latest_cents = int(rows[-1]["price_cents"])
    return {"min_cents": prices_sorted[0], "median_cents": median, "n": n, "latest_cents": latest_cents}


def ensure_subscription(watch_id: int, email: str) -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute(
                "SELECT id FROM watch_subscriptions WHERE watch_id=%s AND email=%s",
                (watch_id, email),
            )
            row = c.fetchone()
            if row:
                return int(row["id"])

            c.execute(
                """
                INSERT INTO watch_subscriptions (watch_id, email, created_utc)
                VALUES (%s,%s,%s)
                RETURNING id
                """,
                (watch_id, email, now),
            )
            return int(c.fetchone()["id"])


def get_subscriptions_for_watch(watch_id: int) -> List[dict]:
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute(
                """
                SELECT id, email, last_emailed_cents, last_emailed_seen_utc
                FROM watch_subscriptions
                WHERE watch_id=%s
                ORDER BY id ASC
                """,
                (watch_id,),
            )
            return list(c.fetchall())


def update_subscription_last_emailed(subscription_id: int, last_emailed_cents: int, seen_utc: str) -> None:
    with connect() as conn:
        with conn.cursor() as c:
            c.execute(
                """
                UPDATE watch_subscriptions
                SET last_emailed_cents=%s, last_emailed_seen_utc=%s
                WHERE id=%s
                """,
                (last_emailed_cents, seen_utc, subscription_id),
            )


def delete_subscription(watch_id: int, email: str) -> int:
    with connect() as conn:
        with conn.cursor() as c:
            c.execute(
                "DELETE FROM watch_subscriptions WHERE watch_id=%s AND email=%s",
                (watch_id, email),
            )
            return c.rowcount


def onboarding_email_queue(email: str, watch_id: int) -> None:
    if not email:
        return
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with connect() as conn:
        with conn.cursor() as c:
            c.execute(
                """
                INSERT INTO onboarding_email_queue (email, watch_id, created_utc, sent_utc)
                VALUES (%s,%s,%s,NULL)
                ON CONFLICT (email, watch_id) DO NOTHING
                """,
                (email, watch_id, now),
            )


def fetch_pending_onboarding_rows(cutoff_iso: str) -> List[dict]:
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute(
                """
                SELECT
                    q.id AS queue_id,
                    q.email,
                    q.created_utc,
                    w.id AS watch_id,
                    w.origin, w.destination, w.depart_date, w.cabin, w.adults, w.currency
                FROM onboarding_email_queue q
                JOIN watches w ON w.id = q.watch_id
                WHERE q.sent_utc IS NULL
                  AND q.created_utc <= %s
                ORDER BY q.email, q.created_utc ASC
                """,
                (cutoff_iso,),
            )
            return list(c.fetchall())


def mark_onboarding_sent(queue_ids: List[int], sent_utc: str) -> None:
    if not queue_ids:
        return
    with connect() as conn:
        with conn.cursor() as c:
            c.execute(
                """
                UPDATE onboarding_email_queue
                SET sent_utc=%s
                WHERE id = ANY(%s)
                """,
                (sent_utc, queue_ids),
            )


def list_watches_with_stats() -> List[dict]:
    """
    One query: watches + snapshot counts + min + latest.
    (Median still computed client-side if you want it.)
    """
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute(
                """
                SELECT
                    w.id, w.origin, w.destination, w.depart_date, w.cabin, w.adults, w.currency, w.created_utc,
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
            )
            return list(c.fetchall())
