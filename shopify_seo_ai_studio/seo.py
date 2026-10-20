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
    # Reusing the product name across H1, body and metadata is normal.
    stuffing = any(repeated_words(field) for field in (title, desc, meta_title, meta_desc))
    checks = {
        "title": bool(title) * (1 if 20 <= len(title) <= 70 else .55 if title else 0),
        "description": bool(desc) * (1 if len(desc) >= 80 and "<" in (content.get("descriptionHtml") or "") else .8 if len(desc) >= 80 else .5),
        "metaTitle": bool(meta_title) * (1 if 30 <= len(meta_title) <= 60 else .55 if meta_title else 0),
        "metaDescription": bool(meta_desc) * (1 if 110 <= len(meta_desc) <= 160 else .55 if meta_desc else 0),
        "keywords": .35 if stuffing else float((ai or {}).get("keywords", .75)),
        "accuracy": float((ai or {}).get("accuracy", .75)),
    }
    # Length is guidance, not sufficient proof of useful copy.
    normalize=lambda value:re.sub(r"[^\w]+"," ",plain(value).casefold()).strip()
    repeated_snippet=bool(meta_desc) and normalize(meta_desc)==normalize(title)
    core_desc=re.sub(r"(?:riferimento|codice articolo)\s*:?\s*[\w/-]+[.]?", "", desc, flags=re.I).strip()
    title_only_description=bool(title) and normalize(core_desc)==normalize(title)
    if repeated_snippet:checks["metaDescription"]=min(checks["metaDescription"],.3)
    if title_only_description:checks["description"]=min(checks["description"],.3)
    score = round(sum(w[key] * max(0, min(1, checks[key])) for key in w))
    issues = []
    if repeated_snippet:issues.append("La meta description ripete soltanto il titolo: valuta una frase utile con i dettagli documentati")
    if title_only_description:issues.append("Descrizione limitata a titolo e riferimento: informazioni tecniche utili da integrare se documentate")
    if not title: issues.append("Titolo assente")
    elif not 20 <= len(title) <= 70: issues.append("Lunghezza titolo non ottimale")
    if len(desc) < 80: issues.append("Descrizione breve: verifica che contenga dati utili, senza allungarla artificialmente")
    if re.search(r"<h1\b", content.get("descriptionHtml", ""), re.I): issues.append("H1 nella descrizione: usa H2/H3 per evitare duplicazioni con il titolo del tema")
    if re.search(r"prodotto di qualit[aà]|miglior prezzo|imperdibile", meta_desc, re.I): issues.append("Meta description generica: evidenzia caratteristiche documentate del prodotto")
    if not 30 <= len(meta_title) <= 60: issues.append("Meta title assente o di lunghezza non ottimale")
    if not 110 <= len(meta_desc) <= 160: issues.append("Meta description assente o di lunghezza non ottimale")
    if stuffing: issues.append("Possibile keyword stuffing")
    return {"score": max(1, min(100, score)), "issues": issues, "checks": checks}

