/**
 * registry.js (FIXED v3)
 * Singleton con metodi getProviderClass e getAdapter compatibili con provider-client.js.
 *
 * FIX: i provider esportano la classe direttamente (module.exports = Classe),
 * non un oggetto { Classe }. Il vecchio codice faceva mod[Object.keys(mod)[0]],
 * ma Object.keys() su una funzione/classe restituisce [] (i metodi stanno sul
 * prototype, non sono own enumerable properties) -> ClassRef risultava undefined
 * -> "Provider not found" per ogni provider.
 */
const fs = require("fs");
const path = require("path");

class Registry {
    constructor() {
        this.providers = {};
        this.adapters = {};
        this._loaded = false;
    }

    load(logger = console) {
        if (this._loaded) return this;

        const adaptersDir = path.join(__dirname, "adapters");
        const providersDir = path.join(__dirname, "providers");

        // Carica adapters (escludi mock.js)
        for (const file of fs.readdirSync(adaptersDir)) {
            if (!file.endsWith(".js") || file === "mock.js") continue;
            const name = path.basename(file, ".js");
            const mod = require(path.join(adaptersDir, file));
            this.adapters[name] = this._resolveExport(mod, name, "adapter", logger);
        }

        // Carica provider classes (non istanziare, solo referenze)
        for (const file of fs.readdirSync(providersDir)) {
            if (!file.endsWith(".js")) continue;
            const name = path.basename(file, ".js");
            const mod = require(path.join(providersDir, file));
            this.providers[name] = this._resolveExport(mod, name, "provider", logger);
        }

        this._loaded = true;
        return this;
    }

    /**
     * Risolve l'export di un modulo indipendentemente dallo stile usato:
     * - module.exports = Classe (funzione diretta)
     * - module.exports = { Classe }
     * - module.exports = { call } (adapter con funzioni nominate)
     */
    _resolveExport(mod, name, kind, logger) {
        if (typeof mod === "function") {
            return mod;
        }
        if (mod && typeof mod === "object") {
            const keys = Object.keys(mod);
            if (keys.length === 0) {
                logger.warn?.(`[Registry] ${kind} "${name}" esporta un oggetto vuoto`);
                return null;
            }
            // Se l'oggetto ha già la forma giusta (es. adapter con { call }), tienilo com'è
            if (kind === "adapter" && typeof mod.call === "function") {
                return mod;
            }
            // Altrimenti prendi il primo export (compatibilità con { Classe })
            return mod[keys[0]];
        }
        logger.warn?.(`[Registry] ${kind} "${name}" ha un export non riconosciuto`);
        return null;
    }

    getProviderClass(name) {
        if (!this._loaded) this.load();
        return this.providers[name] || null;
    }

    getAdapter(name) {
        if (!this._loaded) this.load();
        return this.adapters[name] || null;
    }
}

// Singleton
const instance = new Registry();
module.exports = instance;