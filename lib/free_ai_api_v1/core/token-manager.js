/**
 * TokenManager
 * Incapsula estimateTokens e fornisce utilità per il confronto
 * tra prompt originali e ottimizzati.
 */
const { estimateTokens } = require("../utils/token-estimator");
const { MODEL_LIMITS } = require("../constants");

class TokenManager {
  static estimate(text) {
    return estimateTokens(text);
  }

  static checkLimit(text, modelName) {
    const tokens = this.estimate(text);
    const limit = MODEL_LIMITS[modelName]?.maxInputTokens || Infinity;
    return {
      tokens,
      limit,
      exceeds: tokens > limit,
      remaining: Math.max(0, limit - tokens)
    };
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
}

module.exports = TokenManager;