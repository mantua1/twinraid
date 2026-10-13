"""Eigenschaften des aktiven Zertifikats als JSON (für den Starttest, Konzept 5.2)."""

import json
from datetime import timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

cert = x509.load_pem_x509_certificates(Path("/run/twinraid-tls/cert.pem").read_bytes())[0]
basic = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
key = cert.public_key()
print(
    json.dumps(
        {
            "ca": basic.ca,
            "server_auth": ExtendedKeyUsageOID.SERVER_AUTH in eku,
            "validity_days": (cert.not_valid_after_utc - cert.not_valid_before_utc)
            / timedelta(days=1),
            "ips": sorted(str(ip) for ip in san.get_values_for_type(x509.IPAddress)),
            "dns": sorted(san.get_values_for_type(x509.DNSName)),
            "curve": key.curve.name if isinstance(key, ec.EllipticCurvePublicKey) else None,
        }
    )
)
