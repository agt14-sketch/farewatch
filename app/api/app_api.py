import os
from datetime import date, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, EmailStr, field_validator

from app.services.amadeus_client import AmadeusClient, AmadeusHTTPError
from app.store.db import (
    init_db,
    list_watches,
    delete_watch_by_id,
    ensure_watch,
    history_min_median,
    history_for_watch,
    get_global_min_for_window,
    ensure_subscription,
    get_subscriptions_for_watch,
    delete_subscription,

)

from fastapi.middleware.cors import CORSMiddleware

VALID_CABINS = {"ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"}

# window config (matches your scheduler defaults)
ORIGIN = os.getenv("ORIGIN", "BWI").upper()
DEST = os.getenv("DEST", "SFO").upper()
START_OFFSET_DAYS = int(os.getenv("START_OFFSET_DAYS", "30"))
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "30"))
START_DATE = os.getenv("START_DATE")  # optional YYYY-MM-DD

app = FastAPI(
    title="Farewatch API",
    description="Local API for your flight price watcher.",
    version="0.1.0",
    on_startup=[init_db],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # later you can lock to your Streamlit URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------------
# Models
# -------------------------
class SearchRequest(BaseModel):
    origin: str = Field(..., min_length=3, max_length=3, description="IATA code of origin airport", examples=["BWI"])
    destination: str = Field(..., min_length=3, max_length=3, description="IATA code of destination airport", examples=["SFO"])
    depart_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="Departure date YYYY-MM-DD", examples=["2025-12-25"])
    adults: int = Field(1, ge=1, le=9)
    cabin: str = Field("ECONOMY", description="Cabin class: ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST")
    currency: str = Field("USD", min_length=3, max_length=3)
    max_price: Optional[float] = Field(None, gt=0)
    max_results: int = Field(10, ge=1, le=50)

    @field_validator("origin", "destination", "currency")
    @classmethod
    def upper_codes(cls, v: str) -> str:
        return v.upper()

    @field_validator("cabin")
    @classmethod
    def normalize_cabin(cls, v: str) -> str:
        v_up = v.upper()
        if v_up not in VALID_CABINS:
            raise ValueError(f"cabin must be one of {sorted(VALID_CABINS)}")
        return v_up


class SearchOffer(BaseModel):
    total: float
    currency: str
    carrier: Optional[str] = None
    segments: Optional[int] = None
    duration: Optional[str] = None


