require("dotenv").config();

const fs = require('fs');
const commons = require("../commons");


// Legge il JSON
const raw = fs.readFileSync(__dirname + '/hidden_input.json', 'utf8');
const products = JSON.parse(raw);

// Filtra solo quelli da nascondere
const toHide = products.filter(p => p.tag === 'illuminazione');

if (toHide.length === 0) {
  console.log('Nessun prodotto con toDelete: true trovato.');
  process.exit(0);
}

console.log(`Trovati ${toHide.length} prodotti da nascondere...\n`);

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
  const data = await commons.shopifyGraphQL(mutation, {
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

    const product = await commons.findBySku(sku);
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