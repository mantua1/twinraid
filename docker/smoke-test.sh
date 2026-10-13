#!/usr/bin/env bash
# Starttest für das Container-Image (Konzept 5.1 bis 5.4, 5.9 und 6.4).
#
# Prüft unter anderem: Start mit read-only Root und --cap-drop=ALL plus nur den
# nötigen Capabilities, Rechte der Prozesse und Dateien, nur HTTPS (HTTP wird
# abgewiesen), CA und Serverzertifikat samt Fingerabdrücken (die CA ist der
# einzige Vertrauensanker), Healthcheck, Login mit Session und CSRF, Live-Datenstrom,
# simulierten Kopierjob, Sperre nach Fehlversuchen samt "unlock", keine
# /api-Route ohne Login, keine Konfigurationswerte in öffentlichen Dateien,
# Netzfilter, Host-Prüfung, Security-Header, Neustart, "renew-cert" mit und ohne
# neue CA, den Modus real nur lesend und den Startabbruch bei Fehlern.
#
# Aufruf aus dem Repo-Hauptordner:
#   docker/smoke-test.sh [IMAGE]          Standard-Image: twinraid:dev
#   CHECK_EACH_CAP=1 docker/smoke-test.sh  prüft zusätzlich, dass jede Capability nötig ist
set -euo pipefail

IMAGE="${1:-twinraid:dev}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${REPO_DIR}/config.example.yaml"
# Konfiguration mit eindeutigen Testwerten für die Prüfung der öffentlichen Dateien.
CANARY_CONFIG="${REPO_DIR}/docker/smoke/canary-config.yaml"
INSPECT_SCRIPT="$(cat "${REPO_DIR}/docker/smoke/inspect_processes.py")"
CONTENT_SCRIPT="$(cat "${REPO_DIR}/docker/smoke/check_public_content.py")"
ROUTES_SCRIPT="$(cat "${REPO_DIR}/docker/smoke/list_routes.py")"
CERT_SCRIPT="$(cat "${REPO_DIR}/docker/smoke/inspect_certificate.py")"
PY=/app/.venv/bin/python
PORT="${SMOKE_PORT:-18443}"
BASE="https://127.0.0.1:${PORT}"
ORIGIN="Origin: ${BASE}"
JSON="Content-Type: application/json"
PREFIX="twinraid-smoke-$$"
WORKDIR="$(mktemp -d)"
# Nur das CA-Zertifikat dient als Vertrauensanker, wie später auf den Geräten.
CERT="${WORKDIR}/ca.pem"
JAR="${WORKDIR}/cookies.txt"
PASSWORD="smoke-test-password-42"
WRONG_PASSWORD="smoke-wrong-password-17"

# Muss mit docker/run-local.sh und Konzept 5.4 übereinstimmen.
CAPS=(SETUID SETGID SETPCAP KILL DAC_READ_SEARCH)
# Die Kommas gehören zu den tmpfs-Optionen, nicht zur Array-Syntax.
# shellcheck disable=SC2054
HARDENING=(
  --read-only
  --tmpfs /run:rw,exec,nosuid,nodev,mode=0755,size=16m
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m
  --cap-drop=ALL
  --security-opt=no-new-privileges
)

failures=0
pass() { echo "ok    $*"; }
fail() { echo "FAIL  $*" >&2; failures=$((failures + 1)); }
expect_equal() {
  local label="$1" actual="$2" expected="$3"
  if [[ "${actual}" == "${expected}" ]]; then
    pass "${label}"
  else
    fail "${label}: expected '${expected}', got '${actual}'"
  fi
}
expect_contains() {
  local label="$1" haystack="$2" needle="$3"
  if [[ "${haystack}" == *"${needle}"* ]]; then
    pass "${label}"
  else
    fail "${label}: '${needle}' not found in '${haystack:0:300}'"
  fi
}
expect_not_contains() {
  local label="$1" haystack="$2" needle="$3"
  if [[ "${haystack}" != *"${needle}"* ]]; then
    pass "${label}"
  else
    fail "${label}: '${needle}' must not appear"
  fi
}

cleanup() {
  local ids volumes
  ids="$(docker ps -aq --filter "name=${PREFIX}")"
  if [[ -n "${ids}" ]]; then
    # shellcheck disable=SC2086
    docker rm -f ${ids} >/dev/null
  fi
  volumes="$(docker volume ls -q --filter "name=${PREFIX}")"
  if [[ -n "${volumes}" ]]; then
    # shellcheck disable=SC2086
    docker volume rm ${volumes} >/dev/null
  fi
  rm -r "${WORKDIR}"
}
trap cleanup EXIT

