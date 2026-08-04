/**
 * Prompt Normalizer
 * Normalizza il testo prima della generazione dell'hash per massimizzare
 * i cache hit su varianti testuali semanticamente identiche.
 */
function normalizePrompt(text) {
  if (!text || typeof text !== "string") return "";
  return text
    .toLowerCase()
    .replace(/\s+/g, " ")           // collassa spazi multipli
    .replace(/[.!?]+$/, "")          // rimuove punteggiatura finale
    .trim();
}

module.exports = { normalizePrompt };