class WatchCreate(BaseModel):
    origin: str = Field(..., min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    destination: str = Field(..., min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    depart_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    adults: int = Field(1, ge=1, le=9)
    cabin: str = Field("ECONOMY", description="Cabin class: ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST")
    currency: str = Field("USD", min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    alert_email: Optional[EmailStr] = None  # optional subscription on create

    @field_validator("origin", "destination", "currency")
    @classmethod
    def upper_codes(cls, v: str) -> str:
        return v.upper()

    @field_validator("cabin")
    @classmethod
    def normalize_cabin(cls, v: str) -> str:
        v_up = v.upper()
        if v_up not in VALID_CABINS:
            raise ValueError(f"cabin must be one of {sorted(VALID_CABINS)}")
        return v_up


class SubscribeRequest(BaseModel):
    watch_id: int
    email: EmailStr


# -------------------------
# Basic routes
# -------------------------
@app.get("/")
def index():
    return {
        "message": "farewatch api up",
        "routes": {
            "docs": "/docs",
            "health": "/healthz",
            "search": "/search",
            "watches": "/watches",
            "subscriptions": "/subscriptions",
            "cheapest": "/cheapest",
            "window": "/window",
        },
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}


# -------------------------
# Watches
# -------------------------
@app.get("/watches")
def get_watches(alert_email: Optional[str] = Query(default=None, description="Filter by subscriber email (optional)")):
    watches = list_watches()

    # attach stats (helps streamlit UI)
    for w in watches:
        stats = history_min_median(w["id"])
        if stats:
            w["n"] = stats["n"]
            w["latest_cents"] = stats["latest_cents"]
            w["min_cents"] = stats["min_cents"]
            w["median_cents"] = stats["median_cents"]
        else:
            w["n"] = 0
            w["latest_cents"] = None
            w["min_cents"] = None
            w["median_cents"] = None

    # NOTE: filtering by alert_email only makes sense if your watch table stores it;
    # your new design is subscriptions-based. So we don't filter watches by email here.
    # Streamlit should instead call /watches/{id}/subscriptions and filter client-side if needed.

    return {"count": len(watches), "watches": watches}


@app.post("/watches")
def create_watch(req: WatchCreate):
    """
    Create/reuse a watch.
    Optionally subscribe alert_email to that watch.
    """
    try:
        wid = ensure_watch(
            req.origin,
            req.destination,
            req.depart_date,
            req.cabin,
            req.adults,
            req.currency,
        )

        sub_id: Optional[int] = None
        if req.alert_email:
            sub_id = ensure_subscription(wid, str(req.alert_email))

        return {
            "watch_id": wid,
            "subscription_id": sub_id,
            "origin": req.origin,
            "destination": req.destination,
            "depart_date": req.depart_date,
            "adults": req.adults,
            "cabin": req.cabin,
            "currency": req.currency,
            "alert_email": str(req.alert_email) if req.alert_email else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/watches/{watch_id}")
def delete_watch(watch_id: int):
    delete_watch_by_id(watch_id)
    return {"ok": True, "watch_id": watch_id}


# -------------------------
# Subscriptions (multi-user emails per watch)
# -------------------------
@app.post("/subscriptions")
def subscribe(req: SubscribeRequest):
    try:
        sid = ensure_subscription(req.watch_id, str(req.email))
        return {"subscription_id": sid, "watch_id": req.watch_id, "email": str(req.email)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/watches/{watch_id}/subscriptions")
def get_watch_subscriptions(watch_id: int):
    try:
        return {"count": 0, "subscriptions": get_subscriptions_for_watch(watch_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/subscriptions")
def unsubscribe(req: SubscribeRequest):
    """
    Delete subscription by (watch_id, email)
    """
    try:
        deleted = delete_subscription(req.watch_id, str(req.email))
        return {"deleted": deleted, "watch_id": req.watch_id, "email": str(req.email)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------
# History + window helpers
# -------------------------
@app.get("/history/{watch_id}")
def get_history(watch_id: int):
    rows = history_for_watch(watch_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No history for that watch_id")
    for r in rows:
        r["price_usd"] = r["price_cents"] / 100.0
    return rows


@app.get("/window")
def get_window_info():
    if START_DATE:
        start = date.fromisoformat(START_DATE)
    else:
        start = date.today() + timedelta(days=START_OFFSET_DAYS)

    end = start + timedelta(days=WINDOW_DAYS - 1)

    watches = list_watches()
    rows = []

    for w in watches:
        if w["origin"] != ORIGIN or w["destination"] != DEST:
            continue

        dep = date.fromisoformat(w["depart_date"])
        if not (start <= dep <= end):
            continue

        stats = history_min_median(w["id"])
        if not stats:
            continue

        rows.append(
            {
                "watch_id": w["id"],
                "depart_date": w["depart_date"],
                "origin": w["origin"],
                "destination": w["destination"],
                "n_snapshots": stats["n"],
                "min_usd": stats["min_cents"] / 100.0,
                "median_usd": stats["median_cents"] / 100.0,
                "latest_usd": stats["latest_cents"] / 100.0,
            }
        )

    rows.sort(key=lambda r: r["depart_date"])

    return {
        "origin": ORIGIN,
        "destination": DEST,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "days": rows,
    }


@app.get("/cheapest")
def get_cheapest_in_window():
    start = date.today() + timedelta(days=START_OFFSET_DAYS)
    end = start + timedelta(days=WINDOW_DAYS - 1)

    gm = get_global_min_for_window(ORIGIN, DEST, start.isoformat(), end.isoformat())
    if not gm:
        return {"message": "No data yet for this window."}

    return {
        "origin": ORIGIN,
        "destination": DEST,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "depart_date": gm["depart_date"],
        "price_usd": gm["min_cents"] / 100.0,
        "watch_id": gm["watch_id"],
    }


# -------------------------
# Live search
# -------------------------
@app.post("/search", response_model=dict)
def search_flights(req: SearchRequest):
    client = AmadeusClient()

    try:
        offers = client.search_offers(
            origin=req.origin,
            dest=req.destination,
            depart_date=req.depart_date,
            adults=req.adults,
            cabin=req.cabin,
            currency=req.currency,
            limit=20,
        )
    except AmadeusHTTPError as e:
        raise HTTPException(status_code=e.status, detail=e.payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not offers:
        return {
            "origin": req.origin,
            "destination": req.destination,
            "depart_date": req.depart_date,
            "count": 0,
            "offers": [],
            "message": "No offers found for that search.",
        }

    simplified: list[SearchOffer] = []

    for o in offers:
        price = o.get("price") or {}
        total_str = price.get("total")
        if total_str is None:
            continue
        try:
            total = float(total_str)
        except ValueError:
            continue

        itineraries = o.get("itineraries") or []
        carrier = None
        segments = 0
        duration = None

        if itineraries:
            it = itineraries[0]
            duration = it.get("duration")
            segs = it.get("segments") or []
            segments = len(segs)
            if segs:
                carrier = segs[0].get("carrierCode")

        simplified.append(
            SearchOffer(
                total=total,
                currency=price.get("currency", req.currency),
                carrier=carrier,
                segments=segments,
                duration=duration,
            )
        )

    simplified.sort(key=lambda s: s.total)

    if req.max_price is not None:
        simplified = [s for s in simplified if s.total <= req.max_price]

    simplified = simplified[: req.max_results]

    return {
        "origin": req.origin,
        "destination": req.destination,
        "depart_date": req.depart_date,
        "count": len(simplified),
        "offers": [s.model_dump() for s in simplified],
    }