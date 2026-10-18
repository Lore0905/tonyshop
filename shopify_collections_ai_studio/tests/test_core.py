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


def test_general_prompt_preview_matches_runtime_context_without_mutating_sources():
    source = {'titolo': 'Valvole YX170', 'catalogo_collection_esistenti': [{'percorso': ['Ricambi', 'Ricambi motori']}]}
    custom = 'Indicazioni negozio {{testo_letterale}}'
    rendered = app.build_generation_prompt(custom, 'p', source, False)
    assert rendered.startswith(custom + '\n\n' + app.CATALOG_INSTRUCTIONS)
    assert json.loads(rendered.split('unica fonte ammessa:\n', 1)[1]) == {'p': source}
    assert app.MAIN_CATEGORY_INSTRUCTIONS not in rendered
    constrained = app.build_generation_prompt(custom, 'p', source, True)
    assert constrained == rendered + '\n\n' + app.MAIN_CATEGORY_INSTRUCTIONS
    preview = app.general_prompt_preview()
    assert preview.startswith(app.SYSTEM_PROMPT)
    assert app.CATALOG_INSTRUCTIONS in preview
    assert '{{collection_create}}' in preview
    assert '{{prompt_personalizzato}}' in preview
    assert '{{titolo_prodotto}}' in preview


@pytest.mark.asyncio
async def test_settings_expose_readonly_general_prompt_preserving_custom_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(app, 'DB', tmp_path / 'general_prompt.sqlite')
    app.init_db()
    settings = app.Settings(ai_prompt='Le mie istruzioni da conservare')
    await app.update_settings(settings)
    result = await app.read_settings()
    assert result['ai_prompt'] == settings.ai_prompt
    assert result['system_prompt'] == app.SYSTEM_PROMPT
    assert result['general_prompt_template'] == app.general_prompt_preview()
    assert app.get_settings().ai_prompt == settings.ai_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize('first_paths', [[], ['Ricambi motori', 'Valvole'], ['Ricambi', 'Ricambi pit bike e cross', 'Valvole']])
async def test_ollama_repairs_empty_or_invalid_root_using_exact_target(monkeypatch, tmp_path, first_paths):
    monkeypatch.setattr(app, 'DB', tmp_path / 'repair.sqlite')
    app.init_db()
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,description_html,content_hash,last_synced_at) VALUES('p','Valvole motore YX170','Valvole aspirazione e scarico','x',?)", (app.now(),))
        row = db.execute("SELECT * FROM products WHERE id='p'").fetchone()
    captured = []
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            path = first_paths if len(captured) == 1 else ['Ricambi', 'Ricambi motori', 'Valvole']
            return {'message': {'content': json.dumps({'p': {'percorsi': [{'livelli': path, 'confidence': .95, 'motivazione': ''}] if path else [], 'nuove_categorie': [], 'criticita': []}})}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, json):
            captured.append(__import__('copy').deepcopy(json))
            return Response()
    async def available(_): return ['m']
    monkeypatch.setattr(app, 'validate_ollama', available)
    monkeypatch.setattr(app.httpx, 'AsyncClient', Client)
    result = await app.call_ollama(row, app.Settings(model='m', main_categories=['Ricambi']))
    assert result['percorsi'][0]['livelli'] == ['Ricambi', 'Ricambi motori', 'Valvole']
    assert len(captured) == 2
    assert 'Valvole motore YX170' in captured[1]['messages'][-1]['content']
    assert captured[1]['options']['temperature'] == 0
    assert captured[0]['options']['num_ctx'] >= 8192


