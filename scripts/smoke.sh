#!/usr/bin/env bash
# Early validation spike:
#   1. RustFS accepts an S3 write + read (the pairing the LGTM stack depends on).
#   2. Each backend reports ready.
# Run via `make smoke`.
set -euo pipefail

PROFILE="${1:-otel-101}"
NS="${2:-observability}"

echo "== 1/2  RustFS S3 write/read =="
AK="$(kubectl -n "$NS" get secret s3-credentials -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' | base64 -d)"
SK="$(kubectl -n "$NS" get secret s3-credentials -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' | base64 -d)"

kubectl -n "$NS" run smoke-mc --rm -i --restart=Never --image=minio/mc:latest \
  --env="AK=$AK" --env="SK=$SK" -- /bin/sh -c '
    set -e
    mc alias set r http://rustfs-svc.observability.svc.cluster.local:9000 "$AK" "$SK" >/dev/null
    echo "hello-otel-101" > /tmp/smoke.txt
    mc mb -p r/smoke >/dev/null 2>&1 || true
    mc cp /tmp/smoke.txt r/smoke/smoke.txt >/dev/null
    OUT=$(mc cat r/smoke/smoke.txt)
    mc rm r/smoke/smoke.txt >/dev/null 2>&1 || true
    [ "$OUT" = "hello-otel-101" ] && echo "  OK: RustFS S3 round-trip works" || { echo "  FAIL: got [$OUT]"; exit 1; }
    echo "  buckets:"; mc ls r
  '

echo ""
echo "== 2/2  Backend readiness =="
kubectl -n "$NS" run smoke-curl --rm -i --restart=Never --image=curlimages/curl:latest -- /bin/sh -c '
  for target in "mimir:8080/ready" "loki:3100/ready" "tempo:3200/ready"; do
    name=${target%%:*}
    if curl -sf "http://${target}" >/dev/null 2>&1; then
      echo "  OK: $name ready"
    else
      echo "  WARN: $name not ready yet (http://$target)"
    fi
  done
'
echo ""
echo "Smoke test done."
