"""TLS-Material für Tests: CA und Serverzertifikat wie im Container."""

import os
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from twinraid.config.models import TlsConfig
from twinraid.shared.tls import generate_ca, generate_server, install_runtime


def install_test_runtime(
    runtime_dir: Path, tls_config: TlsConfig, now: datetime | None = None
) -> tuple[x509.Certificate, x509.Certificate]:
    """Legt Kette, Schlüssel und CA wie twinraid-init ab. Gibt (Server, CA) zurück."""
    now = now or datetime.now(UTC)
    ca_pem, ca_key_pem = generate_ca(tls_config, now)
    ca = x509.load_pem_x509_certificate(ca_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    assert isinstance(ca_key, ec.EllipticCurvePrivateKey)
    server_pem, server_key_pem = generate_server(tls_config, ca, ca_key, now)
    install_runtime(runtime_dir, server_pem + ca_pem, server_key_pem, ca_pem, "ca", os.getgid())
    return x509.load_pem_x509_certificate(server_pem), ca
