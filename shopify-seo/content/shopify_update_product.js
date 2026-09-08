const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
require('dotenv').config({ path: path.resolve(__dirname, '../../.env') });

const FILES_DIR = path.join(__dirname, 'files');
const PRODUCTS_PATH = path.resolve(__dirname, '../../products.json');
const CHECKPOINT_PATH = path.join(__dirname, '.shopify-seo-checkpoint.json');
const REPORT_PATH = path.join(__dirname, 'shopify-seo-update-report.json');
const SHOP_DOMAIN = String(process.env.SHOPIFY_SHOP_DOMAIN || '').replace(/^https?:\/\//, '').replace(/\/$/, '');
const API_VERSION = process.env.SHOPIFY_API_VERSION || '2026-07';
const MAX_RETRIES = 5;

function parseArgs(argv) {
  const args = { apply: false, from: null, to: null, file: null, limit: null, restart: false, stopOnError: false, skipHandles: false, skipImages: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--apply') args.apply = true;
    else if (arg === '--dry-run') args.apply = false;
    else if (arg === '--restart') args.restart = true;
    else if (arg === '--stop-on-error') args.stopOnError = true;
    else if (arg === '--skip-handles') args.skipHandles = true;
    else if (arg === '--skip-images') args.skipImages = true;
    else if (arg === '--from') args.from = Number(argv[++i]);
    else if (arg === '--to') args.to = Number(argv[++i]);
    else if (arg === '--file') args.file = Number(argv[++i]);
    else if (arg === '--limit') args.limit = Number(argv[++i]);
    else if (arg === '--help') args.help = true;
    else throw new Error(`Argomento sconosciuto: ${arg}`);
  }
  for (const key of ['from', 'to', 'file', 'limit']) {
    if (args[key] !== null && (!Number.isInteger(args[key]) || args[key] < 1)) throw new Error(`--${key} deve essere un intero positivo.`);
  }
  return args;
}

function printHelp() {
  console.log(`Uso:
  node shopify-seo/content/shopify_update_product.js              # dry-run di tutti i done
  node shopify-seo/content/shopify_update_product.js --apply      # aggiorna tutto il catalogo

Opzioni: --from N --to N --file N --limit N --restart --skip-handles
         --skip-images --stop-on-error --dry-run --apply`);
}

function discoverFiles(args) {
  return fs.readdirSync(FILES_DIR)
    .map((name) => ({ name, match: name.match(/^(\d+)_done\.json$/) }))
    .filter((x) => x.match)
    .map((x) => ({ number: Number(x.match[1]), path: path.join(FILES_DIR, x.name) }))
    .filter((x) => args.file === null || x.number === args.file)
    .filter((x) => args.from === null || x.number >= args.from)
    .filter((x) => args.to === null || x.number <= args.to)
    .sort((a, b) => a.number - b.number);
}

function validateItem(file, code, item) {
  const required = ['nome', 'descrizione', 'meta_title', 'meta_description', 'url_handle_suggestion'];
  const missing = required.filter((k) => typeof item?.[k] !== 'string' || !item[k].trim());
  if (missing.length) throw new Error(`${file}_done.json/${code}: mancano ${missing.join(', ')}`);
  if (item.meta_title.length > 70) throw new Error(`${file}_done.json/${code}: meta_title oltre 70 caratteri`);
  if (item.meta_description.length > 320) throw new Error(`${file}_done.json/${code}: meta_description oltre 320 caratteri`);
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(item.url_handle_suggestion)) throw new Error(`${file}_done.json/${code}: handle non valido`);
  if (item.faq_schema && !Array.isArray(item.faq_schema)) throw new Error(`${file}_done.json/${code}: faq_schema non è un array`);
}

function loadJobs(files, limit) {
  const source = JSON.parse(fs.readFileSync(PRODUCTS_PATH, 'utf8'));
  const skuByCode = new Map(source.map((p) => [String(p['Codice prodotto']), p.Riferimento]));
  const jobs = [], seen = new Set();
  for (const file of files) {
    const data = JSON.parse(fs.readFileSync(file.path, 'utf8'));
    if (!data || Array.isArray(data) || typeof data !== 'object') throw new Error(`${file.number}_done.json non contiene un oggetto`);
    for (const [code, item] of Object.entries(data)) {
      if (seen.has(code)) throw new Error(`Codice duplicato nei done: ${code}`);
      seen.add(code);
      const sku = skuByCode.get(String(code));
      if (!sku) throw new Error(`SKU assente in products.json per il codice ${code}`);
      validateItem(file.number, code, item);
      const fingerprint = crypto.createHash('sha256').update(JSON.stringify(item)).digest('hex');
      jobs.push({ file: file.number, code: String(code), sku: String(sku), item, fingerprint });
      if (limit && jobs.length >= limit) return jobs;
    }
  }
  return jobs;
}

