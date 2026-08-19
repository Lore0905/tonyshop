# free_ai_api — Documentazione Progetto (v2.1)

> Libreria Node.js per chiamate API AI gratuite con **fallback automatico**, **gestione multi-key**, **ottimizzazione prompt** e **compressione semantica**.

---

## 1. Panoramica

`free_ai_api` è un AI Gateway che massimizza l'utilizzo delle quote gratuite offerte dai provider (Gemini, Groq, ecc.) gestendo automaticamente:

- **Routing intelligente** tra provider, modelli e API Key
- **Round Robin** e **cooldown** delle chiavi
- **Cache ottimizzata** dei prompt template
- **Compressione semantica** automatica dei prompt troppo lunghi
- **Chunking** preventivo degli array di input
- **Retry** e **fallback** trasparenti

L'obiettivo è offrire un'unica funzione pubblica — `freeCallApi()` — che nasconde ogni complessità infrastrutturale.

---

## 2. Installazione & Avvio

```bash
npm install
```

Crea un file `.env` nella root:

```env
GEMINI_KEY_1=...
GEMINI_KEY_2=...
GROQ_KEY_1=...
```

Esempio d'uso:

```js
const { freeCallApi } = require('free_ai_api');

const response = await freeCallApi({
    prompt: "Spiegami la relatività ristretta"
});
```

---

## 3. Architettura

```
free_ai_api/
│
├── index.js                  # Punto di ingresso pubblico
├── constants.js              # Configurazione statica (provider, limiti, cache)
├── commons.js                # Helper generici (sleep, maskString)
├── registry.js               # Caricamento automatico adapter/provider
│
├── core/
│   ├── router.js             # Orchestratore di routing e fallback
│   ├── provider-client.js    # Esecutore uniforme delle chiamate provider
│   ├── key-pool.js           # Gestione circolare key, cooldown, limiti RPM/daily
│   ├── token-manager.js      # Stima token e calcolo limiti effettivi
│   ├── compressor.js         # Compressione semantica via AI
│   ├── chunk-manager.js      # Suddivisione preventiva array troppo grandi
│   ├── prompt-manager.js     # Cache hash SHA256 + ottimizzazione prompt
│   ├── prompt-cache.js       # Persistenza LRU su JSON
│   ├── prompt-optimizer.js   # Ottimizzazione prompt via Gemini
│   └── logger.js             # Logger iniettabile con masking key
│
├── adapters/                 # SOLO logica HTTP e trasformazione payload
│   ├── gemini.js
│   ├── groq.js
│   └── mock.js
│
├── providers/                # Logica di business specifica del provider
│   ├── gemini.js             # preFlightCheck, parseError, logging
│   └── groq.js
│
├── utils/
│   ├── errors.js             # Classificazione errori + ErrorTypes enum
│   ├── token-estimator.js    # Euristiche stima token (text/json/code)
│   ├── prompt-normalizer.js  # Normalizzazione pre-hash
│   └── retry.js              # Wrapper retry generico
│
├── scripts/                  # CLI utility
│   ├── cache-stats.js
│   ├── cache-clear.js
│   ├── optimize-prompt.js
│   └── warmup-cache.js
│
└── tests/
    └── test.js               # Test suite compatibile
```

---

## 4. Separazione Adapter / Provider

| Livello | Responsabilità |
|---------|----------------|
| **Adapter** (`adapters/`) | Logica HTTP pura: costruisce payload, gestisce `AbortController`, parsa risposta. Espone interfaccia uniforme `call({ prompt, model, apiKey, temperature, maxTokens, abortSignal })`. |
| **Provider** (`providers/`) | Logica di business: `preFlightCheck`, interpretazione errori specifici, arricchimento metadati, delega all'adapter. |

**Vantaggio**: se un provider cambia URL o formato errori, si modifica solo il suo file.

---

## 5. Configurazione (`constants.js`)

### Provider

```js
const PROVIDERS = {
    gemini: {
        enabled: true,
        priority: 2,              // più basso = più prioritario
        endpoint: "https://generativelanguage.googleapis.com/v1beta",
        models: ["gemini-flash-latest"],
        apiKeys: [
            {
                id: "gemini_key_1",
                value: process.env.GEMINI_KEY_1,
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            }
        ]
    },
    groq: {
        enabled: true,
        priority: 1,
        endpoint: "https://api.groq.com/openai/v1",
        models: ["openai/gpt-oss-120b"],
        apiKeys: [{
            id: "groq_1",
            value: process.env.GROQ_KEY_1,
            rpmLimit: 30,
            dailyLimit: 14400,
            cooldownMs: 30000
        }]
    }
};
```

