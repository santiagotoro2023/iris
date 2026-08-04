#!/bin/sh
# Serve HTTPS when a certificate is mounted, plain HTTP when it is not.
#
# The microphone and geolocation both need a secure context, so over anything but
# localhost the browser refuses them without TLS. A self-signed certificate is
# enough for the browser to call it secure once you have accepted it.
set -e
CERT=/tls/iris.crt
KEY=/tls/iris.key

if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  echo "[api] serving https on 8000"
  exec uvicorn main:app --host 0.0.0.0 --port 8000 \
       --ssl-certfile "$CERT" --ssl-keyfile "$KEY"
fi
echo "[api] no certificate at $CERT, serving plain http on 8000"
exec uvicorn main:app --host 0.0.0.0 --port 8000
