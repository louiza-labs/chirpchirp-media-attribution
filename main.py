# main.py — SpeciesNet (Google Camera Trap AI) for bird ID via --folders
import os, time, logging, json, subprocess, argparse
from typing import List, Dict, Optional
from dotenv import load_dotenv
from supabase import create_client, Client
from openai import OpenAI
import tempfile
from pathlib import Path
import requests
from collections import defaultdict
from fastapi import FastAPI, Query

# ---- Logging ----
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

load_dotenv()

# ---- Env ----
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_ANON_KEY"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))
CONFIDENCE_THRESHOLD = float(os.getenv("THRESHOLD", "0.30"))
MODEL_VERSION = "speciesnet-ensemble"

# Geofencing: Long Island, NY
LOCATION = {"country": "USA", "admin1_region": "NY"}

# ---- Clients ----
app = FastAPI()
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

BLOCKLIST = {"blank", "unknown", "vehicle", "human", "person", ""}

# ---- Helpers ----
def download_image(url: str, output_path: Path) -> bool:
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        output_path.write_bytes(r.content)
        return True
    except Exception as e:
        logger.error(f"Failed to download {url}: {e}")
        return False

def _extract_species_name(label: str) -> str:
    """Return only the last part of a semicolon-delimited taxonomy path."""
    if not label:
        return "Unknown"
    parts = [p.strip() for p in label.split(";") if p.strip()]
    name = parts[-1] if parts else label
    return name.replace("_", " ").title()

def classify_with_openai(image_url: str) -> List[Dict]:
    """Fallback to OpenAI vision when SpeciesNet returns generic 'Bird'."""
    if not openai_client:
        logger.warning("OpenAI API key not configured, skipping fallback")
        return []
    
    try:
        logger.info("🔄 Falling back to OpenAI vision...")
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Identify the bird species in this image. These images are taken in Long Island, New York. Please only suggest species found in that region. Return ONLY a JSON array with up to 3 possible species, each with 'name' (common name) and 'confidence' (0-1). If no bird or unsure, return empty array []."
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url}
                        }
                    ]
                }
            ],
            max_tokens=500
        )
        
        content = response.choices[0].message.content.strip()
        
        # Parse JSON from response
        if "[" in content and "]" in content:
            start = content.index("[")
            end = content.rindex("]") + 1
            results = json.loads(content[start:end])
            filtered = [r for r in results if r.get("confidence", 0) >= CONFIDENCE_THRESHOLD]
            
            if filtered:
                logger.info("OpenAI predictions:")
                for pred in filtered:
                    logger.info(f"  - {pred['name']}: {pred['confidence']:.2%}")
            
            return filtered
        
        return []
        
    except Exception as e:
        logger.error(f"OpenAI fallback error: {e}")
        return []

