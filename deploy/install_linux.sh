#!/bin/sh
# Install the SARSense hub on Debian, Devuan, Armbian or Raspberry Pi OS.
# Run from the unpacked project folder:  sudo sh deploy/install_linux.sh
set -e
SRC=$(cd "$(dirname "$0")/.." && pwd)

apt-get update
apt-get install -y python3 python3-numpy openssl

id sarsense >/dev/null 2>&1 || useradd --system --home /var/lib/sarsense --shell /usr/sbin/nologin sarsense
mkdir -p /opt/sarsense /etc/sarsense /var/lib/sarsense
cp -r "$SRC/sarsense" "$SRC/tools" /opt/sarsense/
[ -f /etc/sarsense/sarsense.json ] || cp "$SRC/deploy/sarsense.json" /etc/sarsense/sarsense.json
chown -R sarsense /var/lib/sarsense
chmod 640 /etc/sarsense/sarsense.json
chgrp sarsense /etc/sarsense/sarsense.json

if [ -d /run/systemd/system ]; then
    cp "$SRC/deploy/sarsense.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable sarsense
    echo "Edit /etc/sarsense/sarsense.json (set the PIN), then: systemctl start sarsense"
else
    cp "$SRC/deploy/sarsense.init" /etc/init.d/sarsense
    chmod 755 /etc/init.d/sarsense
    update-rc.d sarsense defaults
    echo "Edit /etc/sarsense/sarsense.json (set the PIN), then: service sarsense start"
fi