# Startet einen Container mit eigenem Volume für /data. $1 = Name,
# $2 = wegzulassende Capability (oder ""), $3 = Konfiguration, Rest = docker-run-Argumente.
start() {
  local name="$1" skip="$2" config="$3" cap
  shift 3
  local cap_args=() data_args=(--mount "type=volume,src=${name}-data,dst=/data")
  for cap in "${CAPS[@]}"; do
    [[ "${cap}" == "${skip}" ]] || cap_args+=("--cap-add=${cap}")
  done
  # NO_VOLUME=1: /data kommt aus den übergebenen Argumenten (z. B. ein tmpfs).
  [[ "${NO_VOLUME:-0}" == "1" ]] && data_args=()
  docker run -d --name "${name}" "${HARDENING[@]}" "${cap_args[@]}" \
    -v "${config}:/config/twinraid.yaml:ro" "${data_args[@]}" "$@" "${IMAGE}" >/dev/null
}

running() { [[ "$(docker inspect -f '{{.State.Running}}' "$1")" == "true" ]]; }
exit_code() { docker inspect -f '{{.State.ExitCode}}' "$1"; }

wait_for_https() {
  local name="$1" port="${2:-${PORT}}"
  for _ in $(seq 1 40); do
    if [[ "$(curl -sk --max-time 2 "https://127.0.0.1:${port}/health" || true)" == "ok" ]]; then
      return 0
    fi
    running "${name}" || return 1
    sleep 1
  done
  return 1
}

wait_for_exit() {
  for _ in $(seq 1 30); do
    running "$1" || return 0
    sleep 1
  done
  return 1
}

json_get() { python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)[sys.argv[1]]))' "$@"; }
json_str() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$@"; }
field() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[sys.argv[1]][sys.argv[2]])' "$@"; }

https_get() { curl -s --cacert "${CERT}" "$@"; }
status_of() { curl -s -o /dev/null -w '%{http_code}' --cacert "${CERT}" "$@"; }
headers_of() { curl -s -o /dev/null -D - --cacert "${CERT}" "$@" | tr -d '\r'; }
login_status() {
  status_of -H "${ORIGIN}" -H "${JSON}" -d "{\"password\": \"$1\"}" "${BASE}/api/auth/login"
}
served_fingerprint() {
  python3 - "${PORT}" <<'PY'
import hashlib
import ssl
import sys

pem = ssl.get_server_certificate(("127.0.0.1", int(sys.argv[1])))
print(hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest().hex(":").upper())
PY
}

echo "== Image: ${IMAGE}"

# --- 1. Start mit minimalen Capabilities ------------------------------------------
main="${PREFIX}-main"
start "${main}" "" "${CANARY_CONFIG}" -e TWINRAID_MODE=mock -p "127.0.0.1:${PORT}:8443" \
  --health-interval=2s --health-start-period=60s
if wait_for_https "${main}"; then
  pass "container starts read-only with --cap-drop=ALL + ${CAPS[*]}"
else
  fail "container did not become reachable"
  docker logs "${main}" 2>&1 | tail -40
  exit 1
fi
docker exec "${main}" cat /run/twinraid-tls/ca.pem >"${CERT}"
expect_contains "log warns about simulation mode" "$(docker logs "${main}" 2>&1)" "SIMULATION MODE ACTIVE"

# --- 2. Nur HTTPS, CA, Serverzertifikat und Healthcheck ------------------------------------
expect_equal "HTTPS works with only the CA as trust anchor (name constraints accepted)" \
  "$(https_get "${BASE}/health")" "ok"