let tokenPromise;
async function getToken() {
  if (process.env.SHOPIFY_ACCESS_TOKEN) return process.env.SHOPIFY_ACCESS_TOKEN;
  if (!process.env.SHOPIFY_CLIENT_ID || !process.env.SHOPIFY_CLIENT_SECRET) throw new Error('Configura SHOPIFY_ACCESS_TOKEN oppure SHOPIFY_CLIENT_ID e SHOPIFY_CLIENT_SECRET');
  if (!tokenPromise) tokenPromise = (async () => {
    const response = await fetch(`https://${SHOP_DOMAIN}/admin/oauth/access_token`, {
      method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ grant_type: 'client_credentials', client_id: process.env.SHOPIFY_CLIENT_ID, client_secret: process.env.SHOPIFY_CLIENT_SECRET }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.access_token) throw new Error(`Autenticazione Shopify fallita (${response.status}): ${JSON.stringify(data)}`);
    return data.access_token;
  })();
  return tokenPromise;
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function graphql(query, variables = {}, attempt = 0) {
  let response;
  try {
    response = await fetch(`https://${SHOP_DOMAIN}/admin/api/${API_VERSION}/graphql.json`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Shopify-Access-Token': await getToken() }, body: JSON.stringify({ query, variables }),
    });
  } catch (error) {
    if (attempt < MAX_RETRIES) { await wait(1000 * (2 ** attempt)); return graphql(query, variables, attempt + 1); }
    throw error;
  }
  const data = await response.json().catch(() => ({}));
  const retryable = response.status === 429 || response.status >= 500 || data.errors?.some((e) => e.extensions?.code === 'THROTTLED');
  if (retryable && attempt < MAX_RETRIES) {
    const retryAfter = Number(response.headers.get('retry-after'));
    await wait(Number.isFinite(retryAfter) ? retryAfter * 1000 : 1000 * (2 ** attempt));
    return graphql(query, variables, attempt + 1);
  }
  if (!response.ok) throw new Error(`Shopify HTTP ${response.status}: ${JSON.stringify(data)}`);
  if (data.errors?.length) throw new Error(`GraphQL: ${data.errors.map((e) => e.message).join(' | ')}`);
  return data.data;
}

async function loadSkuIndex() {
  const query = `query SeoVariants($after: String) {
    productVariants(first: 250, after: $after) {
      nodes { sku product { id title handle media(first: 20) { nodes { id alt mediaContentType } } } }
      pageInfo { hasNextPage endCursor }
    }
  }`;
  const index = new Map(); let after = null;
  do {
    const data = await graphql(query, { after });
    for (const variant of data.productVariants.nodes) {
      if (!variant.sku) continue;
      if (index.has(variant.sku) && index.get(variant.sku).id !== variant.product.id) throw new Error(`SKU duplicato su Shopify: ${variant.sku}`);
      index.set(variant.sku, variant.product);
    }
    after = data.productVariants.pageInfo.hasNextPage ? data.productVariants.pageInfo.endCursor : null;
  } while (after);
  return index;
}

