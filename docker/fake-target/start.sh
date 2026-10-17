#!/bin/sh
# Startet das nachgebaute Groß: SSH wie auf Unraid 7.3, Ein/Aus, Wake-on-LAN, Syslog.
set -eu
mkdir -p /state /run/sshd /boot/config/ssh/root /boot/config/twinraid /boot/config/plugins \
  /var/log/plugins /var/local/emhttp
chmod 700 /boot/config/ssh/root
touch /boot/config/ssh/root/authorized_keys
chmod 600 /boot/config/ssh/root/authorized_keys
# Unraid ab 6.9: /root/.ssh zeigt auf die Ablage auf dem USB-Stick.
rm -rf /root/.ssh
ln -s /boot/config/ssh/root /root/.ssh
ssh-keygen -A
# Die Gaming-VM und eine zweite VM, beide aus. power.conf legt der Test an.
printf '[{"uuid": "%s", "name": "%s", "state": "shut off"},' "${FAKE_VM_UUID}" "${FAKE_VM_NAME}" >/state/vms.json
printf ' {"uuid": "7c6b5a49-3827-4161-9504-f3e2d1c0b9a8", "name": "Second VM", "state": "shut off"}]\n' >>/state/vms.json
python3 /opt/fake/fake_target.py &
exec /usr/sbin/sshd -D -e
