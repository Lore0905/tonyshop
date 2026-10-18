"""Reviewed catalog migration: prepare proposals, never approve or publish.
Run with --save only after reviewing data/codex_all_plan_2026-10-16.json.
"""
import json,re,sys,unicodedata,collections,uuid,sqlite3
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from core import reuse_canonical_categories,compatibility_branch,path_key
INPUT=app.DATA/'codex_all_input_2026-10-16.json'
PLAN=app.DATA/'codex_all_plan_2026-10-16.json'
rows=json.loads(INPUT.read_text())
def norm(s):
 s=unicodedata.normalize('NFKD',s.casefold());s=''.join(c for c in s if not unicodedata.combining(c));return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9]+',' ',s)).strip()
def principal(t):
 t=norm(t)
 t=re.sub(r'^(\d+)(set|kit)\b',r'\1 \2',t)
 # Packaging/quantity is not the component being sold.
 for _ in range(5):
  n=re.sub(r'^(?:(?:\d+\s*(?:pz|cp|pezzi|coppia|coppie))|coppia|kit|set|solo|cp|\d+|1 scelta|completo|completa|di|1|4pz|3pz)\s+','',t)
  if n==t:break
  t=n
 return t
RULES=[]
def rule(pattern,path):RULES.append((re.compile(pattern),path))
def R(p,f,c):rule(p,f'Ricambi/{f}/{c}')
def G(p,c,f='Ricambi'):rule(p,f'Giardinaggio/{f}/{c}')
def L(p,c,f='Ricambi'):rule(p,f'illuminazione/{f}/{c}')
# Domain is determined independently below. Garden components use the same leaf,
# with dedicated overrides for complete machinery and attachments.
R(r'^(?:camera (?:d )?aria|kenda camera)','Ricambi gomme',"Camere d'aria")
R(r'^(?:gomma|gomme|cerchio|cerchi|ruota|ruote).*(?:cerchiat|gommat|assemblat|complet)|^motard cerchi','Ruote e mozzi','Ruote complete')
R(r'^(?:pneumatic[oi]|coperton[ei]|kenda gomma)|^gomma (?:\d|anteriore|posteriore|per quad|monopattino|piena|pit bike|pmt|racing|slick|tubl|copertone)','Ricambi gomme','Pneumatici')
R(r'^valvol[ae].*tubeless|^pneumatici valvola|^2 dadi ferma','Ricambi gomme','Valvole pneumatici')
R(r'^(?:cerchi[oi])\b','Ruote e mozzi','Cerchi')
R(r'^(?:asse|assale|solo asse)\b|^perno asse','Ruote e mozzi','Assi ruota')
R(r'^mozz[oi]\b|^gruppo mozzo','Ruote e mozzi','Mozzi')
R(r'^(?:pern[oi] ruot|pern[oi] anteriore|pern[oi] posteriore|raggi?\b|di raggi|spessori perno ruota)|^gomma copri bullone|^copri (?:asse|mozzo)|^mezzelune posteriori','Ruote e mozzi','Componenti ruota')
R(r'^ammortizzator|^sospension','Sospensioni','Ammortizzatori')
R(r'^forcell[aei]\b|^steli forcell|^forcelle sospension','Sospensioni','Forcelle')
R(r'^(?:carro|forcellone)\b','Sospensioni','Forcelloni')
R(r'^(?:braccett[oi]|bracci)\b(?!.*sterzo)|^trapezio','Sospensioni','Braccetti')
R(r'^snod[oi]\b|^testina snodo','Sospensioni','Snodi')
R(r'^boccol[ae].*(?:bracc|carro|forcellone)|^rondell[ae].*(?:bracc|carro)|^spessor[ei].*forcellone','Sospensioni','Boccole')
R(r'^(?:paraol[io]|paraoli|parapolvere).*(?:forcell|sterzo)|^forcella paraolio','Sospensioni','Paraoli forcelle')
R(r'^parastel|^protezione ammortizzatore','Sospensioni','Protezioni forcelle')
R(r'^(?:piastre?|canotto|piantone|perno canotto|palo manubrio|asta manubrio|braccetto.*sterzo|serie cuscinetti.*sterzo|bulloni cuscinetti.*sterzo|cuscinetto sterzo|cuscinetti sterzo)\b','Manubri e comandi','Sterzo')
R(r'^(?:manopola|manopole)\b','Manubri e comandi','Manopole')
R(r'^(?:acceleratore|gas rapido|rapido|comando (?:acceleratore|gas)|gruppo acceleratore|impugnatura acceleratore|leva acceleratore|pulsante acceleratore)\b','Manubri e comandi','Acceleratori')
R(r'^(?:manubri[oi]|semi manubri|traversino)\b','Manubri e comandi','Manubri')
R(r'^(?:raiser|riser|support[oi] manubrio|supporto raiser)|^pern[oi] support[oi].*(?:manubrio|riser)','Manubri e comandi','Supporti manubrio')
R(r'^pastigli[ae]\b','Freni','Pastiglie')
R(r'^disco (?:anteoriore|freno)|^dischi freno','Freni','Dischi freno')
R(r'^ganasc[ei]|^massette freno','Freni','Ganasce')
R(r'^pinza\b','Freni','Pinze')
R(r'^pomp[ae] freno','Freni','Pompe freno')
R(r'^(?:gruppo freno|freni pompa|freno (?:anteriore|posteriore|a disco)|impianto.*freno|(?:anteriore|posteriore) freno|fren[oi] (?:anteriore|posteriore))','Freni','Impianti frenanti')
R(r'^(?:pedal[ei]|pedalin[ae]|freno pedale).*fren','Freni','Pedali freno')
R(r'^(?:leva|leve).*fren|^supporto.leva freno','Freni','Leve freno')
R(r'^(?:cavo|filo|guaina|tubo|tubi).*fren','Freni','Cavi freno')
R(r'^supporto.*(?:pinza|disco)|^gommino.*pinza','Freni','Supporti freno')
R(r'^liquido freni','Freni','Liquido freni')
R(r'^leva (?:cambio|marce)|^pedalina (?:leva|marce)|^pedale leva','Ricambi motori','Leve cambio')
R(r'^(?:leva|leve).*frizion','Manubri e comandi','Leve frizione')
R(r'^(?:cavo|filo|guaina).*frizion','Manubri e comandi','Cavi frizione')
R(r'^(?:cavo|filo|guaina).*accelerator|^filo lungo','Manubri e comandi','Cavi acceleratore')
R(r'^leva aria|^filo aria','Manubri e comandi','Comandi aria')
R(r'^valvol[ae]\b|^semiconi|^piattell[oi].*valvol|^moll[ae] valvol','Ricambi motori','Valvole')
R(r'^albero a camme','Ricambi motori','Albero a camme')
R(r'^alber[oi]\b(?!.*secondario)|^albero.*biella','Ricambi motori','Alberi motore')
R(r'^bilancier','Ricambi motori','Bilancieri')
R(r'^gruppo termico|^cilindro.*pistone|^motore.*cilindro','Ricambi motori','Gruppi termici')
R(r'^cilindr[oi]\b','Ricambi motori','Cilindri')
R(r'^piston[ei]\b','Ricambi motori','Pistoni')
R(r'^fasce?\b','Ricambi motori','Fasce elastiche')
R(r'^test[ae]\b|^testata\b','Ricambi motori','Testate')
R(r'^carburator|^pz26 carburatore','Ricambi motori','Carburatori')
R(r'^corpo farfallato','Ricambi motori','Corpi farfallati')
R(r'^(?:iniettore|di iniezione)','Ricambi motori','Iniettori')
R(r'^(?:air ?box|scatola (?:aria|filtro)|plastich?e? filtro|plastica spugna filtro)','Ricambi motori','Scatole filtro')
R(r'^filtro (?:a |aria|spugna|in spugna|in cotone)|^spugna.*filtro','Ricambi motori','Filtri aria')
R(r'^filtro (?:benzina|carburante)','Ricambi motori','Filtri benzina')
R(r'^filtro olio','Ricambi motori','Filtri olio')
R(r'^(?:collettor[ei]|adattatore.*(?:collettore|carburatore))','Ricambi motori','Collettori')
R(r'^pacco lamell','Ricambi motori','Pacchi lamellari')
R(r'^rubinett','Ricambi motori','Rubinetti benzina')
R(r'^serbatoio','Ricambi motori','Serbatoi')
R(r'^tapp[oi].*serbatoio','Ricambi motori','Tappi serbatoio')
R(r'^tub[oi].*(?:benzina|carburante|iniettore)|^tubi serbatoio','Ricambi motori','Tubi carburante')
R(r'^(?:guarnizion|o ring|oring|o r )','Ricambi motori','Guarnizioni')
R(r'^(?:paraolio|paraoli|parolio|gommino tenuta olio)','Ricambi motori','Paraoli')
R(r'^carter\b|^coperchio.*(?:carter|blocco)|^copri testa','Ricambi motori','Carter')
R(r'^tapp[oi].*(?:testa|carter|olio|valvola)|^vetrino controllo olio','Ricambi motori','Tappi motore')
R(r'^marmitt|^silenziator|^terminale|^terminali|^tubo.*(?:scarico|marmitta)|^scarico','Ricambi motori','Scarichi')
R(r'^radiator','Ricambi motori','Radiatori')
R(r'^pomp[ae] (?:acqua|del |dell |olio)','Ricambi motori','Pompe olio')
R(r'^campana','Ricambi motori','Campane frizione')
R(r'^frizion|^disco frizione|^spingi disch|^innesto frizione|^piattell[oi].*frizione','Ricambi motori','Frizioni')
R(r'^(?:cambio|desmo|selettor[ei]|albero secondario)','Ricambi motori','Cambi')
R(r'^(?:ingranagg|ingranggi)','Ricambi motori','Ingranaggi')
R(r'^ruota libera','Ricambi motori','Ruote libere')
R(r'^variator','Trasmissione','Variatori')
R(r'^cinghi[ae] di','Trasmissione','Cinghie')
R(r'^giunto cardan','Trasmissione','Giunti cardanici')
R(r'^catena.*(?:distribuzione|pompa olio)|^catena coppia pattini|^distribuzione','Ricambi motori','Distribuzione')
R(r'^pignone.*(?:avviamento|puleggia)|^puleggia pignone','Ricambi motori','Ingranaggi')
R(r'^pignon','Trasmissione','Pignoni')
R(r'^coron','Trasmissione','Corone')
R(r'^catena|^catene','Trasmissione','Catene')
R(r'^falsa maglia','Trasmissione','Giunti catena')
R(r'^(?:tirante|tendi|guida|scorri|pattin[oi]|rotell[ae]|boccola tendi|braccio tendi).*catena','Trasmissione','Tendicatena')
R(r'^batteri[ae]|^pacco batteria','Ricambi elettrico','Batterie')
R(r'^(?:carica batter|caricabatter)','Ricambi elettrico','Caricabatterie')
R(r'^bms\b','Ricambi elettrico','Gestione batterie')
R(r'^(?:cablaggio|impianto.*elettrico|impianto cablaggio)','Ricambi elettrico','Cablaggi')
R(r'^(?:centralin|cdi |ecu |controller centralina|inverter centralina)','Ricambi elettrico','Centraline')
R(r'^bobin','Ricambi elettrico','Bobine accensione')
R(r'^candel|^pipetta candela','Ricambi elettrico','Candele')
R(r'^stator','Ricambi elettrico','Statori')
R(r'^volan[oi]','Ricambi elettrico','Volani')
R(r'^regolatore.*(?:tensione|esterno)','Ricambi elettrico','Regolatori tensione')
R(r'^rele\b','Ricambi elettrico','Relè')
R(r'^fusibil|^portafusibil','Ricambi elettrico','Fusibili')
R(r'^(?:interruttor|tast[oi]|tasti|pulsante|comando tasti|gruppo tasti|gruppo comandi)','Ricambi elettrico','Interruttori')
R(r'^(?:sensore|induttore)','Ricambi elettrico','Sensori')
R(r'^(?:display|tachimetro|conta ore)','Ricambi elettrico','Strumentazione')
R(r'^clacson|^campanello','Segnalazione ottica','Segnalatori acustici')
R(r'^blocchett|^blochetto|^serrature','Ricambi elettrico','Blocchetti chiavi')
R(r'^spinott|^attacco spinotto|^presa di|^presa ingresso|^adattatore connettore','Ricambi elettrico','Connettori')
R(r'^motorino.*avviamento|^motorino di avviamento','Ricambi elettrico','Motorini avviamento')
R(r'^avviamento|^accensione (?:superiore|standard)|^pedalina avviamento','Ricambi motori','Avviamenti')
R(r'^(?:blocco motore|blocco per|blocco yx|motore|propulsore)\b','Ricambi motori','Motori completi')
R(r'^fanale|^fanali|^faro|^fari','Segnalazione ottica','Fanali')
R(r'^frecc[ei]','Segnalazione ottica','Frecce')
R(r'^lampadin[ae]','Segnalazione ottica','Lampadine')
R(r'^catarinfrangente|^plastica riflettente','Segnalazione ottica','Catarifrangenti')
R(r'^luce (?:stop|della)','Segnalazione ottica','Luci targa')
R(r'^adesiv|^sticker','Accessori veicoli','Adesivi')
R(r'^parafang','Accessori veicoli','Parafanghi')
R(r'^paramani','Accessori veicoli','Paramani')
R(r'^paracolpi|^paramotor','Accessori veicoli','Paracolpi')
R(r'^carena|^carene|^plastic[ah]e?\b|^plasiche|^codone|^cupolino','Accessori veicoli','Carene')
R(r'^sella\b|^sedile|^poggia schiena|^poggiatesta','Accessori veicoli','Selle')
R(r'^(?:pedal[ei]|pedalin[ae]|pedan[ae]|poggiapiedi|poggia piedi|coppa pedane)\b','Accessori veicoli','Pedaline e poggiapiedi')
R(r'^baule|^baulett|^pizza box','Accessori veicoli','Bauletti')
R(r'^portapacch','Accessori veicoli','Portapacchi')
R(r'^portatarga','Accessori veicoli','Portatarga')
R(r'^specchiett','Accessori veicoli','Specchietti')
R(r'^cavall?etto|^sollevamoto','Accessori veicoli','Cavalletti')
R(r'^gancio traino','Accessori veicoli','Ganci traino')
R(r'^allarme|^antifurto','Accessori veicoli','Antifurti')
R(r'^telaio|^telaietto','Accessori veicoli','Telai')
R(r'^protezione|^paracatena','Accessori veicoli','Protezioni')
R(r'^cuscinet|^gabbia rulli','Minuteria e fissaggi','Cuscinetti')
R(r'^dado|^dadi|^rondell|^vit[ei]\b|^bullon|^prigion|^copigl|^coppigl|^senger|^chiavett|^chiavell|^spina\b','Minuteria e fissaggi','Viteria')
R(r'^fascett','Minuteria e fissaggi','Fascette')
R(r'^boccol|^spessor','Minuteria e fissaggi','Boccole')
# Lighting stock must not inherit erroneous vehicle product_type values.
L(r'^alimentatore|^mean well|^mw ', 'Alimentatori')
L(r'^applique','Applique','Lampade')
L(r'^lampadario','Lampadari','Lampade')
L(r'^plafonier|^40w alluminio','Plafoniere','Lampade')
L(r'^pannell[oi] led','Pannelli LED','Lampade')
L(r'^(?:faretto|mw\d+ faretto|palett[oi] giardino)','Faretti','Lampade')
L(r'^lampad[ae] led|^green lighting|^v tac lampadina','Lampadine')
L(r'^strip led|^strisc[ei]a? led|^tubo led neon','Strisce LED')
L(r'^tubo led','Tubi LED')
L(r'^barra led','Barre LED')
L(r'^(?:profilo|2mt profilo|aluminium profile)','Profili LED')
L(r'^controller rgb|^amplificatore rgb|^centralina dimmer','Controller LED')
L(r'^chip led|^modul[oi] led|^led modul|^piattina pcb','Moduli LED')
L(r'^portalampad','Portalampade')
# Whole vehicles must be matched by principal phrase, never compatibility mentions.
rule(r'^(?:quad atv|quad kayo|at110 predator|at125 predator|au150 kayo|tor 200|renegade 110|kayo atv quad|miniatv miniquad|miniquad)\b','Veicoli/Veicoli quad')
rule(r'^(?:minicross|kayo kmb|kayo kt50)\b','Veicoli/Minicross')
rule(r'^(?:pit bike|motard)\b','Veicoli/Veicoli pitbike')
rule(r'^(?:minimoto|minigp)\b','Veicoli/Veicoli minimoto')
rule(r'^(?:cross (?:125cc|250cc|kayo|kt250)|kayo k2|kayo k6r)\b','Veicoli/Motocross')
rule(r'^(?:utv buggy|es50 buggy|kayo buggy)\b','Veicoli/Buggy')
rule(r'^scooter elettrico','Veicoli/Scooter elettrici')
rule(r'^monopattino elettrico','Veicoli/Monopattini elettrici')
rule(r'^hoverboard','Veicoli/Hoverboard')
rule(r'^kayo balance bike','Veicoli/Biciclette elettriche')
rule(r'^salotto da','Arredamento/Giardino/Salotti')
for pattern,leaf in [(r'^decespugliatore','Decespugliatori'),(r'^tagliasiepi a','Tagliasiepi'),(r'^soffiatore a','Soffiatori'),(r'^robot tagliaerba','Robot tagliaerba'),(r'^motocoltivatore','Motocoltivatori'),(r'^motozappa a','Motozappe'),(r'^motopompa per','Motopompe'),(r'^atomizzatore','Atomizzatori')]:
 rule(pattern,'Giardinaggio/'+leaf)
