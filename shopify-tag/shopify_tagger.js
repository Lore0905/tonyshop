const fs = require('fs');

// ============================================
// CONFIGURAZIONE
// ============================================
const INPUT_FILE = './shopify-tag/tag_data.json'; 
const OUTPUT_FILE = './output-tags.json';

// ============================================
// KEYWORDS
// ============================================

// Parole che indicano un RICAMBIO nel nome (se presenti → NON è un veicolo completo)
const SPARE_NAME_KEYWORDS = [
  'pastigli', 'gomma ', 'cerchio', 'marmitta', 'carburatore', 'batteria',
  'bobina', 'statore', 'volano', 'cdi', 'candela', 'pistone', 'cilindro',
  'frizione', 'campana ', 'catena ', 'pignone', 'cuscinetto', 'guarnizione',
  'leva ', 'manubrio', 'sella ', 'carena', 'plastica', 'serbatoio',
  'ammortizzatore', 'forcella', 'mozzo', 'perno ', "camera d'aria",
  'camera aria', 'tappo ', 'tubo ', 'cavo ', 'kit ', 'set ', 'kit motore',
  'blocco motore', 'impianto ', 'cablaggio', 'radiatore', 'collettore',
  'adattatore', 'prolunga', 'inserto', 'funzione', 'lama ', 'asta ',
  'zaino', 'struttura', 'manico', 'impugnatura', 'cinghia', 'carter',
  'paracolpi', 'parasassi', 'testina', 'spugna', 'interruttore',
  'filtro ', 'boccola', 'coppiglia', 'molla ', 'pattino', 'pompa ',
  'pinza ', 'disco ', 'terminale', 'tubo ', 'coppia ', 'pedane',
  'poggiapiedi', 'parafango', 'codone', 'portanumero', 'adesivi',
  'grafiche', 'placche', 'paramano', 'paramani', 'copertone', 'coperton',
  'pneumatico', 'valvola', 'acceleratore', 'comando', 'tasto ',
  'blocchetto', 'chiavi', 'sell', 'plastich', 'caren', 'faro ', 'fanal',
  'lampadina', 'stator', 'avviamento', 'accensione', 'rapido ', 'regolatore',
  'rele ', 'relè', 'falsa maglia', 'tendicatena', 'pattino', 'guida ',
  'scorri catena', 'leve ', 'pedalina', 'pedale ', 'telaio', 'braccetti',
  'trapezzi', 'testine', 'asse ', 'assale', 'carro ', 'corona ', 'campana ',
  'riduttore', 'cambio', 'marce', 'retromarcia', 'filo ', 'guaina',
  'registro', 'blocca', 'fermo', 'fermapignone', 'triangolo', 'supporti',
  'manopole', 'acceleratore', 'freno ', 'freni ', 'leva aria', 'leva freno',
  'leva frizione', 'comando gas', 'gas ', 'rubinetto', 'benzina',
  'pacco lamellare', 'paccolamellare', 'pacco-lamellare', 'candela ',
  'molla c', 'tappo olio', 'vite carter', 'coppigli', 'puleggia',
  'boccola alluminio', 'filtro benzina', 'filtro aria', 'scatola aria',
  'pacco l', 'leva acceleratore', 'comando tasti', 'tasto spegnimento',
  'blocchetto chiavi', 'impugnatura', 'manopole', 'leve manubrio',
  'pompa freno', 'pinza freno', 'disco freno', 'pastiglie freno',
  'freno a disco', 'freno anteriore', 'freno posteriore', 'set freno',
  'kit freno', 'impianto freno', 'tubo freno', 'olio freno',
  'radiatore olio', 'kit radiatore', 'serbatoio ', 'serbatoi ',
  'rubinetto ', 'tubi ', 'cavi ', 'cavo ', 'filo frizione',
  'cavo frizione', 'guaina ', 'registro ', 'leva frizione',
  'pedale freno', 'pedale cambio', 'pedalina ', 'poggiapiedi',
  'parafango ', 'codone ', 'portanumero', 'adesivi ', 'grafiche ',
  'plastiche ', 'carene ', 'carena ', 'plastica ', 'fanale ',
  'faro ', 'luce ', 'lampada ', 'lampadina ', 'interruttore ',
  'comando ', 'tasto ', 'pulsante', 'blocchetto ', 'chiave ',
  'chiavi ', 'acceleratore ', 'gas ', 'manopola ', 'manopole ',
  'comando acceleratore', 'comando tasti', 'leva aria', 'leva freno',
  'leva frizione', 'leve ', 'manubrio ', 'manubri ', 'riser',
  'supporto manubrio', 'ponticello', 'riser manubrio', 'smaglia catene',
  'gruppo termico', 'protezione sotto motore', 'contenitore box batterie',
  'carica batterie', 'centralina ', 'maglietta ', 'gabbia rulli',
  'chiavella', 'albero spalle', 'albero motore', 'sistema di sicurezza',
  'caduta bimbi', 'cavalletto laterale', 'cupolino'
];

