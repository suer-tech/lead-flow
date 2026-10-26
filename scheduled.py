"""Cheap daily admission; timer retries pending work without repeating a done cycle."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from zoneinfo import ZoneInfo

from worker.engine import load_config
from worker.schedule import cycle_slot


def due(config, now, rows):
    if not config['enabled']:
        return False, 'disabled'
    if not 9 <= now.hour < 22:
        return False, 'outside_window'
    pending = [r for r in rows if r['state'] in ('running', 'failed', 'waiting_resources', 'pending_publish')]
    if pending:
        return True, 'resume_checkpoint'
    interval = config.get('research_interval_minutes', 0)
    if any(r['slot'] == cycle_slot(config, now) for r in rows):
        return False, 'interval_already_completed' if interval else 'daily_cycle_already_completed'
    return True, 'new_interval_cycle' if interval else 'new_daily_cycle'


def main():
    config_path = '/etc/lead-flow/research.json'
    config = load_config(config_path)
    path = Path(config['database'])
    rows = []
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute('SELECT state,slot FROM runs WHERE campaign=?', (config['campaign'],))]
    moment = datetime.now(ZoneInfo(config['timezone']))
    allowed, reason = due(config, moment, rows)
    if allowed and reason in ('new_daily_cycle', 'new_interval_cycle') and shutil.disk_usage(path.parent).free < 1024**3:
        allowed, reason = False, 'low_disk_new_cycle_deferred'
    if not allowed:
        print(json.dumps({'state': reason, 'models_called': 0, 'email_sending': False}))
        return 0
    os.execv(sys.executable, [sys.executable, '-m', 'worker', '--config', config_path, 'run', '--scheduled', '--slot', cycle_slot(config, moment)])


if __name__ == '__main__':
    raise SystemExit(main())
