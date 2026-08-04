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