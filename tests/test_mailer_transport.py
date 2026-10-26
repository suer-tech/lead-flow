import smtplib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from mailer.config import MailConfig
from mailer.transport import compose, send_one


class FakeSMTP:
    def __init__(self, *, failure="", response=250):
        self.failure, self.response, self.payload = failure, response, b""

    def ehlo(self):
        return 250, b"ok"

    def login(self, *args):
        if self.failure == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"private response")

    def mail(self, *args):
        return 250, b"ok"

    def rcpt(self, *args):
        return 550 if self.failure == "rcpt" else 250, b"ok"

    def docmd(self, *args):
        if self.failure == "before_data":
            raise smtplib.SMTPServerDisconnected("private response")
        return 354, b"ok"

    def send(self, payload):
        self.payload = payload
        if self.failure == "write":
            raise OSError("private response")

    def getreply(self):
        if self.failure == "ack":
            raise smtplib.SMTPServerDisconnected("private response")
        return self.response, b"private response"

    def quit(self):
        if self.failure == "quit":
            raise OSError("private response")

    def close(self):
        pass


class SendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        secret = Path(self.temp.name) / "secret"
        secret.write_text("not-a-real-password", encoding="utf-8")
        secret.chmod(0o600)
        self.config = MailConfig(enabled=True, send_enabled=True, password_file=str(secret))
        self.draft = {"from": self.config.sender, "to": "buyer@example.org", "subject": "Проверка",
                      "body": "Здравствуйте.\n.example", "message_id": "<draft-42@lead-flow.invalid>"}

    def send(self, failure="", response=250):
        client = FakeSMTP(failure=failure, response=response)
        result = send_one(self.draft, self.config, smtp_factory=lambda *a, **k: client)
        self.assertNotIn("private response", str(result))
        self.assertNotIn("not-a-real-password", str(result))
        return result, client

    def test_default_disabled_without_network(self):
        result = send_one(self.draft, MailConfig(), smtp_factory=lambda *a, **k: self.fail("network"))
        self.assertEqual(result.error, "sending_disabled")

    def test_smtp_acceptance_is_preserved_when_quit_fails(self):
        result, client = self.send("quit")
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(result.provider_message_id, self.draft["message_id"])
        self.assertIn(b"Message-ID: <draft-42@lead-flow.invalid>", client.payload)
        self.assertIn(b"\r\n..example\r\n", client.payload)
        self.assertTrue(client.payload.endswith(b"\r\n.\r\n"))

    def test_before_data_failures_are_known_not_sent(self):
        for failure in ("auth", "before_data", "rcpt"):
            with self.subTest(failure=failure):
                result, client = self.send(failure)
                self.assertEqual(result.outcome, "failed_before_send")
                self.assertEqual(client.payload, b"")

    def test_partial_data_and_missing_ack_are_uncertain(self):
        for failure in ("write", "ack"):
            with self.subTest(failure=failure):
                result, _ = self.send(failure)
                self.assertEqual(result.outcome, "uncertain")

    def test_explicit_rejection_not_misreported_as_delivered(self):
        result, _ = self.send(response=550)
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(result.provider_message_id, "")

    def test_header_injection_and_sender_mismatch_rejected(self):
        for change in ({"subject": "Hello\r\nBcc: bad@example.org"},
                       {"from": "other@example.org"}, {"to": "a@example.org,b@example.org"},
                       {"message_id": "<id@example.org>\nFake: header"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                compose(self.draft | change, self.config)

    def test_missing_secret_never_connects(self):
        config = replace(self.config, password_file="")
        result = send_one(self.draft, config, smtp_factory=lambda *a, **k: self.fail("network"))
        self.assertEqual(result.outcome, "failed_before_send")


if __name__ == "__main__":
    unittest.main()
