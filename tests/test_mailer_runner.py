import json
import os
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acquisition import Reply, TransportResult
from mailer.config import DispatcherConfig, MailConfig, load_config
from mailer.crm import CrmError, MailCrmClient, NoRedirect
from mailer.inbox import MailState
from mailer.runner import doctor, run, status
from mailer.store import DispatchStore
from mailer.transport import SendResult


def message(index=1):
    return {"id": f"outreach-{index}", "lead_id": index, "to": f"buyer{index}@example.org",
            "from": "ai.studio.ufa@mail.ru", "subject": "Проверка процесса",
            "body": "Здравствуйте. Обсудим ваш процесс обработки документов?",
            "message_id": f"<outreach-{index}@lead-flow.invalid>", "claim_token": "t" * 40 + str(index)}


class FakeCrm:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.claims, self.results, self.reply_payloads, self.heartbeats = 0, [], [], []
        self.fail_ack = 0
        self.ack_conflict = False

    def claim(self):
        self.claims += 1
        return {"message": self.messages.pop(0) if self.messages else None, "reason": "empty"}

    def result(self, outreach_id, payload):
        # Validate against the REAL API schema, including its prohibition on error.
        TransportResult.model_validate(payload)
        self.results.append((outreach_id, payload))
        if self.ack_conflict:
            raise CrmError("crm_http_error", 409)
        if self.fail_ack:
            self.fail_ack -= 1
            raise CrmError("crm_unavailable")
        return {"state": payload["outcome"]}

    def reply(self, payload):
        Reply.model_validate(payload)
        self.reply_payloads.append(payload)
        return {"match_state": "matched", "replayed": False}

    def heartbeat(self, run_id, state, summary):
        self.heartbeats.append((run_id, state, summary))


class MailerRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        secret = root / "password"
        secret.write_text("fake-password", encoding="utf-8")
        secret.chmod(0o600)
        self.patch = patch.dict(os.environ, {"LEAD_CRM_AGENT_TOKEN": "private-test-token-" + "x" * 32})
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.config = DispatcherConfig(mail=MailConfig(enabled=True, send_enabled=True,
            password_file=str(secret), state_path=str(root / "inbox.sqlite3")),
            crm_url="https://crm.example.org", state_path=str(root / "dispatch.sqlite3"))
        self.crm = FakeCrm()
        self.sent = []

    def poll(self, config, callback, **kwargs):
        state = MailState(config.state_path)
        old = state.cursor(config)
        state.set_cursor(config, "1", old[1] if old else 0)
        return {"status": "ok" if old else "baselined", "processed": 0}

    def send(self, payload, config):
        self.assertIsNotNone(MailState(config.state_path).cursor(config))
        self.sent.append(payload["message_id"])
        return SendResult("accepted", payload["message_id"], "SMTP DATA 250 accepted")

    def execute(self, **changes):
        return run(changes.pop("config", self.config), crm_factory=lambda _: self.crm,
                   poller=changes.pop("poller", self.poll), sender=changes.pop("sender", self.send), **changes)

    def test_disabled_never_opens_state_or_network(self):
        result = self.execute(config=replace(self.config, mail=MailConfig()))
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(Path(self.config.state_path).exists())
        self.assertFalse(self.crm.heartbeats)

    def test_pacing_and_off_hours_are_not_reported_as_an_empty_queue(self):
        for reason in ('paced','outside_window','daily_limit'):
            with self.subTest(reason=reason), patch.object(self.crm,'claim',return_value={'message':None,'reason':reason}):
                result=self.execute()
                self.assertEqual(reason,result['reason'])
                self.assertEqual(0,result['attempted'])
        self.assertEqual([],self.sent)

    def test_missing_credentials_block_before_any_network(self):
        result = self.execute(config=replace(self.config, mail=replace(self.config.mail, password_file="")))
        self.assertEqual(result["reason"], "configuration_not_ready")
        self.assertFalse(self.crm.heartbeats)
        self.assertEqual(self.sent, [])

    def test_baseline_before_first_send_and_exact_api_contract(self):
        self.crm.messages = [message()]
        result = self.execute()
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["delivery_confirmed"])
        self.assertEqual(self.crm.results[0][1]["outcome"], "accepted")
        self.assertEqual(status(self.config)["attempt_states"], {"acknowledged": 1})

    def test_lost_crm_ack_replays_receipt_never_smtp(self):
        self.crm.messages = [message()]; self.crm.fail_ack = 1
        first = self.execute()
        self.assertEqual(first["reason"], "crm_ack_pending")
        saved = self.crm.results[0][1]
        second = self.execute()
        self.assertEqual(second["recovered_receipts"], 1)
        self.assertEqual(self.crm.results[1][1], saved)
        self.assertEqual(len(self.sent), 1)

    def test_crashed_attempt_recovers_uncertain_without_sending(self):
        store = DispatchStore(self.config.state_path); store.bind(self.config)
        store.register(message())
        MailState(self.config.mail.state_path).set_cursor(self.config.mail, "1", 0)
        result = self.execute()
        self.assertEqual(result["recovered_receipts"], 1)
        self.assertEqual(self.crm.results[0][1]["outcome"], "uncertain")
        self.assertEqual(self.sent, [])

    def test_expired_crm_claim_requires_reconciliation_persistently(self):
        self.crm.messages = [message()]; self.crm.ack_conflict = True
        self.assertEqual(self.execute()["reason"], "reconciliation_required")
        claims = self.crm.claims
        self.crm.ack_conflict = False
        self.assertEqual(self.execute()["reason"], "reconciliation_required")
        self.assertEqual(self.crm.claims, claims)
        self.assertEqual(len(self.sent), 1)

    def test_uncertain_transport_never_retried(self):
        self.crm.messages = [message()]
        def unknown(payload, config):
            self.sent.append(payload["message_id"])
            return SendResult("uncertain", error="smtp_acknowledgement_unknown")
        first = self.execute(sender=unknown)
        self.assertEqual(first["reason"], "transport_uncertain")
        self.assertEqual(self.crm.results[0][1]["receipt"], "smtp_acknowledgement_unknown")
        self.execute()
        self.assertEqual(len(self.sent), 1)

    def test_inbox_error_or_backlog_prevents_claiming(self):
        for result in ({"status": "error", "processed": 0}, {"status": "ok", "remaining": 7, "processed": 2}):
            def poll(config, callback, **kwargs):
                self.poll(config, callback)
                return result
            self.crm.messages = [message()]
            response = self.execute(poller=poll)
            self.assertIn(response["status"], {"blocked", "paused"})
        self.assertEqual(self.crm.claims, 0)

    def test_lost_inbox_state_is_not_silently_baselined_after_send(self):
        self.crm.messages = [message()]; self.execute()
        Path(self.config.mail.state_path).unlink()
        self.crm.messages = [message(2)]
        result = self.execute()
        self.assertEqual(result["reason"], "inbox_cursor_missing_after_send")
        self.assertEqual(len(self.sent), 1)

    def test_read_only_mode_keeps_reply_polling_without_claim(self):
        config = replace(self.config, mail=replace(self.config.mail, send_enabled=False))
        result = self.execute(config=config)
        self.assertEqual(result["reason"], "sending_disabled")
        self.assertEqual(self.crm.claims, 0)
        self.assertIsNotNone(MailState(config.mail.state_path).cursor(config.mail))

    def test_per_run_limit_is_bounded(self):
        self.crm.messages = [message(i) for i in range(1, 6)]
        result = self.execute()
        self.assertEqual(result["reason"], "run_send_limit")
        self.assertEqual(result["accepted"], 3)
        self.assertEqual(len(self.crm.messages), 2)

    def test_sender_mismatch_is_acknowledged_without_smtp(self):
        self.crm.messages = [message() | {"from": "someone@example.org"}]
        result = self.execute()
        self.assertEqual(result["reason"], "transport_failed_before_send")
        self.assertEqual(self.sent, [])

    def test_duplicate_claim_never_repeats_even_after_ack(self):
        self.crm.messages = [message(), message()]
        result = self.execute()
        self.assertEqual(result["reason"], "duplicate_claim_no_resend")
        self.assertEqual(len(self.sent), 1)

    def test_changed_account_or_crm_cannot_reuse_attempt_journal(self):
        self.execute()
        before = len(self.crm.heartbeats)
        result = self.execute(config=replace(self.config, crm_url="https://other.example.org"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.crm.heartbeats), before)

    def test_status_and_doctor_are_read_only_and_do_not_print_credentials(self):
        self.assertFalse(status(self.config)["state_exists"])
        check = doctor(self.config)
        self.assertEqual(check["network_calls"], 0)
        self.assertFalse(check["live_login_verified"])
        self.assertNotIn("fake-password", json.dumps(check))
        self.assertNotIn("private-test-token", json.dumps(check))
        self.assertFalse(Path(self.config.state_path).exists())

    def test_http_and_credential_url_or_redirect_are_forbidden(self):
        for url in ("http://127.0.0.1", "https://u:p@crm.example.org", "https://crm.example.org/path", "https://crm.example.org/?token=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                MailCrmClient(replace(self.config, crm_url=url))
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.example.org"))

    def test_example_disabled_and_inline_credentials_rejected(self):
        example = Path(__file__).resolve().parents[1] / "mailer/config.example.json"
        config = load_config(str(example))
        self.assertFalse(config.mail.enabled)
        self.assertFalse(config.mail.send_enabled)
        bad = Path(self.temp.name) / "bad.json"
        bad.write_text('{"password":"secret"}')
        with self.assertRaises(ValueError):
            load_config(str(bad))

    def test_private_crm_transport_never_reads_bearer_in_mail_worker(self):
        config=replace(self.config,crm_url='https://lead-flow-ten-virid.vercel.app',crm_transport='iva_vpn')
        with patch.object(DispatcherConfig,'token',side_effect=AssertionError('secret must stay in helper')), \
                patch('worker.crm_transport.SocketCrmTransport') as bridge:
            bridge.return_value.post.return_value={'message':None,'reason':'empty'}
            client=MailCrmClient(config)
            self.assertEqual('empty',client.claim()['reason'])
            bridge.return_value.post.assert_called_once_with('/api/agent/outreach/claim',{})


if __name__ == "__main__":
    unittest.main()