# These are attachments, not entire machines.
G(r'^accessorio fresa','Frese','Accessori')
G(r'^accessorio tagliasiep','Tagliasiepi','Accessori')
G(r'^inserto motosega|^testina potatore','Potatori','Accessori')
G(r'^funzione barra falciante','Barre falcianti','Accessori')
G(r'^aspirazione aspiratore','Aspiratori','Accessori')
G(r'^punta per','Punte trivella','Accessori')
G(r'^asta (?:di trasmissione|fissa|multifunzione)|^tubo asta|^tubo flessibile','Aste trasmissione')
G(r'^barra (?:carving|catena|guida|motosega)','Barre motosega')
G(r'^testina (?:batti|filo)','Testine')
G(r'^lama.*decespugliatore|^disco (?:da |taglio)','Lame','Accessori')
G(r'^lama ricambio|^lame','Lame')
G(r'^coppia conica','Coppie coniche')
G(r'^impugnatura|^manico impugnatura','Impugnature')
G(r'^cinghia tracolla|^zaino imbottito|^attacco per tracolla','Tracolle','Accessori')
G(r'^prolunga (?:decespugliatore|per)','Prolunghe','Accessori')
G(r'^gruppo (?:ingranaggi|meccanico)','Ingranaggi')
# Explicit manual review patches added below.
OVERRIDES={}
ISSUES={}
def override(spec,path,issue=None):
 for chunk in spec.split():
  x=list(map(int,chunk.split('-')))
  for n in range(x[0],x[-1]+1):
   OVERRIDES[n]=path
   if issue:ISSUES[n]=[issue]

