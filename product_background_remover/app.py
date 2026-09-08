"""Local Shopify product background-removal dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
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

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ORIGINALS_DIR = DATA_DIR / "originals"
PROCESSED_DIR = DATA_DIR / "processed"
DATABASE_PATH = DATA_DIR / "database.sqlite"
QUEUE_PATH = DATA_DIR / "queue.json"
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
              original_shopify_url TEXT, FOREIGN KEY(product_id) REFERENCES products(id)
            );
            """
        )


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


async def process_one_job() -> None:
    queue = load_queue()
    job = next((item for item in queue if item["status"] == "queued"), None)
    if not job:
        return
    job["status"] = "processing"
    job["started_at"] = now()
    save_queue(queue)
    with db() as connection:
        connection.execute("UPDATE images SET processing_status = 'processing' WHERE id = ?", (job["image_id"],))
    try:
        await download_and_remove_background(job["product_id"], job["image_id"])
        job["status"] = "completed"
        job["finished_at"] = now()
        with db() as connection:
            connection.execute("UPDATE images SET processing_status = 'completed', processing_error = NULL WHERE id = ?", (job["image_id"],))
        logger.info("Processed image %s", job["image_id"])
    except Exception as exc:  # Worker must continue with the next item.
        logger.exception("Processing failed for image %s", job["image_id"])
        job["status"] = "error"
        job["error"] = str(exc)
        job["finished_at"] = now()
        with db() as connection:
            connection.execute("UPDATE images SET processing_status = 'error', processing_error = ? WHERE id = ?", (str(exc), job["image_id"]))
    save_queue(queue)


async def worker() -> None:
    while True:
        try:
            await process_one_job()
        except Exception:
            logger.exception("Unexpected worker failure")
        await asyncio.sleep(1)


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
    output_path = output_directory / f"{image_id}.png"  # PNG preserves rembg transparency.
    if not input_path.exists():
        async with httpx.AsyncClient(follow_redirects=True, timeout=90) as client:
            response = await client.get(image["original_url"])
            response.raise_for_status()
            input_path.write_bytes(response.content)
    result = await asyncio.to_thread(subprocess.run, ["rembg", "i", str(input_path), str(output_path)], capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not output_path.exists():
        raise RuntimeError(result.stderr.strip() or "rembg did not create an output file")
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
    count, page_info = 0, None
    async with httpx.AsyncClient(timeout=90) as client:
        token = await shopify_token(client)
        headers = {"X-Shopify-Access-Token": token}
        while True:
            params = {"limit": 250, "status": "any"}
            if page_info:
                params["page_info"] = page_info
            response = await client.get(shopify_url("products.json"), headers=headers, params=params)
            response.raise_for_status()
            products = response.json().get("products", [])
            with db() as connection:
                for product in products:
                    connection.execute("INSERT INTO products(id,title,status,variants_json,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,status=excluded.status,variants_json=excluded.variants_json,updated_at=excluded.updated_at", (str(product["id"]), product["title"], product["status"], json.dumps(product.get("variants", [])), now()))
                    for image in product.get("images", []):
                        original_url = image["src"]
                        connection.execute("INSERT INTO images(id,product_id,position,original_url,original_filename,original_shopify_url) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET product_id=excluded.product_id,position=excluded.position,original_url=CASE WHEN images.uploaded_at IS NULL THEN excluded.original_url ELSE images.original_url END", (str(image["id"]), str(product["id"]), image.get("position", 0), original_url, Path(urlparse(original_url).path).name, original_url))
                    count += 1
            link = response.headers.get("link", "")
            if 'rel="next"' not in link:
                break
            page_info = link.split("page_info=")[1].split(">")[0].split("&")[0]
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
        create = await client.post(shopify_url(f"products/{product_id}/images.json"), headers=headers, json={"image": {"attachment": attachment, "filename": f"{image_id}_background_removed.png", "position": image["position"]}})
        create.raise_for_status()
        replacement_id = str(create.json()["image"]["id"])
        delete = await client.delete(shopify_url(f"products/{product_id}/images/{image_id}.json"), headers=headers)
        delete.raise_for_status()
    with db() as connection:
        connection.execute("UPDATE images SET uploaded_at = ?, shopify_replacement_id = ? WHERE id = ?", (now(), replacement_id, image_id))
    logger.info("Uploaded processed image %s for product %s", image_id, product_id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_storage()
    task = asyncio.create_task(worker())
    yield
    task.cancel()


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


@app.post("/api/products/{product_id}/images/{image_id}/process")
async def process_image(product_id: str, image_id: str) -> dict[str, bool]:
    return {"queued": queue_job(product_id, image_id)}


@app.post("/api/products/{product_id}/process_all")
async def process_all(product_id: str) -> dict[str, int]:
    with db() as connection:
        image_ids = [row["id"] for row in connection.execute("SELECT id FROM images WHERE product_id = ?", (product_id,))]
    return {"queued": sum(queue_job(product_id, image_id) for image_id in image_ids)}


@app.post("/api/products/{product_id}/images/{image_id}/upload")
async def upload(product_id: str, image_id: str) -> dict[str, bool]:
    try:
        await upload_image(product_id, image_id)
        return {"uploaded": True}
    except httpx.HTTPError as exc:
        logger.exception("Shopify upload failed")
        raise HTTPException(502, f"Shopify upload failed: {exc}") from exc


@app.post("/api/products/{product_id}/upload_all")
async def upload_all(product_id: str) -> dict[str, int]:
    with db() as connection:
        image_ids = [row["id"] for row in connection.execute("SELECT id FROM images WHERE product_id = ? AND processed_path IS NOT NULL AND uploaded_at IS NULL", (product_id,))]
    uploaded = 0
    for image_id in image_ids:
        await upload_image(product_id, image_id)
        uploaded += 1
    return {"uploaded": uploaded}


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
