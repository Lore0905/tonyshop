import json
from datetime import datetime,timedelta,timezone
import httpx
import pytest
import app
import supplier
from content_safety import clean_html,factual_issues


def catalog():
    return {'products':[{'id':1,'reference':'P1','name':'Carburatore KAYO 125 cc','description':'<p>Diametro 22 mm.</p>','description_short':'','meta_title':'','meta_description':''}], 'combinations':[{'id':2,'id_product':1,'reference':'V2'}]}


def test_exact_all_variant_match_and_optional_metadata():
    result=supplier.match_catalog(catalog(),[{'sku':'P1'},{'sku':'V2'}])
    assert result['status']=='matched'
    assert result['data']['matched_skus']==['P1','V2']
    assert result['data']['metaTitle']==''
    assert supplier.match_catalog(catalog(),[{'sku':'P1'},{'sku':'unknown'}])['status']=='missing'
    assert supplier.match_catalog(catalog(),[{'sku':''}])['status']=='missing'
    assert supplier.match_catalog(catalog(),[])['status']=='missing'


def test_ambiguous_reference_and_multiple_parent_products_block():
    data=catalog();data['products'].append({'id':3,'reference':'P1','name':'Altro'})
    assert supplier.match_catalog(data,[{'sku':'P1'}])['status']=='ambiguous'
    data['products'][1]['reference']='OTHER'
    assert supplier.match_catalog(data,[{'sku':'P1'},{'sku':'OTHER'}])['status']=='ambiguous'


def test_missing_italian_and_empty_description_block(monkeypatch):
    monkeypatch.setenv('AUTOFANTASY_LANGUAGE_ID','1')
    data=catalog();data['products'][0]['name']=[{'id':'2','value':'English'}]
    assert supplier.match_catalog(data,[{'sku':'P1'}])['status']=='incomplete'
    assert supplier.localized([{'id':'1','value':'Italiano'},{'id':'2','value':'English'}])=='Italiano'


def test_html_removes_executable_content_and_attributes():
    html='<h1>Titolo</h1><p onclick="alert(1)">Testo <strong>utile</strong><img src=x onerror=alert(1)></p><script>alert(1)</script><svg><script>bad()</script></svg>'
    cleaned=clean_html(html)
    assert cleaned=='Titolo<p>Testo <strong>utile</strong></p>'
    assert clean_html(cleaned)==cleaned


def test_fact_guard_checks_all_fields_and_promises():
    p={'title':'Carburatore 125 cc','descriptionHtml':'<p>Diametro 26 mm. Spedizione gratuita</p>','metaTitle':'KAYO 999','metaDescription':'Compatibile YAMAHA'}
    issues=factual_issues(p,'Carburatore KAYO 125 cc diametro 22 mm')
    assert any('26' in i for i in issues) and any('999' in i for i in issues)
    assert any('YAMAHA' in i for i in issues) and any('spedizione gratuita' in i for i in issues)


@pytest.fixture
def seeded(monkeypatch,tmp_path):
    monkeypatch.setattr(app,'DB',tmp_path/'test.sqlite');app.init_db()
    current={'title':'Carburatore KAYO 125 cc','descriptionHtml':'<p>Diametro 22 mm.</p>','metaTitle':'','metaDescription':''}
    with app.connect() as c:
        c.execute('INSERT INTO products(id,title,description_html,status,variants_json,content_hash,last_synced_at) VALUES(?,?,?,?,?,?,?)',('p',current['title'],current['descriptionHtml'],'ACTIVE',json.dumps([{'sku':'P1'}]),app.fingerprint(current),app.now()))
        c.execute('INSERT INTO shopify_snapshots VALUES(?,?,?,?,?,?)',('snap','p',app.jdump(current),app.fingerprint(current),app.now(),'sync'))
    async def fetch():return catalog()
    monkeypatch.setattr(app,'fetch_catalog',fetch)
    return current


@pytest.mark.asyncio
async def test_import_invalidation_and_failed_fetch_keeps_previous(seeded,monkeypatch):
    result=await app.import_supplier({'p'});assert result['matched']==1
    before=app.supplier_for('p')
    async def bad():raise RuntimeError('unavailable')
    monkeypatch.setattr(app,'fetch_catalog',bad)
    with pytest.raises(RuntimeError):await app.import_supplier({'p'})
    assert app.supplier_for('p')==before


