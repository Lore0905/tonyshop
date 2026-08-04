#!/usr/bin/env node
/**
 * warmup-cache.js
 * Pre-popola la cache ottimizzando un set di istruzioni comuni.
 * Utile per evitare cache miss in produzione su prompt noti.
 *
 * Uso:
 *   node scripts/warmup-cache.js "instruction 1" "instruction 2" ...
 */
require("dotenv").config();

const { PromptManager } = require("../core/prompt-manager");
const { PROVIDERS, PROMPT_CACHE_CONFIG } = require("../constants");
const KeyPool = require("../core/key-pool");
const Logger = require("../core/logger");

const instructions = process.argv.slice(2);

if (instructions.length === 0) {
  console.log("Uso: node warmup-cache.js \"istruzione 1\" \"istruzione 2\" ...");
  process.exit(1);
}

async function main() {
  const keyPool = new KeyPool(PROVIDERS);
  const logger = new Logger();
  const manager = new PromptManager(keyPool, null, logger, {
    cache: PROMPT_CACHE_CONFIG,
    optimizeOnMiss: true
  });

  console.log(`\n🔄 Warmup avviato per ${instructions.length} prompt...\n`);

  for (const instruction of instructions) {
    const preview = instruction.substring(0, 50) + (instruction.length > 50 ? "..." : "");
    process.stdout.write(`  → "${preview}" ... `);
    try {
      const result = await manager.prepare({ instruction });
      if (result.fromCache) {
        console.log("⚡ già in cache");
      } else if (result.fromOptimizer) {
        console.log(`✅ ottimizzato (-${result.stats.reductionPercent}%)`);
      } else {
        console.log("⚠️  fallback originale");
      }
    } catch (e) {
      console.log(`❌ errore: ${e.message}`);
    }
  }

  const stats = manager.getCacheStats();
  console.log(`\n📊 Cache ora contiene ${stats.totalEntries} entry.\n`);
}

main().catch(console.error);