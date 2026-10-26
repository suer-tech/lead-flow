from __future__ import annotations

import argparse
import json

from .config import load_config
from .runner import doctor, run, status


def main():
    parser = argparse.ArgumentParser(description="Lead Flow mail worker (disabled by default)")
    parser.add_argument("--config", help="Private JSON settings; no passwords in arguments")
    parser.add_argument("command", choices=("doctor", "run", "status"))
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        result = {"doctor": doctor, "run": run, "status": status}[args.command](config)
    except Exception:
        result = {"status": "error", "reason": "mailer_config_or_state_invalid"}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("status") in {"error", "failed", "blocked", "not_ready"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