@pytest.mark.parametrize('vehicle_branch', ['Ricambi pit bike e cross', 'Ricambi quad 4t', 'Ricambi minicross', 'Ricambi scooter'])
def test_reuse_functional_family_for_same_component_across_vehicle_types(vehicle_branch):
    from core import reuse_canonical_categories
    wrong = ['Ricambi', vehicle_branch, "Camere d'aria"]
    right = ['Ricambi', 'Ricambi gomme', "Camere d'aria"]
    output = {'percorsi': [{'livelli': wrong, 'confidence': .9, 'motivazione': 'Compatibile con pit bike'}], 'nuove_categorie': [wrong], 'criticita': []}
    categories = [{'percorso': wrong}, {'percorso': right}]
    result = reuse_canonical_categories(output, categories, 3)
    assert result['percorsi'][0]['livelli'] == right
    assert result['nuove_categorie'] == []
    assert result['percorsi'][0]['motivazione'] == ''


def test_functional_family_reuse_does_not_guess_ambiguous_or_cross_sector_matches():
    from core import reuse_canonical_categories
    path = ['Ricambi', 'Ricambi pit bike e cross', 'Filtri']
    output = {'percorsi': [{'livelli': path, 'confidence': .9}], 'nuove_categorie': [], 'criticita': []}
    categories = [{'percorso': ['Ricambi', family, 'Filtri']} for family in ['Ricambi motori', 'Ricambi carburazione']]
    categories.append({'percorso': ['Giardinaggio', 'Ricambi', 'Filtri']})
    assert reuse_canonical_categories(output, categories, 3)['percorsi'][0]['livelli'] == path


@pytest.mark.asyncio
async def test_large_publish_reads_bulk_memberships_and_preserves_smart_rules(monkeypatch):
    ids = ['p'+str(i) for i in range(50)]
    rules = [{'__typename': 'CollectionSourceInclusionConditionProductTag', 'id': 'rule'}]
    source = {'__typename': 'CollectionConditionsSource', 'id': 's', 'targetType': 'PRODUCTS', 'shareable': False, 'inclusion': {'conditions': rules}}
    collections = [{'id': 'c', 'title': 'Valvole', 'resolved_path': ['Ricambi', 'Ricambi motori', 'Valvole'], 'sources': [source], 'product_ids': ids}]
    calls = []
    async def products(): calls.append('products'); return [{'id': pid} for pid in ids]
    async def fetched(): calls.append('collections'); return collections
    async def individual(_): raise AssertionError('Nessuna lettura per prodotto nei batch grandi')
    monkeypatch.setattr(app, 'fetch_all_products', products)
    monkeypatch.setattr(app, 'fetch_all_collections', fetched)
    monkeypatch.setattr(app, 'fresh_product_state', individual)
    states = await app.fresh_publish_states(ids)
    assert sorted(calls) == ['collections', 'products']
    assert set(states) == set(ids)
    assert states['p0'][0]['kind'] == 'smart'
    assert states['p0'][0]['path'] == ['Ricambi', 'Ricambi motori', 'Valvole']
    assert source['inclusion']['conditions'] == rules


@pytest.mark.asyncio
async def test_large_publish_bulk_read_rejects_deleted_product(monkeypatch):
    ids = ['p'+str(i) for i in range(50)]
    async def products(): return [{'id': pid} for pid in ids[:-1]]
    async def collections(): return []
    monkeypatch.setattr(app, 'fetch_all_products', products)
    monkeypatch.setattr(app, 'fetch_all_collections', collections)
    with pytest.raises(app.HTTPException, match='p49'):
        await app.fresh_publish_states(ids)


