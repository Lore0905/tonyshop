"""Verify saved drafts, approval gates, filters and preservation of the original data."""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from fastapi.testclient import TestClient
from import_codex_proposals import RUN

REV = RUN / 'revision_v2'
before = sqlite3.connect(f'file:{REV / "before.sqlite"}?mode=ro', uri=True)
before.row_factory = sqlite3.Row
with app.connect() as db:
    original = {r['id']: dict(r) for r in before.execute('SELECT * FROM products')}
    current = {r['id']: dict(r) for r in db.execute('SELECT * FROM products')}
    assert original.keys() == current.keys(), 'Catalog membership changed'
    changes = []
    for pid, old in original.items():
        fields = [k for k in old if k not in ('processing_status', 'last_error') and old[k] != current[pid][k]]
        if fields: changes.append({'id': pid, 'fields': fields})
    assert not changes, changes
    for table in ('publications', 'supplier_imports', 'jobs'):
        key = 'product_id' if table == 'supplier_imports' else 'id'
        old = {r[key]: dict(r) for r in before.execute('SELECT * FROM ' + table)}
        new = {r[key]: dict(r) for r in db.execute('SELECT * FROM ' + table)}
        assert old == new, f'{table} changed'
    latest = db.execute('''SELECT g.* FROM products p JOIN generations g ON g.id=(
        SELECT id FROM generations WHERE product_id=p.id ORDER BY created_at DESC,rowid DESC LIMIT 1)''').fetchall()
    publications = db.execute('SELECT count(*) FROM publications').fetchone()[0]
    run_approvals = db.execute('''SELECT count(*) FROM approvals a JOIN generation_sources s ON s.generation_id=a.generation_id
        WHERE json_extract(s.validation_json,'$.run')='codex_catalog_revision_v2' ''').fetchone()[0]
assert run_approvals == 0
statuses = Counter(g['status'] for g in latest)
failures = []
validated = 0
for generation in latest:
    if generation['status'] == 'generated':
        try: app.validate_generation(generation)
        except Exception as exc: failures.append({'id': generation['product_id'], 'detail': str(exc)})
        else: validated += 1
assert not failures, failures
expected = {r['id']: r for f in (REV / 'proposals').glob('batch_*.json') for r in json.loads(f.read_text())}
for g in latest:
    row = expected[g['product_id']]
    assert g['status'] == ('blocked' if row.get('blocking_issues') else 'generated')
    actual = app.jload(g['output_json'], {})
    assert all(actual[k] == row['output'][k] for k in ('title', 'descriptionHtml', 'metaTitle', 'metaDescription', 'keywords'))
client = TestClient(app.app)
filter_checks = {}
for state, count in (('to_approve', validated), ('needs_review', statuses['blocked']), ('queued', 0), ('to_analyze', 0)):
    response = client.get('/api/products', params={'processing': state, 'limit': 1})
    assert response.status_code == 200
    actual = response.json()['total']
    assert actual == count, (state, actual, count)
    filter_checks[state] = actual
report = {'verified_at': app.now(), 'products': len(current), 'latest_generation_status': dict(statuses),
          'validated_ready_drafts': validated, 'validation_failures': failures, 'product_field_changes': changes,
          'publications_before_and_after': publications, 'new_publications': 0, 'new_approvals': run_approvals,
          'supplier_imports_preserved': True, 'jobs_preserved': True, 'filter_checks': filter_checks}
(REV / 'saved_verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report, ensure_ascii=False))
