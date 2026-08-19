/**
 * core/token-manager.js (FIXED v3)
 * FIX: getEffectiveLimit non sottrae maxOutputTokens dai limiti provider.
 *      Ora sottrae SOLO da contextWindow. I limiti provider sono hard limit.
 */
const { estimateTokens } = require("../utils/token-estimator");
const { MODEL_LIMITS, PROVIDER_LIMITS, PROVIDERS } = require("../constants");

class TokenManager {
    static estimate(text, type = "text") {
        return estimateTokens(text, type);
    }

    static estimatePromptTokens(instruction, input, instructionType = "instruction", inputType = "json") {
        const instructionTokens = estimateTokens(instruction, instructionType);
        const inputTokens = estimateTokens(input, inputType);
        const overhead = 4;
        return {
            instruction: instructionTokens,
            input: inputTokens,
            overhead,
            total: instructionTokens + inputTokens + overhead
        };
    }

    static checkLimit(text, modelName, providerName = null) {
        const tokens = this.estimate(text);
        let limit;
        if (providerName) {
            const eff = this.getEffectiveLimit(providerName, modelName);
            limit = eff.limit !== null ? eff.limit : Infinity;
        } else {
            for (const [pName, pCfg] of Object.entries(PROVIDERS)) {
                if (pCfg.models?.includes(modelName)) {
                    const eff = this.getEffectiveLimit(pName, modelName);
                    limit = eff.limit !== null ? eff.limit : Infinity;
                    break;
                }
            }
            if (limit === undefined) {
                limit = MODEL_LIMITS[modelName]?.contextWindow || MODEL_LIMITS[modelName]?.maxOutputTokens || Infinity;
            }
        }
        return { tokens, limit, exceeds: tokens > limit, remaining: Math.max(0, limit - tokens) };
    }

    static getStats(original, optimized) {
        const before = this.estimate(original);
        const after = this.estimate(optimized);
        return {
            before,
            after,
            saved: Math.max(0, before - after),
            reductionPercent: before > 0 ? Math.round(((before - after) / before) * 100) : 0
        };
    }

    static getEffectiveLimit(provider, model) {
        const modelLimit = MODEL_LIMITS[model] || {};
        const providerLimit = PROVIDER_LIMITS[provider] || {};

        const contextWindow = modelLimit.contextWindow || Infinity;
        const maxOutputTokens = modelLimit.maxOutputTokens || 4096;
        const providerMaxRequest = providerLimit.maxRequestTokens || Infinity;
        const providerTpm = providerLimit.tpm || Infinity;
        const tpmIsRateLimit = providerLimit.tpmIsRateLimit ?? true;

        const contextInputLimit = contextWindow - maxOutputTokens;
        let minLimit = Math.min(contextInputLimit, providerMaxRequest);
        if (!tpmIsRateLimit) {
            minLimit = Math.min(minLimit, providerTpm);
        }

        if (!Number.isFinite(minLimit)) {
            return { limit: null, bottleneck: "unknown", factors: { contextWindow, maxOutputTokens, providerMaxRequest, providerTpm, tpmIsRateLimit } };
        }

        const limit = Math.max(0, minLimit);
        let bottleneck = "contextWindow";
        if (limit === Math.max(0, providerMaxRequest)) bottleneck = "providerMaxRequest";
        else if (!tpmIsRateLimit && limit === Math.max(0, providerTpm)) bottleneck = "providerTPM";

        return { limit, rawLimit: minLimit + maxOutputTokens, bottleneck, factors: { contextWindow, maxOutputTokens, providerMaxRequest, providerTpm, tpmIsRateLimit } };
    }

    static canHandleRequest(provider, model, totalTokens) {
        const effective = this.getEffectiveLimit(provider, model);
        if (effective.limit === null) return { ok: true, limit: null, bottleneck: "unknown" };
        if (totalTokens <= effective.limit) return { ok: true, limit: effective.limit, bottleneck: effective.bottleneck };
        return {
            ok: false,
            limit: effective.limit,
            bottleneck: effective.bottleneck,
            reason: `Request requires ${totalTokens} tokens, but ${provider}/${model} allows max ${effective.limit} (bottleneck: ${effective.bottleneck})`
        };
    }
}

module.exports = TokenManager;