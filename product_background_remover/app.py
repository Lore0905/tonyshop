"""Local Shopify product background-removal dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ORIGINALS_DIR = DATA_DIR / "originals"
PROCESSED_DIR = DATA_DIR / "processed"
DATABASE_PATH = DATA_DIR / "database.sqlite"
QUEUE_PATH = DATA_DIR / "queue.json"
UPLOAD_QUEUE_PATH = DATA_DIR / "upload_queue.json"
LOG_PATH = DATA_DIR / "app.log"
load_dotenv(BASE_DIR / ".env")
DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
logger = logging.getLogger("product_background_remover")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_storage() -> None:
    for directory in (DATA_DIR, ORIGINALS_DIR, PROCESSED_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    if not QUEUE_PATH.exists():
        save_queue([])
    if not UPLOAD_QUEUE_PATH.exists():
        save_upload_queue([])
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
              variants_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS images (
              id TEXT PRIMARY KEY, product_id TEXT NOT NULL, position INTEGER,
              original_url TEXT NOT NULL, original_filename TEXT,
              local_original_path TEXT, processed_path TEXT, processing_status TEXT NOT NULL DEFAULT 'new',
              processing_error TEXT, uploaded_at TEXT, shopify_replacement_id TEXT,
              original_shopify_url TEXT, upload_status TEXT NOT NULL DEFAULT 'new',
              upload_error TEXT, FOREIGN KEY(product_id) REFERENCES products(id)
            );
            """
        )
        image_columns = {row["name"] for row in connection.execute("PRAGMA table_info(images)")}
        if "upload_status" not in image_columns:
            connection.execute("ALTER TABLE images ADD COLUMN upload_status TEXT NOT NULL DEFAULT 'new'")
        if "upload_error" not in image_columns:
            connection.execute("ALTER TABLE images ADD COLUMN upload_error TEXT")
        # A restart must never leave a job stranded in "processing" forever.
        connection.execute("UPDATE images SET processing_status = 'queued' WHERE processing_status = 'processing'")
    queue = load_queue()
    changed = False
    for job in queue:
        if job.get("status") == "processing":
            job["status"] = "queued"
            job.pop("started_at", None)
            changed = True
    if changed:
        save_queue(queue)
        logger.info("Recovered interrupted processing jobs back into the queue")
    upload_queue = load_upload_queue()
    changed = False
    for job in upload_queue:
        if job.get("status") == "uploading":
            job["status"] = "queued"
            job.pop("started_at", None)
            changed = True
    if changed:
        save_upload_queue(upload_queue)
        with db() as connection:
            connection.execute("UPDATE images SET upload_status = 'queued' WHERE upload_status = 'uploading'")
        logger.info("Recovered interrupted Shopify upload jobs back into the queue")


def load_queue() -> list[dict[str, Any]]:
    try:
        return json.loads(QUEUE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        logger.exception("Unable to read queue; starting from an empty queue")
        return []


def save_queue(queue: list[dict[str, Any]]) -> None:
    temporary = QUEUE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(queue, indent=2))
    temporary.replace(QUEUE_PATH)


def load_upload_queue() -> list[dict[str, Any]]:
    try:
        return json.loads(UPLOAD_QUEUE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        logger.exception("Unable to read upload queue; starting from an empty queue")
        return []


def save_upload_queue(queue: list[dict[str, Any]]) -> None:
    temporary = UPLOAD_QUEUE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(queue, indent=2))
    temporary.replace(UPLOAD_QUEUE_PATH)


def queue_job(product_id: str, image_id: str) -> bool:
    with db() as connection:
        image = connection.execute("SELECT processing_status FROM images WHERE id = ?", (image_id,)).fetchone()
        if not image:
            raise HTTPException(404, "Image not found")
        if image["processing_status"] == "completed":
            return False
        queue = load_queue()
        if any(job["image_id"] == image_id and job["status"] in {"queued", "processing"} for job in queue):
            return False
        connection.execute("UPDATE images SET processing_status = 'queued', processing_error = NULL WHERE id = ?", (image_id,))
        queue.append({"id": str(uuid.uuid4()), "product_id": product_id, "image_id": image_id, "status": "queued", "created_at": now()})
        save_queue(queue)
    logger.info("Queued image %s for product %s", image_id, product_id)
    return True


