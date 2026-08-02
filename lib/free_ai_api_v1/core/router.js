const { DEFAULT_CONFIG, PROVIDERS } = require("../constants");
const ProviderClient = require("./provider-client");

/**
 * Router - orchestrazione provider, modello, key e fallback automatico
 *
 * Casi gestiti:
 * 1. Provider + modello specificati -> prova solo quella combinazione con tutte le key
 * 2. Solo provider -> prova tutti i modelli del provider con tutte le key
 * 3. Nessuno specificato -> prova provider per priority, modelli per ordine, key round robin
 */
class Router {
    constructor(keyPool, registry, logger) {
        this.keyPool = keyPool;
        this.registry = registry;
        this.logger = logger;
        this.providerClient = new ProviderClient(keyPool, logger);
        this.attempts = [];
    }

    async route(options) {
        const { prompt, provider, model, temperature, maxTokens } = options;

        if (!prompt) {
            throw new Error("prompt is required");
        }

        this.attempts = [];

        const providerList = this.resolveProviders(provider);

        for (const provName of providerList) {
            const provConfig = PROVIDERS[provName];
            if (!provConfig || !provConfig.enabled) continue;

            const modelList = this.resolveModels(provConfig, model);

            for (const modelName of modelList) {
                let key;
                while ((key = this.keyPool.getNextKey(provName)) !== null) {
                    const attempt = {
                        provider: provName,
                        model: modelName,
                        keyId: key.id,
                        error: null
                    };

                    try {
                        const result = await this.executeWithRetry({
                            provider: provName,
                            model: modelName,
                            key,
                            prompt,
                            temperature,
                            maxTokens
                        });

                        return result;
                    } catch (error) {
                        attempt.error = error;
                        this.attempts.push(attempt);

                        const status = error.status;

                        // Prompt troppo lungo -> errore terminale immediato
                        if (status === 413) {
                            throw this.buildFinalError("Prompt too long (413)");
                        }

                        // Errore server o modello inesistente -> cambia modello
                        if (status === 500 || status === 404) {
                            this.logger.fallback({
                                provider: provName,
                                model: modelName,
                                reason: `HTTP ${status}`
                            });
                            break; // esci dal while key, passa al prossimo modello
                        }

                        // Rate limit (429), key invalida (401/403), timeout, rete
                        // -> continua con prossima key (il while continua)
                        // Il ProviderClient ha già applicato cooldown/invalidazione
                    }
                }
            }
        }

        throw this.buildFinalError("All providers, models and keys exhausted");
    }

    resolveProviders(specifiedProvider) {
        if (specifiedProvider) {
            return [specifiedProvider];
        }
        return Object.entries(PROVIDERS)
            .filter(([, config]) => config.enabled)
            .sort(([, a], [, b]) => a.priority - b.priority)
            .map(([name]) => name);
    }

    resolveModels(providerConfig, specifiedModel) {
        if (specifiedModel) {
            return [specifiedModel];
        }
        return providerConfig.models || [];
    }

    async executeWithRetry({ provider, model, key, prompt, temperature, maxTokens }) {
        const payload = { prompt, model, temperature, maxTokens };

        const tryCall = async () => {
            const abortController = new AbortController();
            const timeoutId = setTimeout(() => abortController.abort(), DEFAULT_CONFIG.timeoutMs);

            try {
                return await this.providerClient.execute({
                    provider,
                    payload: { ...payload, key, abortSignal: abortController.signal }
                });
            } finally {
                clearTimeout(timeoutId);
            }
        };

        try {
            return await tryCall();
        } catch (error) {
            if (this.isTimeoutError(error)) {
                this.logger.warn("Timeout detected, retrying once with same key", {
                    provider,
                    model,
                    keyId: key.id
                });
                return await tryCall();
            }
            throw error;
        }
    }

    isTimeoutError(error) {
        if (!error) return false;
        if (error.name === "AbortError") return true;
        if (error.status === 408) return true;
        if (error.message && (
            error.message.toLowerCase().includes("timeout") ||
            error.message.toLowerCase().includes("abort") ||
            error.message.toLowerCase().includes("etimeout")
        )) return true;
        return false;
    }

    buildFinalError(message) {
        const details = this.attempts.map(a =>
            `${a.provider}/${a.model} [${a.keyId}]: ${a.error?.message || "Unknown"}`
        ).join("; ");

        const error = new Error(`${message}. Attempts: ${details}`);
        error.attempts = this.attempts;
        return error;
    }
}

module.exports = Router;
