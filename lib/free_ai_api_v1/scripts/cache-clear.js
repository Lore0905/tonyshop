#!/usr/bin/env node
/**
 * cache-clear.js
 * Svuota completamente la cache dei prompt.
 *
 * Uso:
 *   node scripts/cache-clear.js [--force]
 */
const fs = require("fs");
const path = require("path");

const cachePath = path.join(__dirname, "..", "cache", "prompt-cache.json");

if (!fs.existsSync(cachePath)) {
  console.log("📭 Nessun file cache trovato.");
  process.exit(0);
}

const force = process.argv.includes("--force");

if (!force) {
  console.log("⚠️  Questo eliminerà permanentemente tutte le entry della cache.");
  console.log(`   File: ${cachePath}`);
  console.log("   Riesegui con --force per confermare.\n");
  process.exit(0);
}

fs.unlinkSync(cachePath);
console.log("🗑️  Cache svuotata con successo.\n");