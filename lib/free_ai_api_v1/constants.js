require("dotenv").config();

const DEFAULT_CONFIG = {
    timeoutMs: 30000,
    retryCount: 1
};

const PROVIDERS = {
    gemini: {
        enabled: true,
        priority: 1,
        endpoint: "https://generativelanguage.googleapis.com/v1beta",
        models: [
            "gemini-flash-latest"
        ],
        apiKeys: [
            {
                id: "gemini_key_1",
                value: process.env.GEMINI_KEY_1 || 'AQ.Ab8RN6IITcwZl5EoNxVxCTduS-RaRiQR2owioe6x6OgzVaAmkg',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_2",
                value: process.env.GEMINI_KEY_2 || 'AQ.Ab8RN6KkXQKPox1i0WGRK_tLsThhz4WAPmpBRLRxGgZGI1L41w',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_3",
                value: process.env.GEMINI_KEY_3 || 'AQ.Ab8RN6LZcN2qzGJtSRl0V1DfKA0PZQIM1CfRICGanILcNloBRA',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            },
            {
                id: "gemini_key_4",
                value: process.env.GEMINI_KEY_4 || 'AQ.Ab8RN6K_iECj-ahsGbuseWDl9sBZhLL9fpHOzWVxkvt3GhE-2g',
                rpmLimit: 24,
                dailyLimit: 1500,
                cooldownMs: 60000
            }
        ]
    }
};

module.exports = {
    DEFAULT_CONFIG,
    PROVIDERS
};
