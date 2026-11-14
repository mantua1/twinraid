"""Ausgabe von twinraid-admin sync-check (Konzept 5.5, ab v0.5.1).

Nur lesend: Die Werte stammen aus get_status des Workers. Die drei Ebenen, der Wächter,
das eigene Dataset von TwinRaid, die Spiegel auf Groß aus dem letzten check und die
Grenze des ZFS-Caches auf Groß; ab v0.5.3 die Snapshots beider Seiten wie in der Kachel
Sync, der Abstand zwischen Snapshots und ein Hinweis auf einen Testwert (Konzept 4.1).
"""

from datetime import UTC, datetime

from twinraid.shared.source_guard import (
    GuardCheck,
    GuardCheckMirror,
    GuardDataset,
    GuardDetails,
    GuardState,
)
from twinraid.shared.status import StatusSnapshot

GIB = 1024**3
# Breite der ersten Spalte in der Tabelle der Ebenen.
_NAME_WIDTH = 24
# Standard des Abstands zwischen Snapshots; darunter ist es ein Testwert (Konzept 3.6, F17).
_DEFAULT_INTERVAL = 30
# Ab Version 4 des Wächters (Protokoll 3) steht der Abstand in source-guard.conf.
_INTERVAL_VERSION = 4


def _gib(value: int | None) -> str:
    return "?" if value is None else f"{value / GIB:.1f} GiB"


def _when(moment: datetime | None) -> str:
    if moment is None:
        return "-"
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _mark(present: bool | None) -> str:
    if present is None:
        return "?"
    return "yes" if present else "no"


def _guard_line(status: StatusSnapshot) -> str:
    guard = status.source_guard
    details = guard.details
    if guard.state is GuardState.ACTIVE and details is not None:
        return f"Source guard: active, version {details.version}"
    codes = [
        code.value
        for code in (guard.setup_problem, guard.status_problem, guard.error, guard.error_detail)
        if code is not None
    ]
    suffix = f" ({', '.join(codes)})" if codes else ""
    return f"Source guard: {guard.state.value}{suffix}"


def _levels(status: StatusSnapshot) -> list[str]:
    levels = status.sync.levels
    source = set(levels.source) if levels.source is not None else None
    target = set(levels.target) if levels.target is not None else None
    names = list(levels.configured)
    for extra in sorted((source or set()) | (target or set())):
        if extra not in names:
            names.append(extra)
    lines = [
        "Shares (twinraid.yaml / source-guard.conf / mirror.conf on the target):",
    ]
    for name in names:
        in_target = None if target is None else name in target
        in_source = None if source is None else name in source
        lines.append(
            f"  {name:<{_NAME_WIDTH}} {_mark(name in levels.configured):<4} "
            f"{_mark(in_source):<4} {_mark(in_target)}"
        )
    if target is None:
        lines.append("  mirror.conf is unknown until a dry run has asked the target (check).")
    return lines


def _check(check: GuardCheck | None) -> list[str]:
    if check is None:
        return ["Target: no check yet; start a dry run."]
    lines = [f"Target: last check {_when(check.at)}: {check.result.value}"]
    summary = check.summary
    if summary is not None:
        reserve = "?" if summary.reserve_percent is None else f"{summary.reserve_percent} %"
        lines.append(
            f"  free {_gib(summary.available)} of {_gib(summary.size)}, reserve {reserve}, "
            f"root {summary.root}, mirror.conf {summary.conf}, delegation {summary.deleg}, "
            f"receive {summary.receive}, clones {_count(summary.clones)}"
        )
    for mirror in check.mirrors:
        lines.append(
            f"  {mirror.name}: {mirror.state}, snapshots {_count(mirror.count)}, "
            f"newest received {_when(mirror.newest_received)}, props {mirror.props or '-'}"
        )
    return lines


def _count(value: int | None) -> str:
    return "?" if value is None else str(value)


def _self_dataset(details: GuardDetails | None, shares: tuple[str, ...]) -> str:
    if details is None:
        return "Own dataset of TwinRaid: unknown"
    needed = "needed: appdata is mirrored" if "appdata" in shares else "not needed yet"
    return f"Own dataset of TwinRaid: {details.zfs.self_dataset.value} ({needed})"


