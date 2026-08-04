const { DEFAULT_CONFIG } = require("../constants");

/**
 * Adapter Groq - logica HTTP pura
 *
 * Interfaccia uniforme:
 *   call({ prompt, model, apiKey, temperature, maxTokens, abortSignal })
 *
 * Nota: l'API Groq è compatibile con il formato OpenAI (/chat/completions).
 */
async function call({ prompt, model, apiKey, temperature, maxTokens, abortSignal }) {
    const endpoint = "https://api.groq.com/openai/v1/chat/completions";

    const body = {
        model,
        messages: [{ role: "user", content: prompt }]
    };

    if (temperature !== undefined) {
        body.temperature = temperature;
    }
    if (maxTokens !== undefined) {
        body.max_tokens = maxTokens;
    }

    const response = await fetch(endpoint, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${apiKey}`
        },
        body: JSON.stringify(body),
        signal: abortSignal
    });

    if (!response.ok) {
        const errorText = await response.text().catch(() => "");
        const error = new Error(`Groq API error ${response.status}: ${response.statusText}. ${errorText}`);
        error.status = response.status;
        error.provider = "groq";
        throw error;
    }

    const data = await response.json();

    let text = "";
    if (data.choices && data.choices[0] && data.choices[0].message) {
        text = data.choices[0].message.content || "";
    }

    return {
        text,
        model,
        provider: "groq",
        raw: data
    };
}

module.exports = { call };