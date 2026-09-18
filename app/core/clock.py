import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime.datetime: ...


class SystemClock:
    def now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)


class FrozenClock:
    def __init__(self, at: datetime.datetime) -> None:
        self._at = at

    def now(self) -> datetime.datetime:
        return self._at
