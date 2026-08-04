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