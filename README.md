# ChirpChirp Media Attribution Service

A microservice for automatically identifying bird species in images using Google's SpeciesNet (Camera Trap AI) with intelligent fallback and geofencing.

## Features

- 🦅 **SpeciesNet AI Classification** - State-of-the-art bird species identification using Google's Camera Trap AI
- 🗺️ **Geofencing** - Automatically filters results to species found in Long Island, New York
- 🤖 **OpenAI Fallback** - Intelligent fallback to GPT-4 Vision when SpeciesNet returns generic "Bird" classifications
- 📦 **Batch Processing** - Efficiently processes images in configurable batches
- 🔄 **Continuous Mode** - Process all unattributed images automatically
- ⚡ **Smart Retry Logic** - Automatically retries generic classifications up to 5 times
- 🎯 **Confidence Filtering** - Only stores predictions above configurable confidence threshold
- 🚫 **Blocklist Filtering** - Automatically filters out non-bird detections (humans, vehicles, etc.)

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

Or using a virtual environment (recommended):

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**Note**: The `speciesnet` package requires additional setup. See the [SpeciesNet Installation](#speciesnet-installation) section below.

### 2. Install SpeciesNet

SpeciesNet is a Google Research project for camera trap image classification. You'll need to install it separately:

```bash
# Clone the SpeciesNet repository
git clone https://github.com/google-research/speciesnet.git
cd speciesnet
pip install -e .
```

Or install from PyPI if available:

```bash
pip install speciesnet
```

Make sure the `speciesnet` command is available in your PATH. You can verify with:

```bash
python -m speciesnet.scripts.run_model --help
```

### 3. Configure Environment Variables

Create a `.env` file in the service root directory:

```bash
# Required
SUPABASE_URL=your_supabase_project_url
SUPABASE_ANON_KEY=your_supabase_anonymous_key

# Optional (with defaults)
BATCH_SIZE=50                    # Number of images to process per batch (default: 50)
THRESHOLD=0.30                   # Minimum confidence score (default: 0.30)

# Optional - OpenAI fallback
OPENAI_API_KEY=your_openai_api_key  # Required for fallback classification
```

### 4. Set Up Database Tables

Create the `attributions` table in your Supabase database:

```sql
CREATE TABLE attributions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  image_id UUID NOT NULL REFERENCES images(id),
  model_version TEXT NOT NULL DEFAULT 'speciesnet-ensemble',
  species TEXT NOT NULL,
  confidence FLOAT NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  extra JSONB,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
  UNIQUE(image_id, species, model_version)
);

-- Add indexes for faster queries
CREATE INDEX idx_attributions_image_id ON attributions(image_id);
CREATE INDEX idx_attributions_species ON attributions(species);
CREATE INDEX idx_attributions_confidence ON attributions(confidence);
```

Ensure you also have an `images` table with at least:

- `id` (UUID)
- `image_url` (TEXT)
- `taken_on` (TIMESTAMP)

## Usage

The service can be used in two ways: as a FastAPI HTTP server (recommended for production) or as a CLI script.

### FastAPI HTTP Server (Recommended)

Start the FastAPI server:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Or with auto-reload for development:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The server will be available at `http://localhost:8000`.

#### API Endpoints

**GET `/run-analysis`**

Run bird species attribution analysis on unattributed images.

**Query Parameters:**

- `continuous` (bool, optional): Process all unattributed images in batches until none remain. Default: `false`
- `batch_size` (int, optional): Override `BATCH_SIZE` from env. Default: uses `BATCH_SIZE` from environment or 50

**Response:**

```json
{
  "success": true,
  "images_processed": 50,
  "attributions_created": 120,
  "message": "Processed 50 images, created 120 attributions"
}
```

**Example Requests:**

```bash
# Single batch mode
curl "http://localhost:8000/run-analysis"

# With custom batch size
curl "http://localhost:8000/run-analysis?batch_size=100"

# Continuous mode (process all unattributed images)
curl "http://localhost:8000/run-analysis?continuous=true"

# Combined options
curl "http://localhost:8000/run-analysis?continuous=true&batch_size=25"
```

**What happens when you call the endpoint:**

1. Fetches up to `BATCH_SIZE` unattributed images from Supabase
2. Downloads images to a temporary directory
3. Runs SpeciesNet classification with geofencing
4. Stores results back to Supabase
5. Retries generic "Bird" classifications up to 5 times
6. Falls back to OpenAI Vision if still generic after retries
7. Returns JSON response with processing statistics

### CLI Mode (Backward Compatibility)

You can still run the service as a command-line script:

**Single Batch Mode:**

```bash
python main.py
```

**Continuous Mode:**

```bash
python main.py --continuous
```

**Custom Batch Size:**

```bash
python main.py --batch-size 100
```

Or in continuous mode:

```bash
python main.py --continuous --batch-size 25
```

## How It Works

### 1. Image Selection

The service queries Supabase for images that:

- Have not been attributed yet (not in `attributions` table)
- Have a valid `image_url`
- Are ordered by `taken_on` (newest first)

### 2. Classification Process

1. **Download**: Images are downloaded to a temporary directory
2. **SpeciesNet**: Runs Google's SpeciesNet model with geofencing for Long Island, NY
3. **Parsing**: Extracts species names and confidence scores from predictions
4. **Filtering**: Removes blocklisted items (humans, vehicles, etc.) and low-confidence predictions
5. **Deduplication**: Removes duplicate species predictions, keeping highest confidence
6. **Storage**: Upserts attributions to Supabase

### 3. Retry Logic

If SpeciesNet returns generic "Bird" classifications:

- The service automatically retries up to 5 times
- Only generic classifications are re-processed
- After max retries, falls back to OpenAI Vision (if configured)

### 4. OpenAI Fallback

When SpeciesNet fails to identify specific species:

- Uses GPT-4 Vision API to identify the bird
- Filters results to species found in Long Island, NY
- Only stores predictions above the confidence threshold
- Respects rate limits with 1-second cooldown between requests

## Configuration

### Environment Variables

| Variable            | Required | Default | Description                                |
| ------------------- | -------- | ------- | ------------------------------------------ |
| `SUPABASE_URL`      | ✅ Yes   | -       | Your Supabase project URL                  |
| `SUPABASE_ANON_KEY` | ✅ Yes   | -       | Your Supabase anonymous key                |
| `OPENAI_API_KEY`    | ❌ No    | -       | OpenAI API key for fallback classification |
| `BATCH_SIZE`        | ❌ No    | `50`    | Number of images to process per batch      |
| `THRESHOLD`         | ❌ No    | `0.30`  | Minimum confidence score (0.0-1.0)         |

### Geofencing

The service is currently configured for **Long Island, New York**. To change the location, edit `LOCATION` in `main.py`:

```python
LOCATION = {"country": "USA", "admin1_region": "NY"}
```

### Blocklist

Species that are automatically filtered out:

- `blank`
- `unknown`
- `vehicle`
- `human`
- `person`
- Empty strings

To modify the blocklist, edit `BLOCKLIST` in `main.py`.

## Integration with Other Services

### From Core API Service

When new images are uploaded, trigger attribution via HTTP:

```python
import requests

# After uploading image to Supabase
response = requests.get(
    "http://media-attribution-service:8000/run-analysis",
    params={"batch_size": 10}
)
result = response.json()
print(f"Processed {result['images_processed']} images")
```

### From External Media Service

After processing images, trigger attribution:

```typescript
// After image upload
const response = await fetch(
  "http://media-attribution-service:8000/run-analysis?batch_size=10",
  { method: "GET" }
);
const result = await response.json();
console.log(`Processed ${result.images_processed} images`);
```

### Using Fetch API

```javascript
// Trigger single batch
fetch("http://media-attribution-service:8000/run-analysis")
  .then((res) => res.json())
  .then((data) => console.log(data));

// Trigger continuous processing
fetch("http://media-attribution-service:8000/run-analysis?continuous=true")
  .then((res) => res.json())
  .then((data) => console.log(data));
```

## Scheduled Jobs

To automatically attribute new images, set up scheduled jobs:

### Option 1: HTTP API (Recommended)

If the FastAPI server is running, trigger via HTTP from cron:

```bash
# Run every hour via HTTP
0 * * * * curl "http://localhost:8000/run-analysis?batch_size=20"

# Or continuous mode daily
0 2 * * * curl "http://localhost:8000/run-analysis?continuous=true"
```

### Option 2: CLI Script

Run the CLI script directly:

```bash
# Run every hour
0 * * * * cd /path/to/media-attribution-service && python main.py --batch-size 20

# Or continuous mode daily
0 2 * * * cd /path/to/media-attribution-service && python main.py --continuous
```

### Option 3: GitHub Actions

Create `.github/workflows/attribution.yml`:

```yaml
name: Process Image Attributions
on:
  schedule:
    - cron: "0 * * * *" # Every hour
  workflow_dispatch:

jobs:
  attribute-images:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          # Install SpeciesNet
      - name: Run attribution service
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_ANON_KEY: ${{ secrets.SUPABASE_ANON_KEY }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        run: python main.py --batch-size 20
```

## Architecture

```
media-attribution-service/
├── main.py              # FastAPI service and CLI script
├── requirements.txt     # Python dependencies
├── .gitignore           # Git ignore rules
└── README.md           # This file
```

### Service Architecture

The service is built as a **FastAPI** application that can:

- Run as an HTTP server for external API calls
- Run as a CLI script for local development or cron jobs
- Process images in batches with configurable batch sizes
- Handle retries and fallbacks automatically

### Data Flow

1. **Query** → Fetch unattributed images from Supabase
2. **Download** → Download images to temporary directory
3. **Classify** → Run SpeciesNet with geofencing
4. **Parse** → Extract species and confidence scores
5. **Filter** → Remove blocklisted items and low-confidence predictions
6. **Retry** → Retry generic classifications up to 5 times
7. **Fallback** → Use OpenAI Vision if still generic
8. **Store** → Upsert attributions to Supabase

## Technology Stack

- **Language**: Python 3.11+
- **Web Framework**: FastAPI
- **ASGI Server**: Uvicorn
- **AI Model**: SpeciesNet (Google Camera Trap AI)
- **Fallback**: OpenAI GPT-4 Vision
- **Database**: Supabase (PostgreSQL)
- **Dependencies**:
  - `fastapi` - Modern, fast web framework for building APIs
  - `uvicorn` - ASGI server for running FastAPI
  - `supabase` - Database client
  - `openai` - OpenAI API client
  - `speciesnet` - Google's species classification model
  - `python-dotenv` - Environment variable management
  - `requests` - HTTP requests for image downloads

## Troubleshooting

### SpeciesNet not found

**Error**: `ModuleNotFoundError: No module named 'speciesnet'`

**Solution**: Install SpeciesNet separately (see [SpeciesNet Installation](#speciesnet-installation) above)

### Images not downloading

**Error**: `Failed to download {url}`

**Solutions**:

- Check that image URLs are publicly accessible
- Verify network connectivity
- Check Supabase storage permissions
- Ensure URLs use HTTPS

### Low confidence scores

**Issue**: Many predictions below threshold

**Solutions**:

- Lower `THRESHOLD` in `.env` (e.g., `0.20` instead of `0.30`)
- Check image quality (blurry, distant, or occluded images)
- Verify geofencing is correct for your location

### Generic "Bird" classifications

**Issue**: SpeciesNet returns generic "Bird" instead of specific species

**Solutions**:

- Ensure OpenAI API key is configured for fallback
- Check image quality (better images = better classifications)
- Verify geofencing settings match your location
- The service will automatically retry up to 5 times

### OpenAI rate limits

**Error**: `Rate limit exceeded`

**Solutions**:

- Increase cooldown time in `main.py` (currently 1 second)
- Reduce batch size
- Check your OpenAI API quota

### Database errors

**Error**: `relation "attributions" does not exist`

**Solution**: Run the database setup SQL (see [Set Up Database Tables](#4-set-up-database-tables))

## Performance Tips

- **Batch Size**: Start with 20-50 images per batch for testing
- **Parallel Processing**: Consider running multiple instances for different image sets
- **Caching**: SpeciesNet results are cached, so re-running on same images is faster
- **Network**: Ensure good network connection for downloading images

## Production Deployment

### Running as a Service

1. **Install dependencies** including SpeciesNet:

   ```bash
   pip install -r requirements.txt
   ```

2. **Set environment variables** in production environment

3. **Run the FastAPI server** using a process manager like systemd, supervisor, or PM2:

   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

   Or with production settings:

   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
   ```

4. **Set up reverse proxy** (nginx, Caddy, etc.) if needed

5. **Configure health checks** - The service exposes `/run-analysis` endpoint for health monitoring

### Scheduled Jobs

You can trigger the service via HTTP requests from cron jobs:

```bash
# Run every hour via HTTP
0 * * * * curl "http://localhost:8000/run-analysis?batch_size=20"
```

Or use the CLI mode for scheduled jobs:

```bash
# Run every hour
0 * * * * cd /path/to/media-attribution-service && python main.py --batch-size 20

# Or continuous mode daily
0 2 * * * cd /path/to/media-attribution-service && python main.py --continuous
```

### Monitoring

- **Set up monitoring** for error tracking and performance
- **Configure logging** to track processing progress (logs are output to stdout)
- **Set up alerts** for failed batches or errors
- **Monitor API response times** and success rates

## License

MIT
