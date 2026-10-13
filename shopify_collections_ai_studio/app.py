
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
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

from core import match_collection_paths, build_dry_run, collection_path, empty_manual_collections, normalize_ai_output, path_key, path_label, plain, stable_hash


BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DB = DATA / "collections.sqlite"
LOG = DATA / "app.log"
DATA.mkdir(exist_ok=True)
load_dotenv(BASE / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.FileHandler(LOG), logging.StreamHandler()])
logger = logging.getLogger("shopify_collections_ai_studio")

LEGACY_DEFAULT_PROMPT = """Classifica il prodotto in uno o più percorsi chiari, specifici e utili alla navigazione. Non forzare il riuso delle categorie esistenti: usale solo se descrivono con precisione il prodotto, altrimenti proponi una nuova categoria coerente. Ogni percorso deve avere da 1 a 3 livelli complessivi. Mantieni nomi concisi, coerenti e in italiano."""
SYSTEM_PROMPT = """Sei un responsabile senior della tassonomia di un grande e-commerce di ricambi, accessori e veicoli per minimoto, pit bike, minicross, quad e scooter. Devi assegnare categorie merceologiche STABILI, RIUTILIZZABILI e UTILI ALLA NAVIGAZIONE, non trasformare ogni parola del titolo in una sottocategoria. Rispondi esclusivamente con JSON valido conforme allo schema richiesto.

GERARCHIA DI DECISIONE OBBLIGATORIA:
1. Identifica cosa viene realmente venduto: veicolo completo, ricambio, accessorio, abbigliamento/protezione o altro.
2. Identifica la famiglia merceologica principale e la funzione del prodotto.
3. Distingui la famiglia merceologica dalla compatibilità: marca, modello, colore, cilindrata, anno, motorizzazione e parole come racing sono normalmente attributi, non categorie.
4. Confronta il significato con le collection esistenti e con gli esempi di prodotti simili presenti nel catalogo.
5. Riutilizza il percorso canonico esistente quando rappresenta davvero la stessa famiglia merceologica; proponi un nuovo percorso soltanto se nessuno è semanticamente adeguato e la nuova distinzione migliora concretamente la navigazione.
6. Controlla che prodotti con la stessa funzione ricevano la stessa categoria, anche se cambiano veicolo compatibile, marca, modello o cilindrata.

REGOLE ANTI-PROLIFERAZIONE:
- La specificità del titolo NON è una ragione sufficiente per creare una sottocategoria.
- Preferisci il percorso più generale che classifica correttamente il prodotto. Tre livelli sono il limite massimo, non un obiettivo; usa uno o due livelli se bastano.
- Per impostazione predefinita restituisci un solo percorso principale. È però consentito usare due percorsi paralleli quando il catalogo Shopify dimostra che sono due rami di navigazione reali e utili (per esempio Ricambi > Accessori > Adesivi e Veicoli > Accessori > Adesivi, se entrambi esistono o sono coerenti con la tassonomia). In tal caso applica la stessa scelta a tutti i prodotti della stessa famiglia. Non creare entrambi i percorsi solo perché un accessorio è compatibile con un veicolo: il doppio percorso deve essere supportato dalla struttura del negozio, non dedotto automaticamente dal titolo.
- Non creare categorie quasi sinonime come "Adesivi e decalcomanie", "Adesivi e identificazione" e "Adesivi e protezioni" se la categoria canonica "Adesivi" è sufficiente.
- Un kit adesivi per quad, un kit adesivi per CRF 50 e adesivi per pit bike devono convergere nella stessa famiglia merceologica quando l'inventario conferma che sono tutti adesivi/accessori. Non differenziarli per il modello compatibile.
- Per un veicolo completo, classifica il veicolo in base al tipo (ad esempio Veicoli > Quad oppure Veicoli > Minicross); non creare automaticamente "Quad 300cc" o "Minicross 49cc".
- Per un ricambio o accessorio, classificalo secondo la sua natura/funzione sotto una famiglia coerente di ricambi/accessori. La compatibilità con un quad o una pit bike da sola non impone un ramo di categoria distinto.
- Gli attributi tecnici (2 tempi, 4 tempi, cilindrata, doppio scarico, racing) possono giustificare una categoria solo se identificano una collezione realmente utile e coerente con la tassonomia esistente; non devono moltiplicare le categorie automaticamente.
- Le collection esistenti sono una base informativa, NON un vincolo: non scegliere quella esistente più vicina per forza. Se tutte sono sbagliate o troppo specifiche, proponi un percorso nuovo più coerente.
- Non inventare marchi, compatibilità, caratteristiche o dati non presenti nella fonte. Se il caso è davvero incerto, descrivi una criticità invece di forzare la scelta.
- Usa nomi concisi, canonici, in italiano e coerenti con i nomi già presenti. Mantieni al massimo 3 livelli inclusa la categoria principale.
- `nuove_categorie` deve contenere soltanto percorsi davvero nuovi rispetto alle collection ricevute; non duplicare lì percorsi già esistenti.

ESEMPIO DI CONSISTENZA: "1 KIT ADESIVI QUAD RENEGADE 110", "1 Kit Adesivi Colarati CRF 50" e "Adesivi Arancione Nero Originali Per Pit Bike KAYO Ts90 Ts90r" hanno come concetto principale gli adesivi. Non inventare una diversa sottocategoria per ciascuno in base al veicolo/modello. Scegli percorsi canonici condivisi che siano coerenti con la tassonomia fornita: per esempio "Ricambi > Accessori > Adesivi" e, solo se è un ramo di navigazione reale e utile nel catalogo, anche "Veicoli > Accessori > Adesivi". Se entrambi i rami sono appropriati, restituiscili coerentemente per tutti i prodotti adesivi comparabili; altrimenti usa il solo percorso generale corretto. Se manca una categoria davvero equivalente, proponi un percorso nuovo e generale, non una variante iper-specifica.

Non confondere completezza con quantità: pochi percorsi corretti sono preferibili a molti percorsi ridondanti. Usa esclusivamente i dati sorgente ricevuti, assegna a ogni percorso confidence tra 0 e 1 e una motivazione breve. Ogni campo `motivazione` deve essere una singola frase concisa di massimo 250 caratteri; non superare mai 300 caratteri. Rispetta esattamente lo schema JSON."""
DEFAULT_PROMPT = """Classifica il prodotto per famiglia merceologica e funzione principale, non per ogni dettaglio del titolo. Prima identifica cosa viene venduto (veicolo completo, ricambio o accessorio), poi separa la categoria dalla compatibilità del prodotto. Analizza tutte le collection esistenti e gli esempi di prodotti simili per mantenere categorie canoniche coerenti sull'intero catalogo. Preferisci il percorso più generale che descrive correttamente il prodotto; usa meno di tre livelli quando bastano. Marca, modello, colore, cilindrata, motorizzazione e termini come racing sono normalmente attributi e non giustificano nuove categorie da soli. Prodotti della stessa famiglia funzionale devono ricevere lo stesso percorso anche se compatibili con veicoli differenti. Per accessori trasversali come gli adesivi, considera due percorsi paralleli (ad esempio Ricambi > Accessori > Adesivi e Veicoli > Accessori > Adesivi) solo se entrambi sono coerenti con la tassonomia e rappresentano punti di navigazione utili; se li usi, applicali coerentemente ai prodotti simili. Non creare sinonimi o percorsi ridondanti; aggiungi percorsi multipli solo quando hanno una reale utilità di navigazione. Le collection esistenti non sono un vincolo: se sono tutte inappropriate, proponi una nuova categoria canonica, non una variante iper-specifica. Evita collection con un solo prodotto quando la stessa famiglia può essere raccolta in una categoria esistente o generale. Nomi in italiano, chiari e stabili; massimo tre livelli complessivi. Se mancano prove sufficienti, segnala la criticità."""


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

    @field_validator("motivazione", mode="before")
    @classmethod
    def limit_motivazione(cls, value: Any) -> str:
        # Ollama/local models sometimes exceed the requested limit. Keep the useful
        # beginning of the explanation rather than rejecting an otherwise valid proposal.
        return str(value or "").strip()[:300]


class AIProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    percorsi: list[PathProposal]
    nuove_categorie: list[list[str]]
    criticita: list[str]


class Settings(BaseModel):
    ollama_url: HttpUrl | str = "http://127.0.0.1:11434"
    model: str = "llama3.2"
    # Local models may need several minutes to evaluate a full collection catalogue.
    # Keep a long default and allow operators to tune it from the settings dialog.
    timeout: int = Field(600, ge=10, le=3600)
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


class OllamaRequestTimeoutError(RuntimeError):
    """A retryable timeout while waiting for the local Ollama inference response."""
    pass


def ollama_http_timeout(settings: Settings, attempt: int = 1) -> httpx.Timeout:
    """Use a generous read timeout, increasing it for each queue retry.

    Ollama returns the complete non-streamed JSON only when inference finishes, so
    its read timeout must accommodate long local-model generations. Keep connect,
    write and pool timeouts independent from the inference read timeout.
    """
    retry_index = max(0, min(int(attempt) - 1, 4))
    read_timeout = min(3600.0, float(settings.timeout) * (1.0 + 0.5 * retry_index))
    return httpx.Timeout(connect=20.0, read=read_timeout, write=60.0, pool=20.0)


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
        stored_settings_row = db.execute("SELECT value_json FROM settings WHERE id=1").fetchone()
        stored_settings = loads(stored_settings_row[0], {}) if stored_settings_row else {}
        # Upgrade only the untouched built-in prompt. Never overwrite a merchant's custom prompt.
        settings_changed = False
        if stored_settings.get("ai_prompt") == LEGACY_DEFAULT_PROMPT:
            stored_settings["ai_prompt"] = DEFAULT_PROMPT
            settings_changed = True
        # 180 seconds was the previous application default. Upgrade that legacy value
        # to 600 seconds so existing databases benefit without requiring manual edits.
        # Any other value is treated as an explicit operator setting and preserved.
        if stored_settings.get("timeout") == 180:
            stored_settings["timeout"] = 600
            settings_changed = True
        if settings_changed:
            db.execute("UPDATE settings SET value_json=?,updated_at=? WHERE id=1", (dumps(stored_settings), now()))
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


SHOPIFY_MAX_RETRIES = 5
_shopify_graphql_lock = asyncio.Lock()


def _retry_after_seconds(response: httpx.Response | Any, attempt: int) -> float:
    """Honor Shopify Retry-After and otherwise use bounded exponential backoff."""
    raw = getattr(response, "headers", {}).get("Retry-After") if response is not None else None
    if raw:
        try:
            return max(0.1, min(float(raw), 60.0))
        except (TypeError, ValueError):
            pass
    return min(0.5 * (2 ** attempt), 8.0)


def _is_graphql_throttle(errors: list[dict[str, Any]]) -> bool:
    for error in errors:
        code = str((error.get("extensions") or {}).get("code", "")).upper()
        message = str(error.get("message", "")).casefold()
        if code in {"THROTTLED", "THROTTLE", "RATE_LIMITED"} or "throttl" in message or "rate limit" in message or "too many requests" in message:
            return True
    return False


