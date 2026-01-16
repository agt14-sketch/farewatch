import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Make "app" importable when running as a script
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.store import db
from app.notifiers.emailer import send_email


BATCH_DELAY_MINUTES = int(os.getenv("ONBOARDING_DELAY_MINUTES", "5"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch_pending():
    """
    Fetch all queue rows that are old enough to send.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=BATCH_DELAY_MINUTES)
    cutoff_iso = cutoff.replace(microsecond=0).isoformat()
    return db.fetch_pending_onboarding_rows(cutoff_iso)


def build_email_body(email: str, watches: list[dict]) -> str:
    """
    Create a nice, professional onboarding email body.
    Each 'watch' dict has origin, destination, depart_date, cabin, adults, currency, etc.
    """
    lines = []

    lines.append(f"Hi,\n")
    lines.append("Thanks for using DaiLY to track your flight prices. ✈️\n")
    lines.append("You’ve just created or updated the following watches:\n")

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
        "We’ll automatically check prices in the background and email you when we see a "
        "notable drop or a strong deal for any of these routes."
    )

    lines.append(
        "\nYou can manage or unsubscribe from watches anytime by opening the DaiLY app "
        "and using the “My Watches” and “Unsubscribe” sections."
    )

    lines.append("\nThanks again for trying DaiLY!")
    lines.append("— The DaiLY Team")

    return "\n".join(lines)


def main():
    print(f"[onboarding] starting run at {utc_now_iso()}")
    db.init_db()

    rows = fetch_pending()
    if not rows:
        print("[onboarding] no pending onboarding emails.")
        return

    # Group by recipient email
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r["email"]].append(r)

    now_iso = utc_now_iso()

    for email, items in grouped.items():
        # Build email body using the watch details
        body = build_email_body(email, items)

        subject = "You’re set up with DaiLY flight watches"
        print(f"[onboarding] sending onboarding email to {email} with {len(items)} watches")
        send_email(subject=subject, body=body, email_to=email)

        # Mark all these queue rows as sent
        queue_ids = [item["queue_id"] for item in items]
        db.mark_onboarding_sent(queue_ids, now_iso)

    print(f"[onboarding] run complete at {utc_now_iso()}")


if __name__ == "__main__":
    main()
