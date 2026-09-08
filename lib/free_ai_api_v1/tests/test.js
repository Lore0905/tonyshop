const test = require('node:test');
const assert = require('node:assert/strict');
const { writeFile } = require('fs/promises');
const { freeCallApi } = require('../index');
const { PROVIDERS } = require('../constants');

const promptText = `Agisci come **Senior E-Commerce SEO Specialist e Conversion Copywriter** esperto di Shopify, SEO, Google Shopping, SEO semantica e CRO. Settore: **[INSERISCI SETTORE]**.

Riceverai un array JSON di prodotti. Trasforma ogni prodotto in una scheda SEO naturale, utile e orientata alla conversione.

## REGOLE PRIORITARIE

Usa **SOLO** dati presenti nell'input.

Non modificare, correggere o reinterpretare: Codice prodotto, Riferimento, SKU, EAN, prezzi, quantità, URL, numeri, specifiche, compatibilità, marche, modelli e codici.

**NON INVENTARE MAI** caratteristiche, materiali, dimensioni, colori, prestazioni, compatibilità, anni, certificazioni, omologazioni, garanzie, spedizioni, resi, disponibilità, accessori, promozioni o vantaggi non dimostrabili.

Se un dato manca, omettilo.

Priorità:
**accuratezza > non invenzione > JSON valido > chiarezza > search intent > SEO > conversione > lunghezza.**

Scrivi in italiano naturale e professionale. Evita keyword stuffing, ripetizioni e affermazioni generiche. Usa sinonimi, varianti e termini semanticamente correlati quando supportati dal prodotto. Preferisci termini comprensibili dagli utenti senza alterare le specifiche tecniche.

Non usare ALL CAPS, salvo sigle/unità corrette.

## ANALISI INTERNA

Prima dell'output identifica mentalmente:

* keyword principale;
* keyword secondarie/long-tail;
* search intent, privilegiando transazionale/commerciale;
* prodotto, categoria, marca/modello, tipologia, specifiche e compatibilità;
* eventuale USP, solo se supportata dai dati.

Non mostrare questa analisi.

## OUTPUT

Restituisci **SOLO JSON valido**, senza markdown, commenti o testo esterno.

La chiave principale deve essere "Codice prodotto" convertito in stringa.

Ogni prodotto deve avere esattamente:

{
"3538": {
"nome": "...",
"sommario": "...",
"descrizione": "...",
"meta_title": "...",
"meta_description": "...",
"target_keywords": ["...", "...", "...", "...", "..."],
"h1_suggestion": "...",
"url_handle_suggestion": "...",
"image_alt_text": "...",
"faq_schema": [
{"question": "...", "answer": "..."},
{"question": "...", "answer": "..."},
{"question": "...", "answer": "..."}
]
}
}

## CAMPI

**nome**

* Preferibilmente 50-70 caratteri, massimo 100.
* Keyword principale naturale.
* Includi specifiche/compatibilità disponibili quando utili.
* Non copiare semplicemente il nome originale.

**sommario**

* Un solo <p>.
* Circa 150-250 caratteri.
* 2-3 frasi.
* Spiega cosa è e a cosa serve.
* Keyword principale naturale.

**descrizione**

* Indicativamente 300-600 parole solo se i dati lo consentono.
* Non aggiungere testo artificiale.
* Struttura obbligatoria:

  1. introduzione;
  2. caratteristiche principali;
  3. specifiche tecniche;
  4. perché scegliere il prodotto;
  5. chiusura all'acquisto.
* Quando possibile, almeno 5 bullet.
* Trasforma **caratteristica → utilità → beneficio** solo se il beneficio è supportato dai dati.
* Riporta fedelmente le specifiche.
* Non creare informazioni mancanti.

HTML consentito/preferito:
<p> <h3> <ul> <li> <strong> <table> <thead> <tbody> <tr> <th> <td>

Niente CSS inline, classi, JavaScript o <div> inutili.

**meta_title**

* Massimo 60 caratteri.
* Keyword principale vicino all'inizio.
* Diverso dall'H1.

**meta_description**

* Target 140-160 caratteri, massimo 160.
* Keyword principale naturale.
* Descrittiva e orientata al click.
* Nessuna promozione/garanzia/spedizione/urgenza inventata.

**target_keywords**
Genera **esattamente 5 keyword**:

1. principale;
2. long-tail principale;
3. long-tail secondaria;
4. variante semantica;
5. commerciale/specifica.

**h1_suggestion**

* Diverso dal meta title.
* Massimo 70 caratteri.
* Descrittivo e naturale.
* Keyword principale quando possibile.

**url_handle_suggestion**
Slug SEO-friendly:

* minuscolo;
* parole separate da \`-\`;
* niente accenti/caratteri speciali;
* niente codici casuali;
* niente keyword duplicate;
* mantieni specifiche importanti.

**image_alt_text**

* Massimo 125 caratteri.
* Descrittivo e basato sui dati disponibili.
* Keyword principale quando naturale.
* Non usare "immagine di".
* Non inventare dettagli visivi.

**faq_schema**
Genera **esattamente 3 FAQ** pertinenti al prodotto.

* Domande basate sui dati disponibili.
* Risposte concise, massimo 150 caratteri.
* Non inventare informazioni.

## VALIDAZIONE

Prima dell'output verifica:

* JSON valido;
* tutti i Codici prodotto presenti come chiavi;
* dati originali invariati;
* zero informazioni inventate;
* nome ≤100;
* meta_title ≤60;
* meta_description ≤160;
* esattamente 5 keyword;
* esattamente 3 FAQ;
* H1 diverso dal meta title;
* URL valido;
* ALT ≤125;
* HTML valido;
* nessun markdown;
* nessun keyword stuffing;
* nessuna informazione commerciale inventata.

**OUTPUT: SOLO JSON VALIDO.**

Input:
`;
const input = [
  {
    "Codice prodotto": 3538,
    "Riferimento": "V3538",
    "Nome": "CERCHIO POSTERIORE 110/90-18 PERNO 25 PER CROSS KAYO T4 250cc",
    "Sommario": "<p>402000-0049</p>",
    "Prezzo (Tasse Escluse)": 122.942623,
    "Prezzo (Tasse Incluse)": 149.99,
    "Nomi delle categorie (x,y,z...)": "Home,Ricambi,Cerchi e mozzi,Ricambi per tipologia",
    "Quantità": 7,
    "URL immagine di copertina": "https://autofantasy.it/img/p/4/3/2/1/4321.jpg",
    "tag": "ricambi-gomme"
  },
  {
    "Codice prodotto": 3539,
    "Riferimento": "E3539",
    "Nome": "PARAFANGO ANTERIORE MONOPATTINO ELETTRICO CHAOS SERIE GOLD",
    "Prezzo (Tasse Escluse)": 9.827869,
    "Prezzo (Tasse Incluse)": 11.99,
    "Nomi delle categorie (x,y,z...)": "Home,Ricambi,Ricambi veicoli elettrici,Monopattini",
    "Quantità": 46,
    "URL immagine di copertina": "https://autofantasy.it/img/p/4/3/2/3/4323.jpg",
    "tag": "ricambi-altro"
  }]





const freeApiObj = {
    instruction : promptText,
    input: input
}

const hasApiKey = Object.values(PROVIDERS).some(provider =>
    provider.apiKeys.some(key => Boolean(key.value))
);
test('integrazione SEO: freeCallApi restituisce JSON per tutti i prodotti', {
    skip: hasApiKey ? false : 'Nessuna API key configurata nel file .env'
}, async () => {
    const freeApi = await freeCallApi(freeApiObj);

    assert.equal(typeof freeApi.text, 'string', 'La risposta deve contenere text');
    assert.ok(freeApi.text.trim(), 'La risposta non deve essere vuota');
    assert.ok(freeApi.provider, 'Deve essere indicato il provider utilizzato');
    assert.ok(freeApi.model, 'Deve essere indicato il modello utilizzato');

    const cleanText = freeApi.text

    await writeFile(`${__dirname}/response.json`, JSON.stringify({
        instruction: freeApiObj.instruction,
        input: freeApiObj.input,
        response: JSON.parse(cleanText),
    }, null, 2));
});