# Individually reviewed exceptional products and compound components.
override('36 101 107 2446 3390-3392','', 'Voce di servizio o contributo: non è un prodotto da inserire nelle collection merceologiche.')
override('305-307 2687-2688 3739-3743','', 'Dati insufficienti: il titolo non identifica il componente venduto.')
override('346-347 533 644-645 787-790 1700 2162 2214-2216 2302 2429-2440 2445 2962 3117 3386 3524 3530 3723-3724','', 'Manca una categoria principale pertinente per abbigliamento, protezioni o materiale promozionale.')
override('374-375','', 'Contenitore senza destinazione dichiarata: verificare la categoria principale.')
override('215-216','Ricambi/Ricambi elettrico/Alimentatori','Alimentatore generico: verificare il settore prima di approvare.')
override('2201','Ricambi/Manubri e comandi/Leve','Leva senza funzione specificata: verificare prima di approvare.')
override('3080','Ricambi/Sospensioni/Rialzi','Rialzo senza destinazione specificata: verificare prima di approvare.')
override('19 1115','Ricambi/Ruote e mozzi/Supporti asse')
override('20 1701 2341 2351 2354 2558 2656-2657 3266 3400 3572','Ricambi/Ricambi motori/Frizioni')
override('26 52 1764','Ricambi/Accessori veicoli/Fissaggi carene')
override('44 991 1444-1446 2202 3355-3356','Ricambi/Attrezzatura/Utensili')
override('54','Ricambi/Freni/Supporti leva')
override('57 1132-1133 1194 1202-1204 1746 1753 1763 2640 3385 3464 3497-3499 3522 3531-3532','Ricambi/Accessori veicoli/Poggiapiedi')
override('70 1163 1201 3413 3471 3490-3496 3515','Ricambi/Ricambi motori/Supporti motore')
override('72 1520-1521 3467-3469 3482','Ricambi/Trasmissione/Supporti trasmissione')
override('95 2443-2444 3077','Ricambi/Lubrificanti/Olio motore')
override('108 1978','Ricambi/Ricambi elettrico/Batterie')
override('293 1228 1695 2638 2698 3552','Ricambi/Manubri e comandi/Componenti sterzo')
override('439','Ricambi/Ricambi motori/Motori completi')
override('475-477 1028 1079 1282 1745 3324 3414 3417 3520','Ricambi/Ricambi elettrico/Supporti batterie')
override('863','illuminazione/Ricambi/Cavi alimentazione')
override('864 866 870 1550 3313','Ricambi/Ricambi elettrico/Cablaggi')
override('990 1691 2639 3405','Ricambi/Accessori veicoli/Componenti bauletti')
override('1001 2708 2712','Ricambi/Ricambi motori/Carter')
override('1034 2357','Ricambi/Ricambi elettrico/Connettori')
override('1075 2134','Ricambi/Ricambi motori/Valvole')
override('1076-1077','illuminazione/Ricambi/Connettori LED')
override('1084','Ricambi/Ricambi elettrico/Convertitori tensione')
override('1085','Ricambi/Ricambi motori/Pompe acqua')
override('1088 1094 1236 3389','Ricambi/Ricambi elettrico/Componenti strumenti')
override('1107','Ricambi/Manubri e comandi/Tiranti sterzo')
override('1110-1112','Giardinaggio/Ricambi/Coppie coniche')
override('1113-1114','Ricambi/Sospensioni/Boccole')
override('1116 1284-1286 1293 2713 3543 3551 3591','Ricambi/Accessori veicoli/Carene')
override('1131 3193','Ricambi/Manubri e comandi/Manubri')
override('1164 1166-1167 2624 2641 3052-3054','Ricambi/Ruote e mozzi/Componenti ruota')
override('1165','Ricambi/Accessori veicoli/Fissaggi poggiapiedi')
override('1198','Ricambi/Manubri e comandi/Supporti manubrio')
override('1207-1208 1294','Ricambi/Trasmissione/Tendicatena')
override('1209-1214 3267-3268 3700 3705','Ricambi/Ricambi motori/Tubi raffreddamento')
override('1229 1362 3559','Ricambi/Ricambi motori/Accessori scarico')
override('1231-1232','Ricambi/Trasmissione/Protezioni trasmissione')
override('1233 3407 3409-3412','Ricambi/Manubri e comandi/Protezioni manubrio')
override('1235 3738','Ricambi/Ricambi motori/Ventole')
override('1283 1599-1600 2559 3078-3079 3119','Ricambi/Ricambi motori/Scatole filtro')
override('1299','Ricambi/Sospensioni/Protezioni ammortizzatori')
override('1441','Giardinaggio/Ricambi/Distanziali')
override('1484','Ricambi/Ricambi motori/Frizioni')
override('1516 1519','Ricambi/Ricambi elettrico/Fissaggi cablaggi')
override('1517-1518 1622-1625','Ricambi/Ricambi gomme/Accessori pneumatici')
override('1523 1571 2075 2646-2647 2686','Ricambi/Ricambi motori/Comandi cambio')
override('1549 2306','Ricambi/Accessori veicoli/Serrature sella')
override('1616 1618 2696 2699-2702 3059','Ricambi/Freni/Supporti freno')
override('1617 3419 3746','Ricambi/Ricambi motori/Fissaggi filtri')
override('1619-1621 2697 2703','Ricambi/Ruote e mozzi/Componenti ruota')
override('1626-1627','Ricambi/Ricambi elettrico/Relè')
override('1628 2038','Ricambi/Accessori veicoli/Protezioni termiche')
override('1687','Ricambi/Ricambi motori/Galleggianti serbatoio')
override('1692 1694 1696','Ricambi/Accessori veicoli/Ganci portaoggetti')
override('1693 2455 2457','Ricambi/Accessori veicoli/Supporti sella')
override('1702 3691','Giardinaggio/Ricambi/Pompe olio')
override('1704','Ricambi/Ricambi gomme/Pneumatici')
override('1766','Ricambi/Manubri e comandi/Cavi acceleratore')
override('1768 3041 3703-3704 3719 3722','Ricambi/Ricambi motori/Tubi olio')
override('1771','Ricambi/Ricambi motori/Campane frizione')
override('2034','Ricambi/Ricambi motori/Iniettori')
override('2077','Ricambi/Minuteria e fissaggi/Chiavette')
override('2081','Ricambi/Accessori veicoli/Cingoli')
override('2082 2352 3548 3750-3752','Ricambi/Ricambi motori/Distribuzione')
override('2083','Ricambi/Trasmissione/Pignoni')
override('2112','Ricambi/Manubri e comandi/Componenti sterzo')
override('2113','Ricambi/Ricambi motori/Tappi motore')
override('2114','Ricambi/Ricambi motori/Avviamenti')
override('2125','Ricambi/Accessori veicoli/Rotelle laterali')
override('2132','Giardinaggio/Ricambi/Tubi soffiatori')
override('2142','Ricambi/Segnalazione ottica/Lampadine')
override('2148-2156','illuminazione/Ricambi/Lampadine')
override('2159 3215','Ricambi/Ricambi elettrico/Spie')
override('2207','Ricambi/Manubri e comandi/Leve')
override('2209','Ricambi/Lubrificanti/Lubrificanti catena')
override('2210','Ricambi/Lubrificanti/Sbloccanti')
override('2219','Ricambi/Ricambi motori/Manicotti')
override('2220-2221 3040','Ricambi/Ricambi motori/Collettori')
override('2338','Ricambi/Ricambi motori/Avviamenti')
override('2339-2340 2342 3326','Ricambi/Accessori veicoli/Cavalletti')
override('2343 2345-2350','Ricambi/Freni/Molle freno')
override('2344 2353','Giardinaggio/Ricambi/Frizioni')
override('2355 3132 3462','Ricambi/Ricambi motori/Filtri olio')
override('2358 2454','Giardinaggio/Ricambi/Aste trasmissione')
override('2419-2420','Ricambi/Ricambi elettrico/Statori')
override('2442','Veicoli/Minicross')
override('2458','Ricambi/Manubri e comandi/Sterzo')
override('2463','Ricambi/Accessori veicoli/Parabrezza')
override('2560','Ricambi/Manubri e comandi/Manopole')
override('2625 2637 2644-2645 2648-2649 2653','Ricambi/Sospensioni/Perni sospensione')
override('2633 2642-2643','Ricambi/Ricambi motori/Avviamenti')
override('2636 2650-2652','Ricambi/Trasmissione/Movimenti centrali')
override('2654','Ricambi/Minuteria e fissaggi/Perni','Perno senza funzione dichiarata: verificare la destinazione prima di approvare.')
override('2770','Ricambi/Freni/Pompe freno')
override('2792 3466 3516','Ricambi/Accessori veicoli/Supporti bauletti')
override('2967 3302-3305 3315','Ricambi/Freni/Impianti frenanti')
override('3000','Ricambi/Ricambi elettrico/Programmatori')
override('3049-3050','Ricambi/Accessori veicoli/Antifurti')
override('3081','Ricambi/Trasmissione/Riduttori')
override('3099-3100','Ricambi/Ruote e mozzi/Ruote complete')
override('3114','Giardinaggio/Ricambi/Ruote')
override('3122','illuminazione/Ricambi/Scatole connessione')
override('3272','Ricambi/Ricambi motori/Fissaggi serbatoi')
override('3310-3312','Ricambi/Ricambi motori/Paraoli')
override('3319','Ricambi/Sospensioni/Paraoli forcelle')
override('3345','Ricambi/Trasmissione/Kit trasmissione')
override('3354 3585','Ricambi/Ricambi elettrico/Interruttori sicurezza')
override('3362','Giardinaggio/Soffiatori')
override('3384','Ricambi/Manubri e comandi/Leve frizione')
override('3415','Ricambi/Minuteria e fissaggi/Staffe','Staffa senza funzione dichiarata: verificare la destinazione prima di approvare.')
override('3416 3421 3489','Ricambi/Accessori veicoli/Parafanghi')
override('3418 3420','Ricambi/Ricambi elettrico/Supporti strumenti')
override('3461','Ricambi/Manubri e comandi/Acceleratori')
override('3463','Giardinaggio/Ricambi/Supporti motore')
override('3465','Ricambi/Ruote e mozzi/Supporti asse')
override('3470 3475-3476 3502 3510 3512 3514 3729','illuminazione/Ricambi/Portafaretti')
override('3473-3474 3485 3501','Ricambi/Segnalazione ottica/Supporti fanali')
override('3477-3481','Ricambi/Manubri e comandi/Cavi frizione')
override('3483','Ricambi/Sospensioni/Forcelle')
override('3484 3509','Ricambi/Accessori veicoli/Fissaggi carene')
override('3500','Giardinaggio/Ricambi/Parasassi')
override('3503-3504','illuminazione/Lampade/Plafoniere')
override('3511','Ricambi/Accessori veicoli/Maniglie')
override('3519','Ricambi/Sospensioni/Supporti ammortizzatori')
override('3521','Giardinaggio/Ricambi/Fissaggi serbatoi')
override('3527','Ricambi/Ruote e mozzi/Protezioni ruota')
override('3528-3529','Ricambi/Accessori veicoli/Tappetini')
override('3572','Ricambi/Ricambi motori/Frizioni')
override('3663','Ricambi/Freni/Pedali freno')
override('3698-3699 3706','Ricambi/Ricambi motori/Tubi aspirazione')
override('3701','Ricambi/Trasmissione/Tubi raffreddamento')
override('3721','Giardinaggio/Ricambi/Tubi olio')
override('3727','illuminazione/Accessori/Multiprese')
override('3753','Ricambi/Ricambi motori/Tappi motore')
override('3770','Ricambi/Manubri e comandi/Volanti')

