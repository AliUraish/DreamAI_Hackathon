#!/usr/bin/env bash
# Env-sensing demo against a running backend (:8000).
# Edits demo/customer-app/.env (fake keys only): ANTHROPIC_API_KEY -> OPENAI_API_KEY with a new value.
# Chowkidaar's watcher notices by itself, raises a question, and migrates only after the answer.
#   ./demo/run_env_demo.sh            sense -> ask -> confirm -> migrate
#   ./demo/run_env_demo.sh dismiss    sense -> ask -> dismiss (nothing runs)
set -euo pipefail
API=${API:-http://localhost:8000}
DECISION=${1:-confirm}
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
pause() { python3 -c "import time; time.sleep($1)"; }

echo "1. connect the demo project (baseline: ANTHROPIC_API_KEY)"
CONNECTED=$(curl -s -X POST $API/api/demo/connect)
REPO=$(echo "$CONNECTED" | json "d['id']"); APP=$(echo "$CONNECTED" | json "d['local_path']")
printf 'ORDERS_API_URL=http://localhost:4010\nANTHROPIC_API_KEY=demo-anthropic-value\n' > "$APP/.env"
curl -s -X POST "$API/api/repos/$REPO/env/check" >/dev/null; pause 3
curl -s $API/api/repos/$REPO/env | json "'\n'.join('   %-20s %-12s fingerprint=%s' % (v['name'], v['provider'], v['fingerprint']) for v in d['variables'])"

echo "2. developer edits .env: ANTHROPIC_API_KEY -> OPENAI_API_KEY (new name, new value)"
printf 'ORDERS_API_URL=http://localhost:4010\nOPENAI_API_KEY=demo-openai-value\n' > "$APP/.env"

echo "3. waiting for the watcher to notice (no API call made)"
for _ in $(seq 1 10); do
  CHANGE=$(curl -s "$API/api/env-changes?repo_id=$REPO" | json "d[0]['id'] if d else ''"); [ -n "$CHANGE" ] && break; pause 2
done
[ -n "$CHANGE" ] || { echo "   not detected"; exit 1; }
curl -s "$API/api/env-changes?repo_id=$REPO" | json "'   Q: ' + d[0]['question']['title'] + '\n      ' + d[0]['question']['body']"
echo "   migrations started so far: $(curl -s "$API/api/migrations?repo_id=$REPO" | json "len(d)")"

echo "4. user answers: $DECISION"
curl -s -X POST "$API/api/env-changes/$CHANGE/$DECISION" >/dev/null
if [ "$DECISION" = confirm ]; then
  for _ in $(seq 1 100); do
    STATUS=$(curl -s "$API/api/migrations?repo_id=$REPO" | json "d[0]['status'] if d else 'none'")
    case $STATUS in queued|running|none) pause 3;; *) break;; esac
  done
fi
echo; echo "=== activity feed"
curl -s "$API/api/events?repo_id=$REPO&limit=100" | json "'\n'.join(e['ts'][11:19]+'  '+e['message'] for e in d)"
echo; echo "=== migrations: $(curl -s "$API/api/migrations?repo_id=$REPO" | json "[(m['title'], m['status'], m['kind']) for m in d]")"
# Put the demo back. That edit is itself a provider change, so answer the question it raises.
printf 'ORDERS_API_URL=http://localhost:4010\nANTHROPIC_API_KEY=demo-anthropic-value\n' > "$APP/.env"
for id in $(curl -s -X POST "$API/api/repos/$REPO/env/check" | json "' '.join(c['id'] for c in d)") $(curl -s "$API/api/env-changes?repo_id=$REPO" | json "' '.join(c['id'] for c in d)"); do
  curl -s -X POST "$API/api/env-changes/$id/dismiss" >/dev/null || true
done
