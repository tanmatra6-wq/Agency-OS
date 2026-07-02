#!/usr/bin/env bash
# Deploy control-plane backend to Cloud Run with CORS configuration.
#
# DEPRECATED: the source of truth for deploys is the GitHub Actions CD
# (.github/workflows/deploy.yml), which runs migrations, a canary + smoke test,
# and the traffic shift. This manual script is kept for break-glass use only and
# is aligned to the same region/instance as the CD (us-central1 / aos-db-us).
# See CUTOVER.md before running it by hand.
set -euo pipefail

GCP_PROJECT_ID="aos-control-plane-tmg"
REGION="us-central1"
BACKEND_SERVICE="agency-os-backend"
FRONTEND_SERVICE="agency-os-web"
WORKER_SA="aos-worker@$GCP_PROJECT_ID.iam.gserviceaccount.com"

echo "=== Starting Agency-OS Backend Deployment ==="
echo "Project ID: $GCP_PROJECT_ID"
echo "Region: $REGION"

# 1. Verify gcloud authentication and project configuration
echo "Checking gcloud configuration..."
CURRENT_PROJECT=$(gcloud config get-value project 2>/dev/null || true)
if [ "$CURRENT_PROJECT" != "$GCP_PROJECT_ID" ]; then
    echo "Setting gcloud project to $GCP_PROJECT_ID..."
    gcloud config set project "$GCP_PROJECT_ID"
fi

# 2. Build and push the Docker container image using Google Cloud Build
echo "Building and pushing container image via Google Cloud Build..."
cp -r recipes control-plane/recipes
trap 'rm -rf control-plane/recipes' EXIT

gcloud builds submit --tag "$REGION-docker.pkg.dev/$GCP_PROJECT_ID/aos-docker/control-plane:latest" --project "$GCP_PROJECT_ID" control-plane/

# 4. Dynamically retrieve the Frontend Console URL for CORS configuration
echo "Retrieving Frontend Console URL..."
FRONTEND_URLS_JSON=$(gcloud run services describe "$FRONTEND_SERVICE" \
  --platform managed \
  --region "$REGION" \
  --format="value(metadata.annotations.\"run.googleapis.com/urls\")" \
  --project "$GCP_PROJECT_ID" 2>/dev/null)

if [ -n "$FRONTEND_URLS_JSON" ] && [ "$FRONTEND_URLS_JSON" != "null" ]; then
    echo "Found Frontend URLs (multiple): $FRONTEND_URLS_JSON"
    # Convert ["url1","url2"] to url1,url2
    FRONTEND_URLS=$(echo "$FRONTEND_URLS_JSON" | tr -d '[]"' | tr '\n' ',' | sed 's/,$//')
    ALLOWED_ORIGINS="$FRONTEND_URLS,http://localhost:3000"
else
    # Fallback to single status.url
    if FRONTEND_URL=$(gcloud run services describe "$FRONTEND_SERVICE" \
      --platform managed \
      --region "$REGION" \
      --format="value(status.url)" \
      --project "$GCP_PROJECT_ID" 2>/dev/null); then
        echo "Found Frontend URL (single): $FRONTEND_URL"
        ALLOWED_ORIGINS="$FRONTEND_URL,http://localhost:3000"
      else
        echo "WARNING: Could not retrieve Frontend Console URL. Using default wildcards."
        ALLOWED_ORIGINS="*"
      fi
fi

# 4.5 Resolve live Backend Service URL dynamically
echo "Resolving Backend Service URL..."
APP_URL=$(gcloud run services describe "$BACKEND_SERVICE" --platform managed --region "$REGION" --format="value(status.url)" --project "$GCP_PROJECT_ID" 2>/dev/null || true)
if [ -z "$APP_URL" ] || [ "$APP_URL" = "null" ]; then
    APP_URL="https://agency-os-backend-730671240713.us-central1.run.app"
fi
echo "Backend Service URL: $APP_URL"

# 5. Redeploy/Update the Cloud Run service with full production environment & Cloud SQL configuration
echo "Updating Cloud Run service $BACKEND_SERVICE..."
gcloud run services update "$BACKEND_SERVICE" \
  --image "$REGION-docker.pkg.dev/$GCP_PROJECT_ID/aos-docker/control-plane:latest" \
  --set-env-vars="^###^ALLOWED_ORIGINS=$ALLOWED_ORIGINS###ENV=production###GCP_PROJECT=$GCP_PROJECT_ID###GCP_LOCATION=$REGION###AOS_STATE_BUCKET=aos-tfstate-tmg###OUTBOX_QUEUE_NAME=aos-outbox###APP_URL=$APP_URL###AOS_WORKER_SERVICE_ACCOUNT=$WORKER_SA" \
  --set-secrets="DATABASE_URL=aos-database-url:latest,WORKER_DATABASE_URL=aos-worker-database-url:latest,OPERATOR_TOKEN=aos-operator-token:latest,WHATSAPP_APP_SECRET=whatsapp-app-secret:latest,WHATSAPP_VERIFY_TOKEN=whatsapp-verify-token:latest,SECRET_KEY=aos-oauth-state-secret:latest" \
  --add-cloudsql-instances="aos-control-plane-tmg:us-central1:aos-db-us" \
  --memory 2Gi \
  --region "$REGION" \
  --project "$GCP_PROJECT_ID"

# 6. Provision Cloud Scheduler Jobs
echo "Provisioning Cloud Scheduler Jobs..."
APP_URL="$APP_URL" WORKER_SA="$WORKER_SA" GCP_PROJECT_ID="$GCP_PROJECT_ID" REGION="$REGION" ./control-plane/scripts/provision_scheduler.sh

echo "=== Deployment Successfully Completed! ==="
echo "Backend is now live at: $APP_URL"
