"""Apply audited editorial fixes as new drafts; preserve history and source gates."""
import argparse
import copy
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from content_safety import clean_html, factual_issues


def source_conflicts(source):
    text = app.technical_evidence({'data': source})
    issues = []
    if re.search(r'2[.,]5\s*%', text) and re.search(r'25\s*:\s*1', text):
        issues.append('Verifica tecnica: Autofantasy indica miscela 2.5% e 25:1, valori discordanti; confermare prima della pubblicazione.')
    titles = set(re.findall(r'\bLRS-\d+', source.get('title', ''), re.I))
    short = set(re.findall(r'(?<!L)\bRS-\d+', app.plain(source.get('shortDescriptionHtml', '')), re.I))
    if titles and short:
        issues.append('Verifica tecnica: modello LRS nel titolo e RS nella descrizione breve Autofantasy; confermare il modello effettivo.')
    if re.search(r'\bRS-240\b', source.get('title', ''), re.I) and re.search(r'prod=sp-240\b', source.get('descriptionHtml', ''), re.I):
        issues.append('Verifica tecnica: titolo RS-240 e collegamento del fornitore alla serie SP-240; confermare il modello effettivo.')
    return issues


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--run-dir', type=Path, required=True); args = parser.parse_args()
    run = args.run_dir
    if (run / 'editorial_corrections.json').exists():
        print('Editorial corrections already applied; history preserved.'); return
    if not (run / 'before.sqlite').exists(): raise RuntimeError('Create a consistent backup before editing drafts')
    amendments_path = app.DATA / 'codex_catalog_run/revision_v2/independent_review_20/prepublish_corrections.json'
    amendments = {r['id']: r for r in json.loads(amendments_path.read_text())}
    log = []
    with app.connect() as db:
        rows = db.execute('''SELECT p.*,g.id gid,g.output_json,g.status generation_status,g.prompt_version,s.data_json,s.validation_json
            FROM products p JOIN generations g ON g.id=(SELECT id FROM generations WHERE product_id=p.id ORDER BY created_at DESC,rowid DESC LIMIT 1)
            JOIN generation_sources s ON s.generation_id=g.id ORDER BY p.id''').fetchall()
        for row in rows:
            previous = app.jload(row['output_json'], {})
            out = copy.deepcopy(previous)
            evidence = app.jload(row['data_json'], {})
            validation = app.jload(row['validation_json'], {})
            source = evidence.get('data', {})
            issues = list(validation.get('issues', []))
            reason = []
            if row['id'] in amendments:
                amendment = amendments[row['id']]
                out = amendment['output']
                issues += amendment['blocking_issues']
                reason.append(amendment['reason'])
            conflicts = source_conflicts(source)
            issues += conflicts
            if conflicts: reason.append('Controllo dei conflitti della fonte esteso a tutto il catalogo.')
            for field in ('title', 'descriptionHtml', 'metaTitle', 'metaDescription'):
                text = out[field]
                text = text.replace('dglgli', 'di').replace(' a i ', ' ai ')
                text = re.sub(r'\bI adesivi\b', 'Gli adesivi', text)
                text = re.sub(r'per minicross per minicross', 'per minicross', text, flags=re.I)
                text = re.sub(r'compatibli', 'compatibili', text, flags=re.I)
                if re.search(r'\bmean well\b', app.technical_evidence(evidence), re.I):
                    text = re.sub(r'\bmean wel\b', 'Mean Well', text, flags=re.I)
                out[field] = text
            # These two supplies have consistent model/voltage/power in both
            # supplier title and short description. Rewrite the synonym pile.
            if source.get('reference') in {'LRS-35-12', 'LRS-50-24'}:
                model, voltage, power = ('LRS-35', '12V', '35W') if source['reference'] == 'LRS-35-12' else ('LRS-50', '24V', '50W')
                out['title'] = out['metaTitle'] = f'Alimentatore Mean Well {model} {voltage} {power} per LED'
                out['descriptionHtml'] = f'<p>Alimentatore Mean Well {model} per strisce LED e faretti, con potenza {power} e tensione {voltage}.</p><p>Riferimento: {source["reference"]}.</p>'
                out['metaDescription'] = f'Alimentatore Mean Well {model} per strisce LED e faretti: potenza {power}, tensione {voltage}. Riferimento {source["reference"]}.'
            out['descriptionHtml'] = clean_html(out['descriptionHtml'])
            if source.get('title'): issues += factual_issues(out, app.technical_evidence(evidence))
            issues = list(dict.fromkeys(issues))
            changed = [k for k in ('title', 'descriptionHtml', 'metaTitle', 'metaDescription') if previous[k] != out[k]]
            if not changed and set(issues) == set(validation.get('issues', [])): continue
            out = app.Proposal.model_validate(out).model_dump()
            out['criticita'] = list(dict.fromkeys(out['criticita'] + issues))
            out['voto'] = app.score_content(out, app.get_settings().weights, {'accuracy': 0 if issues else .75})['score']
            gid = str(app.uuid.uuid4()); status = 'blocked' if issues else 'generated'
            before = app.content_of(row); baseline_hash = app.fingerprint(before)
            db.execute('INSERT INTO shopify_snapshots VALUES(?,?,?,?,?,?)',
                       (str(app.uuid.uuid4()), row['id'], app.jdump(before), baseline_hash, app.now(), 'prepublish_review_base'))
            db.execute('''INSERT INTO generations(id,product_id,prompt_version,model,output_json,score,status,source_hash,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)''', (gid, row['id'], row['prompt_version'], 'Codex', app.jdump(out), out['voto'], status, baseline_hash, app.now()))
            validation.update({'issues': issues, 'engine': 'codex', 'run': 'codex_prepublish_review',
                               'human_review_required': True, 'reviewed_in_batch': True,
                               'method': 'Correzioni editoriali dell’audit e controllo dei conflitti prima della pubblicazione autorizzata.',
                               'editorial_review': {'reasons': ' '.join(reason) or 'Correzione di refusi e ripetizioni senza aggiungere fatti.'}})
            db.execute('INSERT INTO generation_sources VALUES(?,?,?,?)', (gid, evidence.get('source_hash', ''), app.jdump(evidence), app.jdump(validation)))
            db.execute("UPDATE products SET processing_status=?,last_error=NULL WHERE id=?", ('needs_review' if issues else 'to_approve', row['id']))
            log.append({'id': row['id'], 'old_generation': row['gid'], 'new_generation': gid, 'changed_fields': changed, 'issues': issues, 'status': status})
    path = run / 'editorial_corrections.json'; path.write_text(json.dumps(log, ensure_ascii=False, indent=2)); path.chmod(0o600)
    print(json.dumps({'new_reviewed_drafts': len(log), 'blocked_in_review': sum(r['status'] == 'blocked' for r in log),
                      'changed_ready': sum(r['status'] == 'generated' for r in log)}, ensure_ascii=False))


if __name__ == '__main__': main()