@pytest.mark.asyncio
@pytest.mark.parametrize('recover_legacy', [False, True])
async def test_publish_verifies_and_finalizes_every_product_when_new_leaf_has_subset(monkeypatch, tmp_path, recover_legacy):
    monkeypatch.setattr(app, 'DB', tmp_path / 'subset.sqlite')
    app.init_db()
    desired = {'p1': [['Ricambi']], 'p2': [['Ricambi', 'Valvole']]}
    remote = {'p1': [], 'p2': []}
    if recover_legacy:
        for paths in remote.values():paths.append({'id':'cRicambi','title':'Ricambi','path':['Vecchio'],'kind':'manual','source_id':'sRicambi'})
    with app.connect() as db:
        for pid, paths in desired.items():
            db.execute('INSERT INTO products(id,title,content_hash,last_synced_at) VALUES(?,?,?,?)', (pid,pid,'x',app.now()))
            db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,approved_paths_json,status,source_hash,created_at) VALUES(?,?,1,'m','{}',?,'approved','x',?)", ('g'+pid,pid,json.dumps(paths),app.now()))
        operations = {'approval_signatures': {pid: stable_hash({'id':'g'+pid,'paths':paths}) for pid,paths in desired.items()}}
        db.execute('INSERT INTO publish_previews VALUES(?,?,?,?,?,0)',('v',json.dumps(list(desired)),json.dumps(operations),'{}',app.now()))
    async def sync(_): return {}
    async def fresh(pid): return remote[pid], 'x'
    async def parent(): return 'list.collection_reference'
    async def channels(): return ['channel']
    async def publish_channels(*args): pass
    async def graphql(query,variables):
        recovered = {'id':'cRicambi','sources':[{'__typename':'CollectionConditionsSource','id':'sRicambi'}]} if recover_legacy and variables.get('handle') == 'category-'+stable_hash('ricambi')[:24] else None
        return {'collectionByHandle': recovered}
    async def mutate(query,variables,field):
        if field == 'metafieldsSet':
            for field_item in variables['metafields']:
                if field_item['ownerId']=='cRicambi' and field_item['key']=='category_path':
                    for paths in remote.values():
                        for item in paths:
                            if item['id']=='cRicambi':item['path']=['Ricambi']
        if field == 'collectionCreate':
            c=variables['collection']; title=c['title']; cid='c'+title; sid='s'+title
            path=['Ricambi'] if title=='Ricambi' else ['Ricambi',title]
            for selection in c['sources'][0]['source']['inclusion']['selections']:
                remote[selection['productId']].append({'id':cid,'title':title,'path':path,'kind':'manual','source_id':sid})
            return {'collection': {'id':cid,'sources':[{'__typename':'CollectionConditionsSource','id':sid}]}}
        return {}
    async def remove(*args): raise AssertionError('Una collection richiesta non deve essere rimossa')
    monkeypatch.setattr(app,'remove_manual_source_products',remove)
    monkeypatch.setattr(app,'sync_shopify',sync)
    monkeypatch.setattr(app,'fresh_product_state',fresh)
    monkeypatch.setattr(app,'parent_collection_type',parent)
    monkeypatch.setattr(app,'collection_channel_ids',channels)
    monkeypatch.setattr(app,'ensure_collection_channels',publish_channels)
    monkeypatch.setattr(app,'graphql',graphql)
    monkeypatch.setattr(app,'mutation',mutate)
    result=await app.publish_execute({'preview_id':'v','confirm':True})
    assert result['published_products']==2
    with app.connect() as db:
        assert db.execute("SELECT count(*) FROM generations WHERE status='published'").fetchone()[0]==2


def test_root_collection_cannot_be_reused_as_another_sectors_child():
    from core import match_collection_paths
    products = [{'desired': [['Giardinaggio','Ricambi','Carburatori']]}, {'desired': [['Ricambi','Ricambi motori','Valvole']]}]
    collections = [{'id':'root','title':'Ricambi','path':['Ricambi']}]
    matched = match_collection_paths(products,collections)
    assert matched[0]['path'] == ['Ricambi']
    result = build_dry_run([{'id':'p','desired':products[0]['desired'],'current':[]}],matched)
    assert ['Giardinaggio','Ricambi'] in [item['path'] for item in result['create_collections']]


def test_configured_root_is_reserved_when_only_other_sector_is_selected():
    from core import match_collection_paths
    products = [{'desired': [['Giardinaggio','Ricambi','Valvole']]}]
    collections = [{'id':'r','title':'Ricambi','path':['Ricambi']}]
    assert match_collection_paths(products, collections, ['Ricambi','Giardinaggio'])[0]['path']==['Ricambi']