expect_equal "plain HTTP gets no answer" "$(curl -s --max-time 5 "http://127.0.0.1:${PORT}/health" || true)" ""
cert_info="$(docker exec "${main}" "${PY}" -c "${CERT_SCRIPT}")"
expect_equal "chain is server plus CA" "$(json_get chain_length <<<"${cert_info}")" "2"
expect_equal "chain ends with the CA" "$(json_get chain_ends_with_ca <<<"${cert_info}")" "true"
expect_equal "server certificate is not a CA" "$(json_get server_ca <<<"${cert_info}")" "false"
expect_equal "server certificate has EKU serverAuth" "$(json_get server_auth <<<"${cert_info}")" "true"
expect_equal "server certificate is valid for exactly 825 days" "$(json_get server_days <<<"${cert_info}")" "825.0"
expect_equal "server certificate has an empty subject" "$(json_get server_subject_empty <<<"${cert_info}")" "true"
expect_equal "SAN is critical (RFC 5280, empty subject)" "$(json_get san_critical <<<"${cert_info}")" "true"
expect_equal "server certificate uses ECDSA P-256" "$(json_str curve <<<"${cert_info}")" "secp256r1"
expect_equal "server certificate is issued by the CA" "$(json_get issued_by_ca <<<"${cert_info}")" "true"
expect_equal "SAN contains the configured IPs" "$(json_get ips <<<"${cert_info}")" '["10.77.31.50", "127.0.0.1"]'
expect_equal "SAN contains the configured hostname" "$(json_get dns <<<"${cert_info}")" '["canary-host-7q.home.arpa"]'
expect_equal "CA is a CA" "$(json_get ca_ca <<<"${cert_info}")" "true"
expect_equal "CA has pathLenConstraint 0" "$(json_get ca_path_length <<<"${cert_info}")" "0"
expect_equal "CA is valid for 10 years" "$(json_get ca_days <<<"${cert_info}")" "3650.0"
expect_equal "CA may sign certificates" "$(json_get ca_key_cert_sign <<<"${cert_info}")" "true"
expect_equal "CA name constraints are critical" "$(json_get name_constraints_critical <<<"${cert_info}")" "true"
expect_equal "CA allows only the own addresses" "$(json_get permitted <<<"${cert_info}")" \
  '["10.77.31.50/32", "127.0.0.1/32", "canary-host-7q.home.arpa"]'
logs="$(docker logs "${main}" 2>&1)"
logged_fp="$(grep -o 'TLS server certificate (ca) SHA-256 fingerprint: [0-9A-F:]*' <<<"${logs}" | tail -1 | awk '{print $NF}')"
first_fp="$(served_fingerprint)"
expect_equal "server fingerprint in the log matches the served certificate" "${logged_fp}" "${first_fp}"
ca_fp="$(python3 -c 'import hashlib,ssl,sys; print(hashlib.sha256(ssl.PEM_cert_to_DER_cert(open(sys.argv[1]).read())).digest().hex(":").upper())' "${CERT}")"
expect_contains "CA fingerprint is in the log" "${logs}" "TLS CA SHA-256 fingerprint: ${ca_fp}"
health="starting"
for _ in $(seq 1 30); do
  health="$(docker inspect -f '{{.State.Health.Status}}' "${main}")"
  [[ "${health}" == "healthy" ]] && break
  sleep 1
done
expect_equal "Docker healthcheck reports healthy" "${health}" "healthy"
expect_not_contains "healthcheck causes no rejected requests" "$(docker logs "${main}" 2>&1)" "not in web.allowed_networks"

# --- 3. Rechte der Prozesse ------------------------------------------------------------
procs="$(docker exec "${main}" python3 -c "${INSPECT_SCRIPT}")"
check() {
  local proc="$1" key="$2" expected="$3"
  expect_equal "${proc} ${key}=${expected}" "$(field "${proc}" "${key}" <<<"${procs}")" "${expected}"
}
check twinraid-worker Uid 0
check twinraid-worker Gid 10001
check twinraid-worker Groups ""
check twinraid-worker CapEff 0000000000000004
check twinraid-worker CapBnd 0000000000000004
check twinraid-worker CapAmb 0000000000000000
check twinraid-worker NoNewPrivs 1
check twinraid-web Uid 10001
check twinraid-web Gid 10001
check twinraid-web Groups ""
check twinraid-web CapEff 0000000000000000
check twinraid-web CapBnd 0000000000000000
check twinraid-web NoNewPrivs 1

