#!/usr/bin/env bash
# Idempotent provisioning script for Agency-OS Cloud Scheduler jobs
set -euo pipefail

# Normalize env variables across deploy.sh and deploy.yml
PROJECT="${GCP_PROJECT:-${PROJECT_ID:-${GCP_PROJECT_ID:-}}}"
LOCATION="${GCP_LOCATION:-${REGION:-}}"
WORKER_SA="${AOS_WORKER_SERVICE_ACCOUNT:-${WORKER_SA:-}}"
APP_URL="${APP_URL:-}"

if [ -z "$PROJECT" ] || [ -z "$LOCATION" ] || [ -z "$WORKER_SA" ] || [ -z "$APP_URL" ]; then
    echo "ERROR: Missing required environment variables for scheduler provisioning!"
    echo "PROJECT: $PROJECT"
    echo "LOCATION: $LOCATION"
    echo "WORKER_SA: $WORKER_SA"
    echo "APP_URL: $APP_URL"
    exit 1
fi

echo "=== Provisioning Cloud Scheduler Jobs ==="
echo "Project: $PROJECT"
echo "Location: $LOCATION"
echo "Worker SA: $WORKER_SA"
echo "App URL: $APP_URL"

upsert_job() {
    local name="$1"
    local schedule="$2"
    local path="$3"
    local description="$4"
    
    local target_uri="${APP_URL}${path}"
    echo "----------------------------------------"
    echo "Job Name:    $name"
    echo "Schedule:    $schedule"
    echo "Target URI:  $target_uri"
    
    if gcloud scheduler jobs describe "$name" --location="$LOCATION" --project="$PROJECT" &>/dev/null; then
        echo "Status:      Existing job found. Updating..."
        gcloud scheduler jobs update http "$name" \
          --schedule="$schedule" \
          --uri="$target_uri" \
          --http-method=POST \
          --description="$description" \
          --oidc-service-account-email="$WORKER_SA" \
          --oidc-token-audience="$target_uri" \
          --location="$LOCATION" \
          --project="$PROJECT" \
          --quiet
    else
        echo "Status:      No existing job. Creating..."
        gcloud scheduler jobs create http "$name" \
          --schedule="$schedule" \
          --uri="$target_uri" \
          --http-method=POST \
          --description="$description" \
          --oidc-service-account-email="$WORKER_SA" \
          --oidc-token-audience="$target_uri" \
          --location="$LOCATION" \
          --project="$PROJECT" \
          --quiet
    fi
    echo "Result:      Success"
}

# Core recurring loops
upsert_job "aos-refresh-tokens" "0 * * * *" "/tasks/refresh-tokens" "Rotate and refresh expiring OAuth connections"
upsert_job "aos-process-cadences" "*/15 * * * *" "/tasks/process-cadences" "Propose recurring audit and optimization Ops from Cadences"
upsert_job "aos-trust-snapshots" "0 * * * *" "/tasks/trust-snapshots" "Capture and record brand trust snapshots"
upsert_job "aos-evaluate-trust" "0 1 * * *" "/tasks/evaluate-trust" "Evaluate campaign ROI and adjust trust tiers"
upsert_job "aos-calibrate-attribution" "0 2 * * *" "/tasks/calibrate-attribution" "Calibrate marketing attribution models"
upsert_job "aos-drain-outbox" "*/5 * * * *" "/tasks/drain-outbox" "Safety-net periodic drain of the transactional outbox"

# Outbox drain — executes APPROVED operations. Safety net for the per-op Cloud
# Tasks trigger: without a periodic drain, an approved Op can stall in APPROVED
# indefinitely (its OutboxItem stays PENDING and never executes).
upsert_job "aos-drain-outbox" "*/2 * * * *" "/tasks/drain-outbox" "Drain the outbox: execute APPROVED operations"

echo "========================================"
echo "=== Scheduler Provisioning Completed Successfully ==="
