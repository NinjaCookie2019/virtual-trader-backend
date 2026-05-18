#!/usr/bin/env bash
set -euo pipefail

SERVICE="${RAILWAY_SERVICE:-virtual-trader-backend}"
ENVIRONMENT="${RAILWAY_ENVIRONMENT:-production}"
BASE_URL="${VIRTUAL_TRADER_BASE_URL:-https://virtual-trader-backend-production.up.railway.app}"
COMMIT="$(git rev-parse HEAD)"

railway whoami >/dev/null

railway variables \
  --service "$SERVICE" \
  --environment "$ENVIRONMENT" \
  --set "APP_COMMIT_SHA=$COMMIT" \
  --skip-deploys >/dev/null

railway up \
  --service "$SERVICE" \
  --environment "$ENVIRONMENT" \
  --detach

echo "Deployed $COMMIT to $SERVICE/$ENVIRONMENT"
echo "Polling $BASE_URL/api/version"

for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  body="$(curl -fsS --max-time 10 "$BASE_URL/api/version" || true)"
  echo "$body"
  if [[ "$body" == *"$COMMIT"* ]]; then
    echo "Verified deployed commit $COMMIT"
    exit 0
  fi
  sleep 10
done

echo "Deployment was uploaded, but /api/version did not report $COMMIT within the polling window." >&2
exit 1
