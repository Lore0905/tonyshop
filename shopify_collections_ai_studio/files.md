
=================================
FILE: .env.example
=================================

SHOPIFY_SHOP_DOMAIN=your-store.myshopify.com
SHOPIFY_ADMIN_ACCESS_TOKEN=
# In alternativa al token statico:
SHOPIFY_CLIENT_ID=
SHOPIFY_CLIENT_SECRET=
SHOPIFY_API_VERSION=2026-10
# Scope richiesti: read_products,write_products
APP_HOST=127.0.0.1
APP_PORT=8780


=================================
FILE: app.py
=================================

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

SYSTEM_PROMPT = """Sei il motore di categorizzazione di Shopify Collections AI Studio. Rispondi soltanto con JSON valido conforme allo schema. Usa esclusivamente i dati sorgente ricevuti. Valuta il prodotto in modo indipendente: l'elenco delle collection esistenti è un riferimento, non un vincolo. Riusa un percorso esistente soltanto quando è semanticamente preciso; proponi un percorso nuovo quando descrive meglio il prodotto. Ogni percorso può contenere al massimo 3 livelli totali, categoria principale inclusa. Non inventare caratteristiche, compatibilità, marche o modelli. Ogni percorso deve avere una confidence tra 0 e 1 e una motivazione breve. Se il caso è dubbio, aggiungi una criticità invece di forzare una scelta."""
DEFAULT_PROMPT = """Classifica il prodotto in uno o più percorsi chiari, specifici e utili alla navigazione. Non forzare il riuso delle categorie esistenti: usale solo se descrivono con precisione il prodotto, altrimenti proponi una nuova categoria coerente. Ogni percorso deve avere da 1 a 3 livelli complessivi. Mantieni nomi concisi, coerenti e in italiano."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def snapshot_fingerprint(source: dict[str, Any], paths: list[dict[str, Any]]) -> str:
    """Fingerprint unico di stato prodotto + percorsi.
    Usato sia da sync_shopify sia da fresh_product_state: unica fonte di verità
    per evitare discrepanze di serializzazione tra sync e publish."""
    return stable_hash({"content": source, "paths": paths})


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
    max_depth: int = Field(3, ge=1, le=3)
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
        db.execute("UPDATE products SET processing_status='completed' WHERE processing_status='reevaluate'")
        # Align records created before the explicit approval/rejection product states
        # were introduced.  The most recent proposal is the source of truth here.
        db.execute("""
            UPDATE products
            SET processing_status = CASE (
                SELECT g.status
                FROM generations g
                WHERE g.product_id = products.id
                ORDER BY g.created_at DESC
                LIMIT 1
            )
                WHEN 'rejected' THEN 'rejected'
                WHEN 'approved' THEN 'approved'
                ELSE processing_status
            END
            WHERE EXISTS (SELECT 1 FROM generations g WHERE g.product_id = products.id)
        """)
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
            snapshot_hash = snapshot_fingerprint(snapshot_content, paths)
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
    depth_limit = min(settings.max_depth, 3)
    source = {"titolo": row["title"], "descrizione": plain(row["description_html"]), "vendor": row["vendor"], "product_type": row["product_type"], "varianti": loads(row["variants_json"], []), "percorsi_attuali": [item["path"] for item in current], "categorie_esistenti": categories, "massimo_livelli": depth_limit}
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
    return normalize_ai_output(validated, depth_limit)


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
        approved.sort(key=path_key)
        previous = loads(row["approved_paths_json"], [])
        changed = sorted(previous, key=path_key) != approved
        if changed or row["status"] != "approved":
            db.execute("UPDATE generations SET approved_paths_json=?,status='approved',approved_at=? WHERE id=?", (dumps(approved), now(), generation_id))
            if changed:
                before = product_paths(db, row["product_id"])
                db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), row["product_id"], "manuale", dumps(before), dumps(approved), row["model"], row["prompt_version"], "approved", None, now()))
        db.execute("UPDATE products SET processing_status='approved',last_error=NULL WHERE id=?", (row["product_id"],))
    return {"approved_paths": approved, "changed": changed}


@app.post("/api/generations/{generation_id}/reject")
async def reject(generation_id: str):
    with connect() as db:
        row = db.execute("SELECT product_id,status FROM generations WHERE id=?", (generation_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Proposta non trovata")
        db.execute("UPDATE generations SET status='rejected',rejected_at=? WHERE id=?", (now(), generation_id))
        db.execute("UPDATE products SET processing_status='rejected',last_error=NULL WHERE id=?", (row["product_id"],))
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
    # ATTENZIONE: la selezione delle varianti deve coincidere con PRODUCTS_QUERY
    # (nodes{id sku title}), altrimenti l'hash fresco non corrisponde mai a quello
    # salvato nella sync e publish_execute fallisce sempre con 409.
    query = """query FreshProductCollections($id:ID!,$after:String){product(id:$id){id title vendor variants(first:100){nodes{id sku title}}collections(first:250,after:$after){nodes{id title categoryPath:metafield(namespace:\"custom\",key:\"category_path\"){value} sources{__typename id ... on CollectionConditionsSource{inclusion{conditions{__typename id}}}}}pageInfo{hasNextPage endCursor}}}}"""
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
            return paths, snapshot_fingerprint(source, paths)
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
METAFIELDS_SET = """mutation SetCollectionPath($metafields:[MetafieldsSetInput!]!){metafieldsSet(metafields:$metafields){metafields{ownerType key value}userErrors{field message code}}}"""
JOB_QUERY = """query JobStatus($id:ID!){job(id:$id){id done}}"""
DELETE_COLLECTION = """mutation DeleteCollection($input:CollectionDeleteInput!){collectionDelete(input:$input){deletedCollectionId userErrors{field message}}}"""


async def wait_for_job(job: dict[str, Any] | None) -> None:
    if not job or job.get("done"):
        return
    for _ in range(60):
        await asyncio.sleep(0.5)
        current = (await graphql(JOB_QUERY, {"id": job["id"]})).get("job")
        if current and current.get("done"):
            return
    raise RuntimeError("Shopify non ha completato l'aggiornamento della collection entro 30 secondi.")


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
        expected_hash = source_hashes.get(product_id)
        if fresh_hash != expected_hash:
            logger.warning("Conflitto publish per %s: fresh=%s atteso=%s", product_id, fresh_hash, expected_hash)
            raise HTTPException(409, f"Le categorie di {product_id} sono cambiate su Shopify: risincronizza e rigenera.")
    created: dict[str, dict[str, str]] = {}
    pending_adds: dict[str, list[str]] = {}
    for item in operations["add_products"]:
        if item["collection_id"] is None:
            pending_adds[path_key(item["path"])] = item["product_ids"]
    for item in operations["create_collections"]:
        product_ids = pending_adds.get(path_key(item["path"]), [])
        source = {"source": {"title": "Selezione manuale", "targetType": "PRODUCTS", "inclusion": {"selections": [{"productId": product_id} for product_id in product_ids[:250]]}}}
        result = await mutation(CREATE_COLLECTION, {"collection": {"title": item["title"], "sources": [source]}}, "collectionCreate")
        condition_source = next(source for source in result["collection"]["sources"] if source["__typename"] == "CollectionConditionsSource")
        collection_id = result["collection"]["id"]
        created[path_key(item["path"])] = {"collection_id": collection_id, "source_id": condition_source["id"]}
        metafield = {"ownerId": collection_id, "namespace": "custom", "key": "category_path", "type": "single_line_text_field", "value": path_label(item["path"])}
        await mutation(METAFIELDS_SET, {"metafields": [metafield]}, "metafieldsSet")
    for item in operations["add_products"]:
        target = {"collection_id": item["collection_id"], "source_id": item["source_id"]} if item["collection_id"] else created[path_key(item["path"])]
        already_created = 250 if item["collection_id"] is None else 0
        for batch in chunks(item["product_ids"][already_created:]):
            update = {"id": target["collection_id"], "sourcesToUpdate": [{"condition": {"id": target["source_id"], "inclusion": {"selectionsToAdd": [{"productId": product_id} for product_id in batch]}}}]}
            result = await mutation(UPDATE_SELECTIONS, {"collection": update}, "collectionUpdate")
            await wait_for_job(result.get("job"))
    for item in operations["remove_products"]:
        for batch in chunks(item["product_ids"]):
            update = {"id": item["collection_id"], "sourcesToUpdate": [{"condition": {"id": item["source_id"], "inclusion": {"selectionsToRemove": [{"productId": product_id} for product_id in batch]}}}]}
            result = await mutation(UPDATE_SELECTIONS, {"collection": update}, "collectionUpdate")
            await wait_for_job(result.get("job"))
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

=================================
FILE: core.py
=================================

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Iterable
from typing import Any


SEPARATOR = " > "
_SPACE = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")


def plain(value: str | None) -> str:
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", value or ""))).strip()


def canonical_level(value: str) -> str:
    return _SPACE.sub(" ", value.strip(" \t-–—>|/"))


def normalize_levels(levels: Iterable[str]) -> list[str]:
    return [clean for item in levels if (clean := canonical_level(str(item)))]


def path_key(levels: Iterable[str]) -> str:
    return SEPARATOR.join(part.casefold() for part in normalize_levels(levels))


def path_label(levels: Iterable[str]) -> str:
    return SEPARATOR.join(normalize_levels(levels))


def display_path(levels: Iterable[str]) -> str:
    return "-".join(normalize_levels(levels))


def collection_path(title: str, metafield_value: str | None) -> list[str]:
    source = (metafield_value or title or "").strip()
    return normalize_levels(source.split(">"))


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def normalize_ai_output(raw: dict[str, Any], max_depth: int) -> dict[str, Any]:
    paths: list[dict[str, Any]] = []
    seen: set[str] = set()
    issues = [str(item).strip() for item in raw.get("criticita", []) if str(item).strip()]
    for item in raw.get("percorsi", []):
        levels = normalize_levels(item.get("livelli", []))
        if not levels:
            issues.append("Percorso vuoto scartato.")
            continue
        if len(levels) > max_depth:
            issues.append(f"Percorso '{path_label(levels)}' scartato: supera {max_depth} livelli.")
            continue
        key = path_key(levels)
        if key in seen:
            continue
        seen.add(key)
        paths.append({
            "livelli": levels,
            "confidence": max(0.0, min(1.0, float(item.get("confidence", 0)))),
            "motivazione": str(item.get("motivazione", "")).strip()[:300],
        })
    new_categories: list[list[str]] = []
    new_seen: set[str] = set()
    for levels in raw.get("nuove_categorie", []):
        normalized = normalize_levels(levels)
        key = path_key(normalized)
        if normalized and len(normalized) <= max_depth and key not in new_seen:
            new_seen.add(key)
            new_categories.append(normalized)
    return {"percorsi": paths, "nuove_categorie": new_categories, "criticita": list(dict.fromkeys(issues))}


def build_dry_run(products: list[dict[str, Any]], collections: list[dict[str, Any]]) -> dict[str, Any]:
    by_path = {path_key(item["path"]): item for item in collections if not item.get("removed")}
    create: dict[str, dict[str, Any]] = {}
    add: dict[str, dict[str, Any]] = {}
    remove: dict[str, dict[str, Any]] = {}
    blocked: list[dict[str, str]] = []
    for product in products:
        current = {path_key(item["path"]): item for item in product.get("current", [])}
        desired = {path_key(path): path for path in product.get("desired", [])}
        for key, levels in desired.items():
            collection = by_path.get(key)
            if collection and collection.get("kind") == "smart":
                if key not in current:
                    blocked.append({"product_id": product["id"], "path": path_label(levels), "reason": "Collection smart in sola lettura"})
                continue
            if collection and not collection.get("source_id"):
                blocked.append({"product_id": product["id"], "path": path_label(levels), "reason": "Sorgente manuale Shopify non disponibile"})
                continue
            if not collection:
                create[key] = {"path": levels, "title": levels[-1]}
                collection_key = f"new:{key}"
            else:
                collection_key = collection["id"]
            if key not in current:
                entry = add.setdefault(collection_key, {"collection_id": collection.get("id") if collection else None, "source_id": collection.get("source_id") if collection else None, "path": levels, "product_ids": []})
                entry["product_ids"].append(product["id"])
        for key, item in current.items():
            if key in desired or item.get("kind") != "manual":
                continue
            if not item.get("source_id"):
                blocked.append({"product_id": product["id"], "path": path_label(item["path"]), "reason": "Sorgente manuale Shopify non disponibile"})
                continue
            entry = remove.setdefault(item["id"], {"collection_id": item["id"], "source_id": item["source_id"], "path": item["path"], "product_ids": []})
            entry["product_ids"].append(product["id"])
    return {
        "create_collections": list(create.values()),
        "add_products": list(add.values()),
        "remove_products": list(remove.values()),
        "blocked": blocked,
    }


def empty_manual_collections(collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in collections if item.get("kind") == "manual" and not item.get("removed") and int(item.get("products_count", 0)) == 0]


=================================
FILE: package-lock.json
=================================

{
  "name": "shopify-collections-ai-studio-ui",
  "lockfileVersion": 3,
  "requires": true,
  "packages": {
    "": {
      "name": "shopify-collections-ai-studio-ui",
      "devDependencies": {
        "tailwindcss": "3.4.17"
      }
    },
    "node_modules/@alloc/quick-lru": {
      "version": "5.3.0",
      "resolved": "https://registry.npmjs.org/@alloc/quick-lru/-/quick-lru-5.3.0.tgz",
      "integrity": "sha512-U4+70Pc5ZS9osnCBCE5Jha/ciHM+Yp+CNMNC/7HvYbNRk1Ldd+f7qO65W5qfhu/TCv+/ozljlXXe9Nj8419DMA==",
      "dev": true,
      "engines": {
        "node": ">=10"
      },
      "funding": {
        "url": "https://github.com/sponsors/sindresorhus"
      }
    },
    "node_modules/@jridgewell/gen-mapping": {
      "version": "0.3.13",
      "resolved": "https://registry.npmjs.org/@jridgewell/gen-mapping/-/gen-mapping-0.3.13.tgz",
      "integrity": "sha512-2kkt/7niJ6MgEPxF0bYdQ6etZaA+fQvDcLKckhy1yIQOzaoKjBBjSj63/aLVjYE3qhRt5dvM+uUyfCg6UKCBbA==",
      "dev": true,
      "dependencies": {
        "@jridgewell/sourcemap-codec": "^1.5.0",
        "@jridgewell/trace-mapping": "^0.3.24"
      }
    },
    "node_modules/@jridgewell/resolve-uri": {
      "version": "3.1.2",
      "resolved": "https://registry.npmjs.org/@jridgewell/resolve-uri/-/resolve-uri-3.1.2.tgz",
      "integrity": "sha512-bRISgCIjP20/tbWSPWMEi54QVPRZExkuD9lJL+UIxUKtwVJA8wW1Trb1jMs1RFXo1CBTNZ/5hpC9QvmKWdopKw==",
      "dev": true,
      "engines": {
        "node": ">=6.0.0"
      }
    },
    "node_modules/@jridgewell/sourcemap-codec": {
      "version": "1.6.0",
      "resolved": "https://registry.npmjs.org/@jridgewell/sourcemap-codec/-/sourcemap-codec-1.6.0.tgz",
      "integrity": "sha512-T7jf+5zgsZHwNJ4lvQ7/aezbyk0nNX+zJVWpmHA7VYsEx7a7qr5Rg5IbtJFqkgze5Y2sruq1RUY8Q837Od7iFw==",
      "dev": true
    },
    "node_modules/@jridgewell/trace-mapping": {
      "version": "0.3.31",
      "resolved": "https://registry.npmjs.org/@jridgewell/trace-mapping/-/trace-mapping-0.3.31.tgz",
      "integrity": "sha512-zzNR+SdQSDJzc8joaeP8QQoCQr8NuYx2dIIytl1QeBEZHJ9uW6hebsrYgbz8hJwUQao3TWCMtmfV8Nu1twOLAw==",
      "dev": true,
      "dependencies": {
        "@jridgewell/resolve-uri": "^3.1.0",
        "@jridgewell/sourcemap-codec": "^1.4.14"
      }
    },
    "node_modules/@nodelib/fs.scandir": {
      "version": "2.1.5",
      "resolved": "https://registry.npmjs.org/@nodelib/fs.scandir/-/fs.scandir-2.1.5.tgz",
      "integrity": "sha512-vq24Bq3ym5HEQm2NKCr3yXDwjc7vTsEThRDnkp2DK9p1uqLR+DHurm/NOTo0KG7HYHU7eppKZj3MyqYuMBf62g==",
      "dev": true,
      "dependencies": {
        "@nodelib/fs.stat": "2.0.5",
        "run-parallel": "^1.1.9"
      },
      "engines": {
        "node": ">= 8"
      }
    },
    "node_modules/@nodelib/fs.stat": {
      "version": "2.0.5",
      "resolved": "https://registry.npmjs.org/@nodelib/fs.stat/-/fs.stat-2.0.5.tgz",
      "integrity": "sha512-RkhPPp2zrqDAQA/2jNhnztcPAlv64XdhIp7a7454A5ovI7Bukxgt7MX7udwAu3zg1DcpPU0rz3VV1SeaqvY4+A==",
      "dev": true,
      "engines": {
        "node": ">= 8"
      }
    },
    "node_modules/@nodelib/fs.walk": {
      "version": "1.2.8",
      "resolved": "https://registry.npmjs.org/@nodelib/fs.walk/-/fs.walk-1.2.8.tgz",
      "integrity": "sha512-oGB+UxlgWcgQkgwo8GcEGwemoTFt3FIO9ababBmaGwXIoBKZ+GTy0pP185beGg7Llih/NSHSV2XAs1lnznocSg==",
      "dev": true,
      "dependencies": {
        "@nodelib/fs.scandir": "2.1.5",
        "fastq": "^1.6.0"
      },
      "engines": {
        "node": ">= 8"
      }
    },
    "node_modules/any-promise": {
      "version": "1.3.0",
      "resolved": "https://registry.npmjs.org/any-promise/-/any-promise-1.3.0.tgz",
      "integrity": "sha512-7UvmKalWRt1wgjL1RrGxoSJW/0QZFIegpeGvZG9kjp8vrRu55XTHbwnqq2GpXm9uLbcuhxm3IqX9OB4MZR1b2A==",
      "dev": true
    },
    "node_modules/anymatch": {
      "version": "3.1.3",
      "resolved": "https://registry.npmjs.org/anymatch/-/anymatch-3.1.3.tgz",
      "integrity": "sha512-KMReFUr0B4t+D+OBkjR3KYqvocp2XaSzO55UcB6mgQMd3KbcE+mWTyvVV7D/zsdEbNnV6acZUutkiHQXvTr1Rw==",
      "dev": true,
      "dependencies": {
        "normalize-path": "^3.0.0",
        "picomatch": "^2.0.4"
      },
      "engines": {
        "node": ">= 8"
      }
    },
    "node_modules/arg": {
      "version": "5.0.2",
      "resolved": "https://registry.npmjs.org/arg/-/arg-5.0.2.tgz",
      "integrity": "sha512-PYjyFOLKQ9y57JvQ6QLo8dAgNqswh8M1RMJYdQduT6xbWSgK36P/Z/v+p888pM69jMMfS8Xd8F6I1kQ/I9HUGg==",
      "dev": true
    },
    "node_modules/binary-extensions": {
      "version": "2.3.0",
      "resolved": "https://registry.npmjs.org/binary-extensions/-/binary-extensions-2.3.0.tgz",
      "integrity": "sha512-Ceh+7ox5qe7LJuLHoY0feh3pHuUDHAcRUeyL2VYghZwfpkNIy/+8Ocg0a3UuSoYzavmylwuLWQOf3hl0jjMMIw==",
      "dev": true,
      "engines": {
        "node": ">=8"
      },
      "funding": {
        "url": "https://github.com/sponsors/sindresorhus"
      }
    },
    "node_modules/braces": {
      "version": "3.0.3",
      "resolved": "https://registry.npmjs.org/braces/-/braces-3.0.3.tgz",
      "integrity": "sha512-yQbXgO/OSZVD2IsiLlro+7Hf6Q18EJrKSEsdoMzKePKXct3gvD8oLcOQdIzGupr5Fj+EDe8gO/lxc1BzfMpxvA==",
      "dev": true,
      "dependencies": {
        "fill-range": "^7.1.1"
      },
      "engines": {
        "node": ">=8"
      }
    },
    "node_modules/camelcase-css": {
      "version": "2.0.1",
      "resolved": "https://registry.npmjs.org/camelcase-css/-/camelcase-css-2.0.1.tgz",
      "integrity": "sha512-QOSvevhslijgYwRx6Rv7zKdMF8lbRmx+uQGx2+vDc+KI/eBnsy9kit5aj23AgGu3pa4t9AgwbnXWqS+iOY+2aA==",
      "dev": true,
      "engines": {
        "node": ">= 6"
      }
    },
    "node_modules/chokidar": {
      "version": "3.6.0",
      "resolved": "https://registry.npmjs.org/chokidar/-/chokidar-3.6.0.tgz",
      "integrity": "sha512-7VT13fmjotKpGipCW9JEQAusEPE+Ei8nl6/g4FBAmIm0GOOLMua9NDDo/DWp0ZAxCr3cPq5ZpBqmPAQgDda2Pw==",
      "dev": true,
      "dependencies": {
        "anymatch": "~3.1.2",
        "braces": "~3.0.2",
        "glob-parent": "~5.1.2",
        "is-binary-path": "~2.1.0",
        "is-glob": "~4.0.1",
        "normalize-path": "~3.0.0",
        "readdirp": "~3.6.0"
      },
      "engines": {
        "node": ">= 8.10.0"
      },
      "funding": {
        "url": "https://paulmillr.com/funding/"
      },
      "optionalDependencies": {
        "fsevents": "~2.3.2"
      }
    },
    "node_modules/chokidar/node_modules/glob-parent": {
      "version": "5.1.2",
      "resolved": "https://registry.npmjs.org/glob-parent/-/glob-parent-5.1.2.tgz",
      "integrity": "sha512-AOIgSQCepiJYwP3ARnGx+5VnTu2HBYdzbGP45eLw1vr3zB3vZLeyed1sC9hnbcOc9/SrMyM5RPQrkGz4aS9Zow==",
      "dev": true,
      "dependencies": {
        "is-glob": "^4.0.1"
      },
      "engines": {
        "node": ">= 6"
      }
    },
    "node_modules/commander": {
      "version": "4.1.1",
      "resolved": "https://registry.npmjs.org/commander/-/commander-4.1.1.tgz",
      "integrity": "sha512-NOKm8xhkzAjzFx8B2v5OAHT+u5pRQc2UCa2Vq9jYL/31o2wi9mxBA7LIFs3sV5VSC49z6pEhfbMULvShKj26WA==",
      "dev": true,
      "engines": {
        "node": ">= 6"
      }
    },
    "node_modules/cssesc": {
      "version": "3.0.0",
      "resolved": "https://registry.npmjs.org/cssesc/-/cssesc-3.0.0.tgz",
      "integrity": "sha512-/Tb/JcjK111nNScGob5MNtsntNM1aCNUDipB/TkwZFhyDrrE47SOx/18wF2bbjgc3ZzCSKW1T5nt5EbFoAz/Vg==",
      "dev": true,
      "bin": {
        "cssesc": "bin/cssesc"
      },
      "engines": {
        "node": ">=4"
      }
    },
    "node_modules/didyoumean": {
      "version": "1.2.2",
      "resolved": "https://registry.npmjs.org/didyoumean/-/didyoumean-1.2.2.tgz",
      "integrity": "sha512-gxtyfqMg7GKyhQmb056K7M3xszy/myH8w+B4RT+QXBQsvAOdc3XymqDDPHx1BgPgsdAA5SIifona89YtRATDzw==",
      "dev": true
    },
    "node_modules/dlv": {
      "version": "1.1.3",
      "resolved": "https://registry.npmjs.org/dlv/-/dlv-1.1.3.tgz",
      "integrity": "sha512-+HlytyjlPKnIG8XuRG8WvmBP8xs8P71y+SKKS6ZXWoEgLuePxtDoUEiH7WkdePWrQ5JBpE6aoVqfZfJUQkjXwA==",
      "dev": true
    },
    "node_modules/es-errors": {
      "version": "1.3.0",
      "resolved": "https://registry.npmjs.org/es-errors/-/es-errors-1.3.0.tgz",
      "integrity": "sha512-Zf5H2Kxt2xjTvbJvP2ZWLEICxA6j+hAmMzIlypy4xcBg1vKVnx89Wy0GbS+kf5cwCVFFzdCFh2XSCFNULS6csw==",
      "dev": true,
      "engines": {
        "node": ">= 0.4"
      }
    },
    "node_modules/fast-glob": {
      "version": "3.3.3",
      "resolved": "https://registry.npmjs.org/fast-glob/-/fast-glob-3.3.3.tgz",
      "integrity": "sha512-7MptL8U0cqcFdzIzwOTHoilX9x5BrNqye7Z/LuC7kCMRio1EMSyqRK3BEAUD7sXRq4iT4AzTVuZdhgQ2TCvYLg==",
      "dev": true,
      "dependencies": {
        "@nodelib/fs.stat": "^2.0.2",
        "@nodelib/fs.walk": "^1.2.3",
        "glob-parent": "^5.1.2",
        "merge2": "^1.3.0",
        "micromatch": "^4.0.8"
      },
      "engines": {
        "node": ">=8.6.0"
      }
    },
    "node_modules/fast-glob/node_modules/glob-parent": {
      "version": "5.1.2",
      "resolved": "https://registry.npmjs.org/glob-parent/-/glob-parent-5.1.2.tgz",
      "integrity": "sha512-AOIgSQCepiJYwP3ARnGx+5VnTu2HBYdzbGP45eLw1vr3zB3vZLeyed1sC9hnbcOc9/SrMyM5RPQrkGz4aS9Zow==",
      "dev": true,
      "dependencies": {
        "is-glob": "^4.0.1"
      },
      "engines": {
        "node": ">= 6"
      }
    },
    "node_modules/fastq": {
      "version": "1.20.3",
      "resolved": "https://registry.npmjs.org/fastq/-/fastq-1.20.3.tgz",
      "integrity": "sha512-XKv5nnLs6nLF71NgiKJLIZFLkPyIEuOselLG7ujZnGrRfQK8HpvY+WqKhAJUAdLomwVHErVS4LfxFlPq0/FTAw==",
      "dev": true,
      "dependencies": {
        "reusify": "^1.0.4"
      }
    },
    "node_modules/fill-range": {
      "version": "7.1.1",
      "resolved": "https://registry.npmjs.org/fill-range/-/fill-range-7.1.1.tgz",
      "integrity": "sha512-YsGpe3WHLK8ZYi4tWDg2Jy3ebRz2rXowDxnld4bkQB00cc/1Zw9AWnC0i9ztDJitivtQvaI9KaLyKrc+hBW0yg==",
      "dev": true,
      "dependencies": {
        "to-regex-range": "^5.0.1"
      },
      "engines": {
        "node": ">=8"
      }
    },
    "node_modules/fsevents": {
      "version": "2.3.3",
      "resolved": "https://registry.npmjs.org/fsevents/-/fsevents-2.3.3.tgz",
      "integrity": "sha512-5xoDfX+fL7faATnagmWPpbFtwh/R77WmMMqqHGS65C3vvB0YHrgF+B1YmZ3441tMj5n63k0212XNoJwzlhffQw==",
      "dev": true,
      "hasInstallScript": true,
      "optional": true,
      "os": [
        "darwin"
      ],
      "engines": {
        "node": "^8.16.0 || ^10.6.0 || >=11.0.0"
      }
    },
    "node_modules/function-bind": {
      "version": "1.1.2",
      "resolved": "https://registry.npmjs.org/function-bind/-/function-bind-1.1.2.tgz",
      "integrity": "sha512-7XHNxH7qX9xG5mIwxkhumTox/MIRNcOgDrxWsMt2pAr23WHp6MrRlN7FBSFpCpr+oVO0F744iUgR82nJMfG2SA==",
      "dev": true,
      "funding": {
        "url": "https://github.com/sponsors/ljharb"
      }
    },
    "node_modules/glob-parent": {
      "version": "6.0.2",
      "resolved": "https://registry.npmjs.org/glob-parent/-/glob-parent-6.0.2.tgz",
      "integrity": "sha512-XxwI8EOhVQgWp6iDL+3b0r86f4d6AX6zSU55HfB4ydCEuXLXc5FcYeOu+nnGftS4TEju/11rt4KJPTMgbfmv4A==",
      "dev": true,
      "dependencies": {
        "is-glob": "^4.0.3"
      },
      "engines": {
        "node": ">=10.13.0"
      }
    },
    "node_modules/hasown": {
      "version": "2.0.4",
      "resolved": "https://registry.npmjs.org/hasown/-/hasown-2.0.4.tgz",
      "integrity": "sha512-T2UbfbBEF32wiepXIsMlTW9+dDYC6wMh/t/vYA4tuOMKqWz/n3vr1NFSxQiyP+zk2mXsoMA/i/7qV6LKut1t1A==",
      "dev": true,
      "dependencies": {
        "function-bind": "^1.1.2"
      },
      "engines": {
        "node": ">= 0.4"
      }
    },
    "node_modules/is-binary-path": {
      "version": "2.1.0",
      "resolved": "https://registry.npmjs.org/is-binary-path/-/is-binary-path-2.1.0.tgz",
      "integrity": "sha512-ZMERYes6pDydyuGidse7OsHxtbI7WVeUEozgR/g7rd0xUimYNlvZRE/K2MgZTjWy725IfelLeVcEM97mmtRGXw==",
      "dev": true,
      "dependencies": {
        "binary-extensions": "^2.0.0"
      },
      "engines": {
        "node": ">=8"
      }
    },
    "node_modules/is-core-module": {
      "version": "2.17.0",
      "resolved": "https://registry.npmjs.org/is-core-module/-/is-core-module-2.17.0.tgz",
      "integrity": "sha512-J/vG0zBCbIKOQFfufSwyXdMrsohyJIUNkrnmo6WZGzoM7tr/lsbfW5b2BvisL6zsyMzK9UxV9L6c7AoFbyXHOA==",
      "dev": true,
      "dependencies": {
        "hasown": "^2.0.4"
      },
      "engines": {
        "node": ">= 0.4"
      },
      "funding": {
        "url": "https://github.com/sponsors/ljharb"
      }
    },
    "node_modules/is-extglob": {
      "version": "2.1.1",
      "resolved": "https://registry.npmjs.org/is-extglob/-/is-extglob-2.1.1.tgz",
      "integrity": "sha512-SbKbANkN603Vi4jEZv49LeVJMn4yGwsbzZworEoyEiutsN3nJYdbO36zfhGJ6QEDpOZIFkDtnq5JRxmvl3jsoQ==",
      "dev": true,
      "engines": {
        "node": ">=0.10.0"
      }
    },
    "node_modules/is-glob": {
      "version": "4.0.3",
      "resolved": "https://registry.npmjs.org/is-glob/-/is-glob-4.0.3.tgz",
      "integrity": "sha512-xelSayHH36ZgE7ZWhli7pW34hNbNl8Ojv5KVmkJD4hBdD3th8Tfk9vYasLM+mXWOZhFkgZfxhLSnrwRr4elSSg==",
      "dev": true,
      "dependencies": {
        "is-extglob": "^2.1.1"
      },
      "engines": {
        "node": ">=0.10.0"
      }
    },
    "node_modules/is-number": {
      "version": "7.0.0",
      "resolved": "https://registry.npmjs.org/is-number/-/is-number-7.0.0.tgz",
      "integrity": "sha512-41Cifkg6e8TylSpdtTpeLVMqvSBEVzTttHvERD741+pnZ8ANv0004MRL43QKPDlK9cGvNp6NZWZUBlbGXYxxng==",
      "dev": true,
      "engines": {
        "node": ">=0.12.0"
      }
    },
    "node_modules/jiti": {
      "version": "1.21.7",
      "resolved": "https://registry.npmjs.org/jiti/-/jiti-1.21.7.tgz",
      "integrity": "sha512-/imKNG4EbWNrVjoNC/1H5/9GFy+tqjGBHCaSsN+P2RnPqjsLmv6UD3Ej+Kj8nBWaRAwyk7kK5ZUc+OEatnTR3A==",
      "dev": true,
      "bin": {
        "jiti": "bin/jiti.js"
      }
    },
    "node_modules/lilconfig": {
      "version": "3.1.3",
      "resolved": "https://registry.npmjs.org/lilconfig/-/lilconfig-3.1.3.tgz",
      "integrity": "sha512-/vlFKAoH5Cgt3Ie+JLhRbwOsCQePABiU3tJ1egGvyQ+33R/vcwM2Zl2QR/LzjsBeItPt3oSVXapn+m4nQDvpzw==",
      "dev": true,
      "engines": {
        "node": ">=14"
      },
      "funding": {
        "url": "https://github.com/sponsors/antonk52"
      }
    },
    "node_modules/lines-and-columns": {
      "version": "1.2.4",
      "resolved": "https://registry.npmjs.org/lines-and-columns/-/lines-and-columns-1.2.4.tgz",
      "integrity": "sha512-7ylylesZQ/PV29jhEDl3Ufjo6ZX7gCqJr5F7PKrqc93v7fzSymt1BpwEU8nAUXs8qzzvqhbjhK5QZg6Mt/HkBg==",
      "dev": true
    },
    "node_modules/merge2": {
      "version": "1.4.1",
      "resolved": "https://registry.npmjs.org/merge2/-/merge2-1.4.1.tgz",
      "integrity": "sha512-8q7VEgMJW4J8tcfVPy8g09NcQwZdbwFEqhe/WZkoIzjn/3TGDwtOCYtXGxA3O8tPzpczCCDgv+P2P5y00ZJOOg==",
      "dev": true,
      "engines": {
        "node": ">= 8"
      }
    },
    "node_modules/micromatch": {
      "version": "4.0.8",
      "resolved": "https://registry.npmjs.org/micromatch/-/micromatch-4.0.8.tgz",
      "integrity": "sha512-PXwfBhYu0hBCPw8Dn0E+WDYb7af3dSLVWKi3HGv84IdF4TyFoC0ysxFd0Goxw7nSv4T/PzEJQxsYsEiFCKo2BA==",
      "dev": true,
      "dependencies": {
        "braces": "^3.0.3",
        "picomatch": "^2.3.1"
      },
      "engines": {
        "node": ">=8.6"
      }
    },
    "node_modules/mz": {
      "version": "2.7.0",
      "resolved": "https://registry.npmjs.org/mz/-/mz-2.7.0.tgz",
      "integrity": "sha512-z81GNO7nnYMEhrGh9LeymoE4+Yr0Wn5McHIZMK5cfQCl+NDX08sCZgUc9/6MHni9IWuFLm1Z3HTCXu2z9fN62Q==",
      "dev": true,
      "dependencies": {
        "any-promise": "^1.0.0",
        "object-assign": "^4.0.1",
        "thenify-all": "^1.0.0"
      }
    },
    "node_modules/nanoid": {
      "version": "3.3.20",
      "resolved": "https://registry.npmjs.org/nanoid/-/nanoid-3.3.20.tgz",
      "integrity": "sha512-uKdg2G3GNCKQn9byYOpxbGqrT2fGO5KRt5J/8b3pok8rT6qxGWF6hxMyJiEYtAf+FVyYuD9hRaDqX5uPFYJ4ZQ==",
      "dev": true,
      "funding": [
        {
          "type": "github",
          "url": "https://github.com/sponsors/ai"
        }
      ],
      "bin": {
        "nanoid": "bin/nanoid.cjs"
      },
      "engines": {
        "node": "^10 || ^12 || ^13.7 || ^14 || >=15.0.1"
      }
    },
    "node_modules/normalize-path": {
      "version": "3.0.0",
      "resolved": "https://registry.npmjs.org/normalize-path/-/normalize-path-3.0.0.tgz",
      "integrity": "sha512-6eZs5Ls3WtCisHWp9S2GUy8dqkpGi4BVSz3GaqiE6ezub0512ESztXUwUB6C6IKbQkY2Pnb/mD4WYojCRwcwLA==",
      "dev": true,
      "engines": {
        "node": ">=0.10.0"
      }
    },
    "node_modules/object-assign": {
      "version": "4.1.1",
      "resolved": "https://registry.npmjs.org/object-assign/-/object-assign-4.1.1.tgz",
      "integrity": "sha512-rJgTQnkUnH1sFw8yT6VSU3zD3sWmu6sZhIseY8VX+GRu3P6F7Fu+JNDoXfklElbLJSnc3FUQHVe4cU5hj+BcUg==",
      "dev": true,
      "engines": {
        "node": ">=0.10.0"
      }
    },
    "node_modules/object-hash": {
      "version": "3.0.0",
      "resolved": "https://registry.npmjs.org/object-hash/-/object-hash-3.0.0.tgz",
      "integrity": "sha512-RSn9F68PjH9HqtltsSnqYC1XXoWe9Bju5+213R98cNGttag9q9yAOTzdbsqvIa7aNm5WffBZFpWYr2aWrklWAw==",
      "dev": true,
      "engines": {
        "node": ">= 6"
      }
    },
    "node_modules/path-parse": {
      "version": "1.0.7",
      "resolved": "https://registry.npmjs.org/path-parse/-/path-parse-1.0.7.tgz",
      "integrity": "sha512-LDJzPVEEEPR+y48z93A0Ed0yXb8pAByGWo/k5YYdYgpY2/2EsOsksJrq7lOHxryrVOn1ejG6oAp8ahvOIQD8sw==",
      "dev": true
    },
    "node_modules/picocolors": {
      "version": "1.1.1",
      "resolved": "https://registry.npmjs.org/picocolors/-/picocolors-1.1.1.tgz",
      "integrity": "sha512-xceH2snhtb5M9liqDsmEw56le376mTZkEX/jEb/RxNFyegNul7eNslCXP9FDj/Lcu0X8KEyMceP2ntpaHrDEVA==",
      "dev": true
    },
    "node_modules/picomatch": {
      "version": "2.3.2",
      "resolved": "https://registry.npmjs.org/picomatch/-/picomatch-2.3.2.tgz",
      "integrity": "sha512-V7+vQEJ06Z+c5tSye8S+nHUfI51xoXIXjHQ99cQtKUkQqqO1kO/KCJUfZXuB47h/YBlDhah2H3hdUGXn8ie0oA==",
      "dev": true,
      "engines": {
        "node": ">=8.6"
      },
      "funding": {
        "url": "https://github.com/sponsors/jonschlinkert"
      }
    },
    "node_modules/pirates": {
      "version": "4.0.7",
      "resolved": "https://registry.npmjs.org/pirates/-/pirates-4.0.7.tgz",
      "integrity": "sha512-TfySrs/5nm8fQJDcBDuUng3VOUKsd7S+zqvbOTiGXHfxX4wK31ard+hoNuvkicM/2YFzlpDgABOevKSsB4G/FA==",
      "dev": true,
      "engines": {
        "node": ">= 6"
      }
    },
    "node_modules/postcss": {
      "version": "8.5.29",
      "resolved": "https://registry.npmjs.org/postcss/-/postcss-8.5.29.tgz",
      "integrity": "sha512-49cGhUbXj8Qenv0iTMxA1cFBzxXoctpC9Ujd77t1WcbJIr6nF/eI7g/8MgxrYldFRuAXvja7xQRwavoW7kgrxQ==",
      "dev": true,
      "funding": [
        {
          "type": "opencollective",
          "url": "https://opencollective.com/postcss/"
        },
        {
          "type": "tidelift",
          "url": "https://tidelift.com/funding/github/npm/postcss"
        },
        {
          "type": "github",
          "url": "https://github.com/sponsors/ai"
        }
      ],
      "dependencies": {
        "nanoid": "^3.3.19",
        "picocolors": "^1.1.1",
        "source-map-js": "^1.2.2"
      },
      "engines": {
        "node": "^10 || ^12 || >=14"
      }
    },
    "node_modules/postcss-import": {
      "version": "15.1.0",
      "resolved": "https://registry.npmjs.org/postcss-import/-/postcss-import-15.1.0.tgz",
      "integrity": "sha512-hpr+J05B2FVYUAXHeK1YyI267J/dDDhMU6B6civm8hSY1jYJnBXxzKDKDswzJmtLHryrjhnDjqqp/49t8FALew==",
      "dev": true,
      "dependencies": {
        "postcss-value-parser": "^4.0.0",
        "read-cache": "^1.0.0",
        "resolve": "^1.1.7"
      },
      "engines": {
        "node": ">=14.0.0"
      },
      "peerDependencies": {
        "postcss": "^8.0.0"
      }
    },
    "node_modules/postcss-js": {
      "version": "4.1.0",
      "resolved": "https://registry.npmjs.org/postcss-js/-/postcss-js-4.1.0.tgz",
      "integrity": "sha512-oIAOTqgIo7q2EOwbhb8UalYePMvYoIeRY2YKntdpFQXNosSu3vLrniGgmH9OKs/qAkfoj5oB3le/7mINW1LCfw==",
      "dev": true,
      "funding": [
        {
          "type": "opencollective",
          "url": "https://opencollective.com/postcss/"
        },
        {
          "type": "github",
          "url": "https://github.com/sponsors/ai"
        }
      ],
      "dependencies": {
        "camelcase-css": "^2.0.1"
      },
      "engines": {
        "node": "^12 || ^14 || >= 16"
      },
      "peerDependencies": {
        "postcss": "^8.4.21"
      }
    },
    "node_modules/postcss-load-config": {
      "version": "4.0.2",
      "resolved": "https://registry.npmjs.org/postcss-load-config/-/postcss-load-config-4.0.2.tgz",
      "integrity": "sha512-bSVhyJGL00wMVoPUzAVAnbEoWyqRxkjv64tUl427SKnPrENtq6hJwUojroMz2VB+Q1edmi4IfrAPpami5VVgMQ==",
      "dev": true,
      "funding": [
        {
          "type": "opencollective",
          "url": "https://opencollective.com/postcss/"
        },
        {
          "type": "github",
          "url": "https://github.com/sponsors/ai"
        }
      ],
      "dependencies": {
        "lilconfig": "^3.0.0",
        "yaml": "^2.3.4"
      },
      "engines": {
        "node": ">= 14"
      },
      "peerDependencies": {
        "postcss": ">=8.0.9",
        "ts-node": ">=9.0.0"
      },
      "peerDependenciesMeta": {
        "postcss": {
          "optional": true
        },
        "ts-node": {
          "optional": true
        }
      }
    },
    "node_modules/postcss-nested": {
      "version": "6.2.0",
      "resolved": "https://registry.npmjs.org/postcss-nested/-/postcss-nested-6.2.0.tgz",
      "integrity": "sha512-HQbt28KulC5AJzG+cZtj9kvKB93CFCdLvog1WFLf1D+xmMvPGlBstkpTEZfK5+AN9hfJocyBFCNiqyS48bpgzQ==",
      "dev": true,
      "funding": [
        {
          "type": "opencollective",
          "url": "https://opencollective.com/postcss/"
        },
        {
          "type": "github",
          "url": "https://github.com/sponsors/ai"
        }
      ],
      "dependencies": {
        "postcss-selector-parser": "^6.1.1"
      },
      "engines": {
        "node": ">=12.0"
      },
      "peerDependencies": {
        "postcss": "^8.2.14"
      }
    },
    "node_modules/postcss-selector-parser": {
      "version": "6.1.4",
      "resolved": "https://registry.npmjs.org/postcss-selector-parser/-/postcss-selector-parser-6.1.4.tgz",
      "integrity": "sha512-bIoJLOmjCO1S9XdY/DcnR5hJxvrDir1PbGChrzXG3vw0/FOliy/fA3dmdhQ441kah4gKv+TwckGzex6wNS5cnQ==",
      "dev": true,
      "dependencies": {
        "cssesc": "^3.0.0",
        "util-deprecate": "^1.0.2"
      },
      "engines": {
        "node": ">=4"
      }
    },
    "node_modules/postcss-value-parser": {
      "version": "4.2.0",
      "resolved": "https://registry.npmjs.org/postcss-value-parser/-/postcss-value-parser-4.2.0.tgz",
      "integrity": "sha512-1NNCs6uurfkVbeXG4S8JFT9t19m45ICnif8zWLd5oPSZ50QnwMfK+H3jv408d4jw/7Bttv5axS5IiHoLaVNHeQ==",
      "dev": true
    },
    "node_modules/queue-microtask": {
      "version": "1.2.3",
      "resolved": "https://registry.npmjs.org/queue-microtask/-/queue-microtask-1.2.3.tgz",
      "integrity": "sha512-NuaNSa6flKT5JaSYQzJok04JzTL1CA6aGhv5rfLW3PgqA+M2ChpZQnAC8h8i4ZFkBS8X5RqkDBHA7r4hej3K9A==",
      "dev": true,
      "funding": [
        {
          "type": "github",
          "url": "https://github.com/sponsors/feross"
        },
        {
          "type": "patreon",
          "url": "https://www.patreon.com/feross"
        },
        {
          "type": "consulting",
          "url": "https://feross.org/support"
        }
      ]
    },
    "node_modules/read-cache": {
      "version": "1.0.2",
      "resolved": "https://registry.npmjs.org/read-cache/-/read-cache-1.0.2.tgz",
      "integrity": "sha512-/peqiBB/n07gQGLsWaHho3WfvUyRscw0gYTsEFMhrIe/nWLkYaf5SbKYjGYqtRV3aPwykJgF2VEMo1ac4bnsGA==",
      "dev": true
    },
    "node_modules/readdirp": {
      "version": "3.6.0",
      "resolved": "https://registry.npmjs.org/readdirp/-/readdirp-3.6.0.tgz",
      "integrity": "sha512-hOS089on8RduqdbhvQ5Z37A0ESjsqz6qnRcffsMU3495FuTdqSm+7bhJ29JvIOsBDEEnan5DPu9t3To9VRlMzA==",
      "dev": true,
      "dependencies": {
        "picomatch": "^2.2.1"
      },
      "engines": {
        "node": ">=8.10.0"
      }
    },
    "node_modules/resolve": {
      "version": "1.22.13",
      "resolved": "https://registry.npmjs.org/resolve/-/resolve-1.22.13.tgz",
      "integrity": "sha512-Dj/cW9zBV2aRj6XzCxuGIs3ESZKv02jixne3q6jFZYZ3/Tg09gHHviSMDL4kObh9pqSahdRHEnDoaP2gn8YLhA==",
      "dev": true,
      "dependencies": {
        "es-errors": "^1.3.0",
        "is-core-module": "^2.17.0",
        "path-parse": "^1.0.7",
        "supports-preserve-symlinks-flag": "^1.0.0"
      },
      "bin": {
        "resolve": "bin/resolve"
      },
      "engines": {
        "node": ">= 0.4"
      },
      "funding": {
        "url": "https://github.com/sponsors/ljharb"
      }
    },
    "node_modules/reusify": {
      "version": "1.1.0",
      "resolved": "https://registry.npmjs.org/reusify/-/reusify-1.1.0.tgz",
      "integrity": "sha512-g6QUff04oZpHs0eG5p83rFLhHeV00ug/Yf9nZM6fLeUrPguBTkTQOdpAWWspMh55TZfVQDPaN3NQJfbVRAxdIw==",
      "dev": true,
      "engines": {
        "iojs": ">=1.0.0",
        "node": ">=0.10.0"
      }
    },
    "node_modules/run-parallel": {
      "version": "1.2.0",
      "resolved": "https://registry.npmjs.org/run-parallel/-/run-parallel-1.2.0.tgz",
      "integrity": "sha512-5l4VyZR86LZ/lDxZTR6jqL8AFE2S0IFLMP26AbjsLVADxHdhB/c0GUsH+y39UfCi3dzz8OlQuPmnaJOMoDHQBA==",
      "dev": true,
      "funding": [
        {
          "type": "github",
          "url": "https://github.com/sponsors/feross"
        },
        {
          "type": "patreon",
          "url": "https://www.patreon.com/feross"
        },
        {
          "type": "consulting",
          "url": "https://feross.org/support"
        }
      ],
      "dependencies": {
        "queue-microtask": "^1.2.2"
      }
    },
    "node_modules/source-map-js": {
      "version": "1.2.2",
      "resolved": "https://registry.npmjs.org/source-map-js/-/source-map-js-1.2.2.tgz",
      "integrity": "sha512-KGj/8Y43x35aZVDtt+J4mK1hoLGHULMYfSkODJNQjNDC3oW1PqPoxMwo0pLUsWM/UEGzON/NxeHywEfNXNP3Vw==",
      "dev": true,
      "engines": {
        "node": ">=0.10.0"
      }
    },
    "node_modules/sucrase": {
      "version": "3.35.1",
      "resolved": "https://registry.npmjs.org/sucrase/-/sucrase-3.35.1.tgz",
      "integrity": "sha512-DhuTmvZWux4H1UOnWMB3sk0sbaCVOoQZjv8u1rDoTV0HTdGem9hkAZtl4JZy8P2z4Bg0nT+YMeOFyVr4zcG5Tw==",
      "dev": true,
      "dependencies": {
        "@jridgewell/gen-mapping": "^0.3.2",
        "commander": "^4.0.0",
        "lines-and-columns": "^1.1.6",
        "mz": "^2.7.0",
        "pirates": "^4.0.1",
        "tinyglobby": "^0.2.11",
        "ts-interface-checker": "^0.1.9"
      },
      "bin": {
        "sucrase": "bin/sucrase",
        "sucrase-node": "bin/sucrase-node"
      },
      "engines": {
        "node": ">=16 || 14 >=14.17"
      }
    },
    "node_modules/supports-preserve-symlinks-flag": {
      "version": "1.0.0",
      "resolved": "https://registry.npmjs.org/supports-preserve-symlinks-flag/-/supports-preserve-symlinks-flag-1.0.0.tgz",
      "integrity": "sha512-ot0WnXS9fgdkgIcePe6RHNk1WA8+muPa6cSjeR3V8K27q9BB1rTE3R1p7Hv0z1ZyAc8s6Vvv8DIyWf681MAt0w==",
      "dev": true,
      "engines": {
        "node": ">= 0.4"
      },
      "funding": {
        "url": "https://github.com/sponsors/ljharb"
      }
    },
    "node_modules/tailwindcss": {
      "version": "3.4.17",
      "resolved": "https://registry.npmjs.org/tailwindcss/-/tailwindcss-3.4.17.tgz",
      "integrity": "sha512-w33E2aCvSDP0tW9RZuNXadXlkHXqFzSkQew/aIa2i/Sj8fThxwovwlXHSPXTbAHwEIhBFXAedUhP2tueAKP8Og==",
      "dev": true,
      "dependencies": {
        "@alloc/quick-lru": "^5.2.0",
        "arg": "^5.0.2",
        "chokidar": "^3.6.0",
        "didyoumean": "^1.2.2",
        "dlv": "^1.1.3",
        "fast-glob": "^3.3.2",
        "glob-parent": "^6.0.2",
        "is-glob": "^4.0.3",
        "jiti": "^1.21.6",
        "lilconfig": "^3.1.3",
        "micromatch": "^4.0.8",
        "normalize-path": "^3.0.0",
        "object-hash": "^3.0.0",
        "picocolors": "^1.1.1",
        "postcss": "^8.4.47",
        "postcss-import": "^15.1.0",
        "postcss-js": "^4.0.1",
        "postcss-load-config": "^4.0.2",
        "postcss-nested": "^6.2.0",
        "postcss-selector-parser": "^6.1.2",
        "resolve": "^1.22.8",
        "sucrase": "^3.35.0"
      },
      "bin": {
        "tailwind": "lib/cli.js",
        "tailwindcss": "lib/cli.js"
      },
      "engines": {
        "node": ">=14.0.0"
      }
    },
    "node_modules/thenify": {
      "version": "3.3.1",
      "resolved": "https://registry.npmjs.org/thenify/-/thenify-3.3.1.tgz",
      "integrity": "sha512-RVZSIV5IG10Hk3enotrhvz0T9em6cyHBLkH/YAZuKqd8hRkKhSfCGIcP2KUY0EPxndzANBmNllzWPwak+bheSw==",
      "dev": true,
      "dependencies": {
        "any-promise": "^1.0.0"
      }
    },
    "node_modules/thenify-all": {
      "version": "1.6.0",
      "resolved": "https://registry.npmjs.org/thenify-all/-/thenify-all-1.6.0.tgz",
      "integrity": "sha512-RNxQH/qI8/t3thXJDwcstUO4zeqo64+Uy/+sNVRBx4Xn2OX+OZ9oP+iJnNFqplFra2ZUVeKCSa2oVWi3T4uVmA==",
      "dev": true,
      "dependencies": {
        "thenify": ">= 3.1.0 < 4"
      },
      "engines": {
        "node": ">=0.8"
      }
    },
    "node_modules/tinyglobby": {
      "version": "0.2.17",
      "resolved": "https://registry.npmjs.org/tinyglobby/-/tinyglobby-0.2.17.tgz",
      "integrity": "sha512-wXR/dYpcqKmfWpEdZjiKJOwCNFndD0DMnrW/cYjVGttEkBfVgcLFHoNrlj47mjOVic9yyNu65alsgF4NQyTa2g==",
      "dev": true,
      "dependencies": {
        "fdir": "^6.5.0",
        "picomatch": "^4.0.4"
      },
      "engines": {
        "node": ">=12.0.0"
      },
      "funding": {
        "url": "https://github.com/sponsors/SuperchupuDev"
      }
    },
    "node_modules/tinyglobby/node_modules/fdir": {
      "version": "6.5.0",
      "resolved": "https://registry.npmjs.org/fdir/-/fdir-6.5.0.tgz",
      "integrity": "sha512-tIbYtZbucOs0BRGqPJkshJUYdL+SDH7dVM8gjy+ERp3WAUjLEFJE+02kanyHtwjWOnwrKYBiwAmM0p4kLJAnXg==",
      "dev": true,
      "engines": {
        "node": ">=12.0.0"
      },
      "peerDependencies": {
        "picomatch": "^3 || ^4"
      },
      "peerDependenciesMeta": {
        "picomatch": {
          "optional": true
        }
      }
    },
    "node_modules/tinyglobby/node_modules/picomatch": {
      "version": "4.0.7",
      "resolved": "https://registry.npmjs.org/picomatch/-/picomatch-4.0.7.tgz",
      "integrity": "sha512-qcJu88Q2IWqJsDD529JKMdwGm/dvInW4HvQnRwiH9JtihJvzGOscDtHE3x1pBKeUOTysQ8kVmLnJ2kJu7yhcGA==",
      "dev": true,
      "engines": {
        "node": ">=12"
      },
      "funding": {
        "url": "https://github.com/sponsors/jonschlinkert"
      }
    },
    "node_modules/to-regex-range": {
      "version": "5.0.1",
      "resolved": "https://registry.npmjs.org/to-regex-range/-/to-regex-range-5.0.1.tgz",
      "integrity": "sha512-65P7iz6X5yEr1cwcgvQxbbIw7Uk3gOy5dIdtZ4rDveLqhrdJP+Li/Hx6tyK0NEb+2GCyneCMJiGqrADCSNk8sQ==",
      "dev": true,
      "dependencies": {
        "is-number": "^7.0.0"
      },
      "engines": {
        "node": ">=8.0"
      }
    },
    "node_modules/ts-interface-checker": {
      "version": "0.1.13",
      "resolved": "https://registry.npmjs.org/ts-interface-checker/-/ts-interface-checker-0.1.13.tgz",
      "integrity": "sha512-Y/arvbn+rrz3JCKl9C4kVNfTfSm2/mEp5FSz5EsZSANGPSlQrpRI5M4PKF+mJnE52jOO90PnPSc3Ur3bTQw0gA==",
      "dev": true
    },
    "node_modules/util-deprecate": {
      "version": "1.0.2",
      "resolved": "https://registry.npmjs.org/util-deprecate/-/util-deprecate-1.0.2.tgz",
      "integrity": "sha512-EPD5q1uXyFxJpCrLnCc1nHnq3gOa6DZBocAIiI2TaSCA7VCJ1UJDMagCzIkXNsUYfD1daK//LTEQ8xiIbrHtcw==",
      "dev": true
    },
    "node_modules/yaml": {
      "version": "2.9.1",
      "resolved": "https://registry.npmjs.org/yaml/-/yaml-2.9.1.tgz",
      "integrity": "sha512-3NxN8+78OdzbT7C/WjGsyfPAtJaN3FNDsWxv7Y7mcDsT/oOmgW8BpyQQFFBnvZE3j9Y2Sdz1ULFLezL7Eb2yFw==",
      "dev": true,
      "bin": {
        "yaml": "bin.mjs"
      },
      "engines": {
        "node": ">= 14.6"
      },
      "funding": {
        "url": "https://github.com/sponsors/eemeli"
      }
    }
  }
}


=================================
FILE: package.json
=================================

{
  "name": "shopify-collections-ai-studio-ui",
  "private": true,
  "scripts": {
    "build:css": "tailwindcss -i ./static/input.css -o ./static/styles.css --minify",
    "watch:css": "tailwindcss -i ./static/input.css -o ./static/styles.css --watch"
  },
  "devDependencies": {
    "tailwindcss": "3.4.17"
  }
}


=================================
FILE: pytest.ini
=================================

[pytest]
pythonpath = .
asyncio_default_fixture_loop_scope = function


=================================
FILE: requirements.txt
=================================

fastapi==0.115.12
uvicorn[standard]==0.34.2
httpx==0.28.1
python-dotenv==1.1.0
pydantic==2.11.4
pytest==8.3.5
pytest-asyncio==0.26.0


=================================
FILE: run_server.py
=================================

import os

import uvicorn
from dotenv import load_dotenv


load_dotenv()


if __name__ == "__main__":
    uvicorn.run("app:app", host=os.getenv("APP_HOST", "127.0.0.1"), port=int(os.getenv("APP_PORT", "8780")), reload=True)


=================================
FILE: shopify_operations.graphql
=================================

query CatalogProducts($after: String) {
  products(first: 100, after: $after) {
    nodes {
      id
      title
      descriptionHtml
      vendor
      productType
      updatedAt
      featuredMedia { preview { image { url } } }
      variants(first: 100) { nodes { id sku title } }
    }
    pageInfo { hasNextPage endCursor }
  }
}

query CatalogCollections($after: String) {
  collections(first: 100, after: $after) {
    nodes {
      id
      title
      handle
      updatedAt
      categoryPath: metafield(namespace: "custom", key: "category_path") { value }
      sources {
        __typename
        id
        ... on CollectionConditionsSource {
          targetType
          inclusion {
            conditions { __typename id }
            selections(first: 1) { nodes { product { id } } }
          }
        }
        ... on CollectionSubCollectionsSource { collections { id } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}

query CollectionProducts($id: ID!, $after: String) {
  collection(id: $id) {
    products(first: 250, after: $after) {
      nodes { id }
      pageInfo { hasNextPage endCursor }
    }
  }
}

query FreshProductCollections($id: ID!, $after: String) {
  product(id: $id) {
    id
    title
    vendor
    variants(first: 100) { nodes { sku title } }
    collections(first: 250, after: $after) {
      nodes {
        id
        title
        categoryPath: metafield(namespace: "custom", key: "category_path") { value }
        sources {
          __typename
          id
          ... on CollectionConditionsSource {
            inclusion { conditions { __typename id } }
          }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}

mutation CreateCollection($collection: CollectionCreateInput!) {
  collectionCreate(collection: $collection) {
    collection { id title sources { __typename id } }
    userErrors { field message }
  }
}

mutation UpdateCollectionSelections($collection: CollectionUpdateInput!) {
  collectionUpdate(collection: $collection) {
    collection { id }
    job { id done }
    userErrors { field message }
  }
}

mutation SetCollectionPath($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { ownerType key value }
    userErrors { field message code }
  }
}

query JobStatus($id: ID!) {
  job(id: $id) { id done }
}

mutation DeleteCollection($input: CollectionDeleteInput!) {
  collectionDelete(input: $input) {
    deletedCollectionId
    userErrors { field message }
  }
}


=================================
FILE: static/app.js
=================================

const state={items:[],selected:new Set(),offset:0,limit:60,total:0,settings:null,preview:null};
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const esc=(v='')=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const debounce=(fn,ms=250)=>{let id;return(...a)=>{clearTimeout(id);id=setTimeout(()=>fn(...a),ms)}};
async function api(url,opt={}){const r=await fetch(url,{...opt,headers:{'Content-Type':'application/json',...(opt.headers||{})}}),b=await r.json().catch(()=>({}));if(!r.ok)throw new Error(typeof b.detail==='string'?b.detail:JSON.stringify(b.detail||b));return b}
function notice(msg,error=false){const n=$('#notice');n.textContent=msg;n.classList.remove('hidden','border-blue-200','bg-blue-50','text-blue-900','border-rose-200','bg-rose-50','text-rose-900');n.classList.add(...(error?['border-rose-200','bg-rose-50','text-rose-900']:['border-blue-200','bg-blue-50','text-blue-900']))}
function statusLabel(s){return({to_analyze:'Da analizzare',queued:'In coda',processing:'In elaborazione',completed:'Elaborato',approved:'Categorie approvate',rejected:'Categorie rifiutate',published:'Pubblicato',error:'Errore'})[s]||s}
function statusClass(s){return s==='error'||s==='rejected'?'badge-rose':s==='processing'||s==='queued'?'badge-amber':'badge-blue'}
const pathText=p=>(p||[]).join('-');
function params(limit=state.limit,offset=state.offset){const p=new URLSearchParams({q:$('#search').value,category:$('#category').value,subcategory:$('#subcategory').value,vendor:$('#vendor').value,processing:$('#processing').value,limit,offset});if($('#uncategorized').checked)p.set('uncategorized','true');if($('#withProposal').checked)p.set('proposal','true');return p}
async function load(){try{const d=await api('/api/products?'+params());state.items=d.items;state.total=d.total;render()}catch(e){notice(e.message,true)}}
function render(){const g=$('#grid');g.innerHTML='';$('#resultCount').textContent=`${state.items.length} visualizzati · ${state.total} nel risultato`;$('#selectedCount').textContent=`${state.selected.size} selezionati`;$('#prev').disabled=state.offset===0;$('#next').disabled=state.offset+state.limit>=state.total;if(!state.items.length){g.innerHTML='<div class="panel col-span-full py-14 text-center text-slate-500">Nessun prodotto corrisponde ai filtri.</div>';return}for(const item of state.items){const card=document.createElement('article');card.className='product-card'+(state.selected.has(item.id)?' selected':'');const sku=item.variants.find(v=>v.sku)?.sku||'—',gen=item.latest_generation;const paths=item.paths.length?item.paths.map(p=>`<li class="path-pill"><span class="mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${p.kind==='smart'?'bg-amber-500':'bg-blue-500'}"></span><span>${esc(pathText(p.path))}</span>${p.kind==='smart'?'<span class="ml-auto text-[10px] uppercase text-amber-700">smart</span>':''}</li>`).join(''):'<li class="rounded-lg border border-dashed border-slate-300 px-3 py-2 text-sm text-slate-500">Senza categoria</li>';const proposal=gen?.status==='generated'?'<span class="badge badge-amber">Proposta aperta</span>':gen?.status==='approved'?'<span class="badge bg-emerald-100 text-emerald-800">Categorie approvate</span>':gen?.status==='rejected'?'<span class="badge badge-rose">Categorie rifiutate</span>':'';card.innerHTML=`<label class="absolute left-3 top-3 z-10 flex h-9 w-9 cursor-pointer items-center justify-center rounded-xl bg-white/95 shadow"><input class="h-5 w-5 rounded border-slate-300 text-blue-600" type="checkbox" ${state.selected.has(item.id)?'checked':''} aria-label="Seleziona ${esc(item.title)}"></label><div class="aspect-[4/3] overflow-hidden bg-slate-100">${item.image_url?`<img class="h-full w-full object-cover transition duration-300 hover:scale-105" loading="lazy" src="${esc(item.image_url)}" alt="${esc(item.title)}">`:'<div class="flex h-full items-center justify-center text-5xl text-slate-300">◇</div>'}</div><div class="p-5"><div class="mb-3 flex flex-wrap gap-2"><span class="badge ${statusClass(item.processing_status)}">${esc(statusLabel(item.processing_status))}</span>${proposal}</div><h3 class="line-clamp-2 min-h-12 text-base font-bold leading-6 text-slate-950">${esc(item.title)}</h3><p class="mt-1 text-xs text-slate-500">SKU ${esc(sku)} · ${esc(item.vendor||'Vendor —')}</p><ul class="mt-4 space-y-2">${paths}</ul><button class="detail mt-5 w-full rounded-xl border border-blue-200 bg-blue-50 px-4 py-2.5 text-sm font-bold text-blue-700 transition hover:bg-blue-100">Dettagli e storico →</button></div>`;card.querySelector('input').onchange=e=>{e.target.checked?state.selected.add(item.id):state.selected.delete(item.id);render()};card.querySelector('.detail').onclick=()=>openDetail(item.id);g.append(card)}}
async function loadFacets(){const category=$('#category').value,d=await api('/api/facets?category='+encodeURIComponent(category));const set=(el,values,label)=>{const previous=el.value;el.innerHTML=`<option value="">${label}</option>`+values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');if(values.includes(previous))el.value=previous};set($('#vendor'),d.vendors,'Tutti');set($('#category'),d.categories,'Tutte');set($('#subcategory'),d.subcategories,'Tutte');$('#subcategory').disabled=!category}
async function selectFiltered(){try{const d=await api('/api/products?'+params(500,0));d.items.forEach(i=>state.selected.add(i.id));render();if(d.total>500)notice('Selezionati i primi 500 prodotti filtrati. Restringi i filtri per elaborare gli altri.')}catch(e){notice(e.message,true)}}
async function generate(){if(!state.selected.size)return notice('Seleziona almeno un prodotto.',true);try{const r=await api('/api/jobs',{method:'POST',body:JSON.stringify({product_ids:[...state.selected]})});notice(`${r.queued} prodotti aggiunti alla coda di generazione.`);await load();await loadQueue()}catch(e){notice(e.message,true)}}
function historyPaths(v){return!v?.length?'<li>Nessun percorso</li>':v.map(i=>`<li>${esc(pathText(Array.isArray(i)?i:i.path||[]))}</li>`).join('')}
function proposalBox(p,index,status){const tone=status==='approved'?'border-emerald-300 bg-emerald-50':status==='rejected'?'border-rose-300 bg-rose-50':'border-slate-200 bg-white hover:border-blue-300';const input=status==='generated'?`<input class="path-approve mt-1 h-4 w-4 rounded text-blue-600" type="checkbox" value="${index}">`:`<span class="mt-1.5 h-3 w-3 shrink-0 rounded-full ${status==='approved'?'bg-emerald-500':'bg-rose-500'}"></span>`;return `<label class="block rounded-xl border p-3 ${tone} ${status==='generated'?'cursor-pointer':''}"><div class="flex gap-3">${input}<div><div class="font-bold text-slate-900">${esc(pathText(p.livelli))}</div><div class="mt-1 text-xs text-slate-600">Confidenza ${Math.round(p.confidence*100)}% · ${esc(p.motivazione)}</div></div></div></label>`}
function proposalLabel(s){return({generated:'Proposta da valutare',approved:'Categorie approvate',rejected:'Categorie rifiutate',published:'Categorie pubblicate'})[s]||'Proposta'}
async function openDetail(id){try{const d=await api('/api/products/'+encodeURIComponent(id)),product=d.product,gen=product.latest_generation,output=gen?.output||{},status=gen?.status||'';$('#detailTitle').textContent=product.title;let html=`<div class="grid gap-5 lg:grid-cols-2"><section class="compare-card"><p class="eyebrow">Attuale su Shopify</p><h3 class="mb-3 font-bold">Percorsi correnti</h3><ul class="space-y-2">${product.paths.length?product.paths.map(p=>`<li class="path-pill">${esc(pathText(p.path))}<span class="ml-auto badge ${p.kind==='smart'?'badge-amber':'badge-blue'}">${p.kind}</span></li>`).join(''):'<li class="text-sm text-slate-500">Nessun percorso</li>'}</ul></section><section class="compare-card"><p class="eyebrow">Proposta AI</p><div class="mb-3 flex items-center justify-between gap-3"><h3 class="font-bold">${gen?proposalLabel(status):'Nessuna proposta'}</h3>${gen?`<span class="badge ${status==='approved'?'bg-emerald-100 text-emerald-800':status==='rejected'?'badge-rose':'badge-amber'}">${proposalLabel(status)}</span>`:''}</div>`;if(gen&&output.percorsi?.length){html+=`<div class="space-y-3">${output.percorsi.map((p,i)=>proposalBox(p,i,status)).join('')}</div>`;if(status==='generated')html+=`<div class="mt-4 flex flex-wrap gap-2"><button class="approve btn-primary" data-id="${gen.id}">Approva selezionati</button><button class="reject btn-secondary" data-id="${gen.id}">Rifiuta proposta</button></div>`;if(status==='approved')html+=`<p class="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm font-semibold text-emerald-900">Le categorie selezionate sono approvate e pronte per il dry-run.</p><div class="mt-3"><button class="reject btn-secondary" data-id="${gen.id}">Rifiuta proposta</button></div>`;if(status==='rejected')html+='<p class="mt-4 rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm font-semibold text-rose-900">Categorie rifiutate. Genera una nuova proposta se desideri riclassificare il prodotto.</p>';if(status==='published')html+='<p class="mt-4 rounded-xl border border-blue-200 bg-blue-50 p-3 text-sm font-semibold text-blue-900">Categorie già pubblicate su Shopify.</p>';if(output.criticita?.length)html+=`<div class="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"><strong>Criticità</strong><ul class="mt-1 list-disc pl-5">${output.criticita.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`}else html+='<p class="text-sm text-slate-500">Nessuna proposta disponibile.</p>';html+='</section></div><section class="mt-6"><h3 class="mb-3 text-lg font-bold">Storico completo</h3><div class="space-y-3">';html+=d.history.length?d.history.map(h=>`<details class="rounded-xl border border-slate-200 bg-white p-4"><summary class="cursor-pointer font-semibold">${new Date(h.created_at).toLocaleString('it-IT')} · ${esc(h.origin)} · ${esc(h.outcome)}</summary><div class="mt-3 grid gap-4 text-sm sm:grid-cols-2"><div><strong>Prima</strong><ul class="mt-1 list-disc pl-5">${historyPaths(h.before)}</ul></div><div><strong>Dopo</strong><ul class="mt-1 list-disc pl-5">${historyPaths(h.after)}</ul></div></div>${h.model?`<p class="mt-3 text-xs text-slate-500">${esc(h.model)} · prompt v${h.prompt_version}</p>`:''}</details>`).join(''):'<p class="text-sm text-slate-500">Nessuno storico.</p>';html+='</div></section>';$('#detailBody').innerHTML=html;$('#detailDialog').showModal();const approve=$('.approve');if(approve)approve.onclick=async()=>{const indexes=$$('.path-approve:checked').map(input=>Number(input.value));if(!indexes.length)return notice('Seleziona almeno un percorso.',true);const r=await api(`/api/generations/${approve.dataset.id}/approve`,{method:'POST',body:JSON.stringify({indexes})});notice(r.changed?'Categorie approvate e storico aggiornato.':'Categorie già approvate: storico invariato.');await openDetail(id);await load()};const reject=$('.reject');if(reject)reject.onclick=async()=>{await api(`/api/generations/${reject.dataset.id}/reject`,{method:'POST'});notice('Categorie rifiutate.');await openDetail(id);await load()}}catch(e){notice(e.message,true)}}
function group(title,items,label,color='text-slate-800'){return `<section class="mb-5"><h3 class="mb-2 font-bold">${title} <span class="badge">${items.length}</span></h3>${items.length?`<ul class="space-y-2">${items.map(i=>`<li class="rounded-xl border border-slate-200 px-3 py-2 text-sm ${color}">${esc(label(i))}</li>`).join('')}</ul>`:'<p class="text-sm text-slate-400">Nessuna</p>'}</section>`}
async function previewPublish(){if(!state.selected.size)return notice('Seleziona almeno un prodotto.',true);try{state.preview=await api('/api/publish/preview',{method:'POST',body:JSON.stringify({product_ids:[...state.selected]})});const p=state.preview;$('#previewBody').innerHTML=`<p class="mb-4 text-sm text-slate-600">Controlla le operazioni. In questa fase non è stata effettuata alcuna scrittura.</p>${group('Collection da creare',p.create_collections,i=>pathText(i.path))}${group('Aggiunte prodotto',p.add_products,i=>`${pathText(i.path)} · ${i.product_ids.length} prodotti`)}${group('Rimozioni prodotto',p.remove_products,i=>`${pathText(i.path)} · ${i.product_ids.length} prodotti`)}${group('Operazioni bloccate',p.blocked,i=>`${i.path} · ${i.reason}`,'text-rose-700')}`;$('#confirmPublish').disabled=p.blocked.length>0;$('#previewDialog').showModal()}catch(e){notice(e.message,true)}}
async function executePublish(){if(!state.preview)return;const b=$('#confirmPublish');b.disabled=true;b.textContent='Applicazione…';try{const r=await api('/api/publish/execute',{method:'POST',body:JSON.stringify({preview_id:state.preview.preview_id,confirm:true})});$('#previewDialog').close();state.preview=null;notice(`${r.published_products} prodotti aggiornati · ${r.created_collections} collection create.`);await loadFacets();await load()}catch(e){notice(e.message,true)}finally{b.disabled=false;b.textContent='Conferma e applica'}}
async function previewCleanup(){try{notice('Sincronizzazione fresca prima della pulizia…');const r=await api('/api/cleanup/preview',{method:'POST'});$('#cleanupBody').innerHTML=r.collections.length?`<p class="mb-4 text-sm text-rose-700">Verranno eliminate solo le collection manuali ancora vuote dopo una sincronizzazione fresca. Le smart collection sono escluse. Sarà salvato un backup JSON.</p><div class="space-y-2">${r.collections.map(i=>`<label class="flex items-center gap-3 rounded-xl border border-rose-200 bg-rose-50 p-3"><input class="cleanup-choice h-4 w-4" type="checkbox" value="${esc(i.id)}" checked><span><strong>${esc(pathText(i.path))}</strong><small class="block text-rose-700">${esc(i.id)}</small></span></label>`).join('')}</div>`:'<p class="py-10 text-center text-slate-500">Nessuna collection manuale vuota.</p>';$('#confirmCleanup').disabled=!r.collections.length;$('#cleanupDialog').showModal()}catch(e){notice(e.message,true)}}
async function executeCleanup(){const ids=$$('.cleanup-choice:checked').map(i=>i.value);if(!ids.length)return notice('Seleziona almeno una collection.',true);if(prompt(`Seconda conferma: scrivi ELIMINA per rimuovere ${ids.length} collection manuali vuote.`)!=='ELIMINA')return;const b=$('#confirmCleanup');b.disabled=true;try{const r=await api('/api/cleanup/execute',{method:'POST',body:JSON.stringify({collection_ids:ids,confirm:true})});$('#cleanupDialog').close();notice(`${r.deleted.length} collection eliminate. Backup JSON salvato.`);await loadFacets();await load()}catch(e){notice(e.message,true)}finally{b.disabled=false}}
async function loadQueue(){try{const q=await api('/api/queue');$('#queue').innerHTML=`<strong class="text-slate-900">Coda AI</strong><span class="text-slate-500">${q.processing} in corso · ${q.queued} in attesa · ${q.completed} completati · ${q.error} errori</span>${q.current?'<span class="h-4 w-4 animate-spin rounded-full border-2 border-blue-200 border-t-blue-600"></span>':''}<span class="ml-auto"></span><button class="queue-toggle btn-secondary">${q.paused?'Riprendi':'Pausa'}</button><button class="queue-cancel btn-ghost">Annulla attesa</button>`;$('.queue-toggle').onclick=async()=>{await api('/api/queue/'+(q.paused?'resume':'pause'),{method:'POST'});loadQueue()};$('.queue-cancel').onclick=async()=>{await api('/api/queue/cancel',{method:'POST'});loadQueue();load()}}catch(e){notice(e.message,true)}}
async function openSettings(){try{state.settings=await api('/api/settings');for(const [k,v] of Object.entries(state.settings)){const input=$(`[name="${k}"]`);if(input&&typeof v!=='object')input.value=v}$('#settingsDialog').showModal()}catch(e){notice(e.message,true)}}
$('#settingsForm').onsubmit=async e=>{e.preventDefault();const form=new FormData(e.currentTarget),next={...state.settings};for(const k of ['ollama_url','model','ai_prompt'])next[k]=form.get(k);for(const k of ['timeout','max_retries','num_predict','max_depth'])next[k]=Number(form.get(k));for(const k of ['pause_seconds','temperature'])next[k]=Number(form.get(k));delete next.system_prompt;delete next.prompt_version;try{const r=await api('/api/settings',{method:'PUT',body:JSON.stringify(next)});$('#settingsDialog').close();notice(`Impostazioni salvate · prompt v${r.prompt_version}.`)}catch(x){notice(x.message,true)}};
$('#testOllama').onclick=async()=>{const o=$('#ollamaState');o.textContent='Connessione…';try{const r=await api('/api/ollama/test',{method:'POST'});$('#ollamaModels').innerHTML=r.models.map(x=>`<option value="${esc(x)}">`).join('');o.textContent=`${r.message} ${r.models.join(', ')}`}catch(e){o.textContent=e.message}};
$('#syncBtn').onclick=async e=>{e.currentTarget.disabled=true;notice('Sincronizzazione completa di prodotti e collection…');try{const r=await api('/api/sync',{method:'POST'});notice(`${r.products_total} prodotti · ${r.collections_new} collection nuove · ${r.collections_modified} modificate · ${r.collections_removed} rimosse.`);state.offset=0;await loadFacets();await load()}catch(x){notice(x.message,true)}finally{e.currentTarget.disabled=false}};
$('#category').onchange=async()=>{await loadFacets();state.offset=0;load()};['search','subcategory','vendor','processing','uncategorized','withProposal'].forEach(id=>$('#'+id).addEventListener('input',debounce(()=>{state.offset=0;load()})));$('#resetFilters').onclick=async()=>{$('#search').value='';$('#category').value='';$('#subcategory').value='';$('#vendor').value='';$('#processing').value='';$('#uncategorized').checked=false;$('#withProposal').checked=false;state.offset=0;await loadFacets();load()};
$('#selectFiltered').onclick=selectFiltered;$('#clearSelection').onclick=()=>{state.selected.clear();render()};$('#generateBtn').onclick=generate;$('#publishBtn').onclick=previewPublish;$('#confirmPublish').onclick=executePublish;$('#cleanupBtn').onclick=previewCleanup;$('#confirmCleanup').onclick=executeCleanup;$('#settingsBtn').onclick=openSettings;$('#prev').onclick=()=>{state.offset=Math.max(0,state.offset-state.limit);load()};$('#next').onclick=()=>{state.offset+=state.limit;load()};$$('.close-btn,.close-action').forEach(b=>b.onclick=()=>b.closest('dialog').close());loadFacets().then(load);loadQueue();setInterval(()=>{loadQueue();if(state.items.some(i=>['queued','processing'].includes(i.processing_status)))load()},2500);


=================================
FILE: static/index.html
=================================

<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Shopify Collections AI Studio</title>
  <meta name="description" content="Categorizzazione assistita con Shopify e Ollama locale">
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body class="min-h-screen bg-slate-50 text-slate-900 antialiased">
  <header class="border-b border-blue-100 bg-white/95 shadow-sm backdrop-blur">
    <div class="mx-auto flex max-w-screen-2xl flex-col gap-5 px-4 py-6 lg:flex-row lg:items-center lg:justify-between lg:px-8">
      <div>
        <div class="mb-1 flex items-center gap-2 text-sm font-semibold uppercase tracking-[.2em] text-blue-600"><span class="h-2 w-2 rounded-full bg-blue-600"></span>Catalog intelligence</div>
        <h1 class="text-2xl font-bold tracking-tight text-slate-950 sm:text-3xl">Shopify Collections <span class="text-blue-600">AI Studio</span></h1>
        <p class="mt-1 text-sm text-slate-500">Organizza il catalogo con categorie controllate e Ollama locale.</p>
      </div>
      <div class="flex flex-wrap gap-2">
        <button id="syncBtn" class="btn-secondary">Sincronizza</button>
        <button id="settingsBtn" class="btn-secondary">Impostazioni</button>
        <button id="cleanupBtn" class="btn-danger-soft">Pulisci vuote</button>
      </div>
    </div>
  </header>

  <main class="mx-auto max-w-screen-2xl px-4 py-6 lg:px-8">
    <div id="notice" role="status" aria-live="polite" class="mb-5 hidden rounded-xl border border-blue-200 bg-blue-50 px-4 py-3 text-sm font-medium text-blue-900"></div>

    <section class="panel mb-5" aria-labelledby="filtersTitle">
      <div class="mb-4 flex items-center justify-between">
        <h2 id="filtersTitle" class="text-base font-bold text-slate-900">Filtra il catalogo</h2>
        <button id="resetFilters" class="text-sm font-semibold text-blue-600 hover:text-blue-800">Azzera filtri</button>
      </div>
      <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-6">
        <label class="field xl:col-span-2"><span>Cerca</span><input id="search" class="control" type="search" placeholder="Titolo, SKU o ID"></label>
        <label class="field"><span>Categoria</span><select id="category" class="control"><option value="">Tutte</option></select></label>
        <label class="field"><span>Sottocategoria</span><select id="subcategory" class="control" disabled><option value="">Tutte</option></select></label>
        <label class="field"><span>Vendor</span><select id="vendor" class="control"><option value="">Tutti</option></select></label>
        <label class="field"><span>Stato</span><select id="processing" class="control"><option value="">Tutti</option><option value="to_analyze">Da analizzare</option><option value="queued">In coda</option><option value="processing">In elaborazione</option><option value="completed">Elaborato</option><option value="approved">Categorie approvate</option><option value="rejected">Categorie rifiutate</option><option value="published">Pubblicato</option><option value="error">Errore</option></select></label>
      </div>
      <div class="mt-4 flex flex-wrap gap-5">
        <label class="check"><input id="uncategorized" type="checkbox"> Senza categoria</label>
        <label class="check"><input id="withProposal" type="checkbox"> Con proposta aperta</label>
      </div>
    </section>

    <section class="sticky top-3 z-20 mb-5 rounded-2xl border border-blue-200 bg-white/95 p-3 shadow-lg shadow-blue-950/5 backdrop-blur" aria-label="Azioni catalogo">
      <div class="flex flex-col gap-3 lg:flex-row lg:items-center">
        <div class="flex flex-wrap items-center gap-2">
          <button id="selectFiltered" class="btn-secondary">Seleziona filtrati</button>
          <button id="clearSelection" class="btn-ghost">Deseleziona tutti</button>
          <span id="selectedCount" class="rounded-full bg-blue-100 px-3 py-1 text-sm font-bold text-blue-700">0 selezionati</span>
        </div>
        <div class="flex flex-wrap gap-2 lg:ml-auto">
          <button id="generateBtn" class="btn-primary">Genera categorie</button>
          <button id="publishBtn" class="btn-dark">Applica su Shopify</button>
        </div>
      </div>
    </section>

    <section id="queue" class="mb-5 flex min-h-14 flex-wrap items-center gap-3 rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm shadow-sm"></section>

    <div class="mb-4 flex items-end justify-between gap-4">
      <div><h2 class="text-xl font-bold">Prodotti</h2><p id="resultCount" class="text-sm text-slate-500"></p></div>
      <div class="flex gap-2"><button id="prev" class="btn-secondary" aria-label="Pagina precedente">←</button><button id="next" class="btn-secondary" aria-label="Pagina successiva">→</button></div>
    </div>
    <section id="grid" class="grid gap-5 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4" aria-live="polite"></section>
  </main>

  <dialog id="detailDialog" class="dialog-shell">
    <div class="dialog-card max-w-6xl">
      <div class="dialog-head"><div><p class="eyebrow">Scheda prodotto</p><h2 id="detailTitle" class="text-xl font-bold"></h2></div><button class="close-btn" aria-label="Chiudi">×</button></div>
      <div id="detailBody" class="max-h-[78vh] overflow-y-auto p-5"></div>
    </div>
  </dialog>

  <dialog id="previewDialog" class="dialog-shell">
    <div class="dialog-card max-w-3xl">
      <div class="dialog-head"><div><p class="eyebrow">Dry-run</p><h2 class="text-xl font-bold">Anteprima modifiche Shopify</h2></div><button class="close-btn" aria-label="Chiudi">×</button></div>
      <div id="previewBody" class="max-h-[70vh] overflow-y-auto p-5"></div>
      <div class="dialog-actions"><button class="btn-secondary close-action">Annulla</button><button id="confirmPublish" class="btn-primary">Conferma e applica</button></div>
    </div>
  </dialog>

  <dialog id="cleanupDialog" class="dialog-shell">
    <div class="dialog-card max-w-3xl">
      <div class="dialog-head"><div><p class="eyebrow text-rose-600">Operazione distruttiva</p><h2 class="text-xl font-bold">Collection manuali vuote</h2></div><button class="close-btn" aria-label="Chiudi">×</button></div>
      <div id="cleanupBody" class="max-h-[70vh] overflow-y-auto p-5"></div>
      <div class="dialog-actions"><button class="btn-secondary close-action">Annulla</button><button id="confirmCleanup" class="btn-danger">Elimina definitivamente</button></div>
    </div>
  </dialog>

  <dialog id="settingsDialog" class="dialog-shell">
    <form id="settingsForm" class="dialog-card max-w-2xl">
      <div class="dialog-head"><div><p class="eyebrow">Configurazione</p><h2 class="text-xl font-bold">Ollama e categorizzazione</h2></div><button type="button" class="close-btn" aria-label="Chiudi">×</button></div>
      <div class="grid max-h-[72vh] gap-4 overflow-y-auto p-5 sm:grid-cols-2">
        <label class="field sm:col-span-2"><span>Endpoint Ollama</span><input class="control" name="ollama_url" required></label>
        <label class="field"><span>Modello</span><input class="control" name="model" list="ollamaModels" required><datalist id="ollamaModels"></datalist></label>
        <label class="field"><span>Livelli massimi</span><input class="control" name="max_depth" type="number" min="1" max="3" readonly><small class="font-normal text-slate-500">Massimo 3, categoria principale inclusa.</small></label>
        <label class="field"><span>Timeout (secondi)</span><input class="control" name="timeout" type="number" min="10"></label>
        <label class="field"><span>Pausa tra job</span><input class="control" name="pause_seconds" type="number" min="0" step="0.5"></label>
        <label class="field"><span>Retry</span><input class="control" name="max_retries" type="number" min="0"></label>
        <label class="field"><span>Temperatura</span><input class="control" name="temperature" type="number" min="0" max="2" step="0.1"></label>
        <label class="field"><span>Token risposta</span><input class="control" name="num_predict" type="number" min="128"></label>
        <label class="field sm:col-span-2"><span>Prompt personalizzato</span><textarea class="control min-h-32" name="ai_prompt"></textarea></label>
        <div class="sm:col-span-2 flex items-center gap-3"><button id="testOllama" type="button" class="btn-secondary">Testa Ollama</button><span id="ollamaState" class="text-sm text-slate-500"></span></div>
      </div>
      <div class="dialog-actions"><button type="button" class="btn-secondary close-action">Annulla</button><button class="btn-primary">Salva impostazioni</button></div>
    </form>
  </dialog>
  <script src="/static/app.js" defer></script>
</body>
</html>


=================================
FILE: static/input.css
=================================

@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  :focus-visible { @apply outline-none ring-2 ring-blue-500 ring-offset-2; }
  dialog::backdrop { @apply bg-slate-950/55 backdrop-blur-sm; }
}

@layer components {
  .panel { @apply rounded-2xl border border-slate-200 bg-white p-5 shadow-sm; }
  .field { @apply flex flex-col gap-1.5 text-sm font-semibold text-slate-700; }
  .control { @apply w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 font-normal text-slate-900 shadow-sm transition placeholder:text-slate-400 hover:border-slate-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-100; }
  .check { @apply flex cursor-pointer items-center gap-2 text-sm font-medium text-slate-700; }
  .check input { @apply h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500; }
  .btn-primary { @apply inline-flex items-center justify-center rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-bold text-white shadow-sm transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50; }
  .btn-dark { @apply inline-flex items-center justify-center rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-bold text-white shadow-sm transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50; }
  .btn-secondary { @apply inline-flex items-center justify-center rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-sm font-bold text-slate-700 shadow-sm transition hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700 disabled:cursor-not-allowed disabled:opacity-50; }
  .btn-ghost { @apply inline-flex items-center justify-center rounded-xl px-3 py-2.5 text-sm font-bold text-slate-600 transition hover:bg-slate-100; }
  .btn-danger { @apply inline-flex items-center justify-center rounded-xl bg-rose-600 px-4 py-2.5 text-sm font-bold text-white transition hover:bg-rose-700 disabled:opacity-50; }
  .btn-danger-soft { @apply inline-flex items-center justify-center rounded-xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-sm font-bold text-rose-700 transition hover:bg-rose-100; }
  .product-card { @apply relative overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm transition hover:-translate-y-0.5 hover:border-blue-300 hover:shadow-lg hover:shadow-blue-950/5; }
  .product-card.selected { @apply border-blue-500 ring-2 ring-blue-100; }
  .badge { @apply inline-flex items-center rounded-full bg-slate-100 px-2.5 py-1 text-xs font-bold text-slate-600; }
  .badge-blue { @apply bg-blue-100 text-blue-700; }
  .badge-amber { @apply bg-amber-100 text-amber-800; }
  .badge-rose { @apply bg-rose-100 text-rose-700; }
  .path-pill { @apply flex items-start gap-2 rounded-lg border border-blue-100 bg-blue-50 px-3 py-2 text-sm font-semibold text-blue-900; }
  .dialog-shell { @apply m-auto w-[calc(100%-2rem)] max-w-none bg-transparent p-0; }
  .dialog-card { @apply mx-auto overflow-hidden rounded-2xl bg-white shadow-2xl; }
  .dialog-head { @apply flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4; }
  .dialog-actions { @apply flex justify-end gap-2 border-t border-slate-200 bg-slate-50 px-5 py-4; }
  .close-btn { @apply flex h-9 w-9 items-center justify-center rounded-full text-2xl text-slate-500 transition hover:bg-slate-100 hover:text-slate-900; }
  .eyebrow { @apply mb-1 text-xs font-bold uppercase tracking-[.16em] text-blue-600; }
  .compare-card { @apply rounded-2xl border border-slate-200 bg-slate-50 p-4; }
}


=================================
FILE: static/styles.css
=================================

*,:after,:before{--tw-border-spacing-x:0;--tw-border-spacing-y:0;--tw-translate-x:0;--tw-translate-y:0;--tw-rotate:0;--tw-skew-x:0;--tw-skew-y:0;--tw-scale-x:1;--tw-scale-y:1;--tw-pan-x: ;--tw-pan-y: ;--tw-pinch-zoom: ;--tw-scroll-snap-strictness:proximity;--tw-gradient-from-position: ;--tw-gradient-via-position: ;--tw-gradient-to-position: ;--tw-ordinal: ;--tw-slashed-zero: ;--tw-numeric-figure: ;--tw-numeric-spacing: ;--tw-numeric-fraction: ;--tw-ring-inset: ;--tw-ring-offset-width:0px;--tw-ring-offset-color:#fff;--tw-ring-color:rgba(59,130,246,.5);--tw-ring-offset-shadow:0 0 #0000;--tw-ring-shadow:0 0 #0000;--tw-shadow:0 0 #0000;--tw-shadow-colored:0 0 #0000;--tw-blur: ;--tw-brightness: ;--tw-contrast: ;--tw-grayscale: ;--tw-hue-rotate: ;--tw-invert: ;--tw-saturate: ;--tw-sepia: ;--tw-drop-shadow: ;--tw-backdrop-blur: ;--tw-backdrop-brightness: ;--tw-backdrop-contrast: ;--tw-backdrop-grayscale: ;--tw-backdrop-hue-rotate: ;--tw-backdrop-invert: ;--tw-backdrop-opacity: ;--tw-backdrop-saturate: ;--tw-backdrop-sepia: ;--tw-contain-size: ;--tw-contain-layout: ;--tw-contain-paint: ;--tw-contain-style: }::backdrop{--tw-border-spacing-x:0;--tw-border-spacing-y:0;--tw-translate-x:0;--tw-translate-y:0;--tw-rotate:0;--tw-skew-x:0;--tw-skew-y:0;--tw-scale-x:1;--tw-scale-y:1;--tw-pan-x: ;--tw-pan-y: ;--tw-pinch-zoom: ;--tw-scroll-snap-strictness:proximity;--tw-gradient-from-position: ;--tw-gradient-via-position: ;--tw-gradient-to-position: ;--tw-ordinal: ;--tw-slashed-zero: ;--tw-numeric-figure: ;--tw-numeric-spacing: ;--tw-numeric-fraction: ;--tw-ring-inset: ;--tw-ring-offset-width:0px;--tw-ring-offset-color:#fff;--tw-ring-color:rgba(59,130,246,.5);--tw-ring-offset-shadow:0 0 #0000;--tw-ring-shadow:0 0 #0000;--tw-shadow:0 0 #0000;--tw-shadow-colored:0 0 #0000;--tw-blur: ;--tw-brightness: ;--tw-contrast: ;--tw-grayscale: ;--tw-hue-rotate: ;--tw-invert: ;--tw-saturate: ;--tw-sepia: ;--tw-drop-shadow: ;--tw-backdrop-blur: ;--tw-backdrop-brightness: ;--tw-backdrop-contrast: ;--tw-backdrop-grayscale: ;--tw-backdrop-hue-rotate: ;--tw-backdrop-invert: ;--tw-backdrop-opacity: ;--tw-backdrop-saturate: ;--tw-backdrop-sepia: ;--tw-contain-size: ;--tw-contain-layout: ;--tw-contain-paint: ;--tw-contain-style: }/*! tailwindcss v3.4.17 | MIT License | https://tailwindcss.com*/*,:after,:before{box-sizing:border-box;border:0 solid #e5e7eb}:after,:before{--tw-content:""}:host,html{line-height:1.5;-webkit-text-size-adjust:100%;-moz-tab-size:4;-o-tab-size:4;tab-size:4;font-family:ui-sans-serif,system-ui,sans-serif,Apple Color Emoji,Segoe UI Emoji,Segoe UI Symbol,Noto Color Emoji;font-feature-settings:normal;font-variation-settings:normal;-webkit-tap-highlight-color:transparent}body{margin:0;line-height:inherit}hr{height:0;color:inherit;border-top-width:1px}abbr:where([title]){-webkit-text-decoration:underline dotted;text-decoration:underline dotted}h1,h2,h3,h4,h5,h6{font-size:inherit;font-weight:inherit}a{color:inherit;text-decoration:inherit}b,strong{font-weight:bolder}code,kbd,pre,samp{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,Liberation Mono,Courier New,monospace;font-feature-settings:normal;font-variation-settings:normal;font-size:1em}small{font-size:80%}sub,sup{font-size:75%;line-height:0;position:relative;vertical-align:baseline}sub{bottom:-.25em}sup{top:-.5em}table{text-indent:0;border-color:inherit;border-collapse:collapse}button,input,optgroup,select,textarea{font-family:inherit;font-feature-settings:inherit;font-variation-settings:inherit;font-size:100%;font-weight:inherit;line-height:inherit;letter-spacing:inherit;color:inherit;margin:0;padding:0}button,select{text-transform:none}button,input:where([type=button]),input:where([type=reset]),input:where([type=submit]){-webkit-appearance:button;background-color:transparent;background-image:none}:-moz-focusring{outline:auto}:-moz-ui-invalid{box-shadow:none}progress{vertical-align:baseline}::-webkit-inner-spin-button,::-webkit-outer-spin-button{height:auto}[type=search]{-webkit-appearance:textfield;outline-offset:-2px}::-webkit-search-decoration{-webkit-appearance:none}::-webkit-file-upload-button{-webkit-appearance:button;font:inherit}summary{display:list-item}blockquote,dd,dl,figure,h1,h2,h3,h4,h5,h6,hr,p,pre{margin:0}fieldset{margin:0}fieldset,legend{padding:0}menu,ol,ul{list-style:none;margin:0;padding:0}dialog{padding:0}textarea{resize:vertical}input::-moz-placeholder,textarea::-moz-placeholder{opacity:1;color:#9ca3af}input::placeholder,textarea::placeholder{opacity:1;color:#9ca3af}[role=button],button{cursor:pointer}:disabled{cursor:default}audio,canvas,embed,iframe,img,object,svg,video{display:block;vertical-align:middle}img,video{max-width:100%;height:auto}[hidden]:where(:not([hidden=until-found])){display:none}:focus-visible{outline:2px solid transparent;outline-offset:2px;--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(2px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(59 130 246/var(--tw-ring-opacity,1));--tw-ring-offset-width:2px}dialog::backdrop{background-color:rgba(2,6,23,.55);--tw-backdrop-blur:blur(4px);-webkit-backdrop-filter:var(--tw-backdrop-blur) var(--tw-backdrop-brightness) var(--tw-backdrop-contrast) var(--tw-backdrop-grayscale) var(--tw-backdrop-hue-rotate) var(--tw-backdrop-invert) var(--tw-backdrop-opacity) var(--tw-backdrop-saturate) var(--tw-backdrop-sepia);backdrop-filter:var(--tw-backdrop-blur) var(--tw-backdrop-brightness) var(--tw-backdrop-contrast) var(--tw-backdrop-grayscale) var(--tw-backdrop-hue-rotate) var(--tw-backdrop-invert) var(--tw-backdrop-opacity) var(--tw-backdrop-saturate) var(--tw-backdrop-sepia)}.panel{border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:1.25rem;--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.field{display:flex;flex-direction:column;gap:.375rem;font-size:.875rem;line-height:1.25rem;font-weight:600;--tw-text-opacity:1;color:rgb(51 65 85/var(--tw-text-opacity,1))}.control{width:100%;border-radius:.75rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:.625rem .75rem;font-weight:400;--tw-text-opacity:1;color:rgb(15 23 42/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.control::-moz-placeholder{--tw-text-opacity:1;color:rgb(148 163 184/var(--tw-text-opacity,1))}.control::placeholder{--tw-text-opacity:1;color:rgb(148 163 184/var(--tw-text-opacity,1))}.control:hover{--tw-border-opacity:1;border-color:rgb(148 163 184/var(--tw-border-opacity,1))}.control:focus{--tw-border-opacity:1;border-color:rgb(59 130 246/var(--tw-border-opacity,1));--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(2px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(219 234 254/var(--tw-ring-opacity,1))}.check{display:flex;cursor:pointer;align-items:center;gap:.5rem;font-size:.875rem;line-height:1.25rem;font-weight:500;--tw-text-opacity:1;color:rgb(51 65 85/var(--tw-text-opacity,1))}.check input{height:1rem;width:1rem;border-radius:.25rem;--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1));--tw-text-opacity:1;color:rgb(37 99 235/var(--tw-text-opacity,1))}.check input:focus{--tw-ring-opacity:1;--tw-ring-color:rgb(59 130 246/var(--tw-ring-opacity,1))}.btn-primary{display:inline-flex;align-items:center;justify-content:center;border-radius:.75rem;--tw-bg-opacity:1;background-color:rgb(37 99 235/var(--tw-bg-opacity,1));padding:.625rem 1rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(255 255 255/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.btn-primary:hover{--tw-bg-opacity:1;background-color:rgb(29 78 216/var(--tw-bg-opacity,1))}.btn-primary:disabled{cursor:not-allowed;opacity:.5}.btn-dark{display:inline-flex;align-items:center;justify-content:center;border-radius:.75rem;--tw-bg-opacity:1;background-color:rgb(15 23 42/var(--tw-bg-opacity,1));padding:.625rem 1rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(255 255 255/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.btn-dark:hover{--tw-bg-opacity:1;background-color:rgb(51 65 85/var(--tw-bg-opacity,1))}.btn-dark:disabled{cursor:not-allowed;opacity:.5}.btn-secondary{display:inline-flex;align-items:center;justify-content:center;border-radius:.75rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:.625rem 1rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(51 65 85/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.btn-secondary:hover{--tw-border-opacity:1;border-color:rgb(147 197 253/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(239 246 255/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(29 78 216/var(--tw-text-opacity,1))}.btn-secondary:disabled{cursor:not-allowed;opacity:.5}.btn-ghost{display:inline-flex;align-items:center;justify-content:center;border-radius:.75rem;padding:.625rem .75rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(71 85 105/var(--tw-text-opacity,1));transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.btn-ghost:hover{--tw-bg-opacity:1;background-color:rgb(241 245 249/var(--tw-bg-opacity,1))}.btn-danger{display:inline-flex;align-items:center;justify-content:center;border-radius:.75rem;--tw-bg-opacity:1;background-color:rgb(225 29 72/var(--tw-bg-opacity,1));padding:.625rem 1rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(255 255 255/var(--tw-text-opacity,1));transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.btn-danger:hover{--tw-bg-opacity:1;background-color:rgb(190 18 60/var(--tw-bg-opacity,1))}.btn-danger:disabled{opacity:.5}.btn-danger-soft{display:inline-flex;align-items:center;justify-content:center;border-radius:.75rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(254 205 211/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 241 242/var(--tw-bg-opacity,1));padding:.625rem 1rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(190 18 60/var(--tw-text-opacity,1));transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.btn-danger-soft:hover{--tw-bg-opacity:1;background-color:rgb(255 228 230/var(--tw-bg-opacity,1))}.product-card{position:relative;overflow:hidden;border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.product-card,.product-card:hover{box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.product-card:hover{--tw-translate-y:-0.125rem;transform:translate(var(--tw-translate-x),var(--tw-translate-y)) rotate(var(--tw-rotate)) skewX(var(--tw-skew-x)) skewY(var(--tw-skew-y)) scaleX(var(--tw-scale-x)) scaleY(var(--tw-scale-y));--tw-border-opacity:1;border-color:rgb(147 197 253/var(--tw-border-opacity,1));--tw-shadow:0 10px 15px -3px rgba(0,0,0,.1),0 4px 6px -4px rgba(0,0,0,.1);--tw-shadow-colored:0 10px 15px -3px var(--tw-shadow-color),0 4px 6px -4px var(--tw-shadow-color);--tw-shadow-color:rgba(23,37,84,.05);--tw-shadow:var(--tw-shadow-colored)}.product-card.selected{--tw-border-opacity:1;border-color:rgb(59 130 246/var(--tw-border-opacity,1));--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(2px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(219 234 254/var(--tw-ring-opacity,1))}.badge{display:inline-flex;align-items:center;border-radius:9999px;background-color:rgb(241 245 249/var(--tw-bg-opacity,1));padding:.25rem .625rem;font-size:.75rem;line-height:1rem;font-weight:700;color:rgb(71 85 105/var(--tw-text-opacity,1))}.badge,.badge-blue{--tw-bg-opacity:1;--tw-text-opacity:1}.badge-blue{background-color:rgb(219 234 254/var(--tw-bg-opacity,1));color:rgb(29 78 216/var(--tw-text-opacity,1))}.badge-amber{background-color:rgb(254 243 199/var(--tw-bg-opacity,1));color:rgb(146 64 14/var(--tw-text-opacity,1))}.badge-amber,.badge-rose{--tw-bg-opacity:1;--tw-text-opacity:1}.badge-rose{background-color:rgb(255 228 230/var(--tw-bg-opacity,1));color:rgb(190 18 60/var(--tw-text-opacity,1))}.path-pill{display:flex;align-items:flex-start;gap:.5rem;border-radius:.5rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(219 234 254/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(239 246 255/var(--tw-bg-opacity,1));padding:.5rem .75rem;font-size:.875rem;line-height:1.25rem;font-weight:600;--tw-text-opacity:1;color:rgb(30 58 138/var(--tw-text-opacity,1))}.dialog-shell{margin:auto;width:calc(100% - 2rem);max-width:none;background-color:transparent;padding:0}.dialog-card{margin-left:auto;margin-right:auto;overflow:hidden;border-radius:1rem;--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));--tw-shadow:0 25px 50px -12px rgba(0,0,0,.25);--tw-shadow-colored:0 25px 50px -12px var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.dialog-head{align-items:flex-start;justify-content:space-between;gap:1rem;border-bottom-width:1px;border-color:rgb(226 232 240/var(--tw-border-opacity,1))}.dialog-actions,.dialog-head{display:flex;--tw-border-opacity:1;padding:1rem 1.25rem}.dialog-actions{justify-content:flex-end;gap:.5rem;border-top-width:1px;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(248 250 252/var(--tw-bg-opacity,1))}.close-btn{display:flex;height:2.25rem;width:2.25rem;align-items:center;justify-content:center;border-radius:9999px;font-size:1.5rem;line-height:2rem;--tw-text-opacity:1;color:rgb(100 116 139/var(--tw-text-opacity,1));transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.close-btn:hover{--tw-bg-opacity:1;background-color:rgb(241 245 249/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(15 23 42/var(--tw-text-opacity,1))}.eyebrow{margin-bottom:.25rem;font-size:.75rem;line-height:1rem;font-weight:700;text-transform:uppercase;letter-spacing:.16em;--tw-text-opacity:1;color:rgb(37 99 235/var(--tw-text-opacity,1))}.compare-card{border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(248 250 252/var(--tw-bg-opacity,1));padding:1rem}.absolute{position:absolute}.sticky{position:sticky}.left-3{left:.75rem}.top-3{top:.75rem}.z-10{z-index:10}.z-20{z-index:20}.col-span-full{grid-column:1/-1}.mx-auto{margin-left:auto;margin-right:auto}.mb-1{margin-bottom:.25rem}.mb-2{margin-bottom:.5rem}.mb-3{margin-bottom:.75rem}.mb-4{margin-bottom:1rem}.mb-5{margin-bottom:1.25rem}.ml-auto{margin-left:auto}.mt-1{margin-top:.25rem}.mt-1\.5{margin-top:.375rem}.mt-3{margin-top:.75rem}.mt-4{margin-top:1rem}.mt-5{margin-top:1.25rem}.mt-6{margin-top:1.5rem}.line-clamp-2{overflow:hidden;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2}.block{display:block}.flex{display:flex}.grid{display:grid}.hidden{display:none}.aspect-\[4\/3\]{aspect-ratio:4/3}.h-1\.5{height:.375rem}.h-2{height:.5rem}.h-3{height:.75rem}.h-4{height:1rem}.h-5{height:1.25rem}.h-9{height:2.25rem}.h-full{height:100%}.max-h-\[70vh\]{max-height:70vh}.max-h-\[72vh\]{max-height:72vh}.max-h-\[78vh\]{max-height:78vh}.min-h-12{min-height:3rem}.min-h-14{min-height:3.5rem}.min-h-32{min-height:8rem}.min-h-screen{min-height:100vh}.w-1\.5{width:.375rem}.w-2{width:.5rem}.w-3{width:.75rem}.w-4{width:1rem}.w-5{width:1.25rem}.w-9{width:2.25rem}.w-full{width:100%}.max-w-2xl{max-width:42rem}.max-w-3xl{max-width:48rem}.max-w-6xl{max-width:72rem}.max-w-screen-2xl{max-width:1536px}.shrink-0{flex-shrink:0}@keyframes spin{to{transform:rotate(1turn)}}.animate-spin{animation:spin 1s linear infinite}.cursor-pointer{cursor:pointer}.list-disc{list-style-type:disc}.flex-col{flex-direction:column}.flex-wrap{flex-wrap:wrap}.items-end{align-items:flex-end}.items-center{align-items:center}.justify-center{justify-content:center}.justify-between{justify-content:space-between}.gap-2{gap:.5rem}.gap-3{gap:.75rem}.gap-4{gap:1rem}.gap-5{gap:1.25rem}.space-y-2>:not([hidden])~:not([hidden]){--tw-space-y-reverse:0;margin-top:calc(.5rem*(1 - var(--tw-space-y-reverse)));margin-bottom:calc(.5rem*var(--tw-space-y-reverse))}.space-y-3>:not([hidden])~:not([hidden]){--tw-space-y-reverse:0;margin-top:calc(.75rem*(1 - var(--tw-space-y-reverse)));margin-bottom:calc(.75rem*var(--tw-space-y-reverse))}.overflow-hidden{overflow:hidden}.overflow-y-auto{overflow-y:auto}.rounded{border-radius:.25rem}.rounded-2xl{border-radius:1rem}.rounded-full{border-radius:9999px}.rounded-lg{border-radius:.5rem}.rounded-xl{border-radius:.75rem}.border{border-width:1px}.border-2{border-width:2px}.border-b{border-bottom-width:1px}.border-dashed{border-style:dashed}.border-amber-200{--tw-border-opacity:1;border-color:rgb(253 230 138/var(--tw-border-opacity,1))}.border-blue-100{--tw-border-opacity:1;border-color:rgb(219 234 254/var(--tw-border-opacity,1))}.border-blue-200{--tw-border-opacity:1;border-color:rgb(191 219 254/var(--tw-border-opacity,1))}.border-emerald-200{--tw-border-opacity:1;border-color:rgb(167 243 208/var(--tw-border-opacity,1))}.border-emerald-300{--tw-border-opacity:1;border-color:rgb(110 231 183/var(--tw-border-opacity,1))}.border-rose-200{--tw-border-opacity:1;border-color:rgb(254 205 211/var(--tw-border-opacity,1))}.border-rose-300{--tw-border-opacity:1;border-color:rgb(253 164 175/var(--tw-border-opacity,1))}.border-slate-200{--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1))}.border-slate-300{--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1))}.border-t-blue-600{--tw-border-opacity:1;border-top-color:rgb(37 99 235/var(--tw-border-opacity,1))}.bg-amber-50{--tw-bg-opacity:1;background-color:rgb(255 251 235/var(--tw-bg-opacity,1))}.bg-amber-500{--tw-bg-opacity:1;background-color:rgb(245 158 11/var(--tw-bg-opacity,1))}.bg-blue-100{--tw-bg-opacity:1;background-color:rgb(219 234 254/var(--tw-bg-opacity,1))}.bg-blue-50{--tw-bg-opacity:1;background-color:rgb(239 246 255/var(--tw-bg-opacity,1))}.bg-blue-500{--tw-bg-opacity:1;background-color:rgb(59 130 246/var(--tw-bg-opacity,1))}.bg-blue-600{--tw-bg-opacity:1;background-color:rgb(37 99 235/var(--tw-bg-opacity,1))}.bg-emerald-100{--tw-bg-opacity:1;background-color:rgb(209 250 229/var(--tw-bg-opacity,1))}.bg-emerald-50{--tw-bg-opacity:1;background-color:rgb(236 253 245/var(--tw-bg-opacity,1))}.bg-emerald-500{--tw-bg-opacity:1;background-color:rgb(16 185 129/var(--tw-bg-opacity,1))}.bg-rose-50{--tw-bg-opacity:1;background-color:rgb(255 241 242/var(--tw-bg-opacity,1))}.bg-rose-500{--tw-bg-opacity:1;background-color:rgb(244 63 94/var(--tw-bg-opacity,1))}.bg-slate-100{--tw-bg-opacity:1;background-color:rgb(241 245 249/var(--tw-bg-opacity,1))}.bg-slate-50{--tw-bg-opacity:1;background-color:rgb(248 250 252/var(--tw-bg-opacity,1))}.bg-white{--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1))}.bg-white\/95{background-color:hsla(0,0%,100%,.95)}.object-cover{-o-object-fit:cover;object-fit:cover}.p-3{padding:.75rem}.p-4{padding:1rem}.p-5{padding:1.25rem}.px-3{padding-left:.75rem;padding-right:.75rem}.px-4{padding-left:1rem;padding-right:1rem}.py-1{padding-top:.25rem;padding-bottom:.25rem}.py-10{padding-top:2.5rem;padding-bottom:2.5rem}.py-14{padding-top:3.5rem;padding-bottom:3.5rem}.py-2{padding-top:.5rem;padding-bottom:.5rem}.py-2\.5{padding-top:.625rem;padding-bottom:.625rem}.py-3{padding-top:.75rem;padding-bottom:.75rem}.py-6{padding-top:1.5rem;padding-bottom:1.5rem}.pl-5{padding-left:1.25rem}.text-center{text-align:center}.text-2xl{font-size:1.5rem;line-height:2rem}.text-5xl{font-size:3rem;line-height:1}.text-\[10px\]{font-size:10px}.text-base{font-size:1rem;line-height:1.5rem}.text-lg{font-size:1.125rem;line-height:1.75rem}.text-sm{font-size:.875rem;line-height:1.25rem}.text-xl{font-size:1.25rem;line-height:1.75rem}.text-xs{font-size:.75rem;line-height:1rem}.font-bold{font-weight:700}.font-medium{font-weight:500}.font-normal{font-weight:400}.font-semibold{font-weight:600}.uppercase{text-transform:uppercase}.leading-6{line-height:1.5rem}.tracking-\[\.2em\]{letter-spacing:.2em}.tracking-tight{letter-spacing:-.025em}.text-amber-700{--tw-text-opacity:1;color:rgb(180 83 9/var(--tw-text-opacity,1))}.text-amber-900{--tw-text-opacity:1;color:rgb(120 53 15/var(--tw-text-opacity,1))}.text-blue-600{--tw-text-opacity:1;color:rgb(37 99 235/var(--tw-text-opacity,1))}.text-blue-700{--tw-text-opacity:1;color:rgb(29 78 216/var(--tw-text-opacity,1))}.text-blue-900{--tw-text-opacity:1;color:rgb(30 58 138/var(--tw-text-opacity,1))}.text-emerald-800{--tw-text-opacity:1;color:rgb(6 95 70/var(--tw-text-opacity,1))}.text-emerald-900{--tw-text-opacity:1;color:rgb(6 78 59/var(--tw-text-opacity,1))}.text-rose-600{--tw-text-opacity:1;color:rgb(225 29 72/var(--tw-text-opacity,1))}.text-rose-700{--tw-text-opacity:1;color:rgb(190 18 60/var(--tw-text-opacity,1))}.text-rose-900{--tw-text-opacity:1;color:rgb(136 19 55/var(--tw-text-opacity,1))}.text-slate-300{--tw-text-opacity:1;color:rgb(203 213 225/var(--tw-text-opacity,1))}.text-slate-400{--tw-text-opacity:1;color:rgb(148 163 184/var(--tw-text-opacity,1))}.text-slate-500{--tw-text-opacity:1;color:rgb(100 116 139/var(--tw-text-opacity,1))}.text-slate-600{--tw-text-opacity:1;color:rgb(71 85 105/var(--tw-text-opacity,1))}.text-slate-800{--tw-text-opacity:1;color:rgb(30 41 59/var(--tw-text-opacity,1))}.text-slate-900{--tw-text-opacity:1;color:rgb(15 23 42/var(--tw-text-opacity,1))}.text-slate-950{--tw-text-opacity:1;color:rgb(2 6 23/var(--tw-text-opacity,1))}.antialiased{-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}.shadow{--tw-shadow:0 1px 3px 0 rgba(0,0,0,.1),0 1px 2px -1px rgba(0,0,0,.1);--tw-shadow-colored:0 1px 3px 0 var(--tw-shadow-color),0 1px 2px -1px var(--tw-shadow-color)}.shadow,.shadow-lg{box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.shadow-lg{--tw-shadow:0 10px 15px -3px rgba(0,0,0,.1),0 4px 6px -4px rgba(0,0,0,.1);--tw-shadow-colored:0 10px 15px -3px var(--tw-shadow-color),0 4px 6px -4px var(--tw-shadow-color)}.shadow-sm{--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.shadow-blue-950\/5{--tw-shadow-color:rgba(23,37,84,.05);--tw-shadow:var(--tw-shadow-colored)}.backdrop-blur{--tw-backdrop-blur:blur(8px);-webkit-backdrop-filter:var(--tw-backdrop-blur) var(--tw-backdrop-brightness) var(--tw-backdrop-contrast) var(--tw-backdrop-grayscale) var(--tw-backdrop-hue-rotate) var(--tw-backdrop-invert) var(--tw-backdrop-opacity) var(--tw-backdrop-saturate) var(--tw-backdrop-sepia);backdrop-filter:var(--tw-backdrop-blur) var(--tw-backdrop-brightness) var(--tw-backdrop-contrast) var(--tw-backdrop-grayscale) var(--tw-backdrop-hue-rotate) var(--tw-backdrop-invert) var(--tw-backdrop-opacity) var(--tw-backdrop-saturate) var(--tw-backdrop-sepia)}.transition{transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.duration-300{transition-duration:.3s}.hover\:scale-105:hover{--tw-scale-x:1.05;--tw-scale-y:1.05;transform:translate(var(--tw-translate-x),var(--tw-translate-y)) rotate(var(--tw-rotate)) skewX(var(--tw-skew-x)) skewY(var(--tw-skew-y)) scaleX(var(--tw-scale-x)) scaleY(var(--tw-scale-y))}.hover\:border-blue-300:hover{--tw-border-opacity:1;border-color:rgb(147 197 253/var(--tw-border-opacity,1))}.hover\:bg-blue-100:hover{--tw-bg-opacity:1;background-color:rgb(219 234 254/var(--tw-bg-opacity,1))}.hover\:text-blue-800:hover{--tw-text-opacity:1;color:rgb(30 64 175/var(--tw-text-opacity,1))}@media (min-width:640px){.sm\:col-span-2{grid-column:span 2/span 2}.sm\:grid-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}.sm\:text-3xl{font-size:1.875rem;line-height:2.25rem}}@media (min-width:768px){.md\:grid-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (min-width:1024px){.lg\:ml-auto{margin-left:auto}.lg\:grid-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}.lg\:flex-row{flex-direction:row}.lg\:items-center{align-items:center}.lg\:justify-between{justify-content:space-between}.lg\:px-8{padding-left:2rem;padding-right:2rem}}@media (min-width:1280px){.xl\:col-span-2{grid-column:span 2/span 2}.xl\:grid-cols-3{grid-template-columns:repeat(3,minmax(0,1fr))}.xl\:grid-cols-6{grid-template-columns:repeat(6,minmax(0,1fr))}}@media (min-width:1536px){.\32xl\:grid-cols-4{grid-template-columns:repeat(4,minmax(0,1fr))}}

=================================
FILE: tailwind.config.js
=================================

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./static/**/*.{html,js}'],
  theme: { extend: {} },
  plugins: [],
};


=================================
FILE: tests/test_core.py
=================================

import asyncio
import json

import pytest

import app
from core import build_dry_run, collection_path, empty_manual_collections, normalize_ai_output, stable_hash


def product_fixture():
    return {
        "id": "gid://shopify/Product/1",
        "title": "Carburatore 125 cc",
        "descriptionHtml": "<p>Ricambio per pit bike.</p>",
        "vendor": "TonyShop",
        "productType": "Ricambi",
        "updatedAt": "2026-10-10T10:00:00Z",
        "featuredMedia": None,
        "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/1", "sku": "CARB-125", "title": "Default"}]},
    }


def collection_fixture(kind="manual"):
    return {
        "id": "gid://shopify/Collection/1",
        "title": "Carburatori",
        "handle": "carburatori",
        "updatedAt": "2026-10-10T10:00:00Z",
        "sources": ([{"__typename": "CollectionConditionsSource", "id": "source-1", "inclusion": {"conditions": [], "selections": {"nodes": []}}}] if kind == "manual" else [{"__typename": "CollectionConditionsSource", "id": "source-1", "inclusion": {"conditions": [{"__typename": "CollectionSourceInclusionConditionProductTag", "id": "condition-1"}], "selections": {"nodes": []}}}]),
        "categoryPath": {"value": "Motore > Alimentazione > Carburatori"},
        "product_ids": ["gid://shopify/Product/1"],
    }


def test_path_construction_prefers_metafield_and_normalizes_spaces():
    assert collection_path("Ignorato", " Motore  > Alimentazione >  Carburatori ") == ["Motore", "Alimentazione", "Carburatori"]
    assert collection_path("Motore > Pistoni", None) == ["Motore", "Pistoni"]


def test_ai_output_is_normalized_deduplicated_and_depth_limited():
    raw = {
        "percorsi": [
            {"livelli": [" Motore ", "Carburatori"], "confidence": 1.4, "motivazione": "Coerente"},
            {"livelli": ["motore", "carburatori"], "confidence": 0.8, "motivazione": "Duplicato"},
            {"livelli": ["A", "B", "C", "D"], "confidence": 0.4, "motivazione": "Troppo profondo"},
        ],
        "nuove_categorie": [["Motore", "Carburatori"]],
        "criticita": [],
    }
    result = normalize_ai_output(raw, 3)
    assert len(result["percorsi"]) == 1
    assert result["percorsi"][0]["confidence"] == 1
    assert "supera 3 livelli" in result["criticita"][0]


def test_settings_never_allow_more_than_three_levels():
    with pytest.raises(ValueError):
        app.Settings(max_depth=4)


@pytest.mark.asyncio
async def test_sync_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB", tmp_path / "sync.sqlite")
    app.init_db()

    async def products():
        return [product_fixture()]

    async def collections():
        return [collection_fixture()]

    monkeypatch.setattr(app, "fetch_all_products", products)
    monkeypatch.setattr(app, "fetch_all_collections", collections)
    first = await app.sync_shopify()
    second = await app.sync_shopify()
    assert first["collections_new"] == 1
    assert second == {"products_total": 1, "collections_new": 0, "collections_modified": 0, "collections_removed": 0}
    with app.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM product_snapshots").fetchone()[0] == 1


def test_dry_run_creates_adds_removes_and_blocks_smart():
    collections = [
        {"id": "manual-old", "source_id": "source-old", "path": ["Vecchia"], "kind": "manual", "removed": 0},
        {"id": "smart", "path": ["Automatica"], "kind": "smart", "removed": 0},
    ]
    products = [{"id": "p", "current": [{"id": "manual-old", "source_id": "source-old", "path": ["Vecchia"], "kind": "manual"}], "desired": [["Nuova"], ["Automatica"]]}]
    result = build_dry_run(products, collections)
    assert result["create_collections"] == [{"path": ["Nuova"], "title": "Nuova"}]
    assert result["remove_products"][0]["collection_id"] == "manual-old"
    assert result["blocked"][0]["reason"] == "Collection smart in sola lettura"


@pytest.mark.asyncio
async def test_publish_conflict_stops_before_mutations(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB", tmp_path / "conflict.sqlite")
    app.init_db()
    product_id = "gid://shopify/Product/1"
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES(?,?,?,?)", (product_id, "P", "x", app.now()))
        db.execute("INSERT INTO product_snapshots VALUES(?,?,?,?,?,?,?)", ("s", product_id, "{}", "[]", "expected", app.now(), "sync"))
        db.execute("INSERT INTO publish_previews VALUES(?,?,?,?,?,0)", ("preview", json.dumps([product_id]), json.dumps({"create_collections": [], "add_products": [], "remove_products": [], "blocked": []}), json.dumps({product_id: "expected"}), app.now()))

    async def changed(_):
        return [], "changed"

    called = False

    async def mutation(*_):
        nonlocal called
        called = True

    monkeypatch.setattr(app, "fresh_product_state", changed)
    monkeypatch.setattr(app, "mutation", mutation)
    with pytest.raises(app.HTTPException) as exc:
        await app.publish_execute({"preview_id": "preview", "confirm": True})
    assert exc.value.status_code == 409
    assert not called


@pytest.mark.asyncio
async def test_approval_is_idempotent_and_rejection_sets_product_status(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB", tmp_path / "approval.sqlite")
    app.init_db()
    output = {"percorsi": [{"livelli": ["Motore", "Carburatori"], "confidence": .9, "motivazione": "Coerente"}], "nuove_categorie": [], "criticita": []}
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES('p','P','x',?)", (app.now(),))
        db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,status,source_hash,created_at) VALUES('g','p',1,'m',?,'generated','x',?)", (json.dumps(output), app.now()))
    first = await app.approve("g", {"indexes": [0]})
    second = await app.approve("g", {"indexes": [0]})
    assert first["changed"] is True
    assert second["changed"] is False
    with app.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM history WHERE outcome='approved'").fetchone()[0] == 1
        assert db.execute("SELECT processing_status FROM products WHERE id='p'").fetchone()[0] == "approved"
    await app.reject("g")
    with app.connect() as db:
        assert db.execute("SELECT processing_status FROM products WHERE id='p'").fetchone()[0] == "rejected"


@pytest.mark.asyncio
async def test_new_collection_is_created_with_initial_membership_and_path_metafield(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB", tmp_path / "create.sqlite")
    app.init_db()
    product_id = "gid://shopify/Product/1"
    source_hash = "expected"
    operations = {"create_collections": [{"path": ["Motore", "Carburatori"], "title": "Carburatori"}], "add_products": [{"collection_id": None, "source_id": None, "path": ["Motore", "Carburatori"], "product_ids": [product_id]}], "remove_products": [], "blocked": []}
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES(?,?,?,?)", (product_id, "P", "x", app.now()))
        db.execute("INSERT INTO product_snapshots VALUES(?,?,?,?,?,?,?)", ("s", product_id, "{}", "[]", source_hash, app.now(), "sync"))
        db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,approved_paths_json,status,source_hash,created_at) VALUES('g',?,1,'m','{}',?,'approved',?,?)", (product_id, json.dumps([["Motore", "Carburatori"]]), source_hash, app.now()))
        db.execute("INSERT INTO publish_previews VALUES(?,?,?,?,?,0)", ("preview", json.dumps([product_id]), json.dumps(operations), json.dumps({product_id: source_hash}), app.now()))

    mutations = []
    async def fresh(_):
        return [], source_hash
    async def fake_mutation(query, variables, field):
        mutations.append((field, variables))
        if field == "collectionCreate":
            return {"collection": {"id": "gid://shopify/Collection/new", "sources": [{"__typename": "CollectionConditionsSource", "id": "gid://shopify/CollectionConditionsSource/new"}]}}
        return {"metafields": [], "userErrors": []}
    async def no_sync(*_):
        return {}
    monkeypatch.setattr(app, "fresh_product_state", fresh)
    monkeypatch.setattr(app, "mutation", fake_mutation)
    monkeypatch.setattr(app, "sync_shopify", no_sync)
    result = await app.publish_execute({"preview_id": "preview", "confirm": True})
    assert result["created_collections"] == 1
    create = next(variables for field, variables in mutations if field == "collectionCreate")
    selection = create["collection"]["sources"][0]["source"]["inclusion"]["selections"]
    assert selection == [{"productId": product_id}]
    metafield = next(variables for field, variables in mutations if field == "metafieldsSet")
    assert metafield["metafields"][0]["value"] == "Motore > Carburatori"


def test_cleanup_excludes_smart_and_nonempty_collections():
    items = [
        {"id": "m0", "kind": "manual", "products_count": 0, "removed": 0},
        {"id": "m1", "kind": "manual", "products_count": 1, "removed": 0},
        {"id": "s0", "kind": "smart", "products_count": 0, "removed": 0},
        {"id": "gone", "kind": "manual", "products_count": 0, "removed": 1},
    ]
    assert [item["id"] for item in empty_manual_collections(items)] == ["m0"]


@pytest.mark.asyncio
async def test_queue_is_strictly_sequential(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB", tmp_path / "queue.sqlite")
    app.init_db()
    active = peak = 0

    async def fake(_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return 0.02

    class TestSettings:
        pause_seconds = 0
        max_retries = 0

    monkeypatch.setattr(app, "process_job", fake)
    monkeypatch.setattr(app, "get_settings", lambda: TestSettings())
    with app.connect() as db:
        for index in range(2):
            product_id = f"p{index}"
            db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES(?,?,?,?)", (product_id, "P", "x", app.now()))
            db.execute("INSERT INTO jobs(id,product_id,kind,status,created_at) VALUES(?,?,'generate','queued',?)", (str(index), product_id, app.now()))
    task = asyncio.create_task(app.worker())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert peak == 1
    with app.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE status='completed'").fetchone()[0] == 2

