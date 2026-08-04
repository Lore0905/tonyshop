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