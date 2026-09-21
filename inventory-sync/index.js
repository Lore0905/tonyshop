const axios = require('axios');
const crypto = require('node:crypto');
const path = require('node:path');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

const API_VERSION = '2026-07';
const PAGE_SIZE = 100;
const MUTATION_SIZE = 100;
const PRESTA_SHOP_ID = process.env.AUTOFANTASY_SHOP_ID?.trim() || '1';
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function required(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Configurazione mancante: ${name}`);
  return value;
}

function locationId() {
  const value = required('DEFAULT_LOCATION_ID');
  return value.startsWith('gid://shopify/Location/') ? value : `gid://shopify/Location/${value}`;
}

function quantity(value, context) {
  const text = String(value ?? '').trim();
  if (!/^-?\d+$/.test(text) || !Number.isSafeInteger(Number(text))) {
    throw new Error(`Quantità non valida: ${context}`);
  }
  return Math.max(0, Number(text));
}

async function withRetry(request, label) {
  for (let attempt = 0; ; attempt++) {
    try { return await request(); }
    catch (error) {
      const status = error.response?.status;
      const retryable = status === 429 || status >= 500 || ['ECONNRESET', 'ETIMEDOUT', 'ECONNABORTED'].includes(error.code);
      if (attempt >= 2 || !retryable) {
        throw new Error(`${label}: ${error.message}`, { cause: error });
      }
      console.warn(`${label}: ${error.message}; nuovo tentativo ${attempt + 2}/3`);
      const retryAfter = Number(error.response?.headers?.['retry-after']);
      await pause(Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter * 1000 : 1000 * 2 ** attempt);
    }
  }
}

async function prestaList(resource, fields) {
  const key = required('AUTOFANTASY_WEBSERVICE_KEY');
  const result = [];
  for (let offset = 0; ; offset += PAGE_SIZE) {
    const label = `PrestaShop ${resource} offset ${offset}`;
    console.log(`Lettura ${label}`);
    const params = { output_format: 'JSON', display: `[${fields.join(',')}]`, limit: `${offset},${PAGE_SIZE}` };
    if (resource === 'stock_availables') {
      params['filter[id_shop]'] = `[${PRESTA_SHOP_ID}]`;
    }
    const response = await withRetry(() => axios.get(`https://autofantasy.it/api/${resource}`, {
      auth: { username: key, password: '' }, params, timeout: 60000,
      headers: { Accept: 'application/json' },
    }), label);
    const rows = response.data?.[resource];
    if (!Array.isArray(rows)) throw new Error(`Risposta PrestaShop non valida: ${resource}`);
    result.push(...rows);
    if (rows.length < PAGE_SIZE) break;
  }
  console.log(`PrestaShop ${resource}: ${result.length} righe`);
  return result;
}

function sourceMap(products, combinations, stocks) {
  const refs = new Map();
  const productsWithCombinations = new Set(combinations.map((combination) => String(combination.id_product)));
  for (const product of products) refs.set(`p:${product.id}`, String(product.reference ?? '').trim());
  for (const combination of combinations) refs.set(`c:${combination.id}`, String(combination.reference ?? '').trim());
  const result = new Map();
  const ambiguous = new Set();
  for (const stock of stocks) {
    const attributeId = Number(stock.id_product_attribute);
    if (!attributeId && productsWithCombinations.has(String(stock.id_product))) continue;
    const sku = refs.get(attributeId ? `c:${attributeId}` : `p:${stock.id_product}`);
    if (!sku) continue;
    if (ambiguous.has(sku)) continue;
    if (result.has(sku)) {
      result.delete(sku);
      ambiguous.add(sku);
      continue;
    }
    result.set(sku, quantity(stock.quantity, `SKU ${sku}`));
  }
  if (!result.size) throw new Error('Nessuna giacenza con SKU restituita da PrestaShop');
  result.ambiguous = ambiguous;
  return result;
}

