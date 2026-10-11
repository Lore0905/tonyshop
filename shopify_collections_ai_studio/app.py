from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator

from core import build_dry_run, collection_path, empty_manual_collections, normalize_ai_output, path_key, path_label, plain, stable_hash


BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DB = DATA / "collections.sqlite"
LOG = DATA / "app.log"
DATA.mkdir(exist_ok=True)
load_dotenv(BASE / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.FileHandler(LOG), logging.StreamHandler()])
logger = logging.getLogger("shopify_collections_ai_studio")

SYSTEM_PROMPT = """Sei il motore di categorizzazione di Shopify Collections AI Studio. Rispondi soltanto con JSON valido conforme allo schema. Usa esclusivamente i dati sorgente ricevuti. Preferisci sempre percorsi di collection esistenti; proponi una nuova categoria solo quando nessuna esistente è adatta. Non inventare caratteristiche, compatibilità, marche o modelli. Ogni percorso deve avere una confidence tra 0 e 1 e una motivazione breve. Se il caso è dubbio, aggiungi una criticità invece di forzare una scelta."""
DEFAULT_PROMPT = """Classifica il prodotto in uno o più percorsi utili e specifici. Riusa i percorsi esistenti quando semanticamente corretti. Mantieni nomi concisi, coerenti e in italiano."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


class PathProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    livelli: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    motivazione: str = Field(min_length=1, max_length=300)


class AIProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    percorsi: list[PathProposal]
    nuove_categorie: list[list[str]]
    criticita: list[str]


class Settings(BaseModel):
    ollama_url: HttpUrl | str = "http://127.0.0.1:11434"
    model: str = "llama3.2"
    timeout: int = Field(180, ge=10, le=1800)
    pause_seconds: float = Field(10, ge=0, le=3600)
    max_retries: int = Field(2, ge=0, le=10)
    temperature: float = Field(0.2, ge=0, le=2)
    num_predict: int = Field(1200, ge=128, le=16384)
    max_depth: int = Field(3, ge=1, le=8)
    ai_prompt: str = DEFAULT_PROMPT

    @field_validator("ollama_url")
    @classmethod
    def validate_url(cls, value: HttpUrl | str) -> str:
        url = str(value).rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("URL Ollama non valido")
        return url


class OllamaConfigurationError(RuntimeError):
    pass


def init_db() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY,title TEXT NOT NULL,description_html TEXT NOT NULL DEFAULT '',image_url TEXT,vendor TEXT NOT NULL DEFAULT '',product_type TEXT NOT NULL DEFAULT '',variants_json TEXT NOT NULL DEFAULT '[]',shopify_updated_at TEXT,content_hash TEXT NOT NULL,last_synced_at TEXT NOT NULL,processing_status TEXT NOT NULL DEFAULT 'to_analyze',last_error TEXT);
        CREATE TABLE IF NOT EXISTS collections(id TEXT PRIMARY KEY,title TEXT NOT NULL,handle TEXT NOT NULL DEFAULT '',path_json TEXT NOT NULL,kind TEXT NOT NULL,source_id TEXT,products_count INTEGER NOT NULL DEFAULT 0,shopify_hash TEXT NOT NULL,removed INTEGER NOT NULL DEFAULT 0,updated_at TEXT,last_synced_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS product_collections(product_id TEXT NOT NULL,collection_id TEXT NOT NULL,PRIMARY KEY(product_id,collection_id),FOREIGN KEY(product_id) REFERENCES products(id),FOREIGN KEY(collection_id) REFERENCES collections(id));
        CREATE TABLE IF NOT EXISTS product_snapshots(id TEXT PRIMARY KEY,product_id TEXT NOT NULL,content_json TEXT NOT NULL,paths_json TEXT NOT NULL,snapshot_hash TEXT NOT NULL,created_at TEXT NOT NULL,origin TEXT NOT NULL,FOREIGN KEY(product_id) REFERENCES products(id));
        CREATE TABLE IF NOT EXISTS prompt_versions(id INTEGER PRIMARY KEY AUTOINCREMENT,version INTEGER UNIQUE NOT NULL,system_prompt TEXT NOT NULL,ai_prompt TEXT NOT NULL,settings_hash TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS generations(id TEXT PRIMARY KEY,product_id TEXT NOT NULL,prompt_version INTEGER NOT NULL,model TEXT NOT NULL,output_json TEXT NOT NULL,approved_paths_json TEXT,status TEXT NOT NULL,source_hash TEXT NOT NULL,created_at TEXT NOT NULL,approved_at TEXT,rejected_at TEXT,published_at TEXT,FOREIGN KEY(product_id) REFERENCES products(id));
        CREATE TABLE IF NOT EXISTS history(id TEXT PRIMARY KEY,product_id TEXT NOT NULL,origin TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,model TEXT,prompt_version INTEGER,outcome TEXT NOT NULL,details TEXT,created_at TEXT NOT NULL,FOREIGN KEY(product_id) REFERENCES products(id));
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,product_id TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,next_attempt_at REAL,error TEXT,duration REAL);
        CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1),value_json TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS queue_state(id INTEGER PRIMARY KEY CHECK(id=1),paused INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS publish_previews(id TEXT PRIMARY KEY,product_ids_json TEXT NOT NULL,operations_json TEXT NOT NULL,source_hashes_json TEXT NOT NULL,created_at TEXT NOT NULL,used INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS deletion_backups(id TEXT PRIMARY KEY,collection_id TEXT NOT NULL,backup_json TEXT NOT NULL,created_at TEXT NOT NULL);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(collections)")}
        if "source_id" not in columns:
            db.execute("ALTER TABLE collections ADD COLUMN source_id TEXT")
        db.execute("INSERT OR IGNORE INTO settings VALUES(1,?,?)", (Settings().model_dump_json(), now()))
        db.execute("INSERT OR IGNORE INTO queue_state VALUES(1,0,?)", (now(),))
        db.execute("UPDATE jobs SET status='queued',started_at=NULL WHERE status='processing'")
        db.execute("UPDATE products SET processing_status='queued' WHERE processing_status='processing'")
    ensure_prompt_version()


def get_settings() -> Settings:
    with connect() as db:
        row = db.execute("SELECT value_json FROM settings WHERE id=1").fetchone()
    return Settings.model_validate_json(row[0])


def ensure_prompt_version() -> int:
    settings = get_settings()
    digest = stable_hash({"system": SYSTEM_PROMPT, "prompt": settings.ai_prompt, "depth": settings.max_depth})
    with connect() as db:
        row = db.execute("SELECT version FROM prompt_versions WHERE settings_hash=?", (digest,)).fetchone()
        if row:
            return row[0]
        version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM prompt_versions").fetchone()[0]
        db.execute("INSERT INTO prompt_versions(version,system_prompt,ai_prompt,settings_hash,created_at) VALUES(?,?,?,?,?)", (version, SYSTEM_PROMPT, settings.ai_prompt, digest, now()))
        db.execute("UPDATE products SET processing_status='reevaluate' WHERE processing_status IN ('completed','published')")
        return version


def shop_domain() -> str:
    domain = os.getenv("SHOPIFY_SHOP_DOMAIN", "").replace("https://", "").rstrip("/")
    if not domain:
        raise HTTPException(400, "SHOPIFY_SHOP_DOMAIN mancante")
    return domain


async def shop_token(client: httpx.AsyncClient) -> str:
    if os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN"):
        return os.environ["SHOPIFY_ADMIN_ACCESS_TOKEN"]
    if not os.getenv("SHOPIFY_CLIENT_ID") or not os.getenv("SHOPIFY_CLIENT_SECRET"):
        raise HTTPException(400, "Credenziali Shopify mancanti")
    response = await client.post(f"https://{shop_domain()}/admin/oauth/access_token", data={"grant_type": "client_credentials", "client_id": os.environ["SHOPIFY_CLIENT_ID"], "client_secret": os.environ["SHOPIFY_CLIENT_SECRET"]})
    response.raise_for_status()
    return response.json()["access_token"]


async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=90) as client:
        token = await shop_token(client)
        version = os.getenv("SHOPIFY_API_VERSION", "2026-10")
        response = await client.post(f"https://{shop_domain()}/admin/api/{version}/graphql.json", headers={"X-Shopify-Access-Token": token}, json={"query": query, "variables": variables})
        response.raise_for_status()
        body = response.json()
    if body.get("errors"):
        raise RuntimeError(" | ".join(error["message"] for error in body["errors"]))
    throttle = body.get("extensions", {}).get("cost", {}).get("throttleStatus", {})
    if throttle and throttle.get("currentlyAvailable", 1000) < 100:
        await asyncio.sleep(max(0.1, (100 - throttle["currentlyAvailable"]) / max(throttle.get("restoreRate", 50), 1)))
    return body["data"]


PRODUCTS_QUERY = """query CatalogProducts($after:String){products(first:100,after:$after){nodes{id title descriptionHtml vendor productType updatedAt featuredMedia{preview{image{url}}} variants(first:100){nodes{id sku title}}}pageInfo{hasNextPage endCursor}}}"""
COLLECTIONS_QUERY = """query CatalogCollections($after:String){collections(first:100,after:$after){nodes{id title handle updatedAt categoryPath:metafield(namespace:\"custom\",key:\"category_path\"){value} sources{__typename id ... on CollectionConditionsSource{targetType inclusion{conditions{__typename id} selections(first:1){nodes{product{id}}}}} ... on CollectionSubCollectionsSource{collections{id}}}}pageInfo{hasNextPage endCursor}}}"""
COLLECTION_PRODUCTS_QUERY = """query CollectionProducts($id:ID!,$after:String){collection(id:$id){products(first:250,after:$after){nodes{id}pageInfo{hasNextPage endCursor}}}}"""


async def fetch_all_products() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    after = None
    while True:
        block = (await graphql(PRODUCTS_QUERY, {"after": after}))["products"]
        items.extend(block["nodes"])
        if not block["pageInfo"]["hasNextPage"]:
            return items
        after = block["pageInfo"]["endCursor"]


async def fetch_all_collections() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    after = None
    while True:
        block = (await graphql(COLLECTIONS_QUERY, {"after": after}))["collections"]
        items.extend(block["nodes"])
        if not block["pageInfo"]["hasNextPage"]:
            break
        after = block["pageInfo"]["endCursor"]
    for item in items:
        item["product_ids"] = []
        product_after = None
        while True:
            data = await graphql(COLLECTION_PRODUCTS_QUERY, {"id": item["id"], "after": product_after})
            connection = data["collection"]["products"]
            item["product_ids"].extend(node["id"] for node in connection["nodes"])
            if not connection["pageInfo"]["hasNextPage"]:
                break
            product_after = connection["pageInfo"]["endCursor"]
    return items


def product_paths(db: sqlite3.Connection, product_id: str) -> list[dict[str, Any]]:
    rows = db.execute("""SELECT c.id,c.path_json,c.kind,c.source_id,c.title FROM collections c JOIN product_collections pc ON pc.collection_id=c.id WHERE pc.product_id=? AND c.removed=0 ORDER BY c.title COLLATE NOCASE""", (product_id,)).fetchall()
    return [{"id": row["id"], "path": loads(row["path_json"], []), "kind": row["kind"], "source_id": row["source_id"], "title": row["title"]} for row in rows]


async def sync_shopify(origin: str = "sync") -> dict[str, int]:
    products, collections = await asyncio.gather(fetch_all_products(), fetch_all_collections())
    synced_at = now()
    new_count = modified_count = 0
    remote_ids = {item["id"] for item in collections}
    with connect() as db:
        previous = {row["id"]: dict(row) for row in db.execute("SELECT * FROM collections")}
        for item in collections:
            path = collection_path(item["title"], (item.get("categoryPath") or {}).get("value"))
            sources = item.get("sources") or []
            condition_sources = [source for source in sources if source.get("__typename") == "CollectionConditionsSource"]
            has_automatic_source = any(source.get("__typename") != "CollectionConditionsSource" or (source.get("inclusion") or {}).get("conditions") for source in sources)
            kind = "smart" if has_automatic_source else "manual"
            source_id = condition_sources[0]["id"] if kind == "manual" and condition_sources else None
            digest = stable_hash({"title": item["title"], "handle": item["handle"], "path": path, "kind": kind, "source_id": source_id, "products": sorted(item["product_ids"])})
            old = previous.get(item["id"])
            if not old:
                new_count += 1
            elif old["shopify_hash"] != digest or old["removed"]:
                modified_count += 1
            db.execute("""INSERT INTO collections(id,title,handle,path_json,kind,source_id,products_count,shopify_hash,removed,updated_at,last_synced_at) VALUES(?,?,?,?,?,?,?, ?,0,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,handle=excluded.handle,path_json=excluded.path_json,kind=excluded.kind,source_id=excluded.source_id,products_count=excluded.products_count,shopify_hash=excluded.shopify_hash,removed=0,updated_at=excluded.updated_at,last_synced_at=excluded.last_synced_at""", (item["id"], item["title"], item["handle"], dumps(path), kind, source_id, len(item["product_ids"]), digest, item.get("updatedAt"), synced_at))
        removed_count = db.execute(f"UPDATE collections SET removed=1,last_synced_at=? WHERE removed=0 AND id NOT IN ({','.join('?' for _ in remote_ids) or "''"})", (synced_at, *remote_ids)).rowcount
        for product in products:
            content = {"title": product["title"], "descriptionHtml": product.get("descriptionHtml") or "", "vendor": product.get("vendor") or "", "productType": product.get("productType") or "", "variants": product["variants"]["nodes"]}
            digest = stable_hash(content)
            image = (((product.get("featuredMedia") or {}).get("preview") or {}).get("image") or {}).get("url")
            db.execute("""INSERT INTO products(id,title,description_html,image_url,vendor,product_type,variants_json,shopify_updated_at,content_hash,last_synced_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,description_html=excluded.description_html,image_url=excluded.image_url,vendor=excluded.vendor,product_type=excluded.product_type,variants_json=excluded.variants_json,shopify_updated_at=excluded.shopify_updated_at,content_hash=excluded.content_hash,last_synced_at=excluded.last_synced_at""", (product["id"], product["title"], product.get("descriptionHtml") or "", image, product.get("vendor") or "", product.get("productType") or "", dumps(product["variants"]["nodes"]), product.get("updatedAt"), digest, synced_at))
        db.execute("DELETE FROM product_collections")
        for collection in collections:
            for product_id in collection["product_ids"]:
                if db.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
                    db.execute("INSERT OR IGNORE INTO product_collections VALUES(?,?)", (product_id, collection["id"]))
        for product in products:
            paths = sorted(product_paths(db, product["id"]), key=lambda item: (path_key(item["path"]), item["id"]))
            snapshot_content = {"title": product["title"], "vendor": product.get("vendor") or "", "variants": product["variants"]["nodes"]}
            snapshot_hash = stable_hash({"content": snapshot_content, "paths": paths})
            latest = db.execute("SELECT snapshot_hash,paths_json FROM product_snapshots WHERE product_id=? ORDER BY created_at DESC LIMIT 1", (product["id"],)).fetchone()
            if not latest or latest["snapshot_hash"] != snapshot_hash:
                before = loads(latest["paths_json"], []) if latest else []
                db.execute("INSERT INTO product_snapshots VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), product["id"], dumps(snapshot_content), dumps(paths), snapshot_hash, synced_at, origin))
                db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), product["id"], origin, dumps(before), dumps(paths), None, None, "success", None, synced_at))
    return {"products_total": len(products), "collections_new": new_count, "collections_modified": modified_count, "collections_removed": removed_count}


def proposal_schema(product_id: str) -> dict[str, Any]:
    path_schema = {"type": "object", "properties": {"livelli": {"type": "array", "items": {"type": "string"}}, "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "motivazione": {"type": "string"}}, "required": ["livelli", "confidence", "motivazione"], "additionalProperties": False}
    body = {
        "type": "object",
        "properties": {
            "percorsi": {"type": "array", "items": path_schema},
            "nuove_categorie": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
            "criticita": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["percorsi", "nuove_categorie", "criticita"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {product_id: body}, "required": [product_id], "additionalProperties": False}


async def ollama_models(settings: Settings) -> list[str]:
    async with httpx.AsyncClient(timeout=min(settings.timeout, 15)) as client:
        response = await client.get(f"{settings.ollama_url}/api/tags")
        response.raise_for_status()
    return [item.get("name") or item.get("model") for item in response.json().get("models", []) if item.get("name") or item.get("model")]


async def validate_ollama(settings: Settings) -> list[str]:
    try:
        models = await ollama_models(settings)
    except httpx.HTTPError as exc:
        raise OllamaConfigurationError(f"Ollama non raggiungibile su {settings.ollama_url}: {exc}") from exc
    if not models:
        raise OllamaConfigurationError("Ollama è raggiungibile ma non ha modelli installati.")
    if settings.model not in models:
        raise OllamaConfigurationError(f"Il modello '{settings.model}' non è installato. Disponibili: {', '.join(models)}")
    return models


async def call_ollama(row: sqlite3.Row, settings: Settings) -> dict[str, Any]:
    with connect() as db:
        current = product_paths(db, row["id"])
        categories = [{"percorso": loads(item["path_json"], []), "tipo": item["kind"]} for item in db.execute("SELECT path_json,kind FROM collections WHERE removed=0 ORDER BY title")]
    source = {"titolo": row["title"], "descrizione": plain(row["description_html"]), "vendor": row["vendor"], "product_type": row["product_type"], "varianti": loads(row["variants_json"], []), "percorsi_attuali": [item["path"] for item in current], "categorie_esistenti": categories, "massimo_livelli": settings.max_depth}
    prompt = f"{settings.ai_prompt}\nProdotto e tassonomia, unica fonte ammessa:\n{dumps({row['id']: source})}"
    payload = {"model": settings.model, "stream": False, "format": proposal_schema(row["id"]), "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}], "options": {"temperature": settings.temperature, "num_predict": settings.num_predict}}
    await validate_ollama(settings)
    async with httpx.AsyncClient(timeout=settings.timeout) as client:
        response = await client.post(f"{settings.ollama_url}/api/chat", json=payload)
        if response.status_code == 404:
            raise OllamaConfigurationError(f"Ollama non espone /api/chat oppure non trova il modello '{settings.model}'.")
        response.raise_for_status()
    try:
        parsed = json.loads(response.json()["message"]["content"])
        validated = AIProposal.model_validate(parsed[row["id"]]).model_dump()
    except (KeyError, json.JSONDecodeError, ValidationError) as exc:
        raise RuntimeError(f"Risposta Ollama non valida: {exc}") from exc
    return normalize_ai_output(validated, settings.max_depth)


async def process_job(job: sqlite3.Row) -> float:
    started = time.monotonic()
    settings = get_settings()
    with connect() as db:
        row = db.execute("SELECT * FROM products WHERE id=?", (job["product_id"],)).fetchone()
        if not row:
            raise RuntimeError("Prodotto non trovato")
        snapshot = db.execute("SELECT snapshot_hash FROM product_snapshots WHERE product_id=? ORDER BY created_at DESC LIMIT 1", (row["id"],)).fetchone()
    if not snapshot:
        raise RuntimeError("Snapshot mancante: risincronizza e rigenera.")
    proposal = await call_ollama(row, settings)
    generation_id = str(uuid.uuid4())
    version = ensure_prompt_version()
    with connect() as db:
        before = product_paths(db, row["id"])
        db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,status,source_hash,created_at) VALUES(?,?,?,?,?,'generated',?,?)", (generation_id, row["id"], version, settings.model, dumps(proposal), snapshot["snapshot_hash"], now()))
        db.execute("UPDATE products SET processing_status='completed',last_error=NULL WHERE id=?", (row["id"],))
        db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), row["id"], "AI", dumps(before), dumps([item["livelli"] for item in proposal["percorsi"]]), settings.model, version, "proposed", None, now()))
    return time.monotonic() - started


worker_task: asyncio.Task | None = None


async def worker() -> None:
    while True:
        try:
            with connect() as db:
                paused = db.execute("SELECT paused FROM queue_state WHERE id=1").fetchone()[0]
                job = None if paused else db.execute("SELECT * FROM jobs WHERE status='queued' AND COALESCE(next_attempt_at,0)<=? ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
                if job:
                    db.execute("UPDATE jobs SET status='processing',started_at=?,attempts=attempts+1 WHERE id=?", (now(), job["id"]))
                    db.execute("UPDATE products SET processing_status='processing' WHERE id=?", (job["product_id"],))
            if not job:
                await asyncio.sleep(0.5)
                continue
            try:
                duration = await process_job(job)
                with connect() as db:
                    db.execute("UPDATE jobs SET status='completed',finished_at=?,duration=?,error=NULL WHERE id=?", (now(), duration, job["id"]))
                await asyncio.sleep(get_settings().pause_seconds)
            except Exception as exc:
                logger.exception("Job %s failed", job["id"])
                attempts = job["attempts"] + 1
                settings = get_settings()
                with connect() as db:
                    if attempts < settings.max_retries + 1 and not isinstance(exc, OllamaConfigurationError):
                        db.execute("UPDATE jobs SET status='queued',error=?,next_attempt_at=? WHERE id=?", (str(exc), time.time() + min(60, 2**attempts), job["id"]))
                        db.execute("UPDATE products SET processing_status='queued',last_error=? WHERE id=?", (str(exc), job["product_id"]))
                    else:
                        db.execute("UPDATE jobs SET status='error',finished_at=?,error=? WHERE id=?", (now(), str(exc), job["id"]))
                        db.execute("UPDATE products SET processing_status='error',last_error=? WHERE id=?", (str(exc), job["product_id"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker loop failure")
            await asyncio.sleep(1)


def product_payload(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["variants"] = loads(item.pop("variants_json"), [])
    item["paths"] = product_paths(db, row["id"])
    generation = db.execute("SELECT * FROM generations WHERE product_id=? ORDER BY created_at DESC LIMIT 1", (row["id"],)).fetchone()
    item["latest_generation"] = generation_payload(generation)
    return item


def generation_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    item = dict(row)
    item["output"] = loads(item.pop("output_json"), {})
    item["approved_paths"] = loads(item.pop("approved_paths_json"), None)
    return item


@asynccontextmanager
async def lifespan(_: FastAPI):
    global worker_task
    init_db()
    worker_task = asyncio.create_task(worker())
    yield
    worker_task.cancel()


app = FastAPI(title="Shopify Collections AI Studio", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
async def home():
    return FileResponse(BASE / "static/index.html")


@app.post("/api/sync")
async def sync_endpoint():
    return await sync_shopify()


@app.get("/api/products")
async def list_products(q: str = "", category: str = "", subcategory: str = "", uncategorized: bool = False, proposal: bool = False, processing: str = "", vendor: str = "", limit: int = 60, offset: int = 0):
    clauses = ["(p.title LIKE ? OR p.id LIKE ? OR p.variants_json LIKE ?)"]
    args: list[Any] = [f"%{q}%"] * 3
    if processing:
        clauses.append("p.processing_status=?")
        args.append(processing)
    if vendor:
        clauses.append("p.vendor=?")
        args.append(vendor)
    if uncategorized:
        clauses.append("NOT EXISTS(SELECT 1 FROM product_collections pc WHERE pc.product_id=p.id)")
    if proposal:
        clauses.append("EXISTS(SELECT 1 FROM generations g WHERE g.product_id=p.id AND g.status IN ('generated','approved'))")
    if category:
        clauses.append("EXISTS(SELECT 1 FROM product_collections pc JOIN collections c ON c.id=pc.collection_id WHERE pc.product_id=p.id AND json_extract(c.path_json,'$[0]')=?)")
        args.append(category)
    if subcategory:
        clauses.append("EXISTS(SELECT 1 FROM product_collections pc JOIN collections c ON c.id=pc.collection_id WHERE pc.product_id=p.id AND json_extract(c.path_json,'$[1]')=?)")
        args.append(subcategory)
    where = " WHERE " + " AND ".join(clauses)
    with connect() as db:
        total = db.execute("SELECT COUNT(*) FROM products p" + where, args).fetchone()[0]
        rows = db.execute("SELECT p.* FROM products p" + where + " ORDER BY p.title COLLATE NOCASE LIMIT ? OFFSET ?", (*args, min(limit, 500), offset)).fetchall()
        items = [product_payload(db, row) for row in rows]
    return {"items": items, "total": total, "offset": offset, "limit": limit}


@app.get("/api/products/{product_id:path}")
async def product_detail(product_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Prodotto non trovato")
        history = [dict(item) for item in db.execute("SELECT * FROM history WHERE product_id=? ORDER BY created_at DESC", (product_id,))]
        for item in history:
            item["before"] = loads(item.pop("before_json"), [])
            item["after"] = loads(item.pop("after_json"), [])
        generations = [generation_payload(item) for item in db.execute("SELECT * FROM generations WHERE product_id=? ORDER BY created_at DESC", (product_id,))]
        return {"product": product_payload(db, row), "history": history, "generations": generations}


@app.get("/api/facets")
async def facets(category: str = ""):
    with connect() as db:
        vendors = [row[0] for row in db.execute("SELECT DISTINCT vendor FROM products WHERE vendor<>'' ORDER BY vendor")]
        categories = [row[0] for row in db.execute("SELECT DISTINCT json_extract(path_json,'$[0]') FROM collections WHERE removed=0 AND json_array_length(path_json)>0 ORDER BY 1")]
        if category:
            subcategories = [row[0] for row in db.execute("SELECT DISTINCT json_extract(path_json,'$[1]') FROM collections WHERE removed=0 AND json_extract(path_json,'$[0]')=? AND json_array_length(path_json)>1 ORDER BY 1", (category,))]
        else:
            subcategories = []
    return {"vendors": vendors, "categories": categories, "subcategories": subcategories}


@app.post("/api/jobs")
async def enqueue(payload: dict[str, Any]):
    product_ids = list(dict.fromkeys(payload.get("product_ids", [])))[:500]
    if not product_ids:
        raise HTTPException(400, "Seleziona almeno un prodotto")
    settings = get_settings()
    try:
        await validate_ollama(settings)
    except OllamaConfigurationError as exc:
        raise HTTPException(409, str(exc)) from exc
    queued = 0
    with connect() as db:
        for product_id in product_ids:
            if not db.execute("SELECT 1 FROM product_snapshots WHERE product_id=?", (product_id,)).fetchone():
                continue
            if db.execute("SELECT 1 FROM jobs WHERE product_id=? AND status IN ('queued','processing')", (product_id,)).fetchone():
                continue
            db.execute("INSERT INTO jobs(id,product_id,kind,status,created_at) VALUES(?,?,'generate','queued',?)", (str(uuid.uuid4()), product_id, now()))
            db.execute("UPDATE products SET processing_status='queued',last_error=NULL WHERE id=?", (product_id,))
            queued += 1
    return {"queued": queued}


@app.get("/api/queue")
async def queue_status():
    with connect() as db:
        counts = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status")}
        paused = bool(db.execute("SELECT paused FROM queue_state WHERE id=1").fetchone()[0])
        current = db.execute("SELECT j.*,p.title FROM jobs j JOIN products p ON p.id=j.product_id WHERE j.status='processing' LIMIT 1").fetchone()
    return {"paused": paused, "queued": counts.get("queued", 0), "processing": counts.get("processing", 0), "completed": counts.get("completed", 0), "error": counts.get("error", 0), "current": dict(current) if current else None}


@app.post("/api/queue/{action}")
async def queue_action(action: Literal["pause", "resume", "cancel"]):
    with connect() as db:
        if action in ("pause", "resume"):
            db.execute("UPDATE queue_state SET paused=?,updated_at=? WHERE id=1", (action == "pause", now()))
        else:
            ids = [row[0] for row in db.execute("SELECT product_id FROM jobs WHERE status='queued'")]
            db.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE status='queued'", (now(),))
            db.executemany("UPDATE products SET processing_status='to_analyze' WHERE id=?", [(item,) for item in ids])
    return {"ok": True}


@app.post("/api/generations/{generation_id}/approve")
async def approve(generation_id: str, payload: dict[str, Any]):
    indexes = {int(item) for item in payload.get("indexes", [])}
    with connect() as db:
        row = db.execute("SELECT * FROM generations WHERE id=?", (generation_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Proposta non trovata")
        output = loads(row["output_json"], {})
        approved = [item["livelli"] for index, item in enumerate(output.get("percorsi", [])) if index in indexes]
        db.execute("UPDATE generations SET approved_paths_json=?,status='approved',approved_at=? WHERE id=?", (dumps(approved), now(), generation_id))
        before = product_paths(db, row["product_id"])
        db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), row["product_id"], "manuale", dumps(before), dumps(approved), row["model"], row["prompt_version"], "approved", None, now()))
    return {"approved_paths": approved}


@app.post("/api/generations/{generation_id}/reject")
async def reject(generation_id: str):
    with connect() as db:
        row = db.execute("SELECT product_id FROM generations WHERE id=?", (generation_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Proposta non trovata")
        db.execute("UPDATE generations SET status='rejected',rejected_at=? WHERE id=?", (now(), generation_id))
    return {"ok": True}


def approved_products(db: sqlite3.Connection, product_ids: list[str]) -> list[dict[str, Any]]:
    result = []
    for product_id in product_ids:
        generation = db.execute("SELECT * FROM generations WHERE product_id=? AND status='approved' ORDER BY created_at DESC LIMIT 1", (product_id,)).fetchone()
        if generation:
            result.append({"id": product_id, "desired": loads(generation["approved_paths_json"], []), "current": product_paths(db, product_id), "generation": dict(generation)})
    return result


@app.post("/api/publish/preview")
async def publish_preview(payload: dict[str, Any]):
    product_ids = list(dict.fromkeys(payload.get("product_ids", [])))[:500]
    with connect() as db:
        products = approved_products(db, product_ids)
        if not products:
            raise HTTPException(400, "Nessuna proposta approvata per i prodotti selezionati")
        collections = [{**dict(row), "path": loads(row["path_json"], [])} for row in db.execute("SELECT * FROM collections")]
        operations = build_dry_run(products, collections)
        source_hashes = {item["id"]: item["generation"]["source_hash"] for item in products}
        preview_id = str(uuid.uuid4())
        db.execute("INSERT INTO publish_previews VALUES(?,?,?,?,?,0)", (preview_id, dumps([item["id"] for item in products]), dumps(operations), dumps(source_hashes), now()))
    return {"preview_id": preview_id, **operations}


async def fresh_product_state(product_id: str) -> tuple[list[dict[str, Any]], str]:
    query = """query FreshProductCollections($id:ID!,$after:String){product(id:$id){id title vendor variants(first:100){nodes{sku title}}collections(first:250,after:$after){nodes{id title categoryPath:metafield(namespace:\"custom\",key:\"category_path\"){value} sources{__typename id ... on CollectionConditionsSource{inclusion{conditions{__typename id}}}}}pageInfo{hasNextPage endCursor}}}}"""
    paths: list[dict[str, Any]] = []
    source: dict[str, Any] | None = None
    after = None
    while True:
        product = (await graphql(query, {"id": product_id, "after": after}))["product"]
        if not product:
            raise HTTPException(404, f"Prodotto Shopify non trovato: {product_id}")
        if source is None:
            source = {"title": product["title"], "vendor": product["vendor"], "variants": product["variants"]["nodes"]}
        for item in product["collections"]["nodes"]:
            sources = item.get("sources") or []
            condition_sources = [source for source in sources if source.get("__typename") == "CollectionConditionsSource"]
            is_smart = any(source.get("__typename") != "CollectionConditionsSource" or (source.get("inclusion") or {}).get("conditions") for source in sources)
            paths.append({"id": item["id"], "path": collection_path(item["title"], (item.get("categoryPath") or {}).get("value")), "kind": "smart" if is_smart else "manual", "source_id": condition_sources[0]["id"] if not is_smart and condition_sources else None, "title": item["title"]})
        if not product["collections"]["pageInfo"]["hasNextPage"]:
            paths.sort(key=lambda item: (path_key(item["path"]), item["id"]))
            return paths, stable_hash({"content": source, "paths": paths})
        after = product["collections"]["pageInfo"]["endCursor"]


def chunks(items: list[str], size: int = 250):
    for start in range(0, len(items), size):
        yield items[start:start + size]


async def mutation(query: str, variables: dict[str, Any], field: str) -> dict[str, Any]:
    payload = (await graphql(query, variables))[field]
    errors = payload.get("userErrors", [])
    if errors:
        raise RuntimeError(" | ".join(error["message"] for error in errors))
    return payload


CREATE_COLLECTION = """mutation CreateCollection($collection:CollectionCreateInput!){collectionCreate(collection:$collection){collection{id title sources{__typename id}}userErrors{field message}}}"""
UPDATE_SELECTIONS = """mutation UpdateCollectionSelections($collection:CollectionUpdateInput!){collectionUpdate(collection:$collection){collection{id}job{id done}userErrors{field message}}}"""
DELETE_COLLECTION = """mutation DeleteCollection($input:CollectionDeleteInput!){collectionDelete(input:$input){deletedCollectionId userErrors{field message}}}"""


@app.post("/api/publish/execute")
async def publish_execute(payload: dict[str, Any]):
    preview_id = payload.get("preview_id")
    if not payload.get("confirm"):
        raise HTTPException(400, "Conferma esplicita richiesta")
    with connect() as db:
        preview = db.execute("SELECT * FROM publish_previews WHERE id=? AND used=0", (preview_id,)).fetchone()
        if not preview:
            raise HTTPException(404, "Anteprima non trovata o già usata")
        product_ids = loads(preview["product_ids_json"], [])
        operations = loads(preview["operations_json"], {})
        source_hashes = loads(preview["source_hashes_json"], {})
        snapshots = {row["product_id"]: row["snapshot_hash"] for row in db.execute("SELECT s.product_id,s.snapshot_hash FROM product_snapshots s JOIN (SELECT product_id,MAX(created_at) created_at FROM product_snapshots GROUP BY product_id) x ON x.product_id=s.product_id AND x.created_at=s.created_at")}
    for product_id in product_ids:
        if product_id not in snapshots:
            raise HTTPException(409, f"Snapshot mancante per {product_id}: risincronizza e rigenera.")
        _, fresh_hash = await fresh_product_state(product_id)
        if fresh_hash != source_hashes.get(product_id):
            raise HTTPException(409, f"Le categorie di {product_id} sono cambiate su Shopify: risincronizza e rigenera.")
    created: dict[str, dict[str, str]] = {}
    for item in operations["create_collections"]:
        metafield = {"namespace": "custom", "key": "category_path", "type": "single_line_text_field", "value": path_label(item["path"])}
        source = {"source": {"title": "Selezione manuale", "targetType": "PRODUCTS", "inclusion": {"selections": []}}}
        result = await mutation(CREATE_COLLECTION, {"collection": {"title": item["title"], "metafields": [metafield], "sources": [source]}}, "collectionCreate")
        condition_source = next(source for source in result["collection"]["sources"] if source["__typename"] == "CollectionConditionsSource")
        created[path_key(item["path"])] = {"collection_id": result["collection"]["id"], "source_id": condition_source["id"]}
    for item in operations["add_products"]:
        target = {"collection_id": item["collection_id"], "source_id": item["source_id"]} if item["collection_id"] else created[path_key(item["path"])]
        for batch in chunks(item["product_ids"]):
            update = {"id": target["collection_id"], "sourcesToUpdate": [{"condition": {"id": target["source_id"], "inclusion": {"selectionsToAdd": [{"productId": product_id} for product_id in batch]}}}]}
            await mutation(UPDATE_SELECTIONS, {"collection": update}, "collectionUpdate")
    for item in operations["remove_products"]:
        for batch in chunks(item["product_ids"]):
            update = {"id": item["collection_id"], "sourcesToUpdate": [{"condition": {"id": item["source_id"], "inclusion": {"selectionsToRemove": [{"productId": product_id} for product_id in batch]}}}]}
            await mutation(UPDATE_SELECTIONS, {"collection": update}, "collectionUpdate")
    with connect() as db:
        db.execute("UPDATE publish_previews SET used=1 WHERE id=?", (preview_id,))
        for product_id in product_ids:
            generation = db.execute("SELECT * FROM generations WHERE product_id=? AND status='approved' ORDER BY created_at DESC LIMIT 1", (product_id,)).fetchone()
            if generation:
                before = product_paths(db, product_id)
                after = loads(generation["approved_paths_json"], [])
                db.execute("UPDATE generations SET status='published',published_at=? WHERE id=?", (now(), generation["id"]))
                db.execute("UPDATE products SET processing_status='published' WHERE id=?", (product_id,))
                db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), product_id, "pubblicazione", dumps(before), dumps(after), generation["model"], generation["prompt_version"], "success", None, now()))
    await sync_shopify("pubblicazione")
    return {"published_products": len(product_ids), "created_collections": len(created)}


@app.post("/api/cleanup/preview")
async def cleanup_preview():
    await sync_shopify("sync")
    with connect() as db:
        collections = [{**dict(row), "path": loads(row["path_json"], [])} for row in db.execute("SELECT * FROM collections")]
    return {"collections": empty_manual_collections(collections)}


@app.post("/api/cleanup/execute")
async def cleanup_execute(payload: dict[str, Any]):
    if not payload.get("confirm"):
        raise HTTPException(400, "Seconda conferma esplicita richiesta")
    await sync_shopify("sync")
    requested = set(payload.get("collection_ids", []))
    with connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM collections WHERE kind='manual' AND removed=0 AND products_count=0") if row["id"] in requested]
    deleted = []
    for item in rows:
        backup = {**item, "path": loads(item["path_json"], [])}
        with connect() as db:
            db.execute("INSERT INTO deletion_backups VALUES(?,?,?,?)", (str(uuid.uuid4()), item["id"], dumps(backup), now()))
        await mutation(DELETE_COLLECTION, {"input": {"id": item["id"]}}, "collectionDelete")
        deleted.append(item["id"])
    await sync_shopify("pubblicazione")
    return {"deleted": deleted}


@app.get("/api/backups")
async def backups():
    with connect() as db:
        return [{**dict(row), "backup": loads(row["backup_json"], {})} for row in db.execute("SELECT * FROM deletion_backups ORDER BY created_at DESC")]


@app.get("/api/settings")
async def read_settings():
    data = get_settings().model_dump(mode="json")
    data["system_prompt"] = SYSTEM_PROMPT
    data["prompt_version"] = ensure_prompt_version()
    return data


@app.put("/api/settings")
async def update_settings(settings: Settings):
    with connect() as db:
        db.execute("UPDATE settings SET value_json=?,updated_at=? WHERE id=1", (settings.model_dump_json(), now()))
    return {"ok": True, "prompt_version": ensure_prompt_version()}


@app.post("/api/ollama/test")
async def test_ollama():
    settings = get_settings()
    models = await validate_ollama(settings)
    return {"models": models, "model_available": settings.model in models, "message": "Ollama raggiungibile."}
