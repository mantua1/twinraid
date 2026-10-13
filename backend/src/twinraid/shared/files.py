"""Sicheres Schreiben von Dateien: atomar, ohne Symlinks zu folgen, mit festen Rechten."""

import contextlib
import os
from pathlib import Path


def write_file_atomic(path: Path, data: bytes, mode: int, group: int | None = None) -> None:
    """Schreibt erst in eine temporäre Datei im selben Verzeichnis und benennt dann um.

    Die Datei hat von Anfang an die gewünschten Rechte; sie ist nie kurz für
    andere lesbar. Ein Absturz hinterlässt nie eine halb geschriebene Datei.
    """
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(fd, mode)
        if group is not None:
            os.fchown(fd, -1, group)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        temporary.unlink(missing_ok=True)
        raise
    os.close(fd)
    temporary.replace(path)