function escapeHtml(value) {
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function buildDescription(item) {
  const clean = item.descrizione
    .replace(/<!--\s*FAQ Schema JSON-LD[\s\S]*?<\/script>\s*/gi, '')
    .replace(/<!--\s*seo-faq:start\s*-->[\s\S]*?<!--\s*seo-faq:end\s*-->/gi, '')
    .replace(/<h3>Domande Frequenti<\/h3>\s*$/i, '').trim();
  if (!Array.isArray(item.faq_schema)) return clean;
  const entries = item.faq_schema.filter((x) => x?.question && x?.answer)
    .map((x) => `<dt><strong>${escapeHtml(x.question)}</strong></dt><dd>${escapeHtml(x.answer)}</dd>`).join('');
  return entries ? `${clean}<h3>Domande Frequenti</h3><!-- seo-faq:start --><dl>${entries}</dl><!-- seo-faq:end -->` : clean;
}

async function updateProduct(product, item, args) {
  const mutation = `mutation SeoProductUpdate($product: ProductUpdateInput!) {
    productUpdate(product: $product) { product { id title handle } userErrors { field message } }
  }`;
  const input = { id: product.id, title: item.nome.trim(), descriptionHtml: buildDescription(item), seo: { title: item.meta_title.trim(), description: item.meta_description.trim() } };
  if (!args.skipHandles && item.url_handle_suggestion !== product.handle) { input.handle = item.url_handle_suggestion; input.redirectNewHandle = true; }
  const data = await graphql(mutation, { product: input });
  const errors = data.productUpdate.userErrors || [];
  if (errors.length) throw new Error(errors.map((e) => `${e.field?.join('.')}: ${e.message}`).join(' | '));
  return data.productUpdate.product;
}

async function updateMainImageAlt(product, alt) {
  const image = product.media?.nodes?.find((x) => x.mediaContentType === 'IMAGE');
  if (!image || !alt?.trim() || image.alt === alt.trim()) return false;
  const mutation = `mutation SeoFileUpdate($files: [FileUpdateInput!]!) {
    fileUpdate(files: $files) { files { id alt fileStatus } userErrors { field message code } }
  }`;
  const data = await graphql(mutation, { files: [{ id: image.id, alt: alt.trim() }] });
  const errors = data.fileUpdate.userErrors || [];
  if (errors.length) throw new Error(errors.map((e) => `${e.code || 'FILE'}: ${e.message}`).join(' | '));
  return true;
}

function readCheckpoint(restart) {
  if (restart || !fs.existsSync(CHECKPOINT_PATH)) return { completed: {} };
  try { const data = JSON.parse(fs.readFileSync(CHECKPOINT_PATH, 'utf8')); return data?.completed ? data : { completed: {} }; }
  catch { throw new Error(`Checkpoint non valido; usa --restart: ${CHECKPOINT_PATH}`); }
}

function writeAtomic(file, data) {
  const temp = `${file}.tmp`; fs.writeFileSync(temp, `${JSON.stringify(data, null, 2)}\n`); fs.renameSync(temp, file);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) return printHelp();
  if (!SHOP_DOMAIN) throw new Error('Configura SHOPIFY_SHOP_DOMAIN nel file .env');
  const files = discoverFiles(args);
  if (!files.length) throw new Error('Nessun file *_done.json corrisponde ai filtri');
  const jobs = loadJobs(files, args.limit), checkpoint = readCheckpoint(args.restart);
  const pending = jobs.filter((job) => checkpoint.completed[`${job.file}:${job.code}`]?.fingerprint !== job.fingerprint);
  console.log(`File: ${files.length} (${files[0].number}–${files.at(-1).number})`);
  console.log(`Prodotti validati: ${jobs.length} | da elaborare: ${pending.length}`);
  console.log(`Modalità: ${args.apply ? 'APPLY' : 'DRY-RUN'} | API: ${API_VERSION}`);
  if (!args.apply) { console.log('Nessuna scrittura. Riesegui con --apply per aggiornare Shopify.'); return; }

  console.log('Indicizzazione del catalogo Shopify per SKU…');
  const skuIndex = await loadSkuIndex();
  console.log(`SKU Shopify indicizzati: ${skuIndex.size}`);
  const report = { startedAt: new Date().toISOString(), apiVersion: API_VERSION, requested: pending.length, succeeded: 0, failed: 0, warnings: 0, errors: [] };

  for (let i = 0; i < pending.length; i += 1) {
    const job = pending[i], marker = `${job.file}:${job.code}`, label = `[${i + 1}/${pending.length}] ${marker} SKU ${job.sku}`;
    try {
      const product = skuIndex.get(job.sku);
      if (!product) throw new Error('SKU non trovato nel catalogo Shopify');
      const updated = await updateProduct(product, job.item, args);
      if (!args.skipImages && job.item.image_alt_text) {
        try { await updateMainImageAlt(product, job.item.image_alt_text); }
        catch (error) { report.warnings += 1; console.warn(`⚠ ${label}: prodotto aggiornato, alt immagine fallito: ${error.message}`); }
      }
      checkpoint.completed[marker] = { sku: job.sku, productId: updated.id, fingerprint: job.fingerprint, updatedAt: new Date().toISOString() };
      writeAtomic(CHECKPOINT_PATH, checkpoint); report.succeeded += 1; console.log(`✓ ${label}`);
    } catch (error) {
      report.failed += 1; report.errors.push({ file: job.file, code: job.code, sku: job.sku, error: error.message }); console.error(`✗ ${label}: ${error.message}`);
      if (args.stopOnError) break;
    }
  }
  report.finishedAt = new Date().toISOString(); writeAtomic(REPORT_PATH, report);
  console.log(`Completato: ${report.succeeded} riusciti, ${report.failed} falliti, ${report.warnings} avvisi.`);
  console.log(`Report: ${REPORT_PATH}`); if (report.failed) process.exitCode = 1;
}

main().catch((error) => { console.error(`Errore: ${error.message}`); process.exitCode = 1; });
