"""Create a reviewable change report and keyword map from frozen local proposals."""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from import_codex_proposals import RUN, source_index

REV = RUN / "revision_v2"
sources = source_index()
with app.connect() as db:
    handles = {r["id"]: r["handle"] for r in db.execute("SELECT id,handle FROM products")}
rows = []
counts = Counter()
for file in sorted((REV / "proposals").glob("batch_*.json")):
    for record in json.loads(file.read_text()):
        source = sources[record["id"]]
        output = record["output"]
        changed = [k for k in ("title", "descriptionHtml", "metaTitle", "metaDescription")
                   if output[k] != source["current"].get(k, "")]
        counts.update(changed)
        rows.append({
            "id": record["id"],
            "skus": [v.get("sku", "") for v in source.get("variants", [])],
            "url": "https://tonishop.it/products/" + handles[record["id"]],
            "source_url": source["supplier"].get("source_url"),
            "source_hash": source["source_hash"],
            "source_status": source["source_status"],
            "before": source["current"], "proposed": output,
            "changed_fields": changed,
            "status": "verifica_tecnica" if record.get("blocking_issues") else "da_approvare",
            "blocking_issues": record.get("blocking_issues", []),
            "source_limitations": record.get("source_limitations", []),
            "editorial_review": record.get("editorial_review", {}),
        })
