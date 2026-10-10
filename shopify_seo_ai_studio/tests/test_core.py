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