### Limiti Modelli (intrinseci)

```js
const MODEL_LIMITS = {
    "gemini-flash-latest": {
        contextWindow: 1048576,
        maxOutputTokens: 8192,
        capabilities: ["multimodal", "compression", "json_mode"]
    },
    "openai/gpt-oss-120b": {
        contextWindow: 131072,
        maxOutputTokens: 4096,
        capabilities: ["compression", "json_mode"]
    }
};
```

### Limiti Provider (infrastrutturali)

```js
const PROVIDER_LIMITS = {
    gemini: {
        maxRequestTokens: 1048576,
        tpm: 1000000,
        rpm: 60,
        supportsCompression: true,
        tpmIsRateLimit: true
    },
    groq: {
        maxRequestTokens: 8000,
        tpm: 8000,
        rpm: 30,
        supportsCompression: true,
        tpmIsRateLimit: false   // su free, TPM = limite anche singola richiesta
    }
};
```

### Cache Config

```js
const PROMPT_CACHE_CONFIG = {
    enabled: true,
    ttlMs: 7 * 24 * 60 * 60 * 1000,
    maxEntries: 1000,
    persistPath: "./cache/prompt-cache.json",
    optimizeOnMiss: true,
    geminiModelForOptimization: "gemini-flash-latest"
};
```

---

## 6. API Pubblica

```js
await freeCallApi({
    prompt: "...",              // OBBLIGATORIO (o instruction + input)
    instruction: "...",         // Template istruzione (per cache/ottimizzazione)
    input: [...],               // Dato dinamico (oggetto/array)
    provider: "gemini",         // opzionale
    model: "gemini-flash-latest", // opzionale
    temperature: 0.7,           // opzionale
    maxTokens: 4000,            // opzionale
    compress: true,             // opzionale, default: true
    logger: customLogger        // opzionale
});
```

**Restituisce:**

```js
{
    text: "...",               // Risposta testuale
    model: "...",
    provider: "...",
    raw: { ... },              // Risposta grezza provider
    fromCache: true,           // Se il prompt ottimizzato era in cache
    hash: "...",               // Hash SHA256 del prompt template
    attempts: [...]            // Log dei tentativi effettuati
}
```

---

## 7. Flusso di Esecuzione (Router)

```
1. Preparazione Prompt
   ├── Se instruction: normalizza → hash → controlla cache
   │   ├── HIT: usa prompt ottimizzato dalla cache
   │   └── MISS: chiama PromptOptimizer (Gemini) → salva in cache
   └── Altrimenti: usa prompt legacy

2. Stima Token
   └── TokenManager.estimatePromptTokens(instruction, input)

3. Chunking Preventivo (solo array)
   └── Se input[] supera limite → split in chunk → route ricorsivo

4. Risoluzione Provider/Modello
   └── Per priority crescente, per ordine modelli

5. Token Preflight
   └── canHandleRequest(provider, model, tokens)
       ├── OK → procedi
       └── KO + compress=true → Compressor comprime semanticamente
           └── Riprova canHandleRequest

6. Ciclo Key (Round Robin)
   └── Per ogni key disponibile (non cooldown, non invalid, non esaurita)
       ├── preFlightCheck provider-specifico
       ├── executeWithRetry (1 retry su timeout)
       └── SUCCESSO → ritorna risultato

7. Gestione Errori
   └── Classificazione via classifyError()
       ├── RATE_LIMIT_TPM → blocca provider temporaneamente
       ├── RATE_LIMIT_RPM / QUOTA → cooldown key
       ├── INVALID_KEY → invalida key
       ├── MODEL_NOT_FOUND / PAYLOAD_TOO_LARGE → salta modello
       └── TIMEOUT → 1 retry stessa key, poi cambia

8. Esaurimento
   └── Se tutti i provider/modelli/key falliscono → errore con dettaglio tentativi
```

---

## 8. Ottimizzazioni Implementate

### 8.1 Prompt Cache & Ottimizzazione

