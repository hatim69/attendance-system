"""Outgoing e-mail through the SMTP server set in .env (the same one the nightly backup uses)."""
import smtplib
from email.message import EmailMessage

from . import config


def configured() -> bool:
    return bool(config.SMTP_HOST)


def send(to: list[str], subject: str, body: str, attachment: tuple[str, bytes] | None = None):
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM or config.SMTP_USER
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment:
        msg.add_attachment(attachment[1], maintype="application", subtype="zip", filename=attachment[0])
    if config.SMTP_PORT == 465:
        s = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=60)
    else:
        s = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=60)
        s.starttls()
    with s:
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)
