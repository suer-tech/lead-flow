import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from worker.crm_transport import (CONNECT_SECONDS, MAX_BYTES, ORIGIN, SOCKET,
    SocketCrmTransport, decode_response, encode_request, validate_config)
from worker.engine import load_config
from worker.runtime import CrmClient


CONFIG = {"crm_url": ORIGIN, "crm_transport": "iva_vpn"}


class FakeSocket:
    def __init__(self, response):
        self.response = response
        self.sent = []
        self.timeouts = []
        self.eof = False
        self.connects = 0

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def settimeout(self, value): self.timeouts.append(value)
    def connect(self, path): self.path = path; self.connects += 1
    def sendall(self, data): self.sent.append(data)
    def shutdown(self, direction): self.eof = True
    def recv(self, maximum):
        if not self.eof:
            raise AssertionError("helper must receive request EOF")
        data, self.response = self.response[:maximum], self.response[maximum:]
        return data


class CrmTransportTests(unittest.TestCase):
    def test_default_remains_direct_and_requires_existing_env_token(self):
        self.assertEqual("direct", validate_config({}))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "crm_agent_token_missing"):
                CrmClient({"crm_url": ORIGIN})
        with patch.dict(os.environ, {"LEAD_CRM_AGENT_TOKEN": "existing-direct-secret"}):
            client = CrmClient({"crm_url": ORIGIN})
        self.assertIsNone(client.transport)
        self.assertEqual("existing-direct-secret", client.token)

    def test_optin_uses_no_caller_token_and_keeps_payload_contract(self):
        with patch.dict(os.environ, {}, clear=True):
            client = CrmClient(CONFIG)
        with patch.object(client.transport, "_exchange", return_value=b'{"ok":true,"http_status":200,"data":{"lead_id":17}}') as exchange:
            self.assertEqual({"lead_id": 17}, client.ingest({"idempotency_key": "same-key"}))
        request = json.loads(exchange.call_args.args[0])
        self.assertEqual({"path": "/api/agent/leads", "payload": {"idempotency_key": "same-key"}}, request)
        self.assertNotIn("token", vars(client))

    def test_fixed_origin_and_mode_reject_lookalikes(self):
        for origin in ("http://lead-flow-ten-virid.vercel.app", ORIGIN + ".evil", ORIGIN + "?x=1",
                       ORIGIN + "/api", "https://evil@lead-flow-ten-virid.vercel.app", ORIGIN + ":443"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                validate_config(dict(CONFIG, crm_url=origin))
        for mode in (True, None, "vpn", "DIRECT"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_config(dict(CONFIG, crm_transport=mode))

    def test_load_config_validates_before_any_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            value = {"campaign": "test", "database": "unused", "iva_root": "unused",
                     "queries": ["A long enough query"], **CONFIG}
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual("iva_vpn", load_config(path)["crm_transport"])
            value["crm_url"] = "https://other.example"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "vpn_crm_origin_mismatch"):
                load_config(path)

    def test_request_paths_bodies_and_size_are_bounded(self):
        for path in ("/api/agent/outreach/claim", "/api/agent/replies", "/api/agent/leads?x=1",
                     "/api/agent/leads/", ORIGIN + "/api/agent/leads", "//evil/x", "/api/agent/../leads"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                encode_request(path, {})
        for payload in ([], None, {"x": "a" * MAX_BYTES}, {"x": float("nan")}):
            with self.subTest(type=type(payload)), self.assertRaises(ValueError):
                encode_request("/api/agent/leads", payload)

    def test_http_statuses_survive_without_remote_body_or_error_text(self):
        for status in (301, 307, 401, 409, 422, 429, 500):
            with self.subTest(status=status), self.assertRaises(HTTPError) as raised:
                decode_response("/api/agent/drafts", json.dumps({"ok": False, "http_status": status, "error": "REMOTE_SECRET"}).encode())
            self.assertEqual(status, raised.exception.code)
            self.assertNotIn("REMOTE_SECRET", str(raised.exception))
            self.assertEqual(b"", raised.exception.read())

    def test_invalid_or_oversized_response_is_sanitized(self):
        for raw in (b"secret", b"[1]", b"x" * (MAX_BYTES + 1),
                    b'{"ok":true,"http_status":200,"data":[]}',
                    b'{"ok":false,"http_status":200,"error":"bad"}',
                    b'{"ok":false,"http_status":null,"error":"private error"}'):
            with self.subTest(raw=raw[:40]), self.assertRaises(URLError) as raised:
                decode_response("/api/agent/leads", raw)
            self.assertNotIn("private", str(raised.exception))

    def _run_socket(self, response, *, uid=None, mode=0o600):
        connection = FakeSocket(response)
        user = os.geteuid() if hasattr(os, "geteuid") else 123
        leaf = SimpleNamespace(st_mode=stat.S_IFSOCK | mode, st_uid=user if uid is None else uid)
        parent = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        with patch("worker.crm_transport.os.geteuid", return_value=user, create=True), \
             patch("worker.crm_transport.socket.AF_UNIX", 1, create=True), \
             patch.object(Path, "lstat", side_effect=[parent, leaf]), \
             patch("worker.crm_transport.socket.socket", return_value=connection):
            result = SocketCrmTransport(CONFIG).post("/api/agent/leads", {"x": 1})
        return result, connection

    def test_actual_socket_contract_sends_eof_before_wait_and_never_retries(self):
        result, connection = self._run_socket(b'{"ok":true,"http_status":200,"data":{"lead_id":3}}')
        self.assertEqual({"lead_id": 3}, result)
        self.assertTrue(connection.eof)
        self.assertEqual(1, connection.connects)
        self.assertEqual(str(SOCKET), connection.path)
        self.assertEqual(CONNECT_SECONDS, connection.timeouts[0])
        self.assertEqual(1, len(connection.sent))

    def test_socket_permissions_and_response_bound_fail_closed(self):
        for mode in (0o660, 0o666):
            with self.subTest(mode=mode), self.assertRaises(URLError):
                self._run_socket(b"{}", mode=mode)
        with self.assertRaises(URLError):
            self._run_socket(b"{}", uid=999999)
        with self.assertRaises(URLError):
            self._run_socket(b"x" * (MAX_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
