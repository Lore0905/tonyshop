
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

from core import collection_priority, reuse_canonical_categories, match_collection_paths, build_dry_run, collection_path, empty_manual_collections, normalize_ai_output, path_key, path_label, plain, stable_hash


BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DB = DATA / "collections.sqlite"
LOG = DATA / "app.log"
DATA.mkdir(exist_ok=True)
load_dotenv(BASE / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.FileHandler(LOG), logging.StreamHandler()])
logger = logging.getLogger("shopify_collections_ai_studio")
logging.getLogger("httpx").setLevel(logging.WARNING)

LEGACY_DEFAULT_PROMPT = """Classifica il prodotto in uno o più percorsi chiari, specifici e utili alla navigazione. Non forzare il riuso delle categorie esistenti: usale solo se descrivono con precisione il prodotto, altrimenti proponi una nuova categoria coerente. Ogni percorso deve avere da 1 a 3 livelli complessivi. Mantieni nomi concisi, coerenti e in italiano."""
SYSTEM_PROMPT = """Sei un responsabile senior della tassonomia di un grande e-commerce di ricambi, accessori e veicoli per minimoto, pit bike, minicross, quad e scooter. Devi assegnare categorie merceologiche STABILI, RIUTILIZZABILI e UTILI ALLA NAVIGAZIONE, non trasformare ogni parola del titolo in una sottocategoria. Rispondi esclusivamente con JSON valido conforme allo schema richiesto.

GERARCHIA DI DECISIONE OBBLIGATORIA:
1. Identifica cosa viene realmente venduto: veicolo completo, ricambio, accessorio, abbigliamento/protezione o altro.
2. Identifica la famiglia merceologica principale e la funzione del prodotto.
3. Distingui la famiglia merceologica dalla compatibilità: marca, modello, colore, cilindrata, anno, motorizzazione e parole come racing sono normalmente attributi, non categorie.
4. Confronta il significato con le collection esistenti e con gli esempi di prodotti simili presenti nel catalogo.
5. La prima scelta è SEMPRE il riuso esatto di un percorso Shopify o già approvato quando copre la famiglia e la funzione del prodotto. Copia esattamente i livelli, senza rinominare, aggiungere sinonimi o inserire attributi. Una nuova collection è una scelta eccezionale: ammessa solo se manca una famiglia o funzione realmente diversa.
6. Prima di proporre una nuova categoria confrontala con TUTTI i percorsi esistenti, inclusi i parent generali: in motivazione indica quale categoria vicina hai valutato e quale differenza merceologica concreta impedisce il riuso. Una differenza di nome, marca, modello, colore o cilindrata non è sufficiente.
7. Controlla che prodotti con la stessa funzione ricevano la stessa categoria, anche se cambiano veicolo compatibile, marca, modello o cilindrata.

REGOLE ANTI-PROLIFERAZIONE:
- La specificità del titolo NON è una ragione sufficiente per creare una sottocategoria.
- Preferisci il percorso più generale che classifica correttamente il prodotto. Tre livelli sono il limite massimo, non un obiettivo; usa uno o due livelli se bastano.
- Per impostazione predefinita restituisci un solo percorso principale. È però consentito usare due percorsi paralleli quando il catalogo Shopify dimostra che sono due rami di navigazione reali e utili (per esempio Ricambi > Accessori > Adesivi e Veicoli > Accessori > Adesivi, se entrambi esistono o sono coerenti con la tassonomia). In tal caso applica la stessa scelta a tutti i prodotti della stessa famiglia. Non creare entrambi i percorsi solo perché un accessorio è compatibile con un veicolo: il doppio percorso deve essere supportato dalla struttura del negozio, non dedotto automaticamente dal titolo.
- Non creare categorie quasi sinonime come "Adesivi e decalcomanie", "Adesivi e identificazione" e "Adesivi e protezioni" se la categoria canonica "Adesivi" è sufficiente.
- Un kit adesivi per quad, un kit adesivi per CRF 50 e adesivi per pit bike devono convergere nella stessa famiglia merceologica quando l'inventario conferma che sono tutti adesivi/accessori. Non differenziarli per il modello compatibile.
- Per un veicolo completo, classifica il veicolo in base al tipo (ad esempio Veicoli > Quad oppure Veicoli > Minicross); non creare automaticamente "Quad 300cc" o "Minicross 49cc".
- Per un ricambio o accessorio, classificalo secondo la sua natura/funzione sotto una famiglia coerente di ricambi/accessori. La compatibilità con un quad o una pit bike da sola non impone un ramo di categoria distinto.
- Gli attributi tecnici (2 tempi, 4 tempi, cilindrata, doppio scarico, racing) possono giustificare una categoria solo se identificano una collezione realmente utile e coerente con la tassonomia esistente; non devono moltiplicare le categorie automaticamente.
- Le collection esistenti costituiscono il vocabolario canonico. Riusa una famiglia generale corretta prima di crearne una più specifica. Se nessuna descrive il prodotto, puoi proporre una famiglia nuova: non assegnare categorie semanticamente sbagliate.
- Le categorie già approvate ma non pubblicate sono parte della tassonomia da riusare. Le sole proposte non approvate dei prodotti simili sono indizi di coerenza, non decisioni definitive.
- Obiettivo SEO: categorie stabili per famiglie di prodotti e intenzioni di navigazione distinte. Evita pagine quasi identiche o dedicate a ogni variante commerciale; preferisci categorie utili a raggruppare prodotti comparabili. Non inventare volumi di ricerca o promettere ranking; i conteggi del catalogo sono evidenza di assortimento, non dati sulle ricerche.
- Categoria e sottocategoria devono aiutare a raggiungere i prodotti attraverso la navigazione. Una nuova categoria non si giustifica solo per inserire più keyword o perché una collection attuale ha pochi prodotti.
- Non inventare marchi, compatibilità, caratteristiche o dati non presenti nella fonte. Se il caso è davvero incerto, descrivi una criticità invece di forzare la scelta.
- Usa nomi concisi, canonici, in italiano e coerenti con i nomi già presenti. Mantieni al massimo 3 livelli inclusa la categoria principale.
- `nuove_categorie` deve contenere soltanto percorsi davvero nuovi rispetto alle collection ricevute; non duplicare lì percorsi già esistenti.

ESEMPIO DI CONSISTENZA: "1 KIT ADESIVI QUAD RENEGADE 110", "1 Kit Adesivi Colarati CRF 50" e "Adesivi Arancione Nero Originali Per Pit Bike KAYO Ts90 Ts90r" hanno come concetto principale gli adesivi. Non inventare una diversa sottocategoria per ciascuno in base al veicolo/modello. Scegli percorsi canonici condivisi che siano coerenti con la tassonomia fornita: per esempio "Ricambi > Accessori > Adesivi" e, solo se è un ramo di navigazione reale e utile nel catalogo, anche "Veicoli > Accessori > Adesivi". Se entrambi i rami sono appropriati, restituiscili coerentemente per tutti i prodotti adesivi comparabili; altrimenti usa il solo percorso generale corretto. Se manca una categoria davvero equivalente, proponi un percorso nuovo e generale, non una variante iper-specifica.

Non confondere completezza con quantità: pochi percorsi corretti sono preferibili a molti percorsi ridondanti. Usa esclusivamente i dati sorgente ricevuti, assegna a ogni percorso confidence tra 0 e 1 e una motivazione breve. Ogni campo `motivazione` deve essere una singola frase concisa di massimo 250 caratteri; non superare mai 300 caratteri. Rispetta esattamente lo schema JSON."""
PREVIOUS_DEFAULT_PROMPT = """Classifica il prodotto per famiglia merceologica e funzione principale, non per ogni dettaglio del titolo. Prima identifica cosa viene venduto (veicolo completo, ricambio o accessorio), poi separa la categoria dalla compatibilità del prodotto. Analizza tutte le collection esistenti e gli esempi di prodotti simili per mantenere categorie canoniche coerenti sull'intero catalogo. Preferisci il percorso più generale che descrive correttamente il prodotto; usa meno di tre livelli quando bastano. Marca, modello, colore, cilindrata, motorizzazione e termini come racing sono normalmente attributi e non giustificano nuove categorie da soli. Prodotti della stessa famiglia funzionale devono ricevere lo stesso percorso anche se compatibili con veicoli differenti. Per accessori trasversali come gli adesivi, considera due percorsi paralleli (ad esempio Ricambi > Accessori > Adesivi e Veicoli > Accessori > Adesivi) solo se entrambi sono coerenti con la tassonomia e rappresentano punti di navigazione utili; se li usi, applicali coerentemente ai prodotti simili. Non creare sinonimi o percorsi ridondanti; aggiungi percorsi multipli solo quando hanno una reale utilità di navigazione. Le collection esistenti non sono un vincolo: se sono tutte inappropriate, proponi una nuova categoria canonica, non una variante iper-specifica. Evita collection con un solo prodotto quando la stessa famiglia può essere raccolta in una categoria esistente o generale. Nomi in italiano, chiari e stabili; massimo tre livelli complessivi. Se mancano prove sufficienti, segnala la criticità."""