# Review pass: compound titles, functional kits, and polluted source types.
override('100 2954','Ricambi/Ricambi gomme/Valvole pneumatici')
override('1377-1378','Ricambi/Freni/Dischi freno')
override('1485 1494-1502','illuminazione/Lampade/Proiettori')
override('891','illuminazione/Ricambi/Controller LED')
override('3210','illuminazione/Accessori/Sensori movimento')
override('2039-2041','Ricambi/Sospensioni/Paraoli forcelle')
override('2042','Ricambi/Ricambi motori/Cambi')
override('507','Ricambi/Ricambi motori/Leve cambio')
override('514','Ricambi/Ricambi motori/Frizioni')
override('534 1138-1141 2732 2736 2740 2751 3087-3091 3634-3635 3669-3672 3684-3685','Ricambi/Ricambi motori/Distribuzione')
override('800-803 819 2115','Ricambi/Ricambi motori/Avviamenti')
override('3280-3284','Ricambi/Ricambi motori/Distribuzione')
override('621','Ricambi/Ricambi elettrico/Campanelli')
override('1030-1032','Ricambi/Ricambi elettrico/Clacson')
override('464 472','Ricambi/Manubri e comandi/Boccole sterzo')
override('466-467','Ricambi/Ricambi motori/Frizioni')
override('468-469','Ricambi/Ricambi motori/Avviamenti')
override('471 1106 3397','Ricambi/Ruote e mozzi/Distanziali ruota')
override('42-43 85-86 474 1105','Ricambi/Ricambi motori/Boccole motore')
override('3394','Ricambi/Ricambi motori/Ruote libere')
override('3396','Ricambi/Manubri e comandi/Componenti sterzo')
override('3393','Ricambi/Accessori veicoli/Portapacchi')
override('2549-2550','Ricambi/Ruote e mozzi/Paraoli ruota')
override('1570','Ricambi/Ricambi elettrico/Cablaggi')
override('1563','Ricambi/Accessori veicoli/Serrature sella')
override('1746','Ricambi/Freni/Pedali freno')
override('2598','Ricambi/Ricambi motori/Avviamenti')
override('2609-2610','Ricambi/Ricambi motori/Leve cambio')
override('3063','Ricambi/Ricambi motori/Frizioni')
override('2963','Giardinaggio/Motopompe')
override('2964','Ricambi/Ricambi motori/Pompe carburante')
override('3288','Ricambi/Ricambi motori/Pompe acqua')
override('2213','Ricambi/Segnalazione ottica/Fanali')
override('1285','Ricambi/Ricambi motori/Ventole')
override('1875','Ricambi/Minuteria e fissaggi/Guarnizioni')
override('2851 2860-2861 2876','Ricambi/Ricambi motori/Scatole filtro')
override('2852-2853 2944-2946','Ricambi/Segnalazione ottica/Supporti fanali')
override('2842 2939','Ricambi/Ricambi elettrico/Supporti batterie')
override('2889 2942-2943','Ricambi/Ricambi elettrico/Componenti strumenti')
override('1183 2919','Ricambi/Sospensioni/Protezioni forcelle')
override('2488-2489','Ricambi/Manubri e comandi/Protezioni manubrio')
override('2957 2961','Ricambi/Accessori veicoli/Schienali')
override('2869','Ricambi/Accessori veicoli/Carene')
override('2849','Giardinaggio/Ricambi/Protezioni aspirazione')
override('772-773','Giardinaggio/Ricambi/Parasassi')
override('763','Giardinaggio/Accessori/Copribarre')
override('620','Ricambi/Trasmissione/Riduttori')
override('1995','Ricambi/Accessori veicoli/Antifurti')
override('2363-2364 2366-2371 2374-2379 3004','Ricambi/Ricambi elettrico/Motori elettrici')

