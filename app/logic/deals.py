from typing import Optional, Dict
from app.store.db import history_min_median

def is_new_low(watch_id: int) -> Optional[Dict]:
    """Return dict if latest price is the lowest ever for this watch."""
    stats = history_min_median(watch_id)
    if not stats or stats["n"] < 2:  # need at least 2 points to call a “new low”
        return None
    latest = stats.get("latest_cents")  # we’ll inject this value from caller
    if latest is None:
        return None
    if latest <= stats["min_cents"]:
        return {"type": "new_low", "min_cents": stats["min_cents"], "n": stats["n"]}
    return None

def drop_pct(old_cents: int, new_cents: int) -> float:
    if old_cents <= 0: 
        return 0.0
    return 100.0 * (old_cents - new_cents) / old_cents