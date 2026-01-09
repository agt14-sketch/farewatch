import os
import logging
from typing import Optional, Dict, Any

from amadeus import Client, ResponseError

from app.store.db import history_min_median

log = logging.getLogger(__name__)

# Amadeus client using your env vars
AMADEUS = Client(
    client_id=os.getenv("AMADEUS_CLIENT_ID"),
    client_secret=os.getenv("AMADEUS_CLIENT_SECRET"),
)


def is_new_low(watch_id: int) -> Optional[Dict]:
    """Return dict if latest price is the lowest ever for this watch."""
    stats = history_min_median(watch_id)
    if not stats or stats["n"] < 2:  # need at least 2 points to call a “new low”
        return None

    latest = stats.get("latest_cents")
    if latest is None:
        return None

    if latest <= stats["min_cents"]:
        return {
            "type": "new_low",
            "min_cents": stats["min_cents"],
            "n": stats["n"],
        }
    return None


def drop_pct(old_cents: int, new_cents: int) -> float:
    if old_cents <= 0:
        return 0.0
    return 100.0 * (old_cents - new_cents) / old_cents


def search_best_offer_for_watch(watch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Call Amadeus for a one-way flight that matches this watch and
    return the single best (cheapest) offer.

    Expects `watch` to have:
        origin, destination, depart_date, cabin, adults, currency

    Returns a dict shaped like:
        {
            "price_total": "123.45",
            "currency": "USD",
            "provider": "amadeus",
            "raw_json": <full offer dict>,
        }
    or None if nothing found / error.
    """
    try:
        response = AMADEUS.shopping.flight_offers_search.get(
            originLocationCode=watch["origin"],
            destinationLocationCode=watch["destination"],
            departureDate=watch["depart_date"],
            adults=int(watch.get("adults", 1)),
            travelClass=watch.get("cabin", "ECONOMY"),
            currencyCode=watch.get("currency", "USD"),
            max=20,  # you can tweak this
        )
    except ResponseError as e:
        log.warning("Amadeus error for watch %s: %s", watch.get("id"), e)
        return None
    except Exception as e:
        log.exception("Unexpected error calling Amadeus for watch %s: %s", watch.get("id"), e)
        return None

    offers = response.data or []
    if not offers:
        log.info("No Amadeus offers returned for watch %s", watch.get("id"))
        return None

    # Pick the cheapest by grandTotal
    def total_price_cents(offer: Dict) -> int:
        price = offer.get("price", {})
        grand_total = float(price.get("grandTotal", price.get("total", 0)))
        return int(round(grand_total * 100))

    best = min(offers, key=total_price_cents)
    price = best.get("price", {})
    grand_total = float(price.get("grandTotal", price.get("total", 0.0)))
    currency = price.get("currency", watch.get("currency", "USD"))

    return {
        "price_total": str(grand_total),
        "currency": currency,
        "provider": "amadeus",
        "raw_json": best,
    }