# --- 4. Dateirechte und Dateisystem ----------------------------------------------------
perm() { docker exec "${main}" stat -c '%a %u' "$1"; }
perm_group() { docker exec "${main}" stat -c '%a %u:%g' "$1"; }
expect_equal "/srv/worker is 0700 root" "$(perm /srv/worker)" "700 0"
expect_equal "/data/secrets is 0700 root" "$(perm /data/secrets)" "700 0"
expect_equal "/data/tls is 0700 root" "$(perm /data/tls)" "700 0"
expect_equal "/data/web is 0770 root:10001" "$(perm_group /data/web)" "770 0:10001"
expect_equal "web database is 0600 web user" "$(perm_group /data/web/web.sqlite)" "600 10001:10001"
expect_equal "/run/twinraid-tls is 0750 root:10001" "$(perm_group /run/twinraid-tls)" "750 0:10001"
expect_equal "TLS key copy is 0640 root:10001" "$(perm_group /run/twinraid-tls/key.pem)" "640 0:10001"
expect_equal "CA key is 0600 root" "$(perm /data/tls/ca-key.pem)" "600 0"
ca_key_scan='import pathlib
key=pathlib.Path("/data/tls/ca-key.pem").read_bytes()
print(any(key in p.read_bytes() for p in pathlib.Path("/run/twinraid-tls").iterdir()))'
expect_equal "web process gets no copy of the CA key" "$(docker exec "${main}" "${PY}" -c "${ca_key_scan}")" "False"
expect_equal "socket dir is 0750 root:10001" "$(perm_group /run/twinraid)" "750 0:10001"
expect_equal "socket is 0660 root:10001" "$(perm_group /run/twinraid/worker.sock)" "660 0:10001"
for dir in /srv/worker /data/secrets /data/tls; do
  if docker exec -u 10001:10001 "${main}" ls "${dir}" >/dev/null 2>&1; then
    fail "web user can read ${dir}"
  else
    pass "web user cannot read ${dir}"
  fi
done
if docker exec "${main}" touch /app/probe >/dev/null 2>&1; then
  fail "root filesystem is writable"
else
  pass "root filesystem is read-only"
fi

# --- 5. Worker-Socket weist fremde UIDs ab ----------------------------------------------
probe='import socket;s=socket.socket(socket.AF_UNIX);s.connect("/run/twinraid/worker.sock");s.sendall(b"{\"command\":\"get_status\"}\n");print(s.recv(300).decode())'
expect_contains "worker rejects foreign UID (SO_PEERCRED)" \
  "$(docker exec -u 10002:10001 "${main}" python3 -c "${probe}" 2>&1 || true)" '"error":"forbidden"'
if docker exec -u 10002:10002 "${main}" python3 -c "${probe}" >/dev/null 2>&1; then
  fail "socket reachable without group"
else
  pass "socket not reachable without group 10001"
fi

# --- 6. Ohne Login ist keine /api-Route erreichbar ---------------------------------------
routes="$(docker exec "${main}" "${PY}" -c "${ROUTES_SCRIPT}")"
checked=0
while read -r method path public; do
  [[ "${method}" == "HEAD" || "${public}" == "True" ]] && continue
  expect_equal "${method} ${path} without session is 401" \
    "$(status_of -X "${method}" -H "${ORIGIN}" "${BASE}${path}")" "401"
  checked=$((checked + 1))
done < <(python3 -c 'import json,sys
for r in json.load(sys.stdin):
    for m in r["methods"]:
        print(m, r["path"], r["public"])' <<<"${routes}")
if [[ "${checked}" -ge 7 ]]; then pass "checked ${checked} protected API routes"; else fail "only ${checked} protected API routes found"; fi
expect_equal "unknown path without session is 401" "$(status_of "${BASE}/api/nope")" "401"

# --- 7. Öffentliche Dateien enthalten keine Konfigurationswerte ----------------------------
content="$(docker exec "${main}" "${PY}" -c "${CONTENT_SCRIPT}")"
printf '%s' "${content}" >"${WORKDIR}/content.json"
leaks="$(python3 - "${BASE}" "${CERT}" "${WORKDIR}/content.json" <<'PY'
import json
import ssl
import sys
import urllib.request

with open(sys.argv[3], encoding="utf-8") as handle:
    data = json.load(handle)
context = ssl.create_default_context(cafile=sys.argv[2])
leaks = []
for path in data["paths"]:
    with urllib.request.urlopen(sys.argv[1] + path, context=context, timeout=5) as response:
        body = response.read().decode("utf-8", errors="replace").lower()
    leaks += [f"{path}: {needle}" for needle in data["needles"] if needle in body]
print(json.dumps({"checked": len(data["paths"]), "leaks": leaks}))
PY
)"
expect_equal "public files contain no config values" "$(json_get leaks <<<"${leaks}")" "[]"
expect_equal "content check covered all public files" "$(json_get checked <<<"${leaks}")" \
  "$(python3 -c 'import json,sys; print(len(json.load(sys.stdin)["paths"]))' <<<"${content}")"

