import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from bulk_publish_catalog import variables, receipts, matches
from prepare_publication_review import source_conflicts


def test_bulk_input_cannot_write_protected_fields():
    fields = {'title': 'Leva', 'descriptionHtml': '<p>Leva.</p>', 'metaTitle': 'Leva', 'metaDescription': 'Leva.',
              'price': '0.01', 'status': 'ACTIVE', 'variants': [{'sku': 'new'}], 'handle': 'new'}
    product = variables('gid://shopify/Product/1', fields)['product']
    assert set(product) == {'id', 'title', 'descriptionHtml', 'seo'}
    assert set(product['seo']) == {'title', 'description'}


def test_receipts_map_shuffled_success_and_failure_by_input_line(tmp_path):
    active = [{'id': 'p1'}, {'id': 'p2'}]
    rows = [{'__lineNumber': 1, 'data': {'productUpdate': {'product': None, 'userErrors': [{'message': 'Invalid'}]}}},
            {'__lineNumber': 0, 'data': {'productUpdate': {'product': {'id': 'p1'}, 'userErrors': []}}}]
    path = tmp_path / 'results.jsonl'; path.write_text('\n'.join(json.dumps(r) for r in rows))
    mapped = receipts(path, active)
    assert mapped['p2']['data']['productUpdate']['userErrors']
    assert mapped['p1']['__lineNumber'] == 0
    rows[1]['data']['productUpdate']['product']['id'] = 'p2'
    path.write_text('\n'.join(json.dumps(r) for r in rows))
    with pytest.raises(RuntimeError, match='mismatch'): receipts(path, active)


def test_live_verification_does_not_ignore_changed_measurements():
    a = {'title': 'Leva', 'descriptionHtml': '<p>Diametro 22mm.</p>', 'metaTitle': 'Leva', 'metaDescription': 'Leva.'}
    assert matches(a, dict(a))
    assert not matches(a, {**a, 'descriptionHtml': '<p>Diametro 24mm.</p>'})
    assert matches(a, {**a, 'metaTitle': ''})
    assert not matches({**a, 'metaTitle': ''}, {**a, 'metaTitle': 'Titolo SEO differente'})


def test_conflicting_source_is_not_automatically_publishable():
    assert source_conflicts({'descriptionHtml': '<p>Miscela 2.5% 25:1</p>'})
    assert source_conflicts({'title': 'Alimentatore LRS-75 12V', 'shortDescriptionHtml': '<p>Alimentatore RS-75</p>'})
    assert not source_conflicts({'title': 'Alimentatore LRS-75 12V', 'shortDescriptionHtml': '<p>Alimentatore LRS-75</p>'})
