import tempfile
import unittest
from pathlib import Path
from acquisition import Reply

from mailer.config import MailConfig
from mailer.inbox import MailState, parse_reply, poll_replies


def raw_message(mid="one", extra="", body="Да, интересно"):
    return (f"From: buyer@example.org\r\nTo: ai.studio.ufa@mail.ru\r\n"
            f"Message-ID: <{mid}@example.org>\r\nIn-Reply-To: <draft-42@lead-flow.invalid>\r\n"
            f"References: <draft-42@lead-flow.invalid>\r\nSubject: Re: hello\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n{extra}\r\n{body}").encode()


class FakeIMAP:
    def __init__(self, messages=None, validity="1"):
        self.messages, self.validity = messages or {}, validity
        self.readonly, self.fetches = None, []

    def login(self, *args):
        return "OK", []

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, key):
        return key, [self.validity.encode()]

    def uid(self, operation, *args):
        if operation == "search":
            return "OK", [b" ".join(str(n).encode() for n in self.messages)]
        if operation == "fetch":
            self.fetches.append(args)
            uid = int(args[0]); raw = self.messages[uid]
            return "OK", [(f"1 (UID {uid} RFC822.SIZE {len(raw)})".encode(), raw)]
        raise AssertionError(operation)

    def logout(self):
        pass


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        secret = Path(self.temp.name) / "secret"
        secret.write_text("fake", encoding="utf-8"); secret.chmod(0o600)
        self.config = MailConfig(enabled=True, password_file=str(secret),
                                 state_path=str(Path(self.temp.name) / "state.sqlite3"))
        self.received = []

    def poll(self, client, callback=None):
        result = poll_replies(self.config, callback or self.received.append,
                              imap_factory=lambda *a, **k: client)
        self.assertTrue(client.readonly)
        self.assertTrue(all("BODY.PEEK[]" in args[1] for args in client.fetches))
        return result

    def test_first_run_baselines_history_then_only_new_uids(self):
        old = FakeIMAP({10: raw_message("old")})
        result = self.poll(old)
        self.assertEqual(result["status"], "baselined"); self.assertFalse(old.fetches)
        live = FakeIMAP({10: raw_message("old"), 11: raw_message("new")})
        self.assertEqual(self.poll(live)["processed"], 1)
        self.assertEqual(self.received[0]["message_id"], "<new@example.org>")
        self.assertEqual(self.poll(live)["processed"], 0)

    def test_empty_baseline_catches_first_message(self):
        self.poll(FakeIMAP())
        self.assertEqual(self.poll(FakeIMAP({1: raw_message()}))["processed"], 1)

    def test_uidvalidity_change_rescans_with_durable_dedup(self):
        self.poll(FakeIMAP())
        self.poll(FakeIMAP({5: raw_message("first")}))
        result = self.poll(FakeIMAP({1: raw_message("first"), 2: raw_message("second")}, "2"))
        self.assertTrue(result["uidvalidity_changed"])
        self.assertEqual(result["processed"], 1)
        self.assertEqual(len(self.received), 2)

    def test_ingestion_failure_leaves_uid_for_retry(self):
        self.poll(FakeIMAP())
        def fail(_):
            raise RuntimeError("private CRM response")
        result = self.poll(FakeIMAP({1: raw_message()}), fail)
        self.assertEqual(result["status"], "error")
        self.assertEqual(MailState(self.config.state_path).cursor(self.config)[1], 0)
        self.assertEqual(self.poll(FakeIMAP({1: raw_message()}))["processed"], 1)

    def test_reply_never_keyword_classifies_interest_or_refusal(self):
        for body in ("Да, интересно", "Не пишите больше", "Я согласен, но не сейчас"):
            self.assertEqual(parse_reply(raw_message(body=body), digest="abc")["classification"], "needs_review")

    def test_auto_reply_and_dsn_are_not_interest(self):
        auto = parse_reply(raw_message(extra="Auto-Submitted: auto-replied\r\n"), digest="abc")
        self.assertEqual(auto["classification"], "auto_reply")
        bounce = b"From: mailer-daemon@example.org\r\nContent-Type: multipart/report; report-type=delivery-status; boundary=x\r\n\r\n--x--\r\n"
        self.assertEqual(parse_reply(bounce, digest="abc")["classification"], "bounce")

    def test_headers_keep_thread_references(self):
        parsed = parse_reply(raw_message(), digest="abc")
        self.assertEqual(parsed["from_email"], "buyer@example.org")
        self.assertEqual(parsed["in_reply_to"], "<draft-42@lead-flow.invalid>")
        self.assertEqual(parsed["references"], ["<draft-42@lead-flow.invalid>"])

    def test_large_message_not_silently_skipped(self):
        self.poll(FakeIMAP())
        result = self.poll(FakeIMAP({1: b"x" * (self.config.max_message_bytes + 1)}))
        self.assertEqual(result["error"], "imap_message_too_large")
        self.assertEqual(MailState(self.config.state_path).cursor(self.config)[1], 0)

    def test_lost_crm_ack_with_missing_date_retries_identical_payload(self):
        self.poll(FakeIMAP())
        attempts = []
        def fail(payload):
            attempts.append(payload)
            raise RuntimeError("ack lost after CRM commit")
        self.poll(FakeIMAP({1: raw_message()}), fail)
        self.poll(FakeIMAP({1: raw_message()}), attempts.append)
        self.assertEqual(attempts[0], attempts[1])

    def test_reply_limits_match_real_crm_schema_and_future_date_is_safe(self):
        raw = raw_message(extra="Date: Mon, 01 Jan 2090 00:00:00 +0000\r\n", body="x" * 25000)
        parsed = parse_reply(raw, digest="abc", observed_at="2026-10-26T09:00:00+00:00")
        self.assertEqual(parsed["received_at"], "2026-10-26T09:00:00+00:00")
        self.assertEqual(len(parsed["body"]), 20000)
        Reply.model_validate(parsed)

    def test_missing_sender_is_explicitly_unidentified_and_reviewable(self):
        parsed = parse_reply(b"Subject: Test\r\n\r\nUnidentified message", digest="abc")
        self.assertEqual(parsed["from_email"], "unidentified@lead-flow.invalid")
        self.assertEqual(parsed["classification"], "needs_review")
        Reply.model_validate(parsed)


if __name__ == "__main__":
    unittest.main()
