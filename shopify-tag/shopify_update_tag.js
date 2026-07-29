const fs = require('fs');
const path = require('path');
const commons = require('../commons');

// --- Configurazione ---
const INPUT_FILE = process.argv[2] || __dirname + '/tag_output.json';

/**
 * Aggiunge un tag al prodotto se non è già presente.
 * Ritorna l'esito dell'operazione.
 */
async function aggiungiTag(productId, tagsEsistenti, nuovoTag) {
  // Evita duplicati
  if (tagsEsistenti.includes(nuovoTag)) {
    return { skipped: true, reason: `Tag "${nuovoTag}" già presente` };
  }

  const mutation = `
    mutation($input: ProductInput!) {
      productUpdate(input: $input) {
        product {
          id
          tags
        }
        userErrors {
          field
          message
        }
      }
    }
  `;

  const result = await commons.shopifyGraphQL(mutation, {
    input: {
      id: productId,
      tags: [...tagsEsistenti, nuovoTag]
    }
  });

  return result.data?.productUpdate || { userErrors: [{ message: 'Risposta GraphQL non valida' }] };
}

// --- Main ---

async function main() {
  if (!fs.existsSync(INPUT_FILE)) {
    console.error(`❌ File non trovato: ${INPUT_FILE}`);
    console.error('Uso: node aggiorna-tags.js [percorso-file.json]');
    process.exit(1);
  }

  const dati = JSON.parse(fs.readFileSync(INPUT_FILE, 'utf8'));
  console.log(`📦 Trovati ${dati.length} prodotti da processare\n`);

  let aggiornati = 0;
  let saltati = 0;
  let errori = 0;

  for (let i = 0; i < dati.length; i++) {
    const item = dati[i];
    const sku = item.Riferimento;
    const tag = item.tag;

    console.log(`[${i + 1}/${dati.length}] Riferimento: ${sku || 'N/D'} | Tag da aggiungere: ${tag || 'N/D'}`);

    if (!sku || !tag) {
      console.log('   ⏭️  SKIP: Riferimento o tag mancante');
      saltati++;
      continue;
    }

    try {
      // Se findBySku è già in commons, puoi usare: await commons.findBySku(sku)
      const prodotto = await commons.findBySku(sku);

      if (!prodotto) {
        console.log(`   ❌ NOT FOUND: nessun prodotto trovato per SKU "${sku}"`);
        errori++;
        continue;
      }

      console.log('ARRIVA');

      const result = await aggiungiTag(prodotto.id, prodotto.tags || [], tag);

      if (result.skipped) {
        console.log(`   ⏭️  SKIP: ${result.reason}`);
        saltati++;
      } else if (result.userErrors && result.userErrors.length > 0) {
        console.log(`   ❌ ERRORE GraphQL:`, result.userErrors);
        errori++;
      } else {
        console.log(`   ✅ OK: tag "${tag}" aggiunto a "${prodotto.title}"`);
        aggiornati++;
      }
    } catch (err) {
      console.error(`   💥 EXCEPTION:`, err.message);
      errori++;
    }
  }

  console.log(`\n🏁 Finito. Aggiornati: ${aggiornati} | Saltati: ${saltati} | Errori: ${errori}`);
}

main().catch(console.error);