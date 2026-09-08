



=================================
FILE: adapters/gemini.js
=================================

const { DEFAULT_CONFIG } = require("../constants");

/**
 * Adapter Gemini - logica HTTP pura
 *
 * Interfaccia uniforme:
 *   call({ prompt, model, apiKey, temperature, maxTokens, abortSignal })
 */
async function call({ prompt, model, apiKey, temperature, maxTokens, abortSignal }) {
    const endpoint = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`;

    const body = {
        contents: [{ parts: [{ text: prompt }] }]
    };

    if (temperature !== undefined || maxTokens !== undefined) {
        body.generationConfig = {};
        if (temperature !== undefined) body.generationConfig.temperature = temperature;
        if (maxTokens !== undefined) body.generationConfig.maxOutputTokens = maxTokens;
    }

    const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: abortSignal
    });

    if (!response.ok) {
        const errorText = await response.text().catch(() => "");
        const error = new Error(`Gemini API error ${response.status}: ${response.statusText}. ${errorText}`);
        error.status = response.status;
        error.provider = "gemini";
        throw error;
    }

    const data = await response.json();

    let text = "";
    if (data.candidates && data.candidates[0] && data.candidates[0].content && data.candidates[0].content.parts) {
        text = data.candidates[0].content.parts.map(p => p.text).join("");
    }

    return {
        text,
        model,
        provider: "gemini",
        raw: data
    };
}

module.exports = { call };


=================================
FILE: adapters/groq.js
=================================

const { DEFAULT_CONFIG } = require("../constants");

/**
 * Adapter Groq - logica HTTP pura
 *
 * Interfaccia uniforme:
 *   call({ prompt, model, apiKey, temperature, maxTokens, abortSignal })
 *
 * Nota: l'API Groq è compatibile con il formato OpenAI (/chat/completions).
 */
async function call({ prompt, model, apiKey, temperature, maxTokens, abortSignal }) {
    const endpoint = "https://api.groq.com/openai/v1/chat/completions";

    const body = {
        model,
        messages: [{ role: "user", content: prompt }]
    };

    if (temperature !== undefined) {
        body.temperature = temperature;
    }
    if (maxTokens !== undefined) {
        body.max_tokens = maxTokens;
    }

    const response = await fetch(endpoint, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${apiKey}`
        },
        body: JSON.stringify(body),
        signal: abortSignal
    });

    if (!response.ok) {
        const errorText = await response.text().catch(() => "");
        const error = new Error(`Groq API error ${response.status}: ${response.statusText}. ${errorText}`);
        error.status = response.status;
        error.provider = "groq";
        throw error;
    }

    const data = await response.json();

    let text = "";
    if (data.choices && data.choices[0] && data.choices[0].message) {
        text = data.choices[0].message.content || "";
    }

    return {
        text,
        model,
        provider: "groq",
        raw: data
    };
}

module.exports = { call };

=================================
FILE: adapters/mock.js
=================================

/**
 * adapters/mock.js
 * Adapter mock per test.
 */
class MockAdapter {
    constructor(scenario = "success") {
        this.scenario = scenario;
        this.callCount = 0;
    }

    async call({ prompt, model, apiKey, temperature, maxTokens, abortSignal }) {
        this.callCount++;
        await new Promise(r => setTimeout(r, 10));

        if (this.scenario === "success") {
            return {
                text: `Risposta mock per: ${prompt?.substring(0, 50) || ""}...`,
                usage: { prompt_tokens: 100, completion_tokens: 50 }
            };
        }

        if (this.scenario === "groq_tpm") {
            const err = new Error("Request too large for model `openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, Requested 70509");
            err.status = 413;
            err.body = {
                error: {
                    message: "Request too large for model `openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, Requested 70509",
                    type: "tokens",
                    code: "rate_limit_exceeded"
                }
            };
            throw err;
        }

        if (this.scenario === "groq_429") {
            const err = new Error("Rate limit exceeded");
            err.status = 429;
            throw err;
        }

        if (this.scenario === "timeout") {
            const err = new Error("Request timeout");
            err.name = "AbortError";
            throw err;
        }

        if (this.scenario === "invalid_key") {
            const err = new Error("Invalid API Key");
            err.status = 401;
            throw err;
        }

        throw new Error("Unknown mock error");
    }
}

module.exports = { MockAdapter };

=================================
FILE: commons.js
=================================

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function maskString(str, start = 4, end = 4) {
    if (!str || str.length <= start + end) return "****";
    return `${str.substring(0, start)}...${str.substring(str.length - end)}`;
}

module.exports = {
    sleep,
    maskString
};


=================================
FILE: constants.js
=================================

const path = require("path");
require("dotenv").config({ path: path.join(__dirname, ".env") });

const DEFAULT_CONFIG = {
    timeoutMs: 30000,
    retryCount: 1
};

const PROMPT_CACHE_CONFIG = {
    enabled: true,
    ttlMs: 7 * 24 * 60 * 60 * 1000,
    maxEntries: 1000,
    persistPath: path.join(__dirname, "cache", "prompt-cache.json"),
    optimizeOnMiss: true,
    geminiModelForOptimization: "gemini-flash-latest"
};

/**
 * MODEL_LIMITS: SOLO limiti intrinseci del modello (context window, output).
 * NON includere limiti provider/account qui.
 */
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
    },
    "llama3-8b-8192": {
        contextWindow: 8192,
        maxOutputTokens: 4096,
        capabilities: ["json_mode"]
    },
    "mixtral-8x7b-32768": {
        contextWindow: 32768,
        maxOutputTokens: 4096,
        capabilities: ["json_mode"]
    }
};

/**
 * PROVIDER_LIMITS: limiti infrastrutturali (account, tier, TPM, RPM).
 * Separato da MODEL_LIMITS per evitare confusione.
 * tpmIsRateLimit: se false, il TPM funge ANCHE da limite dimensione singola richiesta.
 */
const PROVIDER_LIMITS = {
    gemini: {
        maxRequestTokens: 1048576,
        tpm: 1000000,
        rpm: 60,
        tier: "free",
        supportsCompression: true,
        tpmIsRateLimit: true
    },
    groq: {
        maxRequestTokens: 8000,
        tpm: 8000,
        rpm: 30,
        tier: "on_demand",
        supportsCompression: true,
        tpmIsRateLimit: false  // su free, TPM = limite anche per singola richiesta
    }
};

const PROVIDERS = {
    groq: {
        enabled: true,
        priority: 1,
        endpoint: "https://api.groq.com/openai/v1",
        models: ["openai/gpt-oss-120b"],
        apiKeys: [
            {
                id: "groq_1",
                value: process.env.GROQ_KEY_1 || '',
                rpmLimit: 30,
                dailyLimit: 14400,
                cooldownMs: 30000
            }
        ]
    },
    gemini: {
        enabled: true,
        priority: 2,
        endpoint: "https://generativelanguage.googleapis.com/v1beta",
        models: ["gemini-flash-latest"],
        apiKeys: [
            {
                id: "gemini_key_1",
                value: process.env.GEMINI_KEY_1 || '',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_2",
                value: process.env.GEMINI_KEY_2 || '',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_3",
                value: process.env.GEMINI_KEY_3 || '',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_4",
                value: process.env.GEMINI_KEY_4 || '',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_5",
                value: process.env.GEMINI_KEY_5 || '',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            }
        ]
    }
};

module.exports = {
    DEFAULT_CONFIG,
    PROVIDERS,
    MODEL_LIMITS,
    PROVIDER_LIMITS,
    PROMPT_CACHE_CONFIG
};

=================================
FILE: core/chunk-manager.js
=================================

/**
 * core/chunk-manager.js (NEW)
 * FIX #7: gestione preventiva batch troppo grandi.
 */
const TokenManager = require("./token-manager.js");

class ChunkManager {
    constructor(logger = console) {
        this.logger = logger;
    }

    splitArrayIntoChunks(items, instruction, provider, model, safetyMargin = 500) {
        if (!Array.isArray(items)) throw new Error("ChunkManager: input must be an array");

        const effective = TokenManager.getEffectiveLimit(provider, model);
        if (!effective.limit) {
            this.logger.warn(`[ChunkManager] Unknown limit for ${provider}/${model}, default chunk size 100`);
            return this.splitByCount(items, 100);
        }

        const availableTokens = effective.limit - safetyMargin;
        const instructionTokens = TokenManager.estimate(instruction, "instruction");
        const tokensPerItem = items.length > 0 ? TokenManager.estimate(items[0], "json") : 0;
        const overheadPerChunk = 10;
        const usableTokens = availableTokens - instructionTokens - overheadPerChunk;

        if (usableTokens <= 0) {
            throw new Error(`[ChunkManager] Instruction alone exceeds limit for ${provider}/${model}`);
        }

        if (tokensPerItem > usableTokens) {
            this.logger.warn(`[ChunkManager] Single item exceeds chunk limit, single-item chunks`);
            return items.map(item => [item]);
        }

        const maxItemsPerChunk = Math.floor(usableTokens / Math.max(tokensPerItem, 1));
        const chunkSize = Math.max(1, maxItemsPerChunk);
        this.logger.info(`[ChunkManager] Splitting ${items.length} items into ~${chunkSize} per chunk (limit: ${effective.limit} tokens)`);
        return this.splitByCount(items, chunkSize);
    }

    splitByCount(items, count) {
        const chunks = [];
        for (let i = 0; i < items.length; i += count) chunks.push(items.slice(i, i + count));
        return chunks;
    }

    needsChunking(items, instruction, provider, model) {
        if (!Array.isArray(items) || items.length === 0) return false;
        const totalTokens = TokenManager.estimatePromptTokens(instruction, items, "instruction", "json");
        return !TokenManager.canHandleRequest(provider, model, totalTokens.total).ok;
    }
}

module.exports = ChunkManager;

=================================
FILE: core/compressor.js
=================================

/**
 * core/compressor.js (FIXED v2)
 * FIX #6: usa getEffectiveLimit() che include provider+account, non solo contextWindow.
 */
const { PROVIDERS, MODEL_LIMITS } = require("../constants");
const ProviderClient = require("./provider-client");
const TokenManager = require("./token-manager.js");

class Compressor {
  constructor(keyPool, registry, logger) {
    this.keyPool = keyPool;
    this.registry = registry;
    this.logger = logger;
    this.providerClient = new ProviderClient(keyPool, logger);
  }

