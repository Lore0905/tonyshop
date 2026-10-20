"""Explicit, resumable publication of approved SEO fields through Shopify bulk operations.

Stages: snapshot, prepare, execute, verify. Never changes inventory, variants,
prices, status, handles, channels or supplier data on Shopify.
"""
import argparse
import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import httpx
from content_safety import clean_html

logging.getLogger('httpx').setLevel(logging.WARNING)
FIELDS = ['title', 'descriptionHtml', 'metaTitle', 'metaDescription']
EXPORT_QUERY = '''{products{edges{node{id title descriptionHtml status handle vendor productType updatedAt seo{title description} variants{edges{node{id sku price compareAtPrice inventoryQuantity inventoryItem{id}}}}}}}}'''
START_QUERY = '''mutation SeoBulkRead($query:String!){bulkOperationRunQuery(query:$query){bulkOperation{id}userErrors{field message}}}'''
STATUS = '''query SeoBulkStatus($id:ID!){node(id:$id){... on BulkOperation{id status errorCode objectCount url partialDataUrl}}}'''
STAGE = '''mutation SeoBulkStage($input:[StagedUploadInput!]!){stagedUploadsCreate(input:$input){stagedTargets{url parameters{name value}}userErrors{field message}}}'''
UPDATE = '''mutation PublishSeoBulk($product:ProductUpdateInput!){productUpdate(product:$product){product{id title updatedAt seo{title description}}userErrors{field message}}}'''
START_MUTATION = '''mutation SeoBulkWrite($mutation:String!,$path:String!,$client:String!){bulkOperationRunMutation(mutation:$mutation,stagedUploadPath:$path,clientIdentifier:$client){bulkOperation{id}userErrors{field message}}}'''


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def content(product):
    seo = product.get('seo') or {}
    return {'title': product['title'], 'descriptionHtml': product.get('descriptionHtml') or '',
            'metaTitle': seo.get('title') or '', 'metaDescription': seo.get('description') or ''}


def variables(pid, proposed):
    result = {'product': {'id': pid, 'title': proposed['title'], 'descriptionHtml': proposed['descriptionHtml'],
                          'seo': {'title': proposed['metaTitle'], 'description': proposed['metaDescription']}}}
    assert set(result['product']) == {'id', 'title', 'descriptionHtml', 'seo'}
    assert set(result['product']['seo']) == {'title', 'description'}
    return result


def html_key(value):
    # Shopify may serialize entities and inter-tag whitespace differently.
    return re.sub(r'>\s+<', '><', clean_html(value)).strip()


def matches(actual, expected):
    # An unset Shopify SEO title uses the product title. Keep the raw value in
    # snapshots, but compare the effective title rather than treating its
    # absence as an editorial conflict with an identical explicit override.
    actual = {**actual, 'metaTitle': actual['metaTitle'] or actual['title']}
    expected = {**expected, 'metaTitle': expected['metaTitle'] or expected['title']}
    return all(html_key(actual[k]) == html_key(expected[k]) if k == 'descriptionHtml' else actual[k] == expected[k]
               for k in FIELDS)


def roots(path):
    products = {}
    children = []
    for line in path.read_text().splitlines():
        if not line.strip(): continue
        row = json.loads(line)
        if row.get('__parentId'): children.append(row)
        elif row.get('id', '').startswith('gid://shopify/Product/'):
            row['variants_data'] = []
            products[row['id']] = row
    for row in children:
        if row.get('id', '').startswith('gid://shopify/ProductVariant/'):
            if row['__parentId'] not in products: raise RuntimeError('Variant without product in bulk export')
            products[row['__parentId']]['variants_data'].append(row)
    return products


async def operation_status(opid):
    result = (await app.graphql(STATUS, {'id': opid})).get('node')
    if not result: raise RuntimeError('Bulk operation not found: ' + opid)
    return result


async def finish(opid, label):
    deadline = time.monotonic() + 2400
    previous = None
    while time.monotonic() < deadline:
        status = await operation_status(opid)
        signal = (status['status'], status['objectCount'])
        if signal != previous:
            print(json.dumps({'phase': label, 'operation': opid, 'status': signal[0], 'objects': signal[1]}), flush=True)
            previous = signal
        if status['status'] in {'COMPLETED', 'FAILED', 'CANCELED', 'EXPIRED'}: return status
        await asyncio.sleep(8)
    raise RuntimeError('Bulk operation still running; resume using its saved ID, do not resubmit')


