/**
 * KeyPool - Gestione circolare API Key con cooldown, rate limit e tracciamento
 */
class KeyPool {
    constructor(providersConfig) {
        this.config = providersConfig;
        this.state = new Map();
        this.roundRobinIndex = new Map();

        for (const [providerName, providerConfig] of Object.entries(providersConfig)) {
            if (!providerConfig.enabled) continue;
            this.roundRobinIndex.set(providerName, 0);
            for (const key of providerConfig.apiKeys || []) {
                this.state.set(key.id, {
                    provider: providerName,
                    rpmRequests: [],
                    dailyRequests: [],
                    cooldownUntil: 0,
                    invalid: false
                });
            }
        }
    }

    /**
     * Restituisce la prossima API Key disponibile per il provider (Round Robin)
     * Non traccia ancora la richiesta (trackRequest va chiamato separatamente)
     */
    getNextKey(providerName) {
        const providerConfig = this.config[providerName];
        if (!providerConfig || !providerConfig.enabled) return null;

        const keys = providerConfig.apiKeys || [];
        if (keys.length === 0) return null;

        const startIndex = this.roundRobinIndex.get(providerName) || 0;

        for (let i = 0; i < keys.length; i++) {
            const idx = (startIndex + i) % keys.length;
            const key = keys[idx];

            if (this.isKeyAvailable(key.id)) {
                this.roundRobinIndex.set(providerName, (idx + 1) % keys.length);
                return key;
            }
        }

        return null;
    }

    /**
     * Verifica se una key è utilizzabile (non cooldown, non invalida, non limiti)
     */
    isKeyAvailable(keyId) {
        const state = this.state.get(keyId);
        if (!state) return false;
        if (state.invalid) return false;
        if (Date.now() < state.cooldownUntil) return false;

        const keyConfig = this.findKeyConfig(keyId);
        if (!keyConfig) return false;

        if (this.isRpmExceeded(keyConfig)) return false;
        if (this.isDailyExceeded(keyConfig)) return false;

        return true;
    }

    findKeyConfig(keyId) {
        for (const [, config] of Object.entries(this.config)) {
            const key = config.apiKeys?.find(k => k.id === keyId);
            if (key) return key;
        }
        return null;
    }

    isRpmExceeded(key) {
        const state = this.state.get(key.id);
        const now = Date.now();
        const oneMinuteAgo = now - 60000;
        state.rpmRequests = state.rpmRequests.filter(t => t > oneMinuteAgo);
        return state.rpmRequests.length >= key.rpmLimit;
    }

    isDailyExceeded(key) {
        const state = this.state.get(key.id);
        const now = Date.now();
        const oneDayAgo = now - 86400000;
        state.dailyRequests = state.dailyRequests.filter(t => t > oneDayAgo);
        return state.dailyRequests.length >= key.dailyLimit;
    }

    trackRequest(keyId) {
        const state = this.state.get(keyId);
        if (!state) return;
        const now = Date.now();
        state.rpmRequests.push(now);
        state.dailyRequests.push(now);
    }

    setCooldown(keyId, durationMs) {
        const state = this.state.get(keyId);
        if (state) {
            state.cooldownUntil = Date.now() + durationMs;
        }
    }

    invalidate(keyId) {
        const state = this.state.get(keyId);
        if (state) {
            state.invalid = true;
        }
    }

    getProviderConfig(providerName) {
        return this.config[providerName];
    }
}

module.exports = KeyPool;
