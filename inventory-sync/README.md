# Sincronizzazione giacenze Autofantasy → Shopify

Lo script legge `products`, `combinations` e `stock_availables` dal Webservice PrestaShop di Autofantasy. Collega le righe tramite ID prodotto/combinazione, abbina gli SKU alle varianti Shopify e confronta la quantità **available nella sede configurata**. Aggiorna solo le differenze, incluse le quantità zero. Gli SKU senza corrispondenza o con inventario non tracciato vengono contati e saltati. Gli SKU duplicati nella fonte o in Shopify sono esclusi e riportati nel log/riepilogo, perché non identificano una giacenza univoca. Quantità non valide o nessuna corrispondenza interrompono il job.

## Configurazione GitHub

Impostare in **Settings → Secrets and variables → Actions**:

| Secret | Contenuto |
| --- | --- |
| `AUTOFANTASY_WEBSERVICE_KEY` | Chiave Webservice PrestaShop con permesso GET su `products`, `combinations`, `stock_availables` |
| `SHOPIFY_SHOP_DOMAIN` | Dominio Admin Shopify usato da `commons.js` |
| `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` | Credenziali Shopify per OAuth client credentials, come in `commons.js` |
| `DEFAULT_LOCATION_ID` | ID numerico o GID della sede Shopify da aggiornare |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Bot e chat per notifiche di esito |

L'app Shopify richiede `read_products`, `read_inventory` e `write_inventory`. Lo script legge lo **shop PrestaShop 1** per impostazione predefinita: per `M271` corrisponde alla quantità 0 dell'export pubblico Autofantasy, mentre gli shop 2 e 3 riportano 12. Per usare un altro negozio, impostare la **variable** `AUTOFANTASY_SHOP_ID`; il filtro riguarda `stock_availables`. Le righe aggregate dei prodotti con combinazioni sono escluse: lo stock viene preso dalle combinazioni.

Il workflow gira ogni giorno alle **07:00 UTC** (09:00 in estate, 08:00 in inverno in Italia). Il primo avvio manuale con `dry_run` attivo confronta tutto senza scrivere. Per applicare gli aggiornamenti, avviare manualmente con `dry_run` disattivato oppure usare la pianificazione. Esecuzione locale: `npm run sync:dry-run` oppure `npm run sync` con le stesse variabili in `.env`.

Ogni esecuzione manda un riepilogo Telegram; un errore imposta il job GitHub come fallito. Per una fonte multishop, scegliere la sede PrestaShop corretta prima della prima esecuzione reale. Non salvare chiavi o token nel repository.
