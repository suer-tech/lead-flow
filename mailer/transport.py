from __future__ import annotations

import re
import smtplib
import ssl
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from datetime import datetime, timezone

from .config import MailConfig


@dataclass(frozen=True)
class SendResult:
    outcome: str
    provider_message_id: str = ""
    receipt: str = ""
    error: str = ""

    def as_dict(self):
        return asdict(self)


def _address(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", value):
        raise ValueError("invalid_address")
    return value


def compose(draft: dict, config: MailConfig) -> bytes:
    sender = _address(draft.get("from", ""))
    recipient = _address(draft.get("to", ""))
    if sender.lower() != config.sender.lower():
        raise ValueError("sender_mismatch")
    message_id = draft.get("message_id", "")
    if not isinstance(message_id, str) or not re.fullmatch(r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+>", message_id):
        raise ValueError("invalid_message_id")
    subject, body = draft.get("subject", ""), draft.get("body", "")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 998 or "\r" in subject or "\n" in subject:
        raise ValueError("invalid_subject")
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > 100_000:
        raise ValueError("invalid_body")
    msg = EmailMessage(policy=SMTP)
    msg["From"], msg["To"], msg["Subject"] = sender, recipient, subject
    msg["Message-ID"] = message_id
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg.set_content(body)
    return msg.as_bytes()


def send_one(draft: dict, config: MailConfig, *, smtp_factory=smtplib.SMTP_SSL) -> SendResult:
    """One transaction only. No retry, Sent-folder mutation, or delivery claim."""
    if not config.enabled or not config.send_enabled:
        return SendResult("failed_before_send", error="sending_disabled")
    client, data_started = None, False
    try:
        payload = compose(draft, config)
        password = config.password()
        client = smtp_factory(config.smtp_host, config.smtp_port,
                              timeout=config.timeout, context=ssl.create_default_context())
        code, _ = client.ehlo()
        if code != 250:
            return SendResult("failed_before_send", error="smtp_ehlo_rejected")
        client.login(config.sender, password)
        code, _ = client.mail(config.sender)
        if code != 250:
            return SendResult("failed_before_send", error="smtp_sender_rejected")
        code, _ = client.rcpt(draft["to"])
        if code not in (250, 251):
            return SendResult("failed_before_send", error="smtp_recipient_rejected")
        code, _ = client.docmd("DATA")
        if code != 354:
            return SendResult("failed_before_send", error="smtp_data_not_permitted")
        # Mark uncertainty BEFORE writing: partial socket writes are unobservable.
        data_started = True
        wire = re.sub(br"(?m)^\.", b"..", payload)
        if not wire.endswith(b"\r\n"):
            wire += b"\r\n"
        client.send(wire + b".\r\n")
        code, _ = client.getreply()
        if code == 250:
            return SendResult("accepted", draft["message_id"],
                              f"{config.smtp_host}:{config.smtp_port} DATA 250 accepted")
        if 400 <= code <= 599:
            return SendResult("rejected", receipt=f"SMTP DATA {code} rejected", error="smtp_data_rejected")
        return SendResult("uncertain", error="smtp_unexpected_data_response")
    except smtplib.SMTPAuthenticationError:
        return SendResult("failed_before_send", error="smtp_authentication_failed")
    except (OSError, smtplib.SMTPException, ValueError, TypeError):
        return SendResult("uncertain" if data_started else "failed_before_send",
                          error="smtp_acknowledgement_unknown" if data_started else "smtp_setup_failed")
    finally:
        # A failed QUIT after 250 cannot change an accepted transaction.
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass
