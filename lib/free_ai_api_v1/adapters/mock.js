/**
 * adapters/mock.js
 * Adapter mock per test.
 */
class MockAdapter {
    constructor(scenario = "success") {
        this.scenario = scenario;
        this.callCount = 0;
    }

    async call({ prompt, model, apiKey, temperature, maxTokens, abortSignal }) {
        this.callCount++;
        await new Promise(r => setTimeout(r, 10));

        if (this.scenario === "success") {
            return {
                text: `Risposta mock per: ${prompt?.substring(0, 50) || ""}...`,
                usage: { prompt_tokens: 100, completion_tokens: 50 }
            };
        }

        if (this.scenario === "groq_tpm") {
            const err = new Error("Request too large for model `openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, Requested 70509");
            err.status = 413;
            err.body = {
                error: {
                    message: "Request too large for model `openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, Requested 70509",
                    type: "tokens",
                    code: "rate_limit_exceeded"
                }
            };
            throw err;
        }

        if (this.scenario === "groq_429") {
            const err = new Error("Rate limit exceeded");
            err.status = 429;
            throw err;
        }

        if (this.scenario === "timeout") {
            const err = new Error("Request timeout");
            err.name = "AbortError";
            throw err;
        }

        if (this.scenario === "invalid_key") {
            const err = new Error("Invalid API Key");
            err.status = 401;
            throw err;
        }

        throw new Error("Unknown mock error");
    }
}

module.exports = { MockAdapter };