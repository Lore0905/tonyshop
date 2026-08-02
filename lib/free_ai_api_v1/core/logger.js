/**
 * Logger centralizzato della libreria
 *
 * Supporta:
 * - logger personalizzato tramite injection
 * - default console
 * - masking API key
 * - formato uniforme
 */
class Logger {
    constructor(customLogger = console) {
        this.logger = customLogger;
    }

    /**
     * Nasconde una API Key nei log
     *
     * esempio:
     * gemini_key_123456
     *
     * diventa:
     * gemi...3456
     */
    maskKey(apiKey) {
        if (!apiKey) {
            return "unknown";
        }
        if (apiKey.length <= 8) {
            return "****";
        }
        return `${apiKey.substring(0, 4)}...${apiKey.substring(apiKey.length - 4)}`;
    }

    info(message, data = {}) {
        this.logger.info("[INFO]", message, data);
    }

    warn(message, data = {}) {
        this.logger.warn("[WARN]", message, data);
    }

    error(message, data = {}) {
        this.logger.error("[ERROR]", message, data);
    }

    request({ provider, model, apiKey, duration, retry = false }) {
        this.info("API request", {
            provider,
            model,
            apiKey: this.maskKey(apiKey),
            duration,
            retry
        });
    }

    fallback({ provider, model, reason }) {
        this.warn("Fallback triggered", {
            provider,
            model,
            reason
        });
    }

    failure({ provider, model, error }) {
        this.error("Request failed", {
            provider,
            model,
            error
        });
    }
}

module.exports = Logger;
