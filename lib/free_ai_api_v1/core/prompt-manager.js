/**
 * PromptManager
 * Orchestratore centrale della strategia di cache e ottimizzazione.
 *
 * FIX #3: la cache è solo sull'istruzione normalizzata, non sull'input dinamico.
 */
const crypto = require("crypto");
const { normalizePrompt } = require("../utils/prompt-normalizer");
const PromptCache = require("./prompt-cache");
const TokenManager = require("./token-manager.js");

class PromptManager {
  constructor(keyPool, registry, logger, options = {}) {
    this.cache = new PromptCache(options.cache);
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

    return {
      prompt: this._buildPrompt(instruction, input),
      hash,
      fromCache: false,
      fromOptimizer: false,
      optimized: instruction,
      original: instruction,
      stats: TokenManager.getStats(instruction, instruction)
    };
  }

  getCompressed(instruction, provider, model) {
    const hash = this._generateHash(`${normalizePrompt(instruction)}\n${provider}/${model}`);
    const cached = this.cache.get(hash);
    return cached ? { hash, text: cached.optimized } : { hash, text: null };
  }

  saveCompressed(hash, instruction, compressed) {
    const stats = TokenManager.getStats(instruction, compressed);
    this.cache.set(hash, {
      original: instruction, optimized: compressed,
      tokensBefore: stats.before, tokensAfter: stats.after, usageCount: 1
    });
    return stats;
  }

  buildPrompt(instruction, input) { return this._buildPrompt(instruction, input); }

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
