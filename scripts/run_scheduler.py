import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone

# Make "app." imports work when run as a script
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.store import db
from app.logic.deals import is_new_low, search_best_offer_for_watch
from app.notifiers.emailer import send_email

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Minimum time between emails to the same subscription (anti-spam)
MIN_HOURS_BETWEEN_ALERTS = 6


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch_watches_with_subscribers() -> list[dict]:
    """
    Only process watches that have at least one subscription.
    """
    with db.connect() as c:
        rows = c.execute(
            """
            SELECT DISTINCT w.*
            FROM watches w
            JOIN watch_subscriptions s ON s.watch_id = w.id
            ORDER BY w.depart_date ASC, w.id DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def take_snapshot_for_watch(watch: dict) -> dict | None:
    """
    1) Call Amadeus / deals logic to get the best current offer
    2) Append to fare_snapshots
    3) Return the latest snapshot row for this watch
    """
    offer = search_best_offer_for_watch(watch)
    if not offer:
        log.info("No offer found for watch %s", watch["id"])
        return None

    price_total = float(offer["price_total"])
    currency = offer.get("currency", watch["currency"])
    provider = offer.get("provider", "amadeus")
    raw = offer.get("raw_json", offer)
    offer_json = raw if isinstance(raw, str) else json.dumps(raw)

    db.append_snapshot(
        watch_id=watch["id"],
        provider=provider,
        price_total=price_total,
        currency=currency,
        offer_json=offer_json,
    )

    return db.latest_snapshot(watch["id"])


def should_send_email(sub: dict, watch: dict, latest_snapshot: dict) -> bool:
    """
    Decide if we should email this subscription.

    Rules:
      1. Must be a new low overall (is_new_low).
      2. Must beat this subscriber's last_emailed_cents (if any).
      3. Respect MIN_HOURS_BETWEEN_ALERTS.
    """
    current_price = latest_snapshot["price_cents"]
    last_price = sub.get("last_emailed_cents")
    last_seen_str = sub.get("last_emailed_seen_utc")

    # 1) Require a "new low" overall for this watch
    low_info = is_new_low(watch["id"])
    if not low_info:
        return False

    # 2) For this specific email, only send if price improved
    if last_price is not None and current_price >= last_price:
        return False

    # 3) Time throttle per subscription (anti-spam)
    if last_seen_str:
        try:
            last_seen = datetime.fromisoformat(last_seen_str)
            if datetime.now(timezone.utc) - last_seen < timedelta(hours=MIN_HOURS_BETWEEN_ALERTS):
                return False
        except Exception:
            # If parsing fails, ignore and treat as "no last email"
            pass

    return True


def format_email(watch: dict, snapshot: dict) -> tuple[str, str]:
    """
    Build subject + body for the alert email.
    """
    price_dollars = snapshot["price_cents"] / 100.0
    subject = (
        f"New low fare {watch['origin']} → {watch['destination']} "
        f"on {watch['depart_date']}: ${price_dollars:,.0f}"
    )
    body = (
        "Good news!\n\n"
        "We just found a new low price for one of your fare watches:\n\n"
        f"Route: {watch['origin']} → {watch['destination']}\n"
        f"Date: {watch['depart_date']}\n"
        f"Cabin: {watch['cabin']}, Adults: {watch['adults']}\n\n"
        f"Latest price: ${price_dollars:,.2f} {snapshot['currency']}\n"
        f"Seen at: {snapshot['seen_utc']} UTC\n\n"
        "You’re getting this email because you subscribed to this watch in Farewatch.\n"
    )
    return subject, body


def send_alerts_for_watch(watch: dict, latest_snapshot: dict) -> None:
    subs = db.get_subscriptions_for_watch(watch["id"])
    if not subs:
        return

    for sub in subs:
        if not should_send_email(sub, watch, latest_snapshot):
            continue

        subject, body = format_email(watch, latest_snapshot)
        send_email(subject=subject, body=body, email_to=sub["email"])

        # Update per-subscription "last emailed" so we don't double send
        db.update_subscription_last_emailed(
            subscription_id=sub["id"],
            last_emailed_cents=latest_snapshot["price_cents"],
            seen_utc=latest_snapshot["seen_utc"],
        )


def process_watch(watch: dict) -> None:
    """
    Full pipeline for one watch: take snapshot + maybe send emails.
    """
    try:
        latest = take_snapshot_for_watch(watch)
        if not latest:
            return
        send_alerts_for_watch(watch, latest)
    except Exception as e:
        log.exception("Error processing watch %s: %s", watch["id"], e)


def main():
    log.info("Scheduler run started at %s", utcnow_iso())
    db.init_db()  # safe if schema already exists + runs migrations

    watches = fetch_watches_with_subscribers()
    log.info("Found %d watches with subscriptions", len(watches))

    for watch in watches:
        process_watch(watch)

    log.info("Scheduler run finished at %s", utcnow_iso())


if __name__ == "__main__":
    main()
