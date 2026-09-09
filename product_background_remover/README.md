# Product Background Remover

Dashboard locale per mettere in coda la rimozione dello sfondo delle immagini prodotto Shopify e caricare le versioni elaborate.

## Avvio sul Mac

```bash
cd product_background_remover
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run_server.py
```

Apri [http://127.0.0.1:8765](http://127.0.0.1:8765). Imposta prima nel file `.env` il dominio del negozio e una delle due autenticazioni:

- `SHOPIFY_ADMIN_ACCESS_TOKEN`: token Admin di una custom app (scelta consigliata);
- `SHOPIFY_CLIENT_ID` e `SHOPIFY_CLIENT_SECRET`: mantiene il flusso OAuth client-credentials già presente nel progetto principale.

L'app usa le REST Admin API perché richiesto. La custom app deve avere almeno gli scope `read_products` e `write_products`.

## Dati locali

`data/database.sqlite` conserva prodotti, immagini, URL originali e upload. `data/queue.json` è la coda persistente. Gli originali e le elaborazioni sono in `data/originals/<product_id>/` e `data/processed/<product_id>/`; gli originali non vengono mai eliminati. `rembg` crea prima un ritaglio con alpha e l'app lo compone su bianco puro (`#FFFFFF`), salvando un JPEG pronto per Shopify.

Nel caricamento viene prima creata l'immagine elaborata su Shopify e soltanto dopo viene eliminata quella precedente. L'URL originale, filename e ID della nuova immagine restano nel database locale per eventuale ripristino manuale. Tutte le operazioni sono registrate in `data/app.log`.