# --- 8. Passwort, Login, Session, CSRF, Logout --------------------------------------------
expect_contains "no password set after first start" "$(https_get "${BASE}/api/auth/state")" '"password_set":false'
expect_equal "login fails while no password is set" "$(login_status "irgendein-passwort")" "401"
expect_contains "set-password via admin CLI" \
  "$(docker exec -i "${main}" twinraid-admin set-password --stdin <<<"${PASSWORD}" 2>&1)" "Password set"
expect_contains "password is now reported as set" "$(https_get "${BASE}/api/auth/state")" '"password_set":true'
expect_equal "wrong password is rejected" "$(login_status "${WRONG_PASSWORD}")" "401"
expect_equal "login without Origin is rejected" \
  "$(status_of -H "${JSON}" -d "{\"password\": \"${PASSWORD}\"}" "${BASE}/api/auth/login")" "403"
expect_equal "login with foreign Origin is rejected" \
  "$(status_of -H "Origin: https://evil.example" -H "${JSON}" -d "{\"password\": \"${PASSWORD}\"}" "${BASE}/api/auth/login")" "403"
login_headers="$(curl -s -D - -o "${WORKDIR}/login.json" -c "${JAR}" --cacert "${CERT}" -H "${ORIGIN}" -H "${JSON}" \
  -d "{\"password\": \"${PASSWORD}\"}" "${BASE}/api/auth/login" | tr -d '\r')"
expect_contains "login succeeds" "${login_headers}" "HTTP/1.1 200"
cookie_line="$(grep -i '^set-cookie:' <<<"${login_headers}" || true)"
for attribute in "__Host-twinraid_session=" "HttpOnly" "Secure" "Path=/" "SameSite=strict"; do
  expect_contains "session cookie has ${attribute}" "${cookie_line,,}" "${attribute,,}"
done
csrf="$(json_str csrf_token <"${WORKDIR}/login.json")"
expect_equal "status with session" "$(status_of -b "${JAR}" "${BASE}/api/status")" "200"
expect_contains "API delivers config values after login" "$(https_get -b "${JAR}" "${BASE}/api/info")" \
  "$(json_str source_name <<<"${content}")"
expect_contains "audit log lists the login" "$(https_get -b "${JAR}" "${BASE}/api/audit")" '"action":"login","result":"success"'
expect_contains "status carries reasons and the job flag" "$(https_get -b "${JAR}" "${BASE}/api/status")" '"job_running":false'
ca_download="${WORKDIR}/twinraid-ca.crt"
expect_equal "CA certificate download" \
  "$(curl -s -o "${ca_download}" -w '%{http_code} %{content_type}' --cacert "${CERT}" -b "${JAR}" "${BASE}/api/tls/ca-certificate")" \
  "200 application/octet-stream"
expect_equal "downloaded CA is the served CA (DER)" \
  "$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).digest().hex(":").upper())' "${ca_download}")" "${ca_fp}"
events="$(curl -s -N --max-time 4 --cacert "${CERT}" -b "${JAR}" "${BASE}/api/events" || true)"
expect_contains "live stream delivers the status" "${events}" "event: status"
expect_contains "simulated copy job can be switched on" "$(docker exec "${main}" twinraid-admin mock-job on 2>&1)" "now running"
job_seen=""
for _ in $(seq 1 10); do
  job_seen="$(https_get -b "${JAR}" "${BASE}/api/status")"
  [[ "${job_seen}" == *'"job_running":true'* ]] && break
  sleep 1
done
expect_contains "status reports the running copy job" "${job_seen}" '"job_running":true'
docker exec "${main}" twinraid-admin mock-job off >/dev/null
expect_equal "logout without CSRF token is rejected" \
  "$(status_of -b "${JAR}" -H "${ORIGIN}" -X POST "${BASE}/api/auth/logout")" "403"
expect_equal "logout with CSRF token" \
  "$(status_of -b "${JAR}" -c "${JAR}" -H "${ORIGIN}" -H "X-CSRF-Token: ${csrf}" -X POST "${BASE}/api/auth/logout")" "200"
expect_equal "session is gone after logout" "$(status_of -b "${JAR}" "${BASE}/api/status")" "401"

