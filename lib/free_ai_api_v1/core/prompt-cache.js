/**
 * PromptCache (FIXED v3)
 * FIX: _persist in set() wrappato in try/catch per non far fallire
 *      l'intera chiamata AI in caso di errore disco/permessi.
 */
const fs = require("fs");
const path = require("path");

class PromptCache {
  constructor(options = {}) {
    this.cache = new Map();
    this.ttlMs = options.ttlMs || 7 * 24 * 60 * 60 * 1000;
    this.maxEntries = options.maxEntries || 1000;
    this.persistPath = options.persistPath || null;
    this.stats = { hits: 0, misses: 0 };

    if (this.persistPath) {
      this._ensureDir();
      this.load();
    }
  }

  _ensureDir() {
    const dir = path.dirname(this.persistPath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
  }

  get(hash) {
    this._cleanup();
    const entry = this.cache.get(hash);
    if (!entry) {
      this.stats.misses++;
      return null;
    }
    const age = Date.now() - new Date(entry.createdAt).getTime();
    if (age > this.ttlMs) {
      this.cache.delete(hash);
      this.stats.misses++;
      return null;
    }
    entry.lastUsed = new Date().toISOString();
    entry.usageCount = (entry.usageCount || 0) + 1;
    this.stats.hits++;
    return entry;
  }

  set(hash, data) {
    if (this.cache.size >= this.maxEntries && !this.cache.has(hash)) {
      let oldestHash = null;
      let oldestTime = Infinity;
      for (const [h, e] of this.cache) {
        const t = new Date(e.lastUsed || e.createdAt).getTime();
        if (t < oldestTime) {
          oldestTime = t;
          oldestHash = h;
        }
      }
      if (oldestHash) this.cache.delete(oldestHash);
    }

    const existing = this.cache.get(hash);
    const savedTokens = (existing?.savedTokens || 0) + (data.tokensBefore - data.tokensAfter);

    this.cache.set(hash, {
      ...data,
      savedTokens: Math.max(0, savedTokens),
      createdAt: existing?.createdAt || new Date().toISOString(),
      lastUsed: new Date().toISOString(),
    });

    // FIX: non far crashare l'intera chiamata se il disco ha problemi
    try {
      this._persist();
    } catch (e) {
      // Silenzioso: la cache in-memory è sufficiente
    }

    return this;
  }

  has(hash) {
    return this.get(hash) !== null;
  }

  getStats() {
    const entries = Array.from(this.cache.values());
    const totalSaved = entries.reduce((s, e) => s + (e.savedTokens || 0), 0);
    return {
      ...this.stats,
      totalEntries: this.cache.size,
      totalSavedTokens: totalSaved,
      topPrompts: entries
        .sort((a, b) => (b.usageCount || 0) - (a.usageCount || 0))
        .slice(0, 10)
        .map((e) => ({
          original:
            e.original.substring(0, 60) + (e.original.length > 60 ? "..." : ""),
          usageCount: e.usageCount,
          savedTokens: e.savedTokens,
          reduction:
            e.tokensBefore > 0
              ? Math.round(((e.tokensBefore - e.tokensAfter) / e.tokensBefore) * 100)
              : 0,
        })),
    };
  }

  clear() {
    this.cache.clear();
    this.stats = { hits: 0, misses: 0 };
    this._persist();
  }

  load() {
    try {
      if (!fs.existsSync(this.persistPath)) return;
      const raw = fs.readFileSync(this.persistPath, "utf8");
      const data = JSON.parse(raw);
      if (data.entries) {
        for (const [hash, entry] of Object.entries(data.entries)) {
          this.cache.set(hash, entry);
        }
      }
      if (data.stats) this.stats = data.stats;
    } catch (e) {
      // Cache corrotta: ignora
    }
  }

  _persist() {
    if (!this.persistPath) return;
    const data = {
      entries: Object.fromEntries(this.cache),
      stats: this.stats,
      lastPersisted: new Date().toISOString(),
    };
    fs.writeFileSync(this.persistPath, JSON.stringify(data, null, 2));
  }

  _cleanup() {
    const now = Date.now();
    for (const [hash, entry] of this.cache) {
      if (now - new Date(entry.createdAt).getTime() > this.ttlMs) {
        this.cache.delete(hash);
      }
    }
  }
}

module.exports = PromptCache;