  async compress(prompt, targetModel, targetProvider) {
    const promptTokens = TokenManager.estimate(prompt);
    const targetLimit = TokenManager.getEffectiveLimit(targetProvider, targetModel).limit;

    if (!targetLimit) {
      throw new Error(`Cannot determine limit for ${targetProvider}/${targetModel}`);
    }

    this.logger.info("Prompt exceeds target model limit, initiating semantic compression", {
      targetProvider,
      targetModel,
      promptTokens,
      targetLimit
    });

    const compressor = this.findCompressorModel(promptTokens);
    if (!compressor) {
      const error = new Error(
        `No model available with sufficient context window to compress prompt (${promptTokens} tokens)`
      );
      error.status = 413;
      throw error;
    }

    this.logger.info("Selected compressor model", {
      provider: compressor.provider,
      model: compressor.model,
      keyId: compressor.key.id
    });

    const compressionPrompt = this.buildCompressionPrompt(prompt, targetModel, targetProvider);
    const compressionTokens = TokenManager.estimate(compressionPrompt);

    const compressorLimit = TokenManager.getEffectiveLimit(compressor.provider, compressor.model).limit;
    if (compressorLimit && compressionTokens > compressorLimit) {
      const error = new Error(
        `Compression prompt too long even for compressor (${compressionTokens} > ${compressorLimit})`
      );
      error.status = 413;
      throw error;
    }

    const startTime = Date.now();
    try {
      const result = await this.providerClient.execute({
        provider: compressor.provider,
        payload: {
          prompt: compressionPrompt,
          model: compressor.model,
          key: compressor.key,
          temperature: 0.2,
          maxTokens: Math.min(8192, Math.floor((compressorLimit || 65536) * 0.5))
        }
      });

      const duration = Date.now() - startTime;
      const compressedText = result.text || "";
      const compressedTokens = TokenManager.estimate(compressedText);

      this.logger.info("Compression completed", {
        provider: compressor.provider,
        model: compressor.model,
        originalTokens: promptTokens,
        compressedTokens,
        duration,
        reduction: `${Math.round((1 - compressedTokens / promptTokens) * 100)}%`
      });

      if (compressedTokens > targetLimit) {
        const error = new Error(
          `Compressed prompt still exceeds target model limit (${compressedTokens} > ${targetLimit})`
        );
        error.status = 413;
        throw error;
      }

      return compressedText;
    } catch (error) {
      this.logger.error("Compression failed", {
        provider: compressor.provider,
        model: compressor.model,
        error: error.message
      });
      throw error;
    }
  }

  /**
   * Trova il miglior modello per la compressione.
   * PRIORITÀ: Gemini PRIMA, poi altri provider per limite effettivo maggiore.
   * FIX #6: usa getEffectiveLimit invece di contextWindow raw.
   */
  findCompressorModel(promptTokens) {
    const candidates = [];

    for (const [provName, provConfig] of Object.entries(PROVIDERS)) {
      if (!provConfig.enabled) continue;

      for (const modelName of provConfig.models || []) {
        const effective = TokenManager.getEffectiveLimit(provName, modelName);
        if (effective.limit && effective.limit > promptTokens * 1.2) {
          candidates.push({
            provider: provName,
            model: modelName,
            limit: effective.limit,
            isGemini: provName === "gemini"
          });
        }
      }
    }

    // ORDINAMENTO: Gemini PRIMA, poi per limite effettivo decrescente
    candidates.sort((a, b) => {
      if (a.isGemini !== b.isGemini) return a.isGemini ? -1 : 1;
      return b.limit - a.limit;
    });

    for (const candidate of candidates) {
      const key = this.keyPool.getNextKey(candidate.provider);
      if (key) {
        return { ...candidate, key };
      }
    }

    return null;
  }

  buildCompressionPrompt(prompt, targetModel, targetProvider) {
    const targetLimit = TokenManager.getEffectiveLimit(targetProvider, targetModel).limit || 0;

    return `You are a semantic compression engine. Your task is to compress the following text while preserving ALL semantic information, facts, data, names, dates, numbers, relationships, and instructions.

CRITICAL REQUIREMENTS:
1. Remove ONLY: redundancies, repetitions, filler words, unnecessary elaborations, and decorative language.
2. Preserve EXACTLY: all technical details, requirements, constraints, names, dates, numbers, code, and logical relationships.
3. The compressed text MUST fit within ${targetLimit} tokens when processed by an AI model.
4. The meaning must remain 100% intact. Another AI reading the compressed version should produce the same result as if it had read the original.
5. Do NOT add explanations, markdown formatting, or meta-commentary. Output ONLY the compressed text.

ORIGINAL TEXT:
${prompt}

COMPRESSED TEXT:`;
  }
}

module.exports = Compressor;

=================================
FILE: core/key-pool.js
=================================

/**
 * core/key-pool.js (FIXED v2)
 * FIX: aggiunti metodi getNextKey, trackRequest, setCooldown compatibili
 * con provider-client.js e router.js esistenti.
 */
class KeyPool {
    constructor(providersConfig, logger = console) {
        this.config = providersConfig;
        this.logger = logger;
        this.state = new Map(); // provider -> keyId -> { rpmCount[], dailyCount, cooldownUntil, invalid }
        this.roundRobin = new Map(); // provider -> index
        this.keyIdToProvider = new Map(); // keyId -> provider

        // Build reverse map
        for (const [provider, cfg] of Object.entries(providersConfig)) {
            for (const key of cfg.apiKeys || []) {
                this.keyIdToProvider.set(key.id, provider);
            }
        }
    }

    _ensureState(provider, keyId) {
        if (!this.state.has(provider)) this.state.set(provider, new Map());
        const p = this.state.get(provider);
        if (!p.has(keyId)) {
            p.set(keyId, { rpmCount: [], dailyCount: 0, cooldownUntil: 0, invalid: false });
        }
        return p.get(keyId);
    }

    getAvailableKeys(provider) {
        const cfg = this.config[provider];
        if (!cfg) return [];
        const now = Date.now();
        const oneMinuteAgo = now - 60000;
        const oneDayAgo = now - 86400000;

        return cfg.apiKeys
            .map(k => {
                const s = this._ensureState(provider, k.id);
                s.rpmCount = s.rpmCount.filter(t => t > oneMinuteAgo);
                if (s.dailyReset && s.dailyReset < oneDayAgo) {
                    s.dailyCount = 0;
                    s.dailyReset = now;
                }
                return { ...k, state: s };
            })
            .filter(k => {
                if (k.state.invalid) return false;
                if (k.state.cooldownUntil > now) return false;
                if (k.state.rpmCount.length >= k.rpmLimit) return false;
                if (k.state.dailyCount >= k.dailyLimit) return false;
                return true;
            });
    }

    /**
     * Restituisce la prossima key disponibile in round-robin.
     * Compatibile con router.js e provider-client.js.
     */
    getNextKey(provider) {
        const available = this.getAvailableKeys(provider);
        if (available.length === 0) return null;

        let idx = this.roundRobin.get(provider) || 0;
        const key = available[idx % available.length];
        this.roundRobin.set(provider, (idx + 1) % available.length);
        return key;
    }

    markUsed(provider, keyId) {
        const s = this._ensureState(provider, keyId);
        s.rpmCount.push(Date.now());
        s.dailyCount++;
        if (!s.dailyReset) s.dailyReset = Date.now();
    }

    /**
     * Alias compatibile con provider-client.js.
     */
    trackRequest(keyId) {
        const provider = this.keyIdToProvider.get(keyId);
        if (provider) this.markUsed(provider, keyId);
    }

    cooldown(provider, keyId, ms) {
        const s = this._ensureState(provider, keyId);
        s.cooldownUntil = Date.now() + ms;
        this.logger.warn(`[KeyPool] ${provider}/${keyId} cooldown for ${ms}ms`);
    }

    /**
     * Alias compatibile con provider-client.js.
     */
    setCooldown(keyId, ms) {
        const provider = this.keyIdToProvider.get(keyId);
        if (provider) this.cooldown(provider, keyId, ms);
    }

    invalidate(provider, keyId) {
        const s = this._ensureState(provider, keyId);
        s.invalid = true;
        this.logger.error(`[KeyPool] ${provider}/${keyId} invalidated`);
    }
}

module.exports = KeyPool;
module.exports.KeyPool = KeyPool;

=================================
FILE: core/logger.js
=================================

/**
 * Logger centralizzato della libreria
 *
 * Supporta:
 * - logger personalizzato tramite injection
 * - default console
 * - masking API key
 * - formato uniforme
 */
class Logger {
    constructor(customLogger = console) {
        this.logger = customLogger;
    }

    /**
     * Nasconde una API Key nei log
     *
     * esempio:
     * gemini_key_123456
     *
     * diventa:
     * gemi...3456
     */
    maskKey(apiKey) {
        if (!apiKey) {
            return "unknown";
        }
        if (apiKey.length <= 8) {
            return "****";
        }
        return `${apiKey.substring(0, 4)}...${apiKey.substring(apiKey.length - 4)}`;
    }

    info(message, data = {}) {
        this.logger.info("[INFO]", message, data);
    }

    warn(message, data = {}) {
        this.logger.warn("[WARN]", message, data);
    }

    error(message, data = {}) {
        this.logger.error("[ERROR]", message, data);
    }

    request({ provider, model, apiKey, duration, retry = false }) {
        this.info("API request", {
            provider,
            model,
            apiKey: this.maskKey(apiKey),
            duration,
            retry
        });
    }

    fallback({ provider, model, reason }) {
        this.warn("Fallback triggered", {
            provider,
            model,
            reason
        });
    }

    failure({ provider, model, error }) {
        this.error("Request failed", {
            provider,
            model,
            error
        });
    }
}

module.exports = Logger;


=================================
FILE: core/prompt-cache.js
=================================

/**
 * PromptCache (FIXED v3)
 * FIX: _persist in set() wrappato in try/catch per non far fallire
 *      l'intera chiamata AI in caso di errore disco/permessi.
 */
const fs = require("fs");
const path = require("path");

class PromptCache {
  constructor(options = {}) {
    this.cache = new Map();
    this.ttlMs = options.ttlMs || 7 * 24 * 60 * 60 * 1000;
    this.maxEntries = options.maxEntries || 1000;
    this.persistPath = options.persistPath || null;
    this.stats = { hits: 0, misses: 0 };

    if (this.persistPath) {
      this._ensureDir();
      this.load();
    }
  }

