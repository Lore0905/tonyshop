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


def build_dry_run(products: list[dict[str, Any]], collections: list[dict[str, Any]]) -> dict[str, Any]:
    by_path = {path_key(item["path"]): item for item in collections if not item.get("removed")}
    create: dict[str, dict[str, Any]] = {}
    add: dict[str, dict[str, Any]] = {}
    remove: dict[str, dict[str, Any]] = {}
    blocked: list[dict[str, str]] = []
    for product in products:
        current = {path_key(item["path"]): item for item in product.get("current", [])}
        desired = {path_key(path): path for path in product.get("desired", [])}
        for key, levels in desired.items():
            collection = by_path.get(key)
            if collection and collection.get("kind") == "smart":
                if key not in current:
                    blocked.append({"product_id": product["id"], "path": path_label(levels), "reason": "Collection smart in sola lettura"})
                continue
            if collection and not collection.get("source_id"):
                blocked.append({"product_id": product["id"], "path": path_label(levels), "reason": "Sorgente manuale Shopify non disponibile"})
                continue
            if not collection:
                create[key] = {"path": levels, "title": levels[-1]}
                collection_key = f"new:{key}"
            else:
                collection_key = collection["id"]
            if key not in current:
                entry = add.setdefault(collection_key, {"collection_id": collection.get("id") if collection else None, "source_id": collection.get("source_id") if collection else None, "path": levels, "product_ids": []})
                entry["product_ids"].append(product["id"])
        for key, item in current.items():
            if key in desired or item.get("kind") != "manual":
                continue
            if not item.get("source_id"):
                blocked.append({"product_id": product["id"], "path": path_label(item["path"]), "reason": "Sorgente manuale Shopify non disponibile"})
                continue
            entry = remove.setdefault(item["id"], {"collection_id": item["id"], "source_id": item["source_id"], "path": item["path"], "product_ids": []})
            entry["product_ids"].append(product["id"])
    return {
        "create_collections": list(create.values()),
        "add_products": list(add.values()),
        "remove_products": list(remove.values()),
        "blocked": blocked,
    }


def empty_manual_collections(collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in collections if item.get("kind") == "manual" and not item.get("removed") and int(item.get("products_count", 0)) == 0]