async function shopifyClient() {
  const domain = required('SHOPIFY_SHOP_DOMAIN').replace(/^https?:\/\//, '').replace(/\/$/, '');
  if (!/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(domain)) throw new Error('SHOPIFY_SHOP_DOMAIN non valido');
  console.log('Autenticazione Shopify');
  const response = await withRetry(() => axios.post(`https://${domain}/admin/oauth/access_token`, new URLSearchParams({
    grant_type: 'client_credentials', client_id: required('SHOPIFY_CLIENT_ID'), client_secret: required('SHOPIFY_CLIENT_SECRET'),
  }).toString(), { headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, timeout: 30000 }), 'Autenticazione Shopify');
  const token = response.data?.access_token;
  if (!token) throw new Error('Shopify non ha restituito un access token');
  return async (query, variables) => {
    const label = query.includes('productVariants') ? 'Lettura varianti Shopify' : 'Aggiornamento inventario Shopify';
    const reply = await withRetry(() => axios.post(`https://${domain}/admin/api/${API_VERSION}/graphql.json`,
      { query, variables }, { headers: { 'X-Shopify-Access-Token': token }, timeout: 30000 }), label);
    if (reply.data?.errors?.length) throw new Error(`Shopify GraphQL: ${reply.data.errors.map((x) => x.message).join('; ')}`);
    if (!reply.data?.data) throw new Error('Risposta Shopify senza data');
    return reply.data.data;
  };
}

async function shopifyMap(graphql, selectedLocation) {
  const query = `query Variants($after: String, $location: ID!) {
    productVariants(first: 100, after: $after) {
      nodes { sku inventoryItem { id tracked inventoryLevel(locationId: $location) { quantities(names: ["available"]) { quantity } } } }
      pageInfo { hasNextPage endCursor }
    }
  }`;
  const result = new Map();
  const ambiguous = new Set();
  let after = null;
  let pages = 0;
  do {
    console.log(`Lettura varianti Shopify, pagina ${pages + 1}`);
    const page = (await graphql(query, { after, location: selectedLocation })).productVariants;
    pages++;
    if (!page?.nodes || !page.pageInfo) throw new Error('Pagina varianti Shopify non valida');
    for (const variant of page.nodes) {
      const sku = String(variant.sku ?? '').trim();
      if (!sku) continue;
      if (ambiguous.has(sku)) continue;
      if (result.has(sku)) {
        result.delete(sku);
        ambiguous.add(sku);
        continue;
      }
      const item = variant.inventoryItem;
      result.set(sku, {
        id: item?.id, tracked: item?.tracked,
        available: item?.inventoryLevel?.quantities?.find((q) => q.quantity != null)?.quantity ?? null,
      });
    }
    if (page.pageInfo.hasNextPage && (!page.pageInfo.endCursor || page.pageInfo.endCursor === after)) throw new Error('Paginazione Shopify bloccata');
    after = page.pageInfo.hasNextPage ? page.pageInfo.endCursor : null;
  } while (after);
  result.ambiguous = ambiguous;
  console.log(`Shopify: ${result.size} SKU su ${pages} pagine`);
  return result;
}

function plan(source, destination) {
  const updates = [];
  const stats = { source: source.size, shopify: destination.size, matched: 0, unchanged: 0, missing: 0, skipped: 0, zero: 0,
    ambiguousSource: source.ambiguous?.size ?? 0, ambiguousShopify: destination.ambiguous?.size ?? 0 };
  for (const [sku, desired] of source) {
    const item = destination.get(sku);
    if (!item) { if (!destination.ambiguous?.has(sku)) stats.missing++; continue; }
    stats.matched++;
    if (!item.tracked || item.available === null || !item.id) { stats.skipped++; continue; }
    if (desired === item.available) { stats.unchanged++; continue; }
    if (desired === 0) stats.zero++;
    updates.push({ sku, inventoryItemId: item.id, quantity: desired, changeFromQuantity: item.available });
  }
  if (!stats.matched) throw new Error('Nessuno SKU della fonte corrisponde a Shopify');
  return { updates, stats };
}

function inventoryInput(updates, selectedLocation) {
  return {
    name: 'available',
    reason: 'correction',
    quantities: updates.map(({ inventoryItemId, quantity, changeFromQuantity }) => ({
      inventoryItemId, locationId: selectedLocation, quantity, changeFromQuantity,
    })),
  };
}

async function apply(graphql, updates, selectedLocation) {
  const mutation = `mutation SetInventory($input: InventorySetQuantitiesInput!, $key: String!) {
    inventorySetQuantities(input: $input) @idempotent(key: $key) {
      inventoryAdjustmentGroup { id }
      userErrors { field message code }
    }
  }`;
  let updated = 0;
  for (let i = 0; i < updates.length; i += MUTATION_SIZE) {
    const chunk = updates.slice(i, i + MUTATION_SIZE);
    const batch = Math.floor(i / MUTATION_SIZE) + 1;
    const totalBatches = Math.ceil(updates.length / MUTATION_SIZE);
    console.log(`Aggiornamento Shopify: lotto ${batch}/${totalBatches} (${chunk.length} SKU)`);
    const input = inventoryInput(chunk, selectedLocation);
    const data = await graphql(mutation, { input, key: crypto.randomUUID() });
    const payload = data.inventorySetQuantities;
    if (!payload || payload.userErrors?.length || !payload.inventoryAdjustmentGroup) {
      throw new Error(`Lotto ${i / MUTATION_SIZE + 1} con errori; verificare le quantità in Shopify: ${JSON.stringify(payload?.userErrors ?? [])}`);
    }
    updated += chunk.length;
    console.log(`Lotto ${batch}/${totalBatches} confermato da Shopify; ${updated}/${updates.length} SKU inviati`);
  }
  return updated;
}

async function notify(message) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;
  if (!token || !chatId) { console.warn('Telegram non configurato'); return; }
  const run = process.env.GITHUB_REPOSITORY && process.env.GITHUB_RUN_ID
    ? `\nhttps://github.com/${process.env.GITHUB_REPOSITORY}/actions/runs/${process.env.GITHUB_RUN_ID}` : '';
  await withRetry(() => axios.post(`https://api.telegram.org/bot${token}/sendMessage`,
    { chat_id: chatId, text: `${message}${run}` }, { timeout: 15000 }), 'Telegram');
}

