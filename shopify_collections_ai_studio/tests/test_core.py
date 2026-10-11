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
async def test_new_collection_is_created_with_initial_membership_and_path_metafield(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB", tmp_path / "create.sqlite")
    app.init_db()
    product_id = "gid://shopify/Product/1"
    source_hash = "expected"
    operations = {"create_collections": [{"path": ["Motore", "Carburatori"], "title": "Carburatori"}], "add_products": [{"collection_id": None, "source_id": None, "path": ["Motore", "Carburatori"], "product_ids": [product_id]}], "remove_products": [], "blocked": []}
    with app.connect() as db:
        db.execute("INSERT INTO products(id,title,content_hash,last_synced_at) VALUES(?,?,?,?)", (product_id, "P", "x", app.now()))
        db.execute("INSERT INTO product_snapshots VALUES(?,?,?,?,?,?,?)", ("s", product_id, "{}", "[]", source_hash, app.now(), "sync"))
        db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,approved_paths_json,status,source_hash,created_at) VALUES('g',?,1,'m','{}',?,'approved',?,?)", (product_id, json.dumps([["Motore", "Carburatori"]]), source_hash, app.now()))
        db.execute("INSERT INTO publish_previews VALUES(?,?,?,?,?,0)", ("preview", json.dumps([product_id]), json.dumps(operations), json.dumps({product_id: source_hash}), app.now()))

    mutations = []
    async def fresh(_):
        return [], source_hash
    async def fake_mutation(query, variables, field):
        mutations.append((field, variables))
        if field == "collectionCreate":
            return {"collection": {"id": "gid://shopify/Collection/new", "sources": [{"__typename": "CollectionConditionsSource", "id": "gid://shopify/CollectionConditionsSource/new"}]}}
        return {"metafields": [], "userErrors": []}
    async def no_sync(*_):
        return {}
    monkeypatch.setattr(app, "fresh_product_state", fresh)
    monkeypatch.setattr(app, "mutation", fake_mutation)
    monkeypatch.setattr(app, "sync_shopify", no_sync)
    result = await app.publish_execute({"preview_id": "preview", "confirm": True})
    assert result["created_collections"] == 1
    create = next(variables for field, variables in mutations if field == "collectionCreate")
    selection = create["collection"]["sources"][0]["source"]["inclusion"]["selections"]
    assert selection == [{"productId": product_id}]
    metafield = next(variables for field, variables in mutations if field == "metafieldsSet")
    assert metafield["metafields"][0]["value"] == "Motore > Carburatori"


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