DEFAULT_PROMPT = """Classifica per famiglia merceologica e funzione principale. Prima confronta il prodotto con l'intero catalogo delle collection Shopify e con le categorie già approvate, anche non pubblicate. Se una famiglia esistente è corretta, riusa ESATTAMENTE nomi e gerarchia: non creare sinonimi, nuove etichette o sottocategorie per marca, modello, colore o cilindrata. Prodotti con la stessa funzione devono convergere in categorie stabili e utili alla navigazione. Preferisci un percorso principale e al massimo tre livelli; percorsi paralleli sono ammessi solo per rami realmente utili del negozio. Proponi una nuova famiglia solo se nessuna esistente copre il prodotto: nella motivazione indica la categoria vicina valutata e la differenza merceologica concreta. Non forzare categorie sbagliate. Per la SEO privilegia pagine di categoria utili con assortimenti coerenti e intenzioni distinte, evita la frammentazione per keyword o varianti, non inventare dati di ricerca. Distingui le approvazioni umane dalle proposte non approvate dei prodotti simili. Segnala le incertezze."""


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

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("Confidenza non valida")
        percent = isinstance(value, str) and value.strip().endswith("%")
        text = value.strip().removesuffix("%").strip().replace(",", ".") if isinstance(value, str) else value
        try:
            number = float(text)
        except (TypeError, ValueError) as exc:
            raise ValueError("Confidenza numerica non valida") from exc
        if percent:
            if not 0 <= number <= 100:
                raise ValueError("Percentuale di confidenza fuori intervallo")
            return number / 100
        if 1 < number <= 100:
            return number / 100
        return number

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
    main_categories: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("main_categories", mode="before")
    @classmethod
    def validate_main_categories(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("Le categorie principali devono essere un elenco")
        result = []
        seen = set()
        for item in value:
            if not isinstance(item, str) or any(char in item for char in (">", "\n", "\r")):
                raise ValueError("Inserisci solo nomi di categorie principali, senza percorsi o righe multiple")
            name = " ".join(item.split())
            if not name or len(name) > 100:
                raise ValueError("Ogni categoria principale deve contenere da 1 a 100 caratteri")
            if name.casefold() not in seen:
                result.append(name)
                seen.add(name.casefold())
        return result

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
        CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,action TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT,error TEXT,created_at TEXT NOT NULL,finished_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status,next_attempt_at,created_at);
        CREATE INDEX IF NOT EXISTS idx_generations_product ON generations(product_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_generations_approved ON generations(product_id,status,approved_at DESC);
        CREATE INDEX IF NOT EXISTS idx_snapshots_product ON product_snapshots(product_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_membership_collection ON product_collections(collection_id);
        CREATE INDEX IF NOT EXISTS idx_operations_queue ON operations(status,created_at);
        CREATE TABLE IF NOT EXISTS deletion_backups(id TEXT PRIMARY KEY,collection_id TEXT NOT NULL,backup_json TEXT NOT NULL,created_at TEXT NOT NULL);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(collections)")}
        if "source_id" not in columns:
            db.execute("ALTER TABLE collections ADD COLUMN source_id TEXT")
        collection_columns = {row[1] for row in db.execute("PRAGMA table_info(collections)")}
        for column in ("category_value", "parent_value", "parent_type"):
            if column not in collection_columns:
                db.execute(f"ALTER TABLE collections ADD COLUMN {column} TEXT")
        db.execute("INSERT OR IGNORE INTO settings VALUES(1,?,?)", (Settings().model_dump_json(), now()))
        stored_settings_row = db.execute("SELECT value_json FROM settings WHERE id=1").fetchone()
        stored_settings = loads(stored_settings_row[0], {}) if stored_settings_row else {}
        # Upgrade only the untouched built-in prompt. Never overwrite a merchant's custom prompt.
        settings_changed = False
        if stored_settings.get("ai_prompt") in {LEGACY_DEFAULT_PROMPT, PREVIOUS_DEFAULT_PROMPT}:
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
        db.execute("UPDATE operations SET status='queued' WHERE status='processing'")
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
    with connect() as db:
        supersede_old_approvals(db)
    ensure_prompt_version()


def supersede_old_approvals(db: sqlite3.Connection) -> int:
    return db.execute("""UPDATE generations SET status='superseded' WHERE status='approved' AND EXISTS (
        SELECT 1 FROM generations newer WHERE newer.product_id=generations.product_id
        AND newer.status IN ('approved','published') AND (
            COALESCE(newer.approved_at,newer.created_at)>COALESCE(generations.approved_at,generations.created_at)
            OR (COALESCE(newer.approved_at,newer.created_at)=COALESCE(generations.approved_at,generations.created_at) AND newer.created_at>generations.created_at)
        ))""").rowcount


def get_settings() -> Settings:
    with connect() as db:
        row = db.execute("SELECT value_json FROM settings WHERE id=1").fetchone()
    return Settings.model_validate_json(row[0])


def ensure_prompt_version() -> int:
    settings = get_settings()
    fingerprint = {"system": SYSTEM_PROMPT, "prompt": settings.ai_prompt, "depth": settings.max_depth}
    if settings.main_categories:
        fingerprint["main_categories"] = settings.main_categories
    digest = stable_hash(fingerprint)
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


_token_cache: dict[str, Any] = {}
_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def shopify_client():
    if _http_client is not None:
        yield _http_client
    else:
        async with httpx.AsyncClient(timeout=90) as client:
            yield client


async def shop_token(client: httpx.AsyncClient) -> str:
    if os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN"):
        return os.environ["SHOPIFY_ADMIN_ACCESS_TOKEN"]
    if not os.getenv("SHOPIFY_CLIENT_ID") or not os.getenv("SHOPIFY_CLIENT_SECRET"):
        raise HTTPException(400, "Credenziali Shopify mancanti")
    credential_key = stable_hash([shop_domain(), os.environ["SHOPIFY_CLIENT_ID"], os.environ["SHOPIFY_CLIENT_SECRET"]])
    if _token_cache.get("key") == credential_key and _token_cache.get("expires_at", 0) > time.monotonic():
        return _token_cache["token"]
    response = await client.post(f"https://{shop_domain()}/admin/oauth/access_token", data={"grant_type": "client_credentials", "client_id": os.environ["SHOPIFY_CLIENT_ID"], "client_secret": os.environ["SHOPIFY_CLIENT_SECRET"]})
    response.raise_for_status()
    body = response.json()
    _token_cache.update(key=credential_key, token=body["access_token"], expires_at=time.monotonic() + max(0, float(body.get("expires_in", 3600)) - 60))
    return body["access_token"]


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
                async with shopify_client() as client:
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

            if response.status_code == 401 and attempt < SHOPIFY_MAX_RETRIES and not os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN"):
                _token_cache.clear()
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
COLLECTIONS_QUERY = """query CatalogCollections($after:String){collections(first:100,after:$after){nodes{id title handle updatedAt categoryPath:metafield(namespace:\"custom\",key:\"category_path\"){value} parentCollection:metafield(namespace:\"custom\",key:\"parent_collection\"){type value} sources{__typename id ... on CollectionConditionsSource{targetType shareable inclusion{conditions{__typename id} selections(first:1){nodes{product{id}}}}} ... on CollectionSubCollectionsSource{collections{id}}}}pageInfo{hasNextPage endCursor}}}"""
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
    if len(items) >= 20 and os.getenv("SHOPIFY_BULK_SYNC", "auto").lower() not in {"0", "false", "off"}:
        memberships = await bulk_collection_memberships({item["id"] for item in items})
        for item in items:
            item["product_ids"] = memberships[item["id"]]
        resolve_collection_hierarchy(items)
        return items
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


BULK_MEMBERSHIPS_QUERY = """{collections{edges{node{id products{edges{node{id}}}}}}}"""
BULK_START = """mutation BulkCollectionMemberships($query:String!){bulkOperationRunQuery(query:$query){bulkOperation{id}userErrors{field message}}}"""
BULK_STATUS = """query BulkMembershipStatus($id:ID!){node(id:$id){... on BulkOperation{status url errorCode objectCount}}}"""


async def bulk_collection_memberships(collection_ids: set[str]) -> dict[str, list[str]]:
    operation = await mutation(BULK_START, {"query": BULK_MEMBERSHIPS_QUERY}, "bulkOperationRunQuery")
    operation_id = operation["bulkOperation"]["id"]
    deadline = time.monotonic() + 600
    delay = 1.0
    while time.monotonic() < deadline:
        status = (await graphql(BULK_STATUS, {"id": operation_id})).get("node")
        if not status:
            raise RuntimeError("Operazione bulk Shopify non trovata")
        if status["status"] == "COMPLETED":
            if not status.get("url"):
                raise RuntimeError("Esportazione bulk senza file: snapshot locale conservato")
            return await read_bulk_memberships(status["url"], collection_ids, int(status["objectCount"]))
        if status["status"] in {"FAILED", "CANCELED", "EXPIRED"}:
            raise RuntimeError("Esportazione bulk fallita: " + str(status.get("errorCode") or status["status"]))
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 10)
    raise RuntimeError("Esportazione bulk oltre il timeout; snapshot locale conservato")


async def read_bulk_memberships(url: str, collection_ids: set[str], expected_count: int | None = None) -> dict[str, list[str]]:
    memberships: dict[str, list[str]] = {}
    roots = set()
    count = 0
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                item = json.loads(line)
                count += 1
                parent = item.get("__parentId")
                if parent:
                    memberships.setdefault(parent, []).append(item["id"])
                else:
                    roots.add(item["id"])
                    memberships.setdefault(item["id"], [])
    if expected_count is not None and count != expected_count:
        raise RuntimeError("Esportazione bulk incompleta: snapshot precedente conservato")
    if roots != collection_ids or set(memberships) != collection_ids:
        raise RuntimeError("Catalogo collection cambiato durante il bulk: ripeti la sincronizzazione; snapshot precedente conservato")
    return memberships


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


def writable_collection_source(sources: list[dict[str, Any]]) -> str | None:
    candidates = [source for source in sources if source.get("__typename") == "CollectionConditionsSource" and source.get("targetType", "PRODUCTS") == "PRODUCTS" and not source.get("shareable", False)]
    candidates.sort(key=lambda source: (bool((source.get("inclusion") or {}).get("conditions")), source["id"]))
    return candidates[0]["id"] if candidates else None


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
    return await asyncio.to_thread(store_shopify_snapshot, products, collections, origin)


def store_shopify_snapshot(products: list[dict[str, Any]], collections: list[dict[str, Any]], origin: str) -> dict[str, int]:
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
            source_id = writable_collection_source(sources)
            digest = stable_hash({"title": item["title"], "handle": item["handle"], "path": path, "kind": kind, "source_id": source_id, "products": sorted(item["product_ids"])})
            old = previous.get(item["id"])
            if not old:
                new_count += 1
            elif old["shopify_hash"] != digest or old["removed"]:
                modified_count += 1
            db.execute("""INSERT INTO collections(id,title,handle,path_json,kind,source_id,products_count,shopify_hash,removed,updated_at,last_synced_at) VALUES(?,?,?,?,?,?,?, ?,0,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,handle=excluded.handle,path_json=excluded.path_json,kind=excluded.kind,source_id=excluded.source_id,products_count=excluded.products_count,shopify_hash=excluded.shopify_hash,removed=0,updated_at=excluded.updated_at,last_synced_at=excluded.last_synced_at""", (item["id"], item["title"], item["handle"], dumps(path), kind, source_id, len(item["product_ids"]), digest, item.get("updatedAt"), synced_at))
            parent_field = item.get("parentCollection") or {}
            db.execute("UPDATE collections SET category_value=?,parent_value=?,parent_type=? WHERE id=?", ((item.get("categoryPath") or {}).get("value"), parent_field.get("value"), parent_field.get("type"), item["id"]))
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


def proposal_schema(product_id: str, main_categories: list[str] | None = None) -> dict[str, Any]:
    path_schema = {"type": "object", "properties": {"livelli": {"type": "array", "items": {"type": "string"}}, "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "motivazione": {"type": "string"}}, "required": ["livelli", "confidence", "motivazione"], "additionalProperties": False}
    if main_categories:
        path_schema["properties"]["livelli"].update(prefixItems=[{"type": "string", "enum": main_categories}], minItems=1, maxItems=3)
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
            editable = bool(candidate["source_id"])
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
            "collection_id": collection_id,
            "origine": "shopify",
            "assegnabile_manualmente": True,
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
    # Include the last human-approved taxonomy across prompt versions, even before
    # Shopify publication. Rejected/superseded/unapproved paths do not become canonical.
    approvals = db.execute("SELECT product_id,approved_paths_json,status FROM generations WHERE status IN ('approved','published') ORDER BY approved_at DESC,created_at DESC").fetchall()
    approved_by_product = {}
    approved_paths = {}
    for approval in approvals:
        if approval["product_id"] in approved_by_product:
            continue
        paths = loads(approval["approved_paths_json"], [])
        approved_by_product[approval["product_id"]] = paths
        for path in paths:
            key = path_key(path)
            if key:
                record = approved_paths.setdefault(key, {"percorso": path, "origine": "approvazione", "tipo": "manual", "prodotti_count": 0, "prodotti_approvati_count": 0})
                record["prodotti_approvati_count"] += 1
    existing_keys = {path_key(entry["percorso"]) for entry in collections}
    collections.extend(record for key, record in approved_paths.items() if key not in existing_keys)
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
        paths = approved_by_product.get(item["id"]) or [path["path"] for path in related_paths]
        origin = "approvazione" if approved_by_product.get(item["id"]) else "shopify"
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
                origin = "proposta_non_approvata" if generation["status"] == "generated" else "approvazione"
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
            "origine_percorsi": origin,
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
        "categorie_principali_consentite": settings.main_categories,
        "regola_precedenza": "Le categorie principali consentite prevalgono sui percorsi Shopify precedenti: Ricambi freni, ad esempio, può essere una sottocategoria di Ricambi ma non una nuova radice se non compare nell'elenco." if settings.main_categories else "Nessun vincolo sulle categorie principali; mantieni la tassonomia e le regole attuali.",
        "catalogo_collection_esistenti": categories,
        "prodotti_simili_catalogo": similar_products,
        "regole_tassonomia": {
            "preferire_famiglia_merceologica": True,
            "marca_modello_cilindrata_sono_attributi_non_categorie_automatiche": True,
            "creare_collection_solo_se_semanticamente_necessario": True,
            "riutilizzare_esattamente_nomi_e_gerarchia_esistenti": True,
            "categorie_approvate_non_pubblicate_sono_canoniche": True,
            "nuove_categorie_richiedono_confronto_con_famiglie_esistenti": True,
            "non_creare_varianti_per_keyword_marca_modello_colore": True,
            "numero_percorsi_preferito": 1,
            "percorsi_paralleli_ammessi_se_gia_coerenti_col_catalogo": True,
        },
        "massimo_livelli": depth_limit,
    }
    prompt = f"{settings.ai_prompt}\n\nAnalizza l'intero elenco delle collection e gli eventuali prodotti simili qui sotto. Non limitarti a trovare una corrispondenza lessicale: confronta la funzione del prodotto, la gerarchia, i conteggi e gli esempi. La priorità è riusare esattamente una collection o un percorso approvato adeguato alla funzione del prodotto. Non rinominare categorie equivalenti. Proponi una nuova famiglia soltanto se manca nel catalogo e motiva la differenza rispetto alla categoria esistente più vicina; non forzare però categorie semanticamente sbagliate. Mantieni un solo percorso principale salvo reale necessità.\nDati sorgente e tassonomia, unica fonte ammessa:\n{dumps({row['id']: source})}"
    if settings.main_categories:
        prompt += "\n\nVINCOLO OBBLIGATORIO DEL NEGOZIO: il primo livello di OGNI percorso deve essere uno dei nomi in categorie_principali_consentite, copiato esattamente. Puoi proporre sottocategorie pertinenti soltanto sotto queste categorie principali; non inventare altre radici, nemmeno riusando una collection Shopify fuori dall'elenco. Se il prodotto non può essere classificato correttamente sotto le radici consentite, restituisci percorsi vuoti e spiega la criticità; non forzare una categoria errata."
    payload = {
        "model": settings.model,
        "stream": False,
        "format": proposal_schema(row["id"], settings.main_categories),
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
    result = reuse_canonical_categories(validated, categories, depth_limit)
    return enforce_main_categories(result, settings.main_categories)


def enforce_main_categories(output: dict[str, Any], categories: list[str]) -> dict[str, Any]:
    if not categories:
        return output
    allowed = {path_key([name]): name for name in categories}
    invalid = [item["livelli"][0] for item in output["percorsi"] if path_key([item["livelli"][0]]) not in allowed]
    if invalid:
        raise RuntimeError("L'AI ha proposto categorie principali non consentite: " + ", ".join(dict.fromkeys(invalid)) + ". La proposta non è stata salvata e verrà ritentata.")
    for item in output["percorsi"]:
        item["livelli"][0] = allowed[path_key([item["livelli"][0]])]
    for path in output["nuove_categorie"]:
        if path_key([path[0]]) not in allowed:
            raise RuntimeError("Nuova categoria principale non consentita: " + path[0])
        path[0] = allowed[path_key([path[0]])]
    return output


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
    if get_settings().main_categories != settings.main_categories:
        raise RuntimeError("Categorie principali cambiate durante la generazione: proposta scartata, il job verrà ritentato.")
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
operation_worker_task: asyncio.Task | None = None


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
    global worker_task, operation_worker_task, _http_client
    init_db()
    _http_client = httpx.AsyncClient(timeout=90)
    worker_task = asyncio.create_task(worker())
    operation_worker_task = asyncio.create_task(operation_worker())
    try:
        yield
    finally:
        worker_task.cancel()
        operation_worker_task.cancel()
        await asyncio.gather(worker_task, operation_worker_task, return_exceptions=True)
        await _http_client.aclose()
        _http_client = None


app = FastAPI(title="Shopify Collections AI Studio", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
async def home():
    return FileResponse(BASE / "static/index.html")



OPERATION_ACTIONS = {"sync", "generate", "preview", "publish", "cleanup_preview", "cleanup", "main_categories_import"}


@app.post("/api/operations", status_code=202)
async def submit_operation(payload: dict[str, Any]):
    action = payload.get("action")
    if action not in OPERATION_ACTIONS:
        raise HTTPException(400, "Operazione non valida")
    data = payload.get("payload") or {}
    if not isinstance(data, dict):
        raise HTTPException(400, "Payload non valido")
    encoded = dumps(data)
    with connect() as db:
        existing = db.execute("SELECT id FROM operations WHERE action=? AND payload_json=? AND status IN ('queued','processing')", (action, encoded)).fetchone()
        operation_id = existing["id"] if existing else str(uuid.uuid4())
        if not existing:
            db.execute("INSERT INTO operations(id,action,payload_json,status,created_at) VALUES(?,?,?,'queued',?)", (operation_id, action, encoded, now()))
    return {"operation_id": operation_id, "status": "queued"}


@app.get("/api/operations/{operation_id}")
async def operation_status(operation_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Operazione non trovata")
    return {"id": row["id"], "action": row["action"], "status": row["status"], "result": loads(row["result_json"]), "error": row["error"]}


async def run_operation(row: sqlite3.Row) -> None:
    handlers = {"sync": lambda _: sync_endpoint(), "generate": enqueue, "preview": publish_preview, "publish": publish_execute, "cleanup_preview": lambda _: cleanup_preview(), "cleanup": cleanup_execute, "main_categories_import": lambda _: import_main_categories()}
    try:
        if row["action"] == "generate":
            await sync_shopify("sync")
            with connect() as db:
                current = db.execute("SELECT status FROM operations WHERE id=?", (row["id"],)).fetchone()
            if current["status"] == "cancelled":
                return
            result = await _enqueue_products(loads(row["payload_json"], {}), operation_id=row["id"])
        else:
            result = await handlers[row["action"]](loads(row["payload_json"], {}))
        with connect() as db:
            db.execute("UPDATE operations SET status='completed',result_json=?,finished_at=? WHERE id=? AND status<>'cancelled'", (dumps(result), now(), row["id"]))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        logger.exception("Operazione Shopify %s fallita", row["id"])
        with connect() as db:
            db.execute("UPDATE operations SET status='error',error=?,finished_at=? WHERE id=?", (error, now(), row["id"]))


async def operation_worker() -> None:
    while True:
        try:
            with connect() as db:
                row = db.execute("SELECT * FROM operations WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE operations SET status='processing' WHERE id=?", (row["id"],))
            if row:
                await run_operation(row)
            else:
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker Shopify interrotto")
            await asyncio.sleep(1)


MAIN_CATEGORIES_QUERY = """query MainCollectionCandidates($after:String){collections(first:100,after:$after){nodes{id title parentCollection:metafield(namespace:"custom",key:"parent_collection"){type value}}pageInfo{hasNextPage endCursor}}}"""


@app.post("/api/settings/main-categories/import")
async def import_main_categories():
    categories = []
    after = None
    seen_cursors = set()
    seen_names = set()
    while True:
        block = (await graphql(MAIN_CATEGORIES_QUERY, {"after": after}))["collections"]
        for item in block["nodes"]:
            field = item.get("parentCollection")
            if field and field.get("value") not in {None, "", "null"}:
                if field.get("type") == "list.collection_reference":
                    try:
                        parents = json.loads(field["value"])
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise RuntimeError("Campo padre non valido per " + item["title"]) from exc
                    if not isinstance(parents, list):
                        raise RuntimeError("Campo padre non valido per " + item["title"])
                    if parents:
                        continue
                else:
                    continue
            name = " ".join(item["title"].split())
            if name and name.casefold() not in seen_names:
                categories.append(name)
                seen_names.add(name.casefold())
        if not block["pageInfo"]["hasNextPage"]:
            return {"main_categories": sorted(categories, key=str.casefold)}
        after = block["pageInfo"]["endCursor"]
        if not after or after in seen_cursors:
            raise RuntimeError("Paginazione collection Shopify non valida")
        seen_cursors.add(after)

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
    await sync_shopify("sync")
    return await _enqueue_products(payload)


async def _enqueue_products(payload: dict[str, Any], operation_id: str | None = None):
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
        if operation_id:
            operation = db.execute("SELECT status FROM operations WHERE id=?", (operation_id,)).fetchone()
            if operation and operation["status"] == "cancelled":
                return {"queued": 0, "cancelled": True}
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
        shopify_counts = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) n FROM operations GROUP BY status")}
        shopify_current = db.execute("SELECT id,action,status FROM operations WHERE status='processing' LIMIT 1").fetchone()
    return {"shopify_queued": shopify_counts.get("queued", 0), "shopify_processing": shopify_counts.get("processing", 0), "shopify_current": dict(shopify_current) if shopify_current else None, "paused": paused, "queued": counts.get("queued", 0), "processing": counts.get("processing", 0), "completed": counts.get("completed", 0), "error": counts.get("error", 0), "current": dict(current) if current else None}


@app.post("/api/queue/{action}")
async def queue_action(action: Literal["pause", "resume", "cancel"]):
    with connect() as db:
        if action in ("pause", "resume"):
            db.execute("UPDATE queue_state SET paused=?,updated_at=? WHERE id=1", (action == "pause", now()))
        else:
            db.execute("UPDATE operations SET status='cancelled',finished_at=? WHERE action='generate' AND status IN ('queued','processing')", (now(),))
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
        generation = db.execute("SELECT * FROM generations WHERE product_id=? AND status IN ('approved','published') ORDER BY COALESCE(approved_at,created_at) DESC,created_at DESC LIMIT 1", (product_id,)).fetchone()
        if generation and generation["status"] == "approved":
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
    query = """query FreshProductCollections($id:ID!,$after:String){product(id:$id){id title vendor variants(first:100){nodes{id sku title}}collections(first:250,after:$after){nodes{id title categoryPath:metafield(namespace:\"custom\",key:\"category_path\"){value} parentCollection:metafield(namespace:\"custom\",key:\"parent_collection\"){type value} sources{__typename id ... on CollectionConditionsSource{targetType shareable inclusion{conditions{__typename id}}}}}pageInfo{hasNextPage endCursor}}}}"""
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
            paths.append({"id": item["id"], "path": resolved_path, "kind": "smart" if is_smart else "manual", "source_id": writable_collection_source(sources), "title": item["title"]})
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


COLLECTION_BY_HANDLE = """query CollectionRecovery($handle:String!){collectionByHandle:collectionByIdentifier(identifier:{handle:$handle}){id title sources{__typename id ... on CollectionConditionsSource{targetType shareable inclusion{conditions{__typename id}}}}}}"""
CREATE_COLLECTION = """mutation CreateCollection($collection:CollectionCreateInput!){collectionCreate(collection:$collection){collection{id title sources{__typename id}}userErrors{field message}}}"""
UPDATE_SELECTIONS = """mutation UpdateCollectionSelections($collection:CollectionUpdateInput!){collectionUpdate(collection:$collection){collection{id}job{id done}userErrors{field message}}}"""
METAFIELDS_SET = """mutation SetCollectionPath($metafields:[MetafieldsSetInput!]!){metafieldsSet(metafields:$metafields){metafields{ownerType key value}userErrors{field message code}}}"""
JOB_QUERY = """query JobStatus($id:ID!){job(id:$id){id done}}"""
DELETE_COLLECTION = """mutation DeleteCollection($input:CollectionDeleteInput!){collectionDelete(input:$input){deletedCollectionId userErrors{field message}}}"""


COLLECTION_SOURCE_QUERY = """query CollectionSelectionSources($id:ID!){collection(id:$id){id sources{__typename id ... on CollectionConditionsSource{targetType shareable inclusion{conditions{__typename id}}}}}}"""
CREATE_SELECTION_SOURCE = """mutation CreateCollectionSelectionSource($collection:CollectionUpdateInput!){collectionUpdate(collection:$collection){collection{id}job{id done}userErrors{field message}}}"""


async def ensure_product_selection_source(collection_id: str, product_ids: list[str]) -> tuple[str, int]:
    collection = (await graphql(COLLECTION_SOURCE_QUERY, {"id": collection_id})).get("collection")
    if not collection:
        raise RuntimeError("Collection Shopify non trovata: " + collection_id)
    source_id = writable_collection_source(collection.get("sources") or [])
    if source_id:
        return source_id, 0
    initial = product_ids[:250]
    update = {"id": collection_id, "sourcesToCreate": [{"source": {"title": "Prodotti approvati dall'app", "targetType": "PRODUCTS", "inclusion": {"selections": [{"productId": product_id} for product_id in initial]}}}]}
    result = await mutation(CREATE_SELECTION_SOURCE, {"collection": update}, "collectionUpdate")
    await wait_for_job(result.get("job"))
    for attempt in range(5):
        collection = (await graphql(COLLECTION_SOURCE_QUERY, {"id": collection_id})).get("collection")
        source_id = writable_collection_source((collection or {}).get("sources") or [])
        if source_id:
            return source_id, len(initial)
        if attempt < 4:
            await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError("Shopify non conferma la nuova sorgente manuale: riprova la pubblicazione")

SOURCE_SELECTIONS_QUERY = """query ManualSourceSelections($id:ID!,$after:String){node(id:$id){... on CollectionConditionsSource{id targetType shareable inclusion{conditions{__typename id} selections(first:250,after:$after){nodes{product{id}}pageInfo{hasNextPage endCursor}}} exclusion{conditions{__typename id}}}}}"""


async def remove_manual_source_products(collection_id: str, source_id: str, product_ids: list[str]) -> None:
    selected: set[str] = set()
    after = None
    cursors = set()
    while True:
        source = (await graphql(SOURCE_SELECTIONS_QUERY, {"id": source_id, "after": after})).get("node")
        if not source:
            # Recovery after deleting a source: verify membership in the caller.
            return
        if source.get("shareable") or source.get("targetType") != "PRODUCTS":
            raise RuntimeError("La sorgente non è più una selezione locale di prodotti: risincronizza " + collection_id)
        inclusion = source.get("inclusion") or {}
        if inclusion.get("conditions"):
            raise RuntimeError("La sorgente ora contiene regole automatiche: rimozione interrotta per conservarle invariate")
        block = inclusion["selections"]
        selected.update(item["product"]["id"] for item in block["nodes"])
        if not block["pageInfo"]["hasNextPage"]:
            break
        after = block["pageInfo"]["endCursor"]
        if not after or after in cursors:
            raise RuntimeError("Paginazione selezioni manuali non valida")
        cursors.add(after)
    remove = selected & set(product_ids)
    if not remove:
        return
    if not selected - remove:
        if (source.get("exclusion") or {}).get("conditions"):
            raise RuntimeError("La sorgente contiene esclusioni automatiche: nessuna regola modificata")
        # Shopify rejects an empty conditions-based source. Detach the rules-free
        # manual source instead of deleting the collection or inventing conditions.
        update = {"id": collection_id, "sourcesToDelete": [source_id]}
        result = await mutation(UPDATE_SELECTIONS, {"collection": update}, "collectionUpdate")
        await wait_for_job(result.get("job"))
        return
    for batch in chunks(sorted(remove)):
        update = {"id": collection_id, "sourcesToUpdate": [{"condition": {"id": source_id, "inclusion": {"selectionsToRemove": [{"productId": product_id} for product_id in batch]}}}]}
        result = await mutation(UPDATE_SELECTIONS, {"collection": update}, "collectionUpdate")
        await wait_for_job(result.get("job"))

PUBLICATIONS_QUERY = """query CollectionSalesChannels($after:String){publications(first:100,after:$after){nodes{id}pageInfo{hasNextPage endCursor}}}"""
PUBLICATION_SCOPES_QUERY = """query CollectionPublicationScopes{currentAppInstallation{accessScopes{handle}}}"""
COLLECTION_PUBLICATIONS_QUERY = """query CollectionPublicationStatus($id:ID!,$after:String){collection(id:$id){resourcePublicationsV2(first:100,after:$after){nodes{isPublished publication{id}}pageInfo{hasNextPage endCursor}}}}"""
PUBLISH_COLLECTION = """mutation PublishCollectionChannels($id:ID!,$input:[PublicationInput!]!){publishablePublish(id:$id,input:$input){publishable{... on Collection{id}}userErrors{field message}}}"""


async def collection_channel_ids() -> list[str]:
    installation = (await graphql(PUBLICATION_SCOPES_QUERY, {})).get("currentAppInstallation") or {}
    scopes = {item["handle"] for item in installation.get("accessScopes", [])}
    if "write_publications" not in scopes:
        raise RuntimeError("Manca il permesso Shopify write_publications: abilita read_publications e write_publications per pubblicare le collection su tutti i canali. Nessuna nuova collection creata.")
    ids: list[str] = []
    after = None
    seen_cursors = set()
    while True:
        block = (await graphql(PUBLICATIONS_QUERY, {"after": after}))["publications"]
        ids.extend(item["id"] for item in block["nodes"])
        if not block["pageInfo"]["hasNextPage"]:
            break
        after = block["pageInfo"]["endCursor"]
        if not after or after in seen_cursors:
            raise RuntimeError("Paginazione canali Shopify non valida: nessuna nuova collection creata.")
        seen_cursors.add(after)
    if not ids:
        raise RuntimeError("Shopify non restituisce canali di pubblicazione: nessuna nuova collection creata.")
    return list(dict.fromkeys(ids))


async def published_collection_channels(collection_id: str) -> set[str]:
    ids: set[str] = set()
    after = None
    seen_cursors = set()
    while True:
        collection = (await graphql(COLLECTION_PUBLICATIONS_QUERY, {"id": collection_id, "after": after})).get("collection")
        if not collection:
            raise RuntimeError("Collection non trovata durante la verifica dei canali: " + collection_id)
        block = collection["resourcePublicationsV2"]
        ids.update(item["publication"]["id"] for item in block["nodes"] if item["isPublished"])
        if not block["pageInfo"]["hasNextPage"]:
            return ids
        after = block["pageInfo"]["endCursor"]
        if not after or after in seen_cursors:
            raise RuntimeError("Paginazione pubblicazioni collection non valida")
        seen_cursors.add(after)


async def ensure_collection_channels(collection_id: str, channel_ids: list[str]) -> None:
    desired = set(channel_ids)
    current = await published_collection_channels(collection_id)
    missing = sorted(desired - current)
    for batch in chunks(missing):
        await mutation(PUBLISH_COLLECTION, {"id": collection_id, "input": [{"publicationId": channel_id} for channel_id in batch]}, "publishablePublish")
    for attempt in range(5):
        if desired <= await published_collection_channels(collection_id):
            return
        if attempt < 4:
            await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError("Shopify non conferma la collection su tutti i canali: " + collection_id + ". L'approvazione resta salvata; riprova per completare i canali mancanti.")


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
    for item in sorted(collections, key=collection_priority):
        if item["id"]:
            targets.setdefault(path_key(item["path"]), {"collection_id": item["id"], "source_id": item["source_id"]})
    needed_paths = {path_key(path[:depth]) for product in products for path in product["desired"] for depth in range(1, len(path) + 1)}
    existing_paths = {path_key(item["path"]) for item in collections}
    if any(key in existing_paths and key not in targets for key in needed_paths):
        raise HTTPException(409, "Gerarchia con collection non disponibile: nessuna scrittura eseguita.")
    managed_collection_ids = {item["id"] for item in collections if item.get("handle", "").startswith("category-") and path_key(item["path"]) in needed_paths}
    # A deterministic handle also identifies a creation recovered after a lost response.
    channel_ids = await collection_channel_ids() if operations["create_collections"] or managed_collection_ids else []
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
        source_id = writable_collection_source(sources)
        collection_id = result["collection"]["id"]
        await ensure_collection_channels(collection_id, channel_ids)
        created[path_key(item["path"])] = {"collection_id": collection_id, "source_id": source_id}
        targets[path_key(item["path"])] = created[path_key(item["path"])]
        metafield = {"ownerId": collection_id, "namespace": "custom", "key": "category_path", "type": "single_line_text_field", "value": path_label(item["path"])}
        await mutation(METAFIELDS_SET, {"metafields": [metafield]}, "metafieldsSet")
    for item in operations["add_products"]:
        target = {"collection_id": item["collection_id"], "source_id": item["source_id"]} if item["collection_id"] else created[path_key(item["path"])]
        already_created = 250 if path_key(item["path"]) in initialized else 0
        if not target["source_id"]:
            source_id, seeded = await ensure_product_selection_source(target["collection_id"], item["product_ids"])
            target["source_id"] = source_id
            targets[path_key(item["path"])] = target
            already_created = seeded
        for batch in chunks(item["product_ids"][already_created:]):
            update = {"id": target["collection_id"], "sourcesToUpdate": [{"condition": {"id": target["source_id"], "inclusion": {"selectionsToAdd": [{"productId": product_id} for product_id in batch]}}}]}
            result = await mutation(UPDATE_SELECTIONS, {"collection": update}, "collectionUpdate")
            await wait_for_job(result.get("job"))
    # Deduplicate metadata across products and skip values already confirmed by sync.
    known = {item["id"]: item for item in collections}
    for key, target in created.items():
        known[target["collection_id"]] = {"category_value": next(path_label(item["path"]) for item in operations["create_collections"] if path_key(item["path"]) == key)}
    metafields: dict[tuple[str, str], dict[str, Any]] = {}
    for product in products:
        for desired in product["desired"]:
            for depth in range(1, len(desired) + 1):
                levels = desired[:depth]
                child = targets.get(path_key(levels))
                if not child:
                    raise RuntimeError("Gerarchia non aggiornabile: " + path_label(levels))
                collection_id = child["collection_id"]
                current = known.get(collection_id, {})
                if current.get("category_value") != path_label(levels):
                    metafields[(collection_id, "category_path")] = {"ownerId": collection_id, "namespace": "custom", "key": "category_path", "type": "single_line_text_field", "value": path_label(levels)}
                if depth > 1:
                    parent = targets.get(path_key(levels[:-1]))
                    if not parent:
                        raise RuntimeError("Gerarchia non aggiornabile: " + path_label(levels))
                    value = parent["collection_id"] if parent_type == "collection_reference" else dumps([parent["collection_id"]])
                    equivalent = current.get("parent_value") == value if parent_type == "collection_reference" else loads(current.get("parent_value")) == loads(value)
                    if current.get("parent_type") != parent_type or not equivalent:
                        metafields[(collection_id, "parent_collection")] = {"ownerId": collection_id, "namespace": "custom", "key": "parent_collection", "type": parent_type, "value": value}
    for batch in chunks(list(metafields.values()), size=25):
        await mutation(METAFIELDS_SET, {"metafields": batch}, "metafieldsSet")
    # Reconcile channels for new collections and creations recovered on subsequent runs.
    channel_collection_ids = managed_collection_ids - {target["collection_id"] for target in created.values()}
    for collection_id in sorted(channel_collection_ids):
        await ensure_collection_channels(collection_id, channel_ids)
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
        await remove_manual_source_products(item["collection_id"], item["source_id"], item["product_ids"])

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
        supersede_old_approvals(db)
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


def _normalized_path(path: Iterable[str]) -> list[str]:
    return [part.casefold() for part in normalize_levels(path)]


def _is_path_prefix(parent: Iterable[str], child: Iterable[str]) -> bool:
    """Return True when parent is the same path as child or one of its ancestors."""
    parent_parts = _normalized_path(parent)
    child_parts = _normalized_path(child)
    return bool(parent_parts) and len(parent_parts) <= len(child_parts) and child_parts[:len(parent_parts)] == parent_parts


def collection_priority(item: dict[str, Any]) -> tuple[int, int, str, str]:
    """Prefer a collection that can be updated safely when duplicate paths exist."""
    kind = item.get("kind")
    source_id = item.get("source_id")
    usable_manual = bool(source_id)
    manual = kind == "manual"
    priority = 0 if usable_manual else 1 if manual else 2
    return (priority, -int(item.get("products_count", 0) or 0), str(item.get("title", "")).casefold(), str(item.get("id", "")))


def build_dry_run(products: list[dict[str, Any]], collections: list[dict[str, Any]]) -> dict[str, Any]:
    """Build safe, deduplicated collection changes without dropping useful parent membership.

    A product assigned to ``Veicoli > Minicross > Racing`` must remain in an existing
    ``Veicoli > Minicross`` collection so that the parent collection still displays it.
    If Shopify has duplicate collections with the same canonical path, prefer the
    manually editable one and migrate product memberships to that representative.
    """
    active = [item for item in collections if not item.get("removed") and normalize_levels(item.get("path", []))]
    by_id = {item["id"]: item for item in active}
    by_path: dict[str, dict[str, Any]] = {}
    for item in sorted(active, key=collection_priority):
        by_path.setdefault(path_key(item["path"]), item)

    create: dict[str, dict[str, Any]] = {}
    add: dict[str, dict[str, Any]] = {}
    remove: dict[str, dict[str, Any]] = {}
    blocked: list[dict[str, str]] = []
    blocked_seen: set[tuple[str, str, str]] = set()

    def block(product_id: str, levels: Iterable[str], reason: str) -> None:
        record = (product_id, path_label(levels), reason)
        if record not in blocked_seen:
            blocked_seen.add(record)
            blocked.append({"product_id": product_id, "path": record[1], "reason": reason})

    def add_membership(product_id: str, collection: dict[str, Any] | None, levels: list[str], key: str) -> None:
        if collection is None:
            create.setdefault(key, {"path": levels, "title": levels[-1]})
            collection_key = f"new:{key}"
        else:
            collection_key = collection["id"]
        entry = add.setdefault(collection_key, {
            "collection_id": collection.get("id") if collection else None,
            "source_id": collection.get("source_id") if collection else None,
            "path": levels,
            "product_ids": [],
        })
        if product_id not in entry["product_ids"]:
            entry["product_ids"].append(product_id)

    def remove_membership(product_id: str, item: dict[str, Any]) -> None:
        entry = remove.setdefault(item["id"], {
            "collection_id": item["id"],
            "source_id": item["source_id"],
            "path": item["path"],
            "product_ids": [],
        })
        if product_id not in entry["product_ids"]:
            entry["product_ids"].append(product_id)

    for product in products:
        product_id = product["id"]
        current_items = [{**item, "path": by_id.get(item["id"], item).get("path", [])} for item in product.get("current", []) if normalize_levels(item.get("path", []))]
        desired: dict[str, list[str]] = {}
        for path in product.get("desired", []):
            normalized = normalize_levels(path)
            if normalized:
                desired.setdefault(path_key(normalized), normalized)

        for child in list(desired.values()):
            for depth in range(1, len(child)):
                desired.setdefault(path_key(child[:depth]), child[:depth])

        for key, levels in desired.items():
            collection = by_path.get(key)
            # A same-path membership in a duplicate collection is not enough: assign the
            # product to the chosen canonical collection ID to keep the taxonomy stable.
            already_in_target = bool(collection) and any(
                item["id"] == collection["id"] for item in current_items
            )
            if collection is None or not already_in_target:
                add_membership(product_id, collection, levels, key)

        # Remove obsolete manual memberships. Existing smart memberships are controlled
        # by Shopify conditions and are never modified by this application.
        effective_desired_paths = list(desired.values())
        for item in current_items:
            key = path_key(item["path"])
            if key in desired:
                canonical = by_path.get(key)
                if (
                    canonical
                    and item["id"] != canonical["id"]
                    and item.get("kind") == "manual"
                    and item.get("source_id")
                    and canonical.get("kind") == "manual"
                    and canonical.get("source_id")
                ):
                    remove_membership(product_id, item)
                continue
            if item.get("kind") != "manual":
                continue
            if any(len(item["path"]) < len(path) and _is_path_prefix(item["path"], path) for path in effective_desired_paths):
                continue
            if not item.get("source_id"):
                block(product_id, item["path"], "Sorgente manuale Shopify non disponibile")
                continue
            remove_membership(product_id, item)

    return {
        "create_collections": sorted(create.values(), key=lambda item: path_key(item["path"])),
        "add_products": sorted(add.values(), key=lambda item: path_key(item["path"])),
        "remove_products": sorted(remove.values(), key=lambda item: path_key(item["path"])),
        "create_sources": [{"collection_id": item["collection_id"], "path": item["path"]} for item in add.values() if item["collection_id"] and not item["source_id"]],
        "blocked": blocked,
    }


def empty_manual_collections(collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in collections if item.get("kind") == "manual" and not item.get("removed") and int(item.get("products_count", 0)) == 0]


def match_collection_paths(products: list[dict[str, Any]], collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reuse an unambiguous legacy collection lacking hierarchy before creating one."""
    result = [dict(item) for item in collections]
    paths = {path_key(item['path']) for item in result if not item.get('removed')}
    for product in products:
        for desired in product.get('desired', []):
            for depth in range(1, len(desired) + 1):
                levels = desired[:depth]
                key = path_key(levels)
                if key in paths:
                    continue
                candidates = [item for item in result if not item.get('removed') and len(item.get('path', [])) == 1 and path_key([item['title']]) == path_key([levels[-1]])]
                if len(candidates) > 1:
                    raise ValueError('Collection omonime senza gerarchia: ' + path_label(levels))
                if candidates:
                    candidates[0]['path'] = levels
                    paths.add(key)
    return result


# Deliberately narrow aliases: match equivalent names only within the same branch.
_CATEGORY_ALIASES = {
    'adesivi': 'adesivi', 'adesivo': 'adesivi', 'decalcomanie': 'adesivi',
    'decalcomania': 'adesivi', 'adesivi e decalcomanie': 'adesivi',
    'decalcomanie e adesivi': 'adesivi', 'kit adesivi': 'adesivi',
    'quad': 'quad', 'atv': 'quad', 'quad e atv': 'quad', 'quad atv': 'quad',
    'camera d aria': 'camere aria', 'camere d aria': 'camere aria',
    'camere aria': 'camere aria', 'camera aria': 'camere aria',
}


def _category_name_key(name: str) -> str:
    return _SPACE.sub(' ', re.sub(r"[’'/_-]+", ' ', name.casefold())).strip()


def reuse_canonical_categories(output: dict[str, Any], categories: list[dict[str, Any]], max_depth: int) -> dict[str, Any]:
    """Repair spelling/explicit synonyms; never guess equivalence from fuzzy similarity."""
    known = [entry for entry in categories if entry.get('percorso')]
    known.sort(key=lambda entry: (entry.get('tipo') == 'smart', entry.get('origine') != 'shopify', -int(entry.get('prodotti_count', 0)), path_key(entry['percorso'])))
    exact = {}
    aliases = {}
    def exact_key(path):
        return tuple(_category_name_key(level) for level in path)
    def alias_key(path):
        key = exact_key(path)
        return (*key[:-1], _CATEGORY_ALIASES.get(key[-1], key[-1]))
    for entry in known:
        levels = normalize_levels(entry['percorso'])
        exact.setdefault(exact_key(levels), levels)
        aliases.setdefault(alias_key(levels), levels)
    result = normalize_ai_output(output, max_depth)
    for item in result['percorsi']:
        original = item['livelli']
        chosen = exact.get(exact_key(original)) or aliases.get(alias_key(original))
        if chosen and chosen != original:
            item['livelli'] = chosen
            result['criticita'].append(f"Riutilizzato il percorso canonico '{path_label(chosen)}' invece di '{path_label(original)}'.")
        elif not chosen:
            for depth in range(len(original) - 1, 0, -1):
                prefix = exact.get(exact_key(original[:depth]))
                if prefix:
                    item['livelli'] = prefix + original[depth:]
                    break
    result = normalize_ai_output(result, max_depth)
    # The model cannot omit a new path from the new-category list or list an existing
    # collection as new: derive it from the actual selected canonical paths.
    existing_keys = {exact_key(entry['percorso']) for entry in known}
    result['nuove_categorie'] = [item['livelli'] for item in result['percorsi'] if exact_key(item['livelli']) not in existing_keys]
    return result


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
      parentCollection: metafield(namespace: "custom", key: "parent_collection") { type value }
      sources {
        __typename
        id
        ... on CollectionConditionsSource {
          targetType
          shareable
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
    variants(first: 100) { nodes { id sku title } }
    collections(first: 250, after: $after) {
      nodes {
        id
        title
        categoryPath: metafield(namespace: "custom", key: "category_path") { value }
      parentCollection: metafield(namespace: "custom", key: "parent_collection") { type value }
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

query CollectionSalesChannels($after: String) {
  publications(first: 100, after: $after) {
    nodes { id }
    pageInfo { hasNextPage endCursor }
  }
}

query CollectionPublicationScopes {
  currentAppInstallation { accessScopes { handle } }
}

query CollectionPublicationStatus($id: ID!, $after: String) {
  collection(id: $id) {
    resourcePublicationsV2(first: 100, after: $after) {
      nodes { isPublished publication { id } }
      pageInfo { hasNextPage endCursor }
    }
  }
}

mutation PublishCollectionChannels($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    publishable { ... on Collection { id } }
    userErrors { field message }
  }
}

mutation BulkCollectionMemberships($query: String!) {
  bulkOperationRunQuery(query: $query) {
    bulkOperation { id }
    userErrors { field message }
  }
}

query BulkMembershipStatus($id: ID!) {
  node(id: $id) {
    ... on BulkOperation { status url errorCode objectCount }
  }
}

query CollectionSelectionSources($id: ID!) {
  collection(id: $id) {
    id
    sources {
      __typename
      id
      ... on CollectionConditionsSource {
        targetType
        shareable
        inclusion { conditions { __typename id } }
      }
    }
  }
}

mutation CreateCollectionSelectionSource($collection: CollectionUpdateInput!) {
  collectionUpdate(collection: $collection) {
    collection { id }
    job { id done }
    userErrors { field message }
  }
}

query ManualSourceSelections($id: ID!, $after: String) {
  node(id: $id) {
    ... on CollectionConditionsSource {
      id
      targetType
      shareable
      inclusion {
        conditions { __typename id }
        selections(first: 250, after: $after) {
          nodes { product { id } }
          pageInfo { hasNextPage endCursor }
        }
      }
      exclusion { conditions { __typename id } }
    }
  }
}


=================================
FILE: static/app.js
=================================


const state={items:[],selected:new Set(),offset:0,limit:60,total:0,settings:null,preview:null};
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const esc=(v='')=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const debounce=(fn,ms=250)=>{let id;return(...a)=>{clearTimeout(id);id=setTimeout(()=>fn(...a),ms)}};
async function api(url,opt={}){const actions={'/api/jobs':'generate','/api/publish/preview':'preview','/api/publish/execute':'publish','/api/sync':'sync','/api/cleanup/preview':'cleanup_preview','/api/cleanup/execute':'cleanup','/api/settings/main-categories/import':'main_categories_import'};if(opt.method==='POST'&&actions[url]){const op=await api('/api/operations',{method:'POST',body:JSON.stringify({action:actions[url],payload:opt.body?JSON.parse(opt.body):{}})});notice('Operazione accodata. Puoi continuare a usare la pagina.');for(;;){await new Promise(resolve=>setTimeout(resolve,1000));const job=await api('/api/operations/'+op.operation_id);if(job.status==='completed')return job.result;if(job.status==='error')throw new Error(job.error);if(job.status==='cancelled')throw new Error('Operazione annullata.');}}const r=await fetch(url,{...opt,headers:{'Content-Type':'application/json',...(opt.headers||{})}}),b=await r.json().catch(()=>({}));if(!r.ok)throw new Error(typeof b.detail==='string'?b.detail:JSON.stringify(b.detail||b));return b}
function notice(msg,error=false){const n=$('#notice');n.textContent=msg;n.classList.remove('hidden','border-blue-200','bg-blue-50','text-blue-900','border-rose-200','bg-rose-50','text-rose-900');n.classList.add(...(error?['border-rose-200','bg-rose-50','text-rose-900']:['border-blue-200','bg-blue-50','text-blue-900']))}
function statusLabel(s){return({to_analyze:'Da analizzare',queued:'In coda',processing:'In elaborazione',completed:'Elaborato',approved:'Categorie approvate',rejected:'Categorie rifiutate',published:'Pubblicato',error:'Errore'})[s]||s}
function statusClass(s){return s==='error'||s==='rejected'?'badge-rose':s==='processing'||s==='queued'?'badge-amber':'badge-blue'}
const pathText=p=>(p||[]).join('-');
function uniquePaths(items){const seen=new Set();return(items||[]).filter(p=>{const key=pathText(p.path).normalize('NFKC').replace(/\s+/g,' ').trim().toLocaleLowerCase('it-IT');if(!key||seen.has(key))return false;seen.add(key);return true})}
function params(limit=state.limit,offset=state.offset){const p=new URLSearchParams({q:$('#search').value,category:$('#category').value,subcategory:$('#subcategory').value,vendor:$('#vendor').value,processing:$('#processing').value,limit,offset});if($('#uncategorized').checked)p.set('uncategorized','true');if($('#withProposal').checked)p.set('proposal','true');return p}
let loadingProducts=false,reloadProducts=false;async function load(){if(loadingProducts){reloadProducts=true;return;}loadingProducts=true;try{const d=await api('/api/products?'+params());if(state.total!==d.total||JSON.stringify(state.items)!==JSON.stringify(d.items)){state.items=d.items;state.total=d.total;render()}}catch(e){notice(e.message,true)}finally{loadingProducts=false;if(reloadProducts){reloadProducts=false;load()}}}
function render(){const g=$('#grid');g.innerHTML='';$('#resultCount').textContent=`${state.items.length} visualizzati · ${state.total} nel risultato`;$('#selectedCount').textContent=`${state.selected.size} selezionati`;$('#prev').disabled=state.offset===0;$('#next').disabled=state.offset+state.limit>=state.total;if(!state.items.length){g.innerHTML='<div class="panel col-span-full py-14 text-center text-slate-500">Nessun prodotto corrisponde ai filtri.</div>';return}for(const item of state.items){const card=document.createElement('article');card.className='product-card'+(state.selected.has(item.id)?' selected':'');const sku=item.variants.find(v=>v.sku)?.sku||'—',gen=item.latest_generation,visiblePaths=uniquePaths(item.paths);const paths=visiblePaths.length?visiblePaths.map(p=>`<li class="path-pill"><span class="mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${p.kind==='smart'?'bg-amber-500':'bg-blue-500'}"></span><span>${esc(pathText(p.path))}</span>${p.kind==='smart'?'<span class="ml-auto text-[10px] uppercase text-amber-700">smart</span>':''}</li>`).join(''):'<li class="rounded-lg border border-dashed border-slate-300 px-3 py-2 text-sm text-slate-500">Senza categoria</li>';const badgeItems=[{label:statusLabel(item.processing_status),className:statusClass(item.processing_status)}];if(gen?.status==='generated')badgeItems.push({label:'Proposta aperta',className:'badge-amber'});if(gen?.status==='approved'||item.latest_approved_generation?.status==='approved')badgeItems.push({label:'Categorie approvate',className:'bg-emerald-100 text-emerald-800'});if(gen?.status==='rejected')badgeItems.push({label:'Categorie rifiutate',className:'badge-rose'});const seenBadgeLabels=new Set();const badges=badgeItems.filter(b=>{const key=b.label.trim().toLocaleLowerCase('it-IT');if(!key||seenBadgeLabels.has(key))return false;seenBadgeLabels.add(key);return true}).map(b=>`<span class="badge ${b.className}">${esc(b.label)}</span>`).join('');card.innerHTML=`<label class="absolute left-3 top-3 z-10 flex h-9 w-9 cursor-pointer items-center justify-center rounded-xl bg-white/95 shadow"><input class="h-5 w-5 rounded border-slate-300 text-blue-600" type="checkbox" ${state.selected.has(item.id)?'checked':''} aria-label="Seleziona ${esc(item.title)}"></label><div class="aspect-[4/3] overflow-hidden bg-slate-100">${item.image_url?`<img class="h-full w-full object-cover transition duration-300 hover:scale-105" loading="lazy" src="${esc(item.image_url)}" alt="${esc(item.title)}">`:'<div class="flex h-full items-center justify-center text-5xl text-slate-300">◇</div>'}</div><div class="p-5"><div class="mb-3 flex flex-wrap gap-2">${badges}</div><h3 class="line-clamp-2 min-h-12 text-base font-bold leading-6 text-slate-950">${esc(item.title)}</h3><p class="mt-1 text-xs text-slate-500">SKU ${esc(sku)} · ${esc(item.vendor||'Vendor —')}</p><ul class="mt-4 space-y-2">${paths}</ul><button class="detail mt-5 w-full rounded-xl border border-blue-200 bg-blue-50 px-4 py-2.5 text-sm font-bold text-blue-700 transition hover:bg-blue-100">Dettagli e storico →</button></div>`;card.querySelector('input').onchange=e=>{e.target.checked?state.selected.add(item.id):state.selected.delete(item.id);render()};card.querySelector('.detail').onclick=()=>openDetail(item.id);g.append(card)}}
async function loadFacets(){const category=$('#category').value,d=await api('/api/facets?category='+encodeURIComponent(category));const set=(el,values,label)=>{const previous=el.value;el.innerHTML=`<option value="">${label}</option>`+values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');if(values.includes(previous))el.value=previous};set($('#vendor'),d.vendors,'Tutti');set($('#category'),d.categories,'Tutte');set($('#subcategory'),d.subcategories,'Tutte');$('#subcategory').disabled=!category}
async function selectFiltered(){try{const d=await api('/api/products?'+params(500,0));d.items.forEach(i=>state.selected.add(i.id));render();if(d.total>500)notice('Selezionati i primi 500 prodotti filtrati. Restringi i filtri per elaborare gli altri.')}catch(e){notice(e.message,true)}}
async function generate(){if(!state.selected.size)return notice('Seleziona almeno un prodotto.',true);try{const r=await api('/api/jobs',{method:'POST',body:JSON.stringify({product_ids:[...state.selected]})});notice(`${r.queued} prodotti aggiunti alla coda di generazione.`);await load();await loadQueue()}catch(e){notice(e.message,true)}}
function historyPaths(v){return!v?.length?'<li>Nessun percorso</li>':v.map(i=>`<li>${esc(pathText(Array.isArray(i)?i:i.path||[]))}</li>`).join('')}
function proposalBox(p,index,status){const tone=status==='approved'?'border-emerald-300 bg-emerald-50':status==='rejected'?'border-rose-300 bg-rose-50':'border-slate-200 bg-white hover:border-blue-300';const input=status==='generated'?`<input class="path-approve mt-1 h-4 w-4 rounded text-blue-600" type="checkbox" value="${index}">`:`<span class="mt-1.5 h-3 w-3 shrink-0 rounded-full ${status==='approved'?'bg-emerald-500':'bg-rose-500'}"></span>`;return `<label class="block rounded-xl border p-3 ${tone} ${status==='generated'?'cursor-pointer':''}"><div class="flex gap-3">${input}<div><div class="font-bold text-slate-900">${esc(pathText(p.livelli))}</div><div class="mt-1 text-xs text-slate-600">Confidenza ${Math.round(p.confidence*100)}% · ${esc(p.motivazione)}</div></div></div></label>`}
function proposalLabel(s){return({generated:'Proposta da valutare',approved:'Categorie approvate',rejected:'Categorie rifiutate',published:'Categorie pubblicate'})[s]||'Proposta'}
async function openDetail(id){try{const d=await api('/api/products/'+encodeURIComponent(id)),product=d.product,gen=product.latest_generation,output=gen?.output||{},status=gen?.status||'',currentPaths=uniquePaths(product.paths);$('#detailTitle').textContent=product.title;let html=`<div class="grid gap-5 lg:grid-cols-2"><section class="compare-card"><p class="eyebrow">Attuale su Shopify</p><h3 class="mb-3 font-bold">Percorsi correnti</h3><ul class="space-y-2">${currentPaths.length?currentPaths.map(p=>`<li class="path-pill">${esc(pathText(p.path))}<span class="ml-auto badge ${p.kind==='smart'?'badge-amber':'badge-blue'}">${p.kind}</span></li>`).join(''):'<li class="text-sm text-slate-500">Nessun percorso</li>'}</ul></section><section class="compare-card"><p class="eyebrow">Proposta AI</p><div class="mb-3 flex items-center justify-between gap-3"><h3 class="font-bold">Stato della proposta</h3>${gen?`<span class="badge ${status==='approved'?'bg-emerald-100 text-emerald-800':status==='rejected'?'badge-rose':'badge-amber'}">${proposalLabel(status)}</span>`:''}</div>`;if(gen&&output.percorsi?.length){html+=`<div class="space-y-3">${output.percorsi.map((p,i)=>proposalBox(p,i,status)).join('')}</div>`;if(status==='generated')html+=`<div class="mt-4 flex flex-wrap gap-2"><button class="approve btn-primary" data-id="${gen.id}">Approva selezionati</button><button class="reject btn-secondary" data-id="${gen.id}">Rifiuta proposta</button></div>`;if(status==='approved')html+=`<p class="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm font-semibold text-emerald-900">Le categorie selezionate sono approvate e pronte per il dry-run.</p><div class="mt-3"><button class="reject btn-secondary" data-id="${gen.id}">Rifiuta proposta</button></div>`;if(status==='rejected')html+='<p class="mt-4 rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm font-semibold text-rose-900">Categorie rifiutate. Genera una nuova proposta se desideri riclassificare il prodotto.</p>';if(status==='published')html+='<p class="mt-4 rounded-xl border border-blue-200 bg-blue-50 p-3 text-sm font-semibold text-blue-900">Categorie già pubblicate su Shopify.</p>';if(output.criticita?.length)html+=`<div class="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"><strong>Criticità</strong><ul class="mt-1 list-disc pl-5">${output.criticita.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`}else html+='<p class="text-sm text-slate-500">Nessuna proposta disponibile.</p>';if(product.latest_approved_generation&&product.latest_approved_generation.id!==gen?.id){const approved=product.latest_approved_generation;html+=`<div class="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm"><strong>Ultime categorie approvate conservate</strong><ul class="mt-2 list-disc pl-5">${historyPaths(approved.approved_paths)}</ul><p class="mt-2">${approved.status==='published'?'Già pubblicate.':'Pronte per la pubblicazione finché non approvi una nuova proposta.'}</p></div>`}html+='</section></div><section class="mt-6"><h3 class="mb-3 text-lg font-bold">Storico completo</h3><div class="space-y-3">';html+=d.history.length?d.history.map(h=>`<details class="rounded-xl border border-slate-200 bg-white p-4"><summary class="cursor-pointer font-semibold">${new Date(h.created_at).toLocaleString('it-IT')} · ${esc(h.origin)} · ${esc(h.outcome)}</summary><div class="mt-3 grid gap-4 text-sm sm:grid-cols-2"><div><strong>Prima</strong><ul class="mt-1 list-disc pl-5">${historyPaths(h.before)}</ul></div><div><strong>Dopo</strong><ul class="mt-1 list-disc pl-5">${historyPaths(h.after)}</ul></div></div>${h.model?`<p class="mt-3 text-xs text-slate-500">${esc(h.model)} · prompt v${h.prompt_version}</p>`:''}</details>`).join(''):'<p class="text-sm text-slate-500">Nessuno storico.</p>';html+='</div></section>';$('#detailBody').innerHTML=html;$('#detailDialog').showModal();const approve=$('.approve');if(approve)approve.onclick=async()=>{const indexes=$$('.path-approve:checked').map(input=>Number(input.value));if(!indexes.length)return notice('Seleziona almeno un percorso.',true);const r=await api(`/api/generations/${approve.dataset.id}/approve`,{method:'POST',body:JSON.stringify({indexes})});notice(r.changed?'Categorie approvate e storico aggiornato.':'Categorie già approvate: storico invariato.');await openDetail(id);await load()};const reject=$('.reject');if(reject)reject.onclick=async()=>{await api(`/api/generations/${reject.dataset.id}/reject`,{method:'POST'});notice('Categorie rifiutate.');await openDetail(id);await load()}}catch(e){notice(e.message,true)}}
function group(title,items,label,color='text-slate-800'){return `<section class="mb-5"><h3 class="mb-2 font-bold">${title} <span class="badge">${items.length}</span></h3>${items.length?`<ul class="space-y-2">${items.map(i=>`<li class="rounded-xl border border-slate-200 px-3 py-2 text-sm ${color}">${esc(label(i))}</li>`).join('')}</ul>`:'<p class="text-sm text-slate-400">Nessuna</p>'}</section>`}
async function previewPublish(){if(!state.selected.size)return notice('Seleziona almeno un prodotto.',true);try{state.preview=await api('/api/publish/preview',{method:'POST',body:JSON.stringify({product_ids:[...state.selected]})});const p=state.preview;$('#previewBody').innerHTML=`<p class="mb-4 text-sm text-slate-600">Controlla le operazioni. Le nuove collection saranno abilitate su tutti i canali Shopify disponibili. In questa fase non è stata effettuata alcuna scrittura.</p>${group('Collection da creare',p.create_collections,i=>pathText(i.path))}${group('Sorgenti manuali da aggiungere',p.create_sources||[],i=>pathText(i.path))}${group('Aggiunte prodotto',p.add_products,i=>`${pathText(i.path)} · ${i.product_ids.length} prodotti`)}${group('Rimozioni prodotto',p.remove_products,i=>`${pathText(i.path)} · ${i.product_ids.length} prodotti`)}${group('Gerarchia collection',p.hierarchy_updates||[],i=>`${pathText(i.path)} · ${i.parent_path.length?'padre: '+pathText(i.parent_path):'categoria principale'}`)}${group('Operazioni bloccate',p.blocked,i=>`${i.path} · ${i.reason}`,'text-rose-700')}`;$('#confirmPublish').disabled=p.blocked.length>0;$('#previewDialog').showModal()}catch(e){notice(e.message,true)}}
async function executePublish(){if(!state.preview)return;const b=$('#confirmPublish');b.disabled=true;b.textContent='Applicazione…';try{const r=await api('/api/publish/execute',{method:'POST',body:JSON.stringify({preview_id:state.preview.preview_id,confirm:true})});$('#previewDialog').close();state.preview=null;notice(`${r.published_products} prodotti aggiornati · ${r.created_collections} collection create.`);await loadFacets();await load()}catch(e){notice(e.message,true);await loadFacets();await load()}finally{b.disabled=false;b.textContent='Conferma e applica'}}
async function previewCleanup(){try{notice('Sincronizzazione fresca prima della pulizia…');const r=await api('/api/cleanup/preview',{method:'POST'});$('#cleanupBody').innerHTML=r.collections.length?`<p class="mb-4 text-sm text-rose-700">Verranno eliminate solo le collection manuali ancora vuote dopo una sincronizzazione fresca. Le smart collection sono escluse. Sarà salvato un backup JSON.</p><div class="space-y-2">${r.collections.map(i=>`<label class="flex items-center gap-3 rounded-xl border border-rose-200 bg-rose-50 p-3"><input class="cleanup-choice h-4 w-4" type="checkbox" value="${esc(i.id)}" checked><span><strong>${esc(pathText(i.path))}</strong><small class="block text-rose-700">${esc(i.id)}</small></span></label>`).join('')}</div>`:'<p class="py-10 text-center text-slate-500">Nessuna collection manuale vuota.</p>';$('#confirmCleanup').disabled=!r.collections.length;$('#cleanupDialog').showModal()}catch(e){notice(e.message,true)}}
async function executeCleanup(){const ids=$$('.cleanup-choice:checked').map(i=>i.value);if(!ids.length)return notice('Seleziona almeno una collection.',true);if(prompt(`Seconda conferma: scrivi ELIMINA per rimuovere ${ids.length} collection manuali vuote.`)!=='ELIMINA')return;const b=$('#confirmCleanup');b.disabled=true;try{const r=await api('/api/cleanup/execute',{method:'POST',body:JSON.stringify({collection_ids:ids,confirm:true})});$('#cleanupDialog').close();notice(`${r.deleted.length} collection eliminate. Backup JSON salvato.`);await loadFacets();await load()}catch(e){notice(e.message,true)}finally{b.disabled=false}}
let loadingQueue=false;async function loadQueue(){if(loadingQueue)return;loadingQueue=true;try{const q=await api('/api/queue');$('#queue').innerHTML=`<strong class="text-slate-900">Shopify</strong><span class="text-slate-500">${q.shopify_processing||0} in corso · ${q.shopify_queued||0} in attesa${q.shopify_current?' · '+esc(({generate:'preparazione categorie',preview:'anteprima',publish:'pubblicazione',sync:'sincronizzazione',cleanup:'pulizia',cleanup_preview:'verifica pulizia'})[q.shopify_current.action]||q.shopify_current.action):''}</span><strong class="text-slate-900">Coda AI</strong><span class="text-slate-500">${q.processing} in corso · ${q.queued} in attesa · ${q.completed} completati · ${q.error} errori</span>${q.current?'<span class="h-4 w-4 animate-spin rounded-full border-2 border-blue-200 border-t-blue-600"></span>':''}<span class="ml-auto"></span><button class="queue-toggle btn-secondary">${q.paused?'Riprendi':'Pausa'}</button><button class="queue-cancel btn-ghost">Annulla attesa</button>`;$('.queue-toggle').onclick=async()=>{await api('/api/queue/'+(q.paused?'resume':'pause'),{method:'POST'});loadQueue()};$('.queue-cancel').onclick=async()=>{await api('/api/queue/cancel',{method:'POST'});loadQueue();load()}}catch(e){notice(e.message,true)}finally{loadingQueue=false}}
async function openSettings(){try{state.settings=await api('/api/settings');for(const [k,v] of Object.entries(state.settings)){const input=$(`[name="${k}"]`);if(input&&typeof v!=='object')input.value=v}state.settings.main_categories=state.settings.main_categories||[];$('#mainCategoryInput').value='';renderMainCategories();mainCategoryFeedback('Le modifiche verranno applicate con Salva impostazioni.');$('#settingsDialog').showModal()}catch(e){notice(e.message,true)}}
function mainCategoryFeedback(message,error=false){const el=$('#mainCategoriesFeedback');el.textContent=message;el.className='mt-2 text-sm '+(error?'text-rose-700':'text-slate-500')}
function renderMainCategories(){const names=state.settings?.main_categories||[];const box=$('#mainCategoriesCards');box.innerHTML=names.length?names.map((name,index)=>`<article class="flex items-start justify-between gap-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm"><div class="min-w-0"><svg class="mb-2 h-7 w-7 text-blue-600" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" aria-hidden="true"><path stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg><p class="break-words text-sm font-bold text-slate-900">${esc(name)}</p><p class="mt-1 text-xs text-slate-500">Categoria principale consentita</p></div><button type="button" data-remove-main="${index}" class="shrink-0 rounded-lg p-2 text-slate-400 transition hover:bg-rose-50 hover:text-rose-600 focus:outline-none focus:ring-2 focus:ring-rose-300" aria-label="Rimuovi ${esc(name)}" title="Rimuovi categoria"><svg class="h-5 w-5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" aria-hidden="true"><path stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v5m4-5v5"/></svg></button></article>`).join(''):'<p class="sm:col-span-2 rounded-xl border border-dashed border-slate-300 bg-white p-4 text-sm text-slate-500">Nessuna categoria principale impostata: l’AI può scegliere liberamente.</p>';box.querySelectorAll('[data-remove-main]').forEach(button=>button.onclick=()=>{state.settings.main_categories.splice(Number(button.dataset.removeMain),1);renderMainCategories();mainCategoryFeedback('Categoria rimossa dall’elenco. Salva per applicare la modifica.');})}
function addMainCategory(){const input=$('#mainCategoryInput'),name=input.value.replace(/\s+/g,' ').trim();if(!name)return mainCategoryFeedback('Inserisci il nome della categoria.',true);if(/[>\r\n]/.test(name))return mainCategoryFeedback('Inserisci un solo nome, senza percorsi con >.',true);if(state.settings.main_categories.some(item=>item.toLocaleLowerCase('it-IT')===name.toLocaleLowerCase('it-IT')))return mainCategoryFeedback('Questa categoria è già presente.',true);if(state.settings.main_categories.length>=100)return mainCategoryFeedback('Puoi impostare al massimo 100 categorie principali.',true);state.settings.main_categories.push(name);input.value='';renderMainCategories();mainCategoryFeedback('Categoria aggiunta. Salva per applicare la modifica.');input.focus()}
$('#addMainCategory').onclick=addMainCategory;
$('#mainCategoryInput').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();addMainCategory()}};
$('#importMainCategories').onclick=async()=>{const button=$('#importMainCategories'),save=$('#settingsForm button[type="submit"]'),draft=state.settings;button.disabled=true;if(save)save.disabled=true;button.textContent='Importazione…';mainCategoryFeedback('Recupero delle collection Shopify senza padre…');try{const result=await api('/api/settings/main-categories/import',{method:'POST'});if(state.settings!==draft||!$('#settingsDialog').open)return;const existing=new Set(draft.main_categories.map(item=>item.toLocaleLowerCase('it-IT')));let count=0;for(const name of result.main_categories){if(!existing.has(name.toLocaleLowerCase('it-IT'))){if(draft.main_categories.length>=100)break;draft.main_categories.push(name);existing.add(name.toLocaleLowerCase('it-IT'));count++}}renderMainCategories();mainCategoryFeedback(`${count} ${count===1?'categoria aggiunta':'categorie aggiunte'} da Shopify. Le categorie manuali sono conservate. Salva per applicare le modifiche.`)}catch(error){if(state.settings===draft)mainCategoryFeedback(error.message,true)}finally{button.disabled=false;button.textContent='Importa da Shopify';if(save)save.disabled=false}};
$('#settingsForm').onsubmit=async e=>{e.preventDefault();const form=new FormData(e.currentTarget),next={...state.settings};for(const k of ['ollama_url','model','ai_prompt'])next[k]=form.get(k);for(const k of ['timeout','max_retries','num_predict','max_depth'])next[k]=Number(form.get(k));for(const k of ['pause_seconds','temperature'])next[k]=Number(form.get(k));delete next.system_prompt;delete next.prompt_version;try{const r=await api('/api/settings',{method:'PUT',body:JSON.stringify(next)});$('#settingsDialog').close();notice(`Impostazioni salvate · prompt v${r.prompt_version}.`)}catch(x){notice(x.message,true)}};
$('#testOllama').onclick=async()=>{const o=$('#ollamaState');o.textContent='Connessione…';try{const r=await api('/api/ollama/test',{method:'POST'});$('#ollamaModels').innerHTML=r.models.map(x=>`<option value="${esc(x)}">`).join('');o.textContent=`${r.message} ${r.models.join(', ')}`}catch(e){o.textContent=e.message}};
$('#syncBtn').onclick=async e=>{e.currentTarget.disabled=true;notice('Sincronizzazione completa di prodotti e collection…');try{const r=await api('/api/sync',{method:'POST'});notice(`${r.products_total} prodotti · ${r.collections_new} collection nuove · ${r.collections_modified} modificate · ${r.collections_removed} rimosse.`);state.offset=0;await loadFacets();await load()}catch(x){notice(x.message,true)}finally{e.currentTarget.disabled=false}};
$('#category').onchange=async()=>{await loadFacets();state.offset=0;load()};['search','subcategory','vendor','processing','uncategorized','withProposal'].forEach(id=>$('#'+id).addEventListener('input',debounce(()=>{state.offset=0;load()})));$('#resetFilters').onclick=async()=>{$('#search').value='';$('#category').value='';$('#subcategory').value='';$('#vendor').value='';$('#processing').value='';$('#uncategorized').checked=false;$('#withProposal').checked=false;state.offset=0;await loadFacets();load()};
$('#selectFiltered').onclick=selectFiltered;$('#clearSelection').onclick=()=>{state.selected.clear();render()};$('#generateBtn').onclick=async()=>{const b=$('#generateBtn');b.disabled=true;try{await generate()}finally{b.disabled=false}};$('#publishBtn').onclick=async()=>{const b=$('#publishBtn');b.disabled=true;try{await previewPublish()}finally{b.disabled=false}};$('#confirmPublish').onclick=executePublish;$('#cleanupBtn').onclick=previewCleanup;$('#confirmCleanup').onclick=executeCleanup;$('#settingsBtn').onclick=openSettings;$('#prev').onclick=()=>{state.offset=Math.max(0,state.offset-state.limit);load()};$('#next').onclick=()=>{state.offset+=state.limit;load()};$$('.close-btn,.close-action').forEach(b=>b.onclick=()=>b.closest('dialog').close());loadFacets().then(load);loadQueue();setInterval(()=>{loadQueue();if(state.items.some(i=>['queued','processing'].includes(i.processing_status)))load()},2500);


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
        <section class="sm:col-span-2 rounded-2xl border border-slate-200 bg-slate-50 p-4" aria-labelledby="mainCategoriesTitle">
          <div class="mb-3 flex flex-wrap items-start justify-between gap-3">
            <div><h3 id="mainCategoriesTitle" class="font-bold text-slate-900">Categorie principali</h3><p class="mt-1 text-sm text-slate-500">L’AI userà queste categorie come primo livello. Lascia vuoto per mantenere la scelta libera.</p></div>
            <button id="importMainCategories" type="button" class="btn-secondary">Importa da Shopify</button>
          </div>
          <label for="mainCategoryInput" class="mb-1 block text-sm font-semibold text-slate-700">Nuova categoria principale</label>
          <div class="flex gap-2"><input id="mainCategoryInput" class="control min-w-0 flex-1" type="text" maxlength="100" placeholder="Es. Ricambi o Veicoli"><button id="addMainCategory" type="button" class="btn-primary">Aggiungi</button></div>
          <p id="mainCategoriesFeedback" class="mt-2 text-sm text-slate-500" role="status" aria-live="polite">Le modifiche verranno applicate con Salva impostazioni.</p>
          <div id="mainCategoriesCards" class="mt-3 grid gap-3 sm:grid-cols-2"></div>
        </section>
        <label class="field sm:col-span-2"><span>Prompt personalizzato</span><textarea class="control min-h-32" name="ai_prompt"></textarea></label>
        <div class="sm:col-span-2 flex items-center gap-3"><button id="testOllama" type="button" class="btn-secondary">Testa Ollama</button><span id="ollamaState" class="text-sm text-slate-500"></span></div>
      </div>
      <div class="dialog-actions"><button type="button" class="btn-secondary close-action">Annulla</button><button type="submit" class="btn-primary">Salva impostazioni</button></div>
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

*,:after,:before{--tw-border-spacing-x:0;--tw-border-spacing-y:0;--tw-translate-x:0;--tw-translate-y:0;--tw-rotate:0;--tw-skew-x:0;--tw-skew-y:0;--tw-scale-x:1;--tw-scale-y:1;--tw-pan-x: ;--tw-pan-y: ;--tw-pinch-zoom: ;--tw-scroll-snap-strictness:proximity;--tw-gradient-from-position: ;--tw-gradient-via-position: ;--tw-gradient-to-position: ;--tw-ordinal: ;--tw-slashed-zero: ;--tw-numeric-figure: ;--tw-numeric-spacing: ;--tw-numeric-fraction: ;--tw-ring-inset: ;--tw-ring-offset-width:0px;--tw-ring-offset-color:#fff;--tw-ring-color:rgba(59,130,246,.5);--tw-ring-offset-shadow:0 0 #0000;--tw-ring-shadow:0 0 #0000;--tw-shadow:0 0 #0000;--tw-shadow-colored:0 0 #0000;--tw-blur: ;--tw-brightness: ;--tw-contrast: ;--tw-grayscale: ;--tw-hue-rotate: ;--tw-invert: ;--tw-saturate: ;--tw-sepia: ;--tw-drop-shadow: ;--tw-backdrop-blur: ;--tw-backdrop-brightness: ;--tw-backdrop-contrast: ;--tw-backdrop-grayscale: ;--tw-backdrop-hue-rotate: ;--tw-backdrop-invert: ;--tw-backdrop-opacity: ;--tw-backdrop-saturate: ;--tw-backdrop-sepia: ;--tw-contain-size: ;--tw-contain-layout: ;--tw-contain-paint: ;--tw-contain-style: }::backdrop{--tw-border-spacing-x:0;--tw-border-spacing-y:0;--tw-translate-x:0;--tw-translate-y:0;--tw-rotate:0;--tw-skew-x:0;--tw-skew-y:0;--tw-scale-x:1;--tw-scale-y:1;--tw-pan-x: ;--tw-pan-y: ;--tw-pinch-zoom: ;--tw-scroll-snap-strictness:proximity;--tw-gradient-from-position: ;--tw-gradient-via-position: ;--tw-gradient-to-position: ;--tw-ordinal: ;--tw-slashed-zero: ;--tw-numeric-figure: ;--tw-numeric-spacing: ;--tw-numeric-fraction: ;--tw-ring-inset: ;--tw-ring-offset-width:0px;--tw-ring-offset-color:#fff;--tw-ring-color:rgba(59,130,246,.5);--tw-ring-offset-shadow:0 0 #0000;--tw-ring-shadow:0 0 #0000;--tw-shadow:0 0 #0000;--tw-shadow-colored:0 0 #0000;--tw-blur: ;--tw-brightness: ;--tw-contrast: ;--tw-grayscale: ;--tw-hue-rotate: ;--tw-invert: ;--tw-saturate: ;--tw-sepia: ;--tw-drop-shadow: ;--tw-backdrop-blur: ;--tw-backdrop-brightness: ;--tw-backdrop-contrast: ;--tw-backdrop-grayscale: ;--tw-backdrop-hue-rotate: ;--tw-backdrop-invert: ;--tw-backdrop-opacity: ;--tw-backdrop-saturate: ;--tw-backdrop-sepia: ;--tw-contain-size: ;--tw-contain-layout: ;--tw-contain-paint: ;--tw-contain-style: }/*! tailwindcss v3.4.17 | MIT License | https://tailwindcss.com*/*,:after,:before{box-sizing:border-box;border:0 solid #e5e7eb}:after,:before{--tw-content:""}:host,html{line-height:1.5;-webkit-text-size-adjust:100%;-moz-tab-size:4;-o-tab-size:4;tab-size:4;font-family:ui-sans-serif,system-ui,sans-serif,Apple Color Emoji,Segoe UI Emoji,Segoe UI Symbol,Noto Color Emoji;font-feature-settings:normal;font-variation-settings:normal;-webkit-tap-highlight-color:transparent}body{margin:0;line-height:inherit}hr{height:0;color:inherit;border-top-width:1px}abbr:where([title]){-webkit-text-decoration:underline dotted;text-decoration:underline dotted}h1,h2,h3,h4,h5,h6{font-size:inherit;font-weight:inherit}a{color:inherit;text-decoration:inherit}b,strong{font-weight:bolder}code,kbd,pre,samp{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,Liberation Mono,Courier New,monospace;font-feature-settings:normal;font-variation-settings:normal;font-size:1em}small{font-size:80%}sub,sup{font-size:75%;line-height:0;position:relative;vertical-align:baseline}sub{bottom:-.25em}sup{top:-.5em}table{text-indent:0;border-color:inherit;border-collapse:collapse}button,input,optgroup,select,textarea{font-family:inherit;font-feature-settings:inherit;font-variation-settings:inherit;font-size:100%;font-weight:inherit;line-height:inherit;letter-spacing:inherit;color:inherit;margin:0;padding:0}button,select{text-transform:none}button,input:where([type=button]),input:where([type=reset]),input:where([type=submit]){-webkit-appearance:button;background-color:transparent;background-image:none}:-moz-focusring{outline:auto}:-moz-ui-invalid{box-shadow:none}progress{vertical-align:baseline}::-webkit-inner-spin-button,::-webkit-outer-spin-button{height:auto}[type=search]{-webkit-appearance:textfield;outline-offset:-2px}::-webkit-search-decoration{-webkit-appearance:none}::-webkit-file-upload-button{-webkit-appearance:button;font:inherit}summary{display:list-item}blockquote,dd,dl,figure,h1,h2,h3,h4,h5,h6,hr,p,pre{margin:0}fieldset{margin:0}fieldset,legend{padding:0}menu,ol,ul{list-style:none;margin:0;padding:0}dialog{padding:0}textarea{resize:vertical}input::-moz-placeholder,textarea::-moz-placeholder{opacity:1;color:#9ca3af}input::placeholder,textarea::placeholder{opacity:1;color:#9ca3af}[role=button],button{cursor:pointer}:disabled{cursor:default}audio,canvas,embed,iframe,img,object,svg,video{display:block;vertical-align:middle}img,video{max-width:100%;height:auto}[hidden]:where(:not([hidden=until-found])){display:none}:focus-visible{outline:2px solid transparent;outline-offset:2px;--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(2px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(59 130 246/var(--tw-ring-opacity,1));--tw-ring-offset-width:2px}dialog::backdrop{background-color:rgba(2,6,23,.55);--tw-backdrop-blur:blur(4px);-webkit-backdrop-filter:var(--tw-backdrop-blur) var(--tw-backdrop-brightness) var(--tw-backdrop-contrast) var(--tw-backdrop-grayscale) var(--tw-backdrop-hue-rotate) var(--tw-backdrop-invert) var(--tw-backdrop-opacity) var(--tw-backdrop-saturate) var(--tw-backdrop-sepia);backdrop-filter:var(--tw-backdrop-blur) var(--tw-backdrop-brightness) var(--tw-backdrop-contrast) var(--tw-backdrop-grayscale) var(--tw-backdrop-hue-rotate) var(--tw-backdrop-invert) var(--tw-backdrop-opacity) var(--tw-backdrop-saturate) var(--tw-backdrop-sepia)}.panel{border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:1.25rem;--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.field{display:flex;flex-direction:column;gap:.375rem;font-size:.875rem;line-height:1.25rem;font-weight:600;--tw-text-opacity:1;color:rgb(51 65 85/var(--tw-text-opacity,1))}.control{width:100%;border-radius:.75rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:.625rem .75rem;font-weight:400;--tw-text-opacity:1;color:rgb(15 23 42/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.control::-moz-placeholder{--tw-text-opacity:1;color:rgb(148 163 184/var(--tw-text-opacity,1))}.control::placeholder{--tw-text-opacity:1;color:rgb(148 163 184/var(--tw-text-opacity,1))}.control:hover{--tw-border-opacity:1;border-color:rgb(148 163 184/var(--tw-border-opacity,1))}.control:focus{--tw-border-opacity:1;border-color:rgb(59 130 246/var(--tw-border-opacity,1));--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(2px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(219 234 254/var(--tw-ring-opacity,1))}.check{display:flex;cursor:pointer;align-items:center;gap:.5rem;font-size:.875rem;line-height:1.25rem;font-weight:500;--tw-text-opacity:1;color:rgb(51 65 85/var(--tw-text-opacity,1))}.check input{height:1rem;width:1rem;border-radius:.25rem;--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1));--tw-text-opacity:1;color:rgb(37 99 235/var(--tw-text-opacity,1))}.check input:focus{--tw-ring-opacity:1;--tw-ring-color:rgb(59 130 246/var(--tw-ring-opacity,1))}.btn-primary{display:inline-flex;align-items:center;justify-content:center;border-radius:.75rem;--tw-bg-opacity:1;background-color:rgb(37 99 235/var(--tw-bg-opacity,1));padding:.625rem 1rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(255 255 255/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.btn-primary:hover{--tw-bg-opacity:1;background-color:rgb(29 78 216/var(--tw-bg-opacity,1))}.btn-primary:disabled{cursor:not-allowed;opacity:.5}.btn-dark{display:inline-flex;align-items:center;justify-content:center;border-radius:.75rem;--tw-bg-opacity:1;background-color:rgb(15 23 42/var(--tw-bg-opacity,1));padding:.625rem 1rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(255 255 255/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.btn-dark:hover{--tw-bg-opacity:1;background-color:rgb(51 65 85/var(--tw-bg-opacity,1))}.btn-dark:disabled{cursor:not-allowed;opacity:.5}.btn-secondary{display:inline-flex;align-items:center;justify-content:center;border-radius:.75rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:.625rem 1rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(51 65 85/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.btn-secondary:hover{--tw-border-opacity:1;border-color:rgb(147 197 253/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(239 246 255/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(29 78 216/var(--tw-text-opacity,1))}.btn-secondary:disabled{cursor:not-allowed;opacity:.5}.btn-ghost{display:inline-flex;align-items:center;justify-content:center;border-radius:.75rem;padding:.625rem .75rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(71 85 105/var(--tw-text-opacity,1));transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.btn-ghost:hover{--tw-bg-opacity:1;background-color:rgb(241 245 249/var(--tw-bg-opacity,1))}.btn-danger{display:inline-flex;align-items:center;justify-content:center;border-radius:.75rem;--tw-bg-opacity:1;background-color:rgb(225 29 72/var(--tw-bg-opacity,1));padding:.625rem 1rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(255 255 255/var(--tw-text-opacity,1));transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.btn-danger:hover{--tw-bg-opacity:1;background-color:rgb(190 18 60/var(--tw-bg-opacity,1))}.btn-danger:disabled{opacity:.5}.btn-danger-soft{display:inline-flex;align-items:center;justify-content:center;border-radius:.75rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(254 205 211/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 241 242/var(--tw-bg-opacity,1));padding:.625rem 1rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(190 18 60/var(--tw-text-opacity,1));transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.btn-danger-soft:hover{--tw-bg-opacity:1;background-color:rgb(255 228 230/var(--tw-bg-opacity,1))}.product-card{position:relative;overflow:hidden;border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.product-card,.product-card:hover{box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.product-card:hover{--tw-translate-y:-0.125rem;transform:translate(var(--tw-translate-x),var(--tw-translate-y)) rotate(var(--tw-rotate)) skewX(var(--tw-skew-x)) skewY(var(--tw-skew-y)) scaleX(var(--tw-scale-x)) scaleY(var(--tw-scale-y));--tw-border-opacity:1;border-color:rgb(147 197 253/var(--tw-border-opacity,1));--tw-shadow:0 10px 15px -3px rgba(0,0,0,.1),0 4px 6px -4px rgba(0,0,0,.1);--tw-shadow-colored:0 10px 15px -3px var(--tw-shadow-color),0 4px 6px -4px var(--tw-shadow-color);--tw-shadow-color:rgba(23,37,84,.05);--tw-shadow:var(--tw-shadow-colored)}.product-card.selected{--tw-border-opacity:1;border-color:rgb(59 130 246/var(--tw-border-opacity,1));--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(2px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(219 234 254/var(--tw-ring-opacity,1))}.badge{display:inline-flex;align-items:center;border-radius:9999px;background-color:rgb(241 245 249/var(--tw-bg-opacity,1));padding:.25rem .625rem;font-size:.75rem;line-height:1rem;font-weight:700;color:rgb(71 85 105/var(--tw-text-opacity,1))}.badge,.badge-blue{--tw-bg-opacity:1;--tw-text-opacity:1}.badge-blue{background-color:rgb(219 234 254/var(--tw-bg-opacity,1));color:rgb(29 78 216/var(--tw-text-opacity,1))}.badge-amber{background-color:rgb(254 243 199/var(--tw-bg-opacity,1));color:rgb(146 64 14/var(--tw-text-opacity,1))}.badge-amber,.badge-rose{--tw-bg-opacity:1;--tw-text-opacity:1}.badge-rose{background-color:rgb(255 228 230/var(--tw-bg-opacity,1));color:rgb(190 18 60/var(--tw-text-opacity,1))}.path-pill{display:flex;align-items:flex-start;gap:.5rem;border-radius:.5rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(219 234 254/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(239 246 255/var(--tw-bg-opacity,1));padding:.5rem .75rem;font-size:.875rem;line-height:1.25rem;font-weight:600;--tw-text-opacity:1;color:rgb(30 58 138/var(--tw-text-opacity,1))}.dialog-shell{margin:auto;width:calc(100% - 2rem);max-width:none;background-color:transparent;padding:0}.dialog-card{margin-left:auto;margin-right:auto;overflow:hidden;border-radius:1rem;--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));--tw-shadow:0 25px 50px -12px rgba(0,0,0,.25);--tw-shadow-colored:0 25px 50px -12px var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.dialog-head{align-items:flex-start;justify-content:space-between;gap:1rem;border-bottom-width:1px;border-color:rgb(226 232 240/var(--tw-border-opacity,1))}.dialog-actions,.dialog-head{display:flex;--tw-border-opacity:1;padding:1rem 1.25rem}.dialog-actions{justify-content:flex-end;gap:.5rem;border-top-width:1px;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(248 250 252/var(--tw-bg-opacity,1))}.close-btn{display:flex;height:2.25rem;width:2.25rem;align-items:center;justify-content:center;border-radius:9999px;font-size:1.5rem;line-height:2rem;--tw-text-opacity:1;color:rgb(100 116 139/var(--tw-text-opacity,1));transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.close-btn:hover{--tw-bg-opacity:1;background-color:rgb(241 245 249/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(15 23 42/var(--tw-text-opacity,1))}.eyebrow{margin-bottom:.25rem;font-size:.75rem;line-height:1rem;font-weight:700;text-transform:uppercase;letter-spacing:.16em;--tw-text-opacity:1;color:rgb(37 99 235/var(--tw-text-opacity,1))}.compare-card{border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(248 250 252/var(--tw-bg-opacity,1));padding:1rem}.absolute{position:absolute}.sticky{position:sticky}.left-3{left:.75rem}.top-3{top:.75rem}.z-10{z-index:10}.z-20{z-index:20}.col-span-full{grid-column:1/-1}.mx-auto{margin-left:auto;margin-right:auto}.mb-1{margin-bottom:.25rem}.mb-2{margin-bottom:.5rem}.mb-3{margin-bottom:.75rem}.mb-4{margin-bottom:1rem}.mb-5{margin-bottom:1.25rem}.ml-auto{margin-left:auto}.mt-1{margin-top:.25rem}.mt-1\.5{margin-top:.375rem}.mt-2{margin-top:.5rem}.mt-3{margin-top:.75rem}.mt-4{margin-top:1rem}.mt-5{margin-top:1.25rem}.mt-6{margin-top:1.5rem}.line-clamp-2{overflow:hidden;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2}.block{display:block}.flex{display:flex}.grid{display:grid}.hidden{display:none}.aspect-\[4\/3\]{aspect-ratio:4/3}.h-1\.5{height:.375rem}.h-2{height:.5rem}.h-3{height:.75rem}.h-4{height:1rem}.h-5{height:1.25rem}.h-7{height:1.75rem}.h-9{height:2.25rem}.h-full{height:100%}.max-h-\[70vh\]{max-height:70vh}.max-h-\[72vh\]{max-height:72vh}.max-h-\[78vh\]{max-height:78vh}.min-h-12{min-height:3rem}.min-h-14{min-height:3.5rem}.min-h-32{min-height:8rem}.min-h-screen{min-height:100vh}.w-1\.5{width:.375rem}.w-2{width:.5rem}.w-3{width:.75rem}.w-4{width:1rem}.w-5{width:1.25rem}.w-7{width:1.75rem}.w-9{width:2.25rem}.w-full{width:100%}.min-w-0{min-width:0}.max-w-2xl{max-width:42rem}.max-w-3xl{max-width:48rem}.max-w-6xl{max-width:72rem}.max-w-screen-2xl{max-width:1536px}.flex-1{flex:1 1 0%}.shrink-0{flex-shrink:0}@keyframes spin{to{transform:rotate(1turn)}}.animate-spin{animation:spin 1s linear infinite}.cursor-pointer{cursor:pointer}.list-disc{list-style-type:disc}.flex-col{flex-direction:column}.flex-wrap{flex-wrap:wrap}.items-start{align-items:flex-start}.items-end{align-items:flex-end}.items-center{align-items:center}.justify-center{justify-content:center}.justify-between{justify-content:space-between}.gap-2{gap:.5rem}.gap-3{gap:.75rem}.gap-4{gap:1rem}.gap-5{gap:1.25rem}.space-y-2>:not([hidden])~:not([hidden]){--tw-space-y-reverse:0;margin-top:calc(.5rem*(1 - var(--tw-space-y-reverse)));margin-bottom:calc(.5rem*var(--tw-space-y-reverse))}.space-y-3>:not([hidden])~:not([hidden]){--tw-space-y-reverse:0;margin-top:calc(.75rem*(1 - var(--tw-space-y-reverse)));margin-bottom:calc(.75rem*var(--tw-space-y-reverse))}.overflow-hidden{overflow:hidden}.overflow-y-auto{overflow-y:auto}.break-words{overflow-wrap:break-word}.rounded{border-radius:.25rem}.rounded-2xl{border-radius:1rem}.rounded-full{border-radius:9999px}.rounded-lg{border-radius:.5rem}.rounded-xl{border-radius:.75rem}.border{border-width:1px}.border-2{border-width:2px}.border-b{border-bottom-width:1px}.border-dashed{border-style:dashed}.border-amber-200{--tw-border-opacity:1;border-color:rgb(253 230 138/var(--tw-border-opacity,1))}.border-blue-100{--tw-border-opacity:1;border-color:rgb(219 234 254/var(--tw-border-opacity,1))}.border-blue-200{--tw-border-opacity:1;border-color:rgb(191 219 254/var(--tw-border-opacity,1))}.border-emerald-200{--tw-border-opacity:1;border-color:rgb(167 243 208/var(--tw-border-opacity,1))}.border-emerald-300{--tw-border-opacity:1;border-color:rgb(110 231 183/var(--tw-border-opacity,1))}.border-rose-200{--tw-border-opacity:1;border-color:rgb(254 205 211/var(--tw-border-opacity,1))}.border-rose-300{--tw-border-opacity:1;border-color:rgb(253 164 175/var(--tw-border-opacity,1))}.border-slate-200{--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1))}.border-slate-300{--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1))}.border-t-blue-600{--tw-border-opacity:1;border-top-color:rgb(37 99 235/var(--tw-border-opacity,1))}.bg-amber-50{--tw-bg-opacity:1;background-color:rgb(255 251 235/var(--tw-bg-opacity,1))}.bg-amber-500{--tw-bg-opacity:1;background-color:rgb(245 158 11/var(--tw-bg-opacity,1))}.bg-blue-100{--tw-bg-opacity:1;background-color:rgb(219 234 254/var(--tw-bg-opacity,1))}.bg-blue-50{--tw-bg-opacity:1;background-color:rgb(239 246 255/var(--tw-bg-opacity,1))}.bg-blue-500{--tw-bg-opacity:1;background-color:rgb(59 130 246/var(--tw-bg-opacity,1))}.bg-blue-600{--tw-bg-opacity:1;background-color:rgb(37 99 235/var(--tw-bg-opacity,1))}.bg-emerald-100{--tw-bg-opacity:1;background-color:rgb(209 250 229/var(--tw-bg-opacity,1))}.bg-emerald-50{--tw-bg-opacity:1;background-color:rgb(236 253 245/var(--tw-bg-opacity,1))}.bg-emerald-500{--tw-bg-opacity:1;background-color:rgb(16 185 129/var(--tw-bg-opacity,1))}.bg-rose-50{--tw-bg-opacity:1;background-color:rgb(255 241 242/var(--tw-bg-opacity,1))}.bg-rose-500{--tw-bg-opacity:1;background-color:rgb(244 63 94/var(--tw-bg-opacity,1))}.bg-slate-100{--tw-bg-opacity:1;background-color:rgb(241 245 249/var(--tw-bg-opacity,1))}.bg-slate-50{--tw-bg-opacity:1;background-color:rgb(248 250 252/var(--tw-bg-opacity,1))}.bg-white{--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1))}.bg-white\/95{background-color:hsla(0,0%,100%,.95)}.object-cover{-o-object-fit:cover;object-fit:cover}.p-2{padding:.5rem}.p-3{padding:.75rem}.p-4{padding:1rem}.p-5{padding:1.25rem}.px-3{padding-left:.75rem;padding-right:.75rem}.px-4{padding-left:1rem;padding-right:1rem}.py-1{padding-top:.25rem;padding-bottom:.25rem}.py-10{padding-top:2.5rem;padding-bottom:2.5rem}.py-14{padding-top:3.5rem;padding-bottom:3.5rem}.py-2{padding-top:.5rem;padding-bottom:.5rem}.py-2\.5{padding-top:.625rem;padding-bottom:.625rem}.py-3{padding-top:.75rem;padding-bottom:.75rem}.py-6{padding-top:1.5rem;padding-bottom:1.5rem}.pl-5{padding-left:1.25rem}.text-center{text-align:center}.text-2xl{font-size:1.5rem;line-height:2rem}.text-5xl{font-size:3rem;line-height:1}.text-\[10px\]{font-size:10px}.text-base{font-size:1rem;line-height:1.5rem}.text-lg{font-size:1.125rem;line-height:1.75rem}.text-sm{font-size:.875rem;line-height:1.25rem}.text-xl{font-size:1.25rem;line-height:1.75rem}.text-xs{font-size:.75rem;line-height:1rem}.font-bold{font-weight:700}.font-medium{font-weight:500}.font-normal{font-weight:400}.font-semibold{font-weight:600}.uppercase{text-transform:uppercase}.leading-6{line-height:1.5rem}.tracking-\[\.2em\]{letter-spacing:.2em}.tracking-tight{letter-spacing:-.025em}.text-amber-700{--tw-text-opacity:1;color:rgb(180 83 9/var(--tw-text-opacity,1))}.text-amber-900{--tw-text-opacity:1;color:rgb(120 53 15/var(--tw-text-opacity,1))}.text-blue-600{--tw-text-opacity:1;color:rgb(37 99 235/var(--tw-text-opacity,1))}.text-blue-700{--tw-text-opacity:1;color:rgb(29 78 216/var(--tw-text-opacity,1))}.text-blue-900{--tw-text-opacity:1;color:rgb(30 58 138/var(--tw-text-opacity,1))}.text-emerald-800{--tw-text-opacity:1;color:rgb(6 95 70/var(--tw-text-opacity,1))}.text-emerald-900{--tw-text-opacity:1;color:rgb(6 78 59/var(--tw-text-opacity,1))}.text-rose-600{--tw-text-opacity:1;color:rgb(225 29 72/var(--tw-text-opacity,1))}.text-rose-700{--tw-text-opacity:1;color:rgb(190 18 60/var(--tw-text-opacity,1))}.text-rose-900{--tw-text-opacity:1;color:rgb(136 19 55/var(--tw-text-opacity,1))}.text-slate-300{--tw-text-opacity:1;color:rgb(203 213 225/var(--tw-text-opacity,1))}.text-slate-400{--tw-text-opacity:1;color:rgb(148 163 184/var(--tw-text-opacity,1))}.text-slate-500{--tw-text-opacity:1;color:rgb(100 116 139/var(--tw-text-opacity,1))}.text-slate-600{--tw-text-opacity:1;color:rgb(71 85 105/var(--tw-text-opacity,1))}.text-slate-700{--tw-text-opacity:1;color:rgb(51 65 85/var(--tw-text-opacity,1))}.text-slate-800{--tw-text-opacity:1;color:rgb(30 41 59/var(--tw-text-opacity,1))}.text-slate-900{--tw-text-opacity:1;color:rgb(15 23 42/var(--tw-text-opacity,1))}.text-slate-950{--tw-text-opacity:1;color:rgb(2 6 23/var(--tw-text-opacity,1))}.antialiased{-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}.shadow{--tw-shadow:0 1px 3px 0 rgba(0,0,0,.1),0 1px 2px -1px rgba(0,0,0,.1);--tw-shadow-colored:0 1px 3px 0 var(--tw-shadow-color),0 1px 2px -1px var(--tw-shadow-color)}.shadow,.shadow-lg{box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.shadow-lg{--tw-shadow:0 10px 15px -3px rgba(0,0,0,.1),0 4px 6px -4px rgba(0,0,0,.1);--tw-shadow-colored:0 10px 15px -3px var(--tw-shadow-color),0 4px 6px -4px var(--tw-shadow-color)}.shadow-sm{--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.shadow-blue-950\/5{--tw-shadow-color:rgba(23,37,84,.05);--tw-shadow:var(--tw-shadow-colored)}.filter{filter:var(--tw-blur) var(--tw-brightness) var(--tw-contrast) var(--tw-grayscale) var(--tw-hue-rotate) var(--tw-invert) var(--tw-saturate) var(--tw-sepia) var(--tw-drop-shadow)}.backdrop-blur{--tw-backdrop-blur:blur(8px);-webkit-backdrop-filter:var(--tw-backdrop-blur) var(--tw-backdrop-brightness) var(--tw-backdrop-contrast) var(--tw-backdrop-grayscale) var(--tw-backdrop-hue-rotate) var(--tw-backdrop-invert) var(--tw-backdrop-opacity) var(--tw-backdrop-saturate) var(--tw-backdrop-sepia);backdrop-filter:var(--tw-backdrop-blur) var(--tw-backdrop-brightness) var(--tw-backdrop-contrast) var(--tw-backdrop-grayscale) var(--tw-backdrop-hue-rotate) var(--tw-backdrop-invert) var(--tw-backdrop-opacity) var(--tw-backdrop-saturate) var(--tw-backdrop-sepia)}.transition{transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.duration-300{transition-duration:.3s}.hover\:scale-105:hover{--tw-scale-x:1.05;--tw-scale-y:1.05;transform:translate(var(--tw-translate-x),var(--tw-translate-y)) rotate(var(--tw-rotate)) skewX(var(--tw-skew-x)) skewY(var(--tw-skew-y)) scaleX(var(--tw-scale-x)) scaleY(var(--tw-scale-y))}.hover\:border-blue-300:hover{--tw-border-opacity:1;border-color:rgb(147 197 253/var(--tw-border-opacity,1))}.hover\:bg-blue-100:hover{--tw-bg-opacity:1;background-color:rgb(219 234 254/var(--tw-bg-opacity,1))}.hover\:bg-rose-50:hover{--tw-bg-opacity:1;background-color:rgb(255 241 242/var(--tw-bg-opacity,1))}.hover\:text-blue-800:hover{--tw-text-opacity:1;color:rgb(30 64 175/var(--tw-text-opacity,1))}.hover\:text-rose-600:hover{--tw-text-opacity:1;color:rgb(225 29 72/var(--tw-text-opacity,1))}.focus\:outline-none:focus{outline:2px solid transparent;outline-offset:2px}.focus\:ring-2:focus{--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(2px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000)}.focus\:ring-rose-300:focus{--tw-ring-opacity:1;--tw-ring-color:rgb(253 164 175/var(--tw-ring-opacity,1))}@media (min-width:640px){.sm\:col-span-2{grid-column:span 2/span 2}.sm\:grid-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}.sm\:text-3xl{font-size:1.875rem;line-height:2.25rem}}@media (min-width:768px){.md\:grid-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (min-width:1024px){.lg\:ml-auto{margin-left:auto}.lg\:grid-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}.lg\:flex-row{flex-direction:row}.lg\:items-center{align-items:center}.lg\:justify-between{justify-content:space-between}.lg\:px-8{padding-left:2rem;padding-right:2rem}}@media (min-width:1280px){.xl\:col-span-2{grid-column:span 2/span 2}.xl\:grid-cols-3{grid-template-columns:repeat(3,minmax(0,1fr))}.xl\:grid-cols-6{grid-template-columns:repeat(6,minmax(0,1fr))}}@media (min-width:1536px){.\32xl\:grid-cols-4{grid-template-columns:repeat(4,minmax(0,1fr))}}

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


def test_dry_run_creates_adds_removes_and_adds_source_for_smart():
    collections = [
        {"id": "manual-old", "source_id": "source-old", "path": ["Vecchia"], "kind": "manual", "removed": 0},
        {"id": "smart", "path": ["Automatica"], "kind": "smart", "removed": 0},
    ]
    products = [{"id": "p", "current": [{"id": "manual-old", "source_id": "source-old", "path": ["Vecchia"], "kind": "manual"}], "desired": [["Nuova"], ["Automatica"]]}]
    result = build_dry_run(products, collections)
    assert result["create_collections"] == [{"path": ["Nuova"], "title": "Nuova"}]
    assert result["remove_products"][0]["collection_id"] == "manual-old"
    assert result["blocked"] == []
    assert result["create_sources"] == [{"collection_id": "smart", "path": ["Automatica"]}]


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
async def test_publish_reconciles_changed_memberships_and_sets_parent(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB", tmp_path / "publish.sqlite")
    app.init_db()
    pid = "gid://shopify/Product/1"
    desired = [["Motore", "Carburatori"]]
    signature = stable_hash({"id": "g", "paths": desired})
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES(?,?,?,?)", (pid, "P", "x", app.now()))
        db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,approved_paths_json,status,source_hash,created_at) VALUES('g',?,1,'m','{}',?,'approved','obsolete',?)", (pid, json.dumps(desired), app.now()))
        operations = {"approval_signatures": {pid: signature}}
        db.execute("INSERT INTO publish_previews VALUES(?,?,?,?,?,0)", ("preview", json.dumps([pid]), json.dumps(operations), '{}', app.now()))
    mutations = []
    members = []
    published = set()
    async def fresh(_):
        return list(members), "changed"
    async def fake_mutation(query, variables, field):
        mutations.append((field, variables))
        if field == "collectionCreate":
            title = variables['collection']['title']
            cid = 'gid://shopify/Collection/' + title
            members.append({'id': cid, 'path': [title] if title == 'Motore' else desired[0], 'kind': 'manual', 'source_id': 'source-' + title})
            return {"collection": {"id": cid, "sources": [{"__typename": "CollectionConditionsSource", "id": "source-" + title}]}}
        if field == 'publishablePublish':
            published.add(variables['id'])
        return {"metafields": [], "userErrors": []}
    async def no_sync(*_):
        return {}
    async def fake_graphql(query, variables):
        if query == app.PUBLICATION_SCOPES_QUERY:
            return {'currentAppInstallation': {'accessScopes': [{'handle': 'write_publications'}]}}
        if query == app.PUBLICATIONS_QUERY:
            return {'publications': {'nodes': [{'id': 'channel-1'}, {'id': 'channel-2'}], 'pageInfo': {'hasNextPage': False}}}
        if query == app.COLLECTION_PUBLICATIONS_QUERY:
            return {'collection': {'resourcePublicationsV2': {'nodes': [{'publication': {'id': channel}, 'isPublished': variables['id'] in published} for channel in ['channel-1', 'channel-2']], 'pageInfo': {'hasNextPage': False}}}}
        if query == app.PARENT_DEFINITION_QUERY:
            return {'metafieldDefinitions': {'nodes': [{'type': {'name': 'collection_reference'}}]}}
        return {'collectionByHandle': None}
    monkeypatch.setattr(app, "fresh_product_state", fresh)
    monkeypatch.setattr(app, "mutation", fake_mutation)
    monkeypatch.setattr(app, "graphql", fake_graphql)
    monkeypatch.setattr(app, "sync_shopify", no_sync)
    result = await app.publish_execute({"preview_id": "preview", "confirm": True})
    assert result["created_collections"] == 2
    creates = [variables for field, variables in mutations if field == 'collectionCreate']
    assert [item['collection']['title'] for item in creates] == ['Motore', 'Carburatori']
    assert all(item['collection']['sources'][0]['source']['inclusion']['selections'] == [{'productId': pid}] for item in creates)
    parent = [item for field, variables in mutations if field == 'metafieldsSet' for item in variables['metafields'] if item['key'] == 'parent_collection']
    assert parent[0]['value'] == 'gid://shopify/Collection/Motore'
    assert len(published) == 2
    assert all(len(variables['input']) == 2 for field, variables in mutations if field == 'publishablePublish')
    assert not any(field == 'collectionUpdate' for field, _ in mutations)
    with app.connect() as db:
        assert db.execute("SELECT status FROM generations WHERE id='g'").fetchone()[0] == 'published'


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


def test_hierarchy_uses_reference_ids_and_supports_lists():
    for field in ({'type': 'collection_reference', 'value': 'parent'}, {'type': 'list.collection_reference', 'value': '["parent"]'}):
        items = [{'id': 'child', 'title': 'Gomme', 'parentCollection': field}, {'id': 'parent', 'title': 'Ricambi'}]
        app.resolve_collection_hierarchy(items)
        assert items[0]['resolved_path'] == ['Ricambi', 'Gomme']


@pytest.mark.parametrize('items', [
    [{'id': 'a', 'title': 'A', 'parentCollection': {'type': 'collection_reference', 'value': 'a'}}],
    [{'id': 'a', 'title': 'A', 'parentCollection': {'type': 'collection_reference', 'value': 'missing'}}],
    [{'id': 'a', 'title': 'A', 'parentCollection': {'type': 'list.collection_reference', 'value': '["x","y"]'}}],
    [{'id': 'a', 'title': 'A', 'parentCollection': {'type': 'list.single_line_text_field', 'value': '["A"]'}}],
])
def test_invalid_hierarchy_is_rejected(items):
    with pytest.raises(RuntimeError):
        app.resolve_collection_hierarchy(items)


def test_all_ancestors_created_and_added_and_existing_child_reused():
    collections = [{'id': 'child', 'title': 'Camere', 'path': ['Ricambi', 'Gomme', 'Camere'], 'kind': 'manual', 'source_id': 's'}]
    result = build_dry_run([{'id': 'p', 'desired': [['Ricambi', 'Gomme', 'Camere']], 'current': []}], collections)
    assert [item['path'] for item in result['create_collections']] == [['Ricambi'], ['Ricambi', 'Gomme']]
    assert len(result['add_products']) == 3
    assert next(item for item in result['add_products'] if len(item['path']) == 3)['collection_id'] == 'child'


def test_legacy_title_is_reused_and_ambiguous_title_is_blocked():
    from core import match_collection_paths
    products = [{'id': 'p', 'desired': [['Ricambi', 'Camere']]}]
    collections = [{'id': 'a', 'title': 'Camere', 'path': ['Camere'], 'kind': 'manual', 'source_id': 's'}]
    matched = match_collection_paths(products, collections)
    assert matched[0]['path'] == ['Ricambi', 'Camere']
    assert collections[0]['path'] == ['Camere']
    with pytest.raises(ValueError):
        match_collection_paths(products, collections + [{**collections[0], 'id': 'b'}])


@pytest.mark.asyncio
async def test_sync_failure_preserves_approved_and_memberships(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'failure.sqlite')
    app.init_db()
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES('p','P','x',?)", (app.now(),))
        db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,approved_paths_json,status,source_hash,created_at) VALUES('g','p',1,'m','{}','[[\"Ricambi\"]]','approved','x',?)", (app.now(),))
    async def products():
        return []
    async def failure():
        raise RuntimeError('429 exhausted')
    monkeypatch.setattr(app, 'fetch_all_products', products)
    monkeypatch.setattr(app, 'fetch_all_collections', failure)
    with pytest.raises(RuntimeError):
        await app.sync_shopify()
    with app.connect() as db:
        assert db.execute("SELECT approved_paths_json,status FROM generations WHERE id='g'").fetchone()[:] == ('[["Ricambi"]]', 'approved')


@pytest.mark.asyncio
async def test_approval_supersedes_old_approval_and_empty_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'approve.sqlite')
    app.init_db()
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES('p','P','x',?)", (app.now(),))
        for gid, status, date in [('old', 'approved', '2026-10-01'), ('new', 'generated', '2026-10-02')]:
            db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,status,source_hash,created_at) VALUES(?,'p',1,'m',?,?,'x',?)", (gid, json.dumps({'percorsi': [{'livelli': ['Nuova']}]}), status, date))
    with pytest.raises(app.HTTPException):
        await app.approve('old', {'indexes': [0]})
    with pytest.raises(app.HTTPException):
        await app.approve('new', {'indexes': []})
    await app.approve('new', {'indexes': [0]})
    with app.connect() as db:
        assert db.execute("SELECT status FROM generations WHERE id='old'").fetchone()[0] == 'superseded'
        assert app.approved_products(db, ['p'])[0]['desired'] == [['Nuova']]


@pytest.mark.asyncio
async def test_partial_publish_keeps_approval_and_retry_reuses_collections(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'retry.sqlite')
    app.init_db()
    pid = 'p'
    desired = [['Ricambi']]
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES('p','P','x',?)", (app.now(),))
        db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,approved_paths_json,status,source_hash,created_at) VALUES('g','p',1,'m','{}',?,'approved','x',?)", (json.dumps(desired), app.now()))
        ops = {'approval_signatures': {pid: stable_hash({'id': 'g', 'paths': desired})}}
        db.execute('INSERT INTO publish_previews VALUES(?,?,?,?,?,0)', ('preview', '["p"]', json.dumps(ops), '{}', app.now()))
    remote = []
    creates = 0
    fail = True
    async def fake_sync(origin):
        with app.connect() as db:
            for item in remote:
                db.execute("INSERT OR REPLACE INTO collections(id,title,handle,path_json,kind,source_id,products_count,shopify_hash,last_synced_at) VALUES(?,'Ricambi','category-test','[\"Ricambi\"]','manual','s',1,'x',?)", (item['id'], app.now()))
                db.execute('INSERT OR IGNORE INTO product_collections VALUES(?,?)', (pid, item['id']))
        return {}
    async def fresh(_):
        return list(remote), 'fresh'
    async def graphql(query, variables):
        if query == app.PUBLICATION_SCOPES_QUERY:
            return {'currentAppInstallation': {'accessScopes': [{'handle': 'write_publications'}]}}
        if query == app.PUBLICATIONS_QUERY:
            return {'publications': {'nodes': [{'id': 'channel'}], 'pageInfo': {'hasNextPage': False}}}
        if query == app.COLLECTION_PUBLICATIONS_QUERY:
            return {'collection': {'resourcePublicationsV2': {'nodes': [{'publication': {'id': 'channel'}, 'isPublished': True}], 'pageInfo': {'hasNextPage': False}}}}
        if query == app.PARENT_DEFINITION_QUERY:
            return {'metafieldDefinitions': {'nodes': [{'type': {'name': 'list.collection_reference'}}]}}
        return {'collectionByHandle': None}
    async def mutate(query, variables, field):
        nonlocal creates, fail
        if field == 'collectionCreate':
            creates += 1
            remote.append({'id': 'c', 'title': 'Ricambi', 'path': ['Ricambi'], 'kind': 'manual', 'source_id': 's'})
            return {'collection': {'id': 'c', 'sources': [{'__typename': 'CollectionConditionsSource', 'id': 's'}]}}
        if field == 'metafieldsSet' and fail:
            fail = False
            raise RuntimeError('Timeout after creation')
        return {}
    monkeypatch.setattr(app, 'sync_shopify', fake_sync)
    monkeypatch.setattr(app, 'fresh_product_state', fresh)
    monkeypatch.setattr(app, 'graphql', graphql)
    monkeypatch.setattr(app, 'mutation', mutate)
    with pytest.raises(app.HTTPException) as error:
        await app.publish_execute({'preview_id': 'preview', 'confirm': True})
    assert error.value.status_code == 502
    with app.connect() as db:
        assert db.execute("SELECT status FROM generations WHERE id='g'").fetchone()[0] == 'approved'
        assert db.execute("SELECT used FROM publish_previews WHERE id='preview'").fetchone()[0] == 0
    result = await app.publish_execute({'preview_id': 'preview', 'confirm': True})
    assert result['published_products'] == 1
    assert result['created_collections'] == 0
    assert creates == 1
    with pytest.raises(app.HTTPException) as error:
        await app.publish_execute({'preview_id': 'preview', 'confirm': True})
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_changed_approval_invalidates_preview_before_writing(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'changed.sqlite')
    app.init_db()
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES('p','P','x',?)", (app.now(),))
        db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,approved_paths_json,status,source_hash,created_at) VALUES('g','p',1,'m','{}','[[\"Nuova\"]]','approved','x',?)", (app.now(),))
        db.execute('INSERT INTO publish_previews VALUES(?,?,?,?,?,0)', ('preview', '["p"]', json.dumps({'approval_signatures': {'p': 'previous'}}), '{}', app.now()))
    async def no_sync(*_):
        return {}
    async def no_mutations(*_):
        pytest.fail('Stale approval must never write')
    monkeypatch.setattr(app, 'sync_shopify', no_sync)
    monkeypatch.setattr(app, 'mutation', no_mutations)
    with pytest.raises(app.HTTPException) as error:
        await app.publish_execute({'preview_id': 'preview', 'confirm': True})
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_fresh_hierarchy_loads_missing_parent_from_shopify(monkeypatch):
    items = {'child': {'id': 'child', 'title': 'Camere', 'categoryPath': {'value': 'Vecchio > Camere'}, 'parentCollection': {'type': 'collection_reference', 'value': 'parent'}}}
    calls = []
    async def graphql(query, variables):
        calls.append(variables['id'])
        return {'collection': {'id': 'parent', 'title': 'Gomme', 'categoryPath': {'value': 'Ricambi > Gomme'}}}
    monkeypatch.setattr(app, 'graphql', graphql)
    await app.hydrate_collection_parents(items)
    app.resolve_collection_hierarchy(list(items.values()))
    assert items['child']['resolved_path'] == ['Ricambi', 'Gomme', 'Camere']
    assert calls == ['parent']


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['http429', 'graphql_throttled'])
async def test_shopify_throttling_retries_safely(monkeypatch, first):
    import httpx
    responses = []
    if first == 'http429':
        responses.append(httpx.Response(429, headers={'Retry-After': '1'}, request=httpx.Request('POST', 'https://test')))
    else:
        responses.append(httpx.Response(200, json={'errors': [{'message': 'Throttled', 'extensions': {'code': 'THROTTLED'}}]}, request=httpx.Request('POST', 'https://test')))
    responses.append(httpx.Response(200, json={'data': {'ok': True}}, request=httpx.Request('POST', 'https://test')))
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            return responses.pop(0)
    async def token(_):
        return 'test-token'
    async def sleep(_):
        pass
    monkeypatch.setattr(app.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(app, 'shop_token', token)
    monkeypatch.setattr(app, 'shop_domain', lambda: 'test.myshopify.com')
    monkeypatch.setattr(app.asyncio, 'sleep', sleep)
    assert await app.graphql('mutation Test{test}', {}) == {'ok': True}
    assert responses == []


@pytest.mark.asyncio
async def test_ambiguous_mutation_timeout_is_not_retried(monkeypatch):
    import httpx
    calls = 0
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout('lost response')
    async def token(_):
        return 'test-token'
    monkeypatch.setattr(app.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(app, 'shop_token', token)
    monkeypatch.setattr(app, 'shop_domain', lambda: 'test.myshopify.com')
    with pytest.raises(RuntimeError, match='non è stata ritentata'):
        await app.graphql('mutation Create{test}', {})
    assert calls == 1


def test_reused_legacy_member_is_never_removed_when_path_is_repaired():
    from core import match_collection_paths
    legacy = {'id': 'c', 'title': 'Camere', 'path': ['Camere'], 'kind': 'manual', 'source_id': 's'}
    products = [{'id': 'p', 'current': [legacy], 'desired': [['Ricambi', 'Camere']]}]
    result = build_dry_run(products, match_collection_paths(products, [legacy]))
    assert result['remove_products'] == []
    assert [item['path'] for item in result['create_collections']] == [['Ricambi']]
    assert all(item['collection_id'] != 'c' for item in result['add_products'])


@pytest.mark.asyncio
async def test_channel_list_is_paginated_and_deduplicated(monkeypatch):
    cursors = []
    async def graphql(query, variables):
        if query == app.PUBLICATION_SCOPES_QUERY:
            return {'currentAppInstallation': {'accessScopes': [{'handle': 'write_publications'}]}}
        cursors.append(variables['after'])
        return {'publications': {'nodes': [{'id': 'one'}, {'id': 'two'}] if variables['after'] is None else [{'id': 'two'}, {'id': 'three'}], 'pageInfo': {'hasNextPage': variables['after'] is None, 'endCursor': 'next'}}}
    monkeypatch.setattr(app, 'graphql', graphql)
    assert await app.collection_channel_ids() == ['one', 'two', 'three']
    assert cursors == [None, 'next']


@pytest.mark.asyncio
async def test_missing_publication_scope_blocks_before_enumerating_channels(monkeypatch):
    calls = []
    async def graphql(query, variables):
        calls.append(query)
        return {'currentAppInstallation': {'accessScopes': [{'handle': 'write_products'}]}}
    monkeypatch.setattr(app, 'graphql', graphql)
    with pytest.raises(RuntimeError, match='write_publications'):
        await app.collection_channel_ids()
    assert calls == [app.PUBLICATION_SCOPES_QUERY]


@pytest.mark.asyncio
async def test_partial_channel_publication_retry_only_publishes_missing(monkeypatch):
    published = {'one'}
    requests = []
    fail = True
    async def graphql(query, variables):
        return {'collection': {'resourcePublicationsV2': {'nodes': [{'publication': {'id': channel}, 'isPublished': True} for channel in published], 'pageInfo': {'hasNextPage': False}}}}
    async def mutation(query, variables, field):
        nonlocal fail
        requests.append([item['publicationId'] for item in variables['input']])
        if fail:
            fail = False
            published.add('two')
            raise RuntimeError('Response lost after partial write')
        published.update(requests[-1])
        return {}
    monkeypatch.setattr(app, 'graphql', graphql)
    monkeypatch.setattr(app, 'mutation', mutation)
    with pytest.raises(RuntimeError):
        await app.ensure_collection_channels('collection', ['one', 'two', 'three'])
    await app.ensure_collection_channels('collection', ['one', 'two', 'three'])
    assert requests == [['three', 'two'], ['three']]
    assert published == {'one', 'two', 'three'}


@pytest.mark.asyncio
async def test_unconfirmed_channels_are_never_reported_as_success(monkeypatch):
    async def graphql(query, variables):
        return {'collection': {'resourcePublicationsV2': {'nodes': [], 'pageInfo': {'hasNextPage': False}}}}
    async def mutation(*args):
        return {}
    async def sleep(_):
        pass
    monkeypatch.setattr(app, 'graphql', graphql)
    monkeypatch.setattr(app, 'mutation', mutation)
    monkeypatch.setattr(app.asyncio, 'sleep', sleep)
    with pytest.raises(RuntimeError, match='non conferma'):
        await app.ensure_collection_channels('collection', ['one'])


@pytest.mark.asyncio
async def test_collection_publication_status_paginates_and_ignores_unpublished(monkeypatch):
    async def graphql(query, variables):
        return {'collection': {'resourcePublicationsV2': {'nodes': [{'publication': {'id': 'one'}, 'isPublished': True}, {'publication': {'id': 'scheduled'}, 'isPublished': False}] if variables['after'] is None else [{'publication': {'id': 'two'}, 'isPublished': True}], 'pageInfo': {'hasNextPage': variables['after'] is None, 'endCursor': 'next'}}}}
    monkeypatch.setattr(app, 'graphql', graphql)
    assert await app.published_collection_channels('collection') == {'one', 'two'}


@pytest.mark.asyncio
async def test_operation_submission_is_immediate_deduplicated_and_durable(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'operations.sqlite')
    app.init_db()
    async def network(*_):
        pytest.fail('Submitting must not call Shopify or AI')
    monkeypatch.setattr(app, 'sync_shopify', network)
    payload = {'action': 'generate', 'payload': {'product_ids': ['p']}}
    first = await app.submit_operation(payload)
    assert await app.submit_operation(payload) == first
    assert (await app.operation_status(first['operation_id']))['status'] == 'queued'
    with app.connect() as db:
        db.execute("UPDATE operations SET status='processing'")
    app.init_db()
    assert (await app.operation_status(first['operation_id']))['status'] == 'queued'


@pytest.mark.asyncio
async def test_operation_failure_remains_visible(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'op_error.sqlite')
    app.init_db()
    async def failed():
        raise RuntimeError('Shopify unavailable')
    monkeypatch.setattr(app, 'sync_endpoint', failed)
    job = await app.submit_operation({'action': 'sync'})
    with app.connect() as db:
        row = db.execute('SELECT * FROM operations WHERE id=?', (job['operation_id'],)).fetchone()
    await app.run_operation(row)
    status = await app.operation_status(job['operation_id'])
    assert status['status'] == 'error'
    assert status['error'] == 'Shopify unavailable'


@pytest.mark.asyncio
async def test_oauth_token_is_reused_until_expiry(monkeypatch):
    import time
    monkeypatch.delenv('SHOPIFY_ADMIN_ACCESS_TOKEN', raising=False)
    monkeypatch.setenv('SHOPIFY_CLIENT_ID', 'test-client')
    monkeypatch.setenv('SHOPIFY_CLIENT_SECRET', 'test-secret')
    monkeypatch.setattr(app, 'shop_domain', lambda: 'test.myshopify.com')
    monkeypatch.setattr(app, '_token_cache', {})
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {'access_token': 'cached', 'expires_in': 3600}
    class Client:
        calls = 0
        async def post(self, *args, **kwargs):
            self.calls += 1
            return Response()
    client = Client()
    assert await app.shop_token(client) == 'cached'
    assert await app.shop_token(client) == 'cached'
    assert client.calls == 1
    app._token_cache['expires_at'] = time.monotonic() - 1
    await app.shop_token(client)
    assert client.calls == 2


@pytest.mark.asyncio
async def test_ai_worker_processes_only_one_product_at_a_time(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'serial.sqlite')
    app.init_db()
    with app.connect() as db:
        for index in range(3):
            pid = str(index)
            db.execute('INSERT INTO products(id,title,content_hash,last_synced_at) VALUES(?,?,?,?)', (pid, 'P', 'x', app.now()))
            db.execute("INSERT INTO jobs(id,product_id,kind,status,created_at) VALUES(?,?,'generate','queued',?)", (pid, pid, app.now()))
    active = maximum = completed = 0
    done = asyncio.Event()
    async def process(job):
        nonlocal active, maximum, completed
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        active -= 1
        completed += 1
        if completed == 3:
            done.set()
        return 0.001
    monkeypatch.setattr(app, 'process_job', process)
    monkeypatch.setattr(app, 'get_settings', lambda: app.Settings(pause_seconds=0))
    task = asyncio.create_task(app.worker())
    try:
        await asyncio.wait_for(done.wait(), timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert maximum == 1
    assert completed == 3


@pytest.mark.asyncio
async def test_bulk_reader_checks_completeness_and_preserves_empty_collections(monkeypatch):
    lines = ['{"id":"c1"}', '{"id":"p1","__parentId":"c1"}', '{"id":"c2"}']
    class Response:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def raise_for_status(self):
            pass
        async def aiter_lines(self):
            for line in lines:
                yield line
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def stream(self, *args):
            return Response()
    monkeypatch.setattr(app.httpx, 'AsyncClient', Client)
    assert await app.read_bulk_memberships('https://example.test', {'c1', 'c2'}, 3) == {'c1': ['p1'], 'c2': []}
    with pytest.raises(RuntimeError, match='incompleta'):
        await app.read_bulk_memberships('https://example.test', {'c1', 'c2'}, 4)
    with pytest.raises(RuntimeError, match='Catalogo'):
        await app.read_bulk_memberships('https://example.test', {'c1', 'c2', 'c3'}, 3)


@pytest.mark.asyncio
async def test_cancelled_generation_preparation_does_not_queue_ai(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'cancel.sqlite')
    app.init_db()
    job = await app.submit_operation({'action': 'generate', 'payload': {'product_ids': ['p']}})
    async def cancelled_sync(*_):
        await app.queue_action('cancel')
        return {}
    async def inference(*_):
        pytest.fail('Cancelled preparation must not reach AI')
    monkeypatch.setattr(app, 'sync_shopify', cancelled_sync)
    monkeypatch.setattr(app, 'validate_ollama', inference)
    with app.connect() as db:
        row = db.execute('SELECT * FROM operations WHERE id=?', (job['operation_id'],)).fetchone()
    await app.run_operation(row)
    assert (await app.operation_status(job['operation_id']))['status'] == 'cancelled'
    with app.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


def test_canonical_reuse_collapses_synonyms_without_crossing_branches():
    from core import reuse_canonical_categories
    categories = [{'percorso': ['Ricambi', 'Accessori', 'Adesivi'], 'origine': 'shopify', 'tipo': 'manual', 'prodotti_count': 20}]
    def proposal(path):
        return {'percorsi': [{'livelli': path, 'confidence': .9, 'motivazione': 'Famiglia coerente'}], 'nuove_categorie': [path], 'criticita': []}
    fixed = reuse_canonical_categories(proposal(['ricambi', 'accessori', 'Adesivi e decalcomanie']), categories, 3)
    assert fixed['percorsi'][0]['livelli'] == categories[0]['percorso']
    assert fixed['nuove_categorie'] == []
    separate = reuse_canonical_categories(proposal(['Veicoli', 'Accessori', 'Adesivi e decalcomanie']), categories, 3)
    assert separate['nuove_categorie'] == [['Veicoli', 'Accessori', 'Adesivi e decalcomanie']]
    distinct = reuse_canonical_categories(proposal(['Ricambi', 'Accessori', 'Protezioni']), categories, 3)
    assert distinct['nuove_categorie'] == [['Ricambi', 'Accessori', 'Protezioni']]


def test_new_categories_derived_from_selected_paths_not_model_claims():
    from core import reuse_canonical_categories
    output = {'percorsi': [{'livelli': ['Ricambi', 'Radiatori'], 'confidence': .9, 'motivazione': 'Raffreddamento'}], 'nuove_categorie': [['Inutilizzata']], 'criticita': []}
    assert reuse_canonical_categories(output, [], 3)['nuove_categorie'] == [['Ricambi', 'Radiatori']]


@pytest.mark.asyncio
async def test_actual_ollama_prompt_contains_shopify_and_unpublished_approved_taxonomy(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'prompt.sqlite')
    app.init_db()
    with app.connect() as db:
        for pid in ['target', 'approved', 'rejected']:
            db.execute('INSERT INTO products(id,title,content_hash,last_synced_at) VALUES(?,?,?,?)', (pid, 'Kit adesivi quad', 'x', app.now()))
        db.execute("INSERT INTO collections(id,title,path_json,kind,source_id,products_count,shopify_hash,last_synced_at) VALUES('c','Adesivi','[\"Ricambi\",\"Adesivi\"]','manual','s',20,'x',?)", (app.now(),))
        for pid, status, path in [('approved', 'approved', ['Ricambi', 'Plastiche']), ('rejected', 'rejected', ['Ricambi', 'Categoria sbagliata'])]:
            db.execute('INSERT INTO generations(id,product_id,prompt_version,model,output_json,approved_paths_json,status,source_hash,created_at) VALUES(?,?,1,\'m\',\'{}\',?,?,\'x\',?)', (pid, pid, json.dumps([path]), status, app.now()))
        row = db.execute("SELECT * FROM products WHERE id='target'").fetchone()
    captured = []
    class Response:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            output = {'percorsi': [{'livelli': ['Ricambi', 'Adesivi e decalcomanie'], 'confidence': 1, 'motivazione': 'Stessa famiglia'}], 'nuove_categorie': [['Ricambi', 'Adesivi e decalcomanie']], 'criticita': []}
            return {'message': {'content': json.dumps({'target': output})}}
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, json):
            captured.append(json)
            return Response()
    async def available(_):
        return ['m']
    monkeypatch.setattr(app, 'validate_ollama', available)
    monkeypatch.setattr(app.httpx, 'AsyncClient', Client)
    result = await app.call_ollama(row, app.Settings(model='m'))
    source = json.loads(captured[0]['messages'][1]['content'].split('unica fonte ammessa:\n', 1)[1])['target']
    categories = source['catalogo_collection_esistenti']
    assert {'Ricambi > Adesivi', 'Ricambi > Plastiche'} == {' > '.join(c['percorso']) for c in categories}
    assert next(c for c in categories if c['percorso'][-1] == 'Adesivi')['collection_id'] == 'c'
    assert next(c for c in categories if c['percorso'][-1] == 'Plastiche')['origine'] == 'approvazione'
    assert result['percorsi'][0]['livelli'] == ['Ricambi', 'Adesivi']
    assert result['nuove_categorie'] == []


def test_default_prompt_upgrade_preserves_custom_prompts(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'settings.sqlite')
    app.init_db()
    for old, expected in [(app.PREVIOUS_DEFAULT_PROMPT, app.DEFAULT_PROMPT), ('Il mio prompt personalizzato', 'Il mio prompt personalizzato')]:
        with app.connect() as db:
            settings = app.Settings(ai_prompt=old)
            db.execute('UPDATE settings SET value_json=? WHERE id=1', (settings.model_dump_json(),))
        app.init_db()
        assert app.get_settings().ai_prompt == expected


def test_main_categories_normalize_duplicates_and_reject_nested_names():
    assert app.Settings(main_categories=[' Ricambi ', 'ricambi', '  Veicoli  ']).main_categories == ['Ricambi', 'Veicoli']
    assert app.Settings().main_categories == []
    for invalid in [[''], ['Ricambi > Gomme'], ['Ricambi\nVeicoli'], [123], 'Ricambi']:
        with pytest.raises(ValueError):
            app.Settings(main_categories=invalid)


@pytest.mark.asyncio
async def test_import_main_categories_pages_filters_parent_and_preserves_local_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'roots.sqlite')
    app.init_db()
    await app.update_settings(app.Settings(main_categories=['Manuale']))
    queries = []
    async def graphql(query, variables):
        queries.append(variables['after'])
        if variables['after'] is None:
            nodes = [{'id': '1', 'title': 'Ricambi', 'parentCollection': None}, {'id': '2', 'title': 'Gomme', 'parentCollection': {'type': 'collection_reference', 'value': 'parent'}}, {'id': '3', 'title': 'Veicoli', 'parentCollection': {'type': 'list.collection_reference', 'value': ' [] '}}]
        else:
            nodes = [{'id': '4', 'title': 'ricambi', 'parentCollection': None}, {'id': '5', 'title': 'Accessori', 'parentCollection': {'type': 'list.collection_reference', 'value': '["parent"]'}}, {'id': '6', 'title': 'Abbigliamento', 'parentCollection': {'type': 'collection_reference', 'value': ''}}]
        return {'collections': {'nodes': nodes, 'pageInfo': {'hasNextPage': variables['after'] is None, 'endCursor': 'next'}}}
    monkeypatch.setattr(app, 'graphql', graphql)
    assert (await app.import_main_categories())['main_categories'] == ['Abbigliamento', 'Ricambi', 'Veicoli']
    assert queries == [None, 'next']
    assert app.get_settings().main_categories == ['Manuale']


def test_main_category_enforcement_rejects_ai_invented_roots_without_remapping():
    output = {'percorsi': [{'livelli': ['Ricambi', 'Gomme']}], 'nuove_categorie': [['Ricambi', 'Gomme']], 'criticita': []}
    assert app.enforce_main_categories(output, []) is output
    assert app.enforce_main_categories(output, ['RICAMBI'])['percorsi'][0]['livelli'] == ['RICAMBI', 'Gomme']
    with pytest.raises(RuntimeError, match='non consentite'):
        app.enforce_main_categories({'percorsi': [{'livelli': ['Inventata', 'Gomme']}], 'nuove_categorie': [], 'criticita': []}, ['Ricambi'])


@pytest.mark.asyncio
async def test_main_categories_are_saved_and_included_in_ollama_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'root_prompt.sqlite')
    app.init_db()
    settings = app.Settings(main_categories=['Ricambi'], model='m')
    await app.update_settings(settings)
    assert app.get_settings().main_categories == ['Ricambi']
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES('p','Gomme','x',?)", (app.now(),))
        row = db.execute("SELECT * FROM products WHERE id='p'").fetchone()
    captured = []
    class Response:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {'message': {'content': json.dumps({'p': {'percorsi': [{'livelli': ['Ricambi', 'Gomme'], 'confidence': 1, 'motivazione': 'Famiglia corretta'}], 'nuove_categorie': [], 'criticita': []}})}}
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, json):
            captured.append(json)
            return Response()
    async def available(_):
        return ['m']
    monkeypatch.setattr(app.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(app, 'validate_ollama', available)
    result = await app.call_ollama(row, settings)
    prompt = captured[0]['messages'][1]['content']
    assert '"categorie_principali_consentite":["Ricambi"]' in prompt
    assert 'VINCOLO OBBLIGATORIO' in prompt
    assert result['percorsi'][0]['livelli'][0] == 'Ricambi'


@pytest.mark.asyncio
async def test_main_categories_change_prompt_version_and_empty_restores_current_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'root_version.sqlite')
    app.init_db()
    original = app.ensure_prompt_version()
    await app.update_settings(app.Settings(main_categories=['Ricambi']))
    assert app.ensure_prompt_version() != original
    await app.update_settings(app.Settings(main_categories=[]))
    assert app.ensure_prompt_version() == original


@pytest.mark.parametrize('value,expected', [(100, 1), (95, .95), (.95, .95), (1, 1), (0, 0), ('100', 1), ('95%', .95), ('1%', .01), ('0,95', .95)])
def test_ollama_confidence_accepts_probabilities_and_percentages(value, expected):
    proposal = app.AIProposal.model_validate({'percorsi': [{'livelli': ['Ricambi', 'Pastiglie freno'], 'confidence': value, 'motivazione': 'Ricambio freni'}], 'nuove_categorie': [], 'criticita': []})
    assert proposal.percorsi[0].confidence == expected


@pytest.mark.parametrize('value', [-1, 101, '101%', True, float('nan'), float('inf'), 'non valida'])
def test_ollama_confidence_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        app.PathProposal(livelli=['Ricambi'], confidence=value, motivazione='Ricambio')


def test_main_categories_constrain_first_level_in_ollama_json_schema():
    schema = app.proposal_schema('p', ['Ricambi', 'Veicoli'])
    levels = schema['properties']['p']['properties']['percorsi']['items']['properties']['livelli']
    assert levels['prefixItems'][0]['enum'] == ['Ricambi', 'Veicoli']
    assert levels['items'] == {'type': 'string'}
    free = app.proposal_schema('p')['properties']['p']['properties']['percorsi']['items']['properties']['livelli']
    assert 'prefixItems' not in free


def test_smart_parent_and_child_accept_additions_and_keep_automatic_memberships():
    collections = [
        {'id': 'root', 'path': ['Ricambi'], 'kind': 'smart', 'source_id': 'root-source'},
        {'id': 'quad', 'path': ['Ricambi', 'Ricambi quad 4t'], 'kind': 'smart', 'source_id': 'quad-source'},
        {'id': 'old', 'path': ['Vecchia automatica'], 'kind': 'smart', 'source_id': 'old-source'},
    ]
    plan = build_dry_run([{'id': 'p', 'desired': [['Ricambi', 'Ricambi quad 4t']], 'current': [collections[2]]}], collections)
    assert plan['blocked'] == []
    assert plan['create_collections'] == []
    assert {item['source_id'] for item in plan['add_products']} == {'root-source', 'quad-source'}
    assert plan['remove_products'] == []
    assert plan['create_sources'] == []


def test_writable_source_does_not_touch_shared_or_variant_sources():
    sources = [
        {'id': 'shared', '__typename': 'CollectionConditionsSource', 'targetType': 'PRODUCTS', 'shareable': True},
        {'id': 'variants', '__typename': 'CollectionConditionsSource', 'targetType': 'VARIANTS', 'shareable': False},
        {'id': 'sub', '__typename': 'CollectionSubCollectionsSource'},
        {'id': 'auto', '__typename': 'CollectionConditionsSource', 'targetType': 'PRODUCTS', 'shareable': False, 'inclusion': {'conditions': [{'id': 'tag-rule'}]}},
    ]
    assert app.writable_collection_source(sources) == 'auto'
    assert app.writable_collection_source(sources[:3]) is None


@pytest.mark.asyncio
async def test_sync_keeps_rules_and_exposes_selection_source_for_smart(monkeypatch, tmp_path):
    import copy
    monkeypatch.setattr(app, 'DB', tmp_path / 'smart-sync.sqlite')
    app.init_db()
    item = collection_fixture('smart')
    item['sources'][0].update(targetType='PRODUCTS', shareable=False)
    before = copy.deepcopy(item)
    async def products():
        return [product_fixture()]
    async def collections():
        return [item]
    monkeypatch.setattr(app, 'fetch_all_products', products)
    monkeypatch.setattr(app, 'fetch_all_collections', collections)
    await app.sync_shopify()
    assert item['sources'] == before['sources']
    with app.connect() as db:
        row = db.execute('SELECT kind,source_id FROM collections').fetchone()
        assert row['kind'] == 'smart'
        assert row['source_id'] == 'source-1'


@pytest.mark.asyncio
async def test_new_scoped_source_preserves_shared_rules_and_is_reused(monkeypatch):
    import copy
    shared = {'id': 'shared', '__typename': 'CollectionConditionsSource', 'targetType': 'PRODUCTS', 'shareable': True, 'inclusion': {'conditions': [{'id': 'rule', 'values': ['quad']}], 'matchType': 'ALL'}}
    before = copy.deepcopy(shared)
    sources = [shared]
    updates = []
    async def graphql(query, variables):
        return {'collection': {'id': 'collection', 'sources': sources}}
    async def mutate(query, variables, field):
        updates.append(variables['collection'])
        sources.append({'id': 'scoped', '__typename': 'CollectionConditionsSource', 'targetType': 'PRODUCTS', 'shareable': False, 'inclusion': {'conditions': []}})
        return {'job': None}
    monkeypatch.setattr(app, 'graphql', graphql)
    monkeypatch.setattr(app, 'mutation', mutate)
    assert await app.ensure_product_selection_source('collection', ['p']) == ('scoped', 1)
    assert shared == before
    assert updates == [{'id': 'collection', 'sourcesToCreate': [{'source': {'title': "Prodotti approvati dall'app", 'targetType': 'PRODUCTS', 'inclusion': {'selections': [{'productId': 'p'}]}}}]}]
    assert await app.ensure_product_selection_source('collection', ['p']) == ('scoped', 0)
    assert len(updates) == 1


@pytest.mark.asyncio
async def test_publish_adds_to_smart_with_selection_delta_only(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'smart-publish.sqlite')
    app.init_db()
    desired = [['Ricambi']]
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES('p','P','x',?)", (app.now(),))
        db.execute("INSERT INTO collections(id,title,handle,path_json,kind,source_id,products_count,shopify_hash,last_synced_at,category_value) VALUES('c','Ricambi','ricambi','[\"Ricambi\"]','smart','auto-source',10,'x',?,'Ricambi')", (app.now(),))
        db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,approved_paths_json,status,source_hash,created_at) VALUES('g','p',1,'m','{}',?,'approved','x',?)", (json.dumps(desired), app.now()))
        db.execute('INSERT INTO publish_previews VALUES(?,?,?,?,?,0)', ('preview', '["p"]', json.dumps({'approval_signatures': {'p': stable_hash({'id': 'g', 'paths': desired})}}), '{}', app.now()))
    updates = []
    members = []
    async def sync(*_):
        return {}
    async def fresh(_):
        return list(members), 'hash'
    async def graphql(query, variables):
        assert query == app.PARENT_DEFINITION_QUERY
        return {'metafieldDefinitions': {'nodes': [{'type': {'name': 'collection_reference'}}]}}
    async def mutate(query, variables, field):
        assert field == 'collectionUpdate'
        updates.append(variables['collection'])
        members.append({'id': 'c', 'title': 'Ricambi', 'path': ['Ricambi'], 'kind': 'smart', 'source_id': 'auto-source'})
        return {'job': None}
    monkeypatch.setattr(app, 'sync_shopify', sync)
    monkeypatch.setattr(app, 'fresh_product_state', fresh)
    monkeypatch.setattr(app, 'graphql', graphql)
    monkeypatch.setattr(app, 'mutation', mutate)
    result = await app.publish_execute({'preview_id': 'preview', 'confirm': True})
    assert result['published_products'] == 1
    assert result['created_collections'] == 0
    assert updates == [{'id': 'c', 'sourcesToUpdate': [{'condition': {'id': 'auto-source', 'inclusion': {'selectionsToAdd': [{'productId': 'p'}]}}}]}]


@pytest.mark.asyncio
@pytest.mark.parametrize('selected,remove,delete_source', [(['p'], ['p'], True), (['p','q'], ['p'], False), (['p'], ['missing'], False)])
async def test_removal_of_last_manual_selection_detaches_source(monkeypatch, selected, remove, delete_source):
    updates = []
    async def graphql(query, variables):
        return {'node': {'id': 's', 'shareable': False, 'targetType': 'PRODUCTS', 'inclusion': {'conditions': [], 'selections': {'nodes': [{'product': {'id': pid}} for pid in selected], 'pageInfo': {'hasNextPage': False}}}, 'exclusion': {'conditions': []}}}
    async def mutate(query, variables, field):
        updates.append(variables['collection'])
        return {'job': None}
    monkeypatch.setattr(app, 'graphql', graphql)
    monkeypatch.setattr(app, 'mutation', mutate)
    await app.remove_manual_source_products('c','s',remove)
    if delete_source:
        assert updates == [{'id': 'c', 'sourcesToDelete': ['s']}]
    elif 'p' in remove:
        assert updates == [{'id': 'c', 'sourcesToUpdate': [{'condition': {'id': 's', 'inclusion': {'selectionsToRemove': [{'productId': 'p'}]}}}]}]
    else:
        assert updates == []


@pytest.mark.asyncio
async def test_remove_manual_source_never_deletes_source_with_rules(monkeypatch):
    async def graphql(query, variables):
        return {'node': {'id': 's', 'shareable': False, 'targetType': 'PRODUCTS', 'inclusion': {'conditions': [{'id': 'rule'}]}}}
    async def mutate(*_):
        pytest.fail('Rules must not be modified')
    monkeypatch.setattr(app, 'graphql', graphql)
    monkeypatch.setattr(app, 'mutation', mutate)
    with pytest.raises(RuntimeError, match='regole automatiche'):
        await app.remove_manual_source_products('c','s',['p'])


def test_published_latest_approval_prevents_reapplying_old_pending_categories(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'old-approvals.sqlite')
    app.init_db()
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES('p','P','x',?)", (app.now(),))
        for gid,status,date,path in [('old','approved','2026-10-10',[['Vecchia']]), ('new','published','2026-10-14',[['Nuova']])]:
            db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,approved_paths_json,status,source_hash,created_at,approved_at) VALUES(?,'p',1,'m','{}',?,?,'x',?,?)", (gid,json.dumps(path),status,date,date))
        assert app.approved_products(db,['p']) == []
        assert app.supersede_old_approvals(db) == 1
        assert db.execute("SELECT status FROM generations WHERE id='old'").fetchone()[0] == 'superseded'
        assert db.execute("SELECT status FROM generations WHERE id='new'").fetchone()[0] == 'published'


@pytest.mark.asyncio
async def test_manual_removal_checks_all_pages_before_detaching_source(monkeypatch):
    updates = []
    async def graphql(query, variables):
        after = variables['after']
        return {'node': {'id': 's', 'targetType': 'PRODUCTS', 'shareable': False, 'inclusion': {'conditions': [], 'selections': {'nodes': [{'product': {'id': 'p' if after is None else 'q'}}], 'pageInfo': {'hasNextPage': after is None, 'endCursor': 'next'}}}, 'exclusion': {'conditions': []}}}
    async def mutate(query, variables, field):
        updates.append(variables['collection'])
        return {'job': None}
    monkeypatch.setattr(app, 'graphql', graphql)
    monkeypatch.setattr(app, 'mutation', mutate)
    await app.remove_manual_source_products('c','s',['p'])
    assert updates == [{'id': 'c', 'sourcesToUpdate': [{'condition': {'id': 's', 'inclusion': {'selectionsToRemove': [{'productId': 'p'}]}}}]}]