async def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Call Shopify Admin GraphQL with serialized traffic and safe throttle retries.

    HTTP 429 and GraphQL THROTTLED responses are retried because Shopify rejects them
    before applying the requested operation. Mutations are deliberately not retried on
    ambiguous timeouts/5xx responses, which could otherwise create duplicate collections.
    The lock covers response processing and any necessary wait so a second request cannot
    bypass the bucket-restoration delay while the first call is sleeping.
    """
    is_mutation = bool(re.search(r"\bmutation\b", query, flags=re.IGNORECASE))
    last_error: Exception | None = None
    async with _shopify_graphql_lock:
        for attempt in range(SHOPIFY_MAX_RETRIES + 1):
            response: httpx.Response | None = None
            try:
                async with httpx.AsyncClient(timeout=90) as client:
                    token = await shop_token(client)
                    response = await client.post(
                        f"https://{shop_domain()}/admin/api/{os.getenv('SHOPIFY_API_VERSION', '2026-10')}/graphql.json",
                        headers={"X-Shopify-Access-Token": token},
                        json={"query": query, "variables": variables},
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if is_mutation or attempt >= SHOPIFY_MAX_RETRIES:
                    raise RuntimeError(
                        "Errore di connessione durante la chiamata Shopify. "
                        "Per sicurezza la mutazione non è stata ritentata automaticamente; verifica lo stato su Shopify prima di ripubblicare."
                        if is_mutation else f"Errore di connessione Shopify dopo i tentativi disponibili: {exc}"
                    ) from exc
                delay = min(0.5 * (2 ** attempt), 8.0)
                logger.warning("Shopify connection error; retry %s/%s in %.1fs", attempt + 1, SHOPIFY_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code == 429:
                last_error = RuntimeError("Shopify ha restituito HTTP 429 (throttling).")
                if attempt >= SHOPIFY_MAX_RETRIES:
                    raise RuntimeError(
                        f"Shopify continua a restituire HTTP 429 dopo {SHOPIFY_MAX_RETRIES} tentativi; nessuna ulteriore scrittura è stata eseguita."
                    )
                delay = _retry_after_seconds(response, attempt)
                logger.warning("Shopify HTTP 429; retry %s/%s in %.1fs", attempt + 1, SHOPIFY_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 500 and not is_mutation and attempt < SHOPIFY_MAX_RETRIES:
                delay = _retry_after_seconds(response, attempt)
                logger.warning("Shopify HTTP %s on query; retry %s/%s in %.1fs", response.status_code, attempt + 1, SHOPIFY_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            body = response.json()
            errors = body.get("errors") or []
            if errors:
                if _is_graphql_throttle(errors) and attempt < SHOPIFY_MAX_RETRIES:
                    cost = (body.get("extensions") or {}).get("cost", {})
                    throttle = cost.get("throttleStatus", {})
                    available = throttle.get("currentlyAvailable")
                    requested = cost.get("requestedQueryCost")
                    restore_rate = max(float(throttle.get("restoreRate", 50) or 50), 1.0)
                    if available is not None and requested is not None:
                        delay = max(0.25, min(max(float(requested) - float(available), 0) / restore_rate, 30.0))
                    else:
                        # Some throttled responses include only errors[].extensions.code.
                        delay = min(0.5 * (2 ** attempt), 8.0)
                    logger.warning("Shopify GraphQL throttled; retry %s/%s in %.1fs", attempt + 1, SHOPIFY_MAX_RETRIES, delay)
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(" | ".join(str(error.get("message", error)) for error in errors))

            cost = (body.get("extensions") or {}).get("cost", {})
            throttle = cost.get("throttleStatus", {})
            if throttle:
                available = float(throttle.get("currentlyAvailable", 1000) or 0)
                requested = float(cost.get("requestedQueryCost", 0) or 0)
                restore_rate = max(float(throttle.get("restoreRate", 50) or 50), 1.0)
                # Refill enough points for a query of similar cost before releasing the
                # lock. This is more accurate than a fixed pause that can be too short
                # for expensive collection/product pagination requests.
                if requested > 0 and available < requested:
                    delay = min((requested - available) / restore_rate, 30.0)
                    if delay > 0:
                        await asyncio.sleep(delay)
            if "data" not in body or body["data"] is None:
                raise RuntimeError("Shopify GraphQL non ha restituito il campo data.")
            return body["data"]

    raise RuntimeError(f"Chiamata Shopify fallita dopo i tentativi disponibili: {last_error or 'errore sconosciuto'}")


PRODUCTS_QUERY = """query CatalogProducts($after:String){products(first:100,after:$after){nodes{id title descriptionHtml vendor productType updatedAt featuredMedia{preview{image{url}}} variants(first:100){nodes{id sku title}}}pageInfo{hasNextPage endCursor}}}"""
COLLECTIONS_QUERY = """query CatalogCollections($after:String){collections(first:100,after:$after){nodes{id title handle updatedAt categoryPath:metafield(namespace:\"custom\",key:\"category_path\"){value} parentCollection:metafield(namespace:\"custom\",key:\"parent_collection\"){type value} sources{__typename id ... on CollectionConditionsSource{targetType inclusion{conditions{__typename id} selections(first:1){nodes{product{id}}}}} ... on CollectionSubCollectionsSource{collections{id}}}}pageInfo{hasNextPage endCursor}}}"""
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
    resolve_collection_hierarchy(items)
    return items


def resolve_collection_hierarchy(items: list[dict[str, Any]]) -> None:
    by_id = {item["id"]: item for item in items}
    visiting: set[str] = set()
    resolved: dict[str, list[str]] = {}

    def resolve(collection_id: str) -> list[str]:
        if collection_id in resolved:
            return resolved[collection_id]
        if collection_id in visiting:
            raise RuntimeError("Gerarchia collection ciclica: " + collection_id)
        if collection_id not in by_id:
            raise RuntimeError("Collection padre inesistente: " + collection_id)
        visiting.add(collection_id)
        item = by_id[collection_id]
        field = item.get("parentCollection")
        fallback = collection_path(item["title"], (item.get("categoryPath") or {}).get("value"))
        parents = []
        if field:
            field_type = field.get("type")
            if field_type == "collection_reference":
                parents = [field["value"]] if field.get("value") else []
            elif field_type == "list.collection_reference":
                parents = json.loads(field["value"])
                if not isinstance(parents, list) or any(not isinstance(parent, str) for parent in parents):
                    raise RuntimeError("Lista collection padre non valida: " + collection_id)
            else:
                raise RuntimeError("custom.parent_collection deve essere collection_reference o list.collection_reference")
        # A single canonical path cannot represent several distinct parents safely.
        if len(set(parents)) > 1:
            raise RuntimeError("Collection con più padri: seleziona un padre canonico prima di pubblicare: " + item["title"])
        if parents:
            path = resolve(parents[0]) + [fallback[-1]]
        else:
            path = fallback
        visiting.remove(collection_id)
        resolved[collection_id] = path
        item["resolved_path"] = path
        return path

    for collection_id in by_id:
        resolve(collection_id)


PARENT_DEFINITION_QUERY = """query ParentDefinition{metafieldDefinitions(first:10,ownerType:COLLECTION,namespace:"custom",key:"parent_collection"){nodes{type{name}}}}"""


async def parent_collection_type() -> str:
    nodes = (await graphql(PARENT_DEFINITION_QUERY, {}))["metafieldDefinitions"]["nodes"]
    if len(nodes) != 1 or nodes[0]["type"]["name"] not in {"collection_reference", "list.collection_reference"}:
        raise RuntimeError("Definizione custom.parent_collection assente o non supportata: nessuna scrittura eseguita.")
    return nodes[0]["type"]["name"]


def product_paths(db: sqlite3.Connection, product_id: str) -> list[dict[str, Any]]:
    rows = db.execute("""SELECT c.id,c.path_json,c.kind,c.source_id,c.title FROM collections c JOIN product_collections pc ON pc.collection_id=c.id WHERE pc.product_id=? AND c.removed=0 ORDER BY c.title COLLATE NOCASE""", (product_id,)).fetchall()
    return [{"id": row["id"], "path": loads(row["path_json"], []), "kind": row["kind"], "source_id": row["source_id"], "title": row["title"]} for row in rows]


_sync_lock = asyncio.Lock()
_publish_lock = asyncio.Lock()


async def sync_shopify(origin: str = "sync") -> dict[str, int]:
    async with _sync_lock:
        return await _sync_shopify(origin)


async def _sync_shopify(origin: str = "sync") -> dict[str, int]:
    products, collections = await asyncio.gather(fetch_all_products(), fetch_all_collections())
    resolve_collection_hierarchy(collections)
    synced_at = now()
    new_count = modified_count = 0
    remote_ids = {item["id"] for item in collections}
    with connect() as db:
        previous = {row["id"]: dict(row) for row in db.execute("SELECT * FROM collections")}
        for item in collections:
            path = item.get("resolved_path") or collection_path(item["title"], (item.get("categoryPath") or {}).get("value"))
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


_CORE_TYPE_TOKENS = {
    "adesivo", "adesivi", "decalcomania", "decalcomanie", "carburatore", "carburatori", "catena", "catene",
    "motore", "motori", "scarico", "scarichi", "freno", "freni", "pastiglia", "pastiglie", "pneumatico",
    "pneumatici", "camera", "camere", "manubrio", "manubri", "forcella", "forcelle", "ammortizzatore",
    "ammortizzatori", "filtro", "filtri", "candela", "candele", "pistone", "pistoni", "casco", "caschi",
    "guanto", "guanti", "plastiche", "plastica", "paramani", "pedana", "pedane", "batteria", "batterie",
    "carena", "carene", "parafango", "parafanghi", "frizione", "frizioni", "radiatore", "radiatori",
}

_TOKEN_STOPWORDS = {
    "per", "con", "da", "del", "della", "delle", "dei", "degli", "dello", "di", "a", "ad", "al", "alla", "alle", "ai", "agli", "allo",
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "the", "kit", "set", "tipo", "modello", "originale", "originali",
    "ricambio", "ricambi", "accessorio", "accessori", "nuovo", "nuova", "nuovi", "nuove", "nero", "nera", "neri", "nere", "bianco", "bianca",
    "rosso", "rossa", "blu", "arancione", "colorato", "colorata", "colorati", "colorate", "cc", "cm", "mm", "perno", "pezzo", "pezzi",
}
_TOKEN_RE = re.compile(r"[a-zA-ZÀ-ÿ0-9]+")


def _product_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return {
        token for token in _TOKEN_RE.findall(normalized)
        if len(token) >= 3 and token not in _TOKEN_STOPWORDS and not any(char.isdigit() for char in token)
    }


def _catalog_context(db: sqlite3.Connection, product_id: str, title: str, prompt_version: int | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build compact context from the full Shopify collection taxonomy and similar products.

    The AI sees every distinct active category path with its live synced product count.
    Exact duplicate paths are collapsed so they cannot bias the model into repeating the
    same category under multiple Shopify collection records. Product examples are attached
    only to the most relevant paths to keep the local model's context practical.
    """
    title_tokens = _product_tokens(title)
    rows = db.execute(
        "SELECT id,title,handle,path_json,kind,source_id,products_count FROM collections WHERE removed=0 ORDER BY title COLLATE NOCASE"
    ).fetchall()

    by_path: dict[str, sqlite3.Row] = {}
    path_frequency: dict[str, int] = {}
    for row in rows:
        path = loads(row["path_json"], [])
        if not path:
            continue
        key = path_key(path)
        path_frequency[key] = path_frequency.get(key, 0) + 1
        previous = by_path.get(key)
        def priority(candidate: sqlite3.Row) -> tuple[int, int, str]:
            editable = candidate["kind"] == "manual" and bool(candidate["source_id"])
            manual = candidate["kind"] == "manual"
            return (0 if editable else 1 if manual else 2, -int(candidate["products_count"] or 0), str(candidate["title"]).casefold())
        if previous is None or priority(row) < priority(previous):
            by_path[key] = row

    collections: list[dict[str, Any]] = []
    collection_scores: list[tuple[int, int, str]] = []
    for key, row in by_path.items():
        path = loads(row["path_json"], [])
        overlap = len(title_tokens & _product_tokens(" ".join([row["title"], *path])))
        collection_id = row["id"]
        entry: dict[str, Any] = {
            "percorso": path,
            "tipo": row["kind"],
            "prodotti_count": int(row["products_count"] or 0),
        }
        # Omit redundant text/IDs to keep the complete taxonomy compact for local models.
        if not path or str(row["title"]).casefold() != str(path[-1]).casefold():
            entry["titolo_shopify"] = row["title"]
        if path_frequency.get(key, 1) > 1:
            entry["duplicati_stesso_percorso"] = path_frequency[key]
        collections.append(entry)
        if overlap:
            collection_scores.append((overlap, int(row["products_count"] or 0), collection_id))
    collections.sort(key=lambda item: path_key(item["percorso"]))

    # Supply examples for the strongest matching candidates, without expanding every
    # collection into hundreds of product titles.
    example_by_id = {row["id"]: key for key, row in by_path.items()}
    collection_entry_by_path = {path_key(item["percorso"]): item for item in collections}
    for _, _, collection_id in sorted(collection_scores, key=lambda item: (-item[0], -item[1], item[2]))[:12]:
        sample = db.execute(
            "SELECT p.title FROM product_collections pc JOIN products p ON p.id=pc.product_id "
            "WHERE pc.collection_id=? AND p.id<>? ORDER BY p.title COLLATE NOCASE LIMIT 3",
            (collection_id, product_id),
        ).fetchall()
        key = example_by_id.get(collection_id)
        candidate = collection_entry_by_path.get(key) if key else None
        if candidate is not None and sample:
            candidate["esempi_prodotti"] = [item["title"] for item in sample]

    # Retrieve examples from the complete synced catalogue. Product/function nouns get
    # extra weight so a shared term such as "adesivi" outranks incidental vehicle matches.
    products = db.execute("SELECT id,title,vendor,product_type FROM products WHERE id<>?", (product_id,)).fetchall()
    similar_ranked: list[tuple[float, sqlite3.Row]] = []
    for item in products:
        tokens = _product_tokens(item["title"] or "")
        shared = title_tokens & tokens
        if not shared:
            continue
        weighted_intersection = sum(3.0 if token in _CORE_TYPE_TOKENS else 1.0 for token in shared)
        score = weighted_intersection / max(len(title_tokens | tokens) ** 0.5, 1.0)
        similar_ranked.append((score, item))
    related: list[dict[str, Any]] = []
    for _, item in sorted(similar_ranked, key=lambda pair: (-pair[0], str(pair[1]["title"]).casefold()))[:8]:
        related_paths = product_paths(db, item["id"])
        paths = [path["path"] for path in related_paths]
        # Newly generated/approved products may not yet have been published to Shopify.
        # Reuse their latest non-rejected proposal as a consistency hint for later jobs.
        if not paths:
            if prompt_version is None:
                generation = db.execute(
                    "SELECT status,output_json,approved_paths_json FROM generations "
                    "WHERE product_id=? AND status IN ('generated','approved','published') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (item["id"],),
                ).fetchone()
            else:
                generation = db.execute(
                    "SELECT status,output_json,approved_paths_json FROM generations "
                    "WHERE product_id=? AND prompt_version=? AND status IN ('generated','approved','published') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (item["id"], prompt_version),
                ).fetchone()
            if generation:
                approved = loads(generation["approved_paths_json"], None)
                if approved:
                    paths = approved
                else:
                    output = loads(generation["output_json"], {})
                    paths = [proposal.get("livelli", []) for proposal in output.get("percorsi", []) if proposal.get("livelli")]
        related.append({
            "titolo": item["title"],
            "vendor": item["vendor"],
            "product_type": item["product_type"],
            "percorsi_attuali_o_ultima_proposta": paths,
        })
    return collections, related


async def call_ollama(row: sqlite3.Row, settings: Settings, attempt: int = 1) -> dict[str, Any]:
    prompt_version = ensure_prompt_version()
    with connect() as db:
        current = product_paths(db, row["id"])
        categories, similar_products = _catalog_context(db, row["id"], row["title"], prompt_version)
    depth_limit = min(settings.max_depth, 3)
    source = {
        "titolo": row["title"],
        "descrizione": plain(row["description_html"]),
        "vendor": row["vendor"],
        "product_type": row["product_type"],
        "varianti": loads(row["variants_json"], []),
        "percorsi_attuali": [item["path"] for item in current],
        "catalogo_collection_esistenti": categories,
        "prodotti_simili_catalogo": similar_products,
        "regole_tassonomia": {
            "preferire_famiglia_merceologica": True,
            "marca_modello_cilindrata_sono_attributi_non_categorie_automatiche": True,
            "creare_collection_solo_se_semanticamente_necessario": True,
            "numero_percorsi_preferito": 1,
            "percorsi_paralleli_ammessi_se_gia_coerenti_col_catalogo": True,
        },
        "massimo_livelli": depth_limit,
    }
    prompt = f"{settings.ai_prompt}\n\nAnalizza l'intero elenco delle collection e gli eventuali prodotti simili qui sotto. Non limitarti a trovare una corrispondenza lessicale: confronta la funzione del prodotto, la gerarchia, i conteggi e gli esempi. Non scegliere una collection esistente solo per evitare di crearne una; non crearne una nuova solo perché il titolo è diverso. Mantieni un solo percorso principale salvo reale necessità.\nDati sorgente e tassonomia, unica fonte ammessa:\n{dumps({row['id']: source})}"
    payload = {
        "model": settings.model,
        "stream": False,
        "format": proposal_schema(row["id"]),
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        "options": {"temperature": settings.temperature, "num_predict": settings.num_predict},
    }
    await validate_ollama(settings)
    timeout = ollama_http_timeout(settings, attempt)
    read_timeout = timeout.read or settings.timeout
    logger.info(
        "Chiamata Ollama: prodotto=%s modello=%s tentativo=%s timeout_lettura=%ss prompt_chars=%s",
        row["id"], settings.model, attempt, int(read_timeout), len(prompt),
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{settings.ollama_url}/api/chat", json=payload)
            if response.status_code == 404:
                raise OllamaConfigurationError(f"Ollama non espone /api/chat oppure non trova il modello '{settings.model}'.")
            response.raise_for_status()
    except httpx.ReadTimeout as exc:
        raise OllamaRequestTimeoutError(
            f"Ollama non ha completato la risposta entro {int(read_timeout)} secondi "
            f"(tentativo {attempt}). Il job verrà ritentato con un timeout maggiore. "
            "Verifica che il PC che ospita Ollama non sia in sospensione e che modello/RAM siano sufficienti."
        ) from exc
    except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.WriteTimeout) as exc:
        raise OllamaRequestTimeoutError(
            f"Timeout di connessione/comunicazione con Ollama ({type(exc).__name__}) "
            f"all'indirizzo {settings.ollama_url}. Verifica rete ed endpoint."
        ) from exc
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
    proposal = await call_ollama(row, settings, attempt=int(job["attempts"]) + 1)
    generation_id = str(uuid.uuid4())
    version = ensure_prompt_version()
    with connect() as db:
        current_snapshot = db.execute("SELECT snapshot_hash FROM product_snapshots WHERE product_id=? ORDER BY created_at DESC LIMIT 1", (row["id"],)).fetchone()
        if not current_snapshot or current_snapshot["snapshot_hash"] != snapshot["snapshot_hash"]:
            raise RuntimeError("Shopify è cambiato durante la generazione: proposta scartata, il job verrà ritentato.")
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
                        if isinstance(exc, OllamaRequestTimeoutError):
                            # Give the remote/local inference process time to release resources;
                            # the next attempt also receives a longer read timeout.
                            retry_delay = min(120, 15 * (2 ** max(0, attempts - 1)))
                            logger.warning("Timeout Ollama: nuovo tentativo del job %s tra %s secondi", job["id"], retry_delay)
                        else:
                            retry_delay = min(60, 2**attempts)
                        db.execute("UPDATE jobs SET status='queued',error=?,next_attempt_at=? WHERE id=?", (str(exc), time.time() + retry_delay, job["id"]))
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
    approval = db.execute("SELECT * FROM generations WHERE product_id=? AND status IN ('approved','published') ORDER BY approved_at DESC,created_at DESC LIMIT 1", (row["id"],)).fetchone()
    item["latest_approved_generation"] = generation_payload(approval)
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
    await sync_shopify("sync")
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
    async with _publish_lock:
        return await _approve(generation_id, payload)


async def _approve(generation_id: str, payload: dict[str, Any]):
    indexes = {int(item) for item in payload.get("indexes", [])}
    with connect() as db:
        row = db.execute("SELECT * FROM generations WHERE id=?", (generation_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Proposta non trovata")
        latest = db.execute("SELECT id FROM generations WHERE product_id=? ORDER BY created_at DESC LIMIT 1", (row["product_id"],)).fetchone()
        if latest["id"] != generation_id:
            raise HTTPException(409, "Proposta superata: approva l'ultima generazione.")
        output = loads(row["output_json"], {})
        approved = [item["livelli"] for index, item in enumerate(output.get("percorsi", [])) if index in indexes]
        if not approved:
            raise HTTPException(400, "Approva almeno un percorso")
        db.execute("UPDATE generations SET status='superseded' WHERE product_id=? AND status='approved' AND id<>?", (row["product_id"], generation_id))
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
    async with _publish_lock:
        return await _reject(generation_id)


async def _reject(generation_id: str):
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
        generation = db.execute("SELECT * FROM generations WHERE product_id=? AND status='approved' ORDER BY approved_at DESC,created_at DESC LIMIT 1", (product_id,)).fetchone()
        if generation:
            result.append({"id": product_id, "desired": loads(generation["approved_paths_json"], []), "current": product_paths(db, product_id), "generation": dict(generation)})
    return result


@app.post("/api/publish/preview")
async def publish_preview(payload: dict[str, Any]):
    product_ids = list(dict.fromkeys(payload.get("product_ids", [])))[:500]
    await sync_shopify("sync")
    with connect() as db:
        products = approved_products(db, product_ids)
        if not products:
            raise HTTPException(400, "Nessuna proposta approvata per i prodotti selezionati")
        collections = [{**dict(row), "path": loads(row["path_json"], [])} for row in db.execute("SELECT * FROM collections")]
        try:
            collections = match_collection_paths(products, collections)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        operations = build_dry_run(products, collections)
        hierarchy_paths = {path_key(path[:depth]): path[:depth] for product in products for path in product["desired"] for depth in range(1, len(path) + 1)}
        operations["hierarchy_updates"] = [{"path": levels, "parent_path": levels[:-1]} for levels in hierarchy_paths.values()]
        operations["approval_signatures"] = {item["id"]: stable_hash({"id": item["generation"]["id"], "paths": item["desired"]}) for item in products}
        source_hashes = {item["id"]: item["generation"]["source_hash"] for item in products}
        preview_id = str(uuid.uuid4())
        db.execute("INSERT INTO publish_previews VALUES(?,?,?,?,?,0)", (preview_id, dumps([item["id"] for item in products]), dumps(operations), dumps(source_hashes), now()))
    return {"preview_id": preview_id, **operations}


async def fresh_product_state(product_id: str) -> tuple[list[dict[str, Any]], str]:
    # ATTENZIONE: la selezione delle varianti deve coincidere con PRODUCTS_QUERY
    # (nodes{id sku title}), altrimenti l'hash fresco non corrisponde mai a quello
    # salvato nella sync e publish_execute fallisce sempre con 409.
    query = """query FreshProductCollections($id:ID!,$after:String){product(id:$id){id title vendor variants(first:100){nodes{id sku title}}collections(first:250,after:$after){nodes{id title categoryPath:metafield(namespace:\"custom\",key:\"category_path\"){value} parentCollection:metafield(namespace:\"custom\",key:\"parent_collection\"){type value} sources{__typename id ... on CollectionConditionsSource{inclusion{conditions{__typename id}}}}}pageInfo{hasNextPage endCursor}}}}"""
    paths: list[dict[str, Any]] = []
    remote_collections: dict[str, dict[str, Any]] = {}
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
            remote_collections[item["id"]] = item
            resolved_path = collection_path(item["title"], (item.get("categoryPath") or {}).get("value"))
            paths.append({"id": item["id"], "path": resolved_path, "kind": "smart" if is_smart else "manual", "source_id": condition_sources[0]["id"] if not is_smart and condition_sources else None, "title": item["title"]})
        if not product["collections"]["pageInfo"]["hasNextPage"]:
            await hydrate_collection_parents(remote_collections)
            resolve_collection_hierarchy(list(remote_collections.values()))
            for entry in paths:
                entry["path"] = remote_collections[entry["id"]]["resolved_path"]
            paths.sort(key=lambda item: (path_key(item["path"]), item["id"]))
            return paths, snapshot_fingerprint(source, paths)
        after = product["collections"]["pageInfo"]["endCursor"]


PARENT_COLLECTION_QUERY = """query ParentCollection($id:ID!){collection(id:$id){id title categoryPath:metafield(namespace:"custom",key:"category_path"){value} parentCollection:metafield(namespace:"custom",key:"parent_collection"){type value}}}"""


async def hydrate_collection_parents(items: dict[str, dict[str, Any]]) -> None:
    pending = list(items.values())
    while pending:
        item = pending.pop()
        field = item.get("parentCollection") or {}
        parents = []
        if field.get("type") == "collection_reference" and field.get("value"):
            parents = [field["value"]]
        elif field.get("type") == "list.collection_reference":
            parents = json.loads(field["value"])
            if not isinstance(parents, list) or any(not isinstance(parent, str) for parent in parents):
                raise RuntimeError("Lista collection padre non valida")
        for parent_id in parents:
            if parent_id not in items:
                parent = (await graphql(PARENT_COLLECTION_QUERY, {"id": parent_id})).get("collection")
                if not parent:
                    raise RuntimeError("Collection padre inesistente: " + parent_id)
                items[parent_id] = parent
                pending.append(parent)


def chunks(items: list[str], size: int = 250):
    for start in range(0, len(items), size):
        yield items[start:start + size]


async def mutation(query: str, variables: dict[str, Any], field: str) -> dict[str, Any]:
    payload = (await graphql(query, variables))[field]
    errors = payload.get("userErrors", [])
    if errors:
        raise RuntimeError(" | ".join(error["message"] for error in errors))
    return payload


COLLECTION_BY_HANDLE = """query CollectionRecovery($handle:String!){collectionByHandle:collectionByIdentifier(identifier:{handle:$handle}){id title sources{__typename id ... on CollectionConditionsSource{inclusion{conditions{__typename id}}}}}}"""
CREATE_COLLECTION = """mutation CreateCollection($collection:CollectionCreateInput!){collectionCreate(collection:$collection){collection{id title sources{__typename id}}userErrors{field message}}}"""
UPDATE_SELECTIONS = """mutation UpdateCollectionSelections($collection:CollectionUpdateInput!){collectionUpdate(collection:$collection){collection{id}job{id done}userErrors{field message}}}"""
METAFIELDS_SET = """mutation SetCollectionPath($metafields:[MetafieldsSetInput!]!){metafieldsSet(metafields:$metafields){metafields{ownerType key value}userErrors{field message code}}}"""
JOB_QUERY = """query JobStatus($id:ID!){job(id:$id){id done}}"""
DELETE_COLLECTION = """mutation DeleteCollection($input:CollectionDeleteInput!){collectionDelete(input:$input){deletedCollectionId userErrors{field message}}}"""


async def wait_for_job(job: dict[str, Any] | None) -> None:
    if not job or job.get("done"):
        return
    for _ in range(120):
        await asyncio.sleep(0.5)
        current = (await graphql(JOB_QUERY, {"id": job["id"]})).get("job")
        if current and current.get("done"):
            return
    raise RuntimeError("Shopify non ha completato l'aggiornamento della collection entro 60 secondi; verifica lo stato della collection prima di ripetere la pubblicazione.")


@app.post("/api/publish/execute")
async def publish_execute(payload: dict[str, Any]):
    async with _publish_lock:
        try:
            return await _publish_execute(payload)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Pubblicazione interrotta; approvazione conservata")
            with connect() as db:
                preview = db.execute("SELECT product_ids_json FROM publish_previews WHERE id=?", (payload.get("preview_id"),)).fetchone()
                if preview:
                    for product_id in loads(preview["product_ids_json"], []):
                        db.execute("UPDATE products SET last_error=? WHERE id=?", (str(exc), product_id))
            try:
                await sync_shopify("recupero")
            except Exception:
                logger.exception("Sincronizzazione di recupero fallita; snapshot precedente conservato")
            raise HTTPException(502, f"Pubblicazione interrotta: {exc}. L'ultima approvazione è conservata; ripeti la pubblicazione per completare le operazioni mancanti.") from exc


async def _publish_execute(payload: dict[str, Any]):
    preview_id = payload.get("preview_id")
    if not payload.get("confirm"):
        raise HTTPException(400, "Conferma esplicita richiesta")
    with connect() as db:
        preview = db.execute("SELECT * FROM publish_previews WHERE id=? AND used=0", (preview_id,)).fetchone()
        if not preview:
            raise HTTPException(404, "Anteprima non trovata o già usata")
        product_ids = loads(preview["product_ids_json"], [])
        operations = loads(preview["operations_json"], {})
        desired_paths = {}
        for product_id in product_ids:
            generation = db.execute("SELECT approved_paths_json FROM generations WHERE product_id=? AND status='approved' ORDER BY approved_at DESC,created_at DESC LIMIT 1", (product_id,)).fetchone()
            if generation:
                desired_paths[product_id] = loads(generation["approved_paths_json"], [])
    if not desired_paths or set(desired_paths) != set(product_ids):
        raise HTTPException(409, "Approvazione mancante o revocata: crea una nuova anteprima.")
    if not operations.get("approval_signatures"):
        raise HTTPException(409, "Anteprima precedente alla nuova versione: crea una nuova anteprima.")
    await sync_shopify("sync")
    with connect() as db:
        products = approved_products(db, product_ids)
        signatures = {item["id"]: stable_hash({"id": item["generation"]["id"], "paths": item["desired"]}) for item in products}
        if signatures != operations.get("approval_signatures"):
            raise HTTPException(409, "Approvazioni cambiate o anteprima precedente alla nuova versione: crea una nuova anteprima.")
        collections = [{**dict(row), "path": loads(row["path_json"], [])} for row in db.execute("SELECT * FROM collections WHERE removed=0")]
        # Reconcile live memberships, including writes completed before a previous failure.
        for product in products:
            product["current"], _ = await fresh_product_state(product["id"])
        try:
            collections = match_collection_paths(products, collections)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        operations = build_dry_run(products, collections)
        before_paths = {item["id"]: [entry["path"] for entry in item["current"]] for item in products}
        generation_ids = {item["id"]: item["generation"]["id"] for item in products}
        desired_paths = {item["id"]: item["desired"] for item in products}
    if operations.get("blocked"):
        raise HTTPException(409, "Operazioni bloccate: " + dumps(operations["blocked"]))
    parent_type = await parent_collection_type()
    targets = {}
    for item in sorted(collections, key=lambda item: (-int(item.get("products_count", 0)), item["id"])):
        if item["kind"] == "manual" and item["source_id"]:
            targets.setdefault(path_key(item["path"]), {"collection_id": item["id"], "source_id": item["source_id"]})
    needed_paths = {path_key(path[:depth]) for product in products for path in product["desired"] for depth in range(1, len(path) + 1)}
    existing_paths = {path_key(item["path"]) for item in collections}
    if any(key in existing_paths and key not in targets for key in needed_paths):
        raise HTTPException(409, "Gerarchia con collection smart o senza sorgente manuale: nessuna scrittura eseguita.")
    initialized = set()
    created: dict[str, dict[str, str]] = {}
    pending_adds: dict[str, list[str]] = {}
    for item in operations["add_products"]:
        if item["collection_id"] is None:
            pending_adds[path_key(item["path"])] = item["product_ids"]
    for item in sorted(operations["create_collections"], key=lambda entry: (len(entry["path"]), path_key(entry["path"]))):
        product_ids = pending_adds.get(path_key(item["path"]), [])
        source = {"source": {"title": "Selezione manuale", "targetType": "PRODUCTS", "inclusion": {"selections": [{"productId": product_id} for product_id in product_ids[:250]]}}}
        # A deterministic handle lets recovery find a collection even if creation succeeded
        # but the response or subsequent metafield write was lost.
        handle = "category-" + stable_hash(path_key(item["path"]))[:24]
        lookup = await graphql(COLLECTION_BY_HANDLE, {"handle": handle})
        existing = lookup.get("collectionByHandle")
        if existing:
            result = {"collection": existing}
        else:
            initialized.add(path_key(item["path"]))
            result = await mutation(CREATE_COLLECTION, {"collection": {"title": item["title"], "handle": handle, "sources": [source]}}, "collectionCreate")
        sources = result["collection"].get("sources") or []
        if not sources or any(source["__typename"] != "CollectionConditionsSource" or (source.get("inclusion") or {}).get("conditions") for source in sources):
            raise RuntimeError("Collection di recupero non manuale: " + item["title"])
        condition_source = next(source for source in sources if source["__typename"] == "CollectionConditionsSource")
        collection_id = result["collection"]["id"]
        created[path_key(item["path"])] = {"collection_id": collection_id, "source_id": condition_source["id"]}
        targets[path_key(item["path"])] = created[path_key(item["path"])]
        metafield = {"ownerId": collection_id, "namespace": "custom", "key": "category_path", "type": "single_line_text_field", "value": path_label(item["path"])}
        await mutation(METAFIELDS_SET, {"metafields": [metafield]}, "metafieldsSet")
    for item in operations["add_products"]:
        target = {"collection_id": item["collection_id"], "source_id": item["source_id"]} if item["collection_id"] else created[path_key(item["path"])]
        already_created = 250 if path_key(item["path"]) in initialized else 0
        for batch in chunks(item["product_ids"][already_created:]):
            update = {"id": target["collection_id"], "sourcesToUpdate": [{"condition": {"id": target["source_id"], "inclusion": {"selectionsToAdd": [{"productId": product_id} for product_id in batch]}}}]}
            result = await mutation(UPDATE_SELECTIONS, {"collection": update}, "collectionUpdate")
            await wait_for_job(result.get("job"))
    # Persist canonical paths for reused legacy collections as well as new ones.
    for product in products:
        for desired in product["desired"]:
            for depth in range(1, len(desired) + 1):
                levels = desired[:depth]
                target = targets.get(path_key(levels))
                if target:
                    await mutation(METAFIELDS_SET, {"metafields": [{"ownerId": target["collection_id"], "namespace": "custom", "key": "category_path", "type": "single_line_text_field", "value": path_label(levels)}]}, "metafieldsSet")
    # Link every parent/child, including reused collections; never write labels as IDs.
    for product in products:
        for desired in product["desired"]:
            for depth in range(2, len(desired) + 1):
                child, parent = targets.get(path_key(desired[:depth])), targets.get(path_key(desired[:depth - 1]))
                if not child or not parent:
                    raise RuntimeError("Gerarchia non aggiornabile: " + path_label(desired[:depth]))
                value = parent["collection_id"] if parent_type == "collection_reference" else dumps([parent["collection_id"]])
                await mutation(METAFIELDS_SET, {"metafields": [{"ownerId": child["collection_id"], "namespace": "custom", "key": "parent_collection", "type": parent_type, "value": value}]}, "metafieldsSet")
    # Verify additions BEFORE removing the old categories.
    for product in products:
        required_ids = {targets[path_key(path[:depth])]["collection_id"] for path in product["desired"] for depth in range(1, len(path) + 1) if path_key(path[:depth]) in targets}
        for attempt in range(5):
            actual, _ = await fresh_product_state(product["id"])
            if required_ids <= {entry["id"] for entry in actual}:
                break
            if attempt < 4:
                await asyncio.sleep(0.5 * (attempt + 1))
        else:
            raise RuntimeError("Shopify non conferma le aggiunte: categorie precedenti conservate. Ripeti la pubblicazione.")
    for item in operations["remove_products"]:
        for batch in chunks(item["product_ids"]):
            update = {"id": item["collection_id"], "sourcesToUpdate": [{"condition": {"id": item["source_id"], "inclusion": {"selectionsToRemove": [{"productId": product_id} for product_id in batch]}}}]}
            result = await mutation(UPDATE_SELECTIONS, {"collection": update}, "collectionUpdate")
            await wait_for_job(result.get("job"))

    # Read-after-write verification: don't mark a proposal as published until Shopify
    # confirms both every approved path and every explicit collection membership in the dry-run.
    required_collection_targets: dict[str, list[tuple[str, str]]] = {}
    for operation in operations.get("add_products", []):
        if operation.get("collection_id"):
            target_collection_id = operation["collection_id"]
        else:
            created_target = created.get(path_key(operation["path"]))
            if not created_target:
                raise RuntimeError(f"Collection appena creata non trovata per il percorso {path_label(operation['path'])}.")
            target_collection_id = created_target["collection_id"]
        for product_id in operation.get("product_ids", []):
            required_collection_targets.setdefault(product_id, []).append((target_collection_id, path_label(operation["path"])))

    for product_id in product_ids:
        wanted_paths = desired_paths.get(product_id, [])
        required_keys = {path_key(path) for path in wanted_paths if path}
        required_targets = required_collection_targets.get(product_id, [])
        missing_labels: list[str] = []
        for attempt in range(5):
            actual_paths, _ = await fresh_product_state(product_id)
            actual_keys = {path_key(item["path"]) for item in actual_paths}
            actual_ids = {item["id"] for item in actual_paths}
            missing_keys = required_keys - actual_keys
            missing_targets = [(collection_id, label) for collection_id, label in required_targets if collection_id not in actual_ids]
            missing_labels = [path_label(path) for path in wanted_paths if path_key(path) in missing_keys]
            missing_labels.extend(label for _, label in missing_targets)
            missing_labels = list(dict.fromkeys(missing_labels))
            obsolete = [operation for operation in operations["remove_products"] if product_id in operation["product_ids"] and operation["collection_id"] in actual_ids]
            missing_labels.extend("Rimozione non confermata: " + path_label(operation["path"]) for operation in obsolete)
            if not missing_labels:
                break
            if attempt < 4:
                await asyncio.sleep(0.5 * (attempt + 1))
        if missing_labels:
            logger.error("Shopify publish verification failed for %s; missing memberships: %s", product_id, missing_labels)
            raise RuntimeError(
                f"Shopify non conferma ancora tutte le categorie per il prodotto {product_id}: "
                f"{', '.join(missing_labels)}. Nessuna proposta è stata marcata come pubblicata; sincronizza lo stato Shopify prima di riprovare."
            )

    # Refresh local membership only after Shopify confirms the writes; if sync fails,
    # keep the preview/generation unfinalized instead of reporting a false success.
    await sync_shopify("pubblicazione")
    with connect() as db:
        db.execute("UPDATE publish_previews SET used=1 WHERE id=?", (preview_id,))
        for product_id in product_ids:
            generation = db.execute("SELECT * FROM generations WHERE product_id=? AND status='approved' ORDER BY approved_at DESC,created_at DESC LIMIT 1", (product_id,)).fetchone()
            if generation and generation["id"] == generation_ids[product_id]:
                before = before_paths.get(product_id, [])
                after = [item["path"] for item in product_paths(db, product_id)]
                db.execute("UPDATE generations SET status='published',published_at=? WHERE id=?", (now(), generation["id"]))
                db.execute("UPDATE products SET processing_status='published',last_error=NULL WHERE id=?", (product_id,))
                db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), product_id, "pubblicazione", dumps(before), dumps(after), generation["model"], generation["prompt_version"], "success", None, now()))
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
