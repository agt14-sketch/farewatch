import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Make project importable when running as script
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.store import db
from app.notifiers.emailer import send_email

BATCH_DELAY_MINUTES = int(os.getenv("ONBOARDING_DELAY_MINUTES", "5"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch_pending():
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=BATCH_DELAY_MINUTES)
    cutoff_iso = cutoff.replace(microsecond=0).isoformat()
    return cutoff_iso, db.fetch_pending_onboarding_rows(cutoff_iso)


def build_email_body(email: str, watches: list[dict]) -> str:
    lines = []
    lines.append("Hi,\n")
    lines.append("Thanks for using DaiLY to track your flight prices. ✈️\n")
    lines.append("You’re now tracking the following watches:\n")

    for w in watches:
        line = (
            f"• {w['origin']} → {w['destination']} on {w['depart_date']} "
            f"({w.get('cabin', 'ECONOMY')}, {w.get('adults', 1)} adult"
        )
        if w.get("adults", 1) != 1:
            line += "s"
        line += f", {w.get('currency', 'USD')})"
        lines.append(line)

    lines.append("\nWhat happens next?")
    lines.append(
        "We’ll automatically check prices in the background and email you when we detect "
        "a meaningful drop or a strong deal for any of these watches."
    )

    lines.append(
        "\nYou can manage or unsubscribe from watches anytime in the DaiLY app under “My Watches” and “Unsubscribe”."
    )

    lines.append("\nThanks again for trying DaiLY!")
    lines.append("— The DaiLY Team")

    return "\n".join(lines)


def main():
    print(f"[onboarding] starting run at {utc_now_iso()}")
    print(f"[onboarding] DATABASE_URL set? {bool(os.getenv('DATABASE_URL'))}")
    print(f"[onboarding] ENABLE_EMAIL={os.getenv('ENABLE_EMAIL')}")

    db.init_db()

    cutoff_iso, rows = fetch_pending()
    print(f"[onboarding] cutoff_iso={cutoff_iso} delay_minutes={BATCH_DELAY_MINUTES}")

    if not rows:
        print("[onboarding] no pending onboarding emails.")
        return

    print(f"[onboarding] pending rows eligible to send: {len(rows)}")

    grouped = defaultdict(list)
    for r in rows:
        grouped[r["email"]].append(r)

    now_iso = utc_now_iso()

    for email, items in grouped.items():
        body = build_email_body(email, items)
        subject = "You’re set up with DaiLY flight watches"

        print(f"[onboarding] sending onboarding email to {email} (watches={len(items)})")
        send_email(subject=subject, body=body, email_to=email)

        queue_ids = [int(item["queue_id"]) for item in items]
        db.mark_onboarding_sent(queue_ids, now_iso)

    print(f"[onboarding] run complete at {utc_now_iso()}")


if __name__ == "__main__":
    main()
