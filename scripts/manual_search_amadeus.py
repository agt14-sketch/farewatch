# scripts/manual_search_amadeus.py

import os
import sys
from dotenv import load_dotenv

# 1) allow imports from the project root (so `app/...` works)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# 2) now import your client
from app.services.amadeus_client import AmadeusClient

# 3) load env
load_dotenv()

# 4) params (env overrides allowed)
ORIGIN = os.getenv("ORIGIN", "BWI")
DEST   = os.getenv("DEST",   "SFO")
DATE   = os.getenv("DATE",   "2025-12-10")  # YYYY-MM-DD

ama = AmadeusClient()
offers = ama.search_offers(ORIGIN, DEST, DATE, adults=1, cabin="ECONOMY", currency="USD", limit=10)
print(f"Found {len(offers)} offers for {ORIGIN} -> {DEST} on {DATE}")
if not offers:
    raise SystemExit("No offers found. Try another date/route.")

cheapest = min(offers, key=lambda o: float(o["price"]["total"]))
print("Cheapest (search):", cheapest["price"]["total"], cheapest["price"]["currency"])

confirmed = ama.price_confirm(cheapest)
print("Confirmed (pricing):", confirmed["price"]["total"], confirmed["price"]["currency"])