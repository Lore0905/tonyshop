require("dotenv").config();
const { freeCallApi } = require("../index");

async function test() {
    console.log("🚀 Test free_ai_api iniziato...\n");

    try {
        // Test 1: chiamata semplice (nessun provider specificato)
        console.log("--- Test 1: Chiamata automatica ---");
        const res1 = await freeCallApi({
            prompt: "Dimmi una curiosità scientifica in una frase.",
            model: "openai/gpt-oss-120b"
        });
        console.log("✅ Risposta:", res1.text);
        console.log("   Provider:", res1.provider);
        console.log("   Modello:", res1.model);

    } catch (err) {
        console.error("❌ Errore Test 1:", err.message);
    }
}

test();