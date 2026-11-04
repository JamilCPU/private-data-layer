#!/usr/bin/env bash
# Make a lab CA (once) and a server certificate for a *.lab ingress host, then print the kubectl
# command that stores it as the TLS Secret an Ingress references. Nothing here leaves the machine.
#
#   bash make-lab-tls.sh gateway.lab gateway-lab-tls [out-dir]
#
# Install out-dir/lab-ca.pem on each client (browser/OS trust store; for Node-based clients such
# as Claude Code set NODE_EXTRA_CA_CERTS=/path/to/lab-ca.pem).
set -euo pipefail
host="${1:?host, e.g. gateway.lab}"
secret="${2:?secret name, e.g. gateway-lab-tls}"
out="${3:-./lab-tls}"
mkdir -p "$out" && chmod 700 "$out"
if [[ ! -f "$out/lab-ca.key" ]]; then
  openssl req -x509 -newkey rsa:4096 -nodes -days 3650 -subj "/CN=lab CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$out/lab-ca.key" -out "$out/lab-ca.pem" >/dev/null 2>&1
  echo "created CA: $out/lab-ca.pem (keep $out/lab-ca.key offline)"
fi
openssl req -newkey rsa:2048 -nodes -subj "/CN=$host" \
  -keyout "$out/$host.key" -out "$out/$host.csr" >/dev/null 2>&1
printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n' "$host" > "$out/$host.ext"
openssl x509 -req -in "$out/$host.csr" -CA "$out/lab-ca.pem" -CAkey "$out/lab-ca.key" \
  -CAcreateserial -days 825 -extfile "$out/$host.ext" -out "$out/$host.crt" >/dev/null 2>&1
rm -f "${out:?}/${host:?}.csr" "${out:?}/${host:?}.ext"
echo "certificate: $out/$host.crt"
echo "store it with:"
echo "  kubectl -n lab create secret tls $secret --cert=$out/$host.crt --key=$out/$host.key"
