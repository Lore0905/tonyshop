/**
 * core/provider-client.js (FIXED v2)
 * FIX #5, #9: usa classifyError per strategia diversa per tipo di errore.
 */
const registry = require("../registry");
const { classifyError, ErrorTypes } = require("../utils/errors");

class ProviderClient {
    constructor(keyPool, logger) {
        this.keyPool = keyPool;
        this.logger = logger;
    }

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
            // Traccia la richiesta solo su successo
            this.keyPool.trackRequest(key.id);
            return result;
        } catch (error) {
            this.handleError(provider, key, error);
            throw error;
        }
    }

    /**
     * Gestione errori con classifyError.
     * FIX #9: strategia diversa per ogni tipo.
     */
    handleError(providerName, key, error) {
        if (!error) return;

        const classified = classifyError(error, providerName);
        const status = classified.status || error.status;

        switch (classified.type) {
            case ErrorTypes.RATE_LIMIT_RPM:
            case ErrorTypes.RATE_LIMIT_TPM:
            case ErrorTypes.QUOTA_EXHAUSTED: {
                const providerConfig = this.keyPool.config?.[providerName];
                const cooldown = providerConfig?.apiKeys?.find(k => k.id === key.id)?.cooldownMs || 60000;
                this.keyPool.setCooldown(key.id, cooldown);
                break;
            }
            case ErrorTypes.INVALID_KEY:
                this.keyPool.invalidate(providerName, key.id);
                break;
            default:
                // Nessuna azione su errori generici
                break;
        }
    }
}

module.exports = ProviderClient;