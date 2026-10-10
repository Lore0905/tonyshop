
=================================
FILE: .env.example
=================================

SHOPIFY_SHOP_DOMAIN=your-store.myshopify.com
SHOPIFY_ADMIN_ACCESS_TOKEN=
# Alternative to a static token:
SHOPIFY_CLIENT_ID=
SHOPIFY_CLIENT_SECRET=
SHOPIFY_API_VERSION=2026-10
APP_HOST=127.0.0.1
APP_PORT=8770



=================================
FILE: app.py
=================================

from __future__ import annotations
import asyncio, hashlib, json, logging, os, sqlite3, time, uuid
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
from seo import DEFAULT_WEIGHTS, score_content

BASE = Path(__file__).resolve().parent; DATA = BASE / "data"; DB = DATA / "seo.sqlite"; LOG = DATA / "app.log"
DATA.mkdir(exist_ok=True); load_dotenv(BASE / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.FileHandler(LOG), logging.StreamHandler()])
logger = logging.getLogger("shopify_seo_ai_studio")
SYSTEM_PROMPT = """Sei il motore strutturato di Shopify SEO AI Studio. Rispondi esclusivamente con JSON valido conforme allo schema ricevuto. Usa come unica chiave il GID Shopify fornito. Non aggiungere Markdown o commenti. Non inventare compatibilità, marche, modelli, misure, materiali, codici, EAN o dati tecnici. Conserva il significato tecnico. Il campo voto è una valutazione consultiva: il punteggio finale viene ricalcolato dal backend."""
DEFAULT_SEO_PROMPT = """Agisci come un esperto SEO specializzato nell'e-commerce italiano di ricambi per minimoto, Pit Bike, quad, scooter elettrici e componenti meccanici. Migliora titolo, descrizione, meta title e meta description usando solo dati documentati. Evita keyword stuffing, ripetizioni e promesse ingannevoli. Scrivi in italiano con tono professionale, privilegiando accuratezza, chiarezza e intento di ricerca."""

def now(): return datetime.now(timezone.utc).isoformat()
def connect():
    c=sqlite3.connect(DB, timeout=30); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA journal_mode=WAL"); return c
def jdump(v): return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
def jload(v, default=None):
    try: return json.loads(v)
    except (TypeError, json.JSONDecodeError): return default
def fingerprint(data): return hashlib.sha256(jdump(data).encode()).hexdigest()

class Proposal(BaseModel):
    model_config=ConfigDict(extra="forbid")
    voto:int=Field(ge=0,le=100); title:str=Field(min_length=1,max_length=255); descriptionHtml:str=Field(min_length=1)
    metaTitle:str=Field(max_length=255); metaDescription:str=Field(max_length=500); keywords:list[str]=Field(default_factory=list,max_length=20)
    criticita:list[str]=Field(default_factory=list); modificheSuggerite:list[str]=Field(default_factory=list)

class Settings(BaseModel):
    ollama_url:HttpUrl|str="http://127.0.0.1:11434"; model:str="llama3.2"; timeout:int=Field(180,ge=10,le=1800)
    pause_seconds:float=Field(15,ge=0,le=3600); max_retries:int=Field(2,ge=0,le=10); batch_size:int=Field(20,ge=1,le=500)
    min_score:int=Field(50,ge=1,le=100); num_predict:int=Field(1400,ge=128,le=16384); temperature:float=Field(.2,ge=0,le=2)
    language:str="Italiano"; shop_name:str=""; description_style:str="Professionale"; priority_keywords:str=""; seo_prompt:str=DEFAULT_SEO_PROMPT
    weights:dict[str,int]=Field(default_factory=lambda:DEFAULT_WEIGHTS.copy())
    @field_validator("ollama_url")
    @classmethod
    def local_http(cls,v):
        s=str(v).rstrip("/")
        if not s.startswith(("http://","https://")): raise ValueError("URL Ollama non valido")
        return s
    @field_validator("weights")
    @classmethod
    def weights_total(cls,v):
        if set(v)!=set(DEFAULT_WEIGHTS) or sum(v.values())!=100: raise ValueError("I pesi devono contenere le sei categorie e sommare a 100")
        return v

class OllamaConfigurationError(RuntimeError):
    """Permanent configuration error: retrying the same job cannot fix it."""