  _ensureDir() {
    const dir = path.dirname(this.persistPath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
  }

  get(hash) {
    this._cleanup();
    const entry = this.cache.get(hash);
    if (!entry) {
      this.stats.misses++;
      return null;
    }
    const age = Date.now() - new Date(entry.createdAt).getTime();
    if (age > this.ttlMs) {
      this.cache.delete(hash);
      this.stats.misses++;
      return null;
    }
    entry.lastUsed = new Date().toISOString();
    entry.usageCount = (entry.usageCount || 0) + 1;
    this.stats.hits++;
    return entry;
  }

  set(hash, data) {
    if (this.cache.size >= this.maxEntries && !this.cache.has(hash)) {
      let oldestHash = null;
      let oldestTime = Infinity;
      for (const [h, e] of this.cache) {
        const t = new Date(e.lastUsed || e.createdAt).getTime();
        if (t < oldestTime) {
          oldestTime = t;
          oldestHash = h;
        }
      }
      if (oldestHash) this.cache.delete(oldestHash);
    }

    const existing = this.cache.get(hash);
    const savedTokens = (existing?.savedTokens || 0) + (data.tokensBefore - data.tokensAfter);

    this.cache.set(hash, {
      ...data,
      savedTokens: Math.max(0, savedTokens),
      createdAt: existing?.createdAt || new Date().toISOString(),
      lastUsed: new Date().toISOString(),
    });

    // FIX: non far crashare l'intera chiamata se il disco ha problemi
    try {
      this._persist();
    } catch (e) {
      // Silenzioso: la cache in-memory è sufficiente
    }

    return this;
  }

  has(hash) {
    return this.get(hash) !== null;
  }

  getStats() {
    const entries = Array.from(this.cache.values());
    const totalSaved = entries.reduce((s, e) => s + (e.savedTokens || 0), 0);
    return {
      ...this.stats,
      totalEntries: this.cache.size,
      totalSavedTokens: totalSaved,
      topPrompts: entries
        .sort((a, b) => (b.usageCount || 0) - (a.usageCount || 0))
        .slice(0, 10)
        .map((e) => ({
          original:
            e.original.substring(0, 60) + (e.original.length > 60 ? "..." : ""),
          usageCount: e.usageCount,
          savedTokens: e.savedTokens,
          reduction:
            e.tokensBefore > 0
              ? Math.round(((e.tokensBefore - e.tokensAfter) / e.tokensBefore) * 100)
              : 0,
        })),
    };
  }

  clear() {
    this.cache.clear();
    this.stats = { hits: 0, misses: 0 };
    this._persist();
  }

  load() {
    try {
      if (!fs.existsSync(this.persistPath)) return;
      const raw = fs.readFileSync(this.persistPath, "utf8");
      const data = JSON.parse(raw);
      if (data.entries) {
        for (const [hash, entry] of Object.entries(data.entries)) {
          this.cache.set(hash, entry);
        }
      }
      if (data.stats) this.stats = data.stats;
    } catch (e) {
      // Cache corrotta: ignora
    }
  }

  _persist() {
    if (!this.persistPath) return;
    const data = {
      entries: Object.fromEntries(this.cache),
      stats: this.stats,
      lastPersisted: new Date().toISOString(),
    };
    fs.writeFileSync(this.persistPath, JSON.stringify(data, null, 2));
  }

  _cleanup() {
    const now = Date.now();
    for (const [hash, entry] of this.cache) {
      if (now - new Date(entry.createdAt).getTime() > this.ttlMs) {
        this.cache.delete(hash);
      }
    }
  }
}

module.exports = PromptCache;

=================================
FILE: core/prompt-manager.js
=================================

/**
 * PromptManager
 * Orchestratore centrale della strategia di cache e ottimizzazione.
 *
 * FIX #3: la cache è solo sull'istruzione normalizzata, non sull'input dinamico.
 */
const crypto = require("crypto");
const { normalizePrompt } = require("../utils/prompt-normalizer");
const PromptCache = require("./prompt-cache");
const PromptOptimizer = require("./prompt-optimizer");
const TokenManager = require("./token-manager.js");

class PromptManager {
  constructor(keyPool, registry, logger, options = {}) {
    this.cache = new PromptCache(options.cache);
    this.optimizer = options.optimizeOnMiss !== false
      ? new PromptOptimizer(keyPool, logger)
      : null;
    this.logger = logger;
    this.enabled = options.enabled !== false;
  }

  async prepare(options) {
    const { instruction, input, prompt } = options;

    if (!instruction && prompt) {
      return this._prepareLegacy(prompt);
    }

    if (!instruction) {
      throw new Error("instruction or prompt is required");
    }

    const normalized = normalizePrompt(instruction);
    const hash = this._generateHash(normalized);

    const cached = this.cache.get(hash);
    if (cached) {
      this.logger.info("Prompt cache HIT", {
        hash: hash.substring(0, 8),
        usageCount: cached.usageCount,
        savedTokens: cached.savedTokens
      });
      return {
        prompt: this._buildPrompt(cached.optimized, input),
        hash,
        fromCache: true,
        optimized: cached.optimized,
        original: cached.original,
        stats: TokenManager.getStats(cached.original, cached.optimized)
      };
    }

    this.logger.info("Prompt cache MISS", { hash: hash.substring(0, 8) });

    let optimized = instruction;
    let fromOptimizer = false;

    if (this.enabled && this.optimizer) {
      try {
        optimized = await this.optimizer.optimize(instruction);
        fromOptimizer = true;
        const optStats = TokenManager.getStats(instruction, optimized);
        this.logger.info("Prompt optimized via AI", {
          hash: hash.substring(0, 8),
          originalTokens: optStats.before,
          optimizedTokens: optStats.after,
          reduction: `${optStats.reductionPercent}%`
        });
      } catch (err) {
        this.logger.warn("Prompt optimization failed, using original", {
          error: err.message
        });
      }
    }

    const stats = TokenManager.getStats(instruction, optimized);

    this.cache.set(hash, {
      original: instruction,
      optimized,
      tokensBefore: stats.before,
      tokensAfter: stats.after,
      usageCount: 1,
      savedTokens: 0,
      lastUsed: new Date().toISOString()
    });

    return {
      prompt: this._buildPrompt(optimized, input),
      hash,
      fromCache: false,
      fromOptimizer,
      optimized,
      original: instruction,
      stats
    };
  }

  _prepareLegacy(prompt) {
    return {
      prompt,
      hash: null,
      fromCache: false,
      fromOptimizer: false,
      stats: null
    };
  }

  _buildPrompt(instruction, input) {
    if (input === undefined || input === null) return instruction;
    const inputStr = typeof input === "string" ? input : JSON.stringify(input, null, 2);
    return `${instruction}\n\nInput:\n${inputStr}`;
  }

  _generateHash(text) {
    return crypto.createHash("sha256").update(text).digest("hex");
  }

  getCacheStats() {
    return this.cache.getStats();
  }

  clearCache() {
    this.cache.clear();
  }
}

module.exports = PromptManager;

=================================
FILE: core/prompt-optimizer.js
=================================

/**
 * PromptOptimizer
 * Ottimizza un prompt template chiamando Gemini direttamente.
 */
const ProviderClient = require("./provider-client");

class PromptOptimizer {
  constructor(keyPool, logger) {
    this.providerClient = new ProviderClient(keyPool, logger);
    this.logger = logger;
  }

  async optimize(instruction) {
    const key = this.providerClient.keyPool.getNextKey("gemini");
    if (!key) {
      throw new Error("No Gemini key available for prompt optimization");
    }

    const optimizationPrompt = `You are a prompt optimization engine. Your task is to rewrite the following user instruction to be maximally concise and clear for an AI model, removing all unnecessary words while preserving every requirement, constraint, and technical detail.

Rules:
1. Remove filler words, redundancies, and polite phrases.
2. Keep all technical terms, variable names, field names, and logic intact.
3. Use imperative, direct language.
4. Do NOT add explanations, markdown formatting, or meta-commentary.
5. Output ONLY the optimized instruction text.

Original instruction:
${instruction}

Optimized instruction:`;

    const result = await this.providerClient.execute({
      provider: "gemini",
      payload: {
        prompt: optimizationPrompt,
        model: "gemini-flash-latest",
        key,
        temperature: 0.1,
        maxTokens: 1024
      }
    });

    const optimized = (result.text || "").trim();
    if (!optimized) {
      throw new Error("Optimizer returned empty text");
    }
    return optimized;
  }
}

module.exports = PromptOptimizer;

=================================
FILE: core/provider-client.js
=================================

/**
 * core/provider-client.js (FIXED v2)
 * FIX #5, #9: usa classifyError per strategia diversa per tipo di errore.
 */
const registry = require("../registry");
const { classifyError, ErrorTypes } = require("../utils/errors");

class ProviderClient {
    constructor(keyPool, logger) {
        this.keyPool = keyPool;
        this.logger = logger;
    }

    async execute({ provider, payload }) {
        const ProviderClass = registry.getProviderClass(provider);
        if (!ProviderClass) {
            throw new Error(`Provider not found: ${provider}`);
        }

        const adapter = registry.getAdapter(provider);
        if (!adapter) {
            throw new Error(`Adapter not found: ${provider}`);
        }

        const providerInstance = new ProviderClass(adapter, this.keyPool, this.logger);

        const { key, abortSignal, ...restPayload } = payload;

        try {
            const result = await providerInstance.call({ ...restPayload, key, abortSignal });
            // Traccia la richiesta solo su successo
            this.keyPool.trackRequest(key.id);
            return result;
        } catch (error) {
            this.handleError(provider, key, error);
            throw error;
        }
    }

    /**
     * Gestione errori con classifyError.
     * FIX #9: strategia diversa per ogni tipo.
     */
    handleError(providerName, key, error) {
        if (!error) return;

        const classified = classifyError(error, providerName);
        const status = classified.status || error.status;

        switch (classified.type) {
            case ErrorTypes.RATE_LIMIT_RPM:
            case ErrorTypes.RATE_LIMIT_TPM:
            case ErrorTypes.QUOTA_EXHAUSTED: {
                const providerConfig = this.keyPool.config?.[providerName];
                const cooldown = providerConfig?.apiKeys?.find(k => k.id === key.id)?.cooldownMs || 60000;
                this.keyPool.setCooldown(key.id, cooldown);
                break;
            }
            case ErrorTypes.INVALID_KEY:
                this.keyPool.invalidate(providerName, key.id);
                break;
            default:
                // Nessuna azione su errori generici
                break;
        }
    }
}

module.exports = ProviderClient;

=================================
FILE: core/router.js
=================================

/**
 * core/router.js (FIXED v3)
 *
 * FIX:
 * - executeWithRetry fa davvero retry su timeout (1x stessa key)
 * - 400/INVALID_REQUEST salta modello invece di loopare all'infinito
 * - 500/UNKNOWN salta modello invece di riprovare key all'infinito
 * - _routeChunked mergia oggetti JSON, non concatena stringhe
 * - _routeChunked usa provider per priority, non Object.keys[0]
 * - Ripristina prompt originale quando cambia modello
 * - Aggiunto _promptMeta nel risultato
 * - maxTokens validato vs limite modello prima della chiamata
 * - RATE_LIMIT_TPM esce dal while key e interrompe i modelli del provider
 */

const { DEFAULT_CONFIG, PROVIDERS, MODEL_LIMITS } = require("../constants");

const ProviderClient = require("./provider-client");
const Compressor = require("./compressor");
const ChunkManager = require("./chunk-manager");
const PromptManager = require("./prompt-manager");
const TokenManager = require("./token-manager");

const { classifyError, ErrorTypes } = require("../utils/errors");

class Router {
  constructor(keyPool, registry, logger) {
    this.keyPool = keyPool;
    this.registry = registry;
    this.logger = logger;

    this.providerClient = new ProviderClient(keyPool, logger);
    this.compressor = new Compressor(keyPool, registry, logger);
    this.chunkManager = new ChunkManager(logger);
    this.promptManager = new PromptManager(keyPool, registry, logger, {
      cache: { enabled: true },
    });

    this.attempts = [];
    this.maxAttempts = 50;
    this.providerCooldown = new Map();
  }

