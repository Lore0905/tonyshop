import pytest
from fastapi.testclient import TestClient
import app

STATES=['to_analyze','queued','processing','completed','published','reevaluate','error']

@pytest.fixture
def client(monkeypatch,tmp_path):
    monkeypatch.setattr(app,'DB',tmp_path/'filters.sqlite');app.init_db()
    with app.connect() as c:
        for i,state in enumerate(STATES):
            c.execute('INSERT INTO products(id,title,status,product_type,vendor,processing_status,current_score,last_error,content_hash,last_synced_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (state,f'{i} prodotto','ACTIVE' if i%2==0 else 'DRAFT','Ricambi','KAYO',state,10*i,'errore' if state=='error' else None,'hash',app.now()))
    return TestClient(app.app)

@pytest.mark.parametrize('state',STATES)
def test_each_processing_filter_exact(client,state):
    r=client.get('/api/products',params={'processing':state});assert r.status_code==200
    assert r.json()['total']==1
    assert [p['processing_status'] for p in r.json()['items']]==[state]


def test_multiselect_combination_and_pagination(client):
    r=client.get('/api/products',params=[('processing','to_analyze'),('processing','processing'),('processing','published'),('statuses','ACTIVE'),('min_score','15'),('max_score','50'),('vendors','KAYO'),('types','Ricambi'),('limit','1'),('offset','1')]).json()
    assert r['total']==2
    assert [p['id'] for p in r['items']]==['published']
    assert client.get('/api/products',params={'processing':'queued','statuses':'ACTIVE'}).json()['total']==0
    assert client.get('/api/products',params={'errors':'true'}).json()['items'][0]['id']=='error'


def add_generation(pid,status='generated',version=1):
    with app.connect() as c:
        c.execute('INSERT INTO generations(id,product_id,prompt_version,model,output_json,score,status,source_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)',('g-'+pid,pid,version,'model',app.jdump({'title':'Different title from actual partial publication'}),80,status,'hash',app.now()))


def test_open_proposal_filter_before_limit_and_count(client):
    add_generation('to_analyze','rejected');add_generation('queued','stale');add_generation('published','published')
    add_generation('reevaluate','blocked');add_generation('error','approved')
    result=client.get('/api/products',params={'proposal':'true','limit':1,'offset':1}).json()
    assert result['total']==2
    assert [p['id'] for p in result['items']]==['error']
    assert client.get('/api/products',params={'proposal':'false'}).json()['total']==5


def test_updated_filter_supports_partial_publication_and_pagination(client):
    for pid in ['completed','published']:
        add_generation(pid,'published')
        with app.connect() as c:
            row=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
            current=app.content_of(row)
            c.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?)',('pub-'+pid,pid,'g-'+pid,'["metaTitle"]','{}',app.jdump(current),'published',None,app.now()))
    response=client.get('/api/products',params={'updated':'yes','limit':1,'offset':1}).json()
    assert response['total']==2
    assert len(response['items'])==1 and response['items'][0]['updated'] is True
    assert client.get('/api/products',params={'updated':'no'}).json()['total']==5
    # New Shopify text invalidates the updated badge and filter together.
    with app.connect() as c:c.execute("UPDATE products SET meta_title='Changed elsewhere' WHERE id='published'")
    response=client.get('/api/products',params={'updated':'yes'}).json()
    assert response['total']==1 and response['items'][0]['id']=='completed'
    with app.connect() as c:c.execute("UPDATE generations SET prompt_version=0")
    assert client.get('/api/products',params={'updated':'yes'}).json()['total']==0


@pytest.mark.parametrize('params',[{'offset':-1},{'limit':0},{'limit':501},{'updated':'bad'},{'min_score':101}])
def test_filter_input_bounds(client,params):
    assert client.get('/api/products',params=params).status_code==422


def test_invalid_score_range(client):
    assert client.get('/api/products',params={'min_score':90,'max_score':20}).status_code==400


def test_queue_counts_include_older_jobs_beyond_first_500(client):
    with app.connect() as c:
        for i in range(502):
            c.execute('INSERT INTO jobs(id,product_id,kind,status,created_at) VALUES(?,?,?,?,?)',(str(i),'queued','analyze','queued' if i<2 else 'completed',f'{i:04}'))
    data=client.get('/api/queue').json()
    assert data['queued']==2 and data['completed']==500


def test_startup_repairs_never_analyzed_without_changing_running_states(client):
    with app.connect() as c:
        c.execute("UPDATE products SET processing_status='reevaluate' WHERE id IN ('to_analyze','error')")
    add_generation('reevaluate')
    app.init_db()
    with app.connect() as c:
        assert c.execute("SELECT processing_status FROM products WHERE id='to_analyze'").fetchone()[0]=='to_analyze'
        assert c.execute("SELECT processing_status FROM products WHERE id='error'").fetchone()[0]=='error'
        assert c.execute("SELECT processing_status FROM products WHERE id='reevaluate'").fetchone()[0]=='reevaluate'


@pytest.mark.asyncio
async def test_supplier_import_preserves_unanalyzed_and_error_states(client,monkeypatch):
    async def catalog():return {'products':[],'combinations':[]}
    monkeypatch.setattr(app,'fetch_catalog',catalog)
    await app.import_supplier({'to_analyze','error','completed'})
    with app.connect() as c:
        states=dict(c.execute('SELECT id,processing_status FROM products'))
    assert states['to_analyze']=='to_analyze' and states['error']=='error'
    assert states['completed']=='reevaluate'

@pytest.mark.parametrize('state',['to_approve','needs_review'])
def test_codex_review_status_filter(client,state):
    with app.connect() as c:c.execute('UPDATE products SET processing_status=? WHERE id=?',(state,'to_analyze'))
    response=client.get('/api/products',params={'processing':state}).json()
    assert response['total']==1 and response['items'][0]['processing_status']==state