def init_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY,title TEXT NOT NULL,description_html TEXT NOT NULL DEFAULT '',meta_title TEXT NOT NULL DEFAULT '',meta_description TEXT NOT NULL DEFAULT '',image_url TEXT,status TEXT NOT NULL,product_type TEXT NOT NULL DEFAULT '',vendor TEXT NOT NULL DEFAULT '',handle TEXT NOT NULL DEFAULT '',variants_json TEXT NOT NULL DEFAULT '[]',shopify_updated_at TEXT,content_hash TEXT NOT NULL,last_synced_at TEXT NOT NULL,current_score INTEGER,score_details_json TEXT,processing_status TEXT NOT NULL DEFAULT 'to_analyze',last_error TEXT);
        CREATE TABLE IF NOT EXISTS shopify_snapshots(id TEXT PRIMARY KEY,product_id TEXT NOT NULL,content_json TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL,reason TEXT NOT NULL,FOREIGN KEY(product_id) REFERENCES products(id));
        CREATE TABLE IF NOT EXISTS analyses(id TEXT PRIMARY KEY,product_id TEXT NOT NULL,score INTEGER NOT NULL,details_json TEXT NOT NULL,source TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(product_id) REFERENCES products(id));
        CREATE TABLE IF NOT EXISTS prompt_versions(id INTEGER PRIMARY KEY AUTOINCREMENT,version INTEGER UNIQUE NOT NULL,system_prompt TEXT NOT NULL,seo_prompt TEXT NOT NULL,settings_hash TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS generations(id TEXT PRIMARY KEY,product_id TEXT NOT NULL,prompt_version INTEGER NOT NULL,model TEXT NOT NULL,output_json TEXT NOT NULL,score INTEGER NOT NULL,status TEXT NOT NULL,source_hash TEXT NOT NULL,created_at TEXT NOT NULL,approved_at TEXT,published_at TEXT,rejected_at TEXT,FOREIGN KEY(product_id) REFERENCES products(id));
        CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,field TEXT NOT NULL,approved INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(generation_id,field),FOREIGN KEY(generation_id) REFERENCES generations(id));
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,product_id TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,next_attempt_at REAL,error TEXT,duration REAL);
        CREATE TABLE IF NOT EXISTS publications(id TEXT PRIMARY KEY,product_id TEXT NOT NULL,generation_id TEXT NOT NULL,fields_json TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,status TEXT NOT NULL,error TEXT,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1),value_json TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS queue_state(id INTEGER PRIMARY KEY CHECK(id=1),paused INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS error_logs(id TEXT PRIMARY KEY,scope TEXT NOT NULL,reference_id TEXT,message TEXT NOT NULL,created_at TEXT NOT NULL);
        """)
        c.execute("INSERT OR IGNORE INTO settings VALUES(1,?,?)",(Settings().model_dump_json(),now()))
        c.execute("INSERT OR IGNORE INTO queue_state VALUES(1,0,?)",(now(),))
        c.execute("UPDATE jobs SET status='queued',started_at=NULL WHERE status='processing'")
        c.execute("UPDATE products SET processing_status='queued' WHERE processing_status='processing'")
    ensure_prompt_version()

def get_settings():
    with connect() as c: row=c.execute("SELECT value_json FROM settings WHERE id=1").fetchone()
    return Settings.model_validate_json(row[0])
def ensure_prompt_version():
    s=get_settings(); h=fingerprint({"system":SYSTEM_PROMPT,"seo":s.seo_prompt,"language":s.language,"style":s.description_style,"keywords":s.priority_keywords,"weights":s.weights})
    with connect() as c:
        row=c.execute("SELECT version FROM prompt_versions WHERE settings_hash=?",(h,)).fetchone()
        if row:return row[0]
        version=(c.execute("SELECT COALESCE(MAX(version),0)+1 FROM prompt_versions").fetchone()[0])
        c.execute("INSERT INTO prompt_versions(version,system_prompt,seo_prompt,settings_hash,created_at) VALUES(?,?,?,?,?)",(version,SYSTEM_PROMPT,s.seo_prompt,h,now()))
        c.execute("UPDATE products SET processing_status='reevaluate' WHERE processing_status IN ('completed','published')")
        return version

def content_of(row): return {"title":row["title"],"descriptionHtml":row["description_html"],"metaTitle":row["meta_title"],"metaDescription":row["meta_description"]}
def product_payload(row):
    d=dict(row); d["variants"]=jload(d.pop("variants_json"),[]); d["score_details"]=jload(d.pop("score_details_json"),{})
    with connect() as c:
        g=c.execute("SELECT * FROM generations WHERE product_id=? ORDER BY created_at DESC LIMIT 1",(row["id"],)).fetchone()
        pv=c.execute("SELECT MAX(version) FROM prompt_versions").fetchone()[0]
    d["latest_generation"]=generation_payload(g) if g else None
    published_hash=fingerprint({k:jload(g["output_json"],{}).get(k,"") for k in ("title","descriptionHtml","metaTitle","metaDescription")}) if g else None
    d["updated"]=bool(g and g["status"]=="published" and published_hash==row["content_hash"] and g["prompt_version"]==pv)
    return d
def generation_payload(row):
    if not row:return None
    d=dict(row); d["output"]=jload(d.pop("output_json"),{}); return d

def shop_domain():
    d=os.getenv("SHOPIFY_SHOP_DOMAIN","").replace("https://","").rstrip("/")
    if not d: raise HTTPException(400,"SHOPIFY_SHOP_DOMAIN mancante")
    return d
async def shop_token(client):
    if os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN"): return os.environ["SHOPIFY_ADMIN_ACCESS_TOKEN"]
    if not os.getenv("SHOPIFY_CLIENT_ID") or not os.getenv("SHOPIFY_CLIENT_SECRET"): raise HTTPException(400,"Credenziali Shopify mancanti")
    r=await client.post(f"https://{shop_domain()}/admin/oauth/access_token",data={"grant_type":"client_credentials","client_id":os.environ["SHOPIFY_CLIENT_ID"],"client_secret":os.environ["SHOPIFY_CLIENT_SECRET"]});r.raise_for_status();return r.json()["access_token"]
async def graphql(query,variables):
    async with httpx.AsyncClient(timeout=90) as client:
        token=await shop_token(client); version=os.getenv("SHOPIFY_API_VERSION","2026-10")
        r=await client.post(f"https://{shop_domain()}/admin/api/{version}/graphql.json",headers={"X-Shopify-Access-Token":token},json={"query":query,"variables":variables});r.raise_for_status(); body=r.json()
    if body.get("errors"): raise RuntimeError(" | ".join(e["message"] for e in body["errors"]))
    return body["data"]

PRODUCT_FRAGMENT="""id title descriptionHtml status productType vendor handle updatedAt seo { title description } featuredMedia { preview { image { url } } } variants(first: 50) { nodes { id sku title } }"""
async def sync_shopify():
    query=f"""query ProductsForSeo($after:String){{products(first:100,after:$after){{nodes{{{PRODUCT_FRAGMENT}}} pageInfo{{hasNextPage endCursor}}}}}}"""
    after=None; count=0
    while True:
        data=await graphql(query,{"after":after}); block=data["products"]
        with connect() as c:
            for p in block["nodes"]:
                content={"title":p["title"],"descriptionHtml":p["descriptionHtml"] or "","metaTitle":p["seo"]["title"] or "","metaDescription":p["seo"]["description"] or ""}; h=fingerprint(content); old=c.execute("SELECT content_hash FROM products WHERE id=?",(p["id"],)).fetchone(); score=score_content(content,get_settings().weights)
                c.execute("""INSERT INTO products(id,title,description_html,meta_title,meta_description,image_url,status,product_type,vendor,handle,variants_json,shopify_updated_at,content_hash,last_synced_at,current_score,score_details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,description_html=excluded.description_html,meta_title=excluded.meta_title,meta_description=excluded.meta_description,image_url=excluded.image_url,status=excluded.status,product_type=excluded.product_type,vendor=excluded.vendor,handle=excluded.handle,variants_json=excluded.variants_json,shopify_updated_at=excluded.shopify_updated_at,content_hash=excluded.content_hash,last_synced_at=excluded.last_synced_at,current_score=excluded.current_score,score_details_json=excluded.score_details_json,processing_status=CASE WHEN products.content_hash<>excluded.content_hash THEN 'reevaluate' ELSE products.processing_status END""",(p["id"],content["title"],content["descriptionHtml"],content["metaTitle"],content["metaDescription"],((p.get("featuredMedia") or {}).get("preview") or {}).get("image",{}).get("url"),p["status"],p["productType"] or "",p["vendor"] or "",p["handle"],jdump(p["variants"]["nodes"]),p["updatedAt"],h,now(),score["score"],jdump(score)))
                c.execute("INSERT INTO shopify_snapshots VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),p["id"],jdump(content),h,now(),"sync")); count+=1
        if not block["pageInfo"]["hasNextPage"]:break
        after=block["pageInfo"]["endCursor"]
    return count

def schema_for(gid):
    fields={"voto":{"type":"integer","minimum":0,"maximum":100},"title":{"type":"string"},"descriptionHtml":{"type":"string"},"metaTitle":{"type":"string"},"metaDescription":{"type":"string"},"keywords":{"type":"array","items":{"type":"string"}},"criticita":{"type":"array","items":{"type":"string"}},"modificheSuggerite":{"type":"array","items":{"type":"string"}}}
    return {"type":"object","properties":{gid:{"type":"object","properties":fields,"required":list(fields),"additionalProperties":False}},"required":[gid],"additionalProperties":False}

async def ollama_models(s: Settings) -> list[str]:
    async with httpx.AsyncClient(timeout=min(s.timeout,15)) as client:
        response=await client.get(f"{str(s.ollama_url).rstrip('/')}/api/tags")
        response.raise_for_status()
    return [item.get("name") or item.get("model") for item in response.json().get("models",[]) if item.get("name") or item.get("model")]

async def validate_ollama_configuration(s: Settings) -> list[str]:
    try: models=await ollama_models(s)
    except httpx.HTTPError as exc: raise OllamaConfigurationError(f"Ollama non raggiungibile su {s.ollama_url}: {exc}") from exc
    if not models:
        raise OllamaConfigurationError("Ollama è raggiungibile ma non ha modelli installati. Installa un modello con 'ollama pull NOME_MODELLO', poi selezionalo nelle impostazioni.")
    if s.model not in models:
        raise OllamaConfigurationError(f"Il modello configurato '{s.model}' non è installato. Modelli disponibili: {', '.join(models)}")
    return models

async def call_ollama(row,s):
    gid=row["id"]; source={**content_of(row),"productType":row["product_type"],"vendor":row["vendor"],"variants":jload(row["variants_json"],[])}
    prompt=f"{s.seo_prompt}\nLingua: {s.language}. Stile: {s.description_style}. Negozio: {s.shop_name}. Keyword prioritarie: {s.priority_keywords}.\nProdotto, unica fonte ammessa:\n{jdump({gid:source})}"
    payload={"model":s.model,"stream":False,"format":schema_for(gid),"messages":[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":prompt}],"options":{"temperature":s.temperature,"num_predict":s.num_predict}}
    await validate_ollama_configuration(s)
    async with httpx.AsyncClient(timeout=s.timeout) as client:
        r=await client.post(f"{str(s.ollama_url).rstrip('/')}/api/chat",json=payload)
        if r.status_code>=400:
            try: detail=r.json().get("error") or r.text
            except (ValueError,AttributeError): detail=r.text
            if r.status_code==404: raise OllamaConfigurationError(f"Ollama ha rifiutato il modello '{s.model}': {detail or 'endpoint /api/chat non disponibile'}")
            r.raise_for_status()
        body=r.json()
    raw=body.get("message",{}).get("content","")
    try: parsed=json.loads(raw); proposal=Proposal.model_validate(parsed[gid])
    except (json.JSONDecodeError,KeyError,ValidationError) as exc: raise RuntimeError(f"Risposta Ollama non valida: {exc}") from exc
    return proposal.model_dump()

async def process_job(job):
    start=time.monotonic(); s=get_settings()
    with connect() as c: row=c.execute("SELECT * FROM products WHERE id=?",(job["product_id"],)).fetchone()
    if not row: raise RuntimeError("Prodotto non trovato")
    if job["kind"]=="analyze":
        result=score_content(content_of(row),s.weights)
        with connect() as c:
            c.execute("INSERT INTO analyses VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),row["id"],result["score"],jdump(result),"deterministic",now()));c.execute("UPDATE products SET current_score=?,score_details_json=?,processing_status='completed',last_error=NULL WHERE id=?",(result["score"],jdump(result),row["id"]))
    else:
        proposal=await call_ollama(row,s); ai_factor=proposal["voto"]/100; final=score_content(proposal,s.weights,{"keywords":ai_factor,"accuracy":ai_factor}); proposal["voto"]=final["score"]; proposal["criticita"]=list(dict.fromkeys(proposal["criticita"]+final["issues"])); gen_id=str(uuid.uuid4())
        with connect() as c:
            c.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,score,status,source_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(gen_id,row["id"],ensure_prompt_version(),s.model,jdump(proposal),final["score"],"generated",row["content_hash"],now()));c.execute("UPDATE products SET processing_status='completed',last_error=NULL WHERE id=?",(row["id"],))
    return time.monotonic()-start

worker_task=None
async def worker():
    while True:
        try:
            with connect() as c:
                paused=c.execute("SELECT paused FROM queue_state WHERE id=1").fetchone()[0]
                job=None if paused else c.execute("SELECT * FROM jobs WHERE status='queued' AND COALESCE(next_attempt_at,0)<=? ORDER BY created_at LIMIT 1",(time.time(),)).fetchone()
                if job:c.execute("UPDATE jobs SET status='processing',started_at=?,attempts=attempts+1 WHERE id=?",(now(),job["id"]));c.execute("UPDATE products SET processing_status='processing' WHERE id=?",(job["product_id"],))
            if not job: await asyncio.sleep(.5);continue
            try:
                duration=await process_job(job)
                with connect() as c:c.execute("UPDATE jobs SET status='completed',finished_at=?,duration=?,error=NULL WHERE id=?",(now(),duration,job["id"]))
                await asyncio.sleep(get_settings().pause_seconds)
            except Exception as exc:
                logger.exception("Job %s failed",job["id"]); attempts=job["attempts"]+1; max_attempts=get_settings().max_retries+1
                with connect() as c:
                    if attempts<max_attempts and not isinstance(exc,OllamaConfigurationError):c.execute("UPDATE jobs SET status='queued',error=?,next_attempt_at=? WHERE id=?",(str(exc),time.time()+min(60,2**attempts),job["id"]));c.execute("UPDATE products SET processing_status='queued',last_error=? WHERE id=?",(str(exc),job["product_id"]))
                    else:c.execute("UPDATE jobs SET status='error',finished_at=?,error=? WHERE id=?",(now(),str(exc),job["id"]));c.execute("UPDATE products SET processing_status='error',last_error=? WHERE id=?",(str(exc),job["product_id"]));c.execute("INSERT INTO error_logs VALUES(?,?,?,?,?)",(str(uuid.uuid4()),"queue",job["id"],str(exc),now()))
        except asyncio.CancelledError:raise
        except Exception:logger.exception("Worker loop failure");await asyncio.sleep(1)

@asynccontextmanager
async def lifespan(_):
    global worker_task;init_db();worker_task=asyncio.create_task(worker());yield;worker_task.cancel()
app=FastAPI(title="Shopify SEO AI Studio",lifespan=lifespan);app.mount("/static",StaticFiles(directory=BASE/"static"),name="static")
@app.get("/")
async def home():return FileResponse(BASE/"static/index.html")
@app.get("/api/products")
async def products(q:str="",statuses:list[str]=Query(default=[]),processing:list[str]=Query(default=[]),vendors:list[str]=Query(default=[]),types:list[str]=Query(default=[]),min_score:int=0,max_score:int=100,updated:str="",proposal:bool|None=None,errors:bool=False,limit:int=100,offset:int=0):
    clauses=["(title LIKE ? OR id LIKE ? OR variants_json LIKE ?)"];args=[f"%{q}%"]*3
    def many(field,values):
        if values:clauses.append(f"{field} IN ({','.join('?'*len(values))})");args.extend(values)
    many("status",statuses);many("processing_status",processing);many("vendor",vendors);many("product_type",types);clauses.append("COALESCE(current_score,0) BETWEEN ? AND ?");args.extend([min_score,max_score])
    if errors:clauses.append("last_error IS NOT NULL")
    where=" WHERE "+" AND ".join(clauses); count_args=list(args);sql="SELECT * FROM products"+where+" ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?";args.extend([min(limit,500),offset])
    with connect() as c:rows=c.execute(sql,args).fetchall();total=c.execute("SELECT COUNT(*) FROM products"+where,count_args).fetchone()[0]
    items=[product_payload(r) for r in rows]
    if updated:items=[x for x in items if x["updated"]==(updated=="yes")]
    if proposal is not None:items=[x for x in items if bool(x["latest_generation"] and x["latest_generation"]["status"]!="published")==proposal]
    return {"items":items,"total":total,"offset":offset,"limit":limit}
@app.get("/api/products/{product_id}")
async def product_detail(product_id:str):
    with connect() as c:
        row=c.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone();history=c.execute("SELECT * FROM generations WHERE product_id=? ORDER BY created_at DESC",(product_id,)).fetchall()
    if not row:raise HTTPException(404,"Prodotto non trovato")
    return {"product":product_payload(row),"current":content_of(row),"history":[generation_payload(x) for x in history]}
@app.get("/api/products/{product_id:path}")
async def product_detail_legacy_path(product_id:str):
    """Accept cached clients that still place a Shopify GID in the URL path."""
    return await product_detail(product_id)
@app.get("/api/product-detail")
async def product_detail_by_query(id:str=Query(...)):
    return await product_detail(id)
@app.get("/api/facets")
async def facets():
    with connect() as c:
        vendors=[r[0] for r in c.execute("SELECT DISTINCT vendor FROM products WHERE vendor<>'' ORDER BY vendor")]
        types=[r[0] for r in c.execute("SELECT DISTINCT product_type FROM products WHERE product_type<>'' ORDER BY product_type")]
    return {"vendors":vendors,"types":types}
@app.post("/api/sync")
async def sync():
    try:return {"synced":await sync_shopify()}
    except Exception as e:logger.exception("Sync failed");raise HTTPException(502,str(e))
@app.post("/api/jobs")
async def enqueue(payload:dict):
    kind=payload.get("kind");ids=payload.get("product_ids",[]);limit=int(payload.get("limit") or 500)
    if kind not in {"analyze","generate"}:raise HTTPException(400,"Tipo job non valido")
    if kind=="generate":
        try: await validate_ollama_configuration(get_settings())
        except OllamaConfigurationError as exc: raise HTTPException(409,str(exc)) from exc
    added=0
    with connect() as c:
        for pid in ids[:limit]:
            active=c.execute("SELECT 1 FROM jobs WHERE product_id=? AND kind=? AND status IN ('queued','processing')",(pid,kind)).fetchone()
            if active:continue
            c.execute("INSERT INTO jobs(id,product_id,kind,status,created_at) VALUES(?,?,?,?,?)",(str(uuid.uuid4()),pid,kind,"queued",now()));c.execute("UPDATE products SET processing_status='queued',last_error=NULL WHERE id=?",(pid,));added+=1
    return {"queued":added}
@app.get("/api/queue")
async def queue_status():
    with connect() as c:
        rows=c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 500").fetchall();paused=bool(c.execute("SELECT paused FROM queue_state WHERE id=1").fetchone()[0]);dur=[r[0] for r in c.execute("SELECT duration FROM jobs WHERE status='completed' AND duration IS NOT NULL ORDER BY finished_at DESC LIMIT 20")]
    counts={s:sum(r["status"]==s for r in rows) for s in ("queued","processing","completed","error","cancelled")};avg=sum(dur)/len(dur) if dur else None
    return {**counts,"paused":paused,"current":next((dict(r) for r in rows if r["status"]=="processing"),None),"average_seconds":avg,"eta_seconds":avg*counts["queued"] if avg else None}
@app.post("/api/queue/{action}")
async def queue_action(action:Literal["pause","resume","cancel"]):
    with connect() as c:
        if action in {"pause","resume"}:c.execute("UPDATE queue_state SET paused=?,updated_at=? WHERE id=1",(action=="pause",now()))
        else:c.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE status='queued'",(now(),));c.execute("UPDATE products SET processing_status='to_analyze' WHERE processing_status='queued'")
    return {"ok":True}
@app.get("/api/settings")
async def settings_get():return {**get_settings().model_dump(mode="json"),"system_prompt":SYSTEM_PROMPT,"prompt_version":ensure_prompt_version()}
@app.put("/api/settings")
async def settings_put(payload:dict):
    try:s=Settings.model_validate(payload)
    except ValidationError as e:raise HTTPException(422,e.errors())
    with connect() as c:c.execute("UPDATE settings SET value_json=?,updated_at=? WHERE id=1",(s.model_dump_json(),now()))
    return {"ok":True,"prompt_version":ensure_prompt_version()}
@app.post("/api/ollama/test")
async def ollama_test():
    s=get_settings()
    try:
        models=await ollama_models(s)
        message=("Connessione riuscita, ma non è installato alcun modello." if not models else f"Modello '{s.model}' disponibile." if s.model in models else f"Il modello '{s.model}' non è installato.")
        return {"connected":True,"models":models,"model_available":s.model in models,"message":message}
    except Exception as e:raise HTTPException(502,f"Ollama non raggiungibile: {e}")
@app.post("/api/generations/{generation_id}/approve")
async def approve(generation_id:str,payload:dict):
    fields=payload.get("fields",["title","descriptionHtml","metaTitle","metaDescription"]);valid={"title","descriptionHtml","metaTitle","metaDescription"}
    if not fields or not set(fields)<=valid:raise HTTPException(400,"Campi non validi")
    with connect() as c:
        if not c.execute("SELECT 1 FROM generations WHERE id=?",(generation_id,)).fetchone():raise HTTPException(404,"Generazione non trovata")
        for f in valid:c.execute("INSERT INTO approvals VALUES(?,?,?,?,?) ON CONFLICT(generation_id,field) DO UPDATE SET approved=excluded.approved,created_at=excluded.created_at",(str(uuid.uuid4()),generation_id,f,int(f in fields),now()))
        c.execute("UPDATE generations SET status='approved',approved_at=? WHERE id=?",(now(),generation_id))
    return {"approved":fields}
@app.post("/api/generations/{generation_id}/reject")
async def reject(generation_id:str):
    with connect() as c:c.execute("UPDATE generations SET status='rejected',rejected_at=? WHERE id=?",(now(),generation_id))
    return {"ok":True}
@app.post("/api/generations/{generation_id}/restore")
async def restore(generation_id:str):
    with connect() as c:
        g=c.execute("SELECT * FROM generations WHERE id=?",(generation_id,)).fetchone()
        if not g:raise HTTPException(404,"Generazione non trovata")
        clone=str(uuid.uuid4());c.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,score,status,source_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(clone,g["product_id"],g["prompt_version"],g["model"],g["output_json"],g["score"],"generated",g["source_hash"],now()))
    return {"generation_id":clone}
async def fresh_product(gid):
    q=f"query ProductForSeo($id:ID!){{product(id:$id){{{PRODUCT_FRAGMENT}}}}}";p=(await graphql(q,{"id":gid}))["product"]
    if not p:raise RuntimeError("Prodotto Shopify non trovato")
    return {"title":p["title"],"descriptionHtml":p["descriptionHtml"] or "","metaTitle":p["seo"]["title"] or "","metaDescription":p["seo"]["description"] or ""}
@app.post("/api/generations/{generation_id}/publish")
async def publish(generation_id:str):
    with connect() as c:g=c.execute("SELECT * FROM generations WHERE id=? AND status='approved'",(generation_id,)).fetchone();approvals=c.execute("SELECT field FROM approvals WHERE generation_id=? AND approved=1",(generation_id,)).fetchall() if g else []
    if not g:raise HTTPException(400,"La proposta deve essere approvata")
    fields=[r[0] for r in approvals];before=await fresh_product(g["product_id"]);source_snapshot=None
    with connect() as c:source_snapshot=c.execute("SELECT content_json FROM shopify_snapshots WHERE product_id=? AND content_hash=? ORDER BY created_at DESC LIMIT 1",(g["product_id"],g["source_hash"])).fetchone()
    source=jload(source_snapshot[0],{}) if source_snapshot else {};conflicts=[f for f in fields if source.get(f)!=before.get(f)]
    if conflicts:raise HTTPException(409,{"message":"Conflitto Shopify","fields":conflicts})
    out=jload(g["output_json"],{});inp={"id":g["product_id"]}
    mapping={"title":"title","descriptionHtml":"descriptionHtml","metaTitle":"seo.title","metaDescription":"seo.description"};seo={}
    for f in fields:
        if f.startswith("meta"):seo["title" if f=="metaTitle" else "description"]=out[f]
        else:inp[f]=out[f]
    if seo:inp["seo"]=seo
    mutation="""mutation PublishSeo($product:ProductUpdateInput!){productUpdate(product:$product){product{id updatedAt} userErrors{field message}}}"""
    data=await graphql(mutation,{"product":inp});errs=data["productUpdate"]["userErrors"]
    if errs:raise HTTPException(400,errs)
    after={**before,**{f:out[f] for f in fields}}
    with connect() as c:
        c.execute("INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?)",(str(uuid.uuid4()),g["product_id"],generation_id,jdump(fields),jdump(before),jdump(after),"published",None,now()));c.execute("UPDATE generations SET status='published',published_at=? WHERE id=?",(now(),generation_id));c.execute("UPDATE products SET title=?,description_html=?,meta_title=?,meta_description=?,content_hash=?,processing_status='published' WHERE id=?",(after["title"],after["descriptionHtml"],after["metaTitle"],after["metaDescription"],fingerprint(after),g["product_id"]))
    return {"published":fields}


=================================
FILE: package-lock.json
=================================

{
  "name": "shopify-seo-ai-studio-ui",
  "lockfileVersion": 3,
  "requires": true,
  "packages": {
    "": {
      "name": "shopify-seo-ai-studio-ui",
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
      "dev": true,
      "license": "MIT",
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
      "dev": true,
      "license": "MIT",
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
      "dev": true,
      "license": "MIT",
      "engines": {
        "node": ">=0.10.0"
      }
    },
    "node_modules/is-glob": {
      "version": "4.0.3",
      "dev": true,
      "license": "MIT",
      "dependencies": {
        "is-extglob": "^2.1.1"
      },
      "engines": {
        "node": ">=0.10.0"
      }
    },
    "node_modules/is-number": {
      "version": "7.0.0",
      "dev": true,
      "license": "MIT",
      "engines": {
        "node": ">=0.12.0"
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
      "dev": true,
      "license": "MIT",
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
      "dev": true,
      "license": "ISC"
    },
    "node_modules/picomatch": {
      "version": "2.3.2",
      "dev": true,
      "license": "MIT",
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
    "node_modules/tailwindcss/node_modules/jiti": {
      "version": "1.21.7",
      "resolved": "https://registry.npmjs.org/jiti/-/jiti-1.21.7.tgz",
      "integrity": "sha512-/imKNG4EbWNrVjoNC/1H5/9GFy+tqjGBHCaSsN+P2RnPqjsLmv6UD3Ej+Kj8nBWaRAwyk7kK5ZUc+OEatnTR3A==",
      "dev": true,
      "bin": {
        "jiti": "bin/jiti.js"
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
      "dev": true,
      "license": "MIT",
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
  "name": "shopify-seo-ai-studio-ui",
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

if __name__ == "__main__":
    uvicorn.run("app:app", host=os.getenv("APP_HOST", "127.0.0.1"), port=int(os.getenv("APP_PORT", "8770")), reload=False)



=================================
FILE: seo.py
=================================

from __future__ import annotations
import re
from html import unescape

DEFAULT_WEIGHTS = {"title": 20, "description": 25, "metaTitle": 20, "metaDescription": 15, "keywords": 10, "accuracy": 10}

def plain(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(value or ""))).strip()

def repeated_words(value: str) -> bool:
    words = [w.lower() for w in re.findall(r"[\wÀ-ÿ]+", plain(value)) if len(w) > 3]
    return bool(words) and max(words.count(w) for w in set(words)) > max(3, len(words) // 6)

def score_content(content: dict, weights: dict | None = None, ai: dict | None = None) -> dict:
    w = weights or DEFAULT_WEIGHTS
    if sum(w.values()) != 100:
        raise ValueError("I pesi SEO devono sommare a 100")
    title, desc = plain(content.get("title", "")), plain(content.get("descriptionHtml", ""))
    meta_title, meta_desc = plain(content.get("metaTitle", "")), plain(content.get("metaDescription", ""))
    checks = {
        "title": bool(title) * (1 if 20 <= len(title) <= 70 else .55 if title else 0),
        "description": bool(desc) * (1 if len(desc) >= 180 and "<" in (content.get("descriptionHtml") or "") else .65 if len(desc) >= 80 else .3),
        "metaTitle": bool(meta_title) * (1 if 30 <= len(meta_title) <= 60 else .55 if meta_title else 0),
        "metaDescription": bool(meta_desc) * (1 if 110 <= len(meta_desc) <= 160 else .55 if meta_desc else 0),
        "keywords": .35 if repeated_words(" ".join([title, desc, meta_title, meta_desc])) else float((ai or {}).get("keywords", .75)),
        "accuracy": float((ai or {}).get("accuracy", .75)),
    }
    score = round(sum(w[key] * max(0, min(1, checks[key])) for key in w))
    issues = []
    if not title: issues.append("Titolo assente")
    elif not 20 <= len(title) <= 70: issues.append("Lunghezza titolo non ottimale")
    if len(desc) < 180: issues.append("Descrizione poco informativa")
    if not 30 <= len(meta_title) <= 60: issues.append("Meta title assente o di lunghezza non ottimale")
    if not 110 <= len(meta_desc) <= 160: issues.append("Meta description assente o di lunghezza non ottimale")
    if repeated_words(" ".join([title, desc, meta_title, meta_desc])): issues.append("Possibile keyword stuffing")
    return {"score": max(1, min(100, score)), "issues": issues, "checks": checks}



=================================
FILE: static/app.js
=================================

const S={items:[],selected:new Set(),offset:0,limit:60,total:0,settings:null};const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
async function api(url,opt={}){const r=await fetch(url,{...opt,headers:{'Content-Type':'application/json',...(opt.headers||{})}});const body=await r.json().catch(()=>({}));if(!r.ok)throw new Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail||body));return body}
function notice(msg,error=false){const n=$('#notice');n.textContent=msg;n.classList.toggle('error',error)}function escape(s=''){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function vals(el){return[...el.selectedOptions].map(o=>o.value)}function debounce(fn,t=280){let h;return(...a)=>{clearTimeout(h);h=setTimeout(()=>fn(...a),t)}}
function scoreClass(n){return n==null?'':n<30?'low':n<70?'mid':'high'}function statusLabel(s){return({to_analyze:'Da analizzare',queued:'In coda',processing:'In elaborazione',completed:'Elaborato',published:'Pubblicato',reevaluate:'Da rivalutare',error:'Errore'})[s]||s}
function queryParams(limit=S.limit,offset=S.offset){const p=new URLSearchParams({q:$('#search').value,min_score:$('#minScore').value,max_score:$('#maxScore').value,limit,offset});vals($('#shopifyStatus')).forEach(v=>p.append('statuses',v));vals($('#processing')).forEach(v=>p.append('processing',v));vals($('#vendor')).forEach(v=>p.append('vendors',v));vals($('#productType')).forEach(v=>p.append('types',v));if($('#updated').value)p.set('updated',$('#updated').value);if($('#onlyProposal').checked)p.set('proposal','true');if($('#onlyErrors').checked)p.set('errors','true');return p}async function load(){try{const d=await api('/api/products?'+queryParams());S.items=d.items;S.total=d.total;render()}catch(e){notice(e.message,true)}}
function render(){const g=$('#grid');g.innerHTML='';$('#resultCount').textContent=`${S.items.length} visualizzati · ${S.total} locali`;$('#selectedCount').textContent=`${S.selected.size} selezionati`;$('#prev').disabled=S.offset===0;$('#next').disabled=S.offset+S.limit>=S.total;if(!S.items.length){g.innerHTML='<p>Nessun prodotto corrisponde ai filtri.</p>';return}for(const p of S.items){const sku=p.variants.find(v=>v.sku)?.sku||p.id.split('/').pop();const el=document.createElement('article');el.className='card'+(S.selected.has(p.id)?' selected':'');el.innerHTML=`<input class="card-select" type="checkbox" ${S.selected.has(p.id)?'checked':''}><div class="image">${p.image_url?`<img loading="lazy" src="${escape(p.image_url)}" alt="">`:'<span class="placeholder">◇</span>'}${p.updated?'<span class="updated">✓ Aggiornato</span>':''}</div><div class="card-body"><div class="score ${scoreClass(p.current_score)}">${p.current_score??'—'}</div><div class="badges"><span class="badge">${escape(p.status)}</span><span class="badge ${p.processing_status==='error'?'error':''}">${escape(statusLabel(p.processing_status))}</span></div><h2>${escape(p.title)}</h2><p class="meta">SKU ${escape(sku)} · ${escape(p.vendor||'Vendor —')}</p><div class="card-footer"><span class="meta">${p.latest_generation?'Proposta '+p.latest_generation.score+'/100':'Nessuna proposta'}</span><button class="link">Dettagli →</button></div></div>`;el.querySelector('input').onchange=e=>{e.stopPropagation();e.target.checked?S.selected.add(p.id):S.selected.delete(p.id);render()};el.querySelector('.link').onclick=()=>openDetail(p.id);g.append(el)}}
async function enqueue(kind){const ids=[...S.selected];if(!ids.length)return notice('Seleziona almeno un prodotto.',true);try{const r=await api('/api/jobs',{method:'POST',body:JSON.stringify({kind,product_ids:ids,limit:500})});notice(`${r.queued} prodotti aggiunti alla coda.`);await load();await queue()}catch(e){notice(e.message,true)}}
function valueBox(label,value,changed=false,check=''){return`<div class="field"><div class="field-head"><label>${label}</label>${check}</div><div class="value ${changed?'changed':''}">${value||'<em>Non presente</em>'}</div></div>`}
async function openDetail(id){try{const d=await api('/api/product-detail?id='+encodeURIComponent(id)),g=d.product.latest_generation,out=g?.output||{},cur=d.current;$('#detailTitle').textContent=d.product.title;let html='<div class="compare"><section class="column ai"><div class="field-head"><h3>Proposta AI</h3>'+(g?`<button class="btn primary approve-all" data-id="${g.id}">Approva modifiche</button>`:'')+'</div>';
    if(g){for(const [k,l] of [['title','Titolo'],['descriptionHtml','Descrizione HTML'],['metaTitle','Meta title'],['metaDescription','Meta description']])html+=valueBox(l,escape(out[k]),out[k]!==cur[k],`<label><input class="field-approve" value="${k}" type="checkbox" checked> approva</label>`);html+=`<p><strong>Score proposto: ${g.score}</strong> · Δ ${g.score-(d.product.current_score||0)}</p><p>${(out.criticita||[]).map(escape).join(' · ')}</p>`}else html+='<p>Nessuna generazione disponibile.</p>';html+='</section><section class="column"><div class="field-head"><h3>Versione Shopify</h3><button class="btn keep">Mantieni versione attuale</button></div>';for(const [k,l] of [['title','Titolo'],['descriptionHtml','Descrizione HTML'],['metaTitle','Meta title'],['metaDescription','Meta description']])html+=valueBox(l,escape(cur[k]));html+=`<p><strong>Score attuale: ${d.product.current_score??'—'}</strong></p></section></div><section class="history"><h3>Storico generazioni (${d.history.length})</h3>`;
    for(const h of d.history)html+=`<details><summary>${new Date(h.created_at).toLocaleString('it-IT')} · ${escape(h.model)} · prompt v${h.prompt_version} · ${h.score}/100 · ${escape(h.status)}</summary><p><strong>${escape(h.output.title)}</strong></p><p>${escape(h.output.metaDescription)}</p><div class="history-actions"><button class="btn restore" data-id="${h.id}">Ripristina come proposta</button>${h.status==='approved'?`<button class="btn primary publish-one" data-id="${h.id}">Pubblica</button>`:''}</div></details>`;html+='</section>';$('#detailBody').innerHTML=html;$('#detail').showModal();$('.keep').onclick=async()=>{if(g)await api(`/api/generations/${g.id}/reject`,{method:'POST'});$('#detail').close();notice('Proposta rifiutata; versione Shopify mantenuta.')};const approve=$('.approve-all');if(approve)approve.onclick=async()=>{const fields=$$('.field-approve:checked').map(x=>x.value);await api(`/api/generations/${approve.dataset.id}/approve`,{method:'POST',body:JSON.stringify({fields})});notice('Campi approvati; non sono ancora pubblicati.');openDetail(id)};$$('.restore').forEach(b=>b.onclick=async()=>{await api(`/api/generations/${b.dataset.id}/restore`,{method:'POST'});notice('Versione ripristinata come nuova proposta.');openDetail(id)});$$('.publish-one').forEach(b=>b.onclick=()=>publishGeneration(b.dataset.id,id))}catch(e){notice(e.message,true)}}
async function publishGeneration(gid,pid){if(!confirm('Pubblicare su Shopify soltanto i campi approvati? Verrà verificata prima la presenza di conflitti.'))return;try{const r=await api(`/api/generations/${gid}/publish`,{method:'POST'});notice(`Pubblicati: ${r.published.join(', ')}.`);$('#detail').close();await load()}catch(e){notice(e.message,true)}}
async function publishSelected(){const targets=S.items.filter(p=>S.selected.has(p.id)&&p.latest_generation?.status==='approved');if(!targets.length)return notice('Nessuna proposta approvata tra i prodotti visibili selezionati.',true);if(!confirm(`Pubblicare ${targets.length} prodotti approvati su Shopify?`))return;let ok=0;for(const p of targets){try{await api(`/api/generations/${p.latest_generation.id}/publish`,{method:'POST'});ok++}catch(e){notice(`${p.title}: ${e.message}`,true)}}notice(`${ok}/${targets.length} prodotti pubblicati.`);load()}
async function queue(){try{const q=await api('/api/queue');const eta=q.eta_seconds==null?'—':`${Math.ceil(q.eta_seconds/60)} min`;$('#queue').innerHTML=`<strong>Coda</strong><span>${q.processing} in corso · ${q.queued} attesa · ${q.completed} completati · ${q.error} errori · ETA ${eta}</span>${q.current?`<span class="spinner"></span>`:''}<span style="margin-left:auto"></span><button class="btn qpause">${q.paused?'Riprendi':'Pausa'}</button><button class="btn qcancel">Annulla attesa</button>`;$('.qpause').onclick=async()=>{await api('/api/queue/'+(q.paused?'resume':'pause'),{method:'POST'});queue()};$('.qcancel').onclick=async()=>{if(confirm('Annullare tutti i job in attesa?')){await api('/api/queue/cancel',{method:'POST'});queue();load()}}}catch(e){notice(e.message,true)}}
async function openSettings(){const s=await api('/api/settings');S.settings=s;for(const [k,v] of Object.entries(s)){const el=$(`[name="${k}"]`);if(el&&typeof v!=='object')el.value=v}for(const [k,v] of Object.entries(s.weights))$(`[name="w_${k}"]`).value=v;$('#settings').showModal()}
$('#settingsForm').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.currentTarget),s={...S.settings};for(const k of ['ollama_url','model','language','shop_name','description_style','priority_keywords','seo_prompt'])s[k]=f.get(k);for(const k of ['timeout','max_retries','batch_size','min_score','num_predict'])s[k]=Number(f.get(k));for(const k of ['pause_seconds','temperature'])s[k]=Number(f.get(k));s.weights={};for(const k of ['title','description','metaTitle','metaDescription','keywords','accuracy'])s.weights[k]=Number(f.get('w_'+k));delete s.system_prompt;delete s.prompt_version;try{const r=await api('/api/settings',{method:'PUT',body:JSON.stringify(s)});notice(`Impostazioni salvate · prompt v${r.prompt_version}.`);$('#settings').close();load()}catch(x){notice(x.message,true)}};
$('#testOllama').onclick=async()=>{const o=$('#ollamaState');o.textContent='Connessione…';try{const r=await api('/api/ollama/test',{method:'POST'});$('#ollamaModels').innerHTML=r.models.map(m=>`<option value="${escape(m)}">`).join('');o.textContent=`${r.message} Modelli: ${r.models.join(', ')||'nessuno'}`;o.classList.toggle('error',!r.model_available)}catch(e){o.textContent=e.message;o.classList.add('error')}};
async function loadFacets(){const f=await api('/api/facets');for(const [id,key] of [['vendor','vendors'],['productType','types']])$('#'+id).innerHTML=f[key].map(x=>`<option>${escape(x)}</option>`).join('')}
$('#syncBtn').onclick=async e=>{e.currentTarget.disabled=true;notice('Sincronizzazione Shopify…');try{const r=await api('/api/sync',{method:'POST'});notice(`${r.synced} prodotti sincronizzati.`);S.offset=0;await loadFacets();await load()}catch(x){notice(x.message,true)}finally{e.currentTarget.disabled=false}};$('#analyzeBtn').onclick=()=>enqueue('analyze');$('#generateBtn').onclick=()=>enqueue('generate');$('#publishBtn').onclick=publishSelected;$('#settingsBtn').onclick=openSettings;$('#selectVisible').onclick=async()=>{const d=await api('/api/products?'+queryParams(500,0));for(const p of d.items)S.selected.add(p.id);render();if(d.total>500)notice('Selezionati i primi 500 prodotti filtrati; riduci i filtri per operazioni più piccole.')};$('#prev').onclick=()=>{S.offset=Math.max(0,S.offset-S.limit);load()};$('#next').onclick=()=>{S.offset+=S.limit;load()};$$('dialog .close').forEach(b=>b.onclick=()=>b.closest('dialog').close());['search','minScore','maxScore','shopifyStatus','processing','vendor','productType','updated','onlyProposal','onlyErrors'].forEach(id=>$('#'+id).addEventListener('input',debounce(()=>{S.offset=0;load()})));loadFacets();load();queue();setInterval(()=>{queue();if(S.items.some(x=>['queued','processing'].includes(x.processing_status)))load()},2500);


=================================
FILE: static/index.html
=================================

<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#172554">
  <title>Shopify SEO AI Studio</title>
  <link rel="stylesheet" href="/static/styles.css?v=20261010-4">
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <div>
        <p class="eyebrow">Shopify / AI Operations</p>
        <h1 class="text-3xl font-black tracking-tight sm:text-4xl">SEO AI Studio</h1>
        <p class="subtitle">Analizza, migliora e pubblica i contenuti SEO del catalogo con Ollama locale.</p>
      </div>
      <div class="head-actions">
        <button class="icon" id="settingsBtn" title="Impostazioni" aria-label="Apri impostazioni">⚙</button>
        <button class="btn primary" id="syncBtn">Sincronizza Shopify</button>
      </div>
    </div>
  </header>

  <main class="app-shell">
    <section id="notice" class="notice" role="status">Catalogo locale pronto.</section>
    <section id="queue" class="queuebar" aria-label="Stato coda"></section>

    <section class="toolbar">
      <div class="action-row">
        <button class="btn" id="analyzeBtn">Analizza SEO</button>
        <button class="btn accent" id="generateBtn">Genera miglioramenti</button>
        <button class="btn primary" id="publishBtn">Pubblica approvate</button>
        <span id="selectedCount">0 selezionati</span>
      </div>
      <div class="filters">
        <input id="search" type="search" placeholder="Cerca per titolo, SKU o ID Shopify" aria-label="Cerca prodotti">
        <select id="shopifyStatus" multiple title="Stato Shopify" aria-label="Filtra stato Shopify">
          <option>ACTIVE</option><option>DRAFT</option><option>ARCHIVED</option>
        </select>
        <select id="processing" multiple title="Stato elaborazione" aria-label="Filtra stato elaborazione">
          <option value="to_analyze">Da analizzare</option><option value="queued">In coda</option><option value="processing">In elaborazione</option><option value="completed">Elaborato</option><option value="published">Pubblicato</option><option value="reevaluate">Da rivalutare</option><option value="error">Errore</option>
        </select>
        <select id="vendor" multiple title="Vendor" aria-label="Filtra vendor"></select>
        <select id="productType" multiple title="Product type" aria-label="Filtra tipologia prodotto"></select>
        <select id="updated" aria-label="Filtra stato aggiornamento">
          <option value="">Aggiornamento: tutti</option><option value="yes">Solo aggiornati</option><option value="no">Solo non aggiornati</option>
        </select>
        <label>Score <input id="minScore" type="number" min="0" max="100" value="0" aria-label="Score minimo"><span>–</span><input id="maxScore" type="number" min="1" max="100" value="100" aria-label="Score massimo"></label>
        <label><input id="onlyProposal" type="checkbox"> Proposte aperte</label>
        <label><input id="onlyErrors" type="checkbox"> Solo errori</label>
        <button class="btn subtle" id="selectVisible">Seleziona filtrati</button>
      </div>
    </section>

    <div class="result-head">
      <strong id="resultCount"></strong>
      <div class="flex gap-2"><button class="page" id="prev" aria-label="Pagina precedente">←</button><button class="page" id="next" aria-label="Pagina successiva">→</button></div>
    </div>
    <section id="grid" class="grid" aria-live="polite"></section>
  </main>

  <dialog id="detail">
    <div class="modal-head">
      <div><p class="eyebrow">Confronto prodotto</p><h2 id="detailTitle"></h2></div>
      <button class="icon close" aria-label="Chiudi dettaglio">×</button>
    </div>
    <div id="detailBody"></div>
  </dialog>

  <dialog id="settings">
    <div class="modal-head">
      <div><p class="eyebrow">Configurazione</p><h2>Impostazioni SEO e Ollama</h2></div>
      <button class="icon close" aria-label="Chiudi impostazioni">×</button>
    </div>
    <form id="settingsForm" class="settings-grid">
      <label>Endpoint Ollama<input name="ollama_url" placeholder="http://192.168.1.100:11434"></label>
      <label>Modello<div class="form-inline"><input name="model" list="ollamaModels" placeholder="Seleziona un modello"><datalist id="ollamaModels"></datalist><button type="button" class="btn" id="testOllama">Test</button></div></label>
      <label>Timeout secondi<input name="timeout" type="number"></label>
      <label>Pausa tra job<input name="pause_seconds" type="number" step=".5"></label>
      <label>Retry massimi<input name="max_retries" type="number"></label>
      <label>Dimensione batch<input name="batch_size" type="number"></label>
      <label>Soglia SEO minima<input name="min_score" type="number"></label>
      <label>Token massimi<input name="num_predict" type="number"></label>
      <label>Temperatura<input name="temperature" type="number" step=".1"></label>
      <label>Lingua<input name="language"></label>
      <label>Nome negozio<input name="shop_name"></label>
      <label>Stile descrizioni<input name="description_style"></label>
      <label class="wide">Keyword prioritarie<input name="priority_keywords"></label>
      <label class="wide">Prompt di sistema · protetto<textarea name="system_prompt" readonly></textarea></label>
      <label class="wide">Prompt SEO personalizzabile<textarea name="seo_prompt"></textarea></label>
      <fieldset class="wide weights">
        <legend>Pesi SEO · totale 100</legend>
        <label>Titolo<input name="w_title" type="number"></label>
        <label>Descrizione<input name="w_description" type="number"></label>
        <label>Meta title<input name="w_metaTitle" type="number"></label>
        <label>Meta description<input name="w_metaDescription" type="number"></label>
        <label>Keyword<input name="w_keywords" type="number"></label>
        <label>Accuratezza<input name="w_accuracy" type="number"></label>
      </fieldset>
      <div class="wide form-actions"><span id="ollamaState"></span><button class="btn primary">Salva impostazioni</button></div>
    </form>
  </dialog>

  <script src="/static/app.js?v=20261010-1"></script>
</body>
</html>


=================================
FILE: static/input.css
=================================

@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  * { @apply border-slate-200; }
  html { @apply scroll-smooth bg-slate-50; }
  body { @apply min-h-screen bg-slate-50 font-sans text-slate-900 antialiased; }
  button, input, select, textarea { @apply outline-none; }
  button { @apply cursor-pointer; }
  button:disabled { @apply cursor-not-allowed opacity-45; }
  input:not([type="checkbox"]), select, textarea { @apply w-full rounded-xl border border-slate-300 bg-white px-3.5 py-2.5 text-sm text-slate-900 shadow-sm transition placeholder:text-slate-400 hover:border-blue-300 focus:border-blue-500 focus:ring-4 focus:ring-blue-100; }
  input[type="number"] { @apply tabular-nums; }
  input[type="checkbox"] { @apply size-4 cursor-pointer appearance-none rounded border border-slate-300 bg-white align-middle shadow-sm transition checked:border-blue-600 checked:bg-blue-600 focus:ring-4 focus:ring-blue-100; background-position:center; background-repeat:no-repeat; }
  input[type="checkbox"]:checked { background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 16 16' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='m4 8 2.5 2.5L12 5' stroke='white' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E"); }
  select[multiple] { @apply min-h-24 py-2; }
  select[multiple] option { @apply rounded-md px-2 py-1.5; }
  textarea { @apply min-h-32 resize-y leading-6; }
  textarea[readonly] { @apply cursor-default bg-slate-100 text-slate-500; }
  dialog { @apply m-auto max-h-[92vh] w-[min(1180px,calc(100vw-2rem))] overflow-auto rounded-3xl border border-slate-200 bg-white p-0 shadow-2xl; }
  dialog::backdrop { background:rgb(15 23 42 / .68); backdrop-filter:blur(5px); }
}

@layer components {
  .app-shell { @apply mx-auto w-full max-w-[1600px] px-4 py-6 sm:px-6 lg:px-10 lg:py-8; }
  .topbar { @apply relative overflow-hidden border-b border-blue-800 bg-gradient-to-br from-slate-950 via-blue-950 to-blue-800 text-white shadow-xl; }
  .topbar::after { content:""; @apply pointer-events-none absolute -right-24 -top-32 size-96 rounded-full bg-sky-400/15 blur-3xl; }
  .topbar-inner { @apply relative z-10 mx-auto flex max-w-[1600px] flex-col gap-6 px-4 py-7 sm:px-6 md:flex-row md:items-center md:justify-between lg:px-10 lg:py-9; }
  .eyebrow { @apply mb-2 text-[11px] font-extrabold uppercase tracking-[.2em] text-sky-300; }
  .subtitle { @apply mt-2 max-w-2xl text-sm text-blue-100/75 sm:text-base; }
  .head-actions, .action-row, .form-inline, .result-head, .modal-head, .form-actions { @apply flex items-center gap-2.5; }
  .btn { @apply inline-flex min-h-10 items-center justify-center gap-2 rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-bold text-slate-700 shadow-sm transition duration-200 hover:-translate-y-0.5 hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700 hover:shadow-md active:translate-y-0 focus-visible:ring-4 focus-visible:ring-blue-200; }
  .btn.primary { @apply border-blue-600 bg-blue-600 text-white shadow-blue-900/15 hover:border-blue-700 hover:bg-blue-700 hover:text-white; }
  .btn.accent { @apply border-sky-400 bg-sky-400 text-blue-950 shadow-sky-900/10 hover:border-sky-300 hover:bg-sky-300 hover:text-blue-950; }
  .btn.subtle { @apply border-blue-100 bg-blue-50 text-blue-700 hover:bg-blue-100; }
  .icon, .page { @apply inline-grid size-10 place-items-center rounded-xl border border-slate-300 bg-white text-lg font-bold text-slate-600 shadow-sm transition hover:-translate-y-0.5 hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700 focus-visible:ring-4 focus-visible:ring-blue-200; }
  .topbar .icon { @apply border-white/15 bg-white/10 text-white backdrop-blur hover:border-white/30 hover:bg-white/20 hover:text-white; }
  .notice { @apply mb-4 flex min-h-12 items-center rounded-2xl border border-blue-200 bg-blue-50 px-4 py-3 text-sm font-medium text-blue-800 shadow-sm; }
  .notice::before { content:""; @apply mr-3 size-2 shrink-0 rounded-full bg-blue-500 ring-4 ring-blue-100; }
  .notice.error { @apply border-red-200 bg-red-50 text-red-700; }
  .notice.error::before { @apply bg-red-500 ring-red-100; }
  .queuebar { @apply mb-5 flex min-h-14 flex-wrap items-center gap-3 rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm; }
  .queuebar strong { @apply text-slate-950; }
  .toolbar { @apply overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm; }
  .action-row { @apply flex-wrap border-b border-slate-200 bg-gradient-to-r from-slate-50 to-blue-50/60 p-4; }
  .action-row > span { @apply ml-auto rounded-full bg-white px-3 py-1.5 text-xs font-bold text-slate-500 shadow-sm ring-1 ring-slate-200; }
  .filters { @apply grid grid-cols-1 items-start gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6; }
  .filters > * { @apply min-w-0; }
  .filters > input:first-child { @apply self-start sm:col-span-2; }
  .filters > label { @apply flex min-h-11 items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 text-xs font-bold text-slate-600 transition hover:border-blue-200 hover:bg-blue-50/50; }
  .filters > label input[type="number"] { @apply w-12 min-w-0 border-0 bg-transparent px-0 py-0 text-center shadow-none focus:ring-0; }
  .filters > label input[type="checkbox"] { @apply shrink-0; }
  .result-head { @apply my-5 justify-between text-sm text-slate-500; }
  .result-head strong { @apply text-slate-800; }
  .grid { display:grid; @apply grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5; }
  .card { @apply relative overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm transition duration-200 hover:-translate-y-1 hover:border-blue-200 hover:shadow-xl hover:shadow-blue-900/10; }
  .card.selected { @apply border-blue-500 ring-4 ring-blue-100; }
  .card-select { @apply absolute left-3 top-3 z-20 !size-5; }
  .image { @apply relative grid aspect-[5/4] place-items-center overflow-hidden bg-white; }
  .image::after { content:""; @apply pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-slate-950/10 to-transparent; }
  .image img { @apply absolute inset-0 size-full object-contain object-center transition duration-300; }
  .card:hover .image img { @apply scale-[1.04]; }
  .placeholder { @apply relative z-10 text-5xl font-light text-blue-200; }
  .updated { @apply absolute right-3 top-3 z-10 rounded-full bg-blue-600 px-2.5 py-1 text-[10px] font-extrabold uppercase tracking-wide text-white shadow-lg shadow-blue-900/20; }
  .card-body { @apply relative p-4 pt-10; }
  .card h2 { @apply my-2 line-clamp-2 min-h-10 text-[15px] font-extrabold leading-5 text-slate-900; }
  .meta { @apply truncate text-xs text-slate-500; }
  .badges { @apply flex flex-wrap justify-center gap-1.5; }
  .badge { @apply rounded-full bg-slate-100 px-2 py-1 text-[9px] font-extrabold uppercase tracking-wide text-slate-600 ring-1 ring-inset ring-slate-200; }
  .badge.error { @apply bg-red-50 text-red-700 ring-red-200; }
  .score { display:flex; @apply absolute -top-8 left-1/2 size-16 -translate-x-1/2 items-center justify-center rounded-full border-[5px] border-slate-300 bg-white text-center text-lg font-black tabular-nums text-slate-700 shadow-lg shadow-slate-900/10; }
  .score.low { @apply border-red-500 text-red-700; }
  .score.mid { @apply border-amber-400 text-amber-700; }
  .score.high { @apply border-blue-500 text-blue-700; }
  .card-footer { @apply mt-4 flex items-center justify-between gap-2 border-t border-slate-100 pt-3; }
  .link { @apply shrink-0 rounded-lg px-2 py-1 text-xs font-extrabold text-blue-600 transition hover:bg-blue-50 hover:text-blue-800; }
  .modal-head { @apply sticky top-0 z-30 justify-between border-b border-slate-200 bg-white/95 px-5 py-4 backdrop-blur sm:px-7; }
  .modal-head h2 { @apply max-w-3xl text-xl font-black tracking-tight text-slate-950 sm:text-2xl; }
  .modal-head .eyebrow { @apply mb-1 text-blue-600; }
  .compare { @apply grid grid-cols-1 gap-5 p-5 lg:grid-cols-2 lg:p-7; }
  .column { @apply rounded-2xl border border-slate-200 bg-white p-4 sm:p-5; }
  .column.ai { @apply border-blue-200 bg-gradient-to-br from-blue-50/80 to-white; }
  .column h3, .history h3 { @apply text-base font-black text-slate-950; }
  .field { @apply my-4; }
  .field-head { @apply flex items-center justify-between gap-3; }
  .field-head > label { @apply text-[10px] font-extrabold uppercase tracking-wider text-slate-500; }
  .field-head > label:has(input) { @apply flex items-center gap-2 rounded-full bg-white px-2.5 py-1 text-[10px] text-blue-700 ring-1 ring-blue-200; }
  .value { @apply mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap rounded-xl border border-slate-200 bg-white p-3 text-sm leading-6 text-slate-700 shadow-sm; }
  .value.changed { @apply border-l-4 border-l-blue-500 bg-blue-50/40; }
  .history { @apply mx-5 mb-6 rounded-2xl border border-slate-200 bg-slate-50/70 p-4 lg:mx-7 lg:mb-7; }
  .history details { @apply border-t border-slate-200 py-3 first:border-0; }
  .history summary { @apply cursor-pointer rounded-lg px-2 py-2 text-sm font-bold text-slate-700 transition hover:bg-white hover:text-blue-700; }
  .history details p { @apply px-2 text-sm leading-6 text-slate-600; }
  .history-actions { @apply mt-3 flex flex-wrap gap-2 px-2; }
  .settings-grid { @apply grid grid-cols-1 gap-4 p-5 sm:grid-cols-2 lg:p-7; }
  .settings-grid > label { @apply grid content-start gap-1.5 text-xs font-extrabold text-slate-600; }
  .settings-grid .wide { @apply sm:col-span-2; }
  .weights { @apply grid grid-cols-2 gap-3 rounded-2xl border border-blue-200 bg-blue-50/60 p-4 sm:grid-cols-3; }
  .weights legend { @apply px-2 text-xs font-black uppercase tracking-wide text-blue-800; }
  .weights label { @apply grid gap-1 text-xs font-bold text-slate-600; }
  .form-actions { @apply flex-col justify-between rounded-2xl border border-slate-200 bg-slate-50 p-4 sm:flex-row; }
  #ollamaState.error { @apply text-red-600; }
  .spinner { @apply inline-block size-4 animate-spin rounded-full border-2 border-blue-200 border-t-blue-600; }
}

@media (max-width:640px) {
  .head-actions { @apply w-full; }
  .head-actions .btn, .action-row .btn { @apply flex-1; }
  .action-row > span { @apply order-first mb-1 ml-0 w-full text-center; }
}


=================================
FILE: static/styles.css
=================================

*,:after,:before{--tw-border-spacing-x:0;--tw-border-spacing-y:0;--tw-translate-x:0;--tw-translate-y:0;--tw-rotate:0;--tw-skew-x:0;--tw-skew-y:0;--tw-scale-x:1;--tw-scale-y:1;--tw-pan-x: ;--tw-pan-y: ;--tw-pinch-zoom: ;--tw-scroll-snap-strictness:proximity;--tw-gradient-from-position: ;--tw-gradient-via-position: ;--tw-gradient-to-position: ;--tw-ordinal: ;--tw-slashed-zero: ;--tw-numeric-figure: ;--tw-numeric-spacing: ;--tw-numeric-fraction: ;--tw-ring-inset: ;--tw-ring-offset-width:0px;--tw-ring-offset-color:#fff;--tw-ring-color:rgba(59,130,246,.5);--tw-ring-offset-shadow:0 0 #0000;--tw-ring-shadow:0 0 #0000;--tw-shadow:0 0 #0000;--tw-shadow-colored:0 0 #0000;--tw-blur: ;--tw-brightness: ;--tw-contrast: ;--tw-grayscale: ;--tw-hue-rotate: ;--tw-invert: ;--tw-saturate: ;--tw-sepia: ;--tw-drop-shadow: ;--tw-backdrop-blur: ;--tw-backdrop-brightness: ;--tw-backdrop-contrast: ;--tw-backdrop-grayscale: ;--tw-backdrop-hue-rotate: ;--tw-backdrop-invert: ;--tw-backdrop-opacity: ;--tw-backdrop-saturate: ;--tw-backdrop-sepia: ;--tw-contain-size: ;--tw-contain-layout: ;--tw-contain-paint: ;--tw-contain-style: }::backdrop{--tw-border-spacing-x:0;--tw-border-spacing-y:0;--tw-translate-x:0;--tw-translate-y:0;--tw-rotate:0;--tw-skew-x:0;--tw-skew-y:0;--tw-scale-x:1;--tw-scale-y:1;--tw-pan-x: ;--tw-pan-y: ;--tw-pinch-zoom: ;--tw-scroll-snap-strictness:proximity;--tw-gradient-from-position: ;--tw-gradient-via-position: ;--tw-gradient-to-position: ;--tw-ordinal: ;--tw-slashed-zero: ;--tw-numeric-figure: ;--tw-numeric-spacing: ;--tw-numeric-fraction: ;--tw-ring-inset: ;--tw-ring-offset-width:0px;--tw-ring-offset-color:#fff;--tw-ring-color:rgba(59,130,246,.5);--tw-ring-offset-shadow:0 0 #0000;--tw-ring-shadow:0 0 #0000;--tw-shadow:0 0 #0000;--tw-shadow-colored:0 0 #0000;--tw-blur: ;--tw-brightness: ;--tw-contrast: ;--tw-grayscale: ;--tw-hue-rotate: ;--tw-invert: ;--tw-saturate: ;--tw-sepia: ;--tw-drop-shadow: ;--tw-backdrop-blur: ;--tw-backdrop-brightness: ;--tw-backdrop-contrast: ;--tw-backdrop-grayscale: ;--tw-backdrop-hue-rotate: ;--tw-backdrop-invert: ;--tw-backdrop-opacity: ;--tw-backdrop-saturate: ;--tw-backdrop-sepia: ;--tw-contain-size: ;--tw-contain-layout: ;--tw-contain-paint: ;--tw-contain-style: }/*! tailwindcss v3.4.17 | MIT License | https://tailwindcss.com*/*,:after,:before{box-sizing:border-box;border:0 solid #e5e7eb}:after,:before{--tw-content:""}:host,html{line-height:1.5;-webkit-text-size-adjust:100%;-moz-tab-size:4;-o-tab-size:4;tab-size:4;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;font-feature-settings:normal;font-variation-settings:normal;-webkit-tap-highlight-color:transparent}body{margin:0;line-height:inherit}hr{height:0;color:inherit;border-top-width:1px}abbr:where([title]){-webkit-text-decoration:underline dotted;text-decoration:underline dotted}h1,h2,h3,h4,h5,h6{font-size:inherit;font-weight:inherit}a{color:inherit;text-decoration:inherit}b,strong{font-weight:bolder}code,kbd,pre,samp{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,Liberation Mono,Courier New,monospace;font-feature-settings:normal;font-variation-settings:normal;font-size:1em}small{font-size:80%}sub,sup{font-size:75%;line-height:0;position:relative;vertical-align:baseline}sub{bottom:-.25em}sup{top:-.5em}table{text-indent:0;border-color:inherit;border-collapse:collapse}button,input,optgroup,select,textarea{font-family:inherit;font-feature-settings:inherit;font-variation-settings:inherit;font-size:100%;font-weight:inherit;line-height:inherit;letter-spacing:inherit;color:inherit;margin:0;padding:0}button,select{text-transform:none}button,input:where([type=button]),input:where([type=reset]),input:where([type=submit]){-webkit-appearance:button;background-color:transparent;background-image:none}:-moz-focusring{outline:auto}:-moz-ui-invalid{box-shadow:none}progress{vertical-align:baseline}::-webkit-inner-spin-button,::-webkit-outer-spin-button{height:auto}[type=search]{-webkit-appearance:textfield;outline-offset:-2px}::-webkit-search-decoration{-webkit-appearance:none}::-webkit-file-upload-button{-webkit-appearance:button;font:inherit}summary{display:list-item}blockquote,dd,dl,figure,h1,h2,h3,h4,h5,h6,hr,p,pre{margin:0}fieldset{margin:0}fieldset,legend{padding:0}menu,ol,ul{list-style:none;margin:0;padding:0}dialog{padding:0}textarea{resize:vertical}input::-moz-placeholder,textarea::-moz-placeholder{opacity:1;color:#9ca3af}input::placeholder,textarea::placeholder{opacity:1;color:#9ca3af}[role=button],button{cursor:pointer}:disabled{cursor:default}audio,canvas,embed,iframe,img,object,svg,video{display:block;vertical-align:middle}img,video{max-width:100%;height:auto}[hidden]:where(:not([hidden=until-found])){display:none}*{--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1))}html{scroll-behavior:smooth}body,html{--tw-bg-opacity:1;background-color:rgb(248 250 252/var(--tw-bg-opacity,1))}body{min-height:100vh;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;--tw-text-opacity:1;color:rgb(15 23 42/var(--tw-text-opacity,1));-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}button,input,select,textarea{outline:2px solid transparent;outline-offset:2px}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.45}input:not([type=checkbox]),select,textarea{width:100%;border-radius:.75rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:.625rem .875rem;font-size:.875rem;line-height:1.25rem;--tw-text-opacity:1;color:rgb(15 23 42/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}input:not([type=checkbox])::-moz-placeholder,select::-moz-placeholder,textarea::-moz-placeholder{--tw-text-opacity:1;color:rgb(148 163 184/var(--tw-text-opacity,1))}input:not([type=checkbox])::placeholder,select::placeholder,textarea::placeholder{--tw-text-opacity:1;color:rgb(148 163 184/var(--tw-text-opacity,1))}input:not([type=checkbox]):hover,select:hover,textarea:hover{--tw-border-opacity:1;border-color:rgb(147 197 253/var(--tw-border-opacity,1))}input:not([type=checkbox]):focus,select:focus,textarea:focus{--tw-border-opacity:1;border-color:rgb(59 130 246/var(--tw-border-opacity,1));--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(4px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(219 234 254/var(--tw-ring-opacity,1))}input[type=number]{--tw-numeric-spacing:tabular-nums;font-variant-numeric:var(--tw-ordinal) var(--tw-slashed-zero) var(--tw-numeric-figure) var(--tw-numeric-spacing) var(--tw-numeric-fraction)}input[type=checkbox]{width:1rem;height:1rem;cursor:pointer;-webkit-appearance:none;-moz-appearance:none;appearance:none;border-radius:.25rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));vertical-align:middle;--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}input[type=checkbox]:checked{--tw-border-opacity:1;border-color:rgb(37 99 235/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(37 99 235/var(--tw-bg-opacity,1))}input[type=checkbox]:focus{--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(4px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(219 234 254/var(--tw-ring-opacity,1))}input[type=checkbox]{background-position:50%;background-repeat:no-repeat}input[type=checkbox]:checked{background-image:url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 16 16'%3E%3Cpath stroke='%23fff' stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='m4 8 2.5 2.5L12 5'/%3E%3C/svg%3E")}select[multiple]{min-height:6rem;padding-top:.5rem;padding-bottom:.5rem}select[multiple] option{border-radius:.375rem;padding:.375rem .5rem}textarea{min-height:8rem;resize:vertical;line-height:1.5rem}textarea[readonly]{cursor:default;--tw-bg-opacity:1;background-color:rgb(241 245 249/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(100 116 139/var(--tw-text-opacity,1))}dialog{margin:auto;max-height:92vh;width:min(1180px,calc(100vw - 2rem));overflow:auto;border-radius:1.5rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:0;--tw-shadow:0 25px 50px -12px rgba(0,0,0,.25);--tw-shadow-colored:0 25px 50px -12px var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}dialog::backdrop{background:rgba(15,23,42,.68);-webkit-backdrop-filter:blur(5px);backdrop-filter:blur(5px)}.app-shell{margin-left:auto;margin-right:auto;width:100%;max-width:1600px;padding:1.5rem 1rem}@media (min-width:640px){.app-shell{padding-left:1.5rem;padding-right:1.5rem}}@media (min-width:1024px){.app-shell{padding:2rem 2.5rem}}.topbar{position:relative;overflow:hidden;border-bottom-width:1px;--tw-border-opacity:1;border-color:rgb(30 64 175/var(--tw-border-opacity,1));background-image:linear-gradient(to bottom right,var(--tw-gradient-stops));--tw-gradient-from:#020617 var(--tw-gradient-from-position);--tw-gradient-to:rgba(2,6,23,0) var(--tw-gradient-to-position);--tw-gradient-stops:var(--tw-gradient-from),var(--tw-gradient-to);--tw-gradient-to:rgba(23,37,84,0) var(--tw-gradient-to-position);--tw-gradient-stops:var(--tw-gradient-from),#172554 var(--tw-gradient-via-position),var(--tw-gradient-to);--tw-gradient-to:#1e40af var(--tw-gradient-to-position);--tw-text-opacity:1;color:rgb(255 255 255/var(--tw-text-opacity,1));--tw-shadow:0 20px 25px -5px rgba(0,0,0,.1),0 8px 10px -6px rgba(0,0,0,.1);--tw-shadow-colored:0 20px 25px -5px var(--tw-shadow-color),0 8px 10px -6px var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.topbar:after{content:"";pointer-events:none;position:absolute;right:-6rem;top:-8rem;width:24rem;height:24rem;border-radius:9999px;background-color:rgba(56,189,248,.15);--tw-blur:blur(64px);filter:var(--tw-blur) var(--tw-brightness) var(--tw-contrast) var(--tw-grayscale) var(--tw-hue-rotate) var(--tw-invert) var(--tw-saturate) var(--tw-sepia) var(--tw-drop-shadow)}.topbar-inner{position:relative;z-index:10;margin-left:auto;margin-right:auto;display:flex;max-width:1600px;flex-direction:column;gap:1.5rem;padding:1.75rem 1rem}@media (min-width:640px){.topbar-inner{padding-left:1.5rem;padding-right:1.5rem}}@media (min-width:768px){.topbar-inner{flex-direction:row;align-items:center;justify-content:space-between}}@media (min-width:1024px){.topbar-inner{padding:2.25rem 2.5rem}}.eyebrow{margin-bottom:.5rem;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.2em;--tw-text-opacity:1;color:rgb(125 211 252/var(--tw-text-opacity,1))}.subtitle{margin-top:.5rem;max-width:42rem;font-size:.875rem;line-height:1.25rem;color:rgba(219,234,254,.75)}@media (min-width:640px){.subtitle{font-size:1rem;line-height:1.5rem}}.action-row,.form-actions,.form-inline,.head-actions,.modal-head,.result-head{display:flex;align-items:center;gap:.625rem}.btn{display:inline-flex;min-height:2.5rem;align-items:center;justify-content:center;gap:.5rem;border-radius:.75rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:.5rem 1rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(51 65 85/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.2s}.btn,.btn:hover{box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.btn:hover{--tw-translate-y:-0.125rem;transform:translate(var(--tw-translate-x),var(--tw-translate-y)) rotate(var(--tw-rotate)) skewX(var(--tw-skew-x)) skewY(var(--tw-skew-y)) scaleX(var(--tw-scale-x)) scaleY(var(--tw-scale-y));--tw-border-opacity:1;border-color:rgb(147 197 253/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(239 246 255/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(29 78 216/var(--tw-text-opacity,1));--tw-shadow:0 4px 6px -1px rgba(0,0,0,.1),0 2px 4px -2px rgba(0,0,0,.1);--tw-shadow-colored:0 4px 6px -1px var(--tw-shadow-color),0 2px 4px -2px var(--tw-shadow-color)}.btn:focus-visible{--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(4px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(191 219 254/var(--tw-ring-opacity,1))}.btn:active{--tw-translate-y:0px;transform:translate(var(--tw-translate-x),var(--tw-translate-y)) rotate(var(--tw-rotate)) skewX(var(--tw-skew-x)) skewY(var(--tw-skew-y)) scaleX(var(--tw-scale-x)) scaleY(var(--tw-scale-y))}.btn.primary{--tw-border-opacity:1;border-color:rgb(37 99 235/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(37 99 235/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(255 255 255/var(--tw-text-opacity,1));--tw-shadow-color:rgba(30,58,138,.15);--tw-shadow:var(--tw-shadow-colored)}.btn.primary:hover{--tw-border-opacity:1;border-color:rgb(29 78 216/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(29 78 216/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(255 255 255/var(--tw-text-opacity,1))}.btn.accent{--tw-border-opacity:1;border-color:rgb(56 189 248/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(56 189 248/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(23 37 84/var(--tw-text-opacity,1));--tw-shadow-color:rgba(12,74,110,.1);--tw-shadow:var(--tw-shadow-colored)}.btn.accent:hover{border-color:rgb(125 211 252/var(--tw-border-opacity,1));background-color:rgb(125 211 252/var(--tw-bg-opacity,1));color:rgb(23 37 84/var(--tw-text-opacity,1))}.btn.accent:hover,.btn.subtle{--tw-border-opacity:1;--tw-bg-opacity:1;--tw-text-opacity:1}.btn.subtle{border-color:rgb(219 234 254/var(--tw-border-opacity,1));background-color:rgb(239 246 255/var(--tw-bg-opacity,1));color:rgb(29 78 216/var(--tw-text-opacity,1))}.btn.subtle:hover{--tw-bg-opacity:1;background-color:rgb(219 234 254/var(--tw-bg-opacity,1))}.icon,.page{display:inline-grid;width:2.5rem;height:2.5rem;place-items:center;border-radius:.75rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));font-size:1.125rem;line-height:1.75rem;font-weight:700;--tw-text-opacity:1;color:rgb(71 85 105/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.icon:hover,.page:hover{--tw-translate-y:-0.125rem;transform:translate(var(--tw-translate-x),var(--tw-translate-y)) rotate(var(--tw-rotate)) skewX(var(--tw-skew-x)) skewY(var(--tw-skew-y)) scaleX(var(--tw-scale-x)) scaleY(var(--tw-scale-y));--tw-border-opacity:1;border-color:rgb(147 197 253/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(239 246 255/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(29 78 216/var(--tw-text-opacity,1))}.icon:focus-visible,.page:focus-visible{--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(4px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(191 219 254/var(--tw-ring-opacity,1))}.topbar .icon{border-color:hsla(0,0%,100%,.15);background-color:hsla(0,0%,100%,.1);--tw-text-opacity:1;color:rgb(255 255 255/var(--tw-text-opacity,1));--tw-backdrop-blur:blur(8px);-webkit-backdrop-filter:var(--tw-backdrop-blur) var(--tw-backdrop-brightness) var(--tw-backdrop-contrast) var(--tw-backdrop-grayscale) var(--tw-backdrop-hue-rotate) var(--tw-backdrop-invert) var(--tw-backdrop-opacity) var(--tw-backdrop-saturate) var(--tw-backdrop-sepia);backdrop-filter:var(--tw-backdrop-blur) var(--tw-backdrop-brightness) var(--tw-backdrop-contrast) var(--tw-backdrop-grayscale) var(--tw-backdrop-hue-rotate) var(--tw-backdrop-invert) var(--tw-backdrop-opacity) var(--tw-backdrop-saturate) var(--tw-backdrop-sepia)}.topbar .icon:hover{border-color:hsla(0,0%,100%,.3);background-color:hsla(0,0%,100%,.2);--tw-text-opacity:1;color:rgb(255 255 255/var(--tw-text-opacity,1))}.notice{margin-bottom:1rem;display:flex;min-height:3rem;align-items:center;border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(191 219 254/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(239 246 255/var(--tw-bg-opacity,1));padding:.75rem 1rem;font-size:.875rem;line-height:1.25rem;font-weight:500;--tw-text-opacity:1;color:rgb(30 64 175/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.notice:before{content:"";margin-right:.75rem;width:.5rem;height:.5rem;flex-shrink:0;border-radius:9999px;--tw-bg-opacity:1;background-color:rgb(59 130 246/var(--tw-bg-opacity,1));--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(4px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(219 234 254/var(--tw-ring-opacity,1))}.notice.error{--tw-border-opacity:1;border-color:rgb(254 202 202/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(254 242 242/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(185 28 28/var(--tw-text-opacity,1))}.notice.error:before{--tw-bg-opacity:1;background-color:rgb(239 68 68/var(--tw-bg-opacity,1));--tw-ring-opacity:1;--tw-ring-color:rgb(254 226 226/var(--tw-ring-opacity,1))}.queuebar{margin-bottom:1.25rem;display:flex;min-height:3.5rem;flex-wrap:wrap;align-items:center;gap:.75rem;border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:.75rem 1rem;font-size:.875rem;line-height:1.25rem;--tw-text-opacity:1;color:rgb(71 85 105/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.queuebar strong{--tw-text-opacity:1;color:rgb(2 6 23/var(--tw-text-opacity,1))}.toolbar{overflow:hidden;border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.action-row{flex-wrap:wrap;border-bottom-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));background-image:linear-gradient(to right,var(--tw-gradient-stops));--tw-gradient-from:#f8fafc var(--tw-gradient-from-position);--tw-gradient-to:rgba(248,250,252,0) var(--tw-gradient-to-position);--tw-gradient-stops:var(--tw-gradient-from),var(--tw-gradient-to);--tw-gradient-to:rgba(239,246,255,.6) var(--tw-gradient-to-position);padding:1rem}.action-row>span{margin-left:auto;border-radius:9999px;--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:.375rem .75rem;font-size:.75rem;line-height:1rem;font-weight:700;--tw-text-opacity:1;color:rgb(100 116 139/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(1px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(226 232 240/var(--tw-ring-opacity,1))}.filters{display:grid;grid-template-columns:repeat(1,minmax(0,1fr));gap:1.25rem}@media (min-width:1024px){.filters{grid-template-columns:repeat(3,minmax(0,1fr))}}@media (min-width:1280px){.filters{grid-template-columns:repeat(4,minmax(0,1fr))}}@media (min-width:1536px){.filters{grid-template-columns:repeat(5,minmax(0,1fr))}}.filters{display:grid;grid-template-columns:repeat(1,minmax(0,1fr));align-items:flex-start;gap:.75rem;padding:1rem}@media (min-width:640px){.filters{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (min-width:1024px){.filters{grid-template-columns:repeat(4,minmax(0,1fr))}}@media (min-width:1280px){.filters{grid-template-columns:repeat(6,minmax(0,1fr))}}.filters>*{min-width:0}.filters>input:first-child{align-self:flex-start}@media (min-width:640px){.filters>input:first-child{grid-column:span 2/span 2}}.filters>label{display:flex;min-height:2.75rem;align-items:center;gap:.5rem;border-radius:.75rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(248 250 252/var(--tw-bg-opacity,1));padding-left:.75rem;padding-right:.75rem;font-size:.75rem;line-height:1rem;font-weight:700;--tw-text-opacity:1;color:rgb(71 85 105/var(--tw-text-opacity,1));transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.filters>label:hover{--tw-border-opacity:1;border-color:rgb(191 219 254/var(--tw-border-opacity,1));background-color:rgba(239,246,255,.5)}.filters>label input[type=number]{width:3rem;min-width:0;border-width:0;background-color:transparent;padding:0;text-align:center;--tw-shadow:0 0 #0000;--tw-shadow-colored:0 0 #0000;box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.filters>label input[type=number]:focus{--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000)}.filters>label input[type=checkbox]{flex-shrink:0}.result-head{margin-top:1.25rem;margin-bottom:1.25rem;justify-content:space-between;font-size:.875rem;line-height:1.25rem;--tw-text-opacity:1;color:rgb(100 116 139/var(--tw-text-opacity,1))}.result-head strong{--tw-text-opacity:1;color:rgb(30 41 59/var(--tw-text-opacity,1))}.grid{display:grid;grid-template-columns:repeat(1,minmax(0,1fr));gap:1.25rem}@media (min-width:640px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (min-width:1024px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}@media (min-width:1280px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}}@media (min-width:1536px){.grid{grid-template-columns:repeat(5,minmax(0,1fr))}}.card{position:relative;overflow:hidden;border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.2s}.card,.card:hover{box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.card:hover{--tw-translate-y:-0.25rem;transform:translate(var(--tw-translate-x),var(--tw-translate-y)) rotate(var(--tw-rotate)) skewX(var(--tw-skew-x)) skewY(var(--tw-skew-y)) scaleX(var(--tw-scale-x)) scaleY(var(--tw-scale-y));--tw-border-opacity:1;border-color:rgb(191 219 254/var(--tw-border-opacity,1));--tw-shadow:0 20px 25px -5px rgba(0,0,0,.1),0 8px 10px -6px rgba(0,0,0,.1);--tw-shadow-colored:0 20px 25px -5px var(--tw-shadow-color),0 8px 10px -6px var(--tw-shadow-color);--tw-shadow-color:rgba(30,58,138,.1);--tw-shadow:var(--tw-shadow-colored)}.card.selected{--tw-border-opacity:1;border-color:rgb(59 130 246/var(--tw-border-opacity,1));--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(4px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(219 234 254/var(--tw-ring-opacity,1))}.card-select{position:absolute;left:.75rem;top:.75rem;z-index:20;width:1.25rem!important;height:1.25rem!important}.image{display:grid;grid-template-columns:repeat(1,minmax(0,1fr));gap:1.25rem}@media (min-width:640px){.image{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (min-width:1024px){.image{grid-template-columns:repeat(3,minmax(0,1fr))}}@media (min-width:1280px){.image{grid-template-columns:repeat(4,minmax(0,1fr))}}@media (min-width:1536px){.image{grid-template-columns:repeat(5,minmax(0,1fr))}}.image{position:relative;display:grid;aspect-ratio:5/4;place-items:center;overflow:hidden;--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1))}.image:after{content:"";pointer-events:none;left:0;right:0;bottom:0;height:4rem;background-image:linear-gradient(to top,var(--tw-gradient-stops));--tw-gradient-from:rgba(2,6,23,.1) var(--tw-gradient-from-position);--tw-gradient-to:rgba(2,6,23,0) var(--tw-gradient-to-position);--tw-gradient-stops:var(--tw-gradient-from),var(--tw-gradient-to);--tw-gradient-to:transparent var(--tw-gradient-to-position)}.image img,.image:after{position:absolute}.image img{inset:0;width:100%;height:100%;-o-object-fit:contain;object-fit:contain;-o-object-position:center;object-position:center;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.3s}.card:hover .image img{--tw-scale-x:1.04;--tw-scale-y:1.04;transform:translate(var(--tw-translate-x),var(--tw-translate-y)) rotate(var(--tw-rotate)) skewX(var(--tw-skew-x)) skewY(var(--tw-skew-y)) scaleX(var(--tw-scale-x)) scaleY(var(--tw-scale-y))}.placeholder{position:relative;z-index:10;font-size:3rem;line-height:1;font-weight:300;--tw-text-opacity:1;color:rgb(191 219 254/var(--tw-text-opacity,1))}.updated{position:absolute;right:.75rem;top:.75rem;z-index:10;border-radius:9999px;--tw-bg-opacity:1;background-color:rgb(37 99 235/var(--tw-bg-opacity,1));padding:.25rem .625rem;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.025em;--tw-text-opacity:1;color:rgb(255 255 255/var(--tw-text-opacity,1));--tw-shadow:0 10px 15px -3px rgba(0,0,0,.1),0 4px 6px -4px rgba(0,0,0,.1);--tw-shadow-colored:0 10px 15px -3px var(--tw-shadow-color),0 4px 6px -4px var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);--tw-shadow-color:rgba(30,58,138,.2);--tw-shadow:var(--tw-shadow-colored)}.card-body{position:relative;padding:2.5rem 1rem 1rem}.card h2{margin-top:.5rem;margin-bottom:.5rem;overflow:hidden;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;min-height:2.5rem;font-size:15px;font-weight:800;line-height:1.25rem;--tw-text-opacity:1;color:rgb(15 23 42/var(--tw-text-opacity,1))}.meta{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.75rem;line-height:1rem;--tw-text-opacity:1;color:rgb(100 116 139/var(--tw-text-opacity,1))}.badges{display:flex;flex-wrap:wrap;justify-content:center;gap:.375rem}.badge{border-radius:9999px;--tw-bg-opacity:1;background-color:rgb(241 245 249/var(--tw-bg-opacity,1));padding:.25rem .5rem;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.025em;--tw-text-opacity:1;color:rgb(71 85 105/var(--tw-text-opacity,1));--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(1px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-inset:inset;--tw-ring-opacity:1;--tw-ring-color:rgb(226 232 240/var(--tw-ring-opacity,1))}.badge.error{--tw-bg-opacity:1;background-color:rgb(254 242 242/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(185 28 28/var(--tw-text-opacity,1));--tw-ring-opacity:1;--tw-ring-color:rgb(254 202 202/var(--tw-ring-opacity,1))}.score{display:flex;position:absolute;top:-2rem;left:50%;width:4rem;height:4rem;--tw-translate-x:-50%;transform:translate(var(--tw-translate-x),var(--tw-translate-y)) rotate(var(--tw-rotate)) skewX(var(--tw-skew-x)) skewY(var(--tw-skew-y)) scaleX(var(--tw-scale-x)) scaleY(var(--tw-scale-y));align-items:center;justify-content:center;border-radius:9999px;border-width:5px;--tw-border-opacity:1;border-color:rgb(203 213 225/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));text-align:center;font-size:1.125rem;line-height:1.75rem;font-weight:900;--tw-numeric-spacing:tabular-nums;font-variant-numeric:var(--tw-ordinal) var(--tw-slashed-zero) var(--tw-numeric-figure) var(--tw-numeric-spacing) var(--tw-numeric-fraction);--tw-text-opacity:1;color:rgb(51 65 85/var(--tw-text-opacity,1));--tw-shadow:0 10px 15px -3px rgba(0,0,0,.1),0 4px 6px -4px rgba(0,0,0,.1);--tw-shadow-colored:0 10px 15px -3px var(--tw-shadow-color),0 4px 6px -4px var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow);--tw-shadow-color:rgba(15,23,42,.1);--tw-shadow:var(--tw-shadow-colored)}.score.low{border-color:rgb(239 68 68/var(--tw-border-opacity,1));color:rgb(185 28 28/var(--tw-text-opacity,1))}.score.low,.score.mid{--tw-border-opacity:1;--tw-text-opacity:1}.score.mid{border-color:rgb(251 191 36/var(--tw-border-opacity,1));color:rgb(180 83 9/var(--tw-text-opacity,1))}.score.high{--tw-border-opacity:1;border-color:rgb(59 130 246/var(--tw-border-opacity,1));--tw-text-opacity:1;color:rgb(29 78 216/var(--tw-text-opacity,1))}.card-footer{margin-top:1rem;display:flex;align-items:center;justify-content:space-between;gap:.5rem;border-top-width:1px;--tw-border-opacity:1;border-color:rgb(241 245 249/var(--tw-border-opacity,1));padding-top:.75rem}.link{flex-shrink:0;border-radius:.5rem;padding:.25rem .5rem;font-size:.75rem;line-height:1rem;font-weight:800;--tw-text-opacity:1;color:rgb(37 99 235/var(--tw-text-opacity,1));transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.link:hover{--tw-bg-opacity:1;background-color:rgb(239 246 255/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(30 64 175/var(--tw-text-opacity,1))}.modal-head{position:sticky;top:0;z-index:30;justify-content:space-between;border-bottom-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));background-color:hsla(0,0%,100%,.95);padding:1rem 1.25rem;--tw-backdrop-blur:blur(8px);-webkit-backdrop-filter:var(--tw-backdrop-blur) var(--tw-backdrop-brightness) var(--tw-backdrop-contrast) var(--tw-backdrop-grayscale) var(--tw-backdrop-hue-rotate) var(--tw-backdrop-invert) var(--tw-backdrop-opacity) var(--tw-backdrop-saturate) var(--tw-backdrop-sepia);backdrop-filter:var(--tw-backdrop-blur) var(--tw-backdrop-brightness) var(--tw-backdrop-contrast) var(--tw-backdrop-grayscale) var(--tw-backdrop-hue-rotate) var(--tw-backdrop-invert) var(--tw-backdrop-opacity) var(--tw-backdrop-saturate) var(--tw-backdrop-sepia)}@media (min-width:640px){.modal-head{padding-left:1.75rem;padding-right:1.75rem}}.modal-head h2{max-width:48rem;font-size:1.25rem;line-height:1.75rem;font-weight:900;letter-spacing:-.025em;--tw-text-opacity:1;color:rgb(2 6 23/var(--tw-text-opacity,1))}@media (min-width:640px){.modal-head h2{font-size:1.5rem;line-height:2rem}}.modal-head .eyebrow{margin-bottom:.25rem;--tw-text-opacity:1;color:rgb(37 99 235/var(--tw-text-opacity,1))}.compare{display:grid;grid-template-columns:repeat(1,minmax(0,1fr));gap:1.25rem}@media (min-width:640px){.compare{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (min-width:1024px){.compare{grid-template-columns:repeat(3,minmax(0,1fr))}}@media (min-width:1280px){.compare{grid-template-columns:repeat(4,minmax(0,1fr))}}@media (min-width:1536px){.compare{grid-template-columns:repeat(5,minmax(0,1fr))}}.compare{display:grid;grid-template-columns:repeat(1,minmax(0,1fr));gap:1.25rem;padding:1.25rem}@media (min-width:1024px){.compare{grid-template-columns:repeat(2,minmax(0,1fr));padding:1.75rem}}.column{border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:1rem}@media (min-width:640px){.column{padding:1.25rem}}.column.ai{--tw-border-opacity:1;border-color:rgb(191 219 254/var(--tw-border-opacity,1));background-image:linear-gradient(to bottom right,var(--tw-gradient-stops));--tw-gradient-from:rgba(239,246,255,.8) var(--tw-gradient-from-position);--tw-gradient-to:rgba(239,246,255,0) var(--tw-gradient-to-position);--tw-gradient-stops:var(--tw-gradient-from),var(--tw-gradient-to);--tw-gradient-to:#fff var(--tw-gradient-to-position)}.column h3,.history h3{font-size:1rem;line-height:1.5rem;font-weight:900;--tw-text-opacity:1;color:rgb(2 6 23/var(--tw-text-opacity,1))}.field{margin-top:1rem;margin-bottom:1rem}.field-head{display:flex;align-items:center;justify-content:space-between;gap:.75rem}.field-head>label{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.05em;--tw-text-opacity:1;color:rgb(100 116 139/var(--tw-text-opacity,1))}.field-head>label:has(input){display:flex;align-items:center;gap:.5rem;border-radius:9999px;--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:.25rem .625rem;font-size:10px;--tw-text-opacity:1;color:rgb(29 78 216/var(--tw-text-opacity,1));--tw-ring-offset-shadow:var(--tw-ring-inset) 0 0 0 var(--tw-ring-offset-width) var(--tw-ring-offset-color);--tw-ring-shadow:var(--tw-ring-inset) 0 0 0 calc(1px + var(--tw-ring-offset-width)) var(--tw-ring-color);box-shadow:var(--tw-ring-offset-shadow),var(--tw-ring-shadow),var(--tw-shadow,0 0 #0000);--tw-ring-opacity:1;--tw-ring-color:rgb(191 219 254/var(--tw-ring-opacity,1))}.value{margin-top:.375rem;max-height:12rem;overflow:auto;white-space:pre-wrap;border-radius:.75rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));padding:.75rem;font-size:.875rem;line-height:1.5rem;--tw-text-opacity:1;color:rgb(51 65 85/var(--tw-text-opacity,1));--tw-shadow:0 1px 2px 0 rgba(0,0,0,.05);--tw-shadow-colored:0 1px 2px 0 var(--tw-shadow-color);box-shadow:var(--tw-ring-offset-shadow,0 0 #0000),var(--tw-ring-shadow,0 0 #0000),var(--tw-shadow)}.value.changed{border-left-width:4px;--tw-border-opacity:1;border-left-color:rgb(59 130 246/var(--tw-border-opacity,1));background-color:rgba(239,246,255,.4)}.history{margin-left:1.25rem;margin-right:1.25rem;margin-bottom:1.5rem;border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));background-color:rgba(248,250,252,.7);padding:1rem}@media (min-width:1024px){.history{margin-left:1.75rem;margin-right:1.75rem;margin-bottom:1.75rem}}.history details{border-top-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));padding-top:.75rem;padding-bottom:.75rem}.history details:first-child{border-width:0}.history summary{cursor:pointer;border-radius:.5rem;padding:.5rem;font-size:.875rem;line-height:1.25rem;font-weight:700;--tw-text-opacity:1;color:rgb(51 65 85/var(--tw-text-opacity,1));transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,-webkit-backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter;transition-property:color,background-color,border-color,text-decoration-color,fill,stroke,opacity,box-shadow,transform,filter,backdrop-filter,-webkit-backdrop-filter;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.history summary:hover{--tw-bg-opacity:1;background-color:rgb(255 255 255/var(--tw-bg-opacity,1));--tw-text-opacity:1;color:rgb(29 78 216/var(--tw-text-opacity,1))}.history details p{padding-left:.5rem;padding-right:.5rem;font-size:.875rem;line-height:1.5rem;--tw-text-opacity:1;color:rgb(71 85 105/var(--tw-text-opacity,1))}.history-actions{margin-top:.75rem;display:flex;flex-wrap:wrap;gap:.5rem;padding-left:.5rem;padding-right:.5rem}.settings-grid{display:grid;grid-template-columns:repeat(1,minmax(0,1fr));gap:1.25rem}@media (min-width:1024px){.settings-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}@media (min-width:1280px){.settings-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}@media (min-width:1536px){.settings-grid{grid-template-columns:repeat(5,minmax(0,1fr))}}.settings-grid{display:grid;grid-template-columns:repeat(1,minmax(0,1fr));gap:1rem;padding:1.25rem}@media (min-width:640px){.settings-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (min-width:1024px){.settings-grid{padding:1.75rem}}.settings-grid>label{display:grid;grid-template-columns:repeat(1,minmax(0,1fr));gap:1.25rem}@media (min-width:640px){.settings-grid>label{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (min-width:1024px){.settings-grid>label{grid-template-columns:repeat(3,minmax(0,1fr))}}@media (min-width:1280px){.settings-grid>label{grid-template-columns:repeat(4,minmax(0,1fr))}}@media (min-width:1536px){.settings-grid>label{grid-template-columns:repeat(5,minmax(0,1fr))}}.settings-grid>label{display:grid;align-content:flex-start;gap:.375rem;font-size:.75rem;line-height:1rem;font-weight:800;--tw-text-opacity:1;color:rgb(71 85 105/var(--tw-text-opacity,1))}@media (min-width:640px){.settings-grid .wide{grid-column:span 2/span 2}}.weights{display:grid;grid-template-columns:repeat(1,minmax(0,1fr));gap:1.25rem}@media (min-width:640px){.weights{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (min-width:1024px){.weights{grid-template-columns:repeat(3,minmax(0,1fr))}}@media (min-width:1280px){.weights{grid-template-columns:repeat(4,minmax(0,1fr))}}@media (min-width:1536px){.weights{grid-template-columns:repeat(5,minmax(0,1fr))}}.weights{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.75rem;border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(191 219 254/var(--tw-border-opacity,1));background-color:rgba(239,246,255,.6);padding:1rem}@media (min-width:640px){.weights{grid-template-columns:repeat(3,minmax(0,1fr))}}.weights legend{padding-left:.5rem;padding-right:.5rem;font-size:.75rem;line-height:1rem;font-weight:900;text-transform:uppercase;letter-spacing:.025em;--tw-text-opacity:1;color:rgb(30 64 175/var(--tw-text-opacity,1))}.weights label{display:grid;grid-template-columns:repeat(1,minmax(0,1fr));gap:1.25rem}@media (min-width:640px){.weights label{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (min-width:1024px){.weights label{grid-template-columns:repeat(3,minmax(0,1fr))}}@media (min-width:1280px){.weights label{grid-template-columns:repeat(4,minmax(0,1fr))}}@media (min-width:1536px){.weights label{grid-template-columns:repeat(5,minmax(0,1fr))}}.weights label{display:grid;gap:.25rem;font-size:.75rem;line-height:1rem;font-weight:700;--tw-text-opacity:1;color:rgb(71 85 105/var(--tw-text-opacity,1))}.form-actions{flex-direction:column;justify-content:space-between;border-radius:1rem;border-width:1px;--tw-border-opacity:1;border-color:rgb(226 232 240/var(--tw-border-opacity,1));--tw-bg-opacity:1;background-color:rgb(248 250 252/var(--tw-bg-opacity,1));padding:1rem}@media (min-width:640px){.form-actions{flex-direction:row}}#ollamaState.error{--tw-text-opacity:1;color:rgb(220 38 38/var(--tw-text-opacity,1))}.spinner{display:inline-block;width:1rem;height:1rem}@keyframes spin{to{transform:rotate(1turn)}}.spinner{animation:spin 1s linear infinite;border-radius:9999px;border-width:2px;border-color:rgb(191 219 254/var(--tw-border-opacity,1));--tw-border-opacity:1;border-top-color:rgb(37 99 235/var(--tw-border-opacity,1))}.flex{display:flex}.grid{display:grid}.gap-2{gap:.5rem}.text-3xl{font-size:1.875rem;line-height:2.25rem}.font-black{font-weight:900}.tracking-tight{letter-spacing:-.025em}.filter{filter:var(--tw-blur) var(--tw-brightness) var(--tw-contrast) var(--tw-grayscale) var(--tw-hue-rotate) var(--tw-invert) var(--tw-saturate) var(--tw-sepia) var(--tw-drop-shadow)}@media (max-width:640px){.head-actions{width:100%}.action-row .btn,.head-actions .btn{flex:1 1 0%}.action-row>span{order:-9999;margin-bottom:.25rem;margin-left:0;width:100%;text-align:center}}@media (min-width:640px){.sm\:text-4xl{font-size:2.25rem;line-height:2.5rem}}

=================================
FILE: tailwind.config.js
=================================

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./static/index.html", "./static/app.js"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "BlinkMacSystemFont", "Segoe UI", "sans-serif"]
      }
    }
  }
};


=================================
FILE: tests/test_core.py
=================================

import asyncio, json, sqlite3
from pathlib import Path
import pytest
import app
from app import Proposal
from seo import score_content
from fastapi.testclient import TestClient

def test_seo_score_is_repeatable_and_bounded():
    content={"title":"Carburatore racing per Pit Bike 125 cc","descriptionHtml":"<p>Carburatore documentato per Pit Bike. "+"Descrizione tecnica chiara e completa. "*8+"</p>","metaTitle":"Carburatore Pit Bike 125 cc | Ricambi","metaDescription":"Carburatore per Pit Bike 125 cc con descrizione tecnica chiara. Verifica misure e compatibilità indicate nella scheda prima dell'acquisto online."}
    first=score_content(content);second=score_content(content)
    assert first==second and 1<=first["score"]<=100

def test_weights_must_sum_to_100():
    with pytest.raises(ValueError): score_content({}, {"title":1,"description":1,"metaTitle":1,"metaDescription":1,"keywords":1,"accuracy":1})

def test_ollama_contract_rejects_extra_and_bad_score():
    good={"voto":80,"title":"Titolo","descriptionHtml":"<p>Testo</p>","metaTitle":"Meta","metaDescription":"Desc","keywords":[],"criticita":[],"modificheSuggerite":[]}
    assert Proposal.model_validate(good).voto==80
    with pytest.raises(Exception): Proposal.model_validate({**good,"extra":"no"})
    with pytest.raises(Exception): Proposal.model_validate({**good,"voto":101})

@pytest.mark.asyncio
async def test_queue_is_strictly_sequential(monkeypatch,tmp_path):
    monkeypatch.setattr(app,"DB",tmp_path/"queue.sqlite");app.init_db();active=0;peak=0
    async def fake(job):
        nonlocal active,peak;active+=1;peak=max(peak,active);await asyncio.sleep(.02);active-=1;return .02
    monkeypatch.setattr(app,"process_job",fake)
    class S: pause_seconds=0;max_retries=0
    monkeypatch.setattr(app,"get_settings",lambda:S())
    with app.connect() as c:
        for i in range(2):
            pid=f"gid://shopify/Product/{i}";c.execute("INSERT INTO products(id,title,status,content_hash,last_synced_at) VALUES(?,?,?,?,?)",(pid,"P","ACTIVE","x",app.now()));c.execute("INSERT INTO jobs(id,product_id,kind,status,created_at) VALUES(?,?,?,?,?)",(str(i),pid,"analyze","queued",app.now()))
    task=asyncio.create_task(app.worker());await asyncio.sleep(.15);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert peak==1
    with app.connect() as c:assert c.execute("SELECT COUNT(*) FROM jobs WHERE status='completed'").fetchone()[0]==2

def test_restart_recovers_processing_jobs(monkeypatch,tmp_path):
    monkeypatch.setattr(app,"DB",tmp_path/"recover.sqlite");app.init_db()
    with app.connect() as c:
        c.execute("INSERT INTO products(id,title,status,content_hash,last_synced_at,processing_status) VALUES('p','P','ACTIVE','x',?,'processing')",(app.now(),));c.execute("INSERT INTO jobs(id,product_id,kind,status,created_at) VALUES('j','p','analyze','processing',?)",(app.now(),))
    app.init_db()
    with app.connect() as c:assert c.execute("SELECT status FROM jobs WHERE id='j'").fetchone()[0]=='queued'

@pytest.mark.asyncio
async def test_selective_approval_is_persisted(monkeypatch,tmp_path):
    monkeypatch.setattr(app,"DB",tmp_path/"approval.sqlite");app.init_db()
    output={"voto":80,"title":"Nuovo","descriptionHtml":"<p>Nuova</p>","metaTitle":"Meta","metaDescription":"Descrizione","keywords":[],"criticita":[],"modificheSuggerite":[]}
    with app.connect() as c:
        c.execute("INSERT INTO products(id,title,status,content_hash,last_synced_at) VALUES('p','P','ACTIVE','x',?)",(app.now(),));c.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,score,status,source_hash,created_at) VALUES('g','p',1,'m',?,80,'generated','x',?)",(json.dumps(output),app.now()))
    result=await app.approve('g',{"fields":["metaTitle"]})
    assert result=={"approved":["metaTitle"]}
    with app.connect() as c:
        approved=[r[0] for r in c.execute("SELECT field FROM approvals WHERE generation_id='g' AND approved=1")]
    assert approved==["metaTitle"]

@pytest.mark.asyncio
async def test_publish_stops_on_shopify_conflict(monkeypatch,tmp_path):
    monkeypatch.setattr(app,"DB",tmp_path/"conflict.sqlite");app.init_db();source={"title":"Prima","descriptionHtml":"","metaTitle":"","metaDescription":""};output={"voto":80,"title":"Dopo","descriptionHtml":"","metaTitle":"","metaDescription":"","keywords":[],"criticita":[],"modificheSuggerite":[]}
    with app.connect() as c:
        c.execute("INSERT INTO products(id,title,status,content_hash,last_synced_at) VALUES('p','Prima','ACTIVE',?,?)",(app.fingerprint(source),app.now()));c.execute("INSERT INTO shopify_snapshots VALUES('s','p',?,?,?,'sync')",(json.dumps(source),app.fingerprint(source),app.now()));c.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,score,status,source_hash,created_at) VALUES('g','p',1,'m',?,80,'approved',?,?)",(json.dumps(output),app.fingerprint(source),app.now()));c.execute("INSERT INTO approvals VALUES('a','g','title',1,?)",(app.now(),))
    async def changed(_):return {**source,"title":"Modificato fuori app"}
    monkeypatch.setattr(app,"fresh_product",changed)
    with pytest.raises(Exception) as exc:await app.publish('g')
    assert exc.value.status_code==409

@pytest.mark.asyncio
async def test_restore_keeps_old_generation(monkeypatch,tmp_path):
    monkeypatch.setattr(app,"DB",tmp_path/"restore.sqlite");app.init_db();out=json.dumps({"title":"T"})
    with app.connect() as c:
        c.execute("INSERT INTO products(id,title,status,content_hash,last_synced_at) VALUES('p','P','ACTIVE','x',?)",(app.now(),));c.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,score,status,source_hash,created_at) VALUES('g','p',1,'m',?,70,'rejected','x',?)",(out,app.now()))
    await app.restore('g')
    with app.connect() as c:assert c.execute("SELECT COUNT(*) FROM generations WHERE product_id='p'").fetchone()[0]==2

@pytest.mark.asyncio
async def test_queue_pause_resume_and_cancel(monkeypatch,tmp_path):
    monkeypatch.setattr(app,"DB",tmp_path/"controls.sqlite");app.init_db()
    with app.connect() as c:
        c.execute("INSERT INTO products(id,title,status,content_hash,last_synced_at,processing_status) VALUES('p','P','ACTIVE','x',?,'queued')",(app.now(),));c.execute("INSERT INTO jobs(id,product_id,kind,status,created_at) VALUES('j','p','analyze','queued',?)",(app.now(),))
    await app.queue_action('pause')
    with app.connect() as c:assert c.execute("SELECT paused FROM queue_state").fetchone()[0]==1
    await app.queue_action('resume');await app.queue_action('cancel')
    with app.connect() as c:assert c.execute("SELECT status FROM jobs WHERE id='j'").fetchone()[0]=='cancelled'

def test_prompt_changes_create_versions_and_mark_products(monkeypatch,tmp_path):
    monkeypatch.setattr(app,"DB",tmp_path/"prompts.sqlite");app.init_db()
    with app.connect() as c:c.execute("INSERT INTO products(id,title,status,content_hash,last_synced_at,processing_status) VALUES('p','P','ACTIVE','x',?,'completed')",(app.now(),));raw=c.execute("SELECT value_json FROM settings").fetchone()[0]
    settings=json.loads(raw);settings['seo_prompt']+=' Nuova regola.'
    with app.connect() as c:c.execute("UPDATE settings SET value_json=?,updated_at=? WHERE id=1",(json.dumps(settings),app.now()))
    version=app.ensure_prompt_version()
    with app.connect() as c:
        assert version==2
        assert c.execute("SELECT processing_status FROM products WHERE id='p'").fetchone()[0]=='reevaluate'

def test_product_detail_accepts_full_shopify_gid(monkeypatch,tmp_path):
    monkeypatch.setattr(app,"DB",tmp_path/"detail.sqlite");app.init_db();gid="gid://shopify/Product/9865470443848"
    with app.connect() as c:c.execute("INSERT INTO products(id,title,status,content_hash,last_synced_at) VALUES(?,?,?,?,?)",(gid,"Prodotto","ACTIVE","x",app.now()))
    with TestClient(app.app) as client:response=client.get("/api/product-detail",params={"id":gid})
    assert response.status_code==200
    assert response.json()["product"]["id"]==gid

@pytest.mark.asyncio
async def test_generate_is_rejected_before_queue_when_no_ollama_models(monkeypatch,tmp_path):
    monkeypatch.setattr(app,"DB",tmp_path/"ollama.sqlite");app.init_db()
    async def none(_):return []
    monkeypatch.setattr(app,"ollama_models",none)
    with pytest.raises(app.HTTPException) as exc:await app.enqueue({"kind":"generate","product_ids":["p"]})
    assert exc.value.status_code==409
    assert "non ha modelli installati" in exc.value.detail
    with app.connect() as c:assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]==0

