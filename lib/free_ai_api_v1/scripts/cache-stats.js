#!/usr/bin/env node
/**
 * cache-stats.js
 * Mostra statistiche dettagliate della prompt cache.
 *
 * Uso:
 *   node scripts/cache-stats.js
 */
const fs = require("fs");
const path = require("path");

const cachePath = path.join(__dirname, "..", "cache", "prompt-cache.json");

if (!fs.existsSync(cachePath)) {
  console.log("\n📭 Nessuna cache trovata.\n");
  process.exit(0);
}

const data = JSON.parse(fs.readFileSync(cachePath, "utf8"));
const entries = Object.values(data.entries || {});
const totalSaved = entries.reduce((s, e) => s + (e.savedTokens || 0), 0);
const totalHits = data.stats?.hits || 0;
const totalMisses = data.stats?.misses || 0;
const totalRequests = totalHits + totalMisses;

console.log("\n📊 PROMPT CACHE STATISTICS");
console.log("=".repeat(60));
console.log(`Entries in cache:      ${entries.length}`);
console.log(`Total requests:        ${totalRequests}`);
console.log(`Cache hits:            ${totalHits}`);
console.log(`Cache misses:          ${totalMisses}`);
console.log(`Hit rate:              ${totalRequests > 0 ? ((totalHits / totalRequests) * 100).toFixed(1) : 0}%`);
console.log(`Total tokens saved:    ${totalSaved.toLocaleString()}`);
console.log(`Last persisted:        ${data.lastPersisted || "N/A"}`);

if (entries.length > 0) {
  console.log("\n🏆 TOP 10 MOST REUSED PROMPTS");
  console.log("-".repeat(60));
  entries
    .sort((a, b) => (b.usageCount || 0) - (a.usageCount || 0))
    .slice(0, 10)
    .forEach((e, i) => {
      const red = e.tokensBefore > 0
        ? Math.round(((e.tokensBefore - e.tokensAfter) / e.tokensBefore) * 100)
        : 0;
      const preview = e.original.substring(0, 55) + (e.original.length > 55 ? "..." : "");
      console.log(`${String(i + 1).padStart(2)}. "${preview}"`);
      console.log(`    Usage: ${e.usageCount} | Saved: ${e.savedTokens} tk | Reduction: ${red}%`);
    });
}

console.log("\n");