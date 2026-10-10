# Shopify SEO AI Studio

Applicazione locale separata da `product_background_remover`, costruita con gli stessi principi: FastAPI, frontend statico, SQLite e worker asincrono persistente. Analizza il catalogo, genera proposte con Ollama in JSON strutturato, conserva lo storico, permette approvazioni per campo e pubblica tramite Shopify Admin GraphQL con controllo dei conflitti.

## Avvio

```bash
cd shopify_seo_ai_studio
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run_server.py
```

Apri <http://127.0.0.1:8770>. Configura Ollama dall'interfaccia. L'elaborazione non parte automaticamente: sincronizza, seleziona i prodotti e scegli **Analizza SEO** o **Genera miglioramenti**.

## Layout Tailwind

Il CSS dell'interfaccia viene compilato localmente con Tailwind CSS. Dopo una modifica a `static/input.css`, `static/index.html` o alle classi presenti in `static/app.js`:

```bash
npm install
npm run build:css
```

Durante lo sviluppo è disponibile anche `npm run watch:css`. Il browser carica soltanto `static/styles.css`: non è richiesta alcuna CDN o runtime Tailwind.

## Variabili d'ambiente

- `SHOPIFY_SHOP_DOMAIN`: dominio `*.myshopify.com`.
- `SHOPIFY_ADMIN_ACCESS_TOKEN`: token Admin con `read_products` e `write_products`.
- In alternativa `SHOPIFY_CLIENT_ID` e `SHOPIFY_CLIENT_SECRET` per client credentials.
- `SHOPIFY_API_VERSION`: predefinita `2026-10`.
- `APP_HOST`, `APP_PORT`: bind locale, predefiniti `127.0.0.1:8770`.

I token non vengono salvati nel database o nei log. `data/seo.sqlite` contiene catalogo, snapshot, analisi, versioni prompt, generazioni, approvazioni, pubblicazioni, coda e log applicativi.

## Test

```bash
pytest -q
```

I test non contattano Shopify o Ollama. Il server Ollama deve supportare `/api/chat`, `/api/tags` e JSON Schema nel parametro `format`.
