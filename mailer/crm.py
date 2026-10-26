"""Bounded HTTPS API; bearer credentials never follow redirects."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import quote


class CrmError(RuntimeError):
    def __init__(self, code, status=None):
        super().__init__(code)
        self.code, self.status = code, status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MailCrmClient:
    def __init__(self, config):
        config.validate()
        self.base, self.token = config.crm_url.rstrip("/"), config.token()
        self.timeout = config.http_timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def post(self, path, payload):
        request = urllib.request.Request(self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(128001)
                if len(body) > 128000:
                    raise CrmError("crm_response_too_large")
            result = json.loads(body)
            if not isinstance(result, dict):
                raise CrmError("crm_invalid_response")
            return result
        except urllib.error.HTTPError as exc:
            # Do not log response bodies, request headers, or credential-bearing URLs.
            raise CrmError("crm_http_error", exc.code) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise CrmError("crm_unavailable") from None

    def claim(self):
        return self.post("/api/agent/outreach/claim", {})

    def result(self, outreach_id, payload):
        return self.post("/api/agent/outreach/" + quote(outreach_id, safe="") + "/result", payload)

    def reply(self, payload):
        return self.post("/api/agent/replies", payload)

    def heartbeat(self, run_id, state, summary):
        return self.post("/api/agent/heartbeat", {"run_id": run_id, "state": state, "summary": summary})

    def maintenance(self):
        return self.post("/api/agent/maintenance", {})
