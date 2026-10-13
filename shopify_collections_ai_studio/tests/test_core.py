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
    async def fresh(_):
        return list(members), "changed"
    async def fake_mutation(query, variables, field):
        mutations.append((field, variables))
        if field == "collectionCreate":
            title = variables['collection']['title']
            cid = 'gid://shopify/Collection/' + title
            members.append({'id': cid, 'path': [title] if title == 'Motore' else desired[0], 'kind': 'manual', 'source_id': 'source-' + title})
            return {"collection": {"id": cid, "sources": [{"__typename": "CollectionConditionsSource", "id": "source-" + title}]}}
        return {"metafields": [], "userErrors": []}
    async def no_sync(*_):
        return {}
    async def fake_graphql(query, variables):
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
                db.execute("INSERT OR REPLACE INTO collections(id,title,handle,path_json,kind,source_id,products_count,shopify_hash,last_synced_at) VALUES(?,'Ricambi','ricambi','[\"Ricambi\"]','manual','s',1,'x',?)", (item['id'], app.now()))
                db.execute('INSERT OR IGNORE INTO product_collections VALUES(?,?)', (pid, item['id']))
        return {}
    async def fresh(_):
        return list(remote), 'fresh'
    async def graphql(query, variables):
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
