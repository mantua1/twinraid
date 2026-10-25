"""Schalter für „Kopierjob läuft“ in Tests (JobState, Konzept 4.1)."""


class FakeJobs:
    def __init__(self, running: bool = False) -> None:
        self.running = running

    def is_running(self) -> bool:
        return self.running

    def set_running(self, running: bool) -> None:
        self.running = running
