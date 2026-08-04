const fs = require("fs");
const { freeCallApi } = require('../../lib/free_ai_api_v1');

// ═══════════════════════════════════════════════════════════════
// CONFIGURAZIONE
// ═══════════════════════════════════════════════════════════════

const FILE_NUM = 15;
const PRODOTTI_PATH = __dirname + `/files/${FILE_NUM}_todo.json`;
const OUTPUT_PATH = __dirname + `/files/${FILE_NUM}_done.json`;
const PROGRESS_PATH = __dirname + `/files/${FILE_NUM}_progress.json`;

const BATCH_SIZE = 2;
const DELAY_MS = 4000; // Delay consapevole tra batch per non stressare le API gratuite

// ═══════════════════════════════════════════════════════════════
// PROMPT SEO (ISTRUZIONE FISSA — viene ottimizzata e cache-ata 1 volta)
// ═══════════════════════════════════════════════════════════════
// NOTA: questo prompt NON cambia mai tra i batch.
// free_ai_api lo ottimizza automaticamente alla prima chiamata (cache MISS)
// e riusa la versione ottimizzata per tutti i batch successivi (cache HIT).
// Il risparmio di token è ~2000-3000 token per ogni batch dopo il primo.

const PROMPT_BASE = `Agisci come un Senior E-Commerce SEO Specialist e Conversion Copywriter con 10 anni di esperienza su Shopify, specializzato nel settore [INSERISCI IL TUO SETTORE: es. ricambi auto, giardinaggio, moto].

MISSIONE: Riceverai un array JSON di prodotti. Per ogni prodotto devi produrre un oggetto JSON ottimizzato SEO che massimizzi il ranking su Google Shopping, la ricerca organica e il tasso di conversione.

═══════════════════════════════════════════════════════════════
📥 INPUT CHE RICEVERAI
═══════════════════════════════════════════════════════════════

Array di oggetti con questi campi (esempio):
[
  {
    "Codice prodotto": 9,
    "Riferimento": "M009",
    "Nome": "MINIMOTO GP2 49CC - minicross moto a motore minigp 2 tempi",
    "EAN13": 7426869236474,
    "Sommario": "<p>Testo breve...</p>",
    "Descrizione": "<table>...specifiche tecniche...</table>",
    "Prezzo (Tasse Escluse)": 245.89,
    "Prezzo (Tasse Incluse)": 299.99,
    "Nomi delle categorie": "Veicoli,Veicoli a motore,Minimoto",
    "Quantità": 0,
    "URL immagine di copertina": "https://...",
    "tag": "veicoli-minimoto"
  }
]

═══════════════════════════════════════════════════════════════
⚠️ REGOLE ASSOLUTE — NON TRASGREDIRE
═══════════════════════════════════════════════════════════════

1. NON modificare MAI: id/Codice prodotto, Riferimento, SKU, EAN13, Prezzi, Quantità, URL immagini.
2. NON inventare specifiche tecniche non presenti nell'input.
3. NON usare MAI ALL CAPS nel testo (tranne acronmi tecnici: cc, cv, rpm, kw).
4. Output ESCLUSIVAMENTE in JSON valido. Nessun testo prima o dopo.
5. Ogni campo HTML deve essere valido e ben formattato.

═══════════════════════════════════════════════════════════════
🔍 ANALISI PRELIMINARE OBBLIGATORIA (pensa passo dopo passo)
═══════════════════════════════════════════════════════════════

Prima di scrivere, analizza mentalmente:
- Qual è il termine di ricerca principale (head keyword)? Es: "minimoto 49cc bambini"
- Quali sono le long-tail keywords? Es: "minimoto a scoppio per bambini 8 anni"
- Qual è l'intento di ricerca? (Informativo, Transazionale, Navigazionale)
- Chi è il buyer persona? (Genitore, meccanico, hobbista?)
- Qual è il USP (Unique Selling Proposition) del prodotto?
- Quali objection devi prevenire? (sicurezza, montaggio, compatibilità)

═══════════════════════════════════════════════════════════════
✍️ STRUTTURA OUTPUT PER OGNI PRODOTTO
═══════════════════════════════════════════════════════════════

La chiave del JSON deve essere il "Codice prodotto" (come stringa).

{
  "[Codice prodotto]": {

    "nome": "Titolo prodotto ottimizzato",

    "sommario": "HTML del sommario riscritto",

    "descrizione": "HTML della descrizione riscritta",

    "meta_title": "Meta title ottimizzato",

    "meta_description": "Meta description persuasiva",

    "target_keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"],

    "h1_suggestion": "Suggerimento H1 per la pagina prodotto",

    "url_handle_suggestion": "suggerimento-url-prodotto",

    "image_alt_text": "Testo ALT ottimizzato per l'immagine principale",

    "faq_schema": [
      {"question": "Domanda frequente 1", "answer": "Risposta concisa"},
      {"question": "Domanda frequente 2", "answer": "Risposta concisa"}
    ]
  }
}

═══════════════════════════════════════════════════════════════
📐 SPECIFICHE CAMPO PER CAMPO
═══════════════════════════════════════════════════════════════

─── NOME PRODOTTO ───
• Lunghezza: 50-70 caratteri (massimo 100).
• Struttura: [Keyword principale] + [Attributo chiave] + [Specifica/Beneficio]
• Esempio BUONO: "Minimoto GP2 49cc 2 Tempi per Bambini | Freni a Disco e Doppio Scarico"
• Esempio CATTIVO: "MINIMOTO GP2 49CC - minicross moto a motore minigp 2 tempi"
• Regole: Iniziali maiuscole solo per parole significative. No pipe | all'inizio. Usa | o - per separare sezioni.
. PAROLE CHIAVE DI RICERCA PRIORITARIE: sostituisci sempre i termini tecnici interni (fornitore/magazzino) con i termini che i clienti digitano su Google. Esempio: "a scoppio" > "2 tempi"; "limitatore di velocità" > "regolatore"; "per bambini" > "per bimbo".

─── SOMMARIO ───
• Lunghezza: 150-250 caratteri.
• Deve essere un paragrafo <p> con 2-3 frasi.
• Focus: benefici immediati + chi è il prodotto per.
• Includi la keyword principale entro le prime 100 parole.
• Tono: entusiasta ma professionale. Risolvi la prima obiezione.

─── DESCRIZIONE ───
• Lunghezza: 300-600 parole (HTML incluso).
• Struttura OBBLIGATORIA:
  1. <p>Introduzione emotiva + problema che risolve (2-3 frasi)</p>
  2. <h3>Caratteristiche Principali</h3>
  3. <ul><li>Almeno 5 bullet point con benefici, non solo feature. Usa <strong> per evidenziare.</li></ul>
  4. <h3>Specifiche Tecniche</h3>
  5. <p>o <table> con le specifiche originali riorganizzate in formato leggibile</p>
  6. <h3>Perché Scegliere Questo Prodotto</h3>
  7. <p>Paragrafo di chiusura con garanzia, spedizione o assistenza</p>
• Ogni bullet point deve seguire la formula: Feature + Beneficio + Quando serve.
  Esempio: "<strong>Motore 49cc 2 tempi:</strong> potenza controllata di 4KW per garantire divertimento sicuro ai piccoli piloti, con raffreddamento ad aria per prestazioni costanti."
• Usa HTML semantico: <p>, <h3>, <ul>, <li>, <strong>, <table>, <tbody>, <tr>, <td>.
• No <div> o classi CSS. No testo in ALL CAPS.
• Includi 2-3 keyword secondarie in modo naturale.

─── META TITLE ───
• Max 60 caratteri (inclusi spazi).
• Formula: Keyword principale | Brand o CTA
• Esempio: "Minimoto 49cc Bambini GP2 | Freni a Disco e Scarico Racing"

─── META DESCRIPTION ───
• Max 155-160 caratteri.
• Formula: Keyword + Beneficio + CTA + Emoji opzionale (1 solo)
• Deve creare FOMO o urgenza morbida.
• Esempio: "Scopri la minimoto GP2 49cc per bambini con freni a disco e doppio scarico. Spedizione gratuita e reso 30 giorni. Ordina ora! 🏍️"

─── TARGET_KEYWORDS ───
• Array di 5 stringhe.
• Ordine: 1 head keyword, 2-3 long-tail, 1 branded o intento specifico.
• Esempio: ["minimoto 49cc bambini", "minimoto a scoppio per bambini", "minicross 2 tempi", "minimoto con freni a disco", "minimoto gp2"]

─── H1_SUGGESTION ───
• Deve essere diverso dal meta title.
• Max 70 caratteri.
• Più descrittivo e meno "marketing" del nome prodotto.

─── URL_HANDLE_SUGGESTION ───
• Slug SEO-friendly: tutto minuscolo, trattini, no stop words, no numeri inutili.
• Esempio: "minimoto-gp2-49cc-bambini-freni-disco"

─── IMAGE_ALT_TEXT ───
• Max 125 caratteri.
• Descrivi l'immagine + includi keyword.
• Esempio: "Minimoto GP2 49cc rossa con doppio scarico racing e freni a disco per bambini"

─── FAQ_SCHEMA ───
• 3 domande-risposte pertinenti.
• Domande devono riflettere ricerche reali (People Also Ask style).
• Risposte concise (max 150 caratteri).

═══════════════════════════════════════════════════════════════
🎯 PRINCIPI DI COPYWRITING DA APPLICARE
═══════════════════════════════════════════════════════════════

1. AIDA: Attenzione → Interesse → Desiderio → Azione
2. Trasforma ogni feature in beneficio. "Freni a disco" → "Massima sicurezza con freni a disco che garantiscono arresti precisi anche su terreni sconnessi"
3. Usa la seconda persona ("il tuo bambino", "la tua minimoto").
4. Preveni obiezioni: sicurezza, montaggio, compatibilità, garanzia.
5. CTA implicita ed esplicita.
6. Evita: "ottimo prodotto", "bello", "fantastico" senza contesto. Sostituisci con dati concreti.

═══════════════════════════════════════════════════════════════
📤 FORMATO OUTPUT
═══════════════════════════════════════════════════════════════

Rispondi SOLTANTO con un array JSON valido e formattato. Nessun markdown code block (no json). Nessun testo introduttivo o conclusivo. Il JSON deve essere parseable.

Struttura:
{
  "9": { ... },
  "11": { ... }
}`;

