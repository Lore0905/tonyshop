const fs = require("fs");
const path = require("path");

/**
 * Registry singleton - carica automaticamente adapters e providers
 */
class Registry {
    constructor() {
        this.adapters = new Map();
        this.providers = new Map();
        this.loadAdapters();
        this.loadProviders();
    }

    loadAdapters() {
        const dir = path.join(__dirname, "adapters");
        if (!fs.existsSync(dir)) return;
        const files = fs.readdirSync(dir).filter(f => f.endsWith(".js"));
        for (const file of files) {
            const name = path.basename(file, ".js");
            const adapter = require(path.join(dir, file));
            this.adapters.set(name, adapter);
        }
    }

    loadProviders() {
        const dir = path.join(__dirname, "providers");
        if (!fs.existsSync(dir)) return;
        const files = fs.readdirSync(dir).filter(f => f.endsWith(".js"));
        for (const file of files) {
            const name = path.basename(file, ".js");
            const ProviderClass = require(path.join(dir, file));
            this.providers.set(name, ProviderClass);
        }
    }

    getAdapter(name) {
        return this.adapters.get(name);
    }

    getProviderClass(name) {
        return this.providers.get(name);
    }

    getProviderInstance(name, ...args) {
        const ProviderClass = this.providers.get(name);
        if (!ProviderClass) return null;
        return new ProviderClass(...args);
    }

    listProviders() {
        return Array.from(this.providers.keys());
    }

    listAdapters() {
        return Array.from(this.adapters.keys());
    }
}

module.exports = new Registry();