async def download(url, path):
    if not url or not url.startswith('https://'): raise RuntimeError('Missing secure bulk result URL')
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(response.content); temporary.chmod(0o600); temporary.replace(path)


async def export(run, label, state):
    path = run / (label + '.jsonl')
    if path.exists(): return roots(path)
    key = label + '_operation'
    if not state.get(key):
        result = (await app.graphql(START_QUERY, {'query': EXPORT_QUERY}))['bulkOperationRunQuery']
        if result['userErrors']: raise RuntimeError(str(result['userErrors']))
        state[key] = result['bulkOperation']['id']; save(run / 'state.json', state)
    status = await finish(state[key], label)
    if status['status'] != 'COMPLETED': raise RuntimeError(label + ' failed: ' + str(status.get('errorCode')))
    await download(status['url'], path)
    return roots(path)


def latest_rows():
    with app.connect() as db:
        return db.execute('''SELECT g.*,p.variants_json FROM products p JOIN generations g ON g.id=(
            SELECT id FROM generations WHERE product_id=p.id ORDER BY created_at DESC,rowid DESC LIMIT 1) ORDER BY p.id''').fetchall()


def source_before(g):
    with app.connect() as db:
        row = db.execute('SELECT content_json FROM shopify_snapshots WHERE product_id=? AND content_hash=? ORDER BY created_at DESC LIMIT 1',
                         (g['product_id'], g['source_hash'])).fetchone()
    if not row: raise RuntimeError('Missing generation base snapshot')
    return app.jload(row[0], {})


async def prepare(run, state):
    if (run / 'manifest.json').exists():
        print('Manifest already prepared; not changing approved inputs.'); return
    remote = roots(run / 'before.jsonl')
    manifest, skipped = [], []
    for g in latest_rows():
        pid = g['product_id']
        if g['status'] not in {'generated', 'approved'}:
            skipped.append({'id': pid, 'generation_id': g['id'], 'reason': g['status']}); continue
        try:
            app.validate_generation(g)
            fresh = remote.get(pid)
            if not fresh: raise RuntimeError('Product absent from current Shopify export')
            local_skus = sorted({str(v.get('sku') or '').strip() for v in app.jload(g['variants_json'], [])})
            remote_skus = sorted({str(v.get('sku') or '').strip() for v in fresh['variants_data']})
            if local_skus != remote_skus: raise RuntimeError('SKU changed on Shopify')
            proposed = {k: app.jload(g['output_json'], {})[k] for k in FIELDS}
            before = content(fresh)
            if not matches(before, source_before(g)) and not matches(before, proposed):
                raise RuntimeError('Shopify editorial content changed after generation')
            await app.approve(g['id'], {'fields': FIELDS})
            manifest.append({'id': pid, 'generation_id': g['id'], 'before': before, 'after': proposed,
                             'already_current': matches(before, proposed), 'output_hash': app.fingerprint(app.jload(g['output_json'], {}))})
        except Exception as exc:
            message = str(getattr(exc, 'detail', exc))
            skipped.append({'id': pid, 'generation_id': g['id'], 'reason': message})
    save(run / 'manifest.json', manifest); save(run / 'excluded.json', skipped)
    active = [r for r in manifest if not r['already_current']]
    for name, items, field in [('variables.jsonl', active, 'after'), ('rollback.jsonl', active, 'before')]:
        path = run / name
        path.write_text(''.join(json.dumps(variables(r['id'], r[field]), ensure_ascii=False) + '\n' for r in items))
        path.chmod(0o600)
    state['prepared_at'] = app.now(); state['products'] = len(active)
    state['input_hash'] = app.hashlib.sha256((run / 'variables.jsonl').read_bytes()).hexdigest()
    state['approved'] = len(manifest); state['excluded'] = len(skipped)
    save(run / 'state.json', state)
    print(json.dumps({'approved': len(manifest), 'to_upload': len(active), 'excluded': len(skipped)}), flush=True)


