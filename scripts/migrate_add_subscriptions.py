# scripts/migrate_add_subscriptions.py
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "farewatch.db"

def main():
    print(f"[migrate] using db: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watch_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                created_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                last_emailed_cents INTEGER,
                last_emailed_seen_utc TEXT,
                UNIQUE(watch_id, email),
                FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
            );
            """
        )

        # OPTIONAL: backfill old watches.alert_email into watch_subscriptions
        rows = conn.execute(
            "SELECT id, alert_email FROM watches WHERE alert_email IS NOT NULL AND alert_email != ''"
        ).fetchall()

        inserted = 0
        for wid, email in rows:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO watch_subscriptions (watch_id, email)
                VALUES (?, ?)
                """,
                (wid, email),
            )
            inserted += cur.rowcount

        print(f"[migrate] backfilled {inserted} subscription(s).")

        conn.commit()
        print("[migrate] ✅ watch_subscriptions table is ready.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()