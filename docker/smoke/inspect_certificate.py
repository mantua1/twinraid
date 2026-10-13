"""Eigenschaften von Server- und CA-Zertifikat als JSON (für den Starttest, Konzept 5.2)."""

import json
from datetime import timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

RUNTIME = Path("/run/twinraid-tls")
chain = x509.load_pem_x509_certificates((RUNTIME / "cert.pem").read_bytes())
server, ca = chain[0], x509.load_pem_x509_certificate((RUNTIME / "ca.pem").read_bytes())


def days(cert: x509.Certificate) -> float:
    return (cert.not_valid_after_utc - cert.not_valid_before_utc) / timedelta(days=1)


def ext[T: x509.ExtensionType](cert: x509.Certificate, kind: type[T]) -> x509.Extension[T]:
    return cert.extensions.get_extension_for_class(kind)


san = ext(server, x509.SubjectAlternativeName)
constraints = ext(ca, x509.NameConstraints)
key = server.public_key()
permitted = constraints.value.permitted_subtrees or []
print(
    json.dumps(
        {
            "chain_length": len(chain),
            "chain_ends_with_ca": chain[-1] == ca,
            "server_ca": ext(server, x509.BasicConstraints).value.ca,
            "server_auth": ExtendedKeyUsageOID.SERVER_AUTH in ext(server, x509.ExtendedKeyUsage).value,
            "server_days": days(server),
            "server_subject_empty": len(server.subject) == 0,
            "san_critical": san.critical,
            "ips": sorted(str(ip) for ip in san.value.get_values_for_type(x509.IPAddress)),
            "dns": sorted(san.value.get_values_for_type(x509.DNSName)),
            "curve": key.curve.name if isinstance(key, ec.EllipticCurvePublicKey) else None,
            "issued_by_ca": server.issuer == ca.subject,
            "ca_ca": ext(ca, x509.BasicConstraints).value.ca,
            "ca_path_length": ext(ca, x509.BasicConstraints).value.path_length,
            "ca_days": days(ca),
            "ca_key_cert_sign": ext(ca, x509.KeyUsage).value.key_cert_sign,
            "name_constraints_critical": constraints.critical,
            "permitted": sorted(str(name.value) for name in permitted),
        }
    )
)
