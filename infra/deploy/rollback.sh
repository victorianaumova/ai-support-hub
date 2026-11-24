#!/usr/bin/env bash
# `make rollback` (operations §6, P10-07): send all traffic of hub-api back to the revision
# before the one that serves now. Needs gcloud logged in, GCP_PROJECT_ID and GCP_REGION.
set -euo pipefail
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID}" "${GCP_REGION:?set GCP_REGION}"
service="${SERVICE:-hub-api}"
current="$(gcloud run services describe "$service" --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" --format='value(status.traffic[0].revisionName)')"
previous="$(gcloud run revisions list --service "$service" --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" --sort-by='~metadata.creationTimestamp' --format='value(metadata.name)' \
  | grep -v -x "$current" | head -1)"
if [ -z "$previous" ]; then
  echo "rollback: $service has no revision before $current" >&2
  exit 1
fi
gcloud run services update-traffic "$service" --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" --to-revisions "$previous=100" --quiet
echo "rollback: $service now serves $previous (was $current)"
