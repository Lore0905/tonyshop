/**
 * free_ai_api - index.js
 * Punto di ingresso pubblico.
 */
const { PROVIDERS, DEFAULT_CONFIG } = require("./constants");
const registry = require("./registry");
const { KeyPool } = require("./core/key-pool");
const Router = require("./core/router");
const Logger = require("./core/logger");

let _keyPool = null;
let _router = null;

function init(customLogger) {
    if (_router) return;
    // FIX: evita doppio wrapping se customLogger è già un'istanza Logger
    const logger = (customLogger && customLogger instanceof Logger)
        ? customLogger
        : new Logger(customLogger);
    registry.load(logger);
    _keyPool = new KeyPool(PROVIDERS, logger);
    _router = new Router(_keyPool, registry, logger);
}

async function freeCallApi(options = {}) {
    init(options.logger);

    if (!options.prompt && !options.instruction) {
        throw new TypeError("freeCallApi richiede 'prompt' oppure 'instruction'");
    }
    if (options.prompt && options.instruction) {
        throw new TypeError("Usa 'prompt' oppure 'instruction' + 'input', non entrambi");
    }

    _router.logger.start();

    return _router.route({
        prompt: options.prompt,
        instruction: options.instruction,
        input: options.input,
        provider: options.provider,
        model: options.model,
        temperature: options.temperature,
        maxTokens: options.maxTokens,
        compress: options.compress !== false // default true
    });
}

// Solo per test: consente di ricreare i singleton dopo il mock delle API.
function _resetForTests() {
    _keyPool = null;
    _router = null;
}

module.exports = { freeCallApi, _resetForTests };
