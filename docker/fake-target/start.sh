#!/bin/sh
# Startet das nachgebaute Groß: SSH wie auf Unraid 7.3, API und Wake-on-LAN.
set -eu
mkdir -p /state /run/sshd /boot/config/ssh/root /boot/config/twinraid
chmod 700 /boot/config/ssh/root
touch /boot/config/ssh/root/authorized_keys
chmod 600 /boot/config/ssh/root/authorized_keys
# Unraid ab 6.9: /root/.ssh zeigt auf die Ablage auf dem USB-Stick.
rm -rf /root/.ssh
ln -s /boot/config/ssh/root /root/.ssh
ssh-keygen -A
echo SHUTOFF >/state/vm-state
python3 /opt/fake/fake_unraid.py &
exec /usr/sbin/sshd -D -e
