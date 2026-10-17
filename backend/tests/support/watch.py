"""Ersatz für die Sitzung vm-watch in Tests von Status und Steuerung."""

from dataclasses import dataclass, field

from twinraid.integrations.vm_watch import WatchReport, WatchView
from twinraid.shared.status import StatusReason


def report(
    vm: str = "RUNNING",
    others: tuple[str, ...] | None = (),
    name: str | None = "Windows 11 Gaming",
    connect_plugin: bool = False,
    flash_backup: str = "inactive",
) -> WatchReport:
    return WatchReport(
        vm=vm,
        name=name,
        others=others,
        connect_plugin=connect_plugin,
        flash_backup=flash_backup,
    )


@dataclass
class StubWatch:
    """Liefert einen festen Stand und merkt sich die Ergebnisse der TCP-Prüfung."""

    current: WatchView = field(default_factory=lambda: WatchView(report()))
    online_calls: list[bool] = field(default_factory=list)

    def view(self) -> WatchView:
        return self.current

    def set_target_online(self, online: bool) -> None:
        self.online_calls.append(online)

    def show(self, value: WatchReport | StatusReason) -> None:
        if isinstance(value, StatusReason):
            self.current = WatchView(None, value)
        else:
            self.current = WatchView(value)
