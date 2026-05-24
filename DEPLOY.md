# Deploying LEGO Scanner to Google Cloud Run

## Prerequisites

1. **Google Cloud Account** — Sign up at [cloud.google.com](https://cloud.google.com)
2. **Google Cloud CLI** — Install from [cloud.google.com/sdk](https://cloud.google.com/sdk)
3. **Docker** (optional, GCP can build for you)

## Step 1: Create Google Cloud Project

```bash
# Set your project name
export PROJECT_ID="lego-scanner"
export REGION="us-central1"

# Create project
gcloud projects create $PROJECT_ID

# Set as active project
gcloud config set project $PROJECT_ID

# Enable required APIs
gcloud services enable run.googleapis.com
gcloud services enable cloudbuild.googleapis.com
gcloud services enable containerregistry.googleapis.com
```

## Step 2: Authenticate with Google Cloud

```bash
# Login to Google Cloud
gcloud auth login

# Set default region
gcloud config set run/region $REGION
```

## Step 3: Deploy to Cloud Run

### Option A: Deploy directly from source (easiest)

```bash
cd /Users/jef/Claude/Lego

gcloud run deploy lego-scanner \
  --source . \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars \
    REBRICKABLE_API_KEY=your_key_here,\
    REBRICKABLE_USER_TOKEN=your_token_here,\
    BL_CONSUMER_KEY=your_key_here,\
    BL_CONSUMER_SECRET=your_secret_here,\
    BL_TOKEN=your_token_here,\
    BL_TOKEN_SECRET=your_secret_here
```

### Option B: Deploy from GitHub (recommended for updates)

1. **Push to GitHub:**
   ```bash
   git remote add origin https://github.com/YOUR_USERNAME/lego-scanner.git
   git branch -M main
   git push -u origin main
   ```

2. **Connect to Cloud Run in Console:**
   - Go to [Cloud Run Console](https://console.cloud.google.com/run)
   - Click "Create Service"
   - Choose "Deploy from source repository"
   - Connect GitHub, select repo
   - Set build settings:
     - Runtime: Python
     - Build type: Dockerfile
   - Set environment variables (next step)

## Step 4: Set Environment Variables

**Via Cloud Console:**
1. Open Cloud Run service
2. Click "Edit & Deploy New Revision"
3. Under "Runtime settings" → "Environment variables"
4. Add:
   - `REBRICKABLE_API_KEY`
   - `REBRICKABLE_USER_TOKEN`
   - `BL_CONSUMER_KEY`
   - `BL_CONSUMER_SECRET`
   - `BL_TOKEN`
   - `BL_TOKEN_SECRET`

**Via CLI:**
```bash
gcloud run services update lego-scanner \
  --update-env-vars \
    REBRICKABLE_API_KEY=your_key,\
    REBRICKABLE_USER_TOKEN=your_token,\
    BL_CONSUMER_KEY=your_key,\
    BL_CONSUMER_SECRET=your_secret,\
    BL_TOKEN=your_token,\
    BL_TOKEN_SECRET=your_secret
```

## Step 5: Get Your Public URL

After deployment:

```bash
gcloud run services describe lego-scanner --region us-central1 --format 'value(status.url)'
```

Your app will be live at the returned URL (e.g., `https://lego-scanner-abc123xyz-uc.a.run.app`)

## Step 6: Test on Mobile

1. Get the public URL from step 5
2. Open on your phone (same network not required anymore!)
3. Test scanning a part

## Updating Your App

### From local machine:
```bash
git push origin main
```

The app will automatically rebuild and redeploy (if using GitHub integration).

### Manual redeploy:
```bash
gcloud run deploy lego-scanner --source . --platform managed
```

## Monitoring & Logs

```bash
# View logs
gcloud run services logs read lego-scanner --limit 50

# View real-time logs
gcloud alpha run services logs tail lego-scanner

# Get service details
gcloud run services describe lego-scanner
```

## Troubleshooting

**Service won't deploy:**
- Check logs: `gcloud run services logs read lego-scanner`
- Verify environment variables are set
- Ensure `Dockerfile` is in root directory

**App crashes after deploy:**
- Check logs for errors
- Verify API keys are correct
- Check port binding (must use 8080)

**Slow response times:**
- Normal for first request (cold start)
- Increase memory allocation in Cloud Console
- Add more workers in gunicorn command

## Costs

- **2,000,000 requests/month** — Free
- **vCPU & Memory** — ~$0.00001667 per vCPU-second
- **50,000 scans** — ~$5/month
- **500,000 scans** — ~$50/month

## Notes

- App is public (no authentication required) — Brickognize and Rebrickable handle security
- Scales automatically based on traffic
- First request may be slow (cold start, ~2-3 seconds)
- Subsequent requests are faster due to caching
