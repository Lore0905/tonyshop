
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
async def call_ollama(row,s):
    gid=row["id"]; source={**content_of(row),"productType":row["product_type"],"vendor":row["vendor"],"variants":jload(row["variants_json"],[])}
    prompt=f"{s.seo_prompt}\nLingua: {s.language}. Stile: {s.description_style}. Negozio: {s.shop_name}. Keyword prioritarie: {s.priority_keywords}.\nProdotto, unica fonte ammessa:\n{jdump({gid:source})}"
    payload={"model":s.model,"stream":False,"format":schema_for(gid),"messages":[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":prompt}],"options":{"temperature":s.temperature,"num_predict":s.num_predict}}
    async with httpx.AsyncClient(timeout=s.timeout) as client:
        r=await client.post(f"{str(s.ollama_url).rstrip('/')}/api/chat",json=payload);r.raise_for_status(); body=r.json()
    raw=body.get("message",{}).get("content",""); parsed=json.loads(raw); proposal=Proposal.model_validate(parsed[gid]); return proposal.model_dump()

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
                    if attempts<max_attempts:c.execute("UPDATE jobs SET status='queued',error=?,next_attempt_at=? WHERE id=?",(str(exc),time.time()+min(60,2**attempts),job["id"]));c.execute("UPDATE products SET processing_status='queued',last_error=? WHERE id=?",(str(exc),job["product_id"]))
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
        async with httpx.AsyncClient(timeout=10) as client:r=await client.get(f"{str(s.ollama_url).rstrip('/')}/api/tags");r.raise_for_status();models=[m["name"] for m in r.json().get("models",[])]
        return {"connected":True,"models":models}
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
async function openDetail(id){try{const d=await api('/api/products/'+encodeURIComponent(id)),g=d.product.latest_generation,out=g?.output||{},cur=d.current;$('#detailTitle').textContent=d.product.title;let html='<div class="compare"><section class="column ai"><div class="field-head"><h3>Proposta AI</h3>'+(g?`<button class="btn primary approve-all" data-id="${g.id}">Approva modifiche</button>`:'')+'</div>';
    if(g){for(const [k,l] of [['title','Titolo'],['descriptionHtml','Descrizione HTML'],['metaTitle','Meta title'],['metaDescription','Meta description']])html+=valueBox(l,escape(out[k]),out[k]!==cur[k],`<label><input class="field-approve" value="${k}" type="checkbox" checked> approva</label>`);html+=`<p><strong>Score proposto: ${g.score}</strong> · Δ ${g.score-(d.product.current_score||0)}</p><p>${(out.criticita||[]).map(escape).join(' · ')}</p>`}else html+='<p>Nessuna generazione disponibile.</p>';html+='</section><section class="column"><div class="field-head"><h3>Versione Shopify</h3><button class="btn keep">Mantieni versione attuale</button></div>';for(const [k,l] of [['title','Titolo'],['descriptionHtml','Descrizione HTML'],['metaTitle','Meta title'],['metaDescription','Meta description']])html+=valueBox(l,escape(cur[k]));html+=`<p><strong>Score attuale: ${d.product.current_score??'—'}</strong></p></section></div><section class="history"><h3>Storico generazioni (${d.history.length})</h3>`;
    for(const h of d.history)html+=`<details><summary>${new Date(h.created_at).toLocaleString('it-IT')} · ${escape(h.model)} · prompt v${h.prompt_version} · ${h.score}/100 · ${escape(h.status)}</summary><p><strong>${escape(h.output.title)}</strong></p><p>${escape(h.output.metaDescription)}</p><div class="history-actions"><button class="btn restore" data-id="${h.id}">Ripristina come proposta</button>${h.status==='approved'?`<button class="btn primary publish-one" data-id="${h.id}">Pubblica</button>`:''}</div></details>`;html+='</section>';$('#detailBody').innerHTML=html;$('#detail').showModal();$('.keep').onclick=async()=>{if(g)await api(`/api/generations/${g.id}/reject`,{method:'POST'});$('#detail').close();notice('Proposta rifiutata; versione Shopify mantenuta.')};const approve=$('.approve-all');if(approve)approve.onclick=async()=>{const fields=$$('.field-approve:checked').map(x=>x.value);await api(`/api/generations/${approve.dataset.id}/approve`,{method:'POST',body:JSON.stringify({fields})});notice('Campi approvati; non sono ancora pubblicati.');openDetail(id)};$$('.restore').forEach(b=>b.onclick=async()=>{await api(`/api/generations/${b.dataset.id}/restore`,{method:'POST'});notice('Versione ripristinata come nuova proposta.');openDetail(id)});$$('.publish-one').forEach(b=>b.onclick=()=>publishGeneration(b.dataset.id,id))}catch(e){notice(e.message,true)}}
async function publishGeneration(gid,pid){if(!confirm('Pubblicare su Shopify soltanto i campi approvati? Verrà verificata prima la presenza di conflitti.'))return;try{const r=await api(`/api/generations/${gid}/publish`,{method:'POST'});notice(`Pubblicati: ${r.published.join(', ')}.`);$('#detail').close();await load()}catch(e){notice(e.message,true)}}
async function publishSelected(){const targets=S.items.filter(p=>S.selected.has(p.id)&&p.latest_generation?.status==='approved');if(!targets.length)return notice('Nessuna proposta approvata tra i prodotti visibili selezionati.',true);if(!confirm(`Pubblicare ${targets.length} prodotti approvati su Shopify?`))return;let ok=0;for(const p of targets){try{await api(`/api/generations/${p.latest_generation.id}/publish`,{method:'POST'});ok++}catch(e){notice(`${p.title}: ${e.message}`,true)}}notice(`${ok}/${targets.length} prodotti pubblicati.`);load()}
async function queue(){try{const q=await api('/api/queue');const eta=q.eta_seconds==null?'—':`${Math.ceil(q.eta_seconds/60)} min`;$('#queue').innerHTML=`<strong>Coda</strong><span>${q.processing} in corso · ${q.queued} attesa · ${q.completed} completati · ${q.error} errori · ETA ${eta}</span>${q.current?`<span class="spinner"></span>`:''}<span style="margin-left:auto"></span><button class="btn qpause">${q.paused?'Riprendi':'Pausa'}</button><button class="btn qcancel">Annulla attesa</button>`;$('.qpause').onclick=async()=>{await api('/api/queue/'+(q.paused?'resume':'pause'),{method:'POST'});queue()};$('.qcancel').onclick=async()=>{if(confirm('Annullare tutti i job in attesa?')){await api('/api/queue/cancel',{method:'POST'});queue();load()}}}catch(e){notice(e.message,true)}}
async function openSettings(){const s=await api('/api/settings');S.settings=s;for(const [k,v] of Object.entries(s)){const el=$(`[name="${k}"]`);if(el&&typeof v!=='object')el.value=v}for(const [k,v] of Object.entries(s.weights))$(`[name="w_${k}"]`).value=v;$('#settings').showModal()}
$('#settingsForm').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.currentTarget),s={...S.settings};for(const k of ['ollama_url','model','language','shop_name','description_style','priority_keywords','seo_prompt'])s[k]=f.get(k);for(const k of ['timeout','max_retries','batch_size','min_score','num_predict'])s[k]=Number(f.get(k));for(const k of ['pause_seconds','temperature'])s[k]=Number(f.get(k));s.weights={};for(const k of ['title','description','metaTitle','metaDescription','keywords','accuracy'])s.weights[k]=Number(f.get('w_'+k));delete s.system_prompt;delete s.prompt_version;try{const r=await api('/api/settings',{method:'PUT',body:JSON.stringify(s)});notice(`Impostazioni salvate · prompt v${r.prompt_version}.`);$('#settings').close();load()}catch(x){notice(x.message,true)}};
$('#testOllama').onclick=async()=>{const o=$('#ollamaState');o.textContent='Connessione…';try{const r=await api('/api/ollama/test',{method:'POST'});o.textContent=`Connesso · ${r.models.length} modelli: ${r.models.join(', ')||'nessuno'}`}catch(e){o.textContent=e.message}};
async function loadFacets(){const f=await api('/api/facets');for(const [id,key] of [['vendor','vendors'],['productType','types']])$('#'+id).innerHTML=f[key].map(x=>`<option>${escape(x)}</option>`).join('')}
$('#syncBtn').onclick=async e=>{e.currentTarget.disabled=true;notice('Sincronizzazione Shopify…');try{const r=await api('/api/sync',{method:'POST'});notice(`${r.synced} prodotti sincronizzati.`);S.offset=0;await loadFacets();await load()}catch(x){notice(x.message,true)}finally{e.currentTarget.disabled=false}};$('#analyzeBtn').onclick=()=>enqueue('analyze');$('#generateBtn').onclick=()=>enqueue('generate');$('#publishBtn').onclick=publishSelected;$('#settingsBtn').onclick=openSettings;$('#selectVisible').onclick=async()=>{const d=await api('/api/products?'+queryParams(500,0));for(const p of d.items)S.selected.add(p.id);render();if(d.total>500)notice('Selezionati i primi 500 prodotti filtrati; riduci i filtri per operazioni più piccole.')};$('#prev').onclick=()=>{S.offset=Math.max(0,S.offset-S.limit);load()};$('#next').onclick=()=>{S.offset+=S.limit;load()};$$('dialog .close').forEach(b=>b.onclick=()=>b.closest('dialog').close());['search','minScore','maxScore','shopifyStatus','processing','vendor','productType','updated','onlyProposal','onlyErrors'].forEach(id=>$('#'+id).addEventListener('input',debounce(()=>{S.offset=0;load()})));loadFacets();load();queue();setInterval(()=>{queue();if(S.items.some(x=>['queued','processing'].includes(x.processing_status)))load()},2500);


