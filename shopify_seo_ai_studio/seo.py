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

