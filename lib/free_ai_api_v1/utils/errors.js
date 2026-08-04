/**
 * utils/errors.js (FIXED v2)
 * FIX #5, #9: classificazione precisa con strategia di routing.
 */

class FreeAIAPIError extends Error {
    constructor(message, { status, provider, model, keyId, attempts, type, metadata } = {}) {
        super(message);
        this.name = "FreeAIAPIError";
        this.status = status;
        this.provider = provider;
        this.model = model;
        this.keyId = keyId;
        this.attempts = attempts;
        this.type = type || "UNKNOWN";
        this.metadata = metadata || {};
    }
}

class PromptTooLongError extends FreeAIAPIError {
    constructor(message, meta) {
        super(message, meta);
        this.name = "PromptTooLongError";
    }
}

class RateLimitError extends FreeAIAPIError {
    constructor(message, meta) {
        super(message, meta);
        this.name = "RateLimitError";
    }
}

class QuotaExhaustedError extends FreeAIAPIError {
    constructor(message, meta) {
        super(message, meta);
        this.name = "QuotaExhaustedError";
    }
}

// --- ErrorTypes enum ---
const ErrorTypes = {
    TIMEOUT: "TIMEOUT",
    RATE_LIMIT_RPM: "RATE_LIMIT_RPM",
    RATE_LIMIT_TPM: "RATE_LIMIT_TPM",
    QUOTA_EXHAUSTED: "QUOTA_EXHAUSTED",
    INVALID_KEY: "INVALID_KEY",
    MODEL_NOT_FOUND: "MODEL_NOT_FOUND",
    PROMPT_TOO_LARGE: "PROMPT_TOO_LARGE",
    PAYLOAD_TOO_LARGE: "PAYLOAD_TOO_LARGE",
    NO_CAPACITY: "NO_CAPACITY",
    UNKNOWN: "UNKNOWN"
};

/**
 * Classifica un errore raw del provider.
 * FIX #5: distingue Groq 413 TPM (rate_limit_exceeded) da vero payload too large.
 */
function classifyError(rawError, provider) {
    const status = rawError.status || rawError.statusCode || 0;
    const message = rawError.message || "";
    const code = rawError.code || "";
    const body = rawError.body || {};
    const bodyMsg = body.error?.message || "";
    const bodyCode = body.error?.code || "";

    // --- GROQ SPECIFICO ---
    if (provider === "groq") {
        if (status === 413 && (bodyCode === "rate_limit_exceeded" || message.includes("rate_limit_exceeded"))) {
            if (bodyMsg.includes("tokens per minute") || message.includes("tokens per minute")) {
                return new FreeAIAPIError(
                    "Groq TPM limit exceeded (free tier). Not payload size, but account consumption limit.",
                    { status: 413, provider, type: ErrorTypes.RATE_LIMIT_TPM, metadata: { retryable: false, cooldownMs: 60000, changeProvider: true } }
                );
            }
            return new FreeAIAPIError("Groq rate limit exceeded", { status: 413, provider, type: ErrorTypes.RATE_LIMIT_RPM, metadata: { retryable: true, cooldownMs: 30000 } });
        }
        if (status === 429) {
            return new FreeAIAPIError("Groq RPM limit exceeded", { status: 429, provider, type: ErrorTypes.RATE_LIMIT_RPM, metadata: { retryable: true, cooldownMs: 30000 } });
        }
        if (status === 413 && !bodyMsg.includes("rate_limit") && !message.includes("rate_limit")) {
            return new FreeAIAPIError("Groq payload too large", { status: 413, provider, type: ErrorTypes.PAYLOAD_TOO_LARGE, metadata: { retryable: false, skipModel: true } });
        }
    }

    // --- GEMINI SPECIFICO ---
    if (provider === "gemini") {
        if (status === 429 || bodyMsg.includes("Quota exceeded")) {
            return new FreeAIAPIError("Gemini rate limit or quota exceeded", { status: 429, provider, type: ErrorTypes.RATE_LIMIT_RPM, metadata: { retryable: true, cooldownMs: 60000 } });
        }
        if (status === 413) {
            return new FreeAIAPIError("Gemini request too large", { status: 413, provider, type: ErrorTypes.PAYLOAD_TOO_LARGE, metadata: { retryable: false } });
        }
    }

    // --- GENERICO ---
    if (status === 401 || status === 403) {
        return new FreeAIAPIError("API Key invalid or unauthorized", { status, provider, type: ErrorTypes.INVALID_KEY, metadata: { retryable: false, invalidateKey: true } });
    }
    if (status === 404) {
        return new FreeAIAPIError("Model not found", { status: 404, provider, type: ErrorTypes.MODEL_NOT_FOUND, metadata: { retryable: false, skipModel: true } });
    }
    if (status === 429) {
        return new FreeAIAPIError("Rate limit exceeded", { status: 429, provider, type: ErrorTypes.RATE_LIMIT_RPM, metadata: { retryable: true, cooldownMs: 60000 } });
    }
    if (status === 413) {
        return new FreeAIAPIError("Payload too large", { status: 413, provider, type: ErrorTypes.PAYLOAD_TOO_LARGE, metadata: { retryable: false } });
    }
    if (status === 500) {
        return new FreeAIAPIError("Provider internal error", { status: 500, provider, type: ErrorTypes.UNKNOWN, metadata: { retryable: true, cooldownMs: 5000 } });
    }
    if (rawError.name === "AbortError" || code === "ETIMEDOUT" || code === "ECONNRESET") {
        return new FreeAIAPIError("Request timeout or aborted", { status: 408, provider, type: ErrorTypes.TIMEOUT, metadata: { retryable: true, retrySameKey: true, maxRetries: 1 } });
    }

    return new FreeAIAPIError(message || "Unknown error", { status, provider, type: ErrorTypes.UNKNOWN, metadata: { retryable: false } });
}

module.exports = {
    FreeAIAPIError,
    PromptTooLongError,
    RateLimitError,
    QuotaExhaustedError,
    ErrorTypes,
    classifyError
};