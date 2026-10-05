"""Event bus.

Components talk only through ``MessageBus.publish``/``subscribe``. The
in-process implementation is a deterministic discrete-event scheduler: events
are delivered in (timestamp, publish-sequence) order on a simulated clock, so a
run is exactly reproducible.

Swapping in Kafka or NATS means implementing the same two methods: topics map
1:1 to Kafka topics / NATS subjects, ``key`` becomes the partition key (so all
events for one order stay ordered), and the event timestamp becomes the record
timestamp. Every consumer is idempotent, so at-least-once delivery is safe.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol


@dataclass(order=True)
class Event:
    ts: datetime
    seq: int
    topic: str = field(compare=False)
    payload: Any = field(compare=False)
    key: str | None = field(compare=False, default=None)


Handler = Callable[[Event], None]


class MessageBus(Protocol):
    def publish(self, topic: str, payload: Any, ts: datetime, key: str | None = None) -> None: ...

    def subscribe(self, topic: str, handler: Handler) -> None: ...


class InProcessBus:
    def __init__(self, start: datetime):
        self.now = start
        self._queue: list[Event] = []
        self._seq = 0
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self.delivered: dict[str, int] = defaultdict(int)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].append(handler)

    def publish(self, topic: str, payload: Any, ts: datetime, key: str | None = None) -> None:
        if ts < self.now:  # causality: nothing can be scheduled in the past
            ts = self.now
        self._seq += 1
        heapq.heappush(self._queue, Event(ts, self._seq, topic, payload, key))

    def run(self, until: datetime | None = None) -> None:
        while self._queue and (until is None or self._queue[0].ts <= until):
            ev = heapq.heappop(self._queue)
            self.now = ev.ts
            self.delivered[ev.topic] += 1
            for handler in self._subs.get(ev.topic, ()):
                handler(ev)
