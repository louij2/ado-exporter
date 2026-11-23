#!/bin/bash
# Build, publish and redeploy ado-exporter on TOWER, from the Unraid template
# (/boot/config/plugins/dockerMan/templates-user/my-ado-exporter.xml), never compose.
# The image goes to Docker Hub first, so tower never holds the only copy.
# Run on tower: /mnt/user/appdata/claude-code/workspace/repos/ado-exporter/deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
IMG=louij2/ado-exporter
TAG=$(date +%Y%m%d-%H%M)
C=ado-exporter
TOOLS=/mnt/user/appdata/compose-to-templates

docker run --rm -v "$PWD":/src:ro -w /tmp python:3.13-slim sh -c 'cp /src/*.py . && python3 test_exporter.py' >/dev/null
docker build -t "$IMG:latest" -t "$IMG:$TAG" .
docker push "$IMG:$TAG"
docker push "$IMG:latest"

"$TOOLS/tpldiff.sh" "$C" || true
"$TOOLS/tpl-recreate.sh" "$C"
# No HEALTHCHECK in the image (it would poll ADO on a timer), so check a real scrape.
# The first scrape after a restart can take ~60 s on a cold cache.
for _ in $(seq 12); do
  curl -sf -m 90 http://127.0.0.1:9823/metrics | grep -q '^ado_flow_read_ok 1' && break
  sleep 10
done
if curl -sf -m 90 http://127.0.0.1:9823/metrics | grep -q '^ado_flow_read_ok 1'; then
  "$TOOLS/tpl-recreate.sh" --commit "$C"
  echo "deployed $IMG:$TAG"
else
  docker logs --tail 20 "$C" >&2
  echo "NOT serving flow metrics. Roll back: docker rm -f $C; docker rename $C-old $C; docker start $C" >&2
  exit 1
fi
