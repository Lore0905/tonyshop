#!/usr/bin/env node
/**
 * optimize-prompt.js
 * Ottimizza un singolo prompt via CLI e mostra il confronto.
 *
 * Uso:
 *   node scripts/optimize-prompt.js "Aggiungimi la chiave toDeleted in questo array"
 */
require("dotenv").config();

const { PromptManager } = require("../core/prompt-manager");
const { PROVIDERS, PROMPT_CACHE_CONFIG } = require("../constants");
const KeyPool = require("../core/key-pool");
const Logger = require("../core/logger");

const instruction = process.argv[2];

if (!instruction) {
  console.log("Uso: node optimize-prompt.js \"la tua istruzione qui\"");
  process.exit(1);
}

async function main() {
  const keyPool = new KeyPool(PROVIDERS);
  const logger = new Logger();
  const manager = new PromptManager(keyPool, null, logger, {
    cache: PROMPT_CACHE_CONFIG,
    optimizeOnMiss: true
  });

  console.log("\n📝 ORIGINALE:");
  console.log("-".repeat(60));
  console.log(instruction);

  const result = await manager.prepare({ instruction });

  console.log("\n⚡ OTTIMIZZATO:");
  console.log("-".repeat(60));
  console.log(result.optimized || instruction);

  if (result.stats) {
    console.log("\n📉 STATISTICHE:");
    console.log(`   Token prima:  ${result.stats.before}`);
    console.log(`   Token dopo:   ${result.stats.after}`);
    console.log(`   Risparmio:    ${result.stats.saved} token (${result.stats.reductionPercent}%)`);
    console.log(`   In cache:     ${result.fromCache ? "Sì (HIT)" : "No (MISS, salvato ora)"}`);
  }
  console.log("");
}

main().catch(console.error);