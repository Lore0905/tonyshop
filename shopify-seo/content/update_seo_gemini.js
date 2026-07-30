const fs = require("fs");
const { GoogleGenerativeAI } = require("@google/generative-ai");

// ═══════════════════════════════════════════════════════════════
// CONFIGURAZIONE
// ═══════════════════════════════════════════════════════════════

const FILE_NUM = 1;
const PRODOTTI_PATH = __dirname + `/files/${FILE_NUM}_todo.json`;
const OUTPUT_PATH = __dirname + `/files/${FILE_NUM}_done.json`;
const PROGRESS_PATH = __dirname + `/files/${FILE_NUM}_progress.json`;

// API Key da variabile d'ambiente
const API_KEY =
    process.env.GEMINI_API_KEY || 'AQ.Ab8RN6KkXQKPox1i0WGRK_tLsThhz4WAPmpBRLRxGgZGI1L41w';
    //'AQ.Ab8RN6IITcwZl5EoNxVxCTduS-RaRiQR2owioe6x6OgzVaAmkg'
// "AQ.Ab8RN6K_iECj-ahsGbuseWDl9sBZhLL9fpHOzWVxkvt3GhE-2g";
if (!API_KEY) {
    console.error("❌ Errore: imposta la variabile d'ambiente GEMINI_API_KEY");
    console.error("   Esempio: GEMINI_API_KEY=la_tua_key node script.js");
    process.exit(1);
}

// Modelli disponibili (gratuiti). Il primo funzionante verrà usato.
// gemini-1.5-flash = gratis, 15 RPM, 1M token/min
// gemini-2.0-flash = gratis, più recente, stessi limiti
const MODEL_CANDIDATES = [
    "gemini-2.5-flash", // ← più recente, 1M input / 65K output
    "gemini-2.5-flash-lite", // ← versione lite del 2.5
    "gemini-flash-latest", // ← alias dinamico all'ultimo Flash
    "gemini-2.0-flash-lite", // ← se il 2.5 è in overload
    "gemini-2.0-flash-lite-001", // ← versione pinned del lite,
    "gemini-3.6-flash"
];

const BATCH_SIZE = 2;
const DELAY_MS = 4000; // 15 RPM = 1 chiamata ogni 4s
const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 5000;

// ═══════════════════════════════════════════════════════════════
// PROMPT SEO (invariato)
// ═══════════════════════════════════════════════════════════════

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