(REV / "product_changes.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
keyword_map = [{"id": r["id"], "url": r["url"], "title": r["proposed"]["title"],
                "primary_keyword": next(iter(r["proposed"].get("keywords", [])), None),
                "supporting_keywords": r["proposed"].get("keywords", [])[1:],
                "intent": "transactional", "monthly_volume": None,
                "basis": "Termini del prodotto e lingua delle ricerche pubbliche; nessun volume misurato.",
                "status": r["status"]} for r in rows]
(REV / "keyword_map.json").write_text(json.dumps(keyword_map, ensure_ascii=False, indent=2))
blocked = [r for r in rows if r["blocking_issues"]]
summary = {"created_at": app.now(), "reviewed": len(rows), "ready": len(rows)-len(blocked),
           "blocked": len(blocked), "changed_from_shopify_snapshot": dict(counts),
           "supplier_status": dict(Counter(r["source_status"] for r in rows)),
           "published_by_this_revision": 0, "keyword_volumes": None}
(REV / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
text = f"""# Revisione SEO del catalogo ToniShop

Data: {summary['created_at']}. Analizzate {len(rows):,} schede locali; {summary['ready']:,} proposte da approvare e {len(blocked)} con verifica tecnica obbligatoria. Nessuna pubblicazione eseguita da questa revisione.

## Risultato verificabile

Differenze rispetto ai campi Shopify acquisiti: {dict(counts)}. Il confronto completo prima/dopo, SKU, fonte, hash e blocchi è in `product_changes.json`. La mappa keyword → URL esistente è in `keyword_map.json`; il volume mensile è nullo, non zero.

Le proposte sono state elaborate con regole editoriali per famiglie e controlli mirati delle anomalie. Non sono state eseguite migliaia di inferenze indipendenti. La revisione riduce titoli composti da sinonimi, separa modello del ricambio e modello del veicolo, mantiene misure/posizione/varianti distintive e riorganizza i dettagli importati. Le schede con il solo titolo restano concise: non vengono completate con specifiche dedotte.

## Ricerca delle parole chiave

La selezione segue intento d'acquisto e precisione: tipo di ricambio + modello/codice oppure misura documentata. I termini generici (es. «ricambi pit bike») appartengono alle categorie; una scheda specifica deve intercettare una richiesta specifica (es. carburatore PZ22, camera d'aria con misura esatta). Non si inserisce la medesima lista di keyword in tutti i testi. Le keyword nell'app sono dati di analisi locale e non un inesistente campo Shopify meta keywords.

Le ricerche pubbliche consultate confermano l'uso di termini come «carburatore PZ22», «camera d'aria 80/100-12», «frizione minimoto 49cc», «pastiglie freno Kayo AU180» e «lampadina LED GU10». Sono evidenze linguistiche, non statistiche di popolarità. Ogni applicazione e misura presente nelle proposte deve comunque risultare dall'import Autofantasy di quel prodotto.

Non è stato possibile leggere Search Console tramite GSC Wizard: il servizio ha restituito `subscription_required`. Nessun acquisto o abbonamento attivato. Non sono disponibili volumi, posizioni, CTR, fatturato organico né dati comparativi di traffico. La priorità economica dei prodotti resta quindi da misurare, non viene inventata.

## Criteri applicati

- Titoli descrittivi senza accumulo di sinonimi, quantità espresse chiaramente e dettagli utili a distinguere le varianti.
- Meta title e snippet distinti; le fasce di caratteri sono indicazioni editoriali, non limiti né fattori di ranking garantiti. Google può riscriverli.
- Descrizioni leggibili, HTML semantico, misure e codici documentati, niente promesse di consegna, disponibilità o prestazioni dedotte.
- Controlli su numeri, codici, HTML, duplicati e blocchi; una verifica automatica non certifica ogni compatibilità meccanica.
- URL, SKU, prezzi, stock, stato e canali del prodotto preservati. Fonte e proposta restano confrontabili prima dell'approvazione.

## Interventi ulteriori con priorità e verifica

Questa revisione completa il lavoro editoriale sulle schede locali. Non equivale a un audit tecnico completo del negozio. Le seguenti attività richiedono verifiche dedicate prima di cambiare il tema o i dati live:

| Priorità | Area | Lavoro e criterio di verifica |
| --- | --- | --- |
| P0 | Fonti contraddittorie o assenti | Risolvere i blocchi elencati nel report confrontando documentazione e foto del fornitore; nessuna approvazione automatica. |
| P0 | Indicizzazione | Verificare status HTTP, robots, sitemap, noindex e canonical di homepage/categorie/prodotti. I tentativi di lettura web non riusciti non provano errori del sito. |
| P1 | Categorie e link interni | Assegnare le query generiche alle categorie e i modelli alle pagine pertinenti, verificando prodotti effettivamente pubblicati. Collegare solo destinazioni reali e utili. |
| P1 | Product/Offer/Breadcrumb | Validare il markup già emesso dal tema e la corrispondenza con prezzo/stock correnti; evitare JSON-LD aggiunto manualmente alle descrizioni. |
| P1 | Immagini e mobile | Controllare alt text su immagini effettive, leggibilità, layout e Core Web Vitals. Nessuna descrizione visiva dedotta dal solo titolo. |
| P1 | Misurazione | Acquisire almeno 3 mesi di query/pagine GSC e dati di conversione; stabilire baseline prima della pubblicazione. |
| P2 | Contenuti di supporto | Guide e ricerca ricambi per modello/codice con competenza tecnica verificata; niente migliaia di pagine quasi uguali. |

Per le prime 4–8 settimane dopo un'eventuale pubblicazione, confrontare clic, impressioni, CTR e conversioni delle stesse pagine/query con un periodo comparabile, distinguendo brand/non-brand e mobile/desktop. Non attribuire una variazione di vendite alla SEO senza dati sufficienti.

## Fonti consultate

- [Google: titoli descrittivi e distinti](https://developers.google.com/search/docs/appearance/title-link)
- [Google: snippet e meta description](https://developers.google.com/search/docs/appearance/snippet)
- [Google: contenuti utili e attendibili](https://developers.google.com/search/docs/fundamentals/creating-helpful-content)
- [Shopify: inserimento delle keyword](https://help.shopify.com/en/manual/promoting-marketing/seo/adding-keywords)
- [Google: SEO per e-commerce](https://developers.google.com/search/docs/specialty/ecommerce)
- [Autofantasy: esempio pubblico pastiglie Kayo AU180](https://autofantasy.it/pompe-e-pinze-freni/4375-pastiglie-freno-posteriori-per-quad-kayo-au180-180cc.html)
- [Quaditalia: esempio pubblico ricerca per misura](https://www.quaditalia.it/prodotto/camera-daria-80-100-12-per-gomma-pit-bike/)
- [Minimotolandia: esempio pubblico frizione minimoto](https://www.minimotolandia.com/frizione-2-masse-minimoto-aria-49cc)

## Backup e ripristino

`before.sqlite` è il backup consistente prima della revisione. Le nuove generazioni non sovrascrivono i campi prodotto. Per scartare una proposta usare lo storico locale; per un ripristino integrale arrestare il server, salvare lo stato corrente e recuperare il backup. Il ripristino del DB locale non annulla pubblicazioni Shopify eventualmente eseguite separatamente.
"""
(REV / "REPORT_SEO.md").write_text(text)
print(json.dumps(summary, ensure_ascii=False))
