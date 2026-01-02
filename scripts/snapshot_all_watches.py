import os
import sys

# allow imports from parent directories (app/)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
import time
from datetime import datetime, date, timezone
from app.services.amadeus_client import AmadeusClient, AmadeusHTTPError

from app.store.db import (
    list_watches,
    append_snapshot,
    history_min_median,
    get_subscriptions_for_watch,
    update_subscription_last_emailed
)
from app.logic.deals import drop_pct
from app.notifiers.emailer import send_email  # your existing email helper


SLEEP_BETWEEN = float(os.getenv("SNAPSHOT_SLEEP_BETWEEN", "1.0"))  # seconds
NEW_LOW_DROP_PCT = float(os.getenv("NEW_LOW_DROP_PCT", "15.0"))    # % below median for info logging

def snapshot_single_watch(client: AmadeusClient, w: dict) -> None:
    wid = w["id"]
    origin = w["origin"]
    dest = w["destination"]
    depart_date = w["depart_date"]
    cabin = w.get("cabin", "ECONOMY")
    adults = w.get("adults", 1)
    currency = w.get("currency", "USD")

    try:
        offers = client.search_offers(
            origin=origin,
            dest=dest,
            depart_date=depart_date,
            adults=adults,
            cabin=cabin,
            currency=currency,
            limit=10,
        )
    except AmadeusHTTPError as e:
        print(f"[watch {wid} {origin}->{dest} {depart_date}] HTTP {e.status}: {e.payload} (skipping)")
        return
    except Exception as e:
        print(f"[watch {wid} {origin}->{dest} {depart_date}] unexpected error: {e}")
        return

    if not offers:
        print(f"[watch {wid} {origin}->{dest} {depart_date}] no offers (skipping)")
        return

    # sort offers cheapest → most expensive
    offers_sorted = sorted(offers, key=lambda o: float(o["price"]["total"]))

    confirmed = None
    for offer in offers_sorted:
        try:
            confirmed = client.price_confirm(offer)
            break
        except AmadeusHTTPError as e:
            if e.status == 400:
                print(
                    f"[watch {wid} {origin}->{dest} {depart_date}] "
                    f"pricing failed for one offer: {e.payload} (trying next offer)"
                )
                continue
            print(
                f"[watch {wid} {origin}->{dest} {depart_date}] "
                f"unexpected pricing error {e.status}: {e.payload} (skipping watch)"
            )
            return

    if confirmed is None:
        print(
            f"[watch {wid} {origin}->{dest} {depart_date}] "
            "all offers failed pricing (No fare applicable). Skipping."
        )
        return

    price_total = float(confirmed["price"]["total"])
    price_curr = confirmed["price"]["currency"]
    seen_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    append_snapshot(
        wid,
        "amadeus",
        price_total,
        price_curr,
        json.dumps(confirmed),
    )
    print(f"[watch {wid} {origin}->{dest} {depart_date}] saved: ${price_total:.2f} {price_curr}")

    # --- STATS for this watch
    stats = history_min_median(wid)
    if not stats:
        return

    latest_cents = stats["latest_cents"]
    latest_usd = latest_cents / 100.0
    median_usd = stats["median_cents"] / 100.0

    dp = drop_pct(stats["median_cents"], latest_cents)
    print(
        f"[watch {wid}] candidate email: latest ${latest_usd:.2f}, "
        f"{dp:.1f}% below median ${median_usd:.2f}"
    )

    # --- EMAIL ALL SUBSCRIBERS FOR THIS WATCH ---
    subs = get_subscriptions_for_watch(wid)

    if not subs:
        print(f"[watch {wid}] no subscribers; skipping email send.")
        return

    for sub in subs:
        sub_id = sub["id"]
        email_to = sub["email"]
        last_emailed_cents = sub.get("last_emailed_cents")

        # Anti-spam per subscriber
        if last_emailed_cents is not None and latest_cents >= last_emailed_cents:
            print(f"[watch {wid}] {email_to} already emailed <= this price; skipping.")
            continue

        subject = f"[Farewatch] New best price {origin}->{dest} {depart_date}: ${latest_usd:.2f}"
        body = (
            f"New best observed price for {origin}->{dest} on {depart_date}:\n"
            f"  Current price: ${latest_usd:.2f}\n"
            f"  Median so far: ${median_usd:.2f} ({dp:.1f}% below median)\n"
            f"  Snapshots so far: {stats['n']}\n\n"
            "If this looks good, consider booking soon."
        )

        send_email(subject, body, email_to)
        print(f"[watch {wid}] email sent to {email_to}")

        update_subscription_last_emailed(sub_id, latest_cents, seen_utc)
        print(f"[watch {wid}] subscription {sub_id} last_emailed updated.")

def main() -> None:
    client = AmadeusClient()
    watches = list_watches()
    print(f"[snapshot_all] running for {len(watches)} watches")
    if not watches:
        print("[snapshot_all] no watches in DB, nothing to do")
        return

    today = date.today()
    print(f"[snapshot_all] running for {len(watches)} watches")

    for w in watches:
        # Skip past departures
        try:
            depart = datetime.strptime(w["depart_date"], "%Y-%m-%d").date()
        except ValueError:
            print(f"[watch {w['id']}] bad depart_date '{w['depart_date']}' – skipping")
            continue

        if depart < today:
            print(f"[watch {w['id']}] {w['depart_date']} is in the past – skipping")
            continue

        snapshot_single_watch(client, w)
        time.sleep(SLEEP_BETWEEN)

if __name__ == "__main__":
    main()
        