  async route(options) {
    this.attempts = [];

    const {
      prompt,
      instruction,
      input,
      provider,
      model,
      temperature,
      maxTokens,
      compress = true,
    } = options;

    let effectivePrompt = prompt || "";
    let promptTokens = 0;
    let fromCache = false;
    let promptHash = null;
    let fromOptimizer = false;
    let promptStats = null;

    if (instruction) {
      const prepared = await this.promptManager.prepare({ instruction, input });
      effectivePrompt = prepared.prompt;
      fromCache = prepared.fromCache;
      promptHash = prepared.hash;
      fromOptimizer = prepared.fromOptimizer;
      promptStats = prepared.stats;
      promptTokens = TokenManager.estimate(
        effectivePrompt,
        typeof input === "object" ? "json" : "text",
      );
    } else {
      promptTokens = TokenManager.estimate(effectivePrompt, "text");
    }

    this.logger.info(`[Router] Estimated ${promptTokens} tokens`);

    if (Array.isArray(input) && input.length > 0) {
      const probeProvider = provider || this.resolveProviders(null)[0];
      const probeModel = model || PROVIDERS[probeProvider]?.models[0];
      if (
        this.chunkManager.needsChunking(
          input,
          instruction || effectivePrompt,
          probeProvider,
          probeModel,
        )
      ) {
        return this._routeChunked(options, instruction, input);
      }
    }

    const providers = this.resolveProviders(provider);
    const originalPrompt = effectivePrompt;

    for (const provName of providers) {
      if (this.isProviderBlocked(provName)) continue;

      const provConfig = PROVIDERS[provName];
      if (!provConfig || !provConfig.enabled) continue;

      const models = this.resolveModels(provConfig, model);

      for (const modelName of models) {
        if (this.isProviderBlocked(provName)) break;

        effectivePrompt = originalPrompt;
        promptTokens = TokenManager.estimate(
          effectivePrompt,
          typeof input === "object" ? "json" : "text",
        );

        if (maxTokens) {
          const modelMaxOut = MODEL_LIMITS[modelName]?.maxOutputTokens;
          if (modelMaxOut && maxTokens > modelMaxOut) {
            this.logger.warn(
              `[Router] Skip ${provName}/${modelName}: maxTokens ${maxTokens} > model limit ${modelMaxOut}`,
            );
            this.attempts.push({
              provider: provName,
              model: modelName,
              status: "skipped",
              reason: `maxTokens ${maxTokens} > model limit ${modelMaxOut}`,
              preflight: true,
            });
            continue;
          }
        }

        let check = TokenManager.canHandleRequest(provName, modelName, promptTokens);
        if (!check.ok) {
          this.logger.warn(`[Router] Skip ${provName}/${modelName}: ${check.reason}`);
          this.attempts.push({
            provider: provName,
            model: modelName,
            status: "skipped",
            reason: check.reason,
            preflight: true,
          });

          if (compress) {
            try {
              effectivePrompt = await this.compressor.compress(originalPrompt, modelName, provName);
              promptTokens = TokenManager.estimate(effectivePrompt);
              check = TokenManager.canHandleRequest(provName, modelName, promptTokens);
              if (!check.ok) continue;
            } catch (e) {
              continue;
            }
          } else {
            continue;
          }
        }

        let key;
        while ((key = this.keyPool.getNextKey(provName)) !== null) {
          if (this.attempts.length > this.maxAttempts) {
            throw this.buildFinalError("Maximum routing attempts exceeded");
          }

          const attempt = {
            provider: provName,
            model: modelName,
            keyId: key.id,
            error: null,
          };

          let retryCount = 0;
          const maxRetries = 1;

          do {
            try {
              const ProviderClass = this.registry.getProviderClass(provName);
              if (ProviderClass && ProviderClass.prototype.preFlightCheck) {
                const instance = new ProviderClass(null, this.keyPool, this.logger);
                await instance.preFlightCheck({ model: modelName, tokens: promptTokens, key });
              }

              const result = await this.executeWithRetry({
                provider: provName,
                model: modelName,
                key,
                prompt: effectivePrompt,
                temperature,
                maxTokens,
              });

              return {
                ...result,
                _promptMeta: { fromCache, fromOptimizer, stats: promptStats },
                fromCache,
                hash: promptHash,
                attempts: this.attempts,
              };
            } catch (error) {
              attempt.error = error;
              const isTimeout =
                error.name === "AbortError" ||
                (error.message && error.message.toLowerCase().includes("timeout"));

              if (isTimeout && retryCount < maxRetries) {
                retryCount++;
                this.logger.warn(
                  `[Router] Timeout retry ${retryCount}/${maxRetries} on ${provName}/${modelName} [${key.id}]`,
                );
                continue;
              }
              break;
            }
          } while (retryCount <= maxRetries);

          this.attempts.push(attempt);
          const classified = classifyError(attempt.error, provName);
          this.logger.warn(
            `[Router] ${provName}/${modelName} [${key.id}] -> ${classified.type} (status ${classified.status})`,
          );

          switch (classified.type) {
            case ErrorTypes.RATE_LIMIT_TPM:
              this.blockProvider(provName, 60000);
              key = null;
              break;
            case ErrorTypes.RATE_LIMIT_RPM:
            case ErrorTypes.QUOTA_EXHAUSTED:
              this.keyPool.setCooldown(key.id, 60000);
              break;
            case ErrorTypes.INVALID_KEY:
              this.keyPool.invalidate(provName, key.id);
              break;
            case ErrorTypes.INVALID_REQUEST:
            case ErrorTypes.MODEL_NOT_FOUND:
            case ErrorTypes.PAYLOAD_TOO_LARGE:
              key = null;
              break;
            case ErrorTypes.TIMEOUT:
              break;
            default:
              if (classified.status >= 500) key = null;
              break;
          }
        }
      }
    }

    throw this.buildFinalError("All providers exhausted");
  }

  isProviderBlocked(provider) {
    const until = this.providerCooldown.get(provider);
    if (!until) return false;
    if (Date.now() > until) {
      this.providerCooldown.delete(provider);
      return false;
    }
    return true;
  }

  blockProvider(provider, ms) {
    this.providerCooldown.set(provider, Date.now() + ms);
    this.logger.warn(`[Router] Provider ${provider} blocked for ${ms}ms`);
  }

  async _routeChunked(options, instruction, input) {
    const provider = options.provider || this.resolveProviders(null)[0];
    const model = options.model || PROVIDERS[provider]?.models[0];

    const chunks = this.chunkManager.splitArrayIntoChunks(
      input,
      instruction || options.prompt || "",
      provider,
      model,
    );

    const mergedResults = {};

    for (let i = 0; i < chunks.length; i++) {
      this.logger.info(`[Router] Chunk ${i + 1}/${chunks.length}`);
      const res = await this.route({
        ...options,
        input: chunks[i],
        compress: false,
      });

      let parsed;
      if (typeof res.text === "string") {
        try {
          parsed = JSON.parse(res.text);
        } catch (e) {
          parsed = { _rawChunk: res.text };
        }
      } else if (res.text && typeof res.text === "object") {
        parsed = res.text;
      } else {
        parsed = {};
      }

      Object.assign(mergedResults, parsed);
    }

    return {
      text: mergedResults,
      chunked: true,
      chunksCount: chunks.length,
    };
  }

  resolveProviders(provider) {
    if (provider) return [provider];
    return Object.entries(PROVIDERS)
      .filter(([, cfg]) => cfg.enabled)
      .sort(([, a], [, b]) => a.priority - b.priority)
      .map(([name]) => name);
  }

  resolveModels(config, model) {
    if (model) return [model];
    return config.models || [];
  }

  async executeWithRetry({ provider, model, key, prompt, temperature, maxTokens }) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), DEFAULT_CONFIG.timeoutMs);
    try {
      return await this.providerClient.execute({
        provider,
        payload: {
          prompt,
          model,
          key,
          temperature,
          maxTokens,
          abortSignal: controller.signal,
        },
      });
    } finally {
      clearTimeout(timer);
    }
  }

  buildFinalError(message) {
    const details = this.attempts
      .map((a) => `${a.provider}/${a.model} ${a.keyId}: ${a.error?.message || a.reason}`)
      .join("; ");
    const err = new Error(`${message}. Attempts: ${details}`);
    err.attempts = this.attempts;
    return err;
  }
}

module.exports = Router;

=================================
FILE: core/token-manager.js
=================================

/**
 * core/token-manager.js (FIXED v3)
 * FIX: getEffectiveLimit non sottrae maxOutputTokens dai limiti provider.
 *      Ora sottrae SOLO da contextWindow. I limiti provider sono hard limit.
 */
const { estimateTokens } = require("../utils/token-estimator");
const { MODEL_LIMITS, PROVIDER_LIMITS, PROVIDERS } = require("../constants");

class TokenManager {
    static estimate(text, type = "text") {
        return estimateTokens(text, type);
    }

    static estimatePromptTokens(instruction, input, instructionType = "instruction", inputType = "json") {
        const instructionTokens = estimateTokens(instruction, instructionType);
        const inputTokens = estimateTokens(input, inputType);
        const overhead = 4;
        return {
            instruction: instructionTokens,
            input: inputTokens,
            overhead,
            total: instructionTokens + inputTokens + overhead
        };
    }

    static checkLimit(text, modelName, providerName = null) {
        const tokens = this.estimate(text);
        let limit;
        if (providerName) {
            const eff = this.getEffectiveLimit(providerName, modelName);
            limit = eff.limit !== null ? eff.limit : Infinity;
        } else {
            for (const [pName, pCfg] of Object.entries(PROVIDERS)) {
                if (pCfg.models?.includes(modelName)) {
                    const eff = this.getEffectiveLimit(pName, modelName);
                    limit = eff.limit !== null ? eff.limit : Infinity;
                    break;
                }
            }
            if (limit === undefined) {
                limit = MODEL_LIMITS[modelName]?.contextWindow || MODEL_LIMITS[modelName]?.maxOutputTokens || Infinity;
            }
        }
        return { tokens, limit, exceeds: tokens > limit, remaining: Math.max(0, limit - tokens) };
    }

    static getStats(original, optimized) {
        const before = this.estimate(original);
        const after = this.estimate(optimized);
        return {
            before,
            after,
            saved: Math.max(0, before - after),
            reductionPercent: before > 0 ? Math.round(((before - after) / before) * 100) : 0
        };
    }

    static getEffectiveLimit(provider, model) {
        const modelLimit = MODEL_LIMITS[model] || {};
        const providerLimit = PROVIDER_LIMITS[provider] || {};

        const contextWindow = modelLimit.contextWindow || Infinity;
        const maxOutputTokens = modelLimit.maxOutputTokens || 4096;
        const providerMaxRequest = providerLimit.maxRequestTokens || Infinity;
        const providerTpm = providerLimit.tpm || Infinity;
        const tpmIsRateLimit = providerLimit.tpmIsRateLimit ?? true;

        const contextInputLimit = contextWindow - maxOutputTokens;
        let minLimit = Math.min(contextInputLimit, providerMaxRequest);
        if (!tpmIsRateLimit) {
            minLimit = Math.min(minLimit, providerTpm);
        }

        if (!Number.isFinite(minLimit)) {
            return { limit: null, bottleneck: "unknown", factors: { contextWindow, maxOutputTokens, providerMaxRequest, providerTpm, tpmIsRateLimit } };
        }

        const limit = Math.max(0, minLimit);
        let bottleneck = "contextWindow";
        if (limit === Math.max(0, providerMaxRequest)) bottleneck = "providerMaxRequest";
        else if (!tpmIsRateLimit && limit === Math.max(0, providerTpm)) bottleneck = "providerTPM";

        return { limit, rawLimit: minLimit + maxOutputTokens, bottleneck, factors: { contextWindow, maxOutputTokens, providerMaxRequest, providerTpm, tpmIsRateLimit } };
    }

    static canHandleRequest(provider, model, totalTokens) {
        const effective = this.getEffectiveLimit(provider, model);
        if (effective.limit === null) return { ok: true, limit: null, bottleneck: "unknown" };
        if (totalTokens <= effective.limit) return { ok: true, limit: effective.limit, bottleneck: effective.bottleneck };
        return {
            ok: false,
            limit: effective.limit,
            bottleneck: effective.bottleneck,
            reason: `Request requires ${totalTokens} tokens, but ${provider}/${model} allows max ${effective.limit} (bottleneck: ${effective.bottleneck})`
        };
    }
}

module.exports = TokenManager;

=================================
FILE: index.js
=================================

/**
 * free_ai_api - index.js
 * Punto di ingresso pubblico.
 */
