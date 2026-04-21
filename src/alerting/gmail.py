"""Gmail alert backend.

Sends emails via Gmail SMTP. Requires:
  - GMAIL_ADDRESS: Your Gmail address
  - GMAIL_APP_PASSWORD: App password (not your regular password)
  - GMAIL_RECIPIENT: Where to send alerts (can be same as GMAIL_ADDRESS)

Setup:
  1. Enable 2FA on your Google account
  2. Go to myaccount.google.com → Security → App passwords
  3. Generate an app password for "Mail"
  4. Add to .env:
     GMAIL_ADDRESS=you@gmail.com
     GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
     GMAIL_RECIPIENT=you@gmail.com
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587

SUBJECT_PREFIX = {
    "paper_fill":    "[Kalshi] Paper Fill",
    "live_fill":     "[Kalshi] LIVE Fill",
    "risk_reject":   "[Kalshi] Risk Reject",
    "paper_settle":  "[Kalshi] Paper Settle",
    "system_error":  "[Kalshi] ERROR",
    "daily_summary": "[Kalshi] Daily Summary",
}


class GmailAlert:
    """Send alerts via Gmail SMTP."""

    def __init__(
        self,
        address: str | None = None,
        app_password: str | None = None,
        recipient: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.address = address if address is not None else os.environ.get("GMAIL_ADDRESS", "")
        self.app_password = app_password if app_password is not None else os.environ.get("GMAIL_APP_PASSWORD", "")
        self.recipient = recipient if recipient is not None else os.environ.get("GMAIL_RECIPIENT", "")
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "gmail"

    @property
    def configured(self) -> bool:
        return bool(self.address and self.app_password and self.recipient)

    def send(self, event_type: str, message: str, details: dict | None = None,
             html_body: str | None = None) -> bool:
        if not self.configured:
            logger.debug("Gmail not configured — skipping alert")
            return False

        subject = SUBJECT_PREFIX.get(event_type, f"[Kalshi] {event_type}")

        if html_body is None:
            html_body = f"<h3>{event_type.replace('_', ' ').title()}</h3>"
            html_body += f"<pre>{message}</pre>"
            if details:
                html_body += "<table border='1' cellpadding='4' cellspacing='0'>"
                for key, val in details.items():
                    val_str = str(val)
                    if key in ("market_link", "dashboard_link") and val_str.startswith("http"):
                        label = "Kalshi Market" if key == "market_link" else "Dashboard"
                        val_str = f'<a href="{val_str}">{label}</a>'
                    html_body += f"<tr><td><b>{key}</b></td><td>{val_str}</td></tr>"
                html_body += "</table>"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.address
        msg["To"] = self.recipient
        msg.attach(MIMEText(message, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        try:
            with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT,
                              timeout=self.timeout) as server:
                server.starttls()
                server.login(self.address, self.app_password)
                server.sendmail(self.address, [self.recipient], msg.as_string())
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Gmail send failed: %s", exc)
            return False