# --- 9. Host-Prüfung, Größenlimit, Security-Header ----------------------------------------
expect_equal "foreign Host header is rejected" "$(status_of -H "Host: evil.example" "${BASE}/health")" "400"
head -c 17000 /dev/zero | tr '\0' 'x' >"${WORKDIR}/big.json"
expect_equal "requests over 16 KiB are rejected" \
  "$(status_of -H "${ORIGIN}" -H "${JSON}" --data-binary "@${WORKDIR}/big.json" "${BASE}/api/auth/login")" "413"
for request in "${BASE}/health" "${BASE}/api/status" "-H Host:evil.example ${BASE}/health"; do
  # shellcheck disable=SC2086
  headers="$(headers_of ${request})"
  expect_contains "CSP on ${request}" "${headers,,}" "content-security-policy: default-src 'none'; script-src 'self'"
  expect_contains "X-Frame-Options on ${request}" "${headers,,}" "x-frame-options: deny"
  expect_contains "Referrer-Policy on ${request}" "${headers,,}" "referrer-policy: no-referrer"
  expect_contains "nosniff on ${request}" "${headers,,}" "x-content-type-options: nosniff"
done

# --- 10. Sperre nach 5 Fehlversuchen und Notfall-Entsperrung -------------------------------
for _ in 1 2 3 4 5; do
  login_status "${WRONG_PASSWORD}" >/dev/null
done
expect_equal "IP is locked after 5 failures (even with the right password)" "$(login_status "${PASSWORD}")" "429"
expect_contains "unlock via admin CLI" "$(docker exec "${main}" twinraid-admin unlock 2>&1)" "have been cleared"
expect_equal "login works after unlock" "$(login_status "${PASSWORD}")" "200"
expect_contains "unlock reset the worker throttling" "$(docker logs "${main}" 2>&1)" "Login throttling in the worker was reset"

# --- 11. Keine Secrets in Logs oder Datenbank ----------------------------------------------
logs="$(docker logs "${main}" 2>&1)"
expect_not_contains "log does not contain the password" "${logs}" "${PASSWORD}"
expect_not_contains "log does not contain the wrong password" "${logs}" "${WRONG_PASSWORD}"
expect_not_contains "log does not contain the CSRF token" "${logs}" "${csrf}"
scan='import pathlib,sys
data=b"".join(p.read_bytes() for p in pathlib.Path("/data/web").iterdir() if p.is_file())
print(any(s.encode() in data for s in sys.argv[1:]))'
expect_equal "web database contains no password" "$(docker exec "${main}" "${PY}" -c "${scan}" "${PASSWORD}" "${WRONG_PASSWORD}")" "False"

# --- 12. Neustart und renew-cert --------------------------------------------------------------
docker restart "${main}" >/dev/null
if wait_for_https "${main}"; then pass "container restarts"; else fail "container did not restart"; fi
expect_equal "certificate stays the same after restart" "$(served_fingerprint)" "${first_fp}"
expect_equal "password survives the restart" "$(login_status "${PASSWORD}")" "200"
wait_for_fingerprint() {
  local wanted="$1" served=""
  for _ in $(seq 1 20); do
    served="$(served_fingerprint 2>/dev/null || true)"
    [[ "${served}" == "${wanted}" ]] && break
    sleep 1
  done
  echo "${served}"
}
server_fp_of() { grep -A1 '^Server certificate' <<<"$1" | grep -o 'SHA-256 fingerprint: [0-9A-F:]*' | awk '{print $NF}'; }
renew="$(docker exec "${main}" twinraid-admin renew-cert 2>&1)"
new_fp="$(server_fp_of "${renew}")"
expect_equal "renew-cert serves the new server certificate" "$(wait_for_fingerprint "${new_fp}")" "${new_fp}"
if [[ -n "${new_fp}" && "${new_fp}" != "${first_fp}" ]]; then pass "renew-cert changed the server fingerprint"; else fail "server fingerprint unchanged"; fi
expect_equal "devices with the CA need no change after renew-cert" "$(https_get "${BASE}/health")" "ok"
renew_ca="$(docker exec "${main}" twinraid-admin renew-cert --new-ca 2>&1)"
expect_contains "renew-cert --new-ca asks to reinstall the CA" "${renew_ca}" "Install the new CA certificate on all devices"
newest_fp="$(server_fp_of "${renew_ca}")"
wait_for_fingerprint "${newest_fp}" >/dev/null
expect_equal "the old CA no longer verifies after --new-ca" "$(curl -s --max-time 5 --cacert "${CERT}" "${BASE}/health" || echo rejected)" "rejected"
docker exec "${main}" cat /run/twinraid-tls/ca.pem >"${CERT}"
expect_equal "HTTPS works with the new CA" "$(https_get "${BASE}/health")" "ok"

