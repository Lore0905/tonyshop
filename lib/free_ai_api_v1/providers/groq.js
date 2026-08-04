/**
 * providers/groq.js (FIXED v2)
 * FIX #5: preFlightCheck per TPM, parseError per distinguere 413 TPM vs payload.
 */
const { PROVIDER_LIMITS } = require("../constants");

class GroqProvider {
    constructor(adapter, keyPool, logger) {
        this.adapter = adapter;
        this.keyPool = keyPool;
        this.logger = logger;
        this.name = "groq";
    }

    /**
     * Pre-flight check: verifica che la richiesta non superi il TPM.
     * FIX #5: su free tier, TPM funge da limite dimensione.
     */
    async preFlightCheck({ model, tokens, key }) {
        const limits = PROVIDER_LIMITS.groq;
        if (limits.tpmIsRateLimit === false && tokens > limits.tpm) {
            const err = new Error(
                `Groq free tier TPM limit: ${tokens} > ${limits.tpm} tokens. Reduce request size or upgrade tier.`
            );
            err.status = 413;
            err.body = {
                error: {
                    message: `Request too large for model \`${model}\` on tokens per minute (TPM): Limit ${limits.tpm}, Requested ${tokens}`,
                    type: "tokens",
                    code: "rate_limit_exceeded"
                }
            };
            throw err;
        }
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

            // Arricchisci l'errore con metadati
            error.provider = this.name;
            error.model = model;
            error.keyId = key.id;
            throw error;
        }
    }

    /**
     * Parse error specifico per Groq.
     */
    parseError(error) {
        const status = error.status || 0;
        const body = error.body || {};
        const message = body.error?.message || error.message || "";
        const code = body.error?.code || "";

        if (status === 413 && code === "rate_limit_exceeded" && message.includes("tokens per minute")) {
            const err = new Error(`Groq TPM limit: ${message}`);
            err.status = 413;
            err.code = "rate_limit_exceeded";
            err.provider = "groq";
            err.isTpmLimit = true;
            return err;
        }
        if (status === 429) {
            const err = new Error(`Groq RPM limit: ${message}`);
            err.status = 429;
            err.provider = "groq";
            return err;
        }
        if (status === 413) {
            const err = new Error(`Groq payload too large: ${message}`);
            err.status = 413;
            err.provider = "groq";
            err.isPayloadTooLarge = true;
            return err;
        }
        return error;
    }
}

module.exports = GroqProvider;