def run_speciesnet_on_folder(image_dir: Path, output_json: Path) -> bool:
    """Run SpeciesNet in folder mode with geofencing."""
    cmd = [
        "python", "-m", "speciesnet.scripts.run_model",
        "--folders", str(image_dir),
        "--predictions_json", str(output_json),
        "--country", LOCATION["country"],
        "--admin1_region", LOCATION["admin1_region"],
    ]
    logger.info("Running SpeciesNet (folder mode)...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(
            "SpeciesNet failed.\n"
            f"CMD: {' '.join(cmd)}\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )
        return False
    return True

def parse_speciesnet_output(
    output_json: Path,
    path_to_image_id: Dict[str, str],
    threshold: float
) -> Dict[str, List[Dict]]:
    """
    Parse predictions.json into {image_id: [rows]}.
    Only returns the last element of a semicolon-delimited taxonomy path.
    """
    if not output_json.exists():
        return {}

    data = json.loads(output_json.read_text())
    preds = data.get("predictions", []) or []
    per_image: Dict[str, List[Dict]] = defaultdict(list)

    for p in preds:
        filepath = p.get("filepath")
        image_id = path_to_image_id.get(filepath)
        if not image_id:
            continue

        label = p.get("prediction")
        score = p.get("prediction_score", 0.0)

        # Clean up the label for display
        species_name = _extract_species_name(label)

        if species_name.lower() not in BLOCKLIST and (score or 0.0) >= threshold:
            per_image[image_id].append({
                "name": species_name,
                "confidence": float(score)
            })

        # Classifier fallback top-5
        failures = set(p.get("failures", []) or [])
        if "CLASSIFIER" not in failures:
            cls = p.get("classifications") or {}
            classes = cls.get("classes") or []
            scores = cls.get("scores") or []
            for alt_label, alt_score in list(zip(classes, scores))[:5]:
                alt_species = _extract_species_name(alt_label)
                if alt_species.lower() in BLOCKLIST:
                    continue
                try:
                    alt_conf = float(alt_score)
                except Exception:
                    alt_conf = 0.0
                if alt_conf >= threshold and alt_species != species_name:
                    per_image[image_id].append({
                        "name": alt_species,
                        "confidence": alt_conf
                    })

        # Dedup + sort
        if per_image.get(image_id):
            uniq = {}
            for r in per_image[image_id]:
                k = r["name"].lower()
                if k not in uniq or r["confidence"] > uniq[k]["confidence"]:
                    uniq[k] = r
            per_image[image_id] = sorted(uniq.values(), key=lambda x: x["confidence"], reverse=True)

    return per_image

def get_candidate_images(limit: int):
    images = (sb.table("images")
                .select("id,image_url,taken_on")
                .order("taken_on", desc=True)
                .limit(limit * 2)
                .execute()).data or []
    if not images:
        return []
    ids = [r["id"] for r in images if r.get("id")]
    if not ids:
        return []
    existing = (sb.table("attributions")
                  .select("image_id")
                  .in_("image_id", ids)
                  .execute()).data or []
    attributed = {r["image_id"] for r in existing}
    return [r for r in images if r["id"] not in attributed and r.get("image_url")][:limit]

def upsert_attributions(image_id: str, species_rows: List[Dict]):
    if not species_rows:
        return
    rows = [{
        "image_id": image_id,
        "model_version": MODEL_VERSION,
        "species": s["name"],
        "confidence": s["confidence"],
        "extra": None,
    } for s in species_rows]
    sb.table("attributions").upsert(rows, on_conflict="image_id,species,model_version").execute()

def run_batch(batch_size: Optional[int] = None) -> Dict:
    """Run a single batch of image attributions. Returns stats dict."""
    actual_batch_size = batch_size or BATCH_SIZE
    logger.info("🪶 Starting bird attribution batch (SpeciesNet)…")
    candidates = get_candidate_images(actual_batch_size)
    if not candidates:
        logger.info("No images to attribute.")
        return {
            "success": True,
            "images_processed": 0,
            "attributions_created": 0,
            "message": "No images to attribute"
        }

    logger.info(f"Found {len(candidates)} images to classify")
    attributions_count = 0

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        img_dir = temp_path / "images"
        out_dir = temp_path / "results"
        img_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        output_json = out_dir / "predictions.json"

        path_to_image_id: Dict[str, str] = {}
        downloaded = 0
        for idx, row in enumerate(candidates):
            img_id, url = row["id"], row["image_url"]
            image_path = img_dir / f"{img_id}.jpg"
            if download_image(url, image_path):
                path_to_image_id[str(image_path)] = img_id
                downloaded += 1
            else:
                logger.warning(f"Skip {img_id} (download failed)")
            
            # 100ms cooloff between downloads
            if idx < len(candidates) - 1:
                time.sleep(0.1)

        if downloaded == 0:
            logger.info("Nothing downloaded; exiting.")
            return {
                "success": False,
                "images_processed": 0,
                "attributions_created": 0,
                "message": "Failed to download images"
            }

        max_retries = 5
        for attempt in range(1, max_retries + 1):
            logger.info(f"🔁 SpeciesNet run attempt {attempt}/{max_retries}")
            
            # Delete old predictions to prevent resume errors
            if output_json.exists():
                output_json.unlink()
            
            ok = run_speciesnet_on_folder(img_dir, output_json)
            if not ok or not output_json.exists():
                logger.warning("No predictions file generated")
                continue

            per_image = parse_speciesnet_output(output_json, path_to_image_id, CONFIDENCE_THRESHOLD)

            # Check if any still generic ("Bird"), re-run if needed
            generic_left = []
            for img_id, preds in per_image.items():
                if any(p["name"].lower() == "bird" for p in preds):
                    generic_left.append(img_id)

            # Upsert everything that's not "Bird"
            candidate_ids = [row["id"] for row in candidates]
            for idx, img_id in enumerate(candidate_ids):
                preds = [p for p in per_image.get(img_id, []) if p["name"].lower() != "bird"]
                if preds:
                    logger.info(f"Predictions for {img_id}:")
                    for p in preds:
                        logger.info(f"  - {p['name']}: {p['confidence']:.2%}")
                else:
                    logger.info(f"No species identified above threshold for {img_id}")
                upsert_attributions(img_id, preds)
                attributions_count += len(preds)
                logger.info(f"✅ Saved {len(preds)} species attributions for {img_id}")
                
                # 100ms cooloff between upserts
                if idx < len(candidate_ids) - 1:
                    time.sleep(0.1)

            if not generic_left:
                break  # done
            else:
                if attempt < max_retries:
                    logger.info(f"{len(generic_left)} images returned generic 'Bird'; retrying...")
                    # keep only those for reclassification
                    for fp, iid in list(path_to_image_id.items()):
                        if iid not in generic_left:
                            del path_to_image_id[fp]
                            try:
                                Path(fp).unlink(missing_ok=True)
                            except Exception:
                                pass
                    time.sleep(0.5)
                else:
                    # Final retry exhausted, fall back to OpenAI
                    logger.info(f"⚠️  {len(generic_left)} images still generic after {max_retries} retries. Using OpenAI fallback...")
                    for img_id in generic_left:
                        # Find the original URL from candidates
                        url = next((r["image_url"] for r in candidates if r["id"] == img_id), None)
                        if not url:
                            continue
                        
                        logger.info(f"🤖 OpenAI fallback for {img_id}")
                        openai_preds = classify_with_openai(url)
                        
                        if openai_preds:
                            # Update with OpenAI results
                            upsert_attributions(img_id, openai_preds)
                            logger.info(f"✅ Saved {len(openai_preds)} OpenAI predictions for {img_id}")
                        else:
                            logger.info(f"⚠️  No OpenAI predictions for {img_id}")
                        
                        # Longer cooloff for OpenAI to respect rate limits
                        time.sleep(1)
                    break

    logger.info("✨ Batch complete.")
    return {
        "success": True,
        "images_processed": len(candidates),
        "attributions_created": attributions_count,
        "message": f"Processed {len(candidates)} images, created {attributions_count} attributions"
    }

def run_continuous(batch_size: Optional[int] = None) -> Dict:
    """Run continuous mode: process all unattributed images. Returns stats dict."""
    actual_batch_size = batch_size or BATCH_SIZE
    logger.info("🔄 Continuous mode: processing all unattributed images...")
    total_processed = 0
    total_attributions = 0
    batch_num = 1
    
    while True:
        logger.info(f"\n{'='*60}")
        logger.info(f"📦 Starting batch #{batch_num}")
        logger.info(f"{'='*60}")
        
        # Check how many unattributed images remain
        candidates = get_candidate_images(actual_batch_size)
        if not candidates:
            logger.info("✅ All images have been attributed!")
            break
        
        logger.info(f"Found {len(candidates)} unattributed images in this batch")
        
        # Process this batch
        result = run_batch(actual_batch_size)
        if result.get("success"):
            total_processed += result.get("images_processed", 0)
            total_attributions += result.get("attributions_created", 0)
        
        batch_num += 1
        
        logger.info(f"📊 Progress: {total_processed} images processed so far")
        logger.info("⏸️  Pausing 2 seconds before next batch...")
        time.sleep(2)
    
    logger.info(f"\n{'='*60}")
    logger.info(f"🎉 Complete! Total images processed: {total_processed}")
    logger.info(f"{'='*60}")
    
    return {
        "success": True,
        "images_processed": total_processed,
        "attributions_created": total_attributions,
        "batches_processed": batch_num - 1,
        "message": f"Processed {total_processed} images in {batch_num - 1} batches, created {total_attributions} attributions"
    }

@app.get("/run-analysis")
def run_analysis_endpoint(
    continuous: bool = Query(default=False, description="Process all unattributed images in batches until none remain"),
    batch_size: Optional[int] = Query(default=None, description="Override BATCH_SIZE from env (default: from .env or 50)")
):
    """Run bird species attribution analysis on unattributed images."""
    try:
        if continuous:
            result = run_continuous(batch_size)
        else:
            result = run_batch(batch_size)
        return result
    except Exception as e:
        logger.error(f"Error in run_analysis: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to run analysis"
        }

# CLI entry point for backward compatibility
def main():
    parser = argparse.ArgumentParser(description="Bird species attribution service")
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Process all unattributed images in batches until none remain"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        help="Override BATCH_SIZE from env (default: from .env or 50)"
    )
    args = parser.parse_args()
    
    if args.continuous:
        result = run_continuous(args.batch_size)
        print(json.dumps(result, indent=2))
    else:
        result = run_batch(args.batch_size)
        print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()


