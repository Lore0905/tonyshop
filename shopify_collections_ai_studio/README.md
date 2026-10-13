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

La gerarchia usa il metafield Shopify esistente `custom.parent_collection`: `collection_reference` salva il GID del padre, `list.collection_reference` salva un array JSON di GID. Il percorso leggibile viene mantenuto anche in `custom.category_path`. I riferimenti a padri mancanti, i cicli e le liste con più padri distinti bloccano la sincronizzazione senza sostituire lo snapshot locale: questa app gestisce percorsi canonici con un solo padre per collection. Le smart collection sono visibili in sola lettura.

Prima di generare o pubblicare viene sincronizzato il catalogo. La pubblicazione riconcilia le appartenenze Shopify con l’ultima approvazione senza richiedere una nuova generazione quando cambiano soltanto le categorie remote. Crea anche i padri mancanti, riusa le collection esistenti e aggiunge il prodotto a ogni livello. I titoli senza gerarchia vengono riusati solo se non ambigui. La creazione usa un handle deterministico per recuperare risposte perse senza duplicare collection.

Le aggiunte sono verificate prima delle rimozioni; la proposta diventa pubblicata soltanto dopo la verifica e la sincronizzazione finale. In caso di errore l’approvazione rimane salvata, viene tentata una sincronizzazione di recupero e una nuova esecuzione ricalcola le sole operazioni mancanti. Se cambia l’approvazione, serve una nuova anteprima. Le anteprime create prima di questo aggiornamento devono essere ricreate.
