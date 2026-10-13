"""TLS-Material für Tests: CA und Serverzertifikat wie im Container."""

import os
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from twinraid.shared.addresses import TlsAddresses
from twinraid.shared.tls import (
    CaChange,
    CaChangeReason,
    fingerprint,
    generate_ca,
    generate_server,
    install_runtime,
)


def install_test_runtime(
    runtime_dir: Path,
    addresses: TlsAddresses,
    now: datetime | None = None,
    ca_change_reason: CaChangeReason | None = None,
) -> tuple[x509.Certificate, x509.Certificate]:
    """Legt Kette, Schlüssel, CA und info.json wie twinraid-init ab. Gibt (Server, CA) zurück.

    ca_change_reason: so ablegen, als sei diese CA gerade eine ersetzte gewesen.
    """
    now = now or datetime.now(UTC)
    ca_pem, ca_key_pem = generate_ca(addresses, now)
    ca = x509.load_pem_x509_certificate(ca_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    assert isinstance(ca_key, ec.EllipticCurvePrivateKey)
    server_pem, server_key_pem = generate_server(addresses, ca, ca_key, now)
    ca_change = None
    if ca_change_reason is not None:
        ca_change = CaChange(now, ca_change_reason, fingerprint(ca))
    install_runtime(
        runtime_dir,
        server_pem + ca_pem,
        server_key_pem,
        ca_pem,
        "ca",
        os.getgid(),
        addresses,
        ca_change,
    )
    return x509.load_pem_x509_certificate(server_pem), ca
