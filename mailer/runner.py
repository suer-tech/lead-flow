"""One bounded, serialized inbox/outbox pass, independent of Iva model workers."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import time
import uuid

from .config import DispatcherConfig
from .crm import CrmError, MailCrmClient
from .inbox import MailState, poll_replies
from .store import DispatchStore
from .transport import SendResult, _address, compose, send_one


def doctor(config: DispatcherConfig):
    checks, issues = {}, []
    for name, check in (("configuration", config.validate), ("sender", lambda: _address(config.mail.sender)),
                        ("mail_credential_file", config.mail.password), ("crm_credential", config.token)):
        try:
            check()
            checks[name] = True
        except Exception:
            checks[name] = False
            issues.append(name + "_not_ready")
    return {"status": "configured" if all(checks.values()) else "not_ready",
            "enabled": config.mail.enabled, "send_enabled": config.mail.send_enabled,
            "checks": checks, "issues": issues, "live_login_verified": False,
            "delivery_verified": False, "network_calls": 0}


def status(config):
    """Read-only local status, including when the account is disabled."""
    path = Path(config.state_path).resolve()
    result = {"enabled": config.mail.enabled, "send_enabled": config.mail.send_enabled,
              "state_exists": path.is_file(), "attempt_states": {}, "last_run": None}
    if not path.is_file():
        return result
    if path.is_symlink():
        raise ValueError("state_path_must_not_be_symlink")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        result["attempt_states"] = {r["state"]: r["n"] for r in db.execute("SELECT state,COUNT(*) AS n FROM attempts GROUP BY state")}
        last = db.execute("SELECT id,state,summary_json,started_at,updated_at FROM runs ORDER BY started_at DESC,rowid DESC LIMIT 1").fetchone()
        if last:
            item = dict(last)
            item["summary"] = json.loads(item.pop("summary_json"))
            result["last_run"] = item
    return result


def _receipt(result):
    # SendResult.error is not a field accepted by the CRM TransportResult schema.
    return result.receipt or result.error or "No transport acknowledgement"


def _payload(message, result):
    return {"claim_token": message["claim_token"], "outcome": result.outcome,
            "provider_message_id": result.provider_message_id, "receipt": _receipt(result)}


def _ack(store, crm, item):
    if item["state"] == "reconciliation_required":
        return "reconciliation_required"
    if item["state"] == "started":
        # We cannot prove whether a crashed process reached SMTP. Never repeat it.
        payload = {"claim_token": item["claim_token"], "outcome": "uncertain", "provider_message_id": "",
                   "receipt": "Worker interrupted after durable attempt start; SMTP is never replayed"}
        store.result(item["outreach_id"], payload)
    else:
        payload = json.loads(item["result_json"])
    try:
        response = crm.result(item["outreach_id"], payload)
    except CrmError as exc:
        if exc.status == 409:
            store.mark(item["outreach_id"], "reconciliation_required")
            return "reconciliation_required"
        return "crm_ack_pending"
    except Exception:
        return "crm_ack_pending"
    if response.get("state") != payload["outcome"]:
        return "crm_ack_pending"
    store.mark(item["outreach_id"], "acknowledged")
    return None


def run(config, *, crm_factory=MailCrmClient, poller=poll_replies, sender=send_one, monotonic=time.monotonic):
    # Disabled means no secret reading, state mutation, CRM, IMAP, or SMTP calls.
    if not config.mail.enabled:
        return {"status": "disabled", "accepted": 0, "attempted": 0, "replies": 0}
    checks = doctor(config)
    if checks["status"] != "configured":
        return {"status": "blocked", "reason": "configuration_not_ready", "checks": checks["checks"],
                "accepted": 0, "attempted": 0, "replies": 0}
    store = DispatchStore(config.state_path)
    run_id = "mail-" + uuid.uuid4().hex
    summary = {"run_id": run_id, "status": "running", "accepted": 0, "attempted": 0, "replies": 0,
               "recovered_receipts": 0, "delivery_confirmed": False}
    deadline = monotonic() + config.max_seconds
    try:
        with store.lock():
            store.bind(config)
            crm = crm_factory(config)
            store.run(run_id, "running", summary)
            try:
                crm.heartbeat(run_id, "running", "Почтовый проход начат; доставка не подтверждена.")
            except Exception:
                summary["heartbeat_pending"] = True

            def finish(state, reason=""):
                summary["status"] = state
                if reason:
                    summary["reason"] = reason
                store.run(run_id, state, summary)
                try:
                    crm.heartbeat(run_id, "completed" if state == "completed" else "failed" if state in {"blocked", "failed"} else "paused",
                        json.dumps(summary, ensure_ascii=False))
                    summary.pop("heartbeat_pending", None)
                except Exception:
                    summary["heartbeat_pending"] = True
                store.run(run_id, state, summary)
                return dict(summary)

            # First reconcile receipts from earlier attempts. Only the idempotent
            # CRM acknowledgement is retried, never the SMTP transaction itself.
            for pending in store.pending():
                problem = _ack(store, crm, pending)
                if problem:
                    return finish("blocked", problem)
                summary["recovered_receipts"] += 1
                if monotonic() >= deadline:
                    return finish("paused", "time_budget")

            inbox = MailState(config.mail.state_path)
            if store.has_attempts() and inbox.cursor(config.mail) is None:
                return finish("blocked", "inbox_cursor_missing_after_send")

            def receive(payload):
                response = crm.reply(payload)
                if response.get("match_state") not in {"matched", "needs_review"}:
                    raise CrmError("crm_reply_ack_missing")

            inbox_result = poller(config.mail, receive, deadline=deadline)
            summary["inbox_status"] = inbox_result.get("status", "error")
            summary["replies"] = inbox_result.get("processed", 0)
            if inbox_result.get("status") not in {"ok", "baselined"}:
                return finish("blocked", "inbox_not_checked")
            if inbox.cursor(config.mail) is None:
                return finish("blocked", "inbox_baseline_not_saved")
            if inbox_result.get("remaining", 0):
                summary["inbox_remaining"] = inbox_result["remaining"]
                return finish("paused", "inbox_backlog")

            if not config.mail.send_enabled:
                return finish("completed", "sending_disabled")
            for _ in range(config.max_sends_per_run):
                if monotonic() >= deadline:
                    return finish("paused", "time_budget")
                try:
                    claimed = crm.claim()
                except Exception:
                    return finish("blocked", "crm_claim_failed")
                if "message" not in claimed:
                    return finish("blocked", "invalid_claim_response")
                message = claimed["message"]
                if message is None:
                    return finish("completed", "daily_limit" if claimed.get("reason") == "daily_limit" else "queue_empty")
                # An invalid response cannot authorize a recipient or SMTP send.
                if (not isinstance(message, dict) or not isinstance(message.get("id"), str)
                        or not 1 <= len(message["id"]) <= 100
                        or not isinstance(message.get("claim_token"), str)
                        or not 20 <= len(message["claim_token"]) <= 100
                        or not isinstance(message.get("message_id"), str)):
                    return finish("blocked", "invalid_claim_message")
                try:
                    store.register(message)
                except sqlite3.IntegrityError:
                    return finish("blocked", "duplicate_claim_no_resend")
                summary["attempted"] += 1
                try:
                    compose(message, config.mail)
                except (TypeError, ValueError):
                    result = SendResult("failed_before_send", error="invalid_claim_payload")
                else:
                    try:
                        result = sender(message, config.mail)
                    except Exception:
                        result = SendResult("uncertain", error="transport_interrupted_no_resend")
                if not isinstance(result, SendResult) or result.outcome not in {"accepted", "failed_before_send", "rejected", "uncertain"}:
                    result = SendResult("uncertain", error="invalid_transport_result")
                if result.outcome == "accepted":
                    summary["accepted"] += 1
                payload = _payload(message, result)
                store.result(message["id"], payload)
                problem = _ack(store, crm, {"outreach_id": message["id"], "state": "pending_ack", "result_json": json.dumps(payload)})
                if problem:
                    return finish("blocked", problem)
                if result.outcome != "accepted":
                    return finish("blocked", "transport_" + result.outcome)
                store.run(run_id, "running", summary)
            return finish("completed", "run_send_limit")
    except (BlockingIOError, PermissionError):
        summary.update(status="paused", reason="mailer_locked_or_state_denied")
        return summary
    except Exception:
        # Only local bounded metadata is recorded; no raw exception/provider text.
        summary.update(status="failed", reason="mailer_state_or_runtime_failed")
        try:
            store.run(run_id, "failed", summary)
        except Exception:
            pass
        return summary
