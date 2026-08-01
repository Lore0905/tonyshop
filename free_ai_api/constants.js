// Api key available
const API_KEYS = {
    "gemini": {
        "keys" : [
            {
                id: "AQ.Ab8RN6IITcwZl5EoNxVxCTduS-RaRiQR2owioe6x6OgzVaAmkg",
                limit: 24
            },
            {
                id: "AQ.Ab8RN6KkXQKPox1i0WGRK_tLsThhz4WAPmpBRLRxGgZGI1L41w",
                limit: 24
            },
            {
                id: "AQ.Ab8RN6LZcN2qzGJtSRl0V1DfKA0PZQIM1CfRICGanILcNloBRA",
                limit: 24
            },
            {
                id: "AQ.Ab8RN6K_iECj-ahsGbuseWDl9sBZhLL9fpHOzWVxkvt3GhE-2g",
                limit: 24
            }
        ],
        "model": [
            "gemini-2.5-flash", // ← più recente, 1M input / 65K output
            "gemini-2.5-flash-lite", // ← versione lite del 2.5
            "gemini-flash-latest", // ← alias dinamico all'ultimo Flash
            "gemini-2.0-flash-lite", // ← se il 2.5 è in overload
            "gemini-2.0-flash-lite-001", // ← versione pinned del lite,
            "gemini-3.6-flash"
        ]
    }
}

module.exports = {
    API_KEYS
}