override('2609','Ricambi/Freni/Pedali freno')
override('3289','Ricambi/Ricambi motori/Valvole')
override('2452-2453','illuminazione/Lampade/Lampioncini')
override('3005 3010','Ricambi/Sospensioni/Protezioni ammortizzatori')
override('3006','Ricambi/Freni/Pedali freno')
override('3008','Ricambi/Freni/Protezioni freno')
override('3009 3012','Ricambi/Accessori veicoli/Poggiapiedi')
override('3007 3011 3013-3014','Ricambi/Trasmissione/Protezioni trasmissione')
override('2075','Ricambi/Ricambi motori/Leve cambio')
override('1571','Ricambi/Ricambi elettrico/Sensori')
override('483-492 515-520','Ricambi/Manubri e comandi/Tiranti sterzo')
override('3358 3360','Ricambi/Manubri e comandi/Snodi sterzo')

GARDEN=re.compile(r'\b(?:decespugliator[ei]|motoseg[ah]e?|motocoltivator[ei]|motozapp[ae]|tagliasiep[ei]|tagliaerb[ae]|potator[ei]|soffiator[ei]|mototrivell[ae])\b')
VEHICLE=re.compile(r'\b(?:pit\s?bike|quad|atv|miniquad|miniatv|minicross|minimoto|scooter|monopattino|cross|bicicletta|bici)\b')
def classify(n,row):
 title=norm(row['title']);t=principal(row['title'])
 path=OVERRIDES.get(n)
 if n not in OVERRIDES:
  for regex,chosen in RULES:
   if regex.search(t):path=chosen;break
 if path is None:return None
 if not path:return []
 levels=path.split('/')
 if levels[-1]=='Pedaline e poggiapiedi':levels[-1]='Poggiapiedi'
 if levels==['Ricambi','Ruote e mozzi','Componenti ruota']:
  if re.search(r'\braggi?o?\b|\braggi\b',t):levels[-1]='Raggi'
  elif re.search(r'spessor|distanzial',t):levels[-1]='Distanziali ruota'
  elif re.search(r'copri.*(?:mozzo|bullone)',t):levels[-1]='Coprimozzi'
  elif re.search(r'copri asse|mezzelune',t):levels[-1]='Supporti asse'
  elif re.search(r'flangia|piastra',t):levels[-1]='Flange ruota'
  elif re.search(r'pern',t):levels[-1]='Perni ruota'
 if levels==['Ricambi','Minuteria e fissaggi','Viteria']:
  head=t.split()[0]
  for prefix,leaf in [('rondell','Rondelle'),('prigion','Prigionieri'),('chiav','Chiavette'),('coppigl','Coppiglie'),('copigl','Coppiglie'),('senger','Anelli elastici'),('spina','Spine'),('dado','Dadi'),('dadi','Dadi'),('bullon','Bulloni'),('vit','Viti')]:
   if head.startswith(prefix):levels[-1]=leaf;break
 if levels==['Ricambi','Ricambi motori','Distribuzione']:
  if re.match(r'catena|catene|catena olio',t):levels[-1]='Catene distribuzione'
  elif re.match(r'pignone|puleggia',t):levels[-1]='Pignoni distribuzione'
  elif re.match(r'pattin|rotell',t):levels[-1]='Pattini distribuzione'
  elif re.match(r'tendi|tirante|braccio|molla',t):levels[-1]='Tendicatena'

 if n not in OVERRIDES:
  if len(levels)==3 and levels[1]=='Minuteria e fissaggi' and levels[-1]=='Viteria' and re.match(r'chiavell|chiavett',t):levels[-1]='Chiavette'
  if levels[:2]==['Ricambi','Trasmissione'] and re.search(r'distribuzione|distribuz|catena olio',title):levels=['Ricambi','Ricambi motori','Distribuzione']
  if levels==['Ricambi','Ricambi motori','Paraoli'] and not re.search(r'motor|carter|albero|frizion|valvol|avviamento|cilindr',title):levels=['Ricambi','Minuteria e fissaggi','Paraoli']
  if levels==['Ricambi','Minuteria e fissaggi','Cuscinetti']:
   if re.search(r'sterzo|piastre',title):levels=['Ricambi','Manubri e comandi','Cuscinetti sterzo']
   elif re.search(r'motor|albero|piston|campana|carter',title):levels=['Ricambi','Ricambi motori','Cuscinetti']
   elif re.search(r'ruot|mozzo|asse posteriore',title):levels=['Ricambi','Ruote e mozzi','Cuscinetti ruota']

 if levels[:2]==['Ricambi','Minuteria e fissaggi'] and levels[-1]=='Cuscinetti':levels=['Ricambi','Cuscinetti']
 # Prioritize clear sector in title, not polluted legacy product_type.
 if levels[0]=='Ricambi' and GARDEN.search(title) and not VEHICLE.search(title):
  levels=['Giardinaggio','Ricambi',levels[-1]]
 return levels

