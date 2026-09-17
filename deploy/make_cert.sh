#!/bin/sh
# Self-signed certificate so phones can share GPS with the hub (browsers only
# allow location on https). Usage: sh make_cert.sh 192.168.4.1
IP=${1:?give the hub IP address}
OUT=${2:-/etc/sarsense}
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout "$OUT/key.pem" -out "$OUT/cert.pem" \
  -subj "/CN=sarsense" -addext "subjectAltName=IP:$IP"
chmod 640 "$OUT/key.pem"
chgrp sarsense "$OUT/key.pem" 2>/dev/null || true
echo "Add to sarsense.json:  \"tls_cert\": \"$OUT/cert.pem\", \"tls_key\": \"$OUT/key.pem\""