def update_job(job_id: str, **updates: Any) -> None:
    """Update a job from a fresh queue snapshot, preserving items added mid-run."""
    queue = load_queue()
    job = next((item for item in queue if item["id"] == job_id), None)
    if not job:
        raise RuntimeError(f"Queue job {job_id} disappeared")
    job.update(updates)
    save_queue(queue)


async def process_one_job() -> bool:
    queue = load_queue()
    job = next((item for item in queue if item["status"] == "queued"), None)
    if not job:
        return False
    update_job(job["id"], status="processing", started_at=now())
    with db() as connection:
        connection.execute("UPDATE images SET processing_status = 'processing' WHERE id = ?", (job["image_id"],))
    try:
        await download_and_remove_background(job["product_id"], job["image_id"])
        update_job(job["id"], status="completed", finished_at=now(), error=None)
        with db() as connection:
            connection.execute("UPDATE images SET processing_status = 'completed', processing_error = NULL WHERE id = ?", (job["image_id"],))
        logger.info("Processed image %s", job["image_id"])
    except Exception as exc:  # Worker must continue with the next item.
        logger.exception("Processing failed for image %s", job["image_id"])
        update_job(job["id"], status="error", error=str(exc), finished_at=now())
        with db() as connection:
            connection.execute("UPDATE images SET processing_status = 'error', processing_error = ? WHERE id = ?", (str(exc), job["image_id"]))
    return True


async def worker() -> None:
    while True:
        try:
            processed = await process_one_job()
        except Exception:
            logger.exception("Unexpected worker failure")
            processed = False
        # Immediately claim the next waiting image. Sleep only while the queue is empty.
        await asyncio.sleep(0 if processed else 0.5)


def queue_upload_job(product_id: str, image_id: str) -> bool:
    """Persist one Shopify upload request; only the upload worker sends it."""
    with db() as connection:
        image = connection.execute("SELECT processed_path, uploaded_at, upload_status FROM images WHERE id = ? AND product_id = ?", (image_id, product_id)).fetchone()
        if not image:
            raise HTTPException(404, "Image not found")
        if not image["processed_path"] or not Path(image["processed_path"]).is_file():
            return False
        if image["uploaded_at"]:
            return False
        queue = load_upload_queue()
        if any(job["image_id"] == image_id and job["status"] in {"queued", "uploading"} for job in queue):
            return False
        connection.execute("UPDATE images SET upload_status = 'queued', upload_error = NULL WHERE id = ?", (image_id,))
        queue.append({"id": str(uuid.uuid4()), "product_id": product_id, "image_id": image_id, "status": "queued", "created_at": now(), "attempts": 0})
        save_upload_queue(queue)
    logger.info("Queued Shopify upload for image %s", image_id)
    return True


def update_upload_job(job_id: str, **updates: Any) -> None:
    queue = load_upload_queue()
    job = next((item for item in queue if item["id"] == job_id), None)
    if not job:
        raise RuntimeError(f"Upload job {job_id} disappeared")
    job.update(updates)
    save_upload_queue(queue)


async def process_one_upload_job() -> bool:
    queue = load_upload_queue()
    job = next((item for item in queue if item["status"] == "queued"), None)
    if not job:
        return False
    update_upload_job(job["id"], status="uploading", started_at=now(), attempts=job.get("attempts", 0) + 1)
    with db() as connection:
        connection.execute("UPDATE images SET upload_status = 'uploading', upload_error = NULL WHERE id = ?", (job["image_id"],))
    try:
        await upload_image(job["product_id"], job["image_id"])
        update_upload_job(job["id"], status="completed", finished_at=now(), error=None)
        return True
    except httpx.HTTPStatusError as exc:
        # Shopify tells us exactly when a throttled request may be retried.
        retry_after = int(exc.response.headers.get("Retry-After", "2")) if exc.response.status_code == 429 else 0
        if exc.response.status_code == 429 and job.get("attempts", 0) < 5:
            update_upload_job(job["id"], status="queued", error="Rate limited by Shopify; retry scheduled")
            with db() as connection:
                connection.execute("UPDATE images SET upload_status = 'queued', upload_error = ? WHERE id = ?", ("Rate limited by Shopify; retry scheduled", job["image_id"]))
            await asyncio.sleep(max(1, retry_after))
            return True
        error = f"Shopify HTTP {exc.response.status_code}: {exc.response.text[:300]}"
    except Exception as exc:
        error = str(exc)
    logger.exception("Shopify upload failed for image %s", job["image_id"])
    update_upload_job(job["id"], status="error", error=error, finished_at=now())
    with db() as connection:
        connection.execute("UPDATE images SET upload_status = 'error', upload_error = ? WHERE id = ?", (error, job["image_id"]))
    return True


