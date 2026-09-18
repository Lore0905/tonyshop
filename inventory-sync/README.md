# Sincronizzazione giacenze Autofantasy → Shopify

Lo script legge `products`, `combinations` e `stock_availables` dal Webservice PrestaShop di Autofantasy. Collega le righe tramite ID prodotto/combinazione, abbina gli SKU alle varianti Shopify e confronta la quantità **available nella sede configurata**. Aggiorna solo le differenze, incluse le quantità zero. Gli SKU senza corrispondenza o con inventario non tracciato vengono contati e saltati. SKU duplicati, quantità non valide o nessuna corrispondenza interrompono il job.

## Configurazione GitHub

Impostare in **Settings → Secrets and variables → Actions**:

| Secret | Contenuto |
| --- | --- |
| `AUTOFANTASY_WEBSERVICE_KEY` | Chiave Webservice PrestaShop con permesso GET su `products`, `combinations`, `stock_availables` |
| `SHOPIFY_SHOP_DOMAIN` | Dominio Admin Shopify usato da `commons.js` |
| `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` | Credenziali Shopify per OAuth client credentials, come in `commons.js` |
| `DEFAULT_LOCATION_ID` | ID numerico o GID della sede Shopify da aggiornare |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Bot e chat per notifiche di esito |

L'app Shopify richiede `read_products`, `read_inventory` e `write_inventory`. Se Autofantasy usa multishop, impostare la **variable** `AUTOFANTASY_SHOP_ID` con l'ID shop corretto; il filtro riguarda `stock_availables`.

Il workflow gira ogni giorno alle **07:00 UTC** (09:00 in estate, 08:00 in inverno in Italia). Il primo avvio manuale con `dry_run` attivo confronta tutto senza scrivere. Per applicare gli aggiornamenti, avviare manualmente con `dry_run` disattivato oppure usare la pianificazione. Esecuzione locale: `npm run sync:dry-run` oppure `npm run sync` con le stesse variabili in `.env`.

Ogni esecuzione manda un riepilogo Telegram; un errore imposta il job GitHub come fallito. Per una fonte multishop, scegliere la sede PrestaShop corretta prima della prima esecuzione reale. Non salvare chiavi o token nel repository.
