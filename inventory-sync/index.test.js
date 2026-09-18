const test = require('node:test');
const assert = require('node:assert/strict');
const { quantity, sourceMap, plan } = require('./index');

test('associa stock prodotto e combinazione agli SKU e conserva zero', () => {
  const source = sourceMap(
    [{ id: 10, reference: 'SIMPLE' }],
    [{ id: 20, reference: 'VARIANT' }],
    [
      { id_product: 10, id_product_attribute: 0, quantity: 0 },
      { id_product: 10, id_product_attribute: 20, quantity: 5 },
    ],
  );
  assert.equal(source.get('SIMPLE'), 0);
  assert.equal(source.get('VARIANT'), 5);
});

test('prepara solo le differenze nella sede, incluso azzeramento', () => {
  const result = plan(new Map([['A', 0], ['B', 5], ['C', 3], ['D', 2]]), new Map([
    ['A', { id: 'item-a', tracked: true, available: 2 }],
    ['B', { id: 'item-b', tracked: true, available: 5 }],
    ['C', { id: 'item-c', tracked: false, available: 1 }],
  ]));
  assert.deepEqual(result.updates, [{ sku: 'A', inventoryItemId: 'item-a', quantity: 0, compareQuantity: 2 }]);
  assert.equal(result.stats.missing, 1);
  assert.equal(result.stats.skipped, 1);
  assert.equal(result.stats.unchanged, 1);
});

test('blocca quantità non valide e SKU duplicati', () => {
  assert.throws(() => quantity('n/a', 'SKU A'), /Quantità non valida/);
  assert.throws(() => sourceMap(
    [{ id: 1, reference: 'A' }, { id: 2, reference: 'A' }], [],
    [{ id_product: 1, id_product_attribute: 0, quantity: 1 }, { id_product: 2, id_product_attribute: 0, quantity: 2 }],
  ), /SKU duplicato/);
});
