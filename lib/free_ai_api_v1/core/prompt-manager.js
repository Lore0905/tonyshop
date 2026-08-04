/**
 * PromptManager
 * Orchestratore centrale della strategia di cache e ottimizzazione.
 *
 * Flusso:
 *   1. Normalizza l'istruzione
 *   2. Genera hash SHA256
 *   3. Controlla cache (HIT -> restituisce immediatamente)
 *   4. Cache MISS -> chiama PromptOptimizer (Gemini)
 *   5. Fallback al prompt originale se l'ottimizzazione fallisce
 *   6. Salva in cache e restituisce il prompt finale (optimized + input)
 */
const crypto = require("crypto");
const { normalizePrompt } = require("../utils/prompt-normalizer");
const { estimateTokens } = require("../utils/token-estimator");
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