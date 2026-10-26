"""Opt-in, credential-free client for the fixed private CRM VPN helper.

No automatic retry: a lost response may follow a committed CRM request. Existing
worker idempotency keys/checkpoints, not this transport, govern later retries.
"""
import io
import json
import os
from pathlib import Path
import socket
import stat
import time
from urllib.error import HTTPError, URLError

ORIGIN = "https://lead-flow-ten-virid.vercel.app"
SOCKET = Path("/run/lead-flow-crm.sock")
PATHS = frozenset(("/api/agent/leads", "/api/agent/drafts"))
MAX_BYTES = 128 * 1024
CONNECT_SECONDS = 3
TOTAL_SECONDS = 35


def validate_config(config):
    mode = config.get("crm_transport", "direct")
    if mode not in ("direct", "iva_vpn"):
        raise ValueError("invalid_crm_transport")
    if mode == "iva_vpn" and config.get("crm_url") not in (ORIGIN, ORIGIN + "/"):
        raise ValueError("vpn_crm_origin_mismatch")
    return mode


def encode_request(path, payload):
    if path not in PATHS or not isinstance(payload, dict):
        raise ValueError("unsupported_crm_request")
    data = json.dumps({"path": path, "payload": payload}, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("crm_request_too_large")
    return data


def decode_response(path, raw):
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError()
        status = result.get("http_status")
        if result["ok"]:
            if (set(result) != {"ok", "http_status", "data"} or type(status) is not int
                    or not 200 <= status < 300 or not isinstance(result["data"], dict)):
                raise ValueError()
            return result["data"]
        if (set(result) != {"ok", "http_status", "error"}
                or not isinstance(result["error"], str)
                or status is not None and (type(status) is not int or not 300 <= status <= 599)):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError):
        raise URLError("crm_transport_invalid_response") from None
    if status is not None:
        # Keep conflict/validation statuses used by pending-publish state handling.
        # Do not return untrusted error bodies, headers, or remote exception text.
        raise HTTPError(ORIGIN + path, status, "CRM HTTP failure", {}, io.BytesIO())
    raise URLError("crm_transport_unavailable")


class SocketCrmTransport:
    def __init__(self, config):
        if validate_config(config) != "iva_vpn":
            raise ValueError("vpn_crm_transport_not_selected")

    def _exchange(self, data):
        try:
            parent = SOCKET.parent.lstat()
            info = SOCKET.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0
                    or parent.st_mode & 0o022 or not stat.S_ISSOCK(info.st_mode)
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise OSError("unsafe_socket")
            deadline = time.monotonic() + TOTAL_SECONDS
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(CONNECT_SECONDS)
                connection.connect(str(SOCKET))
                connection.settimeout(max(.01, deadline - time.monotonic()))
                connection.sendall(data)
                connection.shutdown(socket.SHUT_WR)
                chunks = []
                remaining = MAX_BYTES + 1
                while remaining:
                    seconds = deadline - time.monotonic()
                    if seconds <= 0:
                        raise TimeoutError()
                    connection.settimeout(seconds)
                    chunk = connection.recv(min(16384, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                result = b"".join(chunks)
                if len(result) > MAX_BYTES:
                    raise OSError("response_too_large")
                return result
        except (OSError, ValueError):
            raise URLError("crm_transport_unavailable") from None

    def post(self, path, payload):
        return decode_response(path, self._exchange(encode_request(path, payload)))
