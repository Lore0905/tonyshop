
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

require("dotenv").config();

const DEFAULT_CONFIG = {
    timeoutMs: 30000,
    retryCount: 1
};

/**
 * maxInputTokens = massimo token inviabili in UNA singola richiesta
 * (può essere diverso dal context window nominale, es. Groq TPM limit)
 */
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

const PROVIDERS = {
    // Groq PRIORITARIO per chiamate normali
    groq: {
        enabled: true,
        priority: 1,
        endpoint: "https://api.groq.com/openai/v1",
        models: ["openai/gpt-oss-120b"],
        apiKeys: [
            {
                id: "groq_1",
                value: process.env.GROQ_KEY || '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',
                rpmLimit: 30,
                dailyLimit: 14400,
                cooldownMs: 30000
            }
        ]
    },
    // Gemini secondario per chiamate, prioritario per compressione
    gemini: {
        enabled: true,
        priority: 2,
        endpoint: "https://generativelanguage.googleapis.com/v1beta",
        models: ["gemini-flash-latest"],
        apiKeys: [
            {
                id: "gemini_key_1",
                value: process.env.GEMINI_KEY_1 || 'AQ.Ab8RN6IITcwZl5EoNxVxCTduS-RaRiQR2owioe6x6OgzVaAmkg',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_2",
                value: process.env.GEMINI_KEY_2 || 'AQ.Ab8RN6KkXQKPox1i0WGRK_tLsThhz4WAPmpBRLRxGgZGI1L41w',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_3",
                value: process.env.GEMINI_KEY_3 || 'AQ.Ab8RN6LZcN2qzGJtSRl0V1DfKA0PZQIM1CfRICGanILcNloBRA',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_4",
                value: process.env.GEMINI_KEY_4 || 'AQ.Ab8RN6K_iECj-ahsGbuseWDl9sBZhLL9fpHOzWVxkvt3GhE-2g',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_5",
                value: process.env.GEMINI_KEY_5 || 'AQ.Ab8RN6K7ACWFgqLUzn8cq2Fbxmkm8Ha-xZET8LuauyggNJyiZQ',
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
    MODEL_LIMITS
};

=================================
FILE: core/compressor.js
=================================

const { estimateTokens } = require("../utils/token-estimator");
const { MODEL_LIMITS, PROVIDERS } = require("../constants");
const ProviderClient = require("./provider-client");

/**
 * Compressor - gestisce la compressione semantica dei prompt troppo lunghi.
 * 
 * Per le chiamate normali gli altri provider hanno priorità.
 * Per la compressione invece Gemini ha la priorità assoluta
 * per non consumare le quote dei provider principali.
 */
class Compressor {
  constructor(keyPool, registry, logger) {
    this.keyPool = keyPool;
    this.registry = registry;
    this.logger = logger;
    this.providerClient = new ProviderClient(keyPool, logger);
  }

  async compress(prompt, targetModel, targetProvider) {
    const promptTokens = estimateTokens(prompt);
    const targetLimit = this.getModelLimit(targetModel);
    
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
    const compressionTokens = estimateTokens(compressionPrompt);
    
    const compressorLimit = this.getModelLimit(compressor.model);
    if (compressionTokens > compressorLimit) {
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
          maxTokens: Math.min(8192, Math.floor(compressorLimit * 0.5))
        }
      });

      const duration = Date.now() - startTime;
      const compressedText = result.text || "";
      const compressedTokens = estimateTokens(compressedText);
      
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
   * PRIORITÀ: Gemini PRIMA, poi altri provider per context window maggiore.
   */
  findCompressorModel(promptTokens) {
    const candidates = [];
    
    for (const [provName, provConfig] of Object.entries(PROVIDERS)) {
      if (!provConfig.enabled) continue;
      
      for (const modelName of provConfig.models || []) {
        const limit = this.getModelLimit(modelName);
        if (limit > promptTokens * 1.2) {
          candidates.push({
            provider: provName,
            model: modelName,
            limit,
            isGemini: provName === "gemini"
          });
        }
      }
    }

    // ORDINAMENTO CORRETTO: Gemini PRIMA, poi per context window decrescente
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

  getModelLimit(modelName) {
    return MODEL_LIMITS[modelName]?.contextWindow || 0;
  }

  buildCompressionPrompt(prompt, targetModel, targetProvider) {
    const targetLimit = this.getModelLimit(targetModel);
    
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
 * KeyPool - Gestione circolare API Key con cooldown, rate limit e tracciamento
 */
class KeyPool {
    constructor(providersConfig) {
        this.config = providersConfig;
        this.state = new Map();
        this.roundRobinIndex = new Map();

        for (const [providerName, providerConfig] of Object.entries(providersConfig)) {
            if (!providerConfig.enabled) continue;
            this.roundRobinIndex.set(providerName, 0);
            for (const key of providerConfig.apiKeys || []) {
                this.state.set(key.id, {
                    provider: providerName,
                    rpmRequests: [],
                    dailyRequests: [],
                    cooldownUntil: 0,
                    invalid: false
                });
            }
        }
    }

    /**
     * Restituisce la prossima API Key disponibile per il provider (Round Robin)
     * Non traccia ancora la richiesta (trackRequest va chiamato separatamente)
     */
    getNextKey(providerName) {
        const providerConfig = this.config[providerName];
        if (!providerConfig || !providerConfig.enabled) return null;

        const keys = providerConfig.apiKeys || [];
        if (keys.length === 0) return null;

        const startIndex = this.roundRobinIndex.get(providerName) || 0;

        for (let i = 0; i < keys.length; i++) {
            const idx = (startIndex + i) % keys.length;
            const key = keys[idx];

            if (this.isKeyAvailable(key.id)) {
                this.roundRobinIndex.set(providerName, (idx + 1) % keys.length);
                return key;
            }
        }

        return null;
    }

    /**
     * Verifica se una key è utilizzabile (non cooldown, non invalida, non limiti)
     */
    isKeyAvailable(keyId) {
        const state = this.state.get(keyId);
        if (!state) return false;
        if (state.invalid) return false;
        if (Date.now() < state.cooldownUntil) return false;

        const keyConfig = this.findKeyConfig(keyId);
        if (!keyConfig) return false;

        if (this.isRpmExceeded(keyConfig)) return false;
        if (this.isDailyExceeded(keyConfig)) return false;

        return true;
    }

    findKeyConfig(keyId) {
        for (const [, config] of Object.entries(this.config)) {
            const key = config.apiKeys?.find(k => k.id === keyId);
            if (key) return key;
        }
        return null;
    }

    isRpmExceeded(key) {
        const state = this.state.get(key.id);
        const now = Date.now();
        const oneMinuteAgo = now - 60000;
        state.rpmRequests = state.rpmRequests.filter(t => t > oneMinuteAgo);
        return state.rpmRequests.length >= key.rpmLimit;
    }

    isDailyExceeded(key) {
        const state = this.state.get(key.id);
        const now = Date.now();
        const oneDayAgo = now - 86400000;
        state.dailyRequests = state.dailyRequests.filter(t => t > oneDayAgo);
        return state.dailyRequests.length >= key.dailyLimit;
    }

    trackRequest(keyId) {
        const state = this.state.get(keyId);
        if (!state) return;
        const now = Date.now();
        state.rpmRequests.push(now);
        state.dailyRequests.push(now);
    }

    setCooldown(keyId, durationMs) {
        const state = this.state.get(keyId);
        if (state) {
            state.cooldownUntil = Date.now() + durationMs;
        }
    }

    invalidate(keyId) {
        const state = this.state.get(keyId);
        if (state) {
            state.invalid = true;
        }
    }

    getProviderConfig(providerName) {
        return this.config[providerName];
    }
}

module.exports = KeyPool;


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
FILE: core/provider-client.js
=================================

const registry = require("../registry");

/**
 * ProviderClient - layer centrale di comunicazione provider
 *
 * Collega Registry e KeyPool, gestisce errori comuni,
 * predisposto per multi-provider
 */
class ProviderClient {
    constructor(keyPool, logger) {
        this.keyPool = keyPool;
        this.logger = logger;
    }

    /**
     * Esegue una richiesta verso un provider AI
     *
     * @param {Object} options
     * @param {String} options.provider
     * @param {Object} options.payload - contiene key, prompt, model, temperature, maxTokens, abortSignal
     */
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
            // Traccia la richiesta contata dal provider solo su successo
            this.keyPool.trackRequest(key.id);
            return result;
        } catch (error) {
            this.handleError(provider, key, error);
            throw error;
        }
    }

    /**
     * Gestione errori comuni provider
     */
    handleError(providerName, key, error) {
        if (!error) return;

        const status = error.status;

        // Rate limit -> cooldown
        if (status === 429) {
            const providerConfig = this.keyPool.getProviderConfig(providerName);
            const cooldown = providerConfig?.apiKeys?.find(k => k.id === key.id)?.cooldownMs || 60000;
            this.keyPool.setCooldown(key.id, cooldown);
        }

        // Chiave non valida -> invalida permanentemente per questa esecuzione
        if (status === 401 || status === 403) {
            this.keyPool.invalidate(key.id);
        }
    }
}

module.exports = ProviderClient;


=================================
FILE: core/router.js
=================================

const { DEFAULT_CONFIG, PROVIDERS, MODEL_LIMITS } = require("../constants");
const ProviderClient = require("./provider-client");
const { estimateTokens } = require("../utils/token-estimator");
const Compressor = require("./compressor");

class Router {
  constructor(keyPool, registry, logger) {
    this.keyPool = keyPool;
    this.registry = registry;
    this.logger = logger;
    this.providerClient = new ProviderClient(keyPool, logger);
    this.compressor = new Compressor(keyPool, registry, logger);
    this.attempts = [];
  }

  async route(options) {
    const { prompt, provider, model, temperature, maxTokens, compress } = options;

    if (!prompt) {
      throw new Error("prompt is required");
    }

    this.attempts = [];
    const promptTokens = estimateTokens(prompt);

    const providerList = this.resolveProviders(provider);

    for (const provName of providerList) {
      const provConfig = PROVIDERS[provName];
      if (!provConfig || !provConfig.enabled) continue;

      const modelList = this.resolveModels(provConfig, model);

      for (const modelName of modelList) {
        const modelLimit = MODEL_LIMITS[modelName]?.maxInputTokens || 0;
        let effectivePrompt = prompt;

        // --- CHECK PRE-CHIAMATA ---
        if (modelLimit > 0 && promptTokens > modelLimit) {
          if (compress === false) {
            this.logger.fallback({
              provider: provName,
              model: modelName,
              reason: `Prompt too long (${promptTokens} > ${modelLimit} maxInputTokens, compress disabled)`
            });
            continue; // Salta modello, prova successivo
          }

          // compress: true (default) -> tenta compressione
          try {
            effectivePrompt = await this.compressor.compress(prompt, modelName, provName);
          } catch (compressionError) {
            this.logger.fallback({
              provider: provName,
              model: modelName,
              reason: `Compression failed: ${compressionError.message}`
            });
            continue; // Salta modello se compressione fallisce
          }
        }

        let key;
        while ((key = this.keyPool.getNextKey(provName)) !== null) {
          const attempt = {
            provider: provName,
            model: modelName,
            keyId: key.id,
            error: null
          };

          try {
            const result = await this.executeWithRetry({
              provider: provName,
              model: modelName,
              key,
              prompt: effectivePrompt,
              temperature,
              maxTokens
            });

            return result;
          } catch (error) {
            attempt.error = error;
            this.attempts.push(attempt);

            const status = error.status;

            // 413 Payload Too Large -> cambia modello (come 500/404)
            // Può succedere se l'API ha un limite più basso di quanto dichiarato
            if (status === 413 || status === 500 || status === 404) {
              this.logger.fallback({
                provider: provName,
                model: modelName,
                reason: `HTTP ${status}`
              });
              break; // Esci dal while key, passa al prossimo modello
            }

            // Rate limit (429), key invalida (401/403), timeout, rete
            // -> continua con prossima key
          }
        }
      }
    }

    // Se compress: false e nessun modello provato perché tutti troppo corti
    if (compress === false && this.attempts.length === 0) {
      const allLimits = [];
      for (const p of providerList) {
        const cfg = PROVIDERS[p];
        for (const m of cfg.models || []) {
          const l = MODEL_LIMITS[m]?.maxInputTokens || 0;
          allLimits.push(`${p}/${m}: ${l}`);
        }
      }
      const err = new Error(
        `Prompt too long for all available models (${promptTokens} tokens). ` +
        `Model limits: ${allLimits.join(", ")}`
      );
      err.status = 413;
      err.attempts = [];
      throw err;
    }

    throw this.buildFinalError("All providers, models and keys exhausted");
  }

  resolveProviders(specifiedProvider) {
    if (specifiedProvider) {
      return [specifiedProvider];
    }
    return Object.entries(PROVIDERS)
      .filter(([, config]) => config.enabled)
      .sort(([, a], [, b]) => a.priority - b.priority)
      .map(([name]) => name);
  }

  resolveModels(providerConfig, specifiedModel) {
    if (specifiedModel) {
      return [specifiedModel];
    }
    return providerConfig.models || [];
  }

  async executeWithRetry({ provider, model, key, prompt, temperature, maxTokens }) {
    const payload = { prompt, model, temperature, maxTokens };

    const tryCall = async () => {
      const abortController = new AbortController();
      const timeoutId = setTimeout(() => abortController.abort(), DEFAULT_CONFIG.timeoutMs);

      try {
        return await this.providerClient.execute({
          provider,
          payload: { ...payload, key, abortSignal: abortController.signal }
        });
      } finally {
        clearTimeout(timeoutId);
      }
    };

    try {
      return await tryCall();
    } catch (error) {
      if (this.isTimeoutError(error)) {
        this.logger.warn("Timeout detected, retrying once with same key", {
          provider,
          model,
          keyId: key.id
        });
        return await tryCall();
      }
      throw error;
    }
  }

  isTimeoutError(error) {
    if (!error) return false;
    if (error.name === "AbortError") return true;
    if (error.status === 408) return true;
    if (error.message && (
      error.message.toLowerCase().includes("timeout") ||
      error.message.toLowerCase().includes("abort") ||
      error.message.toLowerCase().includes("etimeout")
    )) return true;
    return false;
  }

  buildFinalError(message) {
    const details = this.attempts.map(a =>
      `${a.provider}/${a.model} [${a.keyId}]: ${a.error?.message || "Unknown"}`
    ).join("; ");

    const error = new Error(`${message}. Attempts: ${details}`);
    error.attempts = this.attempts;
    return error;
  }
}

module.exports = Router;

=================================
FILE: index.js
=================================

require("dotenv").config();

const { PROVIDERS } = require("./constants");
const KeyPool = require("./core/key-pool");
const Logger = require("./core/logger");
const Router = require("./core/router");
const registry = require("./registry");

// Bootstrap: inizializza KeyPool una sola volta
const keyPool = new KeyPool(PROVIDERS);

/**
 * Effettua una chiamata AI gestendo automaticamente provider, modelli e API key
 *
 * @param {Object} options
 * @param {String} options.prompt - Testo del prompt (obbligatorio)
 * @param {String} [options.provider] - Provider da usare (es. "gemini")
 * @param {String} [options.model] - Modello da usare (es. "gemini-2.5-flash")
 * @param {Number} [options.temperature] - Temperatura di generazione
 * @param {Number} [options.maxTokens] - Numero massimo di token
 * @param {Object} [options.logger] - Logger custom (default: console)
 * @param {Boolean} [options.compress=true] - Se true, comprime automaticamente i prompt troppo lunghi
 * @returns {Promise<Object>} - Risposta AI: { text, model, provider, raw }
 */
async function freeCallApi(options = {}) {
  if (!options.prompt) {
    throw new Error("prompt is required");
  }

  const logger = new Logger(options.logger);
  const router = new Router(keyPool, registry, logger);

  return await router.route(options);
}

module.exports = {
  freeCallApi
};

=================================
FILE: package.json
=================================

{
  "name": "free_ai_api",
  "version": "1.0.0",
  "description": "Libreria per chiamate API AI gratuite con fallback automatico",
  "main": "index.js",
  "scripts": {
    "test": "echo \"Error: no test specified\" && exit 1"
  },
  "keywords": ["ai", "api", "free", "gemini", "openai", "fallback"],
  "author": "",
  "license": "MIT",
  "dependencies": {
    "dotenv": "^16.4.0"
  },
  "engines": {
    "node": ">=18.0.0"
  }
}

=================================
FILE: providers/gemini.js
=================================

/**
 * Provider Gemini - logica di business specifica
 *
 * Separa la gestione errori/formati di Gemini dal core
 */
class GeminiProvider {
    constructor(adapter, keyPool, logger) {
        this.adapter = adapter;
        this.keyPool = keyPool;
        this.logger = logger;
        this.name = "gemini";
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

            // Arricchisci l'errore con metadati per il router
            error.provider = this.name;
            error.model = model;
            error.keyId = key.id;
            throw error;
        }
    }
}

module.exports = GeminiProvider;


=================================
FILE: providers/groq.js
=================================

/**
 * Provider Groq - logica di business specifica
 *
 * Separa la gestione errori/formati di Groq dal core
 */
class GroqProvider {
    constructor(adapter, keyPool, logger) {
        this.adapter = adapter;
        this.keyPool = keyPool;
        this.logger = logger;
        this.name = "groq";
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

            // Arricchisci l'errore con metadati per il router
            error.provider = this.name;
            error.model = model;
            error.keyId = key.id;
            throw error;
        }
    }
}

module.exports = GroqProvider;

=================================
FILE: registry.js
=================================

const fs = require("fs");
const path = require("path");

/**
 * Registry singleton - carica automaticamente adapters e providers
 */
class Registry {
    constructor() {
        this.adapters = new Map();
        this.providers = new Map();
        this.loadAdapters();
        this.loadProviders();
    }

    loadAdapters() {
        const dir = path.join(__dirname, "adapters");
        if (!fs.existsSync(dir)) return;
        const files = fs.readdirSync(dir).filter(f => f.endsWith(".js"));
        for (const file of files) {
            const name = path.basename(file, ".js");
            const adapter = require(path.join(dir, file));
            this.adapters.set(name, adapter);
        }
    }

    loadProviders() {
        const dir = path.join(__dirname, "providers");
        if (!fs.existsSync(dir)) return;
        const files = fs.readdirSync(dir).filter(f => f.endsWith(".js"));
        for (const file of files) {
            const name = path.basename(file, ".js");
            const ProviderClass = require(path.join(dir, file));
            this.providers.set(name, ProviderClass);
        }
    }

    getAdapter(name) {
        return this.adapters.get(name);
    }

    getProviderClass(name) {
        return this.providers.get(name);
    }

    getProviderInstance(name, ...args) {
        const ProviderClass = this.providers.get(name);
        if (!ProviderClass) return null;
        return new ProviderClass(...args);
    }

    listProviders() {
        return Array.from(this.providers.keys());
    }

    listAdapters() {
        return Array.from(this.adapters.keys());
    }
}

module.exports = new Registry();


=================================
FILE: tests/single_api.js
=================================

require("dotenv").config();
const { freeCallApi } = require("../index");

async function test() {
    console.log("🚀 Test free_ai_api iniziato...\n");

    try {
        // Test 1: chiamata semplice (nessun provider specificato)
        console.log("--- Test 1: Chiamata automatica ---");
        const res1 = await freeCallApi({
            prompt: "Dimmi una curiosità scientifica in una frase.",
            model: "openai/gpt-oss-120b"
        });
        console.log("✅ Risposta:", res1.text);
        console.log("   Provider:", res1.provider);
        console.log("   Modello:", res1.model);

    } catch (err) {
        console.error("❌ Errore Test 1:", err.message);
    }
}

test();

=================================
FILE: utils/errors.js
=================================

class FreeAIAPIError extends Error {
    constructor(message, { status, provider, model, keyId, attempts } = {}) {
        super(message);
        this.name = "FreeAIAPIError";
        this.status = status;
        this.provider = provider;
        this.model = model;
        this.keyId = keyId;
        this.attempts = attempts;
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

module.exports = {
    FreeAIAPIError,
    PromptTooLongError,
    RateLimitError,
    QuotaExhaustedError
};


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
 * Token Estimator - stima conservativa del numero di token di un testo.
 * 
 * Euristiche:
 * - Testo prevalentemente ASCII (latino/inglese): ~3.5 caratteri/token
 * - Testo misto: ~2.5 caratteri/token  
 * - Testo prevalentemente non-ASCII (CJK, emoji, arabo): ~1.5 caratteri/token
 * - Margine di sicurezza del +20% (sovrastima intenzionale)
 * 
 * La sovrastima è voluta per evitare chiamate API che sarebbero destinate 
 * a fallire per eccesso di token.
 */
function estimateTokens(text) {
  if (!text || typeof text !== "string") return 0;
  
  const len = text.length;
  if (len === 0) return 0;
  
  let nonAscii = 0;
  for (let i = 0; i < len; i++) {
    if (text.charCodeAt(i) > 127) nonAscii++;
  }
  
  const asciiRatio = (len - nonAscii) / len;
  let factor;
  if (asciiRatio > 0.9) {
    factor = 3.5;
  } else if (asciiRatio > 0.5) {
    factor = 2.5;
  } else {
    factor = 1.5;
  }
  
  // Margine di sicurezza 20%
  return Math.ceil((len / factor) * 1.2);
}

module.exports = { estimateTokens };
