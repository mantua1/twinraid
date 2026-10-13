"""Drosselung der Passwortprüfung im Worker (Konzept 3.2).

Unabhängig vom Rate-Limit im Web-Prozess: Auch ein übernommener Web-Prozess
kann damit nicht schnell durchprobieren.
"""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

from twinraid.shared.passwords import PasswordStore, VerifyOutcome

MIN_DURATION_SECONDS = 1.0
WINDOW_SECONDS = 15 * 60
FREE_FAILURES = 5
MAX_DELAY_SECONDS = 30.0


class PasswordCheck:
    def __init__(
        self,
        store: PasswordStore,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._failures: deque[float] = deque()

    def penalty(self) -> float:
        """Wartezeit vor der nächsten Prüfung: ab 5 Fehlversuchen 1, 2, 4 … höchstens 30 s."""
        cutoff = self._clock() - WINDOW_SECONDS
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        excess = len(self._failures) - FREE_FAILURES
        if excess < 0:
            return 0.0
        return min(2.0**excess, MAX_DELAY_SECONDS)

    def reset(self) -> None:
        self._failures.clear()

    def is_set(self) -> bool:
        return self._store.is_set()

    async def verify(self, password: str) -> VerifyOutcome:
        # Immer nur eine Prüfung gleichzeitig; asyncio.Lock bedient in Reihenfolge.
        async with self._lock:
            started = self._clock()
            delay = self.penalty()
            if delay > 0:
                await self._sleep(delay)
            outcome = await asyncio.to_thread(self._store.verify, password)
            if outcome is VerifyOutcome.VALID:
                self.reset()
            elif outcome is VerifyOutcome.INVALID:
                self._failures.append(self._clock())
            remaining = MIN_DURATION_SECONDS - (self._clock() - started)
            if remaining > 0:
                await self._sleep(remaining)
            return outcome