async def make_generation(monkeypatch,proposal=None):
    async def generate(row,settings,imported):
        assert imported['data']['supplier_id']=='1'
        return proposal or {'voto':99,'title':'Carburatore KAYO 125 cc','descriptionHtml':'<p>Diametro 22 mm.</p>','metaTitle':'Carburatore KAYO','metaDescription':'Carburatore KAYO 125 cc.','keywords':[],'marca':'KAYO','criticita':[],'modificheSuggerite':[]}
    async def verify(*args):return []
    monkeypatch.setattr(app,'call_ollama',generate);monkeypatch.setattr(app,'verify_facts',verify)
    await app.import_supplier({'p'});await app.process_job({'kind':'generate','product_id':'p'})
    with app.connect() as c:return dict(c.execute('SELECT * FROM generations').fetchone())


@pytest.mark.asyncio
async def test_generation_records_frozen_source_and_blocks_unsupported_numbers(seeded,monkeypatch):
    out={'voto':99,'title':'Carburatore KAYO 125 cc','descriptionHtml':'<p>Diametro 99 mm.</p>','metaTitle':'KAYO','metaDescription':'','marca':'KAYO','criticita':[]}
    generation=await make_generation(monkeypatch,out)
    assert generation['status']=='blocked'
    with pytest.raises(app.HTTPException) as e:await app.approve(generation['id'],{'fields':['descriptionHtml']})
    assert e.value.status_code==409
    with app.connect() as c:assert c.execute('SELECT data_json FROM generation_sources').fetchone()


@pytest.mark.asyncio
async def test_publish_only_approved_fields_and_snapshot_precedes_mutation(seeded,monkeypatch):
    generation=await make_generation(monkeypatch)
    await app.approve(generation['id'],{'fields':['metaTitle']})
    async def fresh(_):return seeded
    async def graphql(query,variables):
        assert variables=={'product':{'id':'p','seo':{'title':'Carburatore KAYO'}}}
        with app.connect() as c:assert c.execute("SELECT 1 FROM shopify_snapshots WHERE reason='before_publish'").fetchone()
        return {'productUpdate':{'product':{'id':'p'},'userErrors':[]}}
    monkeypatch.setattr(app,'fresh_product',fresh);monkeypatch.setattr(app,'graphql',graphql)
    result=await app.publish(generation['id']);assert result=={'published':['metaTitle']}
    with app.connect() as c:
        assert c.execute('SELECT title FROM products').fetchone()[0]==seeded['title']
        assert c.execute('SELECT count(*) FROM publications').fetchone()[0]==1


@pytest.mark.asyncio
async def test_source_change_expires_approval_and_stale_source_blocks(seeded,monkeypatch):
    generation=await make_generation(monkeypatch)
    await app.approve(generation['id'],{'fields':['metaTitle']})
    updated=catalog();updated['products'][0]['description']='<p>Diametro 23 mm.</p>'
    async def changed():return updated
    monkeypatch.setattr(app,'fetch_catalog',changed)
    await app.import_supplier({'p'})
    with app.connect() as c:
        assert c.execute('SELECT status FROM generations').fetchone()[0]=='stale'
        assert not c.execute('SELECT 1 FROM approvals').fetchone()
    with pytest.raises(app.HTTPException):await app.approve(generation['id'],{'fields':['metaTitle']})


@pytest.mark.asyncio
async def test_edit_clears_approvals_and_runs_semantic_review(seeded,monkeypatch):
    generation=await make_generation(monkeypatch)
    await app.approve(generation['id'],{'fields':['metaTitle']})
    async def review(*args):return ['Materiale acciaio non documentato']
    monkeypatch.setattr(app,'verify_facts',review)
    result=await app.edit_generation(generation['id'],{'descriptionHtml':'<p>Corpo in acciaio.</p>'})
    assert result['issues']==['Materiale acciaio non documentato']
    with app.connect() as c:
        assert not c.execute('SELECT 1 FROM approvals').fetchone()
        assert c.execute('SELECT status FROM generations').fetchone()[0]=='blocked'


@pytest.mark.asyncio
async def test_source_expiration_blocks_generation(seeded):
    await app.import_supplier({'p'})
    old=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    with app.connect() as c:c.execute('UPDATE supplier_imports SET imported_at=?',(old,))
    with pytest.raises(RuntimeError,match='scaduto'):app.supplier_ready('p')