// Parole che indicano un prodotto giardinaggio COMPLETO
const GARDEN_COMPLETE = [
  'decespugliatore', 'tagliasiepe', 'motosega', 'soffiatore',
  'motocoltivatore', 'motozappa', 'pompa spruzzo'
];

// Parole che indicano un RICAMBIO per giardinaggio
const GARDEN_SPARE = [
  'ricambio', 'lama', 'asta', 'campana', 'frizione', 'carburatore', 'bobina',
  'tappo', 'tubo', 'manico', 'impugnatura', 'cinghia', 'carter', 'testina',
  'collettore', 'avviamento', 'serbatoio', 'prolunga', 'inserto', 'funzione',
  'blocco motore', 'kit motore', 'kit set motore', 'spugna', 'interruttore',
  'filtro aria', 'boccola', 'coppiglia', 'molla', 'volano', 'statore', 'cdi',
  'guarnizione', 'pistone', 'cilindro', 'coppia conica', 'terminale',
  'parasassi', 'paracolpi', 'connessione', 'zaino', 'struttura',
  'cinghia tracolla', 'ricambi giardinaggio', 'ricambi decespugliatori',
  'ricambi motoseghe', 'ricambi tagliasiepi', 'ricambi 4 tempi'
];

// Indicatori Pit Bike / Cross / Motard
const PITBIKE_INDICATORS = [
  'pit bike', 'pitbike', 'cross 125', 'cross 140', 'cross 150', 'cross 160',
  'cross 190', 'motard', 'dirt bike', 'bse ', 'kayo ', 'orion ', 'scorpion ',
  'monster ', 'crf70', 'crf50', 'crf 70', 'crf 50', 'td125', 'td160', 'tt125',
  'tt140', 'tt160', 'tt170', 'zeus ', 'yx ', 'zs ', 'td ', 'tt ', 'krz ',
  'm1008', 'au110', 'au125', 'au150', 'au180', 'au200', 'au300', 'zs190', 'tt190'
];

// Indicatori Minicross specifici
const MINICROSS_INDICATORS = [
  'cross 49', 'cross 50', 'spider', 'tiger', 'ktm replica', 'replica ktm', 'morini'
];

