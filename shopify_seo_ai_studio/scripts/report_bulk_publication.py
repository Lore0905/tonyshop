"""Summarize verified bulk publication and technical exclusions."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

parser = argparse.ArgumentParser(); parser.add_argument('--run-dir', type=Path, required=True); args = parser.parse_args()
run = args.run_dir
report = json.loads((run / 'report.json').read_text())
excluded = json.loads((run / 'excluded.json').read_text())
details = []
with app.connect() as db:
    for row in excluded:
        product = db.execute('SELECT title,variants_json FROM products WHERE id=?', (row['id'],)).fetchone()
        source = db.execute('SELECT validation_json FROM generation_sources WHERE generation_id=?', (row['generation_id'],)).fetchone()
        details.append({**row, 'title': product['title'], 'skus': [v.get('sku') for v in app.jload(product['variants_json'], [])],
                        'issues': app.jload(source[0], {}).get('issues', []) if source else []})
(run / 'excluded_details.json').write_text(json.dumps(details, ensure_ascii=False, indent=2))
verified_at = datetime.fromisoformat(report['verified_at']).astimezone(ZoneInfo('Europe/Rome')).strftime('%d/%m/%Y %H:%M %Z')
lines = ['# Pubblicazione SEO Shopify in bulk', '', f'Verifica completata il {verified_at}.', '',
         f'- Proposte approvate: **{report["approved"]}**.', f'- Prodotti inviati in bulk: **{report["submitted"]}**.',
         f'- Pubblicazioni confermate con rilettura Shopify: **{report["verified_published"]}**.',
         f'- Fallimenti di verifica: **{len(report["failed"])}**.', f'- Prodotti esclusi: **{report["excluded"]}**.',
         f'- Stato operazione: **{report["mutation_status"]}**.', f'- ID operazione: `{report["bulk_operation"]}`.', '',
         '## Campi aggiornati e verifiche', '',
         'L’input di productUpdate contiene esclusivamente ID prodotto, title, descriptionHtml, seo.title e seo.description. Prezzi, SKU, varianti, inventario, stato, URL e canali non sono presenti negli input di modifica.', '',
         'Dopo il completamento è stata eseguita una seconda esportazione bulk da Shopify. I quattro campi sono stati confrontati con le proposte approvate; le ricevute sono associate ai prodotti tramite __lineNumber e ID, senza presumere che Shopify elabori le righe in ordine. Per il titolo SEO si considera anche il titolo prodotto predefinito quando l’API restituisce un override vuoto; il valore grezzo rimane nei backup e nel DB. Per l’HTML si normalizzano soltanto entità e spazi fra tag.', '',
         f'Osservazioni su differenze nei campi protetti fra le due letture: **{len(report["protected_field_observations"])}**. Eventuali variazioni osservate sono registrate in report.json; non sono campi scritti da questa operazione.', '',
         '## Correzioni prima dell’invio', '',
         'Sono state applicate come nuove generazioni le correzioni del campione di audit e i refusi dello stesso tipo trovati nel catalogo. Lo storico precedente è conservato. I conflitti su miscela, modelli e altre informazioni tecniche non sono stati risolti inventando: le proposte interessate sono rimaste bloccate. Dettagli in editorial_corrections.json e excluded_details.json.', '',
         '## Backup e recupero', '',
         '- before.sqlite: backup consistente dell’app prima delle correzioni e approvazioni di questo lotto.',
         '- before.jsonl: lettura Shopify precedente alla pubblicazione, incluse identità delle varianti e dati protetti.',
         '- manifest.json: prodotto, generazione, valori prima/dopo e pubblicazione locale.',
         '- variables.jsonl: input effettivo inviato al bulk.',
         '- mutation_results.jsonl: ricevute originali di Shopify.',
         '- after.jsonl: rilettura Shopify dopo il bulk.',
         '- rollback.jsonl: input per ripristinare esclusivamente i quattro campi editoriali ai valori del backup, da confrontare con lo stato corrente prima di un eventuale ripristino.', '',
         'Ripristinare il DB locale non annulla le modifiche già effettuate su Shopify. Nessun rollback è stato eseguito.', '',
         '## Prodotti esclusi', '', '| SKU | Prodotto | Motivo |', '| --- | --- | --- |']
for row in details:
    reason = '; '.join(row['issues']) or row['reason']
    esc = lambda value: str(value).replace('|', '\\|').replace('\n', ' ')
    lines.append(f'| {esc(", ".join(row["skus"]))} | {esc(row["title"])} | {esc(reason)} |')
lines += ['', '[Documentazione Shopify sulle importazioni bulk](https://shopify.dev/docs/apps/build/apis/graphql-admin/bulk-operations/imports).', '']
(run / 'REPORT_PUBBLICAZIONE.md').write_text('\n'.join(lines))
print(str(run / 'REPORT_PUBBLICAZIONE.md'))
