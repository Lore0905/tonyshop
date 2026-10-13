
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


def _collection_priority(item: dict[str, Any]) -> tuple[int, int, str, str]:
    """Prefer a collection that can be updated safely when duplicate paths exist."""
    kind = item.get("kind")
    source_id = item.get("source_id")
    usable_manual = kind == "manual" and bool(source_id)
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
    for item in sorted(active, key=_collection_priority):
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

        # If a desired subcategory has an existing manual parent collection, assign
        # the product to the parent too. This keeps parent collection pages complete.
        desired_paths = list(desired.values())
        for ancestor_key, collection in by_path.items():
            ancestor = normalize_levels(collection["path"])
            if ancestor_key in desired:
                continue
            if not any(len(ancestor) < len(child) and _is_path_prefix(ancestor, child) for child in desired_paths):
                continue
            already_in_parent = any(item["id"] == collection["id"] for item in current_items)
            if collection.get("kind") == "manual" and collection.get("source_id"):
                desired[ancestor_key] = ancestor
            elif already_in_parent:
                # A smart parent already contains this product; preserve its membership.
                continue
            elif collection.get("kind") == "smart":
                block(product_id, ancestor, "Collection padre smart: il prodotto non risulta incluso dalle regole Shopify")
            else:
                block(product_id, ancestor, "Collection padre non aggiornabile: sorgente manuale Shopify non disponibile")

        for key, levels in desired.items():
            collection = by_path.get(key)
            if collection and collection.get("kind") == "smart":
                if not any(item["id"] == collection["id"] for item in current_items):
                    block(product_id, levels, "Collection smart in sola lettura")
                continue
            if collection and not collection.get("source_id"):
                block(product_id, levels, "Sorgente manuale Shopify non disponibile")
                continue
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
