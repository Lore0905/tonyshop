require("dotenv").config();

const fs = require('fs');
const { validateShopifyToken } = require("./commons");



// ─── CONFIGURAZIONE ───
const SHOP_DOMAIN = process.env.SHOPIFY_SHOP_DOMAIN;
const API_VERSION = '2024-01';
// ─────────────────────

const ENDPOINT = `https://${SHOP_DOMAIN}/admin/api/${API_VERSION}/graphql.json`;

// Legge il JSON
const raw = fs.readFileSync('/Users/lorenzocastelli/projects/tonyshop/localDecision/data.json', 'utf8');
const products = JSON.parse(raw);

// Filtra solo quelli da nascondere
const toHide = products.filter(p => p['Prezzo (Tasse Escluse)'] === 0);

if (toHide.length === 0) {
  console.log('Nessun prodotto con toDelete: true trovato.');
  process.exit(0);
}

console.log(`Trovati ${toHide.length} prodotti da nascondere...\n`);

// Funzione per chiamare GraphQL
async function shopifyGraphQL(query, variables = {}) {
  const res = await fetch(ENDPOINT, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Shopify-Access-Token': await validateShopifyToken()
    },
    body: JSON.stringify({ query, variables })
  });
  return res.json();
}

// Cerca il prodotto su Shopify tramite SKU (Riferimento)
async function findBySku(sku) {
  const q = `
    query($query: String!) {
      products(first: 1, query: $query) {
        edges {
          node {
            id
            title
            status
            variants(first: 1) {
              edges {
                node { sku }
              }
            }
          }
        }
      }
    }
  `;
  const data = await shopifyGraphQL(q, { query: `sku:${sku}` });
  console.log(data);
  return data.data?.products?.edges?.[0]?.node;
}

// Aggiorna lo status a DRAFT
async function hideProduct(gid) {
  const mutation = `
    mutation($input: ProductInput!) {
      productUpdate(input: $input) {
        product { id title status }
        userErrors { field message }
      }
    }
  `;
  const data = await shopifyGraphQL(mutation, {
    input: { id: gid, status: 'DRAFT' }
  });
  return data.data?.productUpdate;
}

// Esecuzione
(async () => {
  for (const item of toHide) {
    const sku = item.Riferimento;
    const name = item.Nome;

    console.log(`🔍 [${sku}] ${name}`);

    const product = await findBySku(sku);
    console.log(product)
    if (!product) {
      console.log('   ❌ Non trovato su Shopify\n');
      continue;
    }

    console.log(`   Trovato: ${product.title} (attuale: ${product.status})`);

    if (product.status === 'DRAFT') {
      console.log('   ℹ️  Già nascosto, salto\n');
      continue;
    }

    const result = await hideProduct(product.id);
    if (result.userErrors?.length > 0) {
      console.log('   ❌ Errore:', result.userErrors[0].message, '\n');
    } else {
      console.log('   ✅ Nascosto (status: DRAFT)\n');
    }
  }

  console.log('Finito.');
})();