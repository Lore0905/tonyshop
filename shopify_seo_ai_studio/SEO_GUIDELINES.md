# SEO AI Studio — criteri implementati e fonti

Verifica documentale: 19 ottobre 2026. Il lavoro riguarda l’app locale e il flusso delle schede prodotto, non un audit completo di tema, Search Console o concorrenza.

- Contenuti utili, originali e attendibili; niente riscritture massive prive di valore o lunghezze minime artificiali: [Google, contenuti utili](https://developers.google.com/search/docs/fundamentals/creating-helpful-content).
- Titoli descrittivi e distinti, senza accumulo di parole chiave: [Google, title link](https://developers.google.com/search/docs/appearance/title-link).
- Titolo commerciale, descrizione e metadati SEO coerenti con il prodotto: [Shopify, keyword SEO](https://help.shopify.com/en/manual/promoting-marketing/seo/adding-keywords). Le fasce di caratteri mostrate nell’app sono consigli editoriali, non limiti di Google né garanzie di ranking. Google può scegliere titoli e snippet differenti.
- Campi pubblicabili: `title`, `descriptionHtml`, `seo.title`, `seo.description`, tramite [ProductUpdateInput](https://shopify.dev/docs/api/admin-graphql/2026-10/input-objects/ProductUpdateInput) e [SEOInput](https://shopify.dev/docs/api/admin-graphql/2026-10/input-objects/SEOInput). Le keyword generate restano nell’analisi locale; non esiste un campo SEO meta keywords standard da valorizzare.
- Sorgente Autofantasy: risorse PrestaShop `products` e `combinations`, lettura paginata con `display` selettivo e `limit`, secondo [parametri Webservice](https://devdocs.prestashop-project.org/9/webservice/tutorials/advanced-use/additional-list-parameters/) e [campi prodotto](https://devdocs.prestashop-project.org/9/webservice/resources/products/).

## Flusso operativo

1. Sincronizza Shopify per acquisire lo stato corrente e gli snapshot locali. La sincronizzazione limita la query per rispettare il costo GraphQL; prodotti con oltre 50 varianti vengono esclusi dall’abbinamento automatico perché il set di SKU non è completo.
2. Import Autofantasy importa i selezionati; senza selezione importa tutto il catalogo locale. Le richieste sono pagine da 100 prodotti/varianti del fornitore, non una richiesta per scheda Shopify. Il lotto viene applicato al DB soltanto dopo il recupero completo.
3. L’abbinamento richiede tutti gli SKU esatti, sensibili alle maiuscole, riferiti a un unico prodotto Autofantasy. Ambiguità e SKU mancanti non vengono risolti tramite somiglianza. Titolo e descrizione (lunga o breve) devono essere disponibili: la sola SEO description non sostituisce la documentazione tecnica.
4. Genera miglioramenti aggiorna una volta il catalogo fornitore per il lotto prima di accodarlo. I prodotti con fonte inutilizzabile sono esclusi con motivazione. L’AI riceve sia il contesto Shopify sia la fonte Autofantasy, con priorità tecnica per quest’ultima.
5. Dopo la generazione: HTML ripulito con allowlist, controllo deterministico di numeri/codici/acronimi/promesse, secondo passaggio AI per verificare affermazioni e conflitti semantici. Le segnalazioni bloccano l’approvazione. Il voto dell’AI non determina l’accuratezza.
6. Nel dettaglio confronta contenuti e Import Autofantasy. Puoi modificare i quattro campi; la descrizione è visuale con formattazione e sorgente HTML consultabile. Il salvataggio esegue nuovamente la revisione tecnica e azzera le approvazioni precedenti.
7. Approva i campi desiderati e premi Pubblica. La pubblicazione ricontrolla validità della fonte (24 ore), hash dell’import, SKU locali, controlli tecnici e conflitti con Shopify. Cambiamenti all’import invalidano le approvazioni. Proposte antecedenti all’introduzione delle fonti richiedono rigenerazione.

I controlli riducono il rischio di dati inventati ma non certificano automaticamente la compatibilità meccanica: occorre leggere e approvare il confronto con la fonte. I dati Shopify e SEO preesistenti non vengono assunti come prova tecnica delle nuove affermazioni.

## Backup e recupero

- Ogni generazione conserva la fonte completa usata, hash e risultato della validazione in `generation_sources`.
- Ogni pubblicazione salva uno snapshot `before_publish` PRIMA della chiamata remota. `publications` conserva campi, prima e dopo e stato pending/published/failed per ogni tentativo. Se la rete cade durante l’invio, sincronizza Shopify per verificare l’esito prima di riprovare.
- Per annullare una pubblicazione recupera `content_json` dello snapshot precedente, confrontalo con la versione attuale e ripubblica soltanto i campi editoriali da ripristinare dopo revisione. Il pulsante Ripristina nello storico ripristina una proposta locale, non annulla direttamente una scrittura remota.
- Prima dell’import iniziale è stato salvato `data/seo-before-autofantasy-20261019.sqlite` usando il backup consistente SQLite. Per ripristinare tutto il DB locale: arresta l’app, conserva una copia dello stato attuale e ripristina il backup con SQLite. Il backup locale non annulla scritture già eseguite su Shopify.

## Altri aspetti SEO

Alt text immagini, collegamenti interni, canonical, redirect, sitemap, dati strutturati Product/Offer/BreadcrumbList, velocità e Search Console restano importanti, ma richiedono interventi e verifiche sui rispettivi ambiti. Questa modifica non inventa metafield né duplica markup del tema. Riferimenti: [Google e-commerce](https://developers.google.com/search/docs/specialty/ecommerce), [Shopify panoramica SEO](https://help.shopify.com/en/manual/promoting-marketing/seo/seo-overview).

## Elaborazione Codex del catalogo (19 ottobre 2026)

Su richiesta dell’utente, il catalogo è stato elaborato senza chiamate a Ollama o servizi AI esterni. Codex ha definito regole editoriali per famiglie di prodotto, con revisione mirata delle anomalie; non sono state eseguite migliaia di generazioni AI indipendenti. Le descrizioni documentate sono state riorganizzate e ripulite; le schede prive di descrizione restano concise e limitate ai fatti del titolo. Non si aggiungono specifiche per allungare i testi.

- Le proposte riportano modello `Codex` e provenienza del metodo in `generation_sources`.
- `Da approvare` identifica le proposte non approvate che hanno superato i controlli locali.
- `Verifica tecnica` identifica fonti mancanti, articolo non identificabile dal titolo, dati contraddittori, voci logistiche e altri blocchi documentati.
- Solo per proposte Codex che documentano questa limitazione è ammessa una fonte contenente il solo titolo. Il normale worker Ollama mantiene i suoi requisiti di fonte completa.
- L’approvazione di una proposta Codex invariata non chiama AI locali. Se i testi vengono modificati, occorre una nuova revisione Codex prima dell’approvazione.
- Snapshot di partenza, lotti sorgente, proposte e audit completo si trovano in `data/codex_catalog_run/`. Il backup `before.sqlite` conserva lo stato precedente.
- Nessuna proposta di questa elaborazione viene approvata o pubblicata automaticamente.

### Seconda revisione editoriale e keyword

La revisione `codex_catalog_revision_v2` risiede in `data/codex_catalog_run/revision_v2/`: confronto prima/dopo per ogni scheda, mappa keyword verso URL esistenti, audit e report SEO. Mantiene i blocchi precedenti e aggiunge quelli emersi dal confronto fra titolo e descrizione importati. Dove Autofantasy è assente, il vecchio testo Shopify non viene riutilizzato come prova: la scheda resta bloccata, con solo titolo identificativo e riferimento.

Le keyword sono ipotesi pertinenti all'intento d'acquisto, ricavate dai fatti della singola fonte e dal linguaggio osservato nelle ricerche pubbliche. Non sono una classifica delle ricerche più frequenti. GSC Wizard ha restituito `subscription_required`; non sono stati acquisiti volumi, impressioni o posizioni. Non sono state attivate sottoscrizioni. I campi di volume nella mappa keyword sono `null`.

La verifica dello stuffing considera separatamente titolo, descrizione e metadati: ripetere il nome del prodotto fra campi diversi è normale. Il punteggio resta una valutazione editoriale locale e non misura vendite, posizionamento o correttezza meccanica.
