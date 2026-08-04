const path = require("path");
require("dotenv").config({ path: path.join(__dirname, ".env") });

const DEFAULT_CONFIG = {
    timeoutMs: 30000,
    retryCount: 1
};

const PROMPT_CACHE_CONFIG = {
    enabled: true,
    ttlMs: 7 * 24 * 60 * 60 * 1000,
    maxEntries: 1000,
    persistPath: path.join(__dirname, "cache", "prompt-cache.json"),
    optimizeOnMiss: true,
    geminiModelForOptimization: "gemini-flash-latest"
};

/**
 * MODEL_LIMITS: SOLO limiti intrinseci del modello (context window, output).
 * NON includere limiti provider/account qui.
 */
const MODEL_LIMITS = {
    "gemini-flash-latest": {
        contextWindow: 1048576,
        maxOutputTokens: 8192,
        capabilities: ["multimodal", "compression", "json_mode"]
    },
    "openai/gpt-oss-120b": {
        contextWindow: 131072,
        maxOutputTokens: 4096,
        capabilities: ["compression", "json_mode"]
    },
    "llama3-8b-8192": {
        contextWindow: 8192,
        maxOutputTokens: 4096,
        capabilities: ["json_mode"]
    },
    "mixtral-8x7b-32768": {
        contextWindow: 32768,
        maxOutputTokens: 4096,
        capabilities: ["json_mode"]
    }
};

/**
 * PROVIDER_LIMITS: limiti infrastrutturali (account, tier, TPM, RPM).
 * Separato da MODEL_LIMITS per evitare confusione.
 * tpmIsRateLimit: se false, il TPM funge ANCHE da limite dimensione singola richiesta.
 */
const PROVIDER_LIMITS = {
    gemini: {
        maxRequestTokens: 1048576,
        tpm: 1000000,
        rpm: 60,
        tier: "free",
        supportsCompression: true,
        tpmIsRateLimit: true
    },
    groq: {
        maxRequestTokens: 8000,
        tpm: 8000,
        rpm: 30,
        tier: "on_demand",
        supportsCompression: true,
        tpmIsRateLimit: false  // su free, TPM = limite anche per singola richiesta
    }
};

const PROVIDERS = {
    groq: {
        enabled: true,
        priority: 1,
        endpoint: "https://api.groq.com/openai/v1",
        models: ["openai/gpt-oss-120b"],
        apiKeys: [
            {
                id: "groq_1",
                value: process.env.GROQ_KEY_1 || '',
                rpmLimit: 30,
                dailyLimit: 14400,
                cooldownMs: 30000
            }
        ]
    },
    gemini: {
        enabled: true,
        priority: 2,
        endpoint: "https://generativelanguage.googleapis.com/v1beta",
        models: ["gemini-flash-latest"],
        apiKeys: [
            {
                id: "gemini_key_1",
                value: process.env.GEMINI_KEY_1 || '',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_2",
                value: process.env.GEMINI_KEY_2 || '',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_3",
                value: process.env.GEMINI_KEY_3 || '',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_4",
                value: process.env.GEMINI_KEY_4 || '',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_5",
                value: process.env.GEMINI_KEY_5 || '',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            }
        ]
    }
};

module.exports = {
    DEFAULT_CONFIG,
    PROVIDERS,
    MODEL_LIMITS,
    PROVIDER_LIMITS,
    PROMPT_CACHE_CONFIG
};