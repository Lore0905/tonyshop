/**
 * providers/gemini.js (FIXED v2)
 * Aggiunge preFlightCheck.
 */
class GeminiProvider {
    constructor(adapter, keyPool, logger) {
        this.adapter = adapter;
        this.keyPool = keyPool;
        this.logger = logger;
        this.name = "gemini";
    }

    async preFlightCheck({ model, tokens, key }) {
        // Gemini ha limiti molto alti, raramente blocca su dimensione
        return { ok: true };
    }

    async call({ prompt, model, key, temperature, maxTokens, abortSignal }) {
        const startTime = Date.now();

        try {
            const result = await this.adapter.call({
                prompt,
                model,
                apiKey: key.value,
                temperature,
                maxTokens,
                abortSignal
            });

            const duration = Date.now() - startTime;
            this.logger.request({
                provider: this.name,
                model,
                apiKey: key.value,
                duration,
                retry: false
            });

            return result;
        } catch (error) {
            const duration = Date.now() - startTime;
            this.logger.failure({ provider: this.name, model, error: error.message });

            error.provider = this.name;
            error.model = model;
            error.keyId = key.id;
            throw error;
        }
    }

    parseError(error) {
        const status = error.status || 0;
        const message = error.message || "";
        if (status === 429) {
            const err = new Error(`Gemini rate limit: ${message}`);
            err.status = 429;
            err.provider = "gemini";
            return err;
        }
        if (status === 400 && message.includes("API key not valid")) {
            const err = new Error(`Gemini invalid key: ${message}`);
            err.status = 401;
            err.provider = "gemini";
            return err;
        }
        return error;
    }
}

module.exports = GeminiProvider;