async def execute(run, state):
    manifest = load(run / 'manifest.json')
    active = [r for r in manifest if not r['already_current']]
    if not active: return
    if state.get('mutation_operation'):
        status = await finish(state['mutation_operation'], 'publish')
        url = status.get('url') or status.get('partialDataUrl')
        if url: await download(url, run / 'mutation_results.jsonl')
        state['mutation_status'] = status['status']; save(run / 'state.json', state); return
    if state.get('submission_attempted'): raise RuntimeError('Submission outcome unknown: inspect Shopify operations before resuming')
    assert app.hashlib.sha256((run / 'variables.jsonl').read_bytes()).hexdigest() == state['input_hash']
    for row in manifest:
        with app.connect() as db:
            g = db.execute('SELECT * FROM generations WHERE id=?', (row['generation_id'],)).fetchone()
            newest = db.execute('SELECT id FROM generations WHERE product_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (row['id'],)).fetchone()
        if newest[0] != g['id'] or g['status'] != 'approved' or app.fingerprint(app.jload(g['output_json'], {})) != row['output_hash']:
            raise RuntimeError('Approved generation changed before submission')
        app.validate_generation(g)
    # Record intent and rollback snapshot before any remote editorial write.
    with app.connect() as db:
        for row in manifest:
            if row.get('publication_id'):
                existing = db.execute('SELECT status FROM publications WHERE id=?', (row['publication_id'],)).fetchone()
                if existing and existing[0] == 'pending': continue
                raise RuntimeError('Existing publication is no longer pending; reconcile before retrying')
            publication = str(app.uuid.uuid4()); row['publication_id'] = publication
            db.execute('INSERT INTO shopify_snapshots VALUES(?,?,?,?,?,?)',
                       (str(app.uuid.uuid4()), row['id'], app.jdump(row['before']), app.fingerprint(row['before']), app.now(), 'before_bulk_publish'))
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?)',
                       (publication, row['id'], row['generation_id'], app.jdump(FIELDS), app.jdump(row['before']), app.jdump(row['after']), 'pending', None, app.now()))
    save(run / 'manifest.json', manifest)
    staged = (await app.graphql(STAGE, {'input': [{'resource': 'BULK_MUTATION_VARIABLES', 'filename': 'seo-' + run.name + '.jsonl',
                                                'mimeType': 'text/jsonl', 'httpMethod': 'POST'}]}))['stagedUploadsCreate']
    if staged['userErrors']: raise RuntimeError(str(staged['userErrors']))
    target = staged['stagedTargets'][0]
    if not target['url'].startswith('https://'): raise RuntimeError('Insecure upload target')
    parameters = {p['name']: p['value'] for p in target['parameters']}
    async with httpx.AsyncClient(timeout=240) as client:
        with (run / 'variables.jsonl').open('rb') as stream:
            response = await client.post(target['url'], data=parameters, files={'file': ('seo.jsonl', stream, 'text/jsonl')})
            response.raise_for_status()
    state['submission_attempted'] = True; state['submission_at'] = app.now(); save(run / 'state.json', state)
    result = (await app.graphql(START_MUTATION, {'mutation': UPDATE, 'path': parameters['key'], 'client': run.name}))['bulkOperationRunMutation']
    if result['userErrors']:
        state['submission_errors'] = result['userErrors']; save(run / 'state.json', state)
        raise RuntimeError(str(result['userErrors']))
    state['mutation_operation'] = result['bulkOperation']['id']; save(run / 'state.json', state)
    print(json.dumps({'submitted': len(active), 'operation': state['mutation_operation']}), flush=True)
    await execute(run, state)


def receipts(path, active):
    result = {}
    if not path.exists(): return result
    by_id = {r['id']: r for r in active}
    for line in path.read_text().splitlines():
        if not line.strip(): continue
        row = json.loads(line); index = row.get('__lineNumber')
        payload = (row.get('data') or {}).get('productUpdate') or {}
        pid = (payload.get('product') or {}).get('id')
        if index is not None:
            index = int(index)
            if not 0 <= index < len(active): raise RuntimeError('Bulk result line index outside manifest')
            expected_id = active[index]['id']
            if pid and pid != expected_id: raise RuntimeError('Bulk result product/line mismatch')
            pid = expected_id
        if pid not in by_id: raise RuntimeError('Unattributed bulk mutation result')
        if pid in result: raise RuntimeError('Duplicate product receipt')
        result[pid] = row
    return result


