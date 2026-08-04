/**
 * core/router.js
 *
 * FIX:
 * - prevenzione loop infinito fallback
 * - gestione corretta TPM provider limit
 * - max attempts safety
 * - PromptManager singleton
 */

const { DEFAULT_CONFIG, PROVIDERS } = require("../constants");

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
      cache: {
        enabled: true,
      },
    });

    this.attempts = [];

    // protezione anti loop
    this.maxAttempts = 50;

    // provider temporaneamente bloccati
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

    /*
            PREPARAZIONE PROMPT
        */

    if (instruction) {
      const prepared = await this.promptManager.prepare({
        instruction,
        input,
      });

      effectivePrompt = prepared.prompt;

      fromCache = prepared.fromCache;

      promptHash = prepared.hash;

      promptTokens = TokenManager.estimate(
        effectivePrompt,
        typeof input === "object" ? "json" : "text",
      );
    } else {
      promptTokens = TokenManager.estimate(effectivePrompt, "text");
    }

    this.logger.info(`[Router] Estimated ${promptTokens} tokens`);

    /*
            CHUNKING PREVENTIVO
        */

    if (Array.isArray(input) && input.length > 0) {
      const probeProvider =
        provider || Object.keys(PROVIDERS).find((p) => PROVIDERS[p].enabled);

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

    /*
            CICLO PROVIDER
        */

    for (const provName of providers) {
      if (this.isProviderBlocked(provName)) {
        continue;
      }

      const provConfig = PROVIDERS[provName];

      if (!provConfig || !provConfig.enabled) {
        continue;
      }

      const models = this.resolveModels(provConfig, model);

      for (const modelName of models) {
        /*
                    TOKEN PREFLIGHT
                */

        const check = TokenManager.canHandleRequest(
          provName,
          modelName,
          promptTokens,
        );

        if (!check.ok) {
          this.logger.warn(
            `[Router] Skip ${provName}/${modelName}: ${check.reason}`,
          );

          this.attempts.push({
            provider: provName,
            model: modelName,
            status: "skipped",
            reason: check.reason,
            preflight: true,
          });

          if (compress) {
            try {
              effectivePrompt = await this.compressor.compress(
                effectivePrompt,
                modelName,
                provName,
              );

              promptTokens = TokenManager.estimate(effectivePrompt);

              const retry = TokenManager.canHandleRequest(
                provName,
                modelName,
                promptTokens,
              );

              if (!retry.ok) {
                continue;
              }
            } catch (e) {
              continue;
            }
          } else {
            continue;
          }
        }

        /*
                    KEY LOOP
                */

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

          try {
            const ProviderClass = this.registry.getProviderClass(provName);

            if (ProviderClass && ProviderClass.prototype.preFlightCheck) {
              const instance = new ProviderClass(
                null,
                this.keyPool,
                this.logger,
              );

              await instance.preFlightCheck({
                model: modelName,
                tokens: promptTokens,
                key,
              });
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

              fromCache,

              hash: promptHash,

              attempts: this.attempts,
            };
          } catch (error) {
            attempt.error = error;

            this.attempts.push(attempt);

            const classified = classifyError(error, provName);

            this.logger.warn(`[Router] ${provName} ${classified.type}`);

            switch (classified.type) {
              case ErrorTypes.RATE_LIMIT_TPM:
                /*
                                    FIX PRINCIPALE

                                    TPM è provider limit
                                    non key limit
                                */

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

              case ErrorTypes.PROMPT_TOO_LARGE:

              case ErrorTypes.PAYLOAD_TOO_LARGE:

              case ErrorTypes.MODEL_NOT_FOUND:
                key = null;

                break;

              default:
                break;
            }
          }
        }
      }
    }

    throw this.buildFinalError("All providers exhausted");
  }

  isProviderBlocked(provider) {
    const until = this.providerCooldown.get(provider);

    if (!until) {
      return false;
    }

    if (Date.now() > until) {
      this.providerCooldown.delete(provider);

      return false;
    }

    return true;
  }

  blockProvider(provider, ms) {
    this.providerCooldown.set(provider, Date.now() + ms);
  }

  async _routeChunked(options, instruction, input) {
    const provider = options.provider || Object.keys(PROVIDERS)[0];

    const model = options.model || PROVIDERS[provider].models[0];

    const chunks = this.chunkManager.splitArrayIntoChunks(
      input,
      instruction || options.prompt || "",
      provider,
      model,
    );

    const results = [];

    for (const chunk of chunks) {
      const res = await this.route({
        ...options,

        input: chunk,

        compress: false,
      });

      results.push(res.text || res);
    }

    return {
      text: results.join("\n"),

      chunked: true,

      chunksCount: chunks.length,
    };
  }

  resolveProviders(provider) {
    if (provider) {
      return [provider];
    }

    return Object.entries(PROVIDERS)

      .filter(([, cfg]) => cfg.enabled)

      .sort(([, a], [, b]) => a.priority - b.priority)

      .map(([name]) => name);
  }

  resolveModels(config, model) {
    if (model) {
      return [model];
    }

    return config.models || [];
  }

  async executeWithRetry({
    provider,
    model,
    key,
    prompt,
    temperature,
    maxTokens,
  }) {
    const controller = new AbortController();

    const timer = setTimeout(
      () => controller.abort(),
      DEFAULT_CONFIG.timeoutMs,
    );

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
      .map(
        (a) =>
          `${a.provider}/${a.model} ${a.keyId}: ${a.error?.message || a.reason}`,
      )
      .join("; ");

    const err = new Error(`${message}. Attempts: ${details}`);

    err.attempts = this.attempts;

    return err;
  }
}

module.exports = Router;
