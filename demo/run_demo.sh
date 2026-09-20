#!/usr/bin/env bash
# End-to-end demo against a running backend (:8000) and mock provider (:4010).
#   ./demo/run_demo.sh            provider release webhook triggers the migration
#   ./demo/run_demo.sh poll       Chowkidaar notices the change itself on its next poll
set -euo pipefail
API=${API:-http://localhost:8000}; PROVIDER=${PROVIDER:-http://localhost:4010}
MODE=${1:-webhook}
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "1. reset provider to v1"; curl -s -X POST $PROVIDER/admin/reset >/dev/null
echo "2. connect the demo project (its git repository lives under the data directory, not in this repo)"
# Only the demo project is ever reset. Other connected projects are never touched.
for id in $(curl -s $API/api/repos | json "' '.join(r['id'] for r in d if '/demo/customer-app' in r['local_path'])"); do curl -s -X DELETE $API/api/repos/$id; done
REPO=$(curl -s -X POST $API/api/demo/connect | json "d['id']")
for _ in $(seq 1 30); do INT=$(curl -s $API/api/repos/$REPO | json "next((i['id'] for i in d['integrations'] if i['provider'] == 'acme-orders'), '')"); [ -n "$INT" ] && break; python3 -c "import time; time.sleep(1)"; done
echo "   integration: $INT"
if [ "$MODE" = poll ]; then
  echo "3. provider ships v2 silently; Chowkidaar polls"; curl -s -X POST $PROVIDER/admin/release -H 'content-type: application/json' -d '{"mode":"sunset"}' >/dev/null
  curl -s -X POST $API/api/integrations/$INT/check >/dev/null
else
  echo "3. provider ships v2 and announces it"; curl -s -X POST $PROVIDER/admin/release -H 'content-type: application/json' -d "{\"mode\":\"sunset\",\"notify_url\":\"$API/api/webhooks/provider-release\"}" >/dev/null
fi
echo "4. waiting for the migration"
for _ in $(seq 1 100); do
  STATUS=$(curl -s "$API/api/migrations?integration_id=$INT" | json "d[0]['status'] if d else 'none'")
  case $STATUS in queued|running|none) python3 -c "import time; time.sleep(3)";; *) break;; esac
done
echo; echo "=== activity feed"
curl -s "$API/api/events?limit=100" | json "'\n'.join(e['ts'][11:19]+'  '+e['message'] for e in d)"
echo; echo "=== migration"
curl -s "$API/api/migrations?integration_id=$INT" | json "json.dumps(d[0], indent=2)"