@pytest.mark.asyncio
async def test_bulk_pagination_and_language(monkeypatch):
    calls=[]
    monkeypatch.setenv('AUTOFANTASY_LANGUAGE_ID','1')
    def handle(request):
        calls.append(request)
        assert request.url.params['language']=='1'
        resource=request.url.path.split('/')[-1]
        offset=int(request.url.params['limit'].split(',')[0])
        rows=[{'id':i} for i in range(100)] if resource=='products' and offset==0 else []
        return httpx.Response(200,json={resource:rows})
    client=httpx.AsyncClient
    monkeypatch.setenv('AUTOFANTASY_WEBSERVICE_KEY','test-only')
    monkeypatch.setattr(supplier.httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(handle),**kw))
    result=await supplier.fetch_catalog()
    assert len(result['products'])==100 and len(calls)==3

@pytest.mark.asyncio
async def test_failed_publication_keeps_durable_backup_and_attempt(seeded,monkeypatch):
    generation=await make_generation(monkeypatch)
    await app.approve(generation['id'],{'fields':['metaTitle']})
    async def fresh(_):return seeded
    async def fail(*args):raise RuntimeError('remote unavailable')
    monkeypatch.setattr(app,'fresh_product',fresh);monkeypatch.setattr(app,'graphql',fail)
    with pytest.raises(RuntimeError):await app.publish(generation['id'])
    with app.connect() as c:
        assert c.execute("SELECT 1 FROM shopify_snapshots WHERE reason='before_publish'").fetchone()
        assert c.execute('SELECT status FROM publications').fetchone()[0]=='failed'
        assert c.execute('SELECT status FROM generations').fetchone()[0]=='approved'


@pytest.mark.asyncio
async def test_semantic_review_contract_and_fail_closed(monkeypatch):
    responses=iter([{'unsupported_claims':['acciaio non documentato'],'uncertainties':[]},{'unsupported_claims':'bad'}])
    def handle(request):
        payload=json.loads(request.content)
        assert payload['options']['temperature']==0
        assert 'fonte_autofantasy' in payload['messages'][1]['content']
        return httpx.Response(200,json={'message':{'content':json.dumps(next(responses))}})
    client=httpx.AsyncClient
    monkeypatch.setattr(app.httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(handle),**kw))
    result=await app.verify_facts({'title':'Corpo in acciaio'},{'data':{'title':'Corpo'}},app.Settings())
    assert result==['Revisione tecnica AI: acciaio non documentato']
    with pytest.raises(RuntimeError,match='non valida'):await app.verify_facts({}, {'data':{}},app.Settings())

@pytest.mark.asyncio
async def test_codex_unchanged_proposal_approval_never_calls_ollama(seeded,monkeypatch):
    generation=await make_generation(monkeypatch)
    with app.connect() as c:
        c.execute("UPDATE generations SET model='Codex' WHERE id=?",(generation['id'],))
        c.execute("UPDATE generation_sources SET validation_json=? WHERE generation_id=?",(app.jdump({'issues':[],'engine':'codex','human_review_required':True}),generation['id']))
    async def forbidden(*args):raise AssertionError('Codex proposal must not call local AI')
    monkeypatch.setattr(app,'verify_facts',forbidden)
    out=app.jload(generation['output_json'],{})
    saved=await app.edit_generation(generation['id'],{k:out[k] for k in ('title','descriptionHtml','metaTitle','metaDescription')})
    assert saved['issues']==[]
    assert await app.approve(generation['id'],{'fields':['metaTitle']})=={'approved':['metaTitle']}
    changed=await app.edit_generation(generation['id'],{'title':'Titolo cambiato'})
    assert any('revisione Codex' in issue for issue in changed['issues'])
    with pytest.raises(app.HTTPException):await app.approve(generation['id'],{'fields':['title']})


@pytest.mark.asyncio
async def test_title_only_source_requires_explicit_codex_review(seeded):
    await app.import_supplier({'p'})
    with app.connect() as c:c.execute("UPDATE supplier_imports SET status='incomplete'")
    with pytest.raises(RuntimeError):app.supplier_ready('p')
    assert app.supplier_ready('p',allow_title_only=True)['status']=='incomplete'
    with app.connect() as c:c.execute("UPDATE supplier_imports SET data_json=?",(app.jdump({'title':''}),))
    with pytest.raises(RuntimeError):app.supplier_ready('p',allow_title_only=True)

