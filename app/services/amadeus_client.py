# app/services/amadeus_client.py
import os, time, requests
from dotenv import load_dotenv
from typing import Dict, List

load_dotenv()

BASE = "https://test.api.amadeus.com"
KEY = os.getenv("AMADEUS_KEY")
SECRET = os.getenv("AMADEUS_SECRET")

class AmadeusHTTPError(Exception):
    def __init__(self, status: int, payload: dict):
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload

class AmadeusClient:
    def __init__(self):
        if not KEY or not SECRET:
            raise RuntimeError("Set AMADEUS_KEY and AMADEUS_SECRET in your .env")
        self._token = None

    def token(self) -> str:
        if self._token:
            return self._token
        r = requests.post(
            f"{BASE}/v1/security/oauth2/token",
            data={"grant_type": "client_credentials", "client_id": KEY, "client_secret": SECRET},
            timeout=20,
        )
        r.raise_for_status()
        self._token = r.json()["access_token"]
        return self._token

    def _headers(self):
        return {"Authorization": f"Bearer {self.token()}"}

    # ⚙️ central request with selective retries (5xx only)
    def _request(self, method: str, url: str, **kwargs) -> dict:
        max_retries = kwargs.pop("max_retries", 3)
        backoff = 0.75
        for attempt in range(max_retries):
            r = requests.request(method, url, headers=self._headers(), timeout=30, **kwargs)
            ct = r.headers.get("content-type", "")
            body = {}
            try:
                if "json" in ct:
                    body = r.json()
            except Exception:
                body = {"raw": r.text[:300]}

            if 200 <= r.status_code < 300:
                return body

            # 401 → refresh token once
            if r.status_code == 401 and attempt == 0:
                self._token = None
                _ = self.token()
                continue

            # 5xx → retry with backoff
            if 500 <= r.status_code < 600 and attempt < max_retries - 1:
                time.sleep(backoff)
                backoff *= 2
                continue

            # Otherwise raise with payload so caller can decide
            raise AmadeusHTTPError(r.status_code, body)

        raise AmadeusHTTPError(599, {"error": "retry_exhausted"})

    def search_offers(self, origin: str, dest: str, depart_date: str,
                      adults: int = 1, cabin: str = "ECONOMY",
                      currency: str = "USD", limit: int = 20) -> List[Dict]:
        params = {
            "originLocationCode": origin.upper(),
            "destinationLocationCode": dest.upper(),
            "departureDate": depart_date,  # YYYY-MM-DD
            "adults": max(1, int(adults)),
            "travelClass": cabin,          # ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST
            "currencyCode": currency,
            "max": min(int(limit), 20),
        }
        body = self._request("GET", f"{BASE}/v2/shopping/flight-offers", params=params)
        return body.get("data", [])

    def price_confirm(self, offer: Dict) -> Dict:
        # ⚙️ MUST send the offer exactly as returned by search
        payload = {"data": {"type": "flight-offers-pricing", "flightOffers": [offer]}}
        body = self._request("POST", f"{BASE}/v1/shopping/flight-offers/pricing",
                             json=payload)
        return body["data"]["flightOffers"][0]
    
    def try_price_confirm(self, offers, max_candidates: int = 3):
        """
        Try to price-confirm up to `max_candidates` cheapest offers.
        Skips 400/4926, retries 5xx, returns the first confirmed offer.
        """
        # sort by advertised total
        cand = sorted(offers, key=lambda o: float(o["price"]["total"]))[:max_candidates]
        last_err = None
        for i, off in enumerate(cand, 1):
            try:
                return self.price_confirm(off)  # your price_confirm already retries 5xx
            except AmadeusHTTPError as e:
                last_err = e
                # skip unconfirmable offers (common: 400/4926)
                if e.status == 400:
                    continue
                # 5xx is retried inside price_confirm; if we still get here, try next offer
                continue
        # if none confirmed, bubble up the last error
        if last_err:
            raise last_err
        raise RuntimeError("No offers to confirm")