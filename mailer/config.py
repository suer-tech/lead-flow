from __future__ import annotations

import os
import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def private_file(path: str) -> Path:
    """Validate a configured credential path without exposing its contents."""
    if not path:
        raise ValueError("credential_file_missing")
    item = Path(path)
    if not item.is_file() or item.is_symlink():
        raise ValueError("credential_file_invalid")
    info = item.stat()
    if os.name != "nt":
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("credential_file_permissions")
        if info.st_uid != os.geteuid():
            raise ValueError("credential_file_owner")
    return item


def secret_file(path: str, *, minimum=1) -> str:
    item = private_file(path)
    if item.stat().st_size > 4096:
        raise ValueError("credential_file_invalid")
    value = item.read_text(encoding="utf-8").strip()
    if len(value) < minimum or "\n" in value or "\r" in value or len(value) > 1024:
        raise ValueError("credential_file_invalid")
    return value


@dataclass(frozen=True)
class MailConfig:
    sender: str = "ai.studio.ufa@mail.ru"
    password_file: str = ""
    state_path: str = "data/mail-state.sqlite3"
    enabled: bool = False
    send_enabled: bool = False
    smtp_host: str = "smtp.mail.ru"
    smtp_port: int = 465
    imap_host: str = "imap.mail.ru"
    imap_port: int = 993
    mailbox: str = "INBOX"
    timeout: int = 30
    batch_size: int = 50
    max_message_bytes: int = 1_000_000

    @classmethod
    def from_env(cls):
        return cls(sender=os.getenv("LEAD_MAIL_SENDER", "ai.studio.ufa@mail.ru"),
                   password_file=os.getenv("LEAD_MAIL_PASSWORD_FILE", ""),
                   state_path=os.getenv("LEAD_MAIL_STATE", "data/mail-state.sqlite3"),
                   enabled=os.getenv("LEAD_MAIL_ENABLED", "0") == "1",
                   send_enabled=os.getenv("LEAD_MAIL_SEND_ENABLED", "0") == "1")

    def password(self) -> str:
        """Read only from a private file; never include contents in diagnostics."""
        if not self.password_file:
            raise ValueError("password_file_missing")
        return secret_file(self.password_file)


@dataclass(frozen=True)
class DispatcherConfig:
    mail: MailConfig = field(default_factory=MailConfig)
    crm_url: str = ""
    crm_token_env: str = "LEAD_CRM_AGENT_TOKEN"
    crm_token_file: str = ""
    crm_transport: str = "direct"
    state_path: str = "data/mail-dispatch.sqlite3"
    max_sends_per_run: int = 3
    max_seconds: int = 120
    http_timeout: int = 15

    def validate(self):
        from worker.crm_transport import validate_config
        validate_config({"crm_transport": self.crm_transport, "crm_url": self.crm_url})
        url = urlsplit(self.crm_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/")):
            raise ValueError("crm_https_origin_required")
        if type(self.mail.enabled) is not bool or type(self.mail.send_enabled) is not bool:
            raise ValueError("enabled_must_be_boolean")
        for value, lower, upper in ((self.max_sends_per_run, 1, 20), (self.max_seconds, 30, 600),
                                    (self.http_timeout, 1, 60), (self.mail.timeout, 1, 60),
                                    (self.mail.batch_size, 1, 200)):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("invalid_worker_limit")
        if Path(self.state_path).resolve() == Path(self.mail.state_path).resolve():
            raise ValueError("separate_mail_state_paths_required")
        if not self.crm_token_env or not self.crm_token_env.replace("_", "").isalnum():
            raise ValueError("invalid_token_environment_name")
        return self

    def token(self):
        if self.crm_token_file:
            return secret_file(self.crm_token_file, minimum=32)
        value = os.getenv(self.crm_token_env, "")
        if len(value) < 32 or len(value) > 1024 or any(c.isspace() for c in value):
            raise ValueError("crm_agent_token_missing_or_invalid")
        return value


def load_config(path: str | None = None) -> DispatcherConfig:
    """JSON contains settings/secret paths, never passwords or bearer tokens."""
    values = {}
    if path:
        source = Path(path)
        if source.stat().st_size > 16000:
            raise ValueError("mailer_config_too_large")
        values = json.loads(source.read_text(encoding="utf-8-sig"))
    allowed = {"enabled", "send_enabled", "sender", "password_file", "inbox_state_path",
               "state_path", "crm_url", "crm_token_env", "crm_token_file", "max_sends_per_run",
               "max_seconds", "http_timeout", "mail_timeout", "batch_size", "crm_transport"}
    if not isinstance(values, dict) or set(values) - allowed:
        raise ValueError("mailer_config_unknown_fields")
    for name, key in {"LEAD_MAIL_SENDER": "sender", "LEAD_MAIL_PASSWORD_FILE": "password_file",
                      "LEAD_MAIL_STATE": "inbox_state_path", "LEAD_MAIL_DISPATCH_STATE": "state_path",
                      "LEAD_CRM_URL": "crm_url", "LEAD_CRM_AGENT_TOKEN_FILE": "crm_token_file"}.items():
        if name in os.environ:
            values[key] = os.environ[name]
    for name, key in {"LEAD_MAIL_ENABLED": "enabled", "LEAD_MAIL_SEND_ENABLED": "send_enabled"}.items():
        if name in os.environ:
            if os.environ[name] not in {"0", "1"}:
                raise ValueError("enabled_must_be_zero_or_one")
            values[key] = os.environ[name] == "1"
    mail = MailConfig(sender=values.get("sender", "ai.studio.ufa@mail.ru"),
        password_file=values.get("password_file", ""),
        state_path=values.get("inbox_state_path", "data/mail-state.sqlite3"),
        enabled=values.get("enabled", False), send_enabled=values.get("send_enabled", False),
        timeout=values.get("mail_timeout", 30), batch_size=values.get("batch_size", 50))
    return DispatcherConfig(mail=mail, **{key: values[key] for key in (
        "crm_url", "crm_token_env", "crm_token_file", "crm_transport", "state_path", "max_sends_per_run",
        "max_seconds", "http_timeout") if key in values})