def _arc(status: StatusSnapshot) -> str:
    zfs = status.target_zfs
    if zfs is None:
        return "ZFS cache on the target: unknown (target offline or no status line yet)"
    if zfs.arc_max == 0:
        limit = "not set in Unraid (Settings → Disk Settings → zfs_arc_max)"
    else:
        limit = _gib(zfs.arc_max)
    return f"ZFS cache on the target: zfs_arc_max {limit}, c_max {_gib(zfs.c_max)}"


def _interval(status: StatusSnapshot) -> list[str]:
    """Abstand zwischen Snapshots und frühester nächster Snapshot (Konzept 3.6)."""
    details = status.source_guard.details
    if details is None:
        return []
    minutes = details.limits.min_snapshot_interval_minutes
    if details.version < _INTERVAL_VERSION:
        lines = [f"Interval between snapshots: {minutes} min (fixed)"]
    else:
        lines = [f"Interval between snapshots: {minutes} min"]
    if details.version >= _INTERVAL_VERSION and minutes < _DEFAULT_INTERVAL:
        lines.append(
            f"  Note: test value active: interval between snapshots {minutes} min "
            f"(default {_DEFAULT_INTERVAL}); remove min_snapshot_interval_minutes from "
            "source-guard.conf after the tests."
        )
    allowed_at = status.sync.snapshot_allowed_at
    if allowed_at is not None:
        lines.append(f"  Next snapshot not before {_when(allowed_at)}")
    return lines


def _source_snapshots(item: GuardDataset, details: GuardDetails) -> str:
    """Klein: eigene Snapshots aus dem ZFS-Teil und dem Job (Konzept 4.1, Kachel Sync)."""
    job = details.job
    newest = _when(job.created_at) if job is not None else "-"
    base = f"held {_when(item.base_created_at)}" if item.base_created_at is not None else "none"
    return (
        f"    Klein: {_count(item.snapshots)} own, newest {newest}, "
        f"oldest {_when(item.oldest_snapshot_at)}, base {base}, bookmark {_mark(item.bookmark)}, "
        f"kept {details.limits.snapshot_keep_days} days"
    )


def _target_snapshots(mirror: GuardCheckMirror | None, check: GuardCheck | None) -> str:
    """Groß: aus dem letzten check (Konzept 3.7); ohne check unbekannt."""
    if check is None or mirror is None:
        return "    Groß: unknown (no dry run or sync yet)"
    if mirror.state == "absent":
        return f"    Groß: no mirror yet (check {_when(check.at)})"
    if mirror.state == "foreign":
        return f"    Groß: foreign dataset (check {_when(check.at)})"
    locked = ", locked" if mirror.props == "mismatch" else ""
    keep = check.summary.keep_days if check.summary is not None else None
    return (
        f"    Groß: {_count(mirror.count)} snapshots{locked}, "
        f"newest received {_when(mirror.newest_received)}, "
        f"created {_when(mirror.newest_created)}, oldest {_when(mirror.oldest_created)}, "
        f"kept {_count(keep)} days (check {_when(check.at)})"
    )


def _snapshots(status: StatusSnapshot) -> list[str]:
    """Zweite Tabelle (ab v0.5.3): die Snapshots beider Seiten je Share."""
    details = status.source_guard.details
    if details is None:
        return ["Snapshots: unknown (no fresh status of the source guard)"]
    check = details.target_check
    mirrors = {mirror.name: mirror for mirror in check.mirrors} if check is not None else {}
    lines = ["Snapshots per share:"]
    for item in details.zfs.datasets:
        lines.append(f"  {item.name}")
        lines.append(_source_snapshots(item, details))
        lines.append(_target_snapshots(mirrors.get(item.name), check))
    return lines


def sync_check_lines(status: StatusSnapshot) -> list[str]:
    details = status.source_guard.details
    lines = [
        "TwinRaid sync check (read only)",
        _guard_line(status),
        _self_dataset(details, status.sync.levels.configured),
        *_levels(status),
        *_check(details.target_check if details is not None else None),
        _arc(status),
        *_interval(status),
        *_snapshots(status),
    ]
    last = status.sync.last
    if last is not None:
        reason = f" ({last.reason.value})" if last.reason is not None else ""
        lines.append(f"Last dry run: {last.outcome.value}{reason} at {_when(last.finished_at)}")
    return lines
