require("dotenv").config();
const path = require("path");

const DEFAULT_CONFIG = {
    timeoutMs: 30000,
    retryCount: 1
};

/**
 * Configurazione cache prompt ottimizzati
 */
const PROMPT_CACHE_CONFIG = {
    enabled: true,
    ttlMs: 7 * 24 * 60 * 60 * 1000,      // 7 giorni
    maxEntries: 1000,                     // LRU eviction oltre questo limite
    persistPath: path.join(__dirname, "cache", "prompt-cache.json"),
    optimizeOnMiss: true,                 // se false, nessuna chiamata AI per ottimizzare
    geminiModelForOptimization: "gemini-flash-latest"
};

const MODEL_LIMITS = {
    "gemini-flash-latest": { 
        contextWindow: 1048576,
        maxInputTokens: 1048576
    },
    "openai/gpt-oss-120b": { 
        contextWindow: 131072,
        maxInputTokens: 8000
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
                value: process.env.GROQ_KEY || "",
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
                value: process.env.GEMINI_KEY_1 || "",
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_2",
                value: process.env.GEMINI_KEY_2 || "",
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_3",
                value: process.env.GEMINI_KEY_3 || "",
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_4",
                value: process.env.GEMINI_KEY_4 || "",
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_5",
                value: process.env.GEMINI_KEY_5 || "",
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
    PROMPT_CACHE_CONFIG
};