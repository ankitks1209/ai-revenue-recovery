from datetime import datetime, timedelta
from src.infrastructure.ports import ClockPort

class SystemClock(ClockPort):
    def now(self) -> datetime:
        return datetime.utcnow()

class SimulatedClock(ClockPort):
    def __init__(self, initial_time: datetime = None):
        self._current_time = initial_time or datetime(2026, 1, 1, 10, 0, 0)

    def now(self) -> datetime:
        return self._current_time

    def set_time(self, new_time: datetime):
        self._current_time = new_time

    def advance(self, seconds: float = 0, hours: float = 0, days: float = 0):
        delta = timedelta(seconds=seconds, hours=hours, days=days)
        self._current_time += delta