async def verify(run, state):
    manifest = load(run / 'manifest.json'); active = [r for r in manifest if not r['already_current']]
    remote = await export(run, 'after', state)
    response = receipts(run / 'mutation_results.jsonl', active)
    verified, failures, protected_changes = [], [], []
    before_remote = roots(run / 'before.jsonl')
    with app.connect() as db:
        for row in manifest:
            fresh = remote.get(row['id']); actual = content(fresh) if fresh else None
            receipt = response.get(row['id'], {})
            errors = ((receipt.get('data') or {}).get('productUpdate') or {}).get('userErrors', []) or receipt.get('errors', [])
            if not actual or not matches(actual, row['after']):
                reason = {'id': row['id'], 'errors': errors, 'message': 'Live content differs from approved proposal'}
                failures.append(reason)
                if row.get('publication_id'):
                    db.execute("UPDATE publications SET status='failed',error=? WHERE id=? AND status='pending'", (app.jdump(reason), row['publication_id']))
                db.execute("UPDATE products SET processing_status='error',last_error=? WHERE id=?", (reason['message'], row['id']))
                continue
            if not row.get('publication_id'):
                # An already-current product can be reconciled without sending a mutation.
                row['publication_id'] = str(app.uuid.uuid4())
                db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['publication_id'], row['id'], row['generation_id'], app.jdump(FIELDS), app.jdump(row['before']), app.jdump(actual), 'published', None, app.now()))
            else:
                db.execute("UPDATE publications SET status='published',after_json=?,error=NULL WHERE id=?", (app.jdump(actual), row['publication_id']))
            db.execute("UPDATE generations SET status='published',published_at=? WHERE id=?", (app.now(), row['generation_id']))
            newest = db.execute('SELECT id FROM generations WHERE product_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (row['id'],)).fetchone()
            processing = 'published' if newest[0] == row['generation_id'] else 'reevaluate'
            score = app.score_content(actual, app.get_settings().weights)
            db.execute('''UPDATE products SET title=?,description_html=?,meta_title=?,meta_description=?,content_hash=?,
                processing_status=?,last_error=NULL,shopify_updated_at=?,last_synced_at=?,current_score=?,score_details_json=? WHERE id=?''',
                       (actual['title'], actual['descriptionHtml'], actual['metaTitle'], actual['metaDescription'], app.fingerprint(actual),
                        processing, fresh['updatedAt'], app.now(), score['score'], app.jdump(score), row['id']))
            verified.append(row['id'])
            old = before_remote[row['id']]
            for key in ('status', 'handle', 'vendor', 'productType', 'variants_data'):
                left, right = old.get(key), fresh.get(key)
                if key == 'variants_data':
                    left = sorted(left, key=lambda v: v['id']); right = sorted(right, key=lambda v: v['id'])
                if left != right: protected_changes.append({'id': row['id'], 'field': key})
    report = {'verified_at': app.now(), 'bulk_operation': state.get('mutation_operation'), 'mutation_status': state.get('mutation_status'),
              'approved': len(manifest), 'submitted': len(active), 'already_current': len(manifest)-len(active), 'verified_published': len(verified),
              'failed': failures, 'excluded': len(load(run / 'excluded.json', [])), 'protected_field_observations': protected_changes,
              'written_fields': FIELDS, 'result_receipts': len(response)}
    save(run / 'manifest.json', manifest); save(run / 'report.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


async def main():
    parser = argparse.ArgumentParser(); parser.add_argument('phase', choices=['snapshot', 'prepare', 'execute', 'verify'])
    parser.add_argument('--run-dir', type=Path, required=True); args = parser.parse_args()
    run = args.run_dir.resolve(); run.mkdir(parents=True, exist_ok=True); run.chmod(0o700)
    state = load(run / 'state.json', {'started_at': app.now()})
    if args.phase == 'snapshot':
        if not (run / 'before.sqlite').exists():
            with app.connect() as db, sqlite3.connect(run / 'before.sqlite') as backup: db.backup(backup)
            (run / 'before.sqlite').chmod(0o600)
        result = await export(run, 'before', state)
        print(json.dumps({'snapshot_products': len(result)}), flush=True)
    elif args.phase == 'prepare': await prepare(run, state)
    elif args.phase == 'execute': await execute(run, state)
    else: await verify(run, state)


if __name__ == '__main__':
    try: asyncio.run(main())
    except httpx.HTTPError as exc:
        # Do not log signed upload/download URLs or headers.
        print(json.dumps({'error': type(exc).__name__, 'message': 'Network operation failed; inspect saved operation before resubmitting'}), flush=True)
        raise SystemExit(1)
