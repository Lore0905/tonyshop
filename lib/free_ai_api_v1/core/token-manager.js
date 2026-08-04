/**
 * core/token-manager.js (FIXED v2)
 * FIX #2, #10, #12: stima instruction+input, getEffectiveLimit, canHandleRequest.
 */
const { estimateTokens } = require("../utils/token-estimator");
const { MODEL_LIMITS, PROVIDER_LIMITS, PROVIDERS } = require("../constants");

class TokenManager {
    static estimate(text, type = "text") {
        return estimateTokens(text, type);
    }

    /**
     * Stima token di un prompt completo separando instruction e input.
     * FIX #2: controlla sempre instruction + input, non solo prompt finale.
     */
    static estimatePromptTokens(instruction, input, instructionType = "instruction", inputType = "json") {
        const instructionTokens = estimateTokens(instruction, instructionType);
        const inputTokens = estimateTokens(input, inputType);
        const overhead = 4; // separatori
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
        return {
            tokens,
            limit,
            exceeds: tokens > limit,
            remaining: Math.max(0, limit - tokens)
        };
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

    /**
     * Calcola il limite effettivo per provider+modello.
     * FIX #1, #4: min(contextWindow, providerMaxRequest, providerTPM se tpmIsRateLimit=false).
     */
    static getEffectiveLimit(provider, model) {
        const modelLimit = MODEL_LIMITS[model] || {};
        const providerLimit = PROVIDER_LIMITS[provider] || {};

        const factors = {
            contextWindow: modelLimit.contextWindow || Infinity,
            maxOutputTokens: modelLimit.maxOutputTokens || Infinity,
            providerMaxRequest: providerLimit.maxRequestTokens || Infinity,
            providerTpm: providerLimit.tpm || Infinity,
            providerTpmIsRateLimit: providerLimit.tpmIsRateLimit ?? true
        };

        let candidates = [
            { value: factors.contextWindow, name: "contextWindow" },
            { value: factors.providerMaxRequest, name: "providerMaxRequest" }
        ];
        if (!factors.providerTpmIsRateLimit) {
            candidates.push({ value: factors.providerTpm, name: "providerTPM" });
        }

        const finite = candidates.filter(c => Number.isFinite(c.value));
        if (finite.length === 0) return { limit: null, bottleneck: "unknown", factors };

        const min = finite.reduce((a, b) => a.value < b.value ? a : b);
        const usableLimit = min.value - (modelLimit.maxOutputTokens || 4096);
        return {
            limit: Math.max(0, usableLimit),
            rawLimit: min.value,
            bottleneck: min.name,
            factors
        };
    }

    /**
     * Verifica se una richiesta rientra nei limiti.
     */
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