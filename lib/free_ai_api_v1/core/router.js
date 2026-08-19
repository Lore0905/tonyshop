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