# --- 13. Sauberes Stoppen ------------------------------------------------------------------------
started=$(date +%s)
docker stop -t 10 "${main}" >/dev/null
duration=$(($(date +%s) - started))
code="$(exit_code "${main}")"
if [[ "${code}" == "0" && "${duration}" -lt 10 ]]; then
  pass "clean stop in ${duration}s (exit 0)"
else
  fail "stop took ${duration}s, exit ${code}"
fi

# --- 14. Netzfilter: fremdes Netz bekommt 403 --------------------------------------------------
foreign_config="${WORKDIR}/foreign.yaml"
grep -v '172.16.0.0/12' "${CANARY_CONFIG}" >"${foreign_config}"
chmod 0644 "${foreign_config}"
foreign="${PREFIX}-foreign"
start "${foreign}" "" "${foreign_config}" -e TWINRAID_MODE=mock -p "127.0.0.1:$((PORT + 1)):8443"
answer=""
for _ in $(seq 1 40); do
  answer="$(curl -sk --max-time 2 -o /dev/null -w '%{http_code}' "https://127.0.0.1:$((PORT + 1))/health" || true)"
  [[ "${answer}" == "403" ]] && break
  sleep 1
done
expect_equal "client outside allowed_networks gets 403 even for /health" "${answer}" "403"
docker rm -f "${foreign}" >/dev/null

# --- 15. Kein Start bei fehlerhaftem Modus, Konfiguration oder /data ------------------------------
bad_config="${WORKDIR}/bad.yaml"
sed 's/- medien/- system/' "${CONFIG}" >"${bad_config}"
chmod 0644 "${bad_config}"

refuse() {
  local label="$1" expected="$2" config="$3"
  shift 3
  local name="${PREFIX}-refuse-$((RANDOM))"
  start "${name}" "" "${config}" "$@"
  if wait_for_exit "${name}" && [[ "$(exit_code "${name}")" != "0" ]] \
    && docker logs "${name}" 2>&1 | grep -q "${expected}"; then
    pass "refuses to start: ${label}"
  else
    fail "did not refuse: ${label}"
    docker logs "${name}" 2>&1 | tail -10
  fi
  docker rm -f "${name}" >/dev/null
}
refuse "TWINRAID_MODE missing" "TWINRAID_MODE is not set" "${CONFIG}"
refuse "TWINRAID_MODE invalid" "invalid value" "${CONFIG}" -e TWINRAID_MODE=produktion
refuse "forbidden share in config" "must never be synced" "${bad_config}" -e TWINRAID_MODE=mock
# Startfehler des Workers: Container hält an, statt den Dienst endlos neu zu starten.
refuse "worker startup error (web UID 0)" "must not be 0" "${CONFIG}" -e TWINRAID_MODE=mock -e TWINRAID_WEB_UID=0
# /data für alle beschreibbar: unsicher, Start wird mit Reparaturhinweis verweigert.
NO_VOLUME=1 refuse "/data writable by others" "chown root:root" "${CONFIG}" -e TWINRAID_MODE=mock \
  --tmpfs /data:rw,mode=1777

# --- 16. Modus real nur lesend (Konzept 6.4) ----------------------------------------------------
# Server-Adressen aus TEST-NET-1 (RFC 5737): Sie werden nie geroutet, es wird also
# kein echter Server angesprochen. Groß per http, damit der Hinweis erscheint.
real_config="${WORKDIR}/real.yaml"
python3 - "${CANARY_CONFIG}" "${real_config}" <<'PY'
import sys