=================================
FILE: static/index.html
=================================

<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Shopify SEO AI Studio</title><link rel="stylesheet" href="/static/styles.css"></head><body>
<header class="topbar"><div><p class="eyebrow">SHOPIFY / AI OPERATIONS</p><h1>SEO AI Studio</h1><p class="subtitle">Analizza, approva e pubblica contenuti SEO con Ollama locale.</p></div><div class="head-actions"><button class="icon" id="settingsBtn" title="Impostazioni">⚙</button><button class="btn primary" id="syncBtn">Sincronizza Shopify</button></div></header>
<main><section id="notice" class="notice">Catalogo locale pronto.</section><section id="queue" class="queuebar"></section>
<section class="toolbar"><div class="action-row"><button class="btn" id="analyzeBtn">Analizza SEO</button><button class="btn accent" id="generateBtn">Genera miglioramenti</button><button class="btn primary" id="publishBtn">Pubblica approvate</button><span id="selectedCount">0 selezionati</span></div><div class="filters"><input id="search" placeholder="Cerca titolo, SKU o ID"><select id="shopifyStatus" multiple title="Stato Shopify"><option>ACTIVE</option><option>DRAFT</option><option>ARCHIVED</option></select><select id="processing" multiple title="Stato elaborazione"><option value="to_analyze">Da analizzare</option><option value="queued">In coda</option><option value="processing">In elaborazione</option><option value="completed">Elaborato</option><option value="published">Pubblicato</option><option value="reevaluate">Da rivalutare</option><option value="error">Errore</option></select><select id="vendor" multiple title="Vendor"></select><select id="productType" multiple title="Product type"></select><select id="updated"><option value="">Aggiornati: tutti</option><option value="yes">Solo aggiornati</option><option value="no">Solo non aggiornati</option></select><label>Score <input id="minScore" type="number" min="0" max="100" value="0">–<input id="maxScore" type="number" min="1" max="100" value="100"></label><label><input id="onlyProposal" type="checkbox"> Proposte aperte</label><label><input id="onlyErrors" type="checkbox"> Errori</label><button class="btn subtle" id="selectVisible">Seleziona filtrati</button></div></section>
<div class="result-head"><strong id="resultCount"></strong><div><button class="page" id="prev">←</button><button class="page" id="next">→</button></div></div><section id="grid" class="grid"></section></main>
<dialog id="detail"><div class="modal-head"><div><p class="eyebrow">CONFRONTO PRODOTTO</p><h2 id="detailTitle"></h2></div><button class="icon close">×</button></div><div id="detailBody"></div></dialog>
<dialog id="settings"><div class="modal-head"><div><p class="eyebrow">CONFIGURAZIONE</p><h2>Impostazioni SEO e Ollama</h2></div><button class="icon close">×</button></div><form id="settingsForm" class="settings-grid"><label>Endpoint Ollama<input name="ollama_url"></label><label>Modello<div class="inline"><input name="model"><button type="button" class="btn" id="testOllama">Test</button></div></label><label>Timeout secondi<input name="timeout" type="number"></label><label>Pausa tra job<input name="pause_seconds" type="number" step=".5"></label><label>Retry massimi<input name="max_retries" type="number"></label><label>Batch<input name="batch_size" type="number"></label><label>Soglia minima<input name="min_score" type="number"></label><label>Token massimi<input name="num_predict" type="number"></label><label>Temperatura<input name="temperature" type="number" step=".1"></label><label>Lingua<input name="language"></label><label>Nome negozio<input name="shop_name"></label><label>Stile descrizioni<input name="description_style"></label><label class="wide">Keyword prioritarie<input name="priority_keywords"></label><label class="wide">Prompt di sistema (protetto)<textarea name="system_prompt" readonly></textarea></label><label class="wide">Prompt SEO personalizzabile<textarea name="seo_prompt"></textarea></label><fieldset class="wide weights"><legend>Pesi SEO (totale 100)</legend><label>Titolo<input name="w_title" type="number"></label><label>Descrizione<input name="w_description" type="number"></label><label>Meta title<input name="w_metaTitle" type="number"></label><label>Meta description<input name="w_metaDescription" type="number"></label><label>Keyword<input name="w_keywords" type="number"></label><label>Accuratezza<input name="w_accuracy" type="number"></label></fieldset><div class="wide form-actions"><span id="ollamaState"></span><button class="btn primary">Salva impostazioni</button></div></form></dialog>
<script src="/static/app.js"></script></body></html>