async def upload_worker() -> None:
    """One upload at a time: no burst, no manual 60-item chunk can hit the API."""
    while True:
        try:
            uploaded = await process_one_upload_job()
        except Exception:
            logger.exception("Unexpected Shopify upload worker failure")
            uploaded = False
        # Each image replacement performs a POST and a DELETE. This pause keeps
        # the request rate deliberately below Shopify's REST leaky-bucket limit.
        await asyncio.sleep(1 if uploaded else 0.5)


def flatten_on_white(transparent_path: Path, output_path: Path) -> None:
    """Turn rembg's transparent cut-out into a Shopify-ready white JPEG."""
    with Image.open(transparent_path).convert("RGBA") as cutout:
        white_background = Image.new("RGB", cutout.size, "white")
        white_background.paste(cutout, mask=cutout.getchannel("A"))
        white_background.save(output_path, "JPEG", quality=95, subsampling=0)


async def download_and_remove_background(product_id: str, image_id: str) -> None:
    with db() as connection:
        image = connection.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if not image:
        raise RuntimeError("Image no longer exists")
    original_directory = ORIGINALS_DIR / product_id
    output_directory = PROCESSED_DIR / product_id
    original_directory.mkdir(parents=True, exist_ok=True)
    output_directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(urlparse(image["original_url"]).path).suffix.lower() or ".jpg"
    input_path = original_directory / f"{image_id}{suffix}"
    transparent_path = output_directory / f"{image_id}.transparent.png"
    output_path = output_directory / f"{image_id}.jpg"
    if not input_path.exists():
        async with httpx.AsyncClient(follow_redirects=True, timeout=90) as client:
            response = await client.get(image["original_url"])
            response.raise_for_status()
            input_path.write_bytes(response.content)
    result = await asyncio.to_thread(subprocess.run, ["rembg", "i", str(input_path), str(transparent_path)], capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not transparent_path.exists():
        raise RuntimeError(result.stderr.strip() or "rembg did not create an output file")
    # rembg produces an alpha cut-out. Composite it over an opaque white canvas
    # so the exported Shopify image has a real #FFFFFF background.
    flatten_on_white(transparent_path, output_path)
    transparent_path.unlink(missing_ok=True)
    with db() as connection:
        connection.execute("UPDATE images SET local_original_path = ?, processed_path = ? WHERE id = ?", (str(input_path), str(output_path), image_id))


async def shopify_token(client: httpx.AsyncClient) -> str:
    static_token = os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN")
    if static_token:
        return static_token
    client_id, client_secret = os.getenv("SHOPIFY_CLIENT_ID"), os.getenv("SHOPIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(400, "Set SHOPIFY_ADMIN_ACCESS_TOKEN or SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET in .env")
    response = await client.post(f"https://{shop_domain()}/admin/oauth/access_token", data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
    response.raise_for_status()
    return response.json()["access_token"]


def shop_domain() -> str:
    domain = os.getenv("SHOPIFY_SHOP_DOMAIN", "").strip().replace("https://", "").rstrip("/")
    if not domain:
        raise HTTPException(400, "SHOPIFY_SHOP_DOMAIN is missing from .env")
    return domain


def shopify_url(path: str) -> str:
    return f"https://{shop_domain()}/admin/api/{os.getenv('SHOPIFY_API_VERSION', '2024-01')}/{path.lstrip('/')}"


async def synchronize_products() -> int:
    """Synchronize through GraphQL.

    This shop returns an empty list from the legacy REST products endpoint despite
    having products.  GraphQL is also the API already used by ../commons.js.
    Image IDs are converted back to their numeric REST IDs because uploads still
    use the REST image endpoint.
    """
    count, after = 0, None
    query = """
      query productsForBackgroundRemoval($after: String) {
        products(first: 250, after: $after) {
          edges {
            node {
              legacyResourceId
              title
              status
              images(first: 250) { edges { node { id originalSrc } } }
              variants(first: 250) { edges { node { legacyResourceId title sku } } }
            }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    """
    async with httpx.AsyncClient(timeout=90) as client:
        token = await shopify_token(client)
        headers = {"X-Shopify-Access-Token": token}
        while True:
            response = await client.post(shopify_url("graphql.json"), headers=headers, json={"query": query, "variables": {"after": after}})
            response.raise_for_status()
            body = response.json()
            if body.get("errors"):
                raise RuntimeError(" | ".join(error["message"] for error in body["errors"]))
            connection_data = body["data"]["products"]
            with db() as connection:
                for edge in connection_data["edges"]:
                    product = edge["node"]
                    product_id = str(product["legacyResourceId"])
                    variants = [{"id": str(item["node"]["legacyResourceId"]), "title": item["node"]["title"], "sku": item["node"]["sku"]} for item in product["variants"]["edges"]]
                    connection.execute("INSERT INTO products(id,title,status,variants_json,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,status=excluded.status,variants_json=excluded.variants_json,updated_at=excluded.updated_at", (product_id, product["title"], product["status"].lower(), json.dumps(variants), now()))
                    for position, image_edge in enumerate(product["images"]["edges"], start=1):
                        image = image_edge["node"]
                        image_id = image["id"].rsplit("/", 1)[-1]
                        original_url = image["originalSrc"]
                        connection.execute("INSERT INTO images(id,product_id,position,original_url,original_filename,original_shopify_url) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET product_id=excluded.product_id,position=excluded.position,original_url=CASE WHEN images.uploaded_at IS NULL THEN excluded.original_url ELSE images.original_url END", (image_id, product_id, position, original_url, Path(urlparse(original_url).path).name, original_url))
                    count += 1
            if not connection_data["pageInfo"]["hasNextPage"]:
                break
            after = connection_data["pageInfo"]["endCursor"]
    logger.info("Synchronized %s Shopify products", count)
    return count


def image_payload(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["original_local_url"] = f"/files/original/{data['id']}" if data["local_original_path"] else None
    data["processed_local_url"] = f"/files/processed/{data['id']}" if data["processed_path"] else None
    return data


def product_payloads() -> list[dict[str, Any]]:
    with db() as connection:
        products = connection.execute("SELECT * FROM products ORDER BY title COLLATE NOCASE").fetchall()
        result = []
        for product in products:
            images = connection.execute("SELECT * FROM images WHERE product_id = ? ORDER BY position", (product["id"],)).fetchall()
            serialized_images = [image_payload(image) for image in images]
            result.append({**dict(product), "variants": json.loads(product["variants_json"]), "images": serialized_images, "processed_count": sum(image["processing_status"] == "completed" for image in images), "total_images": len(images)})
        return result


async def upload_image(product_id: str, image_id: str) -> None:
    with db() as connection:
        image = connection.execute("SELECT * FROM images WHERE id = ? AND product_id = ?", (image_id, product_id)).fetchone()
    if not image or not image["processed_path"] or not Path(image["processed_path"]).exists():
        raise HTTPException(400, "Process the image before uploading it")
    # Shopify REST accepts an attachment. Create first, then delete the source only on success.
    import base64
    attachment = base64.b64encode(Path(image["processed_path"]).read_bytes()).decode()
    async with httpx.AsyncClient(timeout=120) as client:
        token = await shopify_token(client)
        headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
        create = await client.post(shopify_url(f"products/{product_id}/images.json"), headers=headers, json={"image": {"attachment": attachment, "filename": f"{image_id}_background_removed.jpg", "position": image["position"]}})
        create.raise_for_status()
        replacement_id = str(create.json()["image"]["id"])
        delete = await client.delete(shopify_url(f"products/{product_id}/images/{image_id}.json"), headers=headers)
        delete.raise_for_status()
    with db() as connection:
        connection.execute("UPDATE images SET uploaded_at = ?, shopify_replacement_id = ?, upload_status = 'completed', upload_error = NULL WHERE id = ?", (now(), replacement_id, image_id))
    logger.info("Uploaded processed image %s for product %s", image_id, product_id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_storage()
    processing_task = asyncio.create_task(worker())
    upload_task = asyncio.create_task(upload_worker())
    yield
    processing_task.cancel()
    upload_task.cancel()


app = FastAPI(title="Product Background Remover", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/products")
async def products() -> list[dict[str, Any]]:
    return product_payloads()


@app.post("/api/sync")
async def sync() -> dict[str, Any]:
    try:
        return {"synced": await synchronize_products()}
    except httpx.HTTPError as exc:
        logger.exception("Shopify synchronization failed")
        raise HTTPException(502, f"Shopify synchronization failed: {exc}") from exc


@app.get("/api/queue")
async def queue() -> list[dict[str, Any]]:
    return load_queue()


@app.get("/api/upload_queue")
async def upload_queue() -> list[dict[str, Any]]:
    return load_upload_queue()


@app.post("/api/products/{product_id}/images/{image_id}/process")
async def process_image(product_id: str, image_id: str) -> dict[str, bool]:
    return {"queued": queue_job(product_id, image_id)}


@app.post("/api/products/{product_id}/process_all")
async def process_all(product_id: str) -> dict[str, int]:
    with db() as connection:
        image_ids = [row["id"] for row in connection.execute("SELECT id FROM images WHERE product_id = ?", (product_id,))]
    return {"queued": sum(queue_job(product_id, image_id) for image_id in image_ids)}


@app.post("/api/process_unprocessed_products")
async def process_unprocessed_products() -> dict[str, int]:
    """Queue every image of products that have no completed photo yet."""
    with db() as connection:
        product_ids = [row["id"] for row in connection.execute("""
          SELECT products.id FROM products
          WHERE EXISTS (SELECT 1 FROM images WHERE images.product_id = products.id)
            AND NOT EXISTS (SELECT 1 FROM images WHERE images.product_id = products.id AND images.processing_status = 'completed')
        """)]
        image_rows = connection.execute(f"SELECT id, product_id FROM images WHERE product_id IN ({','.join('?' for _ in product_ids)})", product_ids).fetchall() if product_ids else []
    queued = sum(queue_job(row["product_id"], row["id"]) for row in image_rows)
    logger.info("Queued %s images from %s entirely unprocessed products", queued, len(product_ids))
    return {"queued": queued, "products": len(product_ids)}


@app.post("/api/products/{product_id}/images/{image_id}/upload")
async def upload(product_id: str, image_id: str) -> dict[str, bool]:
    return {"queued": queue_upload_job(product_id, image_id)}


@app.post("/api/products/{product_id}/upload_all")
async def upload_all(product_id: str) -> dict[str, int]:
    with db() as connection:
        image_ids = [row["id"] for row in connection.execute("SELECT id FROM images WHERE product_id = ? AND processed_path IS NOT NULL AND uploaded_at IS NULL", (product_id,))]
    return {"queued": sum(queue_upload_job(product_id, image_id) for image_id in image_ids)}


@app.get("/files/{kind}/{image_id}")
async def local_file(kind: str, image_id: str) -> FileResponse:
    if kind not in {"original", "processed"}:
        raise HTTPException(404)
    field = "local_original_path" if kind == "original" else "processed_path"
    with db() as connection:
        row = connection.execute(f"SELECT {field} AS path FROM images WHERE id = ?", (image_id,)).fetchone()
    if not row or not row["path"] or not Path(row["path"]).is_file():
        raise HTTPException(404, "Local image not found")
    return FileResponse(row["path"])
