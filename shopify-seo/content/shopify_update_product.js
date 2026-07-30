#!/usr/bin/env node

/**
 * Shopify SEO Product Updater
 * Aggiorna prodotti Shopify con dati SEO ottimizzati da un file JSON.
 * 
 * Uso:
 *   node update-shopify-seo.js ./dati-prodotto.json
 * 
 * Variabili d'ambiente richieste:
 *   SHOPIFY_SHOP_DOMAIN    -> tuo-dominio.myshopify.com
 *   SHOPIFY_ACCESS_TOKEN   -> Admin API access token (App private o Custom App)
 *   SHOPIFY_PRODUCT_ID     -> ID numerico del prodotto Shopify (es: 8234156810423)
 * 
 * Se SHOPIFY_PRODUCT_ID non è impostato, lo script cerca il prodotto per handle.
 */

const fs = require('fs').promises;
const path = require('path');

// ==================== CONFIGURAZIONE ====================
const CONFIG = {
  SHOP_DOMAIN: process.env.SHOPIFY_SHOP_DOMAIN,
  ACCESS_TOKEN: process.env.SHOPIFY_ACCESS_TOKEN,
  API_VERSION: process.env.SHOPIFY_API_VERSION || '2024-10',
  PRODUCT_ID: process.env.SHOPIFY_PRODUCT_ID || null,
};

// ==================== UTILS API ====================
function getBaseUrl() {
  return `https://${CONFIG.SHOP_DOMAIN}/admin/api/${CONFIG.API_VERSION}`;
}

function getHeaders() {
  return {
    'Content-Type': 'application/json',
    'X-Shopify-Access-Token': CONFIG.ACCESS_TOKEN,
  };
}

async function shopifyFetch(endpoint, options = {}) {
  const url = `${getBaseUrl()}${endpoint}`;
  const res = await fetch(url, {
    ...options,
    headers: { ...getHeaders(), ...options.headers },
  });

  const data = await res.json().catch(() => ({}));

  if (!res.ok) {
    throw new Error(
      `Shopify API Error ${res.status}: ${JSON.stringify(data.errors || data)}`
    );
  }
  return data;
}

// ==================== FAQ SCHEMA BUILDER ====================
function buildFaqSchema(faqArray) {
  if (!Array.isArray(faqArray) || faqArray.length === 0) return '';
  
  const schema = {
    '@context': 'https://schema.org',
    '@type': 'FAQPage',
    mainEntity: faqArray.map((faq) => ({
      '@type': 'Question',
      name: faq.question,
      acceptedAnswer: {
        '@type': 'Answer',
        text: faq.answer,
      },
    })),
  };

  return `
<!-- FAQ Schema JSON-LD - Iniettato per SEO -->
<script type="application/ld+json">
${JSON.stringify(schema, null, 2)}
</script>`;
}

// ==================== PRODOTTO: GET / UPDATE ====================
async function getProductByHandle(handle) {
  const data = await shopifyFetch(`/products.json?handle=${encodeURIComponent(handle)}&limit=1`);
  if (!data.products || data.products.length === 0) {
    throw new Error(`Prodotto con handle "${handle}" non trovato.`);
  }
  return data.products[0];
}

async function getProductById(id) {
  const data = await shopifyFetch(`/products/${id}.json`);
  return data.product;
}

async function updateProduct(productId, payload) {
  return shopifyFetch(`/products/${productId}.json`, {
    method: 'PUT',
    body: JSON.stringify({ product: payload }),
  });
}

// ==================== METAFIELDS SEO ====================
async function getMetafields(productId) {
  const data = await shopifyFetch(`/products/${productId}/metafields.json?limit=250`);
  return data.metafields || [];
}