async function main() {
  const dryRun = process.argv.includes('--dry-run');
  try {
    const selectedLocation = locationId();
    const [products, combinations, stocks] = await Promise.all([
      prestaList('products', ['id', 'reference']),
      prestaList('combinations', ['id', 'id_product', 'reference']),
      prestaList('stock_availables', ['id', 'id_product', 'id_product_attribute', 'id_shop', 'quantity']),
    ]);
    const source = sourceMap(products, combinations, stocks);
    const graphql = await shopifyClient();
    const destination = await shopifyMap(graphql, selectedLocation);
    const { updates, stats } = plan(source, destination);
    if (source.ambiguous?.size) console.warn(`SKU ambigui in PrestaShop, esclusi: ${[...source.ambiguous].join(', ')}`);
    if (destination.ambiguous?.size) console.warn(`SKU ambigui in Shopify, esclusi: ${[...destination.ambiguous].join(', ')}`);
    console.log(JSON.stringify({ dryRun, ...stats, toUpdate: updates.length }));
    const updated = dryRun ? 0 : await apply(graphql, updates, selectedLocation);
    if (!dryRun && updated) {
      console.log('Verifica finale delle giacenze Shopify');
      const verifiedDestination = await shopifyMap(graphql, selectedLocation);
      const remaining = plan(source, verifiedDestination).updates;
      if (remaining.length) {
        throw new Error(`Verifica finale: ${remaining.length} SKU ancora diversi; primi SKU: ${remaining.slice(0, 10).map((item) => item.sku).join(', ')}`);
      }
      console.log(`Verifica finale superata: ${updated} aggiornamenti confermati`);
    }
    console.log(dryRun ? `Dry run completato: ${updates.length} aggiornamenti previsti, nessuna scrittura` : `Sync completata: ${updated} SKU aggiornati`);
    await notify(`✅ Sync inventario ${dryRun ? 'simulata' : 'completata'}\nSKU fonte: ${stats.source}\nCorrispondenti: ${stats.matched}\nAggiornati: ${updated}\nDa aggiornare: ${updates.length}\nAzzerati: ${stats.zero}\nSenza corrispondenza: ${stats.missing}\nSaltati: ${stats.skipped}\nSKU ambigui fonte: ${stats.ambiguousSource}\nSKU ambigui Shopify: ${stats.ambiguousShopify}`);
  } catch (error) {
    console.error(error.message);
    try { await notify(`❌ Sync inventario fallita\n${error.message}`); }
    catch (notificationError) { console.error(`Telegram: ${notificationError.message}`); }
    process.exitCode = 1;
  }
}

if (require.main === module) main();
module.exports = { quantity, sourceMap, plan, inventoryInput };
