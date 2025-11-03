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

### Single Batch Mode

Process a single batch of unattributed images:

```bash
python main.py
```

This will:

1. Fetch up to `BATCH_SIZE` unattributed images from Supabase
2. Download images to a temporary directory
3. Run SpeciesNet classification with geofencing
4. Store results back to Supabase
5. Retry generic "Bird" classifications up to 5 times
6. Fall back to OpenAI Vision if still generic after retries

### Continuous Mode

Process all unattributed images in batches until none remain:

```bash
python main.py --continuous
```

This mode will:

- Process images in batches of `BATCH_SIZE`
- Continue until all images are attributed
- Show progress after each batch
- Pause 2 seconds between batches

### Custom Batch Size

Override the batch size from environment variables:

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

When new images are uploaded, they can be automatically attributed:

```python
# After uploading image to Supabase
import subprocess

# Trigger attribution service
subprocess.run([
    "python",
    "/path/to/media-attribution-service/main.py",
    "--batch-size", "10"
])
```

### From External Media Service

After processing images, trigger attribution:

```typescript
// After image upload
await fetch("http://media-attribution-service:8000/trigger", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    imageIds: [image.id],
    batchSize: 10,
  }),
});
```

## Scheduled Jobs

To automatically attribute new images, set up a cron job:

### Option 1: Cron Job

```bash
# Run every hour
0 * * * * cd /path/to/media-attribution-service && python main.py --batch-size 20

# Or continuous mode daily
0 2 * * * cd /path/to/media-attribution-service && python main.py --continuous
```

### Option 2: GitHub Actions

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
├── main.py              # Main service script
├── requirements.txt     # Python dependencies
└── README.md           # This file
```

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
- **AI Model**: SpeciesNet (Google Camera Trap AI)
- **Fallback**: OpenAI GPT-4 Vision
- **Database**: Supabase (PostgreSQL)
- **Dependencies**:
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

1. **Install dependencies** including SpeciesNet
2. **Set environment variables** in production environment
3. **Configure cron job** or scheduled task for automatic processing
4. **Set up monitoring** for error tracking and performance
5. **Configure logging** to track processing progress
6. **Set up alerts** for failed batches or errors

## License

MIT
