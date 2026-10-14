# Shopify Collections AI Studio

Applicazione locale FastAPI per sincronizzare l'intero catalogo Shopify, proporre percorsi di collection con Ollama, approvare ogni percorso e applicare le modifiche con dry-run e controllo conflitti.

## Requisiti

- Python 3.11+
- Node.js 18+
- Ollama avviato localmente con almeno un modello installato
- app Shopify con scope `read_products,write_products,read_publications,write_publications`

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

La gerarchia usa il metafield Shopify esistente `custom.parent_collection`: `collection_reference` salva il GID del padre, `list.collection_reference` salva un array JSON di GID. Il percorso leggibile viene mantenuto anche in `custom.category_path`. I riferimenti a padri mancanti, i cicli e le liste con più padri distinti bloccano la sincronizzazione senza sostituire lo snapshot locale: questa app gestisce percorsi canonici con un solo padre per collection. Le smart collection sono visibili in sola lettura.

Prima di generare o pubblicare viene sincronizzato il catalogo. La pubblicazione riconcilia le appartenenze Shopify con l’ultima approvazione senza richiedere una nuova generazione quando cambiano soltanto le categorie remote. Crea anche i padri mancanti, riusa le collection esistenti e aggiunge il prodotto a ogni livello. I titoli senza gerarchia vengono riusati solo se non ambigui. La creazione usa un handle deterministico per recuperare risposte perse senza duplicare collection.

Le aggiunte sono verificate prima delle rimozioni; la proposta diventa pubblicata soltanto dopo la verifica e la sincronizzazione finale. In caso di errore l’approvazione rimane salvata, viene tentata una sincronizzazione di recupero e una nuova esecuzione ricalcola le sole operazioni mancanti. Se cambia l’approvazione, serve una nuova anteprima. Le anteprime create prima di questo aggiornamento devono essere ricreate.

Le collection create dall’app (incluse quelle padre) vengono pubblicate su tutte le pubblicazioni Shopify restituite dall’API. Il codice pagina l’elenco dei canali e verifica le pubblicazioni attive della collection dopo ogni aggiornamento. I permessi di pubblicazione sono controllati prima della creazione. Se un canale restituisce un errore o una risposta viene persa, l’approvazione resta salvata: ripetendo la pubblicazione l’app riconosce la collection dal suo handle deterministico e completa soltanto i canali mancanti, prima di rimuovere le vecchie categorie. I canali non accessibili alle credenziali dell’app devono essere resi accessibili su Shopify; gli errori di autorizzazione non vengono trattati come successo.

Le operazioni lunghe della pagina (sync, preparazione AI, anteprima, pubblicazione e pulizia) vengono inviate alla coda SQLite `/api/operations`, con risposta HTTP 202 immediata e stato consultabile. Un worker Shopify le esegue in ordine; il worker AI resta separato e processa un solo prodotto alla volta. Il pulsante di annullamento cancella anche le preparazioni AI in attesa. La coda riprende dopo un riavvio; i controlli di pubblicazione e recupero rimangono attivi.

Il token OAuth viene riusato fino alla scadenza, con rinnovo dopo HTTP 401, e le connessioni HTTP vengono riusate. Per almeno 20 collection, le appartenenze vengono esportate tramite bulk Shopify e lette in streaming; i risultati parziali o con conteggi incoerenti vengono rifiutati. `SHOPIFY_BULK_SYNC=off` ripristina la paginazione ordinaria. Gli aggiornamenti dei metafield vengono deduplicati, confrontati con lo snapshot fresco e inviati in batch da 25. Le scritture della sincronizzazione SQLite avvengono fuori dal thread dell’event loop per mantenere reattive le API della pagina.

La classificazione AI riceve tutti i percorsi delle collection attive (deduplicati per percorso), con ID Shopify, tipo, conteggi ed esempi dei prodotti pertinenti. Riceve anche i percorsi dell’ultima approvazione umana per ciascun prodotto, indipendentemente dalla versione del prompt e dalla pubblicazione. Le proposte non approvate dei prodotti simili sono segnalate come tali e non entrano nel vocabolario canonico.

Il prompt privilegia il riuso esatto di famiglie e gerarchie esistenti: nuove categorie richiedono una differenza merceologica reale, non un modello, una marca, un colore o una keyword aggiuntiva. Dopo la risposta un controllo deterministico corregge grafie e un elenco ristretto di sinonimi espliciti nello stesso ramo, deduplica i percorsi e ricalcola le categorie effettivamente nuove. Famiglie differenti e rami distinti restano distinti. Il prompt predefinito precedente viene aggiornato automaticamente; i prompt personalizzati vengono conservati.

La tassonomia segue categorie stabili e assortimenti coerenti per aiutare la navigazione. I collegamenti tra categorie, sottocategorie e prodotti del tema Shopify rimangono una componente della SEO: riferimento [Google Search Central](https://developers.google.com/search/docs/specialty/ecommerce/help-google-understand-your-ecommerce-site-structure?hl=it).

In **Impostazioni → Categorie principali** puoi aggiungere nomi uno alla volta, rimuovere le card con il cestino oppure importare le collection Shopify il cui `custom.parent_collection` non è valorizzato (campo assente, riferimento vuoto o lista vuota). L’importazione è accodata, pagina tutte le collection e aggiunge i nomi senza duplicare quelli già presenti né cancellare le categorie manuali. Aggiunta e rimozione modificano soltanto l’elenco locale; applica le modifiche con **Salva impostazioni**.

Se l’elenco contiene categorie, la generazione deve utilizzare esclusivamente quei nomi per il primo livello; le sottocategorie rimangono libere secondo il prompt corrente. Il vincolo è passato al modello e verificato lato server: una radice non consentita fa scartare la risposta e ritentare il job. Una modifica dell’elenco durante l’inferenza fa scartare la risposta precedente. Con elenco vuoto rimangono il prompt e la categorizzazione libera attuali.