const { PROVIDERS, DEFAULT_CONFIG } = require("./constants");
const registry = require("./registry");
const { KeyPool } = require("./core/key-pool");
const Router = require("./core/router");
const Logger = require("./core/logger");

let _keyPool = null;
let _router = null;

function init(customLogger) {
    if (_router) return;
    // FIX: evita doppio wrapping se customLogger è già un'istanza Logger
    const logger = (customLogger && customLogger instanceof Logger)
        ? customLogger
        : new Logger(customLogger);
    registry.load(logger);
    _keyPool = new KeyPool(PROVIDERS, logger);
    _router = new Router(_keyPool, registry, logger);
}

async function freeCallApi(options) {
    init(options.logger);

    return _router.route({
        prompt: options.prompt,
        instruction: options.instruction,
        input: options.input,
        provider: options.provider,
        model: options.model,
        temperature: options.temperature,
        maxTokens: options.maxTokens,
        compress: options.compress !== false // default true
    });
}

module.exports = { freeCallApi };

=================================
FILE: package-lock.json
=================================

{
  "name": "free_ai_api",
  "version": "1.0.0",
  "lockfileVersion": 3,
  "requires": true,
  "packages": {
    "": {
      "name": "free_ai_api",
      "version": "1.0.0",
      "license": "MIT",
      "dependencies": {
        "crypto": "^1.0.1",
        "dotenv": "^16.4.0",
        "path": "^0.12.7"
      },
      "engines": {
        "node": ">=18.0.0"
      }
    },
    "node_modules/crypto": {
      "version": "1.0.1",
      "resolved": "https://registry.npmjs.org/crypto/-/crypto-1.0.1.tgz",
      "integrity": "sha512-VxBKmeNcqQdiUQUW2Tzq0t377b54N2bMtXO/qiLa+6eRRmmC4qT3D4OnTGoT/U6O9aklQ/jTwbOtRMTTY8G0Ig==",
      "deprecated": "This package is no longer supported. It's now a built-in Node module. If you've depended on crypto, you should switch to the one that's built-in.",
      "license": "ISC"
    },
    "node_modules/dotenv": {
      "version": "16.6.1",
      "resolved": "https://registry.npmjs.org/dotenv/-/dotenv-16.6.1.tgz",
      "integrity": "sha512-uBq4egWHTcTt33a72vpSG0z3HnPuIl6NqYcTrKEg2azoEyl2hpW0zqlxysq2pK9HlDIHyHyakeYaYnSAwd8bow==",
      "license": "BSD-2-Clause",
      "engines": {
        "node": ">=12"
      },
      "funding": {
        "url": "https://dotenvx.com"
      }
    },
    "node_modules/inherits": {
      "version": "2.0.3",
      "resolved": "https://registry.npmjs.org/inherits/-/inherits-2.0.3.tgz",
      "integrity": "sha512-x00IRNXNy63jwGkJmzPigoySHbaqpNuzKbBOmzK+g2OdZpQ9w+sxCN+VSB3ja7IAge2OP2qpfxTjeNcyjmW1uw==",
      "license": "ISC"
    },
    "node_modules/path": {
      "version": "0.12.7",
      "resolved": "https://registry.npmjs.org/path/-/path-0.12.7.tgz",
      "integrity": "sha512-aXXC6s+1w7otVF9UletFkFcDsJeO7lSZBPUQhtb5O0xJe8LtYhj/GxldoL09bBj9+ZmE2hNoHqQSFMN5fikh4Q==",
      "license": "MIT",
      "dependencies": {
        "process": "^0.11.1",
        "util": "^0.10.3"
      }
    },
    "node_modules/process": {
      "version": "0.11.10",
      "resolved": "https://registry.npmjs.org/process/-/process-0.11.10.tgz",
      "integrity": "sha512-cdGef/drWFoydD1JsMzuFf8100nZl+GT+yacc2bEced5f9Rjk4z+WtFUTBu9PhOi9j/jfmBPu0mMEY4wIdAF8A==",
      "license": "MIT",
      "engines": {
        "node": ">= 0.6.0"
      }
    },
    "node_modules/util": {
      "version": "0.10.4",
      "resolved": "https://registry.npmjs.org/util/-/util-0.10.4.tgz",
      "integrity": "sha512-0Pm9hTQ3se5ll1XihRic3FDIku70C+iHUdT/W926rSgHV5QgXsYbKZN8MSC3tJtSkhuROzvsQjAaFENRXr+19A==",
      "license": "MIT",
      "dependencies": {
        "inherits": "2.0.3"
      }
    }
  }
}


=================================
FILE: package.json
=================================

{
  "name": "free_ai_api",
  "version": "1.0.0",
  "description": "Libreria per chiamate API AI gratuite con fallback automatico",
  "main": "index.js",
  "scripts": {
    "test": "echo \"Error: no test specified\" && exit 1",
    "cache:stats": "node scripts/cache-stats.js",
    "cache:clear": "node scripts/cache-clear.js --force",
    "cache:warmup": "node scripts/warmup-cache.js",
    "optimize:prompt": "node scripts/optimize-prompt.js"
  },
  "keywords": [
    "ai",
    "api",
    "free",
    "gemini",
    "openai",
    "fallback"
  ],
  "author": "",
  "license": "MIT",
  "dependencies": {
    "crypto": "^1.0.1",
    "dotenv": "^16.4.0",
    "path": "^0.12.7"
  },
  "engines": {
    "node": ">=18.0.0"
  }
}


=================================
FILE: providers/gemini.js
=================================

/**
 * providers/gemini.js (FIXED v2)
 * Aggiunge preFlightCheck.
 */
class GeminiProvider {
    constructor(adapter, keyPool, logger) {
        this.adapter = adapter;
        this.keyPool = keyPool;
        this.logger = logger;
        this.name = "gemini";
    }

    async preFlightCheck({ model, tokens, key }) {
        // Gemini ha limiti molto alti, raramente blocca su dimensione
        return { ok: true };
    }

    async call({ prompt, model, key, temperature, maxTokens, abortSignal }) {
        const startTime = Date.now();

        try {
            const result = await this.adapter.call({
                prompt,
                model,
                apiKey: key.value,
                temperature,
                maxTokens,
                abortSignal
            });

            const duration = Date.now() - startTime;
            this.logger.request({
                provider: this.name,
                model,
                apiKey: key.value,
                duration,
                retry: false
            });

            return result;
        } catch (error) {
            const duration = Date.now() - startTime;
            this.logger.failure({ provider: this.name, model, error: error.message });

            error.provider = this.name;
            error.model = model;
            error.keyId = key.id;
            throw error;
        }
    }

    parseError(error) {
        const status = error.status || 0;
        const message = error.message || "";
        if (status === 429) {
            const err = new Error(`Gemini rate limit: ${message}`);
            err.status = 429;
            err.provider = "gemini";
            return err;
        }
        if (status === 400 && message.includes("API key not valid")) {
            const err = new Error(`Gemini invalid key: ${message}`);
            err.status = 401;
            err.provider = "gemini";
            return err;
        }
        return error;
    }
}

module.exports = GeminiProvider;

=================================
FILE: providers/groq.js
=================================

/**
 * providers/groq.js (FIXED v2)
 * FIX #5: preFlightCheck per TPM, parseError per distinguere 413 TPM vs payload.
 */
const { PROVIDER_LIMITS } = require("../constants");

class GroqProvider {
    constructor(adapter, keyPool, logger) {
        this.adapter = adapter;
        this.keyPool = keyPool;
        this.logger = logger;
        this.name = "groq";
    }

    /**
     * Pre-flight check: verifica che la richiesta non superi il TPM.
     * FIX #5: su free tier, TPM funge da limite dimensione.
     */
    async preFlightCheck({ model, tokens, key }) {
        const limits = PROVIDER_LIMITS.groq;
        if (limits.tpmIsRateLimit === false && tokens > limits.tpm) {
            const err = new Error(
                `Groq free tier TPM limit: ${tokens} > ${limits.tpm} tokens. Reduce request size or upgrade tier.`
            );
            err.status = 413;
            err.body = {
                error: {
                    message: `Request too large for model \`${model}\` on tokens per minute (TPM): Limit ${limits.tpm}, Requested ${tokens}`,
                    type: "tokens",
                    code: "rate_limit_exceeded"
                }
            };
            throw err;
        }
        return { ok: true };
    }

    async call({ prompt, model, key, temperature, maxTokens, abortSignal }) {
        const startTime = Date.now();

        try {
            const result = await this.adapter.call({
                prompt,
                model,
                apiKey: key.value,
                temperature,
                maxTokens,
                abortSignal
            });

            const duration = Date.now() - startTime;
            this.logger.request({
                provider: this.name,
                model,
                apiKey: key.value,
                duration,
                retry: false
            });

            return result;
        } catch (error) {
            const duration = Date.now() - startTime;
            this.logger.failure({ provider: this.name, model, error: error.message });

            // Arricchisci l'errore con metadati
            error.provider = this.name;
            error.model = model;
            error.keyId = key.id;
            throw error;
        }
    }

    /**
     * Parse error specifico per Groq.
     */
    parseError(error) {
        const status = error.status || 0;
        const body = error.body || {};
        const message = body.error?.message || error.message || "";
        const code = body.error?.code || "";

        if (status === 413 && code === "rate_limit_exceeded" && message.includes("tokens per minute")) {
            const err = new Error(`Groq TPM limit: ${message}`);
            err.status = 413;
            err.code = "rate_limit_exceeded";
            err.provider = "groq";
            err.isTpmLimit = true;
            return err;
        }
        if (status === 429) {
            const err = new Error(`Groq RPM limit: ${message}`);
            err.status = 429;
            err.provider = "groq";
            return err;
        }
        if (status === 413) {
            const err = new Error(`Groq payload too large: ${message}`);
            err.status = 413;
            err.provider = "groq";
            err.isPayloadTooLarge = true;
            return err;
        }
        return error;
    }
}

module.exports = GroqProvider;

=================================
FILE: registry.js
=================================

/**
 * registry.js (FIXED v3)
 * Singleton con metodi getProviderClass e getAdapter compatibili con provider-client.js.
 *
 * FIX: i provider esportano la classe direttamente (module.exports = Classe),
 * non un oggetto { Classe }. Il vecchio codice faceva mod[Object.keys(mod)[0]],
 * ma Object.keys() su una funzione/classe restituisce [] (i metodi stanno sul
 * prototype, non sono own enumerable properties) -> ClassRef risultava undefined
 * -> "Provider not found" per ogni provider.
 */
const fs = require("fs");
const path = require("path");

class Registry {
    constructor() {
        this.providers = {};
        this.adapters = {};
        this._loaded = false;
    }

    load(logger = console) {
        if (this._loaded) return this;

        const adaptersDir = path.join(__dirname, "adapters");
        const providersDir = path.join(__dirname, "providers");

        // Carica adapters (escludi mock.js)
        for (const file of fs.readdirSync(adaptersDir)) {
            if (!file.endsWith(".js") || file === "mock.js") continue;
            const name = path.basename(file, ".js");
            const mod = require(path.join(adaptersDir, file));
            this.adapters[name] = this._resolveExport(mod, name, "adapter", logger);
        }

        // Carica provider classes (non istanziare, solo referenze)
        for (const file of fs.readdirSync(providersDir)) {
            if (!file.endsWith(".js")) continue;
            const name = path.basename(file, ".js");
            const mod = require(path.join(providersDir, file));
            this.providers[name] = this._resolveExport(mod, name, "provider", logger);
        }

        this._loaded = true;
        return this;
    }

