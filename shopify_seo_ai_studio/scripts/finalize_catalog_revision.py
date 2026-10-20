"""Canonicalize frozen HTML and remove unsupported proposals for missing imports."""
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from content_safety import clean_html
from import_codex_proposals import RUN, source_index

sources = source_index()
changed = missing = 0
for file in sorted((RUN / 'revision_v2/proposals').glob('batch_*.json')):
    rows = json.loads(file.read_text())
    before = json.dumps(rows, ensure_ascii=False)
    for row in rows:
        source = sources[row['id']]
        out = row['output']
        if not source['supplier'].get('title', '').strip():
            missing += 1
            # Shopify's existing copy is not factual evidence. Retain only the
            # identifying title and SKU while the proposal remains blocked.
            refs = ', '.join(v['sku'] for v in source.get('variants', []) if v.get('sku'))
            out['title'] = source['current']['title']
            out['descriptionHtml'] = '<p>Riferimento articolo: ' + html.escape(refs or row['id']) + '.</p>'
            out['metaTitle'] = out['metaDescription'] = out['marca'] = ''
            out['keywords'] = []
            reason = 'Import Autofantasy assente: nessuna proposta SEO utilizzabile; titolo Shopify conservato soltanto per identificare la scheda.'
            row['source_limitations'] = [reason]
            row['blocking_issues'] = list(dict.fromkeys(row.get('blocking_issues', []) + [reason]))
            out['criticita'] = list(dict.fromkeys(out.get('criticita', []) + [reason]))
            row['editorial_review'] = {'reasons': reason, 'source_policy': 'Nessuna informazione tecnica dedotta dal testo Shopify preesistente.'}
        out['descriptionHtml'] = clean_html(out['descriptionHtml'])
    if json.dumps(rows, ensure_ascii=False) != before:
        changed += 1
        file.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
print(json.dumps({'files_canonicalized': changed, 'missing_sources_blocked': missing}))
