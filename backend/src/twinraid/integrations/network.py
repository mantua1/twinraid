"""Netzwerk-Checks: Ist ein Server per TCP erreichbar? (Konzept 3.4)."""

import asyncio
import contextlib

TCP_TIMEOUT_SECONDS = 2.0


async def tcp_reachable(host: str, port: int, wait_seconds: float = TCP_TIMEOUT_SECONDS) -> bool:
    """Baut nur eine TCP-Verbindung auf und schließt sie sofort; es werden keine Daten gesendet."""
    try:
        async with asyncio.timeout(wait_seconds):
            _reader, writer = await asyncio.open_connection(host, port)
    except OSError, TimeoutError:
        return False
    writer.close()
    with contextlib.suppress(OSError, TimeoutError):
        async with asyncio.timeout(wait_seconds):
            await writer.wait_closed()
    return True