    /**
     * Risolve l'export di un modulo indipendentemente dallo stile usato:
     * - module.exports = Classe (funzione diretta)
     * - module.exports = { Classe }
     * - module.exports = { call } (adapter con funzioni nominate)
     */
    _resolveExport(mod, name, kind, logger) {
        if (typeof mod === "function") {
            return mod;
        }
        if (mod && typeof mod === "object") {
            const keys = Object.keys(mod);
            if (keys.length === 0) {
                logger.warn?.(`[Registry] ${kind} "${name}" esporta un oggetto vuoto`);
                return null;
            }
            // Se l'oggetto ha già la forma giusta (es. adapter con { call }), tienilo com'è
            if (kind === "adapter" && typeof mod.call === "function") {
                return mod;
            }
            // Altrimenti prendi il primo export (compatibilità con { Classe })
            return mod[keys[0]];
        }
        logger.warn?.(`[Registry] ${kind} "${name}" ha un export non riconosciuto`);
        return null;
    }

    getProviderClass(name) {
        if (!this._loaded) this.load();
        return this.providers[name] || null;
    }

    getAdapter(name) {
        if (!this._loaded) this.load();
        return this.adapters[name] || null;
    }
}

// Singleton
const instance = new Registry();
module.exports = instance;

=================================
FILE: scripts/cache-clear.js
=================================

#!/usr/bin/env node
/**
 * cache-clear.js
 * Svuota completamente la cache dei prompt.
 *
 * Uso:
 *   node scripts/cache-clear.js [--force]
 */
const fs = require("fs");
const path = require("path");

const cachePath = path.join(__dirname, "..", "cache", "prompt-cache.json");

if (!fs.existsSync(cachePath)) {
  console.log("📭 Nessun file cache trovato.");
  process.exit(0);
}

const force = process.argv.includes("--force");

if (!force) {
  console.log("⚠️  Questo eliminerà permanentemente tutte le entry della cache.");
  console.log(`   File: ${cachePath}`);
  console.log("   Riesegui con --force per confermare.\n");
  process.exit(0);
}

fs.unlinkSync(cachePath);
console.log("🗑️  Cache svuotata con successo.\n");

=================================
FILE: scripts/cache-stats.js
=================================

#!/usr/bin/env node
/**
 * cache-stats.js
 * Mostra statistiche dettagliate della prompt cache.
 *
 * Uso:
 *   node scripts/cache-stats.js
 */
const fs = require("fs");
const path = require("path");

const cachePath = path.join(__dirname, "..", "cache", "prompt-cache.json");

if (!fs.existsSync(cachePath)) {
  console.log("\n📭 Nessuna cache trovata.\n");
  process.exit(0);
}

const data = JSON.parse(fs.readFileSync(cachePath, "utf8"));
const entries = Object.values(data.entries || {});
const totalSaved = entries.reduce((s, e) => s + (e.savedTokens || 0), 0);
const totalHits = data.stats?.hits || 0;
const totalMisses = data.stats?.misses || 0;
const totalRequests = totalHits + totalMisses;

console.log("\n📊 PROMPT CACHE STATISTICS");
console.log("=".repeat(60));
console.log(`Entries in cache:      ${entries.length}`);
console.log(`Total requests:        ${totalRequests}`);
console.log(`Cache hits:            ${totalHits}`);
console.log(`Cache misses:          ${totalMisses}`);
console.log(`Hit rate:              ${totalRequests > 0 ? ((totalHits / totalRequests) * 100).toFixed(1) : 0}%`);
console.log(`Total tokens saved:    ${totalSaved.toLocaleString()}`);
console.log(`Last persisted:        ${data.lastPersisted || "N/A"}`);

if (entries.length > 0) {
  console.log("\n🏆 TOP 10 MOST REUSED PROMPTS");
  console.log("-".repeat(60));
  entries
    .sort((a, b) => (b.usageCount || 0) - (a.usageCount || 0))
    .slice(0, 10)
    .forEach((e, i) => {
      const red = e.tokensBefore > 0
        ? Math.round(((e.tokensBefore - e.tokensAfter) / e.tokensBefore) * 100)
        : 0;
      const preview = e.original.substring(0, 55) + (e.original.length > 55 ? "..." : "");
      console.log(`${String(i + 1).padStart(2)}. "${preview}"`);
      console.log(`    Usage: ${e.usageCount} | Saved: ${e.savedTokens} tk | Reduction: ${red}%`);
    });
}

console.log("\n");

=================================
FILE: scripts/optimize-prompt.js
=================================

#!/usr/bin/env node
/**
 * optimize-prompt.js
 * Ottimizza un singolo prompt via CLI e mostra il confronto.
 *
 * Uso:
 *   node scripts/optimize-prompt.js "Aggiungimi la chiave toDeleted in questo array"
 */
require("dotenv").config();

const { PromptManager } = require("../core/prompt-manager");
const { PROVIDERS, PROMPT_CACHE_CONFIG } = require("../constants");
const KeyPool = require("../core/key-pool");
const Logger = require("../core/logger");

const instruction = process.argv[2];

if (!instruction) {
  console.log("Uso: node optimize-prompt.js \"la tua istruzione qui\"");
  process.exit(1);
}

async function main() {
  const keyPool = new KeyPool(PROVIDERS);
  const logger = new Logger();
  const manager = new PromptManager(keyPool, null, logger, {
    cache: PROMPT_CACHE_CONFIG,
    optimizeOnMiss: true
  });

  console.log("\n📝 ORIGINALE:");
  console.log("-".repeat(60));
  console.log(instruction);

  const result = await manager.prepare({ instruction });

  console.log("\n⚡ OTTIMIZZATO:");
  console.log("-".repeat(60));
  console.log(result.optimized || instruction);

  if (result.stats) {
    console.log("\n📉 STATISTICHE:");
    console.log(`   Token prima:  ${result.stats.before}`);
    console.log(`   Token dopo:   ${result.stats.after}`);
    console.log(`   Risparmio:    ${result.stats.saved} token (${result.stats.reductionPercent}%)`);
    console.log(`   In cache:     ${result.fromCache ? "Sì (HIT)" : "No (MISS, salvato ora)"}`);
  }
  console.log("");
}

main().catch(console.error);

=================================
FILE: scripts/warmup-cache.js
=================================

#!/usr/bin/env node
/**
 * warmup-cache.js
 * Pre-popola la cache ottimizzando un set di istruzioni comuni.
 * Utile per evitare cache miss in produzione su prompt noti.
 *
 * Uso:
 *   node scripts/warmup-cache.js "instruction 1" "instruction 2" ...
 */
require("dotenv").config();

const { PromptManager } = require("../core/prompt-manager");
const { PROVIDERS, PROMPT_CACHE_CONFIG } = require("../constants");
const KeyPool = require("../core/key-pool");
const Logger = require("../core/logger");

const instructions = process.argv.slice(2);

if (instructions.length === 0) {
  console.log("Uso: node warmup-cache.js \"istruzione 1\" \"istruzione 2\" ...");
  process.exit(1);
}

async function main() {
  const keyPool = new KeyPool(PROVIDERS);
  const logger = new Logger();
  const manager = new PromptManager(keyPool, null, logger, {
    cache: PROMPT_CACHE_CONFIG,
    optimizeOnMiss: true
  });

  console.log(`\n🔄 Warmup avviato per ${instructions.length} prompt...\n`);

  for (const instruction of instructions) {
    const preview = instruction.substring(0, 50) + (instruction.length > 50 ? "..." : "");
    process.stdout.write(`  → "${preview}" ... `);
    try {
      const result = await manager.prepare({ instruction });
      if (result.fromCache) {
        console.log("⚡ già in cache");
      } else if (result.fromOptimizer) {
        console.log(`✅ ottimizzato (-${result.stats.reductionPercent}%)`);
      } else {
        console.log("⚠️  fallback originale");
      }
    } catch (e) {
      console.log(`❌ errore: ${e.message}`);
    }
  }

  const stats = manager.getCacheStats();
  console.log(`\n📊 Cache ora contiene ${stats.totalEntries} entry.\n`);
}

main().catch(console.error);

=================================
FILE: tests/test.js
=================================

const  { writeFile } = require('fs/promises');
const { freeCallApi } = require('../index');

