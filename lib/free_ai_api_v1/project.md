# free_ai_api - Specifiche Progetto (v2)

## Obiettivo

Sviluppare una libreria JavaScript/Node.js chiamata `free_ai_api`, progettata per effettuare chiamate verso API di Intelligenza Artificiale gratuite.

L'obiettivo principale è massimizzare l'utilizzo delle quote gratuite offerte dai vari provider, gestendo automaticamente API Key, modelli e provider senza che il chiamante debba preoccuparsene.

Inizialmente verrà implementato solamente **Gemini**, ma tutta l'architettura deve essere progettata fin dall'inizio per supportare facilmente altri provider come:

- Gemini
- OpenAI
- Groq
- Claude
- Mistral
- DeepSeek
- OpenRouter
- qualsiasi altro provider futuro

**Requisito chiave:** l'aggiunta di un nuovo provider non deve richiedere modifiche al codice principale della libreria.

---

## Struttura del progetto

La libreria deve essere organizzata in modo modulare e con responsabilità chiaramente separate:

```
free_ai_api/
│
├── index.js                  // Punto di ingresso e orchestratore pubblico
├── constants.js              // Configurazione statica e default
├── commons.js                // Helper generici e funzioni condivise
├── registry.js               // Caricamento automatico di provider e adapter
│
├── core/
|   |-- prover-client.js     // nuovo componente 
│   ├── router.js             // Logica di fallback: provider → modello → key
│   ├── key-pool.js           // Gestione circolare key, cooldown, stato, limiti
│   └── logger.js             // Logger configurabile e iniettabile
│
├── adapters/                 // SOLO logica HTTP e trasformazione payload
│     ├── gemini.js
│     ├── openai.js
│     ├── groq.js
│     └── ...
│
├── providers/                // Logica di business: errori specifici, routing key
│     ├── gemini.js
│     ├── openai.js
│     ├── groq.js
│     └── ...
│
└── utils/
      ├── retry.js
      ├── errors.js
      └── ...
```

### Separazione Adapter / Provider

- **`adapters/`**: contiene SOLO la logica di chiamata HTTP verso l'endpoint del provider. Sa come costruire il payload, gestire l'`AbortController`, parsare la risposta. Espone un'interfaccia uniforme.
- **`providers/`**: contiene la logica di business specifica del provider: interpretazione degli errori (es. formato del 429), mapping modelli, delega all'adapter corretto, interfacciamento con il `KeyPool`.

In questo modo, se un provider cambia il formato degli errori o l'URL base, si modifica solo il suo file in `providers/`, senza toccare l'`adapter`.

---

## Configurazione

Tutti i provider devono essere definiti in `constants.js`. Ogni provider ha una configurazione indipendente.

### Esempio configurazione provider

```js
const PROVIDERS = {

    gemini: {
        enabled: true,
        priority: 1,              // Ordine di preferenza quando non specificato (più basso = più prioritario)
        endpoint: "https://generativelanguage.googleapis.com/v1beta",

        models: [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-flash-latest",
            "gemini-2.0-flash-lite"
        ],

        apiKeys: [
            {
                id: "key_1",      // Identificativo interno
                value: process.env.GEMINI_KEY_1,  // Da variabile d'ambiente
                rpmLimit: 24,     // Requests per minute
                dailyLimit: 1500, // Requests per day
                cooldownMs: 60000 // Cooldown dopo un 429 (1 minuto)
            },
            {
                id: "key_2",
                value: process.env.GEMINI_KEY_2,
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            }
        ]
    },

    groq: {
        enabled: true,
        priority: 2,
        endpoint: "https://api.groq.com/openai/v1",
        models: ["llama3-8b-8192", "mixtral-8x7b-32768"],
        apiKeys: [
            {
                id: "groq_1",
                value: process.env.GROQ_KEY_1,
                rpmLimit: 30,
                dailyLimit: 14400,
                cooldownMs: 30000
            }
        ]
    }

};
```

### Note sulla configurazione

- **`priority`**: definisce l'ordine di preferenza tra provider quando l'utente non ne specifica uno. Più basso il numero, più alto la priorità.
- **`value` delle key**: deve supportare variabili d'ambiente. Le API Key non devono mai essere hardcoded nel repository.
- **`rpmLimit` / `dailyLimit`**: il `KeyPool` deve tracciare i contatori in memoria per evitare di invocare una key che sta per esaurire il limite, riducendo i 429.