// ═══════════════════════════════════════════════════════════════
// UTILITIES
// ═══════════════════════════════════════════════════════════════

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function chunkArray(array, size) {
    const chunks = [];
    for (let i = 0; i < array.length; i += size) {
        chunks.push(array.slice(i, i + size));
    }
    return chunks;
}

function loadProgress() {
    try {
        if (fs.existsSync(PROGRESS_PATH)) {
            return JSON.parse(fs.readFileSync(PROGRESS_PATH, "utf8"));
        }
    } catch (e) {
        console.warn("⚠️  Progress file corrotto, parto da zero");
    }
    return { completed: [], results: {} };
}

function saveProgress(progress) {
    fs.writeFileSync(PROGRESS_PATH, JSON.stringify(progress, null, 2), "utf8");
}

// ═══════════════════════════════════════════════════════════════
// LOGGER CUSTOM per free_ai_api
// ═══════════════════════════════════════════════════════════════

const customLogger = {
    info: (tag, msg, data) => {
        if (msg === "API request") {
            console.log(
                `   🤖 [${data.provider}/${data.model}] key=${data.apiKey} | ${data.duration}ms${data.retry ? " (retry)" : ""}`
            );
        }
        if (msg === "Prompt cache HIT") {
            console.log(`   ⚡ Prompt cache HIT (hash: ${data.hash}) — riuso ottimizzato, nessuna chiamata AI per il template`);
        }
        if (msg === "Prompt cache MISS") {
            console.log(`   🆕 Prompt cache MISS (hash: ${data.hash}) — ottimizzazione AI del template in corso...`);
        }
        if (msg === "Prompt optimized via AI") {
            console.log(`   ✅ Template ottimizzato: ${data.originalTokens} → ${data.optimizedTokens} token (-${data.reduction})`);
        }
    },
    warn: (tag, msg, data) => {
        if (msg === "Fallback triggered") {
            console.log(`   🔄 Fallback: ${data.provider}/${data.model} → ${data.reason}`);
        }
        if (msg === "Prompt optimization failed, using original") {
            console.log(`   ⚠️  Ottimizzazione template fallita, uso originale: ${data.error}`);
        }
    },
    error: (tag, msg, data) => {
        console.error(`   ❌ [${tag}] ${msg}`, data);
    }
};

