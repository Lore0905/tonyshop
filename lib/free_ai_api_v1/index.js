require("dotenv").config();

const { PROVIDERS, PROMPT_CACHE_CONFIG } = require("./constants");
const KeyPool = require("./core/key-pool");
const Logger = require("./core/logger");
const Router = require("./core/router");
const PromptManager = require("./core/prompt-manager");
const registry = require("./registry");

const keyPool = new KeyPool(PROVIDERS);

/**
 * Effettua una chiamata AI gestendo automaticamente provider, modelli e API key.
 *
 * NUOVO: supporto per instruction + input con cache e ottimizzazione automatica.
 *
 * @param {Object} options
 * @param {String} [options.prompt]       - Prompt completo legacy (obbligatorio se non instruction)
 * @param {String} [options.instruction]  - Istruzione/template da ottimizzare e cache-are
 * @param {*}      [options.input]          - Dati dinamici da appendere all'istruzione
 * @param {String} [options.provider]       - Provider da usare
 * @param {String} [options.model]          - Modello da usare
 * @param {Number} [options.temperature]    - Temperatura
 * @param {Number} [options.maxTokens]      - Max token output
 * @param {Object} [options.logger]         - Logger custom
 * @param {Boolean}[options.compress=true]  - Compressione semantica se troppo lungo
 * @param {Boolean}[options.optimize=true] - Ottimizzazione prompt via AI (default true)
 * @returns {Promise<Object>}
 */
async function freeCallApi(options = {}) {
  if (!options.prompt && !options.instruction) {
    throw new Error("prompt or instruction is required");
  }

  const logger = new Logger(options.logger);

  let finalPrompt = options.prompt;
  let promptMeta = null;

  // --- PROMPT OPTIMIZATION & CACHE ---
  if (options.instruction) {
    const promptManager = new PromptManager(keyPool, registry, logger, {
      enabled: options.optimize !== false,
      cache: PROMPT_CACHE_CONFIG,
      optimizeOnMiss: options.optimize !== false
    });

    const prepared = await promptManager.prepare({
      instruction: options.instruction,
      input: options.input
    });

    finalPrompt = prepared.prompt;
    promptMeta = prepared;

    if (prepared.fromCache) {
      logger.info("Serving prompt from cache", {
        hash: prepared.hash?.substring(0, 8),
        savedTokens: prepared.stats?.saved
      });
    }
  }

  const router = new Router(keyPool, registry, logger);
  const result = await router.route({ ...options, prompt: finalPrompt });

  // Espone metadati di ottimizzazione nel risultato per debug/metriche
  if (promptMeta) {
    result._promptMeta = {
      hash: promptMeta.hash,
      fromCache: promptMeta.fromCache,
      fromOptimizer: promptMeta.fromOptimizer,
      stats: promptMeta.stats
    };
  }

  return result;
}

module.exports = {
  freeCallApi
};