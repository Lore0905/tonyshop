/**
 * utils/token-estimator.js (FIXED v2)
 * FIX #10: stima accurata per tipo di contenuto (text/json/code/instruction).
 */

/**
 * Stima token in modo accurato in base al tipo di contenuto.
 * @param {string|object} content
 * @param {string} type - 'text', 'json', 'code', 'instruction'
 * @returns {number}
 */
function estimateTokens(content, type = "text") {
    if (!content) return 0;
    let text = typeof content === "string" ? content : JSON.stringify(content);
    const charCount = text.length;
    if (charCount === 0) return 0;

    const words = text.split(/\s+/).filter(w => w.length > 0);
    const wordCount = words.length;
    const specialChars = (text.match(/[{}[\]":,\n\t]/g) || []).length;
    const upperRatio = (text.match(/[A-Z]/g) || []).length / charCount;

    let charsPerToken = 3.8;
    switch (type) {
        case "json":  charsPerToken = 2.5; break;
        case "code":  charsPerToken = 3.0; break;
        case "instruction": charsPerToken = 3.2; break;
        default:
            if (upperRatio > 0.3) charsPerToken = 3.2;
            if (specialChars / charCount > 0.15) charsPerToken = 3.0;
    }

    const byChars = charCount / charsPerToken;
    const byWords = wordCount / 0.75;
    let estimate = (type === "json" || type === "code")
        ? byChars * 0.8 + byWords * 0.2
        : byChars * 0.6 + byWords * 0.4;

    estimate *= 1.05; // overhead formattazione
    estimate *= 1.15; // margine sicurezza 15%
    return Math.ceil(estimate);
}

module.exports = { estimateTokens };