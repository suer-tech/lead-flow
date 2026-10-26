"""Stable local-time slots; missed intervals never create catch-up research."""
def cycle_slot(config, moment):
    interval = config.get("research_interval_minutes", 0)
    if not interval:
        return moment.date().isoformat()
    minutes = max(0, moment.hour * 60 + moment.minute - 9 * 60)
    start = 9 * 60 + (minutes // interval) * interval
    return moment.date().isoformat() + f"T{start // 60:02d}:{start % 60:02d}"
