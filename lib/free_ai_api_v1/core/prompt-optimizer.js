/**
 * PromptOptimizer
 * Ottimizza un prompt template chiamando Gemini direttamente.
 */
const ProviderClient = require("./provider-client");

class PromptOptimizer {
  constructor(keyPool, logger) {
    this.providerClient = new ProviderClient(keyPool, logger);
    this.logger = logger;
  }

  async optimize(instruction) {
    const key = this.providerClient.keyPool.getNextKey("gemini");
    if (!key) {
      throw new Error("No Gemini key available for prompt optimization");
    }

    const optimizationPrompt = `You are a prompt optimization engine. Your task is to rewrite the following user instruction to be maximally concise and clear for an AI model, removing all unnecessary words while preserving every requirement, constraint, and technical detail.

Rules:
1. Remove filler words, redundancies, and polite phrases.
2. Keep all technical terms, variable names, field names, and logic intact.
3. Use imperative, direct language.
4. Do NOT add explanations, markdown formatting, or meta-commentary.
5. Output ONLY the optimized instruction text.

Original instruction:
${instruction}

Optimized instruction:`;

    const result = await this.providerClient.execute({
      provider: "gemini",
      payload: {
        prompt: optimizationPrompt,
        model: "gemini-flash-latest",
        key,
        temperature: 0.1,
        maxTokens: 1024
      }
    });

    const optimized = (result.text || "").trim();
    if (!optimized) {
      throw new Error("Optimizer returned empty text");
    }
    return optimized;
  }
}

module.exports = PromptOptimizer;