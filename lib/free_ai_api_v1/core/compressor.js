/**
 * core/compressor.js (FIXED v3 — STRUCTURAL LOSSLESS)
 *
 * FIX: il prompt di compressione ora impone esplicitamente la conservazione
 *      del 100% della struttura, formato, nomi campi, JSON, sezioni, marker.
 *      Aggiunta validazione post-compressione: se il formato è stato alterato,
 *      la compressione viene rifiutata e lanciato un errore 413.
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

    this.logger.info("Prompt exceeds target model limit, initiating structural compression", {
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
          temperature: 0.1,
          maxTokens: Math.min(8192, Math.floor((compressorLimit || 65536) * 0.5))
        }
      });

      const duration = Date.now() - startTime;
      const compressedText = (result.text || "").trim();
      const compressedTokens = TokenManager.estimate(compressedText);

      this.logger.info("Compression completed", {
        provider: compressor.provider,
        model: compressor.model,
        originalTokens: promptTokens,
        compressedTokens,
        duration,
        reduction: `${Math.round((1 - compressedTokens / promptTokens) * 100)}%`
      });

      // FIX: validazione strutturale post-compressione
      const validation = this.validateCompression(prompt, compressedText);
      if (!validation.ok) {
        const error = new Error(
          `Compression altered the structure: ${validation.reason}. Aborting compressed prompt.`
        );
        error.status = 413;
        throw error;
      }

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
    candidates.sort((a, b) => {
      if (a.isGemini !== b.isGemini) return a.isGemini ? -1 : 1;
      return b.limit - a.limit;
    });
    for (const candidate of candidates) {
      const key = this.keyPool.getNextKey(candidate.provider);
      if (key) return { ...candidate, key };
    }
    return null;
  }

  buildCompressionPrompt(prompt, targetModel, targetProvider) {
    const targetLimit = TokenManager.getEffectiveLimit(targetProvider, targetModel).limit || 0;

    return `You are a STRUCTURAL LOSSLESS compression engine.
Your ONLY goal is to reduce token count by removing natural-language fluff.
You MUST preserve 100% of structure, format, field names, JSON keys, code, examples, and instructions.

ABSOLUTE RULES — NEVER violate these:
1. NEVER modify, remove, or rename JSON keys, field names, or values.
2. NEVER alter code snippets, HTML tags, table structures, or array syntax.
3. NEVER remove section markers (e.g. ═══, ───, 📥, ⚠️, ###) or change section headers.
4. NEVER change output format requirements (e.g. "Output ESCLUSIVAMENTE in JSON", "nessun testo prima o dopo").
5. NEVER remove placeholders, variables, or template syntax.
6. Compress ONLY: filler words, redundant adjectives, repetitive explanations, decorative language in free-text paragraphs, and obvious verbosity.
7. The compressed text must be ISOMORPHIC to the original: same sections, same order, same structure, same fields.
8. Do NOT add explanations, markdown code blocks, or meta-commentary. Output ONLY the compressed text, starting immediately with the first preserved character.
9. Preserve ALL bullet points, numbered lists, and their exact content.

The compressed text MUST fit within ${targetLimit} tokens when processed by an AI model.

ORIGINAL TEXT:
${prompt}

COMPRESSED TEXT (structurally identical, token-reduced):`;
  }

  /**
   * Validazione post-compressione.
   * Controlla che la struttura non sia stata alterata.
   */
  validateCompression(original, compressed) {
    // Se il testo è vuoto, rifiuta
    if (!compressed || compressed.length < original.length * 0.3) {
      return { ok: false, reason: "compressed text is too short (< 30% of original)" };
    }

    // Estrai marker strutturali: linee che iniziano con sezioni, emoji, marker
    const structuralPattern = /^(?:[═─]+|📥|⚠️|🔍|✍️|📐|🎯|📤|###|───|\{|\[|Output|Rispondi|Struttura)/gim;
    const origMarkers = (original.match(structuralPattern) || []).length;
    const compMarkers = (compressed.match(structuralPattern) || []).length;

    if (origMarkers > 0 && compMarkers < origMarkers * 0.8) {
      return { ok: false, reason: `lost ${origMarkers - compMarkers} structural markers` };
    }

    // Se il prompt originale richiede JSON, il compresso deve ancora contenerlo
    const requiresJson = /JSON|json|\{.*\}/.test(original);
    if (requiresJson && !/JSON|json|\{/.test(compressed)) {
      return { ok: false, reason: "JSON requirement or structure lost" };
    }

    // Se il prompt originale contiene sezioni con ───, il compresso deve averle
    const hasSections = /───/.test(original);
    if (hasSections && !/───/.test(compressed)) {
      return { ok: false, reason: "section dividers (───) lost" };
    }

    return { ok: true };
  }
}

module.exports = Compressor;