async function createOrUpdateMetafield(productId, namespace, key, value, type = 'single_line_text_field') {
  // Cerca metafield esistente
  const metafields = await getMetafields(productId);
  const existing = metafields.find(m => m.namespace === namespace && m.key === key);

  const payload = {
    metafield: {
      namespace,
      key,
      value,
      type,
    },
  };

  if (existing) {
    // Update
    return shopifyFetch(`/products/${productId}/metafields/${existing.id}.json`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
  } else {
    // Create
    return shopifyFetch(`/products/${productId}/metafields.json`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }
}

// ==================== IMMAGINI: ALT TEXT ====================
async function updateImagesAltText(productId, altText) {
  // Recupera prodotto per avere le immagini attuali
  const product = await getProductById(productId);
  
  if (!product.images || product.images.length === 0) {
    console.log('⚠️  Nessuna immagine trovata sul prodotto.');
    return;
  }

  const updates = product.images.map((img) =>
    shopifyFetch(`/products/${productId}/images/${img.id}.json`, {
      method: 'PUT',
      body: JSON.stringify({
        image: {
          id: img.id,
          alt: altText,
        },
      }),
    })
  );

  await Promise.all(updates);
  console.log(`✅ Aggiornati ${updates.length} alt text immagini.`);
}

// ==================== CORE LOGIC ====================
async function processProduct(jsonData) {
  // Il tuo JSON ha una struttura numerata: { "9": { ... } }
  // Estraiamo il primo prodotto disponibile o iteriamo su tutti
  const keys = Object.keys(jsonData);
  
  for (const key of keys) {
    const item = jsonData[key];
    console.log(`\n🚀 Processando prodotto chiave "${key}" -> ${item.nome}`);

    // 1. Identifica prodotto Shopify
    let product;
    if (CONFIG.PRODUCT_ID) {
      console.log(`🔍 Ricerca per ID: ${CONFIG.PRODUCT_ID}`);
      product = await getProductById(CONFIG.PRODUCT_ID);
    } else {
      console.log(`🔍 Ricerca per handle: ${item.url_handle_suggestion}`);
      product = await getProductByHandle(item.url_handle_suggestion);
    }

    const productId = product.id;
    console.log(`🎯 Trovato: "${product.title}" (ID: ${productId})`);

    // 2. Prepara descrizione con FAQ Schema
    const faqSchemaHtml = buildFaqSchema(item.faq_schema);
    const fullBodyHtml = `${item.descrizione}\n${faqSchemaHtml}`;

    // 3. Prepara payload prodotto
    const updatePayload = {
      title: item.nome,                       // H1 implicito della pagina prodotto
      body_html: fullBodyHtml,                // Descrizione + Schema
      handle: item.url_handle_suggestion,   // URL ottimizzato
      tags: item.target_keywords || [],      // Keywords come tags
    };

    // 4. Aggiorna prodotto base
    await updateProduct(productId, updatePayload);
    console.log(`✅ Prodotto aggiornato: title, handle, tags, descrizione.`);

    // 5. Aggiorna Metafields SEO (Meta Title & Meta Description)
    // Shopify usa il namespace "global" per i metafield SEO classici
    if (item.meta_title) {
      await createOrUpdateMetafield(
        productId,
        'global',
        'title_tag',
        item.meta_title,
        'single_line_text_field'
      );
      console.log(`✅ Meta Title aggiornato: "${item.meta_title}"`);
    }

    if (item.meta_description) {
      await createOrUpdateMetafield(
        productId,
        'global',
        'description_tag',
        item.meta_description,
        'multi_line_text_field'
      );
      console.log(`✅ Meta Description aggiornata.`);
    }

    // 6. Aggiorna Alt Text immagini
    if (item.image_alt_text) {
      await updateImagesAltText(productId, item.image_alt_text);
    }

    console.log(`🏁 Prodotto "${item.nome}" aggiornato con successo!\n`);
  }
}

// ==================== MAIN ====================
(async () => {
  try {
    // Validazione config
    if (!CONFIG.SHOP_DOMAIN || !CONFIG.ACCESS_TOKEN) {
      console.error('❌ Errore: Configura le variabili d\'ambiente SHOPIFY_SHOP_DOMAIN e SHOPIFY_ACCESS_TOKEN.');
      process.exit(1);
    }

    // Leggi file JSON
    const filePath = process.argv[2];
    if (!filePath) {
      console.error('❌ Errore: Specifica il percorso del file JSON.\nUso: node update-shopify-seo.js ./file.json');
      process.exit(1);
    }

    const raw = await fs.readFile(path.resolve(filePath), 'utf-8');
    const jsonData = JSON.parse(raw);

    await processProduct(jsonData);

  } catch (err) {
    console.error('💥 Errore:', err.message);
    process.exit(1);
  }
})();