// ═══════════════════════════════════════════════════════════════
// AI CLIENT — usa free_ai_api con instruction + input
// ═══════════════════════════════════════════════════════════════
// MODIFICA CHIAVE: separa il prompt fisso (instruction) dai dati dinamici (input).
// free_ai_api ottimizza e cache-a l'istruzione automaticamente.

async function callAI(batch) {
    const result = await freeCallApi({
        instruction: PROMPT_BASE,   // ← Template SEO fisso: ottimizzato 1 volta, riusato N volte
        input: batch,               // ← Dati dinamici: cambiano ad ogni batch
        temperature: 0.1,
        maxTokens: 65536,
        logger: customLogger,
        optimize: true              // ← Abilita ottimizzazione AI del template (default true)
    });

    // Log metadati di ottimizzazione (solo per debug/monitoraggio)
    if (result._promptMeta) {
        const meta = result._promptMeta;
        if (meta.fromCache) {
            console.log(`   💾 Cache: HIT | Token risparmiati: ${meta.stats?.saved || 0}`);
        } else if (meta.fromOptimizer) {
            console.log(`   🧠 Cache: MISS | Template ottimizzato e salvato per i prossimi batch`);
        }
    }

    const text = result.text;
    console.log('text', text)

    return parseAIJson(text);
}