=================================
FILE: static/styles.css
=================================

:root{--ink:#17221d;--muted:#65736c;--line:#dfe7e2;--paper:#f5f7f5;--card:#fff;--green:#177a4b;--lime:#d9f36a;--red:#c83e3e;--shadow:0 12px 30px #18372912}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}.topbar{padding:28px clamp(18px,4vw,56px);display:flex;justify-content:space-between;align-items:center;background:#13251c;color:white}.topbar h1{font-size:32px;margin:2px 0}.subtitle{margin:0;color:#afc1b7}.eyebrow{margin:0;color:#77c79d;font-weight:800;letter-spacing:.14em;font-size:11px}main{padding:24px clamp(16px,4vw,56px);max-width:1600px;margin:auto}.head-actions,.action-row,.inline,.result-head,.modal-head,.form-actions{display:flex;gap:10px;align-items:center}.notice{padding:11px 14px;border:1px solid #cfe3d7;border-radius:10px;background:#edf8f1;margin-bottom:12px}.notice.error{background:#fff0f0;border-color:#efcaca;color:#9d2929}.queuebar{display:flex;align-items:center;gap:12px;min-height:48px;padding:10px 14px;background:white;border:1px solid var(--line);border-radius:12px;margin-bottom:16px}.toolbar{background:white;border:1px solid var(--line);border-radius:16px;padding:14px;box-shadow:var(--shadow)}.action-row{padding-bottom:13px;border-bottom:1px solid var(--line);flex-wrap:wrap}.action-row span{margin-left:auto;color:var(--muted)}.filters{padding-top:13px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}.filters input,.filters select,.settings-grid input,.settings-grid textarea{border:1px solid #ccd8d1;border-radius:9px;padding:9px 10px;background:#fff;color:var(--ink)}#search{min-width:260px}.btn,.icon,.page{border:1px solid #cbd8d0;background:white;border-radius:9px;padding:9px 13px;font-weight:700;cursor:pointer}.btn:hover,.icon:hover{transform:translateY(-1px)}.btn.primary{background:var(--green);border-color:var(--green);color:white}.btn.accent{background:var(--lime);border-color:#c6dc64}.btn.subtle{background:#eff4f1}.btn:disabled{opacity:.45;cursor:not-allowed}.icon{font-size:20px;padding:8px 12px}.result-head{justify-content:space-between;margin:20px 0 10px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(245px,1fr));gap:16px}.card{position:relative;background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden;box-shadow:var(--shadow);transition:.18s}.card:hover{transform:translateY(-2px);box-shadow:0 16px 36px #1837291d}.card.selected{outline:3px solid #71a987}.image{aspect-ratio:4/3;background:#e8eeea;display:grid;place-items:center;overflow:hidden;position:relative}.image img{width:100%;height:100%;object-fit:contain}.image .placeholder{font-size:42px;color:#9aa8a0}.updated{position:absolute;top:10px;right:10px;background:#143e29;color:white;border-radius:999px;padding:5px 8px;font-size:11px;font-weight:800}.card-body{padding:14px}.card h2{font-size:16px;line-height:1.3;margin:8px 0;min-height:42px}.meta{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.badges{display:flex;gap:6px;flex-wrap:wrap}.badge{font-size:10px;text-transform:uppercase;font-weight:800;padding:4px 7px;border-radius:999px;background:#edf1ef}.badge.error{background:#ffe2e2;color:#9b2424}.score{--score:#999;width:62px;height:62px;border-radius:50%;border:6px solid var(--score);display:grid;place-items:center;font-size:18px;font-weight:900;margin-top:-38px;position:relative;background:white;box-shadow:0 3px 9px #0002}.score.low{--score:#d34a43}.score.mid{--score:#e4ad2d}.score.high{--score:#24975c}.card-select{position:absolute;top:10px;left:10px;z-index:2;width:20px;height:20px}.card-footer{display:flex;align-items:center;justify-content:space-between;margin-top:12px}.link{background:none;border:0;color:var(--green);font-weight:800;cursor:pointer}dialog{width:min(1180px,94vw);max-height:92vh;border:0;border-radius:18px;padding:0;box-shadow:0 30px 80px #0005}dialog::backdrop{background:#0e1813aa}.modal-head{justify-content:space-between;padding:20px 24px;border-bottom:1px solid var(--line);position:sticky;top:0;background:white;z-index:3}.modal-head h2{margin:2px 0}.compare{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:20px}.column{border:1px solid var(--line);border-radius:14px;padding:16px}.column.ai{background:#f5faec}.field{margin:14px 0}.field-head{display:flex;justify-content:space-between;align-items:center}.field label{font-size:11px;font-weight:900;text-transform:uppercase;color:var(--muted)}.value{margin-top:5px;padding:10px;background:white;border:1px solid var(--line);border-radius:9px;max-height:190px;overflow:auto;white-space:pre-wrap}.changed{border-left:4px solid #e2b12e}.history{margin:0 20px 22px;border:1px solid var(--line);border-radius:13px;padding:10px 14px}.history details{border-top:1px solid var(--line);padding:10px 0}.history details:first-of-type{border:0}.history summary{cursor:pointer;font-weight:700}.history-actions{display:flex;gap:8px;margin-top:8px}.settings-grid{padding:22px;display:grid;grid-template-columns:1fr 1fr;gap:14px}.settings-grid label{display:grid;gap:5px;font-weight:700}.settings-grid textarea{min-height:110px;resize:vertical}.wide{grid-column:1/-1}.weights{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;border:1px solid var(--line);border-radius:10px}.form-actions{justify-content:space-between}.spinner{width:13px;height:13px;border:2px solid #ccd5d0;border-top-color:var(--green);border-radius:50%;display:inline-block;animation:spin .7s linear infinite}@keyframes spin{to{rotate:360deg}}@media(max-width:760px){.topbar{align-items:flex-start}.compare,.settings-grid{grid-template-columns:1fr}.wide{grid-column:auto}.filters>*{flex:1 1 45%}#search{min-width:100%;flex-basis:100%}.weights{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr 1fr}}@media(max-width:480px){.grid{grid-template-columns:1fr}.topbar{display:block}.head-actions{margin-top:14px}}


=================================
FILE: tests/test_core.py
=================================

import asyncio, json, sqlite3
from pathlib import Path
import pytest
import app
from app import Proposal
from seo import score_content

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