text = open(sys.argv[1], encoding="utf-8").read()
text = text.replace("10.77.31.41", "192.0.2.41").replace("10.77.31.42", "192.0.2.42")
text = text.replace("      scheme: https\n      port: 4443\n", "      scheme: http\n")
open(sys.argv[2], "w", encoding="utf-8").write(text)
PY
chmod 0644 "${real_config}"
real="${PREFIX}-real"
start "${real}" "" "${real_config}" -e TWINRAID_MODE=real -p "127.0.0.1:$((PORT + 2)):8443"
REAL_BASE="https://127.0.0.1:$((PORT + 2))"
if wait_for_https "${real}" "$((PORT + 2))"; then
  pass "container starts with TWINRAID_MODE=real"
  real_logs="$(docker logs "${real}" 2>&1)"
  expect_contains "real mode is announced as read-only" "${real_logs}" "Read-only real mode"
  expect_contains "start log warns about the unencrypted API connection" "${real_logs}" "is unencrypted"
  expect_not_contains "real mode shows no simulation banner" "${real_logs}" "SIMULATION MODE"
  docker exec -i "${real}" twinraid-admin set-password --stdin <<<"${PASSWORD}" >/dev/null
  real_key="SmokeTestKey_0123456789"
  key_out="$(docker exec -i "${real}" twinraid-admin set-api-key target --stdin <<<"${real_key}" 2>&1 || true)"
  expect_contains "set-api-key stores the key" "${key_out}" "saved"
  expect_contains "set-api-key explains an unreachable target" "${key_out}" "is not reachable"
  real_jar="${WORKDIR}/real-cookies.txt"
  real_cert="${WORKDIR}/real-ca.pem"
  docker exec "${real}" cat /run/twinraid-tls/ca.pem >"${real_cert}"
  curl -s -o /dev/null -c "${real_jar}" --cacert "${real_cert}" -H "Origin: ${REAL_BASE}" -H "${JSON}" \
    -d "{\"password\": \"${PASSWORD}\"}" "${REAL_BASE}/api/auth/login"
  real_status=""
  for _ in $(seq 1 20); do
    real_status="$(curl -s --cacert "${real_cert}" -b "${real_jar}" "${REAL_BASE}/api/status")"
    [[ "${real_status}" == *'"target_offline"'* ]] && break
    sleep 1
  done
  expect_contains "unreachable target is reported as offline" "${real_status}" '"target":{"state":"offline","reason":"unreachable"}'
  expect_contains "VM is off because the target is offline" "${real_status}" '"vm":{"state":"offline","reason":"target_offline"}'
  expect_contains "info reports read-only mode" \
    "$(curl -s --cacert "${real_cert}" -b "${real_jar}" "${REAL_BASE}/api/info")" '"read_only":true'
  for command in wake_target shutdown_target vm_start vm_stop vm_force_stop sync_start; do
    action='import socket,sys;s=socket.socket(socket.AF_UNIX);s.connect("/run/twinraid/worker.sock");s.sendall(("{\"command\":\""+sys.argv[1]+"\"}\n").encode());print(s.recv(300).decode())'
    expect_contains "real mode locks ${command}" \
      "$(docker exec -u 10001:10001 "${real}" "${PY}" -c "${action}" "${command}" 2>&1)" '"error":"not_available"'
  done
  expect_contains "mock-job is refused in real mode" "$(docker exec "${real}" twinraid-admin mock-job on 2>&1 || true)" "only works in simulation mode"
  expect_not_contains "the API key never appears in the log" "$(docker logs "${real}" 2>&1)" "${real_key}"
else
  fail "container did not start with TWINRAID_MODE=real"
  docker logs "${real}" 2>&1 | tail -20
fi
docker rm -f "${real}" >/dev/null

# --- 17. Optional: Jede Capability ist nötig ------------------------------------------------------
if [[ "${CHECK_EACH_CAP:-0}" == "1" ]]; then
  for cap in SETUID SETGID SETPCAP; do
    name="${PREFIX}-no-${cap,,}"
    start "${name}" "${cap}" "${CONFIG}" -e TWINRAID_MODE=mock
    if wait_for_exit "${name}" && [[ "$(exit_code "${name}")" != "0" ]]; then
      pass "${cap} is required (start fails without it)"
    else
      fail "${cap} seems unnecessary"
    fi
    docker rm -f "${name}" >/dev/null
  done
  name="${PREFIX}-no-kill"
  start "${name}" KILL "${CONFIG}" -e TWINRAID_MODE=mock
  sleep 6
  docker stop -t 3 "${name}" >/dev/null
  if [[ "$(exit_code "${name}")" == "137" ]]; then
    pass "KILL is required (clean stop fails without it)"
  else
    fail "KILL seems unnecessary (exit $(exit_code "${name}"))"
  fi
  docker rm -f "${name}" >/dev/null
  echo "info  DAC_READ_SEARCH is not checked: the worker reads no shares before phase 5"
fi

echo
if [[ "${failures}" -gt 0 ]]; then
  echo "${failures} check(s) failed." >&2
  exit 1
fi
echo "All checks passed."
