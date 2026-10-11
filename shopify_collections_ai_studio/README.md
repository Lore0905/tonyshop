# Shopify Collections AI Studio

Applicazione locale FastAPI per sincronizzare l'intero catalogo Shopify, proporre percorsi di collection con Ollama, approvare ogni percorso e applicare le modifiche con dry-run e controllo conflitti.

## Requisiti

- Python 3.11+
- Node.js 18+
- Ollama avviato localmente con almeno un modello installato
- app Shopify con scope `read_products,write_products`

## Installazione

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install
cp .env.example .env
```

Compila Tailwind:

```bash
npm run build:css
```

Avvia l'app:

```bash
.venv/bin/python run_server.py
```

Apri `http://127.0.0.1:8780`.

## Flusso operativo

1. **Sincronizza** importa tutti i prodotti, tutte le collection e le membership paginate.
2. Seleziona i prodotti e usa **Genera categorie**.
3. In **Dettagli** approva i singoli percorsi.
4. **Applica su Shopify** crea un dry-run senza scritture; la conferma esegue solo dopo un nuovo controllo conflitti.
5. **Pulisci vuote** effettua una sync fresca, mostra solo collection manuali vuote, richiede una seconda conferma e salva un backup JSON prima dell'eliminazione.

La gerarchia è salvata nel metafield di collection `custom.category_path`. Le smart collection sono visibili ma sempre in sola lettura.