---

## API pubblica

La libreria deve esportare una funzione chiamata:

```js
freeCallApi(options)
```

dove `options` può contenere:

```js
{
    prompt: "...",              // OBBLIGATORIO
    provider: "gemini",         // opzionale
    model: "gemini-2.5-flash",  // opzionale
    temperature: 0.7,           // opzionale
    maxTokens: 4000,            // opzionale
    logger: customLogger        // opzionale - logger iniettabile (default: console)
}
```

L'unico parametro obbligatorio è il `prompt`.

---

## Comportamento richiesto

### Caso 1 - Provider e modello specificati

L'utente specifica `provider` e `model`.

L'algoritmo deve provare **solamente quel modello** utilizzando tutte le API Key disponibili per quel provider, in ordine Round Robin.

### Caso 2 - Solo provider specificato

L'utente specifica solo il `provider`.

L'algoritmo deve provare automaticamente **tutti i modelli disponibili** del provider, in ordine di definizione in `constants.js`.

Per ogni modello deve provare tutte le API Key disponibili.

### Caso 3 - Nessun parametro specificato

L'utente non specifica nulla.

L'algoritmo deve cercare automaticamente una combinazione funzionante seguendo questo ordine:

```
Provider (per priority crescente)
    ↓
Modello (per ordine di definizione)
    ↓
API Key (Round Robin, saltando quelle in cooldown o invalide)
```

Esempio:

```
Gemini (priority 1)
    gemini-2.5-flash
        key1
        key2
        key3
    gemini-2.5-flash-lite
        key1
        key2
Groq (priority 2)
    llama3-8b-8192
        key1
```

**Non appena trova una combinazione funzionante deve interrompere immediatamente la ricerca.**

---

## Gestione automatica degli errori

La libreria deve distinguere gli errori e reagire di conseguenza:

| Errore | Comportamento |
|--------|---------------|
| **Rate Limit (429)** | Cambiare immediatamente API Key. La key viene messa in cooldown per il tempo configurato (`cooldownMs`). |
| **Too Many Requests** | Cambiare API Key (stesso comportamento del 429). |
| **Quota esaurita** | Cambiare API Key. |
| **Timeout** | Riprovare una sola volta con la stessa key usando `AbortController`. Se fallisce ancora: cambiare API Key. |
| **Errore 500** | Cambiare modello (stesso provider, modello successivo). |
| **Modello inesistente / 404 su modello** | Cambiare automaticamente modello. |
| **API Key non valida (401/403)** | La chiave deve essere marcata come **non utilizzabile per tutta l'esecuzione della funzione**. Non deve più essere riprovata. |
| **Prompt troppo lungo (413 / errore specifico)** | Restituire immediatamente errore terminale. Non fare retry. |
| **Nessuna combinazione disponibile** | Se tutti i provider, tutti i modelli e tutte le API Key sono stati provati senza successo, la funzione deve restituire un errore esplicativo con il dettaglio di tutti i tentativi. |

---

## Ottimizzazioni richieste

# Prompt Optimization & Intelligent Cache Strategy

## Obiettivo

Gestire prompt lunghi in modo intelligente evitando chiamate ripetitive ai modelli AI quando:

- Le istruzioni del prompt sono identiche.
- Cambia solamente l'input dinamico.
- Il prompt ottimizzato è già disponibile in cache.
- Il limite massimo di token non viene superato.

L'obiettivo è trasformare `free_ai_api` in un AI Gateway capace di ridurre automaticamente il consumo di token.

---

# Problema attuale

Attualmente ogni richiesta viene inviata al modello AI per essere ottimizzata.

Esempio:

Prompt:
Aggiungimi la chiave toDeleted in questo array di oggetti

Input:
[
{
test:1
},
{
test:1
},
...
]



Se arriva una seconda richiesta con la stessa istruzione ma un array diverso:

Prompt:
Aggiungimi la chiave toDeleted in questo array di oggetti

Input:
[
{
test:2
},
{
test:3
}
]



la chiamata a Gemini viene ripetuta inutilmente.

Il modello deve rielaborare un'istruzione già conosciuta.



---

# Soluzione proposta

Separare il prompt in tre livelli:

SYSTEM / RULES
|
|
PROMPT TEMPLATE (CACHE)
|
|
INPUT DINAMICO


## Esempio

### Prompt Template
Aggiungimi la chiave toDeleted in questo array di oggetti


### Input dinamico

```json
[
 {
   "test":1
 },
 {
   "test":1
 }
]


La parte che deve essere ottimizzata è solo il template.

free_ai_api

├── cache
│   ├── promptCache.json
│   └── tokenCache.json
│
├── core
│   ├── PromptManager.js
│   ├── PromptCache.js
│   ├── PromptOptimizer.js
│   └── TokenManager.js
│
└── providers
    ├── gemini.js
    ├── openai.js
    └── claude.js

Prompt Hash System

Ogni istruzione deve avere un identificativo univoco.

Esempio:

Input:

Aggiungimi la chiave toDeleted in questo array di oggetti

Generazione hash:
9ab32f83ad92...

Questo diventa l'identificativo della cache.

Generazione Hash

const crypto = require("crypto");


function generatePromptHash(prompt){

    return crypto
        .createHash("sha256")
        .update(prompt)
        .digest("hex");

}

Prompt Cache

Struttura:

{
    "9ab32f83ad92": {

        "original":
        "Aggiungimi la chiave toDeleted in questo array di oggetti",

        "optimized":
        "Aggiungi il campo toDeleted booleano agli oggetti dell'array",

        "tokensBefore": 25,

        "tokensAfter": 12,

        "usageCount": 100,

        "savedTokens": 13000,

        "lastUsed":
        "2026-08-04"

    }
}

Flusso di esecuzione
Step 1 - Ricezione richiesta

Input:
{
 instruction:
 "Aggiungimi la chiave toDeleted",

 data:
 [
   {...}
 ]
}

Step 2 - Generazione Hash

Il sistema genera:
promptHash = SHA256(instruction)

Step 3 - Ricerca Cache

Controllo:

promptHash esiste?

Caso A - Cache HIT

La cache contiene:

{
 optimized:
 "Aggiungi il campo toDeleted agli oggetti"
}

Risultato:

NON chiamare Gemini

Utilizzare direttamente:

Prompt ottimizzato

+

Nuovo input
Caso B - Cache MISS

Il sistema esegue:

Prompt originale

        |
        v

Gemini Optimizer

        |
        v

Prompt ottimizzato

        |
        v

Salvataggio cache
Token Management

Ogni provider ha un limite massimo.

Esempio:

Gemini Context Window:

100000 token

Prima della chiamata:

TOKEN TOTALI =
prompt
+
input
Caso OK
Prompt:
20 token

Input:
50000 token

Totale:
50020 token

La richiesta può partire.

Caso KO
Prompt:
20 token

Input:
150000 token

Totale:
150020 token

Il sistema deve fare:

Input troppo grande

        |
        v

Chunk Manager

        |
        v

Divisione dati
Chunk Processing

Esempio:

Input:

10000 oggetti

Divisione:

Chunk 1:
1000 elementi


Chunk 2:
1000 elementi


Chunk 3:
1000 elementi

...

Ogni chunk viene processato singolarmente.

Prompt Similarity (fase avanzata)

L'hash funziona solo se il prompt è identico.

Esempio:

Prompt 1:

Aggiungi il campo toDeleted agli oggetti

Prompt 2:

Inserisci una proprietà toDeleted dentro ogni elemento

Semanticamente sono uguali.

Soluzione:

Usare embeddings.

Salvare:

{
 "prompt":
 "Aggiungi il campo toDeleted",

 "embedding":
 [
   0.234,
   0.543,
   ...
 ],

 "optimized":
 "Aggiungi proprietà toDeleted"
}

Poi:

Cosine Similarity

> 0.95

=

stesso significato

Riutilizzare cache.

PromptManager API

Esempio utilizzo:

const result =
await PromptManager.optimize({

    instruction:
    "Aggiungimi la chiave toDeleted",

    input:data

});

Internamente:

PromptManager

1.
Normalizza prompt


2.
Genera hash


3.
Controlla cache


4.
Controlla token


5.
Chiama optimizer se necessario


6.
Salva risultato


7.
Restituisce prompt finale
Metriche

Ogni prompt deve salvare:

{
    "usageCount":452,

    "tokensSaved":120000,

    "averageReduction":65,

    "lastExecution":
    "2026-08-04"
}

Questo permette di capire:

Quali prompt vengono usati di più.
Quanti token vengono risparmiati.
Quale ottimizzazione porta più valore.
Roadmap Implementazione
Fase 1 - Base

Priorità alta:

PromptCache
PromptHash
PromptManager
TokenManager
Fase 2 - Ottimizzazione
PromptOptimizer
Compressione automatica
Statistiche utilizzo
Fase 3 - AI avanzata
Embedding similarity
Semantic cache
Auto clustering dei prompt


## Gestione automatica dei prompt troppo lunghi

La libreria deve gestire automaticamente i prompt che superano il limite di token supportato dai vari modelli.

### Nuova opzione pubblica

L'API pubblica deve supportare una nuova proprietà:

```javascript
const response = await freeCallApi({
    prompt: "Spiegami cos'è la relatività.",
    compress: true // opzionale, default: true
});
```

La proprietà `compress` permette all'utente di decidere se autorizzare la libreria a comprimere automaticamente il prompt quando necessario.

---

### Caso 1 - `compress: false`

Se l'utente imposta:

```javascript
compress: false
```

la libreria **non deve modificare il prompt originale**.

Prima di effettuare qualsiasi chiamata API dovrà:

1. Stimare il numero di token del prompt.
2. Confrontarlo con il limite massimo supportato dal modello.
3. Se il prompt supera il limite del modello, non effettuare alcuna chiamata API.
4. Passare automaticamente al modello successivo disponibile.
5. Continuare fino a trovare un modello compatibile.
6. Se nessun modello supporta quel numero di token, restituire immediatamente un errore esplicativo.

L'obiettivo è evitare chiamate inutili verso modelli che fallirebbero sicuramente.

---

### Caso 2 - `compress: true` (default)

Se `compress` è `true` oppure non viene specificato, la libreria deve tentare automaticamente di comprimere il prompt.

Il flusso deve essere il seguente:

1. Stimare il numero di token del prompt.
2. Individuare il modello che verrà utilizzato secondo la normale logica di routing.
3. Se il prompt rientra nel limite del modello, inviarlo normalmente.
4. Se invece supera il limite:
   - individuare automaticamente un modello che supporti un contesto sufficientemente grande da ricevere il prompt originale;
   - inviare il prompt completo a tale modello chiedendo di comprimerlo semanticamente;
   - specificare che il prompt risultante dovrà essere compatibile con il limite di token del modello di destinazione;
   - utilizzare il prompt compresso per effettuare la chiamata al modello inizialmente selezionato.

La compressione deve essere **semantica**, non testuale. Devono essere preservate tutte le informazioni importanti eliminando solamente ridondanze, ripetizioni e contenuti non essenziali.

---

### Configurazione dei modelli

Ogni modello definito in `constants.js` dovrà contenere anche il numero massimo di token supportati dal proprio context window.

Esempio:

```javascript
const MODEL_LIMITS = {
    "gemini-flash-latest": { 
        contextWindow: 1048576,
        maxInputTokens: 1048576   // Gemini accetta l'intero context
    },
    "openai/gpt-oss-120b": { 
        contextWindow: 131072,
        maxInputTokens: 8000      // Groq TPM limit reale per richiesta
    }
};
```

La libreria utilizzerà queste informazioni per decidere automaticamente:

- se il prompt è compatibile;
- se deve cambiare modello;
- se deve comprimerlo;
- oppure se deve restituire un errore senza effettuare alcuna chiamata.

---

### Requisiti

- La stima dei token deve essere effettuata **prima** di qualsiasi chiamata API.
- Non devono essere effettuate chiamate verso modelli che non possono gestire il prompt.
- La logica deve essere completamente trasparente per il chiamante.
- L'aggiunta di un nuovo provider non deve richiedere modifiche all'algoritmo.
- La soluzione deve essere modulare, estendibile e indipendente dal provider.

---

### Analisi richiesta

Prima di implementare questa funzionalità:

1. Analizza la soluzione proposta.
2. Evidenzia eventuali problemi architetturali.
3. Suggerisci eventuali strategie migliori per ridurre il numero di chiamate API e massimizzare l'utilizzo delle quote gratuite.
4. Se ritieni che esista una soluzione migliore, implementa quella motivando le scelte progettuali.

### Round Robin

Le API Key devono essere utilizzate in modo circolare per distribuire il carico:

```
Richiesta 1 → key1
Richiesta 2 → key2
Richiesta 3 → key3
Richiesta 4 → key1
```

### Cooldown

Se una API Key riceve un errore di Rate Limit (429), non deve essere riprovata immediatamente. Deve essere messa in cooldown per il tempo configurato (`cooldownMs`). Durante il cooldown deve essere ignorata dal Round Robin.

### Tracciamento limiti (RPM / Daily)

Il `KeyPool` deve tracciare in memoria:
- Numero di richieste effettuate nell'ultimo minuto (per `rpmLimit`)
- Numero di richieste effettuate nell'ultimo giorno (per `dailyLimit`)

Se una key sta per superare uno di questi limiti, viene saltata *prima* di effettuare la chiamata, riducendo i 429 preventivamente.

### Cache stato API Key

Durante l'esecuzione della funzione `freeCallApi`, la libreria deve ricordarsi quali API Key:
- sono valide
- sono in cooldown
- sono invalide (401/403)
- hanno esaurito i limiti

evitando chiamate inutili.

### Retry automatico

Per errori temporanei (timeout, rete, connessione) effettuare **un solo retry** prima di cambiare API Key. Usare `AbortController` per gestire correttamente il timeout e poter annullare la richiesta HTTP.

### Logging

La libreria deve prevedere un sistema di logging configurabile e **iniettabile**.

Ogni chiamata dovrebbe registrare almeno:
- provider utilizzato
- modello utilizzato
- API Key utilizzata (mascherata, es. `key_1...last4`)
- tempo di risposta
- eventuale retry effettuato
- motivo del fallback (quale errore ha causato il passaggio a key/modello successivo)
- errore finale (se tutti i tentativi falliscono)

Il logger di default è `console`, ma deve essere possibile passarne uno personalizzato via `options.logger`.

---

## Estendibilità

L'aggiunta di un nuovo provider deve richiedere **solamente**:

1. Aggiungere la configurazione in `constants.js`
2. Creare il relativo adapter in `adapters/{provider}.js`
3. Creare il file del provider in `providers/{provider}.js`

**Senza modificare `index.js`.**

### Registry automatico

`registry.js` deve scansionare automaticamente le cartelle `adapters/` e `providers/` al bootstrap e registrare tutti i provider trovati. Non deve essere necessario importare manualmente ogni nuovo provider in `index.js`.

### Adapter Pattern

Ogni adapter deve implementare la stessa interfaccia:

```js
async function call({
    prompt,
    model,
    apiKey,
    temperature,
    maxTokens,
    abortSignal
})
```

In questo modo `index.js` e i file in `providers/` non dovranno conoscere le differenze tra Gemini, OpenAI o altri provider.

---

## Obiettivi finali

La libreria deve essere:

- **modulare** - ogni componente ha una responsabilità singola
- **facilmente estendibile** - nuovo provider = 3 file, zero modifiche al core
- **indipendente dal provider** - grazie all'Adapter Pattern
- **resiliente agli errori** - gestione automatica di rate limit, timeout, key invalide
- **ottimizzata per sfruttare al massimo le quote gratuite** - round robin, cooldown, tracciamento limiti
- **semplice da utilizzare** - un'unica funzione pubblica
- **completamente asincrona** - async/await ovunque
- **facilmente manutenibile** - codice separato e ben documentato
- **pronta ad aggiungere nuovi provider** senza modificare il codice esistente

### Esempio d'uso finale

```js
const { freeCallApi } = require('free_ai_api');

const response = await freeCallApi({
    prompt: "Spiegami cos'è la relatività."
});
```

Questa semplice chiamata deve essere sufficiente affinché la libreria:
1. Selezioni automaticamente il miglior provider disponibile (per priority)
2. Scelga il miglior modello
3. Usi la migliore API Key disponibile (Round Robin)
4. Gesti in autonomia retry, fallback, rate limit, quote esaurite
5. Restituisca la risposta o un errore esplicativo

**Senza che il codice chiamante debba occuparsi di alcuna logica aggiuntiva.**

## Stato implementazione

### Completato


## -------------------------------------------