function parseAIJson(text) {
    if (typeof text === "object") {
        return text;
    }

    let clean = text.trim();

    // Rimuove markdown code block ```json ... ```
    clean = clean.replace(/^```json\s*/i, "");
    clean = clean.replace(/^```\s*/i, "");
    clean = clean.replace(/\s*```$/i, "");

    // Rimuove eventuali spazi
    clean = clean.trim();

    try {
        return JSON.parse(clean);
    } catch (error) {
        console.error("❌ JSON non valido ricevuto:");
        console.error(clean.substring(0, 1000));

        throw new Error(
            `AI response non parseabile: ${error.message}`
        );
    }
}

// ═══════════════════════════════════════════════════════════════
// MAIN
// ═══════════════════════════════════════════════════════════════

async function main() {
    console.log("🚀 Avvio ottimizzazione SEO prodotti Shopify (con free_ai_api + Prompt Cache)");
    console.log(`📁 File input: ${PRODOTTI_PATH}`);
    console.log(`📦 Batch size: ${BATCH_SIZE} prodotti`);
    console.log(`⏱️  Delay tra batch: ${DELAY_MS}ms`);
    console.log(`🧠 Prompt Optimization: ATTIVA (il template SEO viene ottimizzato 1 volta e cache-ato)\n`);

    // Carica prodotti
    if (!fs.existsSync(PRODOTTI_PATH)) {
        console.error(`❌ File non trovato: ${PRODOTTI_PATH}`);
        process.exit(1);
    }

    const prodotti = JSON.parse(fs.readFileSync(PRODOTTI_PATH, "utf8"));
    console.log(`📊 Totale prodotti: ${prodotti.length}`);

    // Carica progresso precedente
    const progress = loadProgress();
    const alreadyDone = new Set(progress.completed);
    console.log(`✅ Già completati: ${alreadyDone.size}`);

    const remaining = prodotti.filter(
        (p) => !alreadyDone.has(String(p["Codice prodotto"])),
    );

    if (remaining.length === 0) {
        console.log("🎉 Tutti i prodotti sono già stati processati!");
        if (Object.keys(progress.results).length > 0) {
            fs.writeFileSync(
                OUTPUT_PATH,
                JSON.stringify(progress.results, null, 2),
                "utf8",
            );
            console.log(`💾 File finale salvato: ${OUTPUT_PATH}`);
        }
        return;
    }

    console.log(`🔄 Prodotti rimanenti: ${remaining.length}\n`);

    const batches = chunkArray(remaining, BATCH_SIZE);
    let successCount = 0;
    let failCount = 0;

    for (let i = 0; i < batches.length; i++) {
        const batch = batches[i];
        const batchIds = batch.map((p) => p["Codice prodotto"]);
        const batchNum = i + 1;
        const totalBatches = batches.length;

        console.log(`\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━`);
        console.log(
            `📦 Batch ${batchNum}/${totalBatches} | Prodotti: ${batchIds.join(", ")}`,
        );

        try {
            const result = await callAI(batch);

            const missing = batchIds.filter((id) => !result[String(id)]);
            if (missing.length > 0) {
                console.warn(
                    `   ⚠️  Prodotti mancanti nella risposta: ${missing.join(", ")}`,
                );
            }

            Object.assign(progress.results, result);
            batchIds.forEach((id) => progress.completed.push(String(id)));

            successCount += batch.length - missing.length;
            if (missing.length > 0) failCount += missing.length;

            console.log(
                `   ✅ Batch completato (${Object.keys(result).length} prodotti)`,
            );
        } catch (error) {
            console.error(`   💥 Batch fallito definitivamente:`, error.message);
            if (error.attempts && error.attempts.length > 0) {
                console.error(`   📋 Tentativi falliti:`);
                error.attempts.forEach((a) => {
                    console.error(
                        `      - ${a.provider}/${a.model} [${a.keyId}]: ${a.error?.message || "Unknown"}`,
                    );
                });
            }
            failCount += batch.length;
        }

        saveProgress(progress);

        if (i < batches.length - 1) {
            console.log(`   ⏳ Attesa ${DELAY_MS / 1000}s prima del prossimo batch...`);
            await sleep(DELAY_MS);
        }
    }

    // Salva output finale
    fs.writeFileSync(
        OUTPUT_PATH,
        JSON.stringify(progress.results, null, 2),
        "utf8",
    );

    console.log(`\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━`);
    console.log("🎯 RIEPILOGO");
    console.log(`   ✅ Successo: ${successCount}`);
    console.log(`   ❌ Falliti: ${failCount}`);
    console.log(`   📁 Output: ${OUTPUT_PATH}`);

    if (failCount > 0) {
        console.log(
            `\n⚠️  Alcuni batch sono falliti. Rilancia lo script per riprovare.`,
        );
    } else {
        if (fs.existsSync(PROGRESS_PATH)) {
            fs.unlinkSync(PROGRESS_PATH);
            console.log(`   🧹 Progresso rimosso (tutto completato)`);
        }
    }
}

// Graceful shutdown
process.on("SIGINT", () => {
    console.log(
        "\n\n👋 Interruzione ricevuta. Progresso salvato. Rilancia per continuare.",
    );
    process.exit(0);
});

main().catch((err) => {
    console.error("💥 Errore fatale:", err);
    process.exit(1);
});