@pytest.mark.asyncio
async def test_codex_import_is_idempotent_unapproved_and_source_checked(seeded,monkeypatch,tmp_path):
    import sys
    sys.path.insert(0,str(app.BASE/'scripts'))
    import import_codex_proposals as pipeline
    await app.import_supplier({'p'})
    imported=app.supplier_for('p')
    source={'id':'p','current':seeded,'source_hash':imported['source_hash']}
    output={'voto':75,'title':seeded['title'],'descriptionHtml':seeded['descriptionHtml'],'metaTitle':'Carburatore KAYO','metaDescription':'Carburatore KAYO 125 cc, diametro 22 mm.','keywords':['carburatore KAYO'],'marca':'KAYO','criticita':[],'modificheSuggerite':[]}
    path=tmp_path/'proposals.json';path.write_text(app.jdump([{'id':'p','reviewed':True,'output':output,'blocking_issues':[]}]))
    result=pipeline.import_file(path,{'p':source});assert result['generated']==1
    assert pipeline.import_file(path,{'p':source})['unchanged']==1
    with app.connect() as c:
        assert c.execute("SELECT processing_status FROM products").fetchone()[0]=='to_approve'
        assert not c.execute('SELECT 1 FROM approvals').fetchone()
        assert not c.execute('SELECT 1 FROM publications').fetchone()
    source['source_hash']='changed'
    with pytest.raises(ValueError,match='Source changed'):pipeline.import_file(path,{'p':source})


def test_fact_guard_allows_only_equivalent_typographical_normalization():
    evidence='01pz guarnizione 125cc 4T, 24X16X5 mm, codice MW<strong>-</strong>8502, portata m<sup>3</sup>/s'
    good={'title':'Guarnizione 125 cc 4 tempi (1 pezzo)','descriptionHtml':'<p>Misura 24 x 16 x 5 mm. Codice MW-8502.</p>'}
    assert factual_issues(good,evidence)==[]
    assert factual_issues({'title':'Guarnizione 250 cc','descriptionHtml':'24 x 16 x 6 mm'},evidence)
    assert factual_issues({'title':'Guarnizione','descriptionHtml':'<p>125 mm</p>'},evidence)
    assert factual_issues({'title':'Guarnizione 2 pezzi','descriptionHtml':'Testo'},evidence)


def test_fact_guard_keeps_block_boundaries_and_italian_preposition():
    evidence='<div>VT-774</div><div>Pezzi per confezione: 1</div> pit bike 110 4T'
    proposal={'title':'Pit bike 110 a 4 tempi','descriptionHtml':'<p>Codice VT-774. Pezzi per confezione: 1</p>'}
    assert factual_issues(proposal,evidence)==[]
    assert factual_issues({'title':'Codice VT-775','descriptionHtml':'Testo'},evidence)


def test_fact_guard_never_merges_separate_fields_or_torque_labels():
    evidence='8500<br>Coppia massima: 10 NM<div>Numero: 1208</div><div>V-TAC</div>intervallo 1.6 A 1.85'
    proposal={'title':'Numero 1208','metaTitle':'V-TAC','descriptionHtml':'<p>8500. Coppia massima: 10 NM. Intervallo 1.6 a 1.85.</p>'}
    assert factual_issues(proposal,evidence)==[]
    assert factual_issues({'title':'8500cp','descriptionHtml':'Testo'},evidence)


def test_fact_guard_quantity_comes_from_consumer_pack_not_shipping_carton():
    p={'title':'Supporto (1 pezzo)','descriptionHtml':'Supporto'}
    assert factual_issues(p,'Supporto. Pezzi per confezione: 1')==[]
    assert factual_issues(p,'Supporto. Pezzi per scatola: 1')
    assert factual_issues({'title':'Supporto (2 pezzi)','descriptionHtml':'Supporto'},'Supporto. Pezzi per confezione: 1')


def test_fact_guard_reconstructs_explicit_dimension_split_across_paragraphs():
    source='<p>Interasse fissaggio: 47</p><p>mm</p>'
    assert factual_issues({'title':'Interasse 47mm','descriptionHtml':'Interasse documentato.'},source)==[]
    assert factual_issues({'title':'Interasse 48mm','descriptionHtml':'Testo'},source)
    assert factual_issues({'title':'Interasse 47cm','descriptionHtml':'Testo'},source)
