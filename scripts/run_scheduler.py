# app/logic/scheduler_worker.py
import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import logging
from datetime import datetime, timedelta, timezone
import json

from app.store import db
from app.logic.deals import is_new_low, drop_pct, search_best_offer_for_watch
from app.notifiers.emailer import send_email

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

MIN_HOURS_BETWEEN_ALERTS = 6


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch_watches_with_subscribers():
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
    offer = search_best_offer_for_watch(watch)
    if not offer:
        log.info("No offer found for watch %s", watch["id"])
        return None

    price_total = float(offer["price_total"])
    currency = offer.get("currency", watch["currency"])
    provider = offer.get("provider", "amadeus")
    offer_json = offer.get("raw_json") or json.dumps(offer)

    db.append_snapshot(
        watch_id=watch["id"],
        provider=provider,
        price_total=price_total,
        currency=currency,
        offer_json=offer_json,
    )

    latest = db.latest_snapshot(watch["id"])
    return latest


def should_send_email(sub: dict, watch: dict, latest_snapshot: dict) -> bool:
    current_price = latest_snapshot["price_cents"]
    last_price = sub.get("last_emailed_cents")
    last_seen_str = sub.get("last_emailed_seen_utc")

    low_info = is_new_low(watch["id"])
    if not low_info:
        return False

    if last_price is not None and current_price >= last_price:
        return False

    if last_seen_str:
        try:
            last_seen = datetime.fromisoformat(last_seen_str)
            if datetime.now(timezone.utc) - last_seen < timedelta(hours=MIN_HOURS_BETWEEN_ALERTS):
                return False
        except Exception:
            pass

    return True


def send_alerts_for_watch(watch: dict, latest_snapshot: dict):
    subs = db.get_subscriptions_for_watch(watch["id"])
    if not subs:
        return

    for sub in subs:
        if not should_send_email(sub, watch, latest_snapshot):
            continue

        email = sub["email"]

        subject = f"New low fare {watch['origin']} → {watch['destination']} on {watch['depart_date']}"
        body = (
            f"Watch #{watch['id']}\n"
            f"Route: {watch['origin']} → {watch['destination']}\n"
            f"Depart: {watch['depart_date']}\n"
            f"Cabin: {watch.get('cabin', 'ECONOMY')}\n"
            f"Adults: {watch.get('adults', 1)}\n\n"
            f"Latest price: {latest_snapshot['price_cents'] / 100:.2f} {latest_snapshot['currency']}\n"
        )

        send_email(subject, body, email_to=email)

        db.update_subscription_last_emailed(
            subscription_id=sub["id"],
            last_emailed_cents=latest_snapshot["price_cents"],
            seen_utc=latest_snapshot["seen_utc"],
        )


def process_watch(watch: dict):
    try:
        latest = take_snapshot_for_watch(watch)
        if not latest:
            return

        send_alerts_for_watch(watch, latest)
    except Exception as e:
        log.exception("Error processing watch %s: %s", watch["id"], e)


def run_scheduler_once():
    log.info("Starting scheduler run at %s", utcnow_iso())
    db.init_db()  # safe to call repeatedly

    watches = fetch_watches_with_subscribers()
    log.info("Found %d watches with subscriptions", len(watches))

    for w in watches:
        process_watch(w)

    log.info("Scheduler run complete at %s", utcnow_iso())
