from datetime import datetime

from scheduled import due


def test_one_daily_cycle_and_checkpoint_retry():
    config = {'enabled': True}
    today = datetime(2026, 10, 26, 9)
    assert due(config, today, []) == (True, 'new_daily_cycle')
    assert due(config, today, [{'state': 'completed', 'slot': '2026-10-26'}])[0] is False
    assert due(config, today, [{'state': 'failed', 'slot': '2026-10-25'}]) == (True, 'resume_checkpoint')
    assert due(config, today, [{'state': 'pending_publish', 'slot': '2026-10-25'}]) == (True, 'resume_checkpoint')
    assert due(config, today.replace(hour=8), [])[0] is False
    assert due({'enabled': False}, today, [])[0] is False
