/**
 * core/key-pool.js (FIXED v2)
 * FIX: aggiunti metodi getNextKey, trackRequest, setCooldown compatibili
 * con provider-client.js e router.js esistenti.
 */
class KeyPool {
    constructor(providersConfig, logger = console) {
        this.config = providersConfig;
        this.logger = logger;
        this.state = new Map(); // provider -> keyId -> { rpmCount[], dailyCount, cooldownUntil, invalid }
        this.roundRobin = new Map(); // provider -> index
        this.keyIdToProvider = new Map(); // keyId -> provider

        // Build reverse map
        for (const [provider, cfg] of Object.entries(providersConfig)) {
            for (const key of cfg.apiKeys || []) {
                this.keyIdToProvider.set(key.id, provider);
            }
        }
    }

    _ensureState(provider, keyId) {
        if (!this.state.has(provider)) this.state.set(provider, new Map());
        const p = this.state.get(provider);
        if (!p.has(keyId)) {
            p.set(keyId, { rpmCount: [], dailyCount: 0, cooldownUntil: 0, invalid: false });
        }
        return p.get(keyId);
    }

    getAvailableKeys(provider) {
        const cfg = this.config[provider];
        if (!cfg) return [];
        const now = Date.now();
        const oneMinuteAgo = now - 60000;
        const oneDayAgo = now - 86400000;

        return cfg.apiKeys
            .map(k => {
                const s = this._ensureState(provider, k.id);
                s.rpmCount = s.rpmCount.filter(t => t > oneMinuteAgo);
                if (s.dailyReset && s.dailyReset < oneDayAgo) {
                    s.dailyCount = 0;
                    s.dailyReset = now;
                }
                return { ...k, state: s };
            })
            .filter(k => {
                if (k.state.invalid) return false;
                if (k.state.cooldownUntil > now) return false;
                if (k.state.rpmCount.length >= k.rpmLimit) return false;
                if (k.state.dailyCount >= k.dailyLimit) return false;
                return true;
            });
    }

    /**
     * Restituisce la prossima key disponibile in round-robin.
     * Compatibile con router.js e provider-client.js.
     */
    getNextKey(provider) {
        const available = this.getAvailableKeys(provider);
        if (available.length === 0) return null;

        let idx = this.roundRobin.get(provider) || 0;
        const key = available[idx % available.length];
        this.roundRobin.set(provider, (idx + 1) % available.length);
        return key;
    }

    markUsed(provider, keyId) {
        const s = this._ensureState(provider, keyId);
        s.rpmCount.push(Date.now());
        s.dailyCount++;
        if (!s.dailyReset) s.dailyReset = Date.now();
    }

    /**
     * Alias compatibile con provider-client.js.
     */
    trackRequest(keyId) {
        const provider = this.keyIdToProvider.get(keyId);
        if (provider) this.markUsed(provider, keyId);
    }

    cooldown(provider, keyId, ms) {
        const s = this._ensureState(provider, keyId);
        s.cooldownUntil = Date.now() + ms;
        this.logger.warn(`[KeyPool] ${provider}/${keyId} cooldown for ${ms}ms`);
    }

    /**
     * Alias compatibile con provider-client.js.
     */
    setCooldown(keyId, ms) {
        const provider = this.keyIdToProvider.get(keyId);
        if (provider) this.cooldown(provider, keyId, ms);
    }

    invalidate(provider, keyId) {
        const s = this._ensureState(provider, keyId);
        s.invalid = true;
        this.logger.error(`[KeyPool] ${provider}/${keyId} invalidated`);
    }
}

module.exports = KeyPool;
module.exports.KeyPool = KeyPool;