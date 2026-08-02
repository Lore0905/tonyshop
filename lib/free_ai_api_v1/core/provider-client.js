const registry = require("../registry");

/**
 * ProviderClient - layer centrale di comunicazione provider
 *
 * Collega Registry e KeyPool, gestisce errori comuni,
 * predisposto per multi-provider
 */
class ProviderClient {
    constructor(keyPool, logger) {
        this.keyPool = keyPool;
        this.logger = logger;
    }

    /**
     * Esegue una richiesta verso un provider AI
     *
     * @param {Object} options
     * @param {String} options.provider
     * @param {Object} options.payload - contiene key, prompt, model, temperature, maxTokens, abortSignal
     */
    async execute({ provider, payload }) {
        const ProviderClass = registry.getProviderClass(provider);
        if (!ProviderClass) {
            throw new Error(`Provider not found: ${provider}`);
        }

        const adapter = registry.getAdapter(provider);
        if (!adapter) {
            throw new Error(`Adapter not found: ${provider}`);
        }

        const providerInstance = new ProviderClass(adapter, this.keyPool, this.logger);

        const { key, abortSignal, ...restPayload } = payload;

        try {
            const result = await providerInstance.call({ ...restPayload, key, abortSignal });
            // Traccia la richiesta contata dal provider solo su successo
            this.keyPool.trackRequest(key.id);
            return result;
        } catch (error) {
            this.handleError(provider, key, error);
            throw error;
        }
    }

    /**
     * Gestione errori comuni provider
     */
    handleError(providerName, key, error) {
        if (!error) return;

        const status = error.status;

        // Rate limit -> cooldown
        if (status === 429) {
            const providerConfig = this.keyPool.getProviderConfig(providerName);
            const cooldown = providerConfig?.apiKeys?.find(k => k.id === key.id)?.cooldownMs || 60000;
            this.keyPool.setCooldown(key.id, cooldown);
        }

        // Chiave non valida -> invalida permanentemente per questa esecuzione
        if (status === 401 || status === 403) {
            this.keyPool.invalidate(key.id);
        }
    }
}

module.exports = ProviderClient;
