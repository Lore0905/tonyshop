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