function extractJSON(text) {
    // Prova 1: estrai da markdown code block ```json ... ```
    const mdMatch = text.match(/```(?:json)?\s*([\s\S]*?)```/);
    if (mdMatch) {
        try {
            return JSON.parse(mdMatch[1].trim());
        } catch (e) { /* fallback */ }
    }

    // Prova 2: cerca primo { e ultimo }
    const first = text.indexOf('{');
    const last = text.lastIndexOf('}');
    if (first !== -1 && last !== -1 && last > first) {
        try {
            return JSON.parse(text.substring(first, last + 1));
        } catch (e) { /* fallback */ }
    }

    // Prova 3: pulisci e riprova
    const cleaned = text
        .replace(/```json\s*/gi, '')
        .replace(/```\s*/gi, '')
        .trim();
    const f = cleaned.indexOf('{');
    const l = cleaned.lastIndexOf('}');
    if (f !== -1 && l !== -1 && l > f) {
        return JSON.parse(cleaned.substring(f, l + 1));
    }

    throw new Error('Nessun oggetto JSON valido trovato nella risposta');
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
// LISTA MODELLI DISPONIBILI (utility)
// ═══════════════════════════════════════════════════════════════

async function listAvailableModels() {
    console.log("🔍 Controllo modelli disponibili con la tua API key...\n");
    try {
        const response = await fetch(
            `https://generativelanguage.googleapis.com/v1beta/models?key=${API_KEY}`,
        );
        const data = await response.json();

        if (data.error) {
            console.error("❌ Errore API:", data.error.message);
            return;
        }

        const generativeModels = data.models.filter((m) =>
            m.supportedGenerationMethods?.includes("generateContent"),
        );

        console.log(
            `✅ Trovati ${generativeModels.length} modelli con generateContent:\n`,
        );
        generativeModels.forEach((m) => {
            const name = m.name.replace("models/", "");
            console.log(`   • ${name}`);
            console.log(`     Display: ${m.displayName}`);
            console.log(
                `     Input tokens: ${m.inputTokenLimit}, Output: ${m.outputTokenLimit}`,
            );
            console.log("");
        });

        console.log(
            "💡 Suggerimento: copia uno dei nomi sopra e usalo come MODEL_CANDIDATES[0]",
        );
    } catch (err) {
        console.error("❌ Errore nel recupero modelli:", err.message);
    }
}

// ═══════════════════════════════════════════════════════════════
// GEMINI CLIENT CON FALLBACK
// ═══════════════════════════════════════════════════════════════

const genAI = new GoogleGenerativeAI(API_KEY);
let ACTIVE_MODEL = null;

async function detectWorkingModel() {
    console.log("🔧 Verifica modelli disponibili...\n");

    for (const modelName of MODEL_CANDIDATES) {
        try {
            const model = genAI.getGenerativeModel({ model: modelName });
            // Test rapido con un prompt minimo
            await model.generateContent("Ciao");
            console.log(`   ✅ Modello attivo: ${modelName}\n`);
            ACTIVE_MODEL = modelName;
            return modelName;
        } catch (err) {
            if (err.message?.includes("404")) {
                console.log(`   ❌ ${modelName} → 404 Non trovato`);
            } else {
                console.log(`   ⚠️  ${modelName} → ${err.message}`);
            }
        }
    }

    console.error("\n💥 Nessun modello funzionante trovato!");
    console.error("   Esegui: node script.js --list");
    console.error(
        "   per vedere quali modelli sono disponibili con la tua API key.",
    );
    process.exit(1);
}

async function callGemini(batch, attempt = 1) {
    const model = genAI.getGenerativeModel({
        model: ACTIVE_MODEL,
        generationConfig: {
            temperature: 0.1, // più basso = più coerente
            topP: 0.95,
            topK: 40,
            maxOutputTokens: 65536, // massimo per gemini-flash-latest
            responseMimeType: "application/json", // ← FORZA JSON VALIDO
        },
    });

    const fullPrompt = PROMPT_BASE + "\n\n" + JSON.stringify(batch, null, 2);

    try {
        console.log(`   📝 Prompt ~${Math.round(fullPrompt.length / 4)} tokens`);
        const result = await model.generateContent(fullPrompt);
        const response = await result.response;
        const text = response.text();
        const json = typeof text === 'object' ? text : JSON.parse(text);

        return json;
    } catch (error) {
        console.error(
            `   ❌ Errore (tentativo ${attempt}/${MAX_RETRIES}):`,
            error.message,
        );

        if (attempt < MAX_RETRIES) {
            const waitTime = RETRY_DELAY_MS * attempt;
            console.log(`   ⏳ Attendo ${waitTime / 1000}s prima del retry...`);
            await sleep(waitTime);
            return callGemini(batch, attempt + 1);
        }

        throw error;
    }
}

// ═══════════════════════════════════════════════════════════════
// MAIN
// ═══════════════════════════════════════════════════════════════

async function main() {
    // Se l'utente passa --list, mostra solo i modelli disponibili
    if (process.argv.includes("--list")) {
        await listAvailableModels();
        return;
    }

    console.log("🚀 Avvio ottimizzazione SEO prodotti Shopify");
    console.log(`📁 File input: ${PRODOTTI_PATH}`);
    console.log(`📦 Batch size: ${BATCH_SIZE} prodotti`);
    console.log(`⏱️  Delay tra batch: ${DELAY_MS}ms\n`);

    // Trova il primo modello funzionante
    await detectWorkingModel();

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
        console.log(`Batch ids ${batchIds}`);
        const batchNum = i + 1;
        const totalBatches = batches.length;

        console.log(`\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━`);
        console.log(
            `📦 Batch ${batchNum}/${totalBatches} | Prodotti: ${batchIds.join(", ")}`,
        );

        try {
            const result = await callGemini(batch);

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
            failCount += batch.length;
        }

        saveProgress(progress);

        if (i < batches.length - 1) {
            console.log(`   ⏳ Rate limit: attesa ${DELAY_MS / 1000}s...`);
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
