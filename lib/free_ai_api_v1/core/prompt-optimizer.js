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

    const optimizationPrompt = `You are a prompt optimization engine.

Your task is to reduce the token count of the instruction WITHOUT
changing its behavioral contract.

ABSOLUTE RULES:

1. Never remove an output field.
2. Never rename an output field.
3. Never change the output data type.
4. Never change array cardinality requirements.
5. Never remove examples that define output structure.
6. Never change numerical limits.
7. Never remove validation rules.
8. Never remove "must", "exactly", "only", "never" constraints.
9. Never modify JSON structure.
10. Never modify HTML requirements.
11. Never modify priority rules.
12. Never modify data integrity rules.

The following sections are IMMUTABLE:

OUTPUT SCHEMA
FIELD NAMES
CARDINALITY
VALIDATION RULES
NUMERICAL LIMITS
DATA INTEGRITY RULES

Only remove:
- rhetorical language
- duplicated explanations
- redundant examples
- stylistic prose

Return ONLY the optimized instruction.

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