- **Hash SHA256** sulla `instruction` normalizzata (non sull'input dinamico)
- **Cache LRU** in-memory con TTL e persistenza su JSON
- **Ottimizzazione AI**: su cache miss, Gemini riscrive il prompt in modo conciso
- **Metriche**: token risparmiati, usage count, percentuale riduzione

### 8.2 Compressione Semantica

Quando `compress: true` (default) e il prompt supera il limite del modello target:

1. Il **Compressor** trova il modello con context window più grande (priorità a Gemini)
2. Invia il prompt completo con istruzione di compressione semantica
3. Verifica che il risultato rientri nel limite target
4. Usa il prompt compresso per la chiamata finale

**Requisito**: preserva 100% informazioni tecniche, rimuove solo ridondanze.

### 8.3 Chunking Preventivo

Per input di tipo `Array` che superano i limiti token:

- `ChunkManager.splitArrayIntoChunks()` calcola la dimensione ottimale per chunk
- Ogni chunk viene processato indipendentemente via `route()`
- I risultati testuali vengono concatenati

### 8.4 Gestione Key Avanzata (KeyPool)

- **Round Robin** circolare per distribuire il carico
- **Tracciamento RPM**: contatore sliding window (ultimo minuto)
- **Tracciamento Daily**: contatore giornaliero con reset automatico
- **Cooldown**: key messe in pausa dopo 429/TPM
- **Invalidazione**: key 401/403 marcate come non utilizzabili per l'intera esecuzione
- **Preflight**: nessuna chiamata verso modelli che non possono gestire la richiesta

---

## 9. Classificazione Errori

Il sistema distingue automaticamente:

| Errore | Tipo | Azione Router |
|--------|------|---------------|
| 429 RPM | `RATE_LIMIT_RPM` | Cooldown key (configurato) |
| 429/413 TPM (Groq free) | `RATE_LIMIT_TPM` | Blocca provider ~60s |
| Quota esaurita | `QUOTA_EXHAUSTED` | Cooldown key |
| 401/403 | `INVALID_KEY` | Invalida key permanentemente |
| 404 | `MODEL_NOT_FOUND` | Salta modello, prossimo |
| 413 Payload | `PAYLOAD_TOO_LARGE` | Salta modello |
| 413 Prompt troppo lungo | `PROMPT_TOO_LARGE` | Errore terminale (se compress=false) |
| Timeout / Abort | `TIMEOUT` | 1 retry stessa key, poi cambia |
| 500 | `UNKNOWN` | Cambia modello |

---

## 10. Registry Automatico

`registry.js` scansiona automaticamente `adapters/` e `providers/` al bootstrap:

- **Nessuna modifica a `index.js`** quando si aggiunge un provider
- Risolve esportazioni sia `module.exports = Classe` che `module.exports = { Classe }`
- Espone `getProviderClass(name)` e `getAdapter(name)`

---

## 11. Come Aggiungere un Nuovo Provider

1. **Configura** in `constants.js` (PROVIDERS, MODEL_LIMITS, PROVIDER_LIMITS)
2. **Crea adapter** in `adapters/{nome}.js` con interfaccia `call({...})`
3. **Crea provider** in `providers/{nome}.js` con `preFlightCheck` e `parseError`
4. **Fine** — il Registry lo carica automaticamente

---

## 12. Scripts CLI

| Script | Descrizione |
|--------|-------------|
| `npm run cache:stats` | Statistiche cache (hit rate, token risparmiati, top prompt) |
| `npm run cache:clear` | Svuota cache (richiede `--force`) |
| `npm run cache:warmup` | Pre-popola cache con istruzioni note |
| `npm run optimize:prompt` | Ottimizza un singolo prompt via CLI |

---

## 13. Testing

```bash
node tests/test.js
```

Test coperti:
- Stima token (text/json)
- Limiti Groq vs Gemini
- Pre-flight blocco richieste grandi
- Classificazione errori 413
- KeyPool (round robin, cooldown, invalidazione)
- ChunkManager (divisione array)
- Router (routing richieste piccole/grandi)

---

## 14. Requisiti

- **Node.js** >= 18.0.0
- **Dipendenze**: `dotenv` (env vars), `crypto` (built-in)
- **Fetch API** nativa (Node 18+)

---

## 15. Roadmap

| Fase | Stato | Feature |
|------|-------|---------|
| 1 | ✅ | PromptCache, PromptHash, PromptManager, TokenManager |
| 2 | ✅ | PromptOptimizer, compressione automatica, statistiche |
| 3 | 🔄 | Embedding similarity, semantic cache, auto-clustering prompt |
| 4 | 📋 | Supporto provider: OpenAI, Claude, Mistral, DeepSeek, OpenRouter |

---

*Documentazione aggiornata allo stato attuale del codebase (v2.1).*
