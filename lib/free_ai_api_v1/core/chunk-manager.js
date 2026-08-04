/**
 * core/chunk-manager.js (NEW)
 * FIX #7: gestione preventiva batch troppo grandi.
 */
const TokenManager = require("./token-manager.js");

class ChunkManager {
    constructor(logger = console) {
        this.logger = logger;
    }

    splitArrayIntoChunks(items, instruction, provider, model, safetyMargin = 500) {
        if (!Array.isArray(items)) throw new Error("ChunkManager: input must be an array");

        const effective = TokenManager.getEffectiveLimit(provider, model);
        if (!effective.limit) {
            this.logger.warn(`[ChunkManager] Unknown limit for ${provider}/${model}, default chunk size 100`);
            return this.splitByCount(items, 100);
        }

        const availableTokens = effective.limit - safetyMargin;
        const instructionTokens = TokenManager.estimate(instruction, "instruction");
        const tokensPerItem = items.length > 0 ? TokenManager.estimate(items[0], "json") : 0;
        const overheadPerChunk = 10;
        const usableTokens = availableTokens - instructionTokens - overheadPerChunk;

        if (usableTokens <= 0) {
            throw new Error(`[ChunkManager] Instruction alone exceeds limit for ${provider}/${model}`);
        }

        if (tokensPerItem > usableTokens) {
            this.logger.warn(`[ChunkManager] Single item exceeds chunk limit, single-item chunks`);
            return items.map(item => [item]);
        }

        const maxItemsPerChunk = Math.floor(usableTokens / Math.max(tokensPerItem, 1));
        const chunkSize = Math.max(1, maxItemsPerChunk);
        this.logger.info(`[ChunkManager] Splitting ${items.length} items into ~${chunkSize} per chunk (limit: ${effective.limit} tokens)`);
        return this.splitByCount(items, chunkSize);
    }

    splitByCount(items, count) {
        const chunks = [];
        for (let i = 0; i < items.length; i += count) chunks.push(items.slice(i, i + count));
        return chunks;
    }

    needsChunking(items, instruction, provider, model) {
        if (!Array.isArray(items) || items.length === 0) return false;
        const totalTokens = TokenManager.estimatePromptTokens(instruction, items, "instruction", "json");
        return !TokenManager.canHandleRequest(provider, model, totalTokens.total).ok;
    }
}

module.exports = ChunkManager;