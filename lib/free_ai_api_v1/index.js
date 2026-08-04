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
    const logger = customLogger ? new Logger(customLogger) : new Logger();
    registry.load(logger);
    _keyPool = new KeyPool(PROVIDERS, logger);
    _router = new Router(_keyPool, registry, logger);
}

async function freeCallApi(options) {
    const logger = options.logger ? new Logger(options.logger) : new Logger();
    init(logger);

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

module.exports = { freeCallApi };