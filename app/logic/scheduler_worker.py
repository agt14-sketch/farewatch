# app/logic/scheduler_worker.py
import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone

# Ensure project root is importable when run as module or script
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.store import db
from app.logic.deals import is_new_low, search_best_offer_for_watch
from app.notifiers.emailer import send_email

log = logging.getLogger("scheduler_worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

MIN_HOURS_BETWEEN_ALERTS = 6


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch_watches():
    """
    Process ALL watches so Streamlit can show snapshots even if nobody subscribed yet.
    Emails still only go to subscribed users.
    """
    return db.list_watches()


def take_snapshot_for_watch(watch: dict) -> dict | None:
    offer = search_best_offer_for_watch(watch)
    if not offer:
        log.info("watch_id=%s no offer found", watch["id"])
        return None

    price_total = float(offer["price_total"])
    currency = offer.get("currency") or watch.get("currency", "USD")
    provider = offer.get("provider", "amadeus")

    # Store raw JSON safely
    raw = offer.get("raw_json")
    offer_json = raw if isinstance(raw, str) else json.dumps(raw or offer)

    db.append_snapshot(
        watch_id=watch["id"],
        provider=provider,
        price_total=price_total,
        currency=currency,
        offer_json=offer_json,
    )

    return db.latest_snapshot(watch["id"])


def should_send_email(sub: dict, watch: dict, latest_snapshot: dict) -> bool:
    current_price = int(latest_snapshot["price_cents"])
    last_price = sub.get("last_emailed_cents")
    last_seen_str = sub.get("last_emailed_seen_utc")

    # Only alert on "new low" (your deals.py logic)
    low_info = is_new_low(watch["id"])
    if not low_info:
        return False

    # Don’t spam if price didn’t beat last emailed price
    if last_price is not None and current_price >= int(last_price):
        return False

    # Basic cool-down
    if last_seen_str:
        try:
            last_seen = datetime.fromisoformat(last_seen_str)
            if datetime.now(timezone.utc) - last_seen < timedelta(hours=MIN_HOURS_BETWEEN_ALERTS):
                return False
        except Exception:
            # if parsing fails, don't block alerts
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
        price = latest_snapshot["price_cents"] / 100

        subject = f"New low fare {watch['origin']} → {watch['destination']} on {watch['depart_date']}"
        body = (
            f"Hi,\n\n"
            f"We found a new low price for one of your DaiLY watches.\n\n"
            f"Watch #{watch['id']}\n"
            f"Route: {watch['origin']} → {watch['destination']}\n"
            f"Depart: {watch['depart_date']}\n"
            f"Cabin: {watch.get('cabin', 'ECONOMY')}\n"
            f"Adults: {watch.get('adults', 1)}\n\n"
            f"Latest price: {price:.2f} {latest_snapshot['currency']}\n\n"
            f"— The DaiLY Team"
        )

        send_email(subject=subject, body=body, email_to=email)

        db.update_subscription_last_emailed(
            subscription_id=sub["id"],
            last_emailed_cents=int(latest_snapshot["price_cents"]),
            seen_utc=latest_snapshot["seen_utc"],
        )


def process_watch(watch: dict):
    try:
        latest = take_snapshot_for_watch(watch)
        if not latest:
            return
        send_alerts_for_watch(watch, latest)
    except Exception as e:
        log.exception("watch_id=%s error=%s", watch.get("id"), e)


def run_scheduler_once():
    log.info("==== scheduler run started at %s ====", utcnow_iso())

    # sanity
    log.info("DATABASE_URL set? %s", bool(os.getenv("DATABASE_URL")))
    log.info("ENABLE_EMAIL=%s", os.getenv("ENABLE_EMAIL"))

    db.init_db()

    watches = fetch_watches()
    log.info("Total watches in DB: %d", len(watches))

    for w in watches:
        log.info("processing watch_id=%s %s→%s %s", w["id"], w["origin"], w["destination"], w["depart_date"])
        process_watch(w)

    log.info("==== scheduler run complete at %s ====", utcnow_iso())


if __name__ == "__main__":
    run_scheduler_once()
