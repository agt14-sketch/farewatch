import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone

# --- Make project importable when run as a script (Render cron, local, etc.) ---
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.store import db
from app.logic.deals import is_new_low, drop_pct, search_best_offer_for_watch
from app.notifiers.emailer import send_email

# --- Logging setup ---
log = logging.getLogger("scheduler_worker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Anti-spam guard: minimum hours between alerts to the same subscription
MIN_HOURS_BETWEEN_ALERTS = 6


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch_watches_with_subscribers() -> list[dict]:
    """
    Only process watches that have at least one subscription.
    This ensures we only touch watches created + subscribed via the app.
    """
    with db.connect() as c:
        rows = c.execute(
            """
            SELECT DISTINCT w.*
            FROM watches w
            JOIN watch_subscriptions s ON s.watch_id = w.id
            ORDER BY w.depart_date ASC, w.id ASC
            """
        ).fetchall()
    watches = [dict(r) for r in rows]
    log.info("fetch_watches_with_subscribers: found %d watches", len(watches))
    return watches


def take_snapshot_for_watch(watch: dict) -> dict | None:
    """
    1) Call search_best_offer_for_watch(...) to get the best current offer
    2) Append a snapshot row to fare_snapshots
    3) Return the latest snapshot row for logging / email logic
    """
    wid = watch["id"]
    log.info(
        "watch %s: fetching best offer for %s→%s %s (%s, %s adult(s))",
        wid,
        watch["origin"],
        watch["destination"],
        watch["depart_date"],
        watch.get("cabin", "ECONOMY"),
        watch.get("adults", 1),
    )

    try:
        offer = search_best_offer_for_watch(watch)
    except Exception as e:
        log.exception("watch %s: error calling search_best_offer_for_watch: %s", wid, e)
        return None

    if not offer:
        log.info("watch %s: no offer found (skipping)", wid)
        return None

    # Normalize fields expected by append_snapshot
    try:
        price_total = float(offer["price_total"])
    except Exception:
        log.error("watch %s: offer missing/invalid price_total: %s", wid, offer)
        return None

    currency = offer.get("currency", watch.get("currency", "USD"))
    provider = offer.get("provider", "amadeus")
    offer_json = offer.get("raw_json")
    if offer_json is None:
        offer_json = json.dumps(offer)

    # Save snapshot
    db.append_snapshot(
        watch_id=wid,
        provider=provider,
        price_total=price_total,
        currency=currency,
        offer_json=offer_json,
    )

    latest = db.latest_snapshot(wid)
    if latest:
        log.info(
            "watch %s: snapshot saved price=%s %s at %s",
            wid,
            latest["price_cents"],
            latest["currency"],
            latest["seen_utc"],
        )
    else:
        log.warning("watch %s: snapshot append done but latest_snapshot returned None", wid)

    return latest


def should_send_email(sub: dict, watch: dict, latest_snapshot: dict) -> bool:
    """
    Decide if we should email this subscription.

    Rules:
      1. Must be a "new low" (via is_new_low).
      2. For this subscriber, price must improve over last_emailed_cents.
      3. Respect MIN_HOURS_BETWEEN_ALERTS to avoid spam.
    """
    current_price = latest_snapshot["price_cents"]
    last_price = sub.get("last_emailed_cents")
    last_seen_str = sub.get("last_emailed_seen_utc")

    # 1) Only send if this is a new low for the watch overall
    low_info = is_new_low(watch["id"])
    if not low_info:
        log.info(
            "watch %s / sub %s: not a new low (skipping email)",
            watch["id"],
            sub["email"],
        )
        return False

    # 2) Per-subscriber improvement check
    if last_price is not None and current_price >= last_price:
        log.info(
            "watch %s / sub %s: price %s not below last emailed %s (skipping)",
            watch["id"],
            sub["email"],
            current_price,
            last_price,
        )
        return False

    # 3) Anti-spam time throttle
    if last_seen_str:
        try:
            last_seen = datetime.fromisoformat(last_seen_str)
            if datetime.now(timezone.utc) - last_seen < timedelta(hours=MIN_HOURS_BETWEEN_ALERTS):
                log.info(
                    "watch %s / sub %s: last email too recent (%s), throttling",
                    watch["id"],
                    sub["email"],
                    last_seen_str,
                )
                return False
        except Exception:
            # If parsing fails, fail open and allow email
            pass

    return True


def send_alerts_for_watch(watch: dict, latest_snapshot: dict) -> None:
    wid = watch["id"]
    subs = db.get_subscriptions_for_watch(wid)
    log.info("watch %s: %d subscriptions", wid, len(subs))

    if not subs:
        return

    for sub in subs:
        email = sub["email"]

        if not should_send_email(sub, watch, latest_snapshot):
            continue

        subject = f"New low fare {watch['origin']} → {watch['destination']} on {watch['depart_date']}"
        body = (
            f"Watch #{watch['id']}\n"
            f"Route: {watch['origin']} → {watch['destination']}\n"
            f"Depart: {watch['depart_date']}\n"
            f"Cabin: {watch.get('cabin', 'ECONOMY')}\n"
            f"Adults: {watch.get('adults', 1)}\n\n"
            f"Latest price: {latest_snapshot['price_cents'] / 100:.2f} {latest_snapshot['currency']}\n"
        )

        log.info("watch %s: sending alert email to %s", wid, email)
        send_email(subject, body, email_to=email)

        db.update_subscription_last_emailed(
            subscription_id=sub["id"],
            last_emailed_cents=latest_snapshot["price_cents"],
            seen_utc=latest_snapshot["seen_utc"],
        )
        log.info("watch %s / sub %s: last_emailed updated", wid, email)


def process_watch(watch: dict) -> None:
    wid = watch["id"]
    log.info(
        "=== processing watch %s (%s→%s %s) ===",
        wid,
        watch["origin"],
        watch["destination"],
        watch["depart_date"],
    )

    try:
        latest = take_snapshot_for_watch(watch)
        if not latest:
            return

        send_alerts_for_watch(watch, latest)
    except Exception as e:
        log.exception("Error processing watch %s: %s", wid, e)


def run_scheduler_once() -> None:
    """
    Entry point used by Render cron and for local runs.
    """
    log.info("==== scheduler run started at %s ====", utcnow_iso())
    db.init_db()  # safe to call repeatedly
    log.info("Using DB_PATH=%s", db.DB_PATH)

    # (Optional) quick debug: how many watches exist in total
    all_watches = db.list_watches()
    log.info("Total watches in DB: %d", len(all_watches))

    watches = fetch_watches_with_subscribers()
    log.info("Found %d watches with subscriptions", len(watches))

    for w in watches:
        process_watch(w)

    log.info("==== scheduler run complete at %s ====", utcnow_iso())


if __name__ == "__main__":
    # Allow: python -m app.logic.scheduler_worker
    run_scheduler_once()
