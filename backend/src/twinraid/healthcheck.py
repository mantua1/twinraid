"""Docker-Healthcheck (Konzept 5.4).

Sendet keine HTTP-Anfrage: Er baut nur eine TLS-Verbindung zu 127.0.0.1 auf
und vergleicht das ausgelieferte Zertifikat mit /run/twinraid-tls. Der
Netzfilter arbeitet auf HTTP-Ebene und wird dadurch nicht berührt. Zusätzlich
prüft er den Herzschlag des Workers.
"""

import hashlib
import os
import socket
import ssl
import sys
import time

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from twinraid.config import ConfigError, load_config
from twinraid.shared import runtime

HEARTBEAT_MAX_AGE_SECONDS = 30.0
TIMEOUT_SECONDS = 5.0


class HealthError(Exception):
    pass


def served_certificate_sha256(port: int) -> str:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Geprüft wird nicht die Vertrauenskette, sondern der Abgleich mit der Datei.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT_SECONDS) as raw,
        context.wrap_socket(raw) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    if der is None:
        raise HealthError("no certificate was presented")
    return hashlib.sha256(der).hexdigest()


def check() -> None:
    env = os.environ
    try:
        port = load_config(runtime.config_path(env)).web.port
    except ConfigError as exc:
        raise HealthError("configuration is invalid") from exc
    cert_path = runtime.runtime_tls_dir(env) / "cert.pem"
    try:
        certificate = x509.load_pem_x509_certificates(cert_path.read_bytes())[0]
    except (OSError, ValueError) as exc:
        raise HealthError(f"cannot read {cert_path}") from exc
    expected = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
    try:
        served = served_certificate_sha256(port)
    except OSError as exc:
        raise HealthError(f"web process does not accept TLS connections: {exc}") from exc
    if served != expected:
        raise HealthError("the web process serves a different certificate")

    heartbeat = runtime.socket_path(env).parent / "worker.heartbeat"
    try:
        age = time.time() - heartbeat.stat().st_mtime
    except OSError as exc:
        raise HealthError("worker heartbeat is missing") from exc
    if age > HEARTBEAT_MAX_AGE_SECONDS:
        raise HealthError(f"worker heartbeat is {age:.0f} s old")


def main() -> int:
    try:
        check()
    except HealthError as exc:
        sys.stderr.write(f"unhealthy: {exc}\n")
        return 1
    sys.stdout.write("healthy\n")
    return 0
