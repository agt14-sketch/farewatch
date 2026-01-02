# scripts/snapshot_window.py
import os, sys, json, time
from datetime import date, timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv
from app.services.amadeus_client import AmadeusClient, AmadeusHTTPError
from app.store.db import init_db, ensure_watch, append_snapshot
from app.logic.deals import is_new_low, drop_pct
from app.store.db import history_min_median

load_dotenv()

ORIGIN   = os.getenv("ORIGIN", "BWI").upper()
DEST     = os.getenv("DEST", "SFO").upper()
ADULTS   = int(os.getenv("ADULTS", "1"))
CABIN    = os.getenv("CABIN", "BUSINESS")   # ECONOMY | PREMIUM_ECONOMY | BUSINESS | FIRST
CURRENCY = os.getenv("CURRENCY", "USD").upper()

START_OFFSET_DAYS = int(os.getenv("START_OFFSET_DAYS", "30"))
WINDOW_DAYS       = int(os.getenv("WINDOW_DAYS", "50"))
SLEEP_BETWEEN     = float(os.getenv("SLEEP_BETWEEN", "0.5"))
MAX_CANDIDATES    = int(os.getenv("MAX_CANDIDATES", "3"))

VALID_CLASSES = {"ECONOMY","PREMIUM_ECONOMY","BUSINESS","FIRST"}

def main():
    if os.path.exists("STOP"):
        print("STOP present; exit.")
        return

    # basic param validation to avoid 400s
    if CABIN not in VALID_CLASSES:
        print(f"Invalid travelClass '{CABIN}'. Use one of {sorted(VALID_CLASSES)}.")
        return
    if len(ORIGIN) != 3 or len(DEST) != 3:
        print("IATA codes must be 3 letters (e.g., BWI, SFO).")
        return

    init_db()
    ama = AmadeusClient()

    start = date.today() + timedelta(days=START_OFFSET_DAYS)
    targets = [start + timedelta(days=i) for i in range(WINDOW_DAYS)]
    print(f"Scanning {ORIGIN}->{DEST} for {len(targets)} dates starting {start.isoformat()}")

    for d in targets:
        if os.path.exists("STOP"):
            print("STOP present; stopping mid-loop.")
            return

        depart_str = d.isoformat()
        try:
            offers = ama.search_offers(ORIGIN, DEST, depart_str,
                                       adults=ADULTS, cabin=CABIN,
                                       currency=CURRENCY, limit=10)
            if not offers:
                print(f"[{depart_str}] no offers (skipping)")
                time.sleep(SLEEP_BETWEEN); continue

            # OPTIONAL hygiene: drop obviously broken items
            offers = [o for o in offers if "price" in o and "total" in o["price"] and o.get("itineraries")]
            if not offers:
                print(f"[{depart_str}] offers returned but unusable (skip)")
                time.sleep(SLEEP_BETWEEN); continue

            #  key change: try up to MAX_CANDIDATES cheapest until one confirms
            confirmed = ama.try_price_confirm(offers, max_candidates=MAX_CANDIDATES)

            wid = ensure_watch(ORIGIN, DEST, depart_str, CABIN, ADULTS, CURRENCY)
            append_snapshot(
                wid, "amadeus",
                confirmed["price"]["total"], confirmed["price"]["currency"],
                json.dumps(confirmed)
            )
            print(f"[{depart_str}] saved: ${confirmed['price']['total']} {confirmed['price']['currency']}")
            
            # -------- Deal checks (console alerts + email) --------
            stats = history_min_median(wid)
            if stats:
                latest = stats["latest_cents"]

                # 🔥 new all-time low?
                nl = is_new_low(wid)
                if nl:
                    msg = f"NEW LOW {ORIGIN}->{DEST} on {depart_str}: ${latest/100:.2f} (n={stats['n']})"
                    print("🔥", msg)

                # 💡 drop vs median (optional for now – console only)
                dp = drop_pct(stats["median_cents"], latest)
                if dp >= 15:
                    print(f"💡 {depart_str} is {dp:.1f}% below median: "
                        f"${latest/100:.2f} vs ${stats['median_cents']/100:.2f}")

            
                
        except AmadeusHTTPError as e:
            print(f"[{depart_str}] HTTP {e.status} → {e.payload} (skip)")
        except Exception as ex:
            print(f"[{depart_str}] unexpected error: {ex}")
        finally:
            time.sleep(SLEEP_BETWEEN)

if __name__ == "__main__":
    main()