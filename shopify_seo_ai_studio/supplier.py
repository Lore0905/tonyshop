"""Read-only Autofantasy bulk import. Never match products by similar titles."""
import asyncio
import os
import httpx

FIELDS = ['id', 'reference', 'name', 'description', 'description_short', 'meta_title', 'meta_description', 'date_upd']


def localized(value):
    language = os.getenv('AUTOFANTASY_LANGUAGE_ID', '')
    if isinstance(value, list):
        return next((str(v.get('value') or '') for v in value if str(v.get('id')) == language), '')
    if isinstance(value, dict):
        return localized(value.get('language', []))
    return str(value or '')


async def fetch_catalog():
    key = os.getenv('AUTOFANTASY_WEBSERVICE_KEY', '').strip()
    if not key:
        raise RuntimeError('AUTOFANTASY_WEBSERVICE_KEY mancante: configura la chiave del fornitore sul server.')
    result = {}
    async with httpx.AsyncClient(auth=(key, ''), timeout=90) as client:
        for resource, fields in [('products', FIELDS), ('combinations', ['id', 'id_product', 'reference'])]:
            rows = []
            for offset in range(0, 100000, 100):
                for attempt in range(3):
                    try:
                        r = await client.get(f'https://autofantasy.it/api/{resource}', params={
                            'output_format': 'JSON', 'display': '[' + ','.join(fields) + ']',
                            **({'language': os.environ['AUTOFANTASY_LANGUAGE_ID']} if os.getenv('AUTOFANTASY_LANGUAGE_ID') else {}), 'sort': '[id_ASC]', 'limit': f'{offset},100'})
                        if r.status_code == 429 or r.status_code >= 500:
                            if attempt < 2:
                                await asyncio.sleep(2 ** attempt)
                                continue
                        if r.is_error:
                            raise RuntimeError(f'Autofantasy {resource}: HTTP {r.status_code}. Verifica i permessi di lettura della chiave.')
                        batch = r.json().get(resource, [])
                        if not isinstance(batch, list):
                            raise RuntimeError(f'Risposta Autofantasy non valida: {resource}')
                        break
                    except httpx.TransportError:
                        if attempt == 2:
                            raise RuntimeError('Autofantasy non raggiungibile; import precedente conservato.') from None
                        await asyncio.sleep(2 ** attempt)
                rows.extend(batch)
                if len(batch) < 100:
                    break
            else:
                raise RuntimeError('Catalogo Autofantasy oltre il limite di sicurezza; import interrotto.')
            result[resource] = rows
    return result


def match_catalog(catalog, variants):
    index = {}
    products = {str(p['id']): p for p in catalog['products']}
    for p in catalog['products']:
        ref = str(p.get('reference') or '').strip()
        if ref:
            index.setdefault(ref, set()).add(str(p['id']))
    for v in catalog['combinations']:
        ref = str(v.get('reference') or '').strip()
        if ref:
            index.setdefault(ref, set()).add(str(v['id_product']))
    skus = [str(v.get('sku') or '').strip() for v in variants]
    if not skus or any(not sku for sku in skus):
        return {'status': 'missing', 'reason': 'SKU assente in una o più varianti.'}
    matches = [index.get(sku, set()) for sku in skus]
    if any(len(m) > 1 for m in matches) or len(set.union(set(), *matches)) > 1:
        return {'status': 'ambiguous', 'reason': 'Gli SKU corrispondono a più prodotti del fornitore.'}
    if any(len(m) != 1 for m in matches):
        return {'status': 'missing', 'reason': 'Non tutti gli SKU hanno una corrispondenza esatta Autofantasy.'}
    pid = next(iter(matches[0])); p = products.get(pid)
    if not p:
        return {'status': 'missing', 'reason': 'Prodotto padre del fornitore non disponibile.'}
    data = {'supplier_id': pid, 'matched_skus': sorted(set(skus)), 'reference': str(p.get('reference') or ''),
            'title': localized(p.get('name')), 'descriptionHtml': localized(p.get('description')),
            'shortDescriptionHtml': localized(p.get('description_short')), 'metaTitle': localized(p.get('meta_title')),
            'metaDescription': localized(p.get('meta_description')), 'supplier_updated_at': p.get('date_upd', ''),
            'source_url': f'https://autofantasy.it/index.php?controller=product&id_product={pid}'}
    complete = bool(data['title'].strip() and (data['descriptionHtml'].strip() or data['shortDescriptionHtml'].strip()))
    return {'status': 'matched' if complete else 'incomplete', 'reason': '' if complete else 'Titolo o descrizione assente nella lingua della fonte: verifica AUTOFANTASY_LANGUAGE_ID se i dati sono multilingua.', 'data': data}