// ============================================
// FUNZIONE DI CLASSIFICAZIONE
// ============================================
function classifyProduct(product) {
  const nomeRaw = String(product.Nome || '');
  const nome = nomeRaw.toLowerCase();
  const descrizione = String(product.Descrizione || '').toLowerCase();
  const sommario = String(product.Sommario || '').toLowerCase();
  const categorie = String(product['Nomi delle categorie (x,y,z...)'] || '').toLowerCase();
  const riferimento = String(product.Riferimento || '').toUpperCase();
  const prezzo = product['Prezzo (Tasse Incluse)'] || 0;

  const fullText = `${nome} ${descrizione} ${sommario} ${categorie}`;

  // --- 1. ILLUMINAZIONE / ALIMENTATORI / ARREDAMENTO ---
  if (categorie.includes('illuminazione') && !categorie.includes('ricambi')) {
    return 'illuminazione';
  }
  if (categorie.includes('alimentatori')) {
    return 'illuminazione';
  }
  if (categorie.includes('arredamento') && !categorie.includes('veicoli') && !categorie.includes('ricambi')) {
    return 'arredamento';
  }

  // --- 2. GIARDINAGGIO ---
  // Prodotto giardinaggio COMPLETO
  const isGardenComplete = GARDEN_COMPLETE.some(g => nome.includes(g));
  const hasGardenSpareInName = GARDEN_SPARE.some(s => nome.includes(s));
  const isGardenSpareCategory = ['ricambi giardinaggio', 'ricambi decespugliatori', 'ricambi motoseghe', 'ricambi tagliasiepi', 'ricambi 4 tempi']
    .some(s => categorie.includes(s));

  if (isGardenComplete && !hasGardenSpareInName) {
    return 'giardinaggio-decespugliatori';
  }
  if (isGardenSpareCategory || (categorie.includes('giardinaggio') && hasGardenSpareInName)) {
    return 'giardinaggio-decespugliatori';
  }

  // --- 3. DETERMINA SE è UN VEICOLO COMPLETO ---
  const hasSpareInName = SPARE_NAME_KEYWORDS.some(s => nome.includes(s));
  const startsWithSparePrefix = /^\d+\s*pz\s|^\d+pz\s|^coppia\s|^kit\s|^set\s|^solo\s|^falsa maglia/.test(nome);
  const isSpare = hasSpareInName || startsWithSparePrefix;

  let isVehicle = false;

  if (!isSpare) {
    // Se nelle categorie c'è "Veicoli" e NON "Ricambi"
    if (categorie.includes('veicoli') && !categorie.includes('ricambi')) {
      // Il nome deve contenere esplicitamente un tipo di veicolo
      const vehicleInName = ['minimoto', 'minicross', 'miniquad', 'mini atv', 'mini-atv', 'mini quad', 'quad ', ' atv ', 'pit bike', 'pitbike', 'motard']
        .some(v => nome.startsWith(v) || nome.includes(' ' + v));
      if (vehicleInName) {
        isVehicle = true;
      }
    }

    // Se prezzo è molto alto (>120€) e descrizione lunga con specifiche tecniche
    // e il nome contiene un tipo di veicolo
    if (prezzo > 120 && descrizione.length > 300) {
      const hasEngineSpecs = /motore|potenza|cilindrata|trasmissione|carburante|raffreddamento/.test(descrizione);
      const vehicleInName = ['minimoto', 'minicross', 'miniquad', 'mini atv', 'mini-atv', 'mini quad', 'quad ', ' atv ', 'pit bike', 'pitbike', 'motard']
        .some(v => nome.includes(v));
      if (hasEngineSpecs && vehicleInName) {
        isVehicle = true;
      }
    }
  }

  // --- 4. ASSEGNAZIONE TAG VEICOLI ---
  if (isVehicle) {
    // Priorità al nome esatto
    if (nome.startsWith('minimoto') || nome.includes(' minimoto') || nome.includes('-minimoto')) {
      if (!nome.includes('minicross') || nome.indexOf('minimoto') < nome.indexOf('minicross')) {
        return 'veicoli-minimoto';
      }
    }
    if (nome.startsWith('minicross') || nome.includes(' minicross') || nome.includes('-minicross')) {
      return 'veicoli-minicross';
    }
    if (nome.startsWith('miniquad') || nome.includes(' miniquad') || nome.includes('-miniquad') || nome.includes('mini atv') || nome.includes('mini-atv') || nome.includes('mini quad')) {
      return 'veicoli-miniquad';
    }
    if (nome.startsWith('quad') || nome.includes(' quad')) {
      return 'veicoli-quad';
    }
    if (nome.includes('pit bike') || nome.includes('pitbike') || nome.includes('motard')) {
      return 'veicoli-pitbike';
    }

    // Fallback su testo completo
    if (fullText.includes('minimoto') && !fullText.includes('minicross')) return 'veicoli-minimoto';
    if (fullText.includes('minicross')) return 'veicoli-minicross';
    if (fullText.includes('miniquad') || fullText.includes('mini atv')) return 'veicoli-miniquad';
    if (fullText.includes('quad')) return 'veicoli-quad';
    return 'veicoli-altro';
  }

  // --- 5. ASSEGNAZIONE TAG RICAMBI ---

  // 5.1 Pit Bike / Cross / Motard
  const isPitbike = PITBIKE_INDICATORS.some(p => fullText.includes(p));
  if (isPitbike) {
    if (['gomma', 'copertone', 'pneumatico', 'cerchio', 'cerchiata', "camera d'aria", 'camera aria'].some(k => fullText.includes(k))) {
      return 'ricambi-gomme';
    }
    if (['freno', 'pastiglia', 'pompa freno', 'pinza freno', 'disco freno', 'impianto freno'].some(k => fullText.includes(k))) {
      return 'ricambi-freni';
    }
    if (['motore', 'blocco motore', 'cilindro', 'pistone', 'testa motore', 'albero', 'bielle', 'carter motore'].some(k => fullText.includes(k))) {
      return 'ricambi-motori';
    }
    return 'ricambi-pitbike-cross-motard';
  }

  // 5.2 Minicross
  const isMinicross = fullText.includes('minicross') || MINICROSS_INDICATORS.some(k => nome.includes(k));
  if (isMinicross) {
    if (['gomma', 'copertone', 'pneumatico', 'cerchio', "camera d'aria", 'camera aria'].some(k => fullText.includes(k))) {
      return 'ricambi-gomme';
    }
    if (['freno', 'pastiglia', 'disco freno'].some(k => fullText.includes(k))) {
      return 'ricambi-freni';
    }
    if (['motore', 'cilindro', 'pistone', 'blocco motore'].some(k => fullText.includes(k))) {
      return 'ricambi-motori';
    }
    return 'ricambi-minicross';
  }

  // 5.3 Minimoto
  if (fullText.includes('minimoto')) {
    if (['gomma', 'copertone', 'pneumatico', 'cerchio', "camera d'aria", 'camera aria'].some(k => fullText.includes(k))) {
      return 'ricambi-gomme';
    }
    if (['freno', 'pastiglia', 'disco freno'].some(k => fullText.includes(k))) {
      return 'ricambi-freni';
    }
    if (['motore', 'cilindro', 'pistone', 'blocco motore'].some(k => fullText.includes(k))) {
      return 'ricambi-motori';
    }
    return 'ricambi-minimoto';
  }

  // 5.4 Miniquad 2T
  const isMiniquad2T = categorie.includes('miniquad 2 tempi') ||
    (fullText.includes('miniquad') && (fullText.includes('2 tempi') || fullText.includes('2t') || fullText.includes('49cc')));
  if (isMiniquad2T) {
    if (['gomma', 'copertone', 'pneumatico', 'cerchio', "camera d'aria", 'camera aria'].some(k => fullText.includes(k))) {
      return 'ricambi-gomme';
    }
    if (['freno', 'pastiglia', 'disco freno'].some(k => fullText.includes(k))) {
      return 'ricambi-freni';
    }
    if (['motore', 'cilindro', 'pistone', 'blocco motore'].some(k => fullText.includes(k))) {
      return 'ricambi-motori';
    }
    return 'ricambi-miniquad-2t';
  }

  // 5.5 Quad 4T
  const isQuad4T = categorie.includes('quad 4 tempi') ||
    (fullText.includes('quad') && (fullText.includes('4 tempi') || fullText.includes('4t') || fullText.includes('110cc') || fullText.includes('125cc') || fullText.includes('150cc')));
  if (isQuad4T) {
    if (['gomma', 'copertone', 'pneumatico', 'cerchio', "camera d'aria", 'camera aria'].some(k => fullText.includes(k))) {
      return 'ricambi-gomme';
    }
    if (['freno', 'pastiglia', 'pompa freno', 'pinza', 'disco freno'].some(k => fullText.includes(k))) {
      return 'ricambi-freni';
    }
    if (['motore', 'blocco motore', 'cilindro', 'pistone', 'testa motore'].some(k => fullText.includes(k))) {
      return 'ricambi-motori';
    }
    return 'ricambi-quad-4t';
  }

  // 5.6 Gomme (generico)
  if (['gomma ', 'copertone', 'pneumatico', 'cerchio ', 'cerchiata', "camera d'aria", 'camera aria'].some(k => fullText.includes(k))) {
    return 'ricambi-gomme';
  }

  // 5.7 Freni (generico)
  if (['pastiglia', 'pastiglie', 'freno ', 'freni ', 'pompa freno', 'pinza freno', 'disco freno', 'impianto freno'].some(k => fullText.includes(k))) {
    return 'ricambi-freni';
  }

  // 5.8 Motori
  if (['blocco motore', 'motore ', 'cilindro', 'pistone', 'testa motore', 'albero', 'bielle', 'carter motore'].some(k => fullText.includes(k))) {
    return 'ricambi-motori';
  }

  // 5.9 Elettrico
  if (['batteria', 'statore', 'volano', 'cdi', 'centralina', 'bobina', 'impianto elettrico', 'cablaggio', 'regolatore', 'rele', 'relè', 'interruttore', 'tasto', 'comando', 'lampadina', 'fanale', 'blocchetto chiavi'].some(k => fullText.includes(k))) {
    return 'ricambi-elettrico';
  }

  // 5.10 Giardinaggio fallback
  if (categorie.includes('giardinaggio')) {
    return 'giardinaggio-decespugliatori';
  }

  // 5.11 Default
  return 'ricambi-altro';
}

// ============================================
// ESECUZIONE
// ============================================
try {
  console.log(`Leggo ${INPUT_FILE}...`);
  const raw = fs.readFileSync(INPUT_FILE, 'utf8');
  const products = JSON.parse(raw);

  console.log(`Trovati ${products.length} prodotti. Classifico...`);

  const results = [];
  const stats = {};

  for (const p of products) {
    const tag = classifyProduct(p);
    results.push({
      'Codice prodotto': p['Codice prodotto'],
      tag: tag
    });
    stats[tag] = (stats[tag] || 0) + 1;
  }

  fs.writeFileSync(OUTPUT_FILE, JSON.stringify(results, null, 2));

  console.log(`\n✅ Fatto! Scritto ${OUTPUT_FILE}`);
  console.log(`\n--- Distribuzione tag ---`);
  Object.entries(stats)
    .sort((a, b) => b[1] - a[1])
    .forEach(([tag, count]) => {
      console.log(`${tag.padEnd(40)} | ${String(count).padStart(5)} prodotti`);
    });
  console.log(`-------------------------`);
  console.log(`Totale: ${results.length} prodotti`);

} catch (err) {
  console.error('❌ Errore:', err.message);
  process.exit(1);
}