const promptText = `Agisci come un **Senior E-Commerce SEO Specialist e Conversion Copywriter** con oltre 10 anni di esperienza in Shopify, SEO tecnica, Google Shopping, SEO semantica e ottimizzazione delle schede prodotto, specializzato nel settore **[INSERISCI SETTORE: es. ricambi auto, giardinaggio, moto]**.

# MISSIONE

Riceverai un array JSON contenente uno o più prodotti e dovrai trasformare **ogni prodotto** in una scheda prodotto SEO completa, persuasiva e orientata alla conversione.

L'obiettivo è massimizzare contemporaneamente:

1. Visibilità nella ricerca organica di Google.
2. Rilevanza semantica della pagina.
3. Performance su Google Shopping.
4. CTR nei risultati di ricerca.
5. Tasso di conversione.
6. Chiarezza e utilità per l'utente.
7. Comprensione del prodotto da parte dei motori di ricerca.
8. Coerenza con l'intento di ricerca reale dell'utente.

**IMPORTANTE:** devi ottimizzare i contenuti sulla base delle informazioni realmente presenti nell'input. Non devi inventare caratteristiche, compatibilità, prestazioni, materiali, dimensioni, certificazioni, garanzie o altri dati non esplicitamente disponibili.

---

# INPUT

Riceverai un array JSON di prodotti.

I campi possono variare da prodotto a prodotto. Utilizza esclusivamente le informazioni effettivamente presenti.

Esempio:

[
  {
    "Codice prodotto": 3538,
    "Riferimento": "V3538",
    "Nome": "CERCHIO POSTERIORE 110/90-18 PERNO 25 PER CROSS KAYO T4 250cc",
    "Sommario": "<p>402000-0049</p>",
    "Prezzo (Tasse Escluse)": 122.942623,
    "Prezzo (Tasse Incluse)": 149.99,
    "Nomi delle categorie (x,y,z...)": "Home,Ricambi,Cerchi e mozzi,Ricambi per tipologia",
    "Quantità": 7,
    "URL immagine di copertina": "https://...",
    "tag": "ricambi-gomme"
  }
]

---

# REGOLE ASSOLUTE

## 1. INTEGRITÀ DEI DATI

Non modificare, correggere, reinterpretare o inventare:

* Codice prodotto
* Riferimento
* SKU
* EAN13
* Prezzi
* Quantità
* URL immagini
* Valori numerici presenti nell'input
* Specifiche tecniche
* Compatibilità dichiarate

Questi dati devono essere trattati come **dati sorgente immutabili**.

## 2. NON INVENTARE

Non inventare mai:

* caratteristiche tecniche;
* materiali;
* dimensioni;
* colori;
* prestazioni;
* compatibilità;
* modelli compatibili;
* anni di produzione;
* certificazioni;
* omologazioni;
* garanzie;
* tempi di spedizione;
* resi;
* disponibilità;
* accessori inclusi;
* vantaggi tecnici non dimostrabili.

Se un'informazione non è disponibile, **non inserirla**.

## 3. SEO SENZA KEYWORD STUFFING

Non ripetere artificialmente la stessa keyword.

Usa:

* sinonimi;
* varianti grammaticali;
* keyword correlate;
* entità semantiche;
* termini utilizzati realmente dagli utenti;
* specifiche tecniche presenti nell'input.

La leggibilità e la naturalezza hanno sempre priorità rispetto alla densità delle keyword.

## 4. TERMINOLOGIA UTENTE > TERMINOLOGIA INTERNA

Quando il nome del prodotto contiene termini tecnici, abbreviati o utilizzati internamente dal fornitore, identifica mentalmente quali termini sono più comprensibili e ricercabili dagli utenti.

Esempio:

* "cerchio posteriore" può essere preferibile a una denominazione interna;
* "ricambio monopattino elettrico" può essere più utile di un codice interno;
* "parafango anteriore" deve essere mantenuto quando rappresenta il termine realmente utilizzato dagli utenti.

**Non sostituire però mai una specifica tecnica con un'altra non equivalente.**

## 5. MAI ALL CAPS

Non utilizzare il maiuscolo integrale nei contenuti.

Sono consentite sigle e unità tecniche quando corrette, ad esempio:

* cc
* kW
* rpm
* EAN
* SKU
* LED
* ABS

## 6. OUTPUT

Restituisci **esclusivamente JSON valido**.

Nessun:

* testo introduttivo;
* testo conclusivo;
* markdown;
* code block;
* commento;
* spiegazione;
* nota.

Il risultato deve poter essere passato direttamente a:

JSON.parse()

## 7. HTML

Tutto l'HTML prodotto deve essere:

* valido;
* semanticamente corretto;
* pulito;
* senza CSS inline;
* senza classi;
* senza JavaScript;
* senza <div> inutili.

Utilizza preferibilmente:

<p>, <h3>, <ul>, <li>, <strong>, <table>, <thead>, <tbody>, <tr>, <th>, <td>.

---

# ANALISI SEO INTERNA OBBLIGATORIA

Prima di generare l'output, analizza mentalmente ogni prodotto.

Non mostrare questa analisi nell'output.

Determina:

## 1. Keyword principale

Qual è la query più probabile utilizzata da un potenziale cliente per trovare questo prodotto?

## 2. Keyword secondarie

Individua varianti e long-tail pertinenti.

## 3. Intento di ricerca

Determina principalmente se l'utente ha intento:

* transazionale;
* commerciale;
* informativo;
* navigazionale.

Per una scheda prodotto dai priorità all'intento **transazionale/commerciale** quando appropriato.

## 4. Buyer persona

Determina chi è probabilmente l'acquirente:

* privato;
* appassionato;
* meccanico;
* professionista;
* genitore;
* proprietario del veicolo;
* ecc.

Solo quando è deducibile dal prodotto.

## 5. USP

Identifica il principale elemento distintivo del prodotto **solo sulla base dei dati disponibili**.

## 6. Obiezioni

Individua le possibili domande o dubbi dell'acquirente, ma affrontali esclusivamente quando l'input contiene informazioni sufficienti per rispondere.

## 7. Entità e semantica

Identifica:

* prodotto;
* categoria;
* sottocategoria;
* marca/modello;
* tipologia;
* dimensioni;
* codici;
* compatibilità;
* caratteristiche tecniche.

Usa queste informazioni per costruire un contenuto semanticamente ricco.

---

# OUTPUT

La chiave principale dell'oggetto deve essere sempre il valore di **"Codice prodotto" convertito in stringa**.

Struttura:

{
  "3538": {
    "nome": "...",
    "sommario": "...",
    "descrizione": "...",
    "meta_title": "...",
    "meta_description": "...",
    "target_keywords": [
      "...",
      "...",
      "...",
      "...",
      "..."
    ],
    "h1_suggestion": "...",
    "url_handle_suggestion": "...",
    "image_alt_text": "...",
    "faq_schema": [
      {
        "question": "...",
        "answer": "..."
      },
      {
        "question": "...",
        "answer": "..."
      },
      {
        "question": "...",
        "answer": "..."
      }
    ]
  }
}

---

# SPECIFICHE DEI CAMPI

## NOME

Obiettivo: creare un titolo comprensibile, ricercabile e ottimizzato per SEO e Shopping.

### Regole

* Preferibilmente 50-70 caratteri.
* Massimo assoluto: 100 caratteri.
* Inserisci la keyword principale in modo naturale.
* Inserisci una specifica realmente presente nell'input quando utile.
* Evita keyword stuffing.
* Evita ripetizioni.
* Non utilizzare ALL CAPS.
* Non aggiungere informazioni non presenti.

Struttura consigliata:

**[Prodotto] + [specifica principale] + [compatibilità/modello]**

Esempio:

"Parafango Anteriore per Monopattino Elettrico Chaos Gold"

Non copiare semplicemente il nome originale: **riscrivilo per renderlo più naturale e utile all'utente.**

---

# SOMMARIO

Genera un breve testo commerciale immediatamente comprensibile.

### Regole

* 150-250 caratteri circa.
* Deve essere un unico <p>.
* 2-3 frasi.
* Inserisci la keyword principale naturalmente.
* Comunica cosa è il prodotto.
* Specifica a cosa serve.
* Evidenzia il beneficio principale quando deducibile.
* Non inventare informazioni.

Esempio:

<p>Parafango anteriore per monopattino elettrico Chaos Serie Gold, ideale per sostituire il componente originale e mantenere il veicolo in condizioni ottimali.</p>

---

# DESCRIZIONE

La descrizione deve essere completa, utile e orientata alla conversione.

### Lunghezza

Indicativamente 300-600 parole **quando la quantità di informazioni disponibili lo consente**.

**Non aggiungere testo artificiale solo per raggiungere il numero minimo di parole.**

### Struttura obbligatoria

1. Introduzione
2. Caratteristiche principali
3. Specifiche tecniche
4. Perché scegliere il prodotto
5. Chiusura orientata all'acquisto

Struttura HTML:

<p>...</p>

<h3>Caratteristiche principali</h3>

<ul>
  <li>...</li>
  <li>...</li>
</ul>

<h3>Specifiche tecniche</h3>

<table>
  <thead>
    <tr>
      <th>Caratteristica</th>
      <th>Dettaglio</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>...</td>
      <td>...</td>
    </tr>
  </tbody>
</table>

<h3>Perché scegliere questo prodotto</h3>

<p>...</p>

### Bullet point

Quando le informazioni disponibili lo permettono, crea almeno 5 bullet point.

Ogni bullet dovrebbe trasformare:

**caratteristica → utilità → beneficio**

Esempio:

<strong>Dimensione 110/90-18:</strong> misura specifica del cerchio, utile per individuare rapidamente il ricambio corretto.

Non inventare benefici tecnici che non derivano dalla caratteristica.

### Specifiche

Se nell'input sono presenti specifiche tecniche:

* riportale fedelmente;
* riorganizzale;
* rendile leggibili;
* non modificarne il significato.

Se le specifiche sono insufficienti, non creare una tabella artificiale.

### Importante

Non inserire automaticamente frasi come:

* "spedizione gratuita";
* "reso 30 giorni";
* "garanzia ufficiale";
* "consegna rapida";
* "assistenza dedicata";

a meno che tali informazioni siano presenti nell'input.

---

# META TITLE

### Regole

* Massimo 60 caratteri.
* Keyword principale il più possibile vicina all'inizio.
* Deve essere diverso dall'H1.
* Deve essere naturale.
* Deve incentivare il click.
* Non fare keyword stuffing.

Struttura consigliata:

**[Keyword principale] | [specifica/beneficio]**

---

# META DESCRIPTION

### Regole

* Target: 140-160 caratteri.
* Massimo: 160 caratteri.
* Keyword principale naturale.
* Deve spiegare cosa vende la pagina.
* Deve incentivare il click.
* CTA solo quando naturale.
* Massimo 1 emoji e solo se realmente appropriata.
* Non inventare promozioni, spedizioni, resi o garanzie.

Evita FOMO artificiale come:

"Affrettati!"
"Ultima occasione!"
"Offerta imperdibile!"

se non supportata dai dati disponibili.

---

# TARGET_KEYWORDS

Genera esattamente **5 keyword**.

Ordine:

1. Keyword principale.
2. Long-tail principale.
3. Long-tail secondaria.
4. Variante semantica.
5. Keyword con intento commerciale o specifico.

Le keyword devono essere:

* pertinenti;
* realistiche;
* naturali;
* coerenti con il prodotto.

Non inserire keyword generiche non pertinenti solo per aumentare il volume.

---

# H1_SUGGESTION

Genera un H1 diverso dal meta title.

### Regole

* Massimo 70 caratteri.
* Descrittivo.
* Naturale.
* Deve identificare chiaramente il prodotto.
* Deve contenere la keyword principale quando possibile.
* Meno commerciale del titolo prodotto.

---

# URL_HANDLE_SUGGESTION

Genera uno slug SEO-friendly.

### Regole

* minuscolo;
* parole separate da -;
* niente caratteri speciali;
* niente accenti;
* niente stop word inutili;
* niente codici casuali;
* niente keyword ripetute;
* mantieni le specifiche importanti;
* non inserire informazioni inventate.

Esempio:

cerchio-posteriore-110-90-18-cross-kayo-t4

---

# IMAGE_ALT_TEXT

Genera un ALT descrittivo.

### Regole

* Massimo 125 caratteri.
* Descrivi ciò che rappresenta l'immagine sulla base dei dati disponibili.
* Inserisci la keyword principale quando naturale.
* Non utilizzare keyword stuffing.
* Non scrivere "immagine di".
* Non inventare il colore o dettagli visivi non disponibili nei dati.

---

# FAQ_SCHEMA

Genera esattamente **3 FAQ**.

Le domande devono essere pertinenti al prodotto e basate sulle informazioni disponibili.

Le domande devono riflettere dubbi realistici di un acquirente, ad esempio:

* compatibilità;
* utilizzo;
* dimensioni;
* modello;
* installazione;
* caratteristiche;
* codice prodotto.

### Regole

* 3 domande esatte.
* Risposte concise.
* Massimo 150 caratteri per risposta.
* Non inventare informazioni.
* Se una domanda richiederebbe informazioni non disponibili, formulala in modo che la risposta possa essere basata sui dati forniti.

---

# PRINCIPI DI COPYWRITING

Applica questi principi:

## AIDA

* Attention
* Interest
* Desire
* Action

## Feature → Benefit

Trasforma le caratteristiche in vantaggi concreti **solo quando il vantaggio è logicamente supportato dalla caratteristica**.

## Linguaggio

Utilizza:

* italiano naturale;
* tono professionale;
* linguaggio orientato all'acquisto;
* frasi concise;
* terminologia comprensibile;
* seconda persona quando appropriato.

Evita:

* "ottimo prodotto";
* "fantastico";
* "incredibile";
* "qualità top";
* "imperdibile";
* affermazioni generiche senza prove;
* promesse non supportate.

Preferisci dati e informazioni concrete.

---

# REGOLE SEO AVANZATE

## 1. Search Intent First

Scrivi prima per soddisfare l'intento dell'utente e solo successivamente per i motori di ricerca.

## 2. Semantic SEO

Non limitarti alla keyword principale.

Utilizza naturalmente:

* sinonimi;
* termini correlati;
* caratteristiche;
* categorie;
* modelli;
* codici;
* specifiche;
* termini tecnici pertinenti.

## 3. Zero Keyword Stuffing

La stessa keyword non deve essere ripetuta artificialmente.

## 4. Product Entity

Rendi inequivocabile:

**che cosa è il prodotto + a cosa serve + per quale modello/veicolo è destinato**, quando tali informazioni sono disponibili.

## 5. Conversione

Ogni contenuto deve aiutare l'utente a capire:

* cosa sta acquistando;
* se è il prodotto corretto;
* quale problema risolve;
* quali caratteristiche possiede.

## 6. Accuratezza > Lunghezza

Se l'input contiene poche informazioni, produci un contenuto più breve ma accurato.

**Non riempire la descrizione con contenuto generico o inventato.**

---

# CONTROLLO FINALE OBBLIGATORIO

Prima di restituire il JSON verifica mentalmente:

1. Il JSON è sintatticamente valido?
2. Ogni codice prodotto è presente come chiave?
3. Nessun dato originale è stato modificato?
4. Nessuna specifica è stata inventata?
5. Non esistono frasi in ALL CAPS?
6. Il nome è SEO-friendly?
7. Il meta title è entro 60 caratteri?
8. La meta description è entro 160 caratteri?
9. Esistono esattamente 5 target keywords?
10. Esistono esattamente 3 FAQ?
11. L'H1 è diverso dal meta title?
12. L'URL handle è valido?
13. L'ALT text è entro 125 caratteri?
14. L'HTML è valido?
15. Non sono presenti markdown o code block?
16. Non sono presenti informazioni commerciali inventate?
17. Il contenuto è naturale e non presenta keyword stuffing?
18. Ogni affermazione tecnica è supportata dall'input?

Se una regola entra in conflitto con un'altra, applica questa priorità:

**accuratezza dei dati > non invenzione > validità JSON > chiarezza > search intent > SEO > conversione > lunghezza.**

---

# OUTPUT FINALE

Rispondi **SOLTANTO con un JSON valido**.

Nessun testo prima o dopo.

Nessun markdown.

Nessun code block.

Il risultato deve essere direttamente utilizzabile con JSON.parse().

L'input è il seguente:

[INSERISCI ARRAY JSON DEI PRODOTTI]
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
    prompt : promptText,
    input: input
}

async function apiCall(freeApiObj) {
    const freeApi = await freeCallApi(freeApiObj);
    writeFile(`${__dirname}/response.txt`, typeof freeApi !== 'string' ? JSON.stringify(freeApi) : freeApi)
    
}

apiCall()



=================================
FILE: utils/errors.js
=================================

/**
 * utils/errors.js (FIXED v3)
 * FIX: aggiunto INVALID_REQUEST per 400 generici (maxTokens, parametri errati).
 *      Ora un 400 su maxOutputTokens troppo alto NON causa loop infinito.
 */

class FreeAIAPIError extends Error {
    constructor(message, { status, provider, model, keyId, attempts, type, metadata } = {}) {
        super(message);
        this.name = "FreeAIAPIError";
        this.status = status;
        this.provider = provider;
        this.model = model;
        this.keyId = keyId;
        this.attempts = attempts;
        this.type = type || "UNKNOWN";
        this.metadata = metadata || {};
    }
}

class PromptTooLongError extends FreeAIAPIError {
    constructor(message, meta) {
        super(message, meta);
        this.name = "PromptTooLongError";
    }
}

class RateLimitError extends FreeAIAPIError {
    constructor(message, meta) {
        super(message, meta);
        this.name = "RateLimitError";
    }
}

class QuotaExhaustedError extends FreeAIAPIError {
    constructor(message, meta) {
        super(message, meta);
        this.name = "QuotaExhaustedError";
    }
}

const ErrorTypes = {
    TIMEOUT: "TIMEOUT",
    RATE_LIMIT_RPM: "RATE_LIMIT_RPM",
    RATE_LIMIT_TPM: "RATE_LIMIT_TPM",
    QUOTA_EXHAUSTED: "QUOTA_EXHAUSTED",
    INVALID_KEY: "INVALID_KEY",
    INVALID_REQUEST: "INVALID_REQUEST",
    MODEL_NOT_FOUND: "MODEL_NOT_FOUND",
    PROMPT_TOO_LARGE: "PROMPT_TOO_LARGE",
    PAYLOAD_TOO_LARGE: "PAYLOAD_TOO_LARGE",
    NO_CAPACITY: "NO_CAPACITY",
    UNKNOWN: "UNKNOWN"
};

function classifyError(rawError, provider) {
    const status = rawError.status || rawError.statusCode || 0;
    const message = rawError.message || "";
    const code = rawError.code || "";
    const body = rawError.body || {};
    const bodyMsg = body.error?.message || "";
    const bodyCode = body.error?.code || "";

    if (provider === "groq") {
        if (status === 413 && (bodyCode === "rate_limit_exceeded" || message.includes("rate_limit_exceeded"))) {
            if (bodyMsg.includes("tokens per minute") || message.includes("tokens per minute")) {
                return new FreeAIAPIError(
                    "Groq TPM limit exceeded (free tier).",
                    { status: 413, provider, type: ErrorTypes.RATE_LIMIT_TPM, metadata: { retryable: false, cooldownMs: 60000, changeProvider: true } }
                );
            }
            return new FreeAIAPIError("Groq rate limit exceeded", { status: 413, provider, type: ErrorTypes.RATE_LIMIT_RPM, metadata: { retryable: true, cooldownMs: 30000 } });
        }
        if (status === 429) {
            return new FreeAIAPIError("Groq RPM limit exceeded", { status: 429, provider, type: ErrorTypes.RATE_LIMIT_RPM, metadata: { retryable: true, cooldownMs: 30000 } });
        }
        if (status === 413 && !bodyMsg.includes("rate_limit") && !message.includes("rate_limit")) {
            return new FreeAIAPIError("Groq payload too large", { status: 413, provider, type: ErrorTypes.PAYLOAD_TOO_LARGE, metadata: { retryable: false, skipModel: true } });
        }
    }

    if (provider === "gemini") {
        if (status === 429 || bodyMsg.includes("Quota exceeded")) {
            return new FreeAIAPIError("Gemini rate limit or quota exceeded", { status: 429, provider, type: ErrorTypes.RATE_LIMIT_RPM, metadata: { retryable: true, cooldownMs: 60000 } });
        }
        if (status === 413) {
            return new FreeAIAPIError("Gemini request too large", { status: 413, provider, type: ErrorTypes.PAYLOAD_TOO_LARGE, metadata: { retryable: false } });
        }
    }

    if (status === 400) {
        if ((provider === "gemini" && message.includes("API key not valid")) || message.includes("invalid api key")) {
            return new FreeAIAPIError("API Key invalid", { status: 400, provider, type: ErrorTypes.INVALID_KEY, metadata: { retryable: false, invalidateKey: true } });
        }
        return new FreeAIAPIError("Invalid request parameters", { status: 400, provider, type: ErrorTypes.INVALID_REQUEST, metadata: { retryable: false, skipModel: true } });
    }
    if (status === 401 || status === 403) {
        return new FreeAIAPIError("API Key invalid or unauthorized", { status, provider, type: ErrorTypes.INVALID_KEY, metadata: { retryable: false, invalidateKey: true } });
    }
    if (status === 404) {
        return new FreeAIAPIError("Model not found", { status: 404, provider, type: ErrorTypes.MODEL_NOT_FOUND, metadata: { retryable: false, skipModel: true } });
    }
    if (status === 429) {
        return new FreeAIAPIError("Rate limit exceeded", { status: 429, provider, type: ErrorTypes.RATE_LIMIT_RPM, metadata: { retryable: true, cooldownMs: 60000 } });
    }
    if (status === 413) {
        return new FreeAIAPIError("Payload too large", { status: 413, provider, type: ErrorTypes.PAYLOAD_TOO_LARGE, metadata: { retryable: false } });
    }
    if (status === 500) {
        return new FreeAIAPIError("Provider internal error", { status: 500, provider, type: ErrorTypes.UNKNOWN, metadata: { retryable: true, cooldownMs: 5000 } });
    }
    if (rawError.name === "AbortError" || code === "ETIMEDOUT" || code === "ECONNRESET") {
        return new FreeAIAPIError("Request timeout or aborted", { status: 408, provider, type: ErrorTypes.TIMEOUT, metadata: { retryable: true, retrySameKey: true, maxRetries: 1 } });
    }

    return new FreeAIAPIError(message || "Unknown error", { status, provider, type: ErrorTypes.UNKNOWN, metadata: { retryable: false } });
}

module.exports = {
    FreeAIAPIError,
    PromptTooLongError,
    RateLimitError,
    QuotaExhaustedError,
    ErrorTypes,
    classifyError
};

=================================
FILE: utils/prompt-normalizer.js
=================================

/**
 * Prompt Normalizer
 * Normalizza il testo prima della generazione dell'hash per massimizzare
 * i cache hit su varianti testuali semanticamente identiche.
 */
function normalizePrompt(text) {
  if (!text || typeof text !== "string") return "";
  return text
    .toLowerCase()
    .replace(/\s+/g, " ")           // collassa spazi multipli
    .replace(/[.!?]+$/, "")          // rimuove punteggiatura finale
    .trim();
}

module.exports = { normalizePrompt };

=================================
FILE: utils/retry.js
=================================

async function withRetry(fn, { retries = 1, shouldRetry = () => true, onRetry = () => {} } = {}) {
    let lastError;
    for (let i = 0; i <= retries; i++) {
        try {
            return await fn();
        } catch (error) {
            lastError = error;
            if (i < retries && shouldRetry(error)) {
                onRetry(error, i + 1);
            } else {
                throw error;
            }
        }
    }
    throw lastError;
}

module.exports = { withRetry };


=================================
FILE: utils/token-estimator.js
=================================

/**
 * utils/token-estimator.js (FIXED v2)
 * FIX #10: stima accurata per tipo di contenuto (text/json/code/instruction).
 */

/**
 * Stima token in modo accurato in base al tipo di contenuto.
 * @param {string|object} content
 * @param {string} type - 'text', 'json', 'code', 'instruction'
 * @returns {number}
 */
function estimateTokens(content, type = "text") {
    if (!content) return 0;
    let text = typeof content === "string" ? content : JSON.stringify(content);
    const charCount = text.length;
    if (charCount === 0) return 0;

    const words = text.split(/\s+/).filter(w => w.length > 0);
    const wordCount = words.length;
    const specialChars = (text.match(/[{}[\]":,\n\t]/g) || []).length;
    const upperRatio = (text.match(/[A-Z]/g) || []).length / charCount;

    let charsPerToken = 3.8;
    switch (type) {
        case "json":  charsPerToken = 2.5; break;
        case "code":  charsPerToken = 3.0; break;
        case "instruction": charsPerToken = 3.2; break;
        default:
            if (upperRatio > 0.3) charsPerToken = 3.2;
            if (specialChars / charCount > 0.15) charsPerToken = 3.0;
    }

    const byChars = charCount / charsPerToken;
    const byWords = wordCount / 0.75;
    let estimate = (type === "json" || type === "code")
        ? byChars * 0.8 + byWords * 0.2
        : byChars * 0.6 + byWords * 0.4;

    estimate *= 1.05; // overhead formattazione
    estimate *= 1.15; // margine sicurezza 15%
    return Math.ceil(estimate);
}

module.exports = { estimateTokens };