def prepare():
 plan=[]
 for n,row in enumerate(rows,1):
  path=classify(n,row)
  plan.append({'index':n,'id':row['id'],'title':row['title'],'path':path,'issues':ISSUES.get(n,[])})
 PLAN.write_text(json.dumps(plan,ensure_ascii=False,indent=2))
 groups=collections.Counter(' > '.join(r['path']) if r['path'] else 'UNRESOLVED' if r['path'] is None else 'REVIEW' for r in plan)
 print(json.dumps(groups,ensure_ascii=False,indent=2))
 for r in plan:
  if r['path'] is None:print(r['index'],r['title'])
 return plan
def save():
    plan=json.loads(PLAN.read_text())
    assert len(plan)==len(rows)==3771
    assert [p['id'] for p in plan]==[r['id'] for r in rows]
    assert all(p['path'] is not None for p in plan), 'Classificazione incompleta'
    assert all(p['path'] or p['issues'] for p in plan), 'Caso senza categoria e senza spiegazione'
    settings=app.get_settings()
    version=app.ensure_prompt_version()
    digest=app.stable_hash(plan)
    batch_id='codex-all-20261016-'+digest[:12]
    report=app.DATA/(batch_id+'.json')
    backup=app.DATA/(batch_id+'-before.sqlite')
    if report.exists():
        existing=json.loads(report.read_text())
        with app.connect() as db:
            assert all(db.execute('SELECT 1 FROM generations WHERE id=?',(r['generation_id'],)).fetchone() for r in existing['results'])
        print(app.dumps({'already_saved':True,'processed':len(existing['results']),'report':str(report)}))
        return
    with app.connect() as db:
        if not backup.exists():
            target=sqlite3.connect(backup)
            try:db.backup(target)
            finally:target.close()
    results=[]
    with app.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        assert not db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','processing') LIMIT 1").fetchone(), 'Generazione concorrente: ripetere al termine della coda'
        live=db.execute('SELECT * FROM products ORDER BY title COLLATE NOCASE,id').fetchall()
        assert [r['id'] for r in live]==[r['id'] for r in rows], 'Catalogo cambiato: ripreparare il piano'
        assert all(a['content_hash']==b['content_hash'] for a,b in zip(live,rows)), 'Prodotti aggiornati durante la revisione'
        before_approved=[tuple(r) for r in db.execute("SELECT id,status,approved_paths_json,approved_at,published_at FROM generations WHERE approved_at IS NOT NULL OR status IN ('approved','published') ORDER BY id")]
        categories,_=app._catalog_context(db,live[0]['id'],live[0]['title'],version)
        timestamp=app.now()
        for row,entry in zip(live,plan):
            path=entry['path']
            assert len(path)<=min(settings.max_depth,3), entry
            assert not path or path_key([path[0]]) in {path_key([c]) for c in settings.main_categories},entry
            assert not (len(path)>1 and path_key([path[0]])=='ricambi' and compatibility_branch(path[1])),entry
            raw={'percorsi':[{'livelli':path,'confidence':.65 if entry['issues'] else .95,'motivazione':''}] if path else [],'nuove_categorie':[],'criticita':entry['issues']}
            app.AIProposal.model_validate(raw)
            output=app.enforce_main_categories(reuse_canonical_categories(raw,categories,settings.max_depth),settings.main_categories)
            app.AIProposal.model_validate(output)
            assert bool(output['percorsi'])==bool(path),entry
            if path:
                # No hidden move to legacy compatibility branches during canonicalization.
                actual=output['percorsi'][0]['livelli']
                assert not (len(actual)>1 and path_key([actual[0]])=='ricambi' and compatibility_branch(actual[1])),actual
            snapshot=db.execute('SELECT snapshot_hash FROM product_snapshots WHERE product_id=? ORDER BY created_at DESC LIMIT 1',(row['id'],)).fetchone()
            assert snapshot,'Snapshot mancante: '+row['id']
            generation_id=str(uuid.uuid5(uuid.NAMESPACE_URL,batch_id+':'+row['id']))
            # Previous approvals/publications remain available for review and publishing.
            db.execute("UPDATE generations SET status='superseded' WHERE product_id=? AND status='generated'",(row['id'],))
            db.execute("INSERT INTO generations(id,product_id,prompt_version,model,output_json,status,source_hash,created_at) VALUES(?,?,?,?,?,'generated',?,?)",(generation_id,row['id'],version,'Codex',app.dumps(output),snapshot['snapshot_hash'],timestamp))
            db.execute("UPDATE products SET processing_status='completed',last_error=NULL WHERE id=?",(row['id'],))
            db.execute('INSERT INTO history VALUES(?,?,?,?,?,?,?,?,?,?)',(str(uuid.uuid5(uuid.NAMESPACE_URL,batch_id+':history:'+row['id'])),row['id'],'AI',app.dumps(app.product_paths(db,row['id'])),app.dumps([p['livelli'] for p in output['percorsi']]),'Codex',version,'proposed','Revisione catalogo Codex: famiglie funzionali e componenti, da approvare. Batch '+batch_id,timestamp))
            results.append({'id':row['id'],'title':row['title'],'generation_id':generation_id,'paths':[p['livelli'] for p in output['percorsi']],'issues':output['criticita']})
        after_approved=[tuple(r) for r in db.execute("SELECT id,status,approved_paths_json,approved_at,published_at FROM generations WHERE approved_at IS NOT NULL OR status IN ('approved','published') ORDER BY id")]
        assert before_approved==after_approved,'Approvazioni o pubblicazioni alterate'
        for result in results:
            saved=db.execute('SELECT status,approved_at,published_at,output_json FROM generations WHERE id=?',(result['generation_id'],)).fetchone()
            assert saved['status']=='generated' and not saved['approved_at'] and not saved['published_at']
            assert all(not p.get('motivazione') for p in app.loads(saved['output_json'])['percorsi'])
        # Store the audit report before committing; remove it if commit fails.
        summary={'batch_id':batch_id,'created_at':timestamp,'prompt_version':version,'processed':len(results),'with_categories':sum(bool(r['paths']) for r in results),'three_levels':sum(bool(r['paths']) and len(r['paths'][0])==3 for r in results),'two_levels':sum(bool(r['paths']) and len(r['paths'][0])==2 for r in results),'without_categories':sum(not r['paths'] for r in results),'with_warnings':sum(bool(r['issues']) for r in results),'approvals_preserved':len(before_approved),'approved':0,'published':0,'backup':str(backup),'results':results}
        report.write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(app.dumps({k:v for k,v in summary.items() if k!='results'}))
    print('report',report)


if __name__=='__main__':
    if '--save' in sys.argv:save()
    else:prepare()

