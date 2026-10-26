"""python -m worker --config worker/config.example.json doctor|run|status|preview|publish"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

from .engine import Engine, load_config
from .store import Store

CRM_TOKEN = Path('/etc/lead-flow/crm-agent.token')
CRM_SOCKET = Path('/run/lead-flow-crm.sock')


def fixed_metadata(path, socket=False):
    try:
        mode = path.lstat().st_mode
        return stat.S_ISSOCK(mode) if socket else stat.S_ISREG(mode)
    except FileNotFoundError:
        return False
    except OSError:
        return None


def doctor(config):
    from .crm_transport import validate_config
    root = Path(config["iva_root"])
    transport = validate_config(config)
    private = transport == 'iva_vpn'
    return {"mode": config["mode"], "schedule_enabled_in_config": config["enabled"],
        "iva_runtime_present": (root / "iva_integration/demand_sources.py").is_file(),
        "coordinator_socket_present": Path("/run/iva-browser/control.sock").exists(),
        "codex_login_present": (Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json").is_file(),
        "crm_url_configured": bool(config.get("crm_url")),
        "crm_transport": transport,
        "crm_credential_source": "private_transport_helper_file_metadata" if private else "worker_environment_not_inspected",
        "crm_token_present": fixed_metadata(CRM_TOKEN) if private else None,
        "crm_socket_present": fixed_metadata(CRM_SOCKET, socket=True) if private else None,
        "crm_credential_contents_checked": False, "crm_authentication_verified": False,
        "crm_explanation": "Presence metadata does not verify token contents, helper enablement, authentication or current API connectivity.",
        "email_sending": False, "network_checks_performed": False,
        "config_file_does_not_enable_systemd": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor"); sub.add_parser("status")
    run = sub.add_parser("run"); run.add_argument("--slot"); run.add_argument("--scheduled", action="store_true")
    publish = sub.add_parser("publish"); publish.add_argument("--run-id", required=True)
    preview = sub.add_parser("preview"); preview.add_argument("--run-id", required=True)
    args = parser.parse_args()
    store = None
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            result = doctor(config)
        else:
            store = Store(config["database"])
            if args.command == "status":
                result = store.summary()
            elif args.command == "preview":
                result = {"run_id": args.run_id, "items": [
                    {"domain": row["domain"], "stage": row["stage"], "error": row["error"],
                     "result": json.loads(row["result"]) if row["result"] else None}
                    for row in store.rows("SELECT domain,stage,error,result FROM candidates WHERE run_id=? ORDER BY domain", (args.run_id,))],
                    "email_sending": False}
            elif args.command == "publish":
                from .runtime import CrmClient
                with store.exclusive():
                    result = Engine(config, store, None, CrmClient(config)).publish(args.run_id)
            elif args.scheduled and not config["enabled"]:
                result = {"state": "disabled", "email_sending": False}
            else:
                from .runtime import IvaRuntime, CrmClient
                crm = CrmClient(config) if config["mode"] == "ingest" else None
                result = Engine(config, store, IvaRuntime(config), crm).run(args.slot, args.scheduled)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("ok") is False else 0
    except Exception as exc:
        # Exception messages may include network credentials or untrusted page text.
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "detail": "See private checkpoint status; secrets and source text omitted."}))
        return 1
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
