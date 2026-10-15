# syntax=docker/dockerfile:1
#
# Nachgebautes Unraid Groß für docker/ssh-test.sh (Konzept 5.6). Nur im Test,
# nie im Laufzeit-Image. ssh-test.sh übergibt das per Digest gepinnte Basis-Image
# aus docker/Dockerfile (BASE_IMAGE) und dieselbe openssh-Version wie dort
# (OPENSSH_VERSION). So gibt es beide Werte nur an einer Stelle.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG OPENSSH_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends "openssh-server=${OPENSSH_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

COPY sshd-test.conf /etc/ssh/sshd_config.d/twinraid-test.conf
COPY fake_unraid.py start.sh /opt/fake/
# Platzhalter: schreiben nur mit, was guard.sh aufruft.
COPY --chmod=0755 poweroff /sbin/poweroff
COPY --chmod=0755 virsh /usr/bin/virsh

ENTRYPOINT ["/bin/sh", "/opt/fake/start.sh"]
