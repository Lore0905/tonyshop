require("dotenv").config();

const { PROVIDERS } = require("./constants");
const KeyPool = require("./core/key-pool");
const Logger = require("./core/logger");
const Router = require("./core/router");
const registry = require("./registry");

// Bootstrap: inizializza KeyPool una sola volta
const keyPool = new KeyPool(PROVIDERS);

/**
 * Effettua una chiamata AI gestendo automaticamente provider, modelli e API key
 *
 * @param {Object} options
 * @param {String} options.prompt - Testo del prompt (obbligatorio)
 * @param {String} [options.provider] - Provider da usare (es. "gemini")
 * @param {String} [options.model] - Modello da usare (es. "gemini-2.5-flash")
 * @param {Number} [options.temperature] - Temperatura di generazione
 * @param {Number} [options.maxTokens] - Numero massimo di token
 * @param {Object} [options.logger] - Logger custom (default: console)
 * @returns {Promise<Object>} - Risposta AI: { text, model, provider, raw }
 */
async function freeCallApi(options = {}) {
    if (!options.prompt) {
        throw new Error("prompt is required");
    }

    const logger = new Logger(options.logger);
    const router = new Router(keyPool, registry, logger);

    return await router.route(options);
}

module.exports = {
    freeCallApi
};
