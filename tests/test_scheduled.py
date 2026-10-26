from datetime import datetime

from scheduled import due
from worker.schedule import cycle_slot


def test_one_daily_cycle_and_checkpoint_retry():
    config = {'enabled': True}
    today = datetime(2026, 10, 26, 9)
    assert due(config, today, []) == (True, 'new_daily_cycle')
    assert due(config, today, [{'state': 'completed', 'slot': '2026-10-26'}])[0] is False
    assert due(config, today, [{'state': 'failed', 'slot': '2026-10-25'}]) == (True, 'resume_checkpoint')
    assert due(config, today, [{'state': 'pending_publish', 'slot': '2026-10-25'}]) == (True, 'resume_checkpoint')
    assert due(config, today.replace(hour=8), [])[0] is False
    assert due({'enabled': False}, today, [])[0] is False


def test_interval_slots_resume_and_never_replay_missed_slots():
    config={'enabled':True,'research_interval_minutes':120}
    now=datetime(2026,10,26,16,59)
    assert cycle_slot(config,now)=='2026-10-26T15:00'
    assert due(config,now,[])==(True,'new_interval_cycle')
    done=[{'state':'completed','slot':'2026-10-26T15:00'}]
    assert due(config,now,done)==(False,'interval_already_completed')
    assert due(config,now.replace(hour=17,minute=0),done)==(True,'new_interval_cycle')
    pending=[{'state':'pending_publish','slot':'2026-10-26T09:00'}]
    assert due(config,now,pending)==(True,'resume_checkpoint')
    assert due(config,now.replace(hour=22),[])==(False,'outside_window')
    assert cycle_slot(config,now.replace(hour=9,minute=0))=='2026-10-26T09:00'
