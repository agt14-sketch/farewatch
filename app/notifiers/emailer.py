import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

def send_email(subject: str, body: str, email_to: str | None):
    """
    Send a plain-text email if ENABLE_EMAIL=true and env vars are set.

    Priority:
      1. Use the email_to argument if provided.
      2. Otherwise fall back to ALERT_EMAIL_TO from env.
    If anything important is missing, it just logs and returns silently.
    """
    if os.getenv("ENABLE_EMAIL", "").lower() != "true":
        # alerts disabled; do nothing
        return

    api_key = os.getenv("SENDGRID_API_KEY")
    fallback_to = os.getenv("ALERT_EMAIL_TO")
    from_email = os.getenv("ALERT_EMAIL_FROM")

    # Choose recipient: per-watch email > fallback
    to_email = email_to or fallback_to

    if not api_key or not to_email or not from_email:
        print("[emailer] Missing SENDGRID_API_KEY / ALERT_EMAIL_FROM / recipient email; not sending.")
        return

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        plain_text_content=body,
    )

    try:
        sg = SendGridAPIClient(api_key)
        resp = sg.send(message)
        print(f"[emailer] Sent email '{subject}' to {to_email} (status={resp.status_code})")
    except Exception as e:
        print(f"[emailer] Error sending email: {e}")