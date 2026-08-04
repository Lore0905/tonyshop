/**
 * Token Estimator - stima conservativa del numero di token di un testo.
 * 
 * Euristiche:
 * - Testo prevalentemente ASCII (latino/inglese): ~3.5 caratteri/token
 * - Testo misto: ~2.5 caratteri/token  
 * - Testo prevalentemente non-ASCII (CJK, emoji, arabo): ~1.5 caratteri/token
 * - Margine di sicurezza del +20% (sovrastima intenzionale)
 * 
 * La sovrastima è voluta per evitare chiamate API che sarebbero destinate 
 * a fallire per eccesso di token.
 */
function estimateTokens(text) {
  if (!text || typeof text !== "string") return 0;
  
  const len = text.length;
  if (len === 0) return 0;
  
  let nonAscii = 0;
  for (let i = 0; i < len; i++) {
    if (text.charCodeAt(i) > 127) nonAscii++;
  }
  
  const asciiRatio = (len - nonAscii) / len;
  let factor;
  if (asciiRatio > 0.9) {
    factor = 3.5;
  } else if (asciiRatio > 0.5) {
    factor = 2.5;
  } else {
    factor = 1.5;
  }
  
  // Margine di sicurezza 20%
  return Math.ceil((len / factor) * 1.2);
}

module.exports = { estimateTokens };