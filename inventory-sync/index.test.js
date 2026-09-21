const test = require('node:test');
const assert = require('node:assert/strict');
const { quantity, sourceMap, plan, inventoryInput } = require('./index');

test('associa stock prodotto e combinazione agli SKU e conserva zero', () => {
  const source = sourceMap(
    [{ id: 10, reference: 'SIMPLE' }],
    [{ id: 20, id_product: 10, reference: 'VARIANT' }],
    [
      { id_product: 10, id_product_attribute: 0, quantity: 0 },
      { id_product: 10, id_product_attribute: 20, quantity: 5 },
    ],
  );
  assert.equal(source.has('SIMPLE'), false);
  assert.equal(source.get('VARIANT'), 5);
});

test('ignora il prodotto padre che condivide SKU con la combinazione', () => {
  const source = sourceMap(
    [{ id: 271, reference: 'M271' }],
    [{ id: 230, id_product: 271, reference: 'M271' }],
    [{ id_product: 271, id_product_attribute: 0, quantity: 0 }, { id_product: 271, id_product_attribute: 230, quantity: 12 }],
  );
  assert.equal(source.size, 1);
  assert.equal(source.get('M271'), 12);
});

test('prepara solo le differenze nella sede, incluso azzeramento', () => {
  const result = plan(new Map([['A', 0], ['B', 5], ['C', 3], ['D', 2]]), new Map([
    ['A', { id: 'item-a', tracked: true, available: 2 }],
    ['B', { id: 'item-b', tracked: true, available: 5 }],
    ['C', { id: 'item-c', tracked: false, available: 1 }],
  ]));
  assert.deepEqual(result.updates, [{ sku: 'A', inventoryItemId: 'item-a', quantity: 0, changeFromQuantity: 2 }]);
  assert.equal(result.stats.missing, 1);
  assert.equal(result.stats.skipped, 1);
  assert.equal(result.stats.unchanged, 1);
});

test('genera il payload Shopify 2026-07 con changeFromQuantity', () => {
  const input = inventoryInput([{ inventoryItemId: 'item-a', quantity: 0, changeFromQuantity: 2 }], 'location-a');
  assert.deepEqual(input, {
    name: 'available', reason: 'correction',
    quantities: [{ inventoryItemId: 'item-a', locationId: 'location-a', quantity: 0, changeFromQuantity: 2 }],
  });
  assert.equal('compareQuantity' in input.quantities[0], false);
});

test('blocca quantità non valide ed esclude SKU duplicati', () => {
  assert.throws(() => quantity('n/a', 'SKU A'), /Quantità non valida/);
  const source = sourceMap(
    [{ id: 1, reference: 'A' }, { id: 2, reference: 'A' }, { id: 3, reference: 'B' }], [],
    [{ id_product: 1, id_product_attribute: 0, quantity: 1 }, { id_product: 2, id_product_attribute: 0, quantity: 2 }, { id_product: 3, id_product_attribute: 0, quantity: 3 }],
  );
  assert.equal(source.has('A'), false);
  assert.equal(source.get('B'), 3);
  assert.equal(source.ambiguous.has('A'), true);
});
