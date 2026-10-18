"""French department data and extraction utilities.

All department data is embedded — no external API or network access needed.
Covers all 96 metropolitan departments (01–95, 2A, 2B) and the 5 overseas
departments/regions (971–976).

Extraction priority
-------------------
1. Explicit "(83)" pattern in the location string.
2. Postal code in the location string (83100 → "83").
3. Major French city name lookup (Toulon → "83").
4. Returns None if the department cannot be determined.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Department:
    code: str   # e.g. "83", "2A", "971"
    name: str   # e.g. "Var", "Corse-du-Sud", "Guadeloupe"


# ---------------------------------------------------------------------------
# Static department table
# ---------------------------------------------------------------------------

_DEPARTMENTS: list[Department] = [
    Department("01", "Ain"),
    Department("02", "Aisne"),
    Department("03", "Allier"),
    Department("04", "Alpes-de-Haute-Provence"),
    Department("05", "Hautes-Alpes"),
    Department("06", "Alpes-Maritimes"),
    Department("07", "Ardèche"),
    Department("08", "Ardennes"),
    Department("09", "Ariège"),
    Department("10", "Aube"),
    Department("11", "Aude"),
    Department("12", "Aveyron"),
    Department("13", "Bouches-du-Rhône"),
    Department("14", "Calvados"),
    Department("15", "Cantal"),
    Department("16", "Charente"),
    Department("17", "Charente-Maritime"),
    Department("18", "Cher"),
    Department("19", "Corrèze"),
    Department("2A", "Corse-du-Sud"),
    Department("2B", "Haute-Corse"),
    Department("21", "Côte-d'Or"),
    Department("22", "Côtes-d'Armor"),
    Department("23", "Creuse"),
    Department("24", "Dordogne"),
    Department("25", "Doubs"),
    Department("26", "Drôme"),
    Department("27", "Eure"),
    Department("28", "Eure-et-Loir"),
    Department("29", "Finistère"),
    Department("30", "Gard"),
    Department("31", "Haute-Garonne"),
    Department("32", "Gers"),
    Department("33", "Gironde"),
    Department("34", "Hérault"),
    Department("35", "Ille-et-Vilaine"),
    Department("36", "Indre"),
    Department("37", "Indre-et-Loire"),
    Department("38", "Isère"),
    Department("39", "Jura"),
    Department("40", "Landes"),
    Department("41", "Loir-et-Cher"),
    Department("42", "Loire"),
    Department("43", "Haute-Loire"),
    Department("44", "Loire-Atlantique"),
    Department("45", "Loiret"),
    Department("46", "Lot"),
    Department("47", "Lot-et-Garonne"),
    Department("48", "Lozère"),
    Department("49", "Maine-et-Loire"),
    Department("50", "Manche"),
    Department("51", "Marne"),
    Department("52", "Haute-Marne"),
    Department("53", "Mayenne"),
    Department("54", "Meurthe-et-Moselle"),
    Department("55", "Meuse"),
    Department("56", "Morbihan"),
    Department("57", "Moselle"),
    Department("58", "Nièvre"),
    Department("59", "Nord"),
    Department("60", "Oise"),
    Department("61", "Orne"),
    Department("62", "Pas-de-Calais"),
    Department("63", "Puy-de-Dôme"),
    Department("64", "Pyrénées-Atlantiques"),
    Department("65", "Hautes-Pyrénées"),
    Department("66", "Pyrénées-Orientales"),
    Department("67", "Bas-Rhin"),
    Department("68", "Haut-Rhin"),
    Department("69", "Rhône"),
    Department("70", "Haute-Saône"),
    Department("71", "Saône-et-Loire"),
    Department("72", "Sarthe"),
    Department("73", "Savoie"),
    Department("74", "Haute-Savoie"),
    Department("75", "Paris"),
    Department("76", "Seine-Maritime"),
    Department("77", "Seine-et-Marne"),
    Department("78", "Yvelines"),
    Department("79", "Deux-Sèvres"),
    Department("80", "Somme"),
    Department("81", "Tarn"),
    Department("82", "Tarn-et-Garonne"),
    Department("83", "Var"),
    Department("84", "Vaucluse"),
    Department("85", "Vendée"),
    Department("86", "Vienne"),
    Department("87", "Haute-Vienne"),
    Department("88", "Vosges"),
    Department("89", "Yonne"),
    Department("90", "Territoire de Belfort"),
    Department("91", "Essonne"),
    Department("92", "Hauts-de-Seine"),
    Department("93", "Seine-Saint-Denis"),
    Department("94", "Val-de-Marne"),
    Department("95", "Val-d'Oise"),
    # Overseas departments and regions
    Department("971", "Guadeloupe"),
    Department("972", "Martinique"),
    Department("973", "Guyane"),
    Department("974", "La Réunion"),
    Department("976", "Mayotte"),
]

# Code → Department lookup
_BY_CODE: dict[str, Department] = {d.code: d for d in _DEPARTMENTS}

# Normalized name → Department lookup (accent-insensitive)
_BY_NAME_NORM: dict[str, Department] = {}



def _normalize_name(s: str) -> str:
    """Lowercase + strip diacritics + remove hyphens for fuzzy matching."""
    import unicodedata
    nfd = unicodedata.normalize("NFD", s.lower())
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return stripped.replace("-", " ").replace("'", " ").replace("’", " ")


for _d in _DEPARTMENTS:
    _BY_NAME_NORM[_normalize_name(_d.name)] = _d


# ---------------------------------------------------------------------------
# Commune → department code mapping
# Covers all prefectures, sub-prefectures, and major communes likely to
# appear in job location strings.  Keys are pre-normalized (lowercase,
# diacritics stripped, hyphens/apostrophes → space) so the lookup works
# without normalizing again at call time.
# ---------------------------------------------------------------------------

_CITY_TO_DEPT: dict[str, str] = {
    # ---- 01 Ain ----
    "bourg en bresse": "01",
    "oyonnax": "01",
    "ambieu en bugey": "01",
    "amberieu en bugey": "01",
    "bellegarde sur valserine": "01",
    "saint genis pouilly": "01",
    "ferney voltaire": "01",
    "gex": "01",
    "trevoux": "01",
    "meximieux": "01",
    "montluel": "01",
    "miribel": "01",
    "saint vulbas": "01",
    # ---- 02 Aisne ----
    "laon": "02",
    "saint quentin": "02",
    "soissons": "02",
    "chateau thierry": "02",
    "hirson": "02",
    "tergnier": "02",
    # ---- 03 Allier ----
    "moulins": "03",
    "vichy": "03",
    "montlucon": "03",
    "cusset": "03",
    "yzeure": "03",
    # ---- 04 Alpes-de-Haute-Provence ----
    "digne les bains": "04",
    "manosque": "04",
    "apt": "84",
    "sisteron": "04",
    "forcalquier": "04",
    # ---- 05 Hautes-Alpes ----
    "gap": "05",
    "briancon": "05",
    "embrun": "05",
    # ---- 06 Alpes-Maritimes ----
    "nice": "06",
    "antibes": "06",
    "cannes": "06",
    "menton": "06",
    "grasse": "06",
    "cagnes sur mer": "06",
    "saint laurent du var": "06",
    "vence": "06",
    "mougins": "06",
    "vallauris": "06",
    "mandelieu la napoule": "06",
    "villeneuve loubet": "06",
    "biot": "06",
    "valbonne": "06",
    "roquebrune cap martin": "06",
    "beausoleil": "06",
    "la trinite": "06",
    "peymeinade": "06",
    "pegomas": "06",
    "golfe juan": "06",
    "juan les pins": "06",
    "sophia antipolis": "06",
    "carros": "06",
    "le cannet": "06",
    "le bar sur loup": "06",
    "tourrettes sur loup": "06",
    "auribeau sur siagne": "06",
    "speracedes": "06",
    "cabris": "06",
    # ---- 07 Ardèche ----
    "privas": "07",
    "aubenas": "07",
    "annonay": "07",
    "guilherand granges": "07",
    "tournon sur rhone": "07",
    "montelimar": "26",  # note: Montélimar is in 26 Drôme
    # ---- 08 Ardennes ----
    "charleville mezieres": "08",
    "sedan": "08",
    "revin": "08",
    # ---- 09 Ariège ----
    "foix": "09",
    "pamiers": "09",
    "saint girons": "09",
    # ---- 10 Aube ----
    "troyes": "10",
    "romilly sur seine": "10",
    "bar sur aube": "10",
    # ---- 11 Aude ----
    "carcassonne": "11",
    "narbonne": "11",
    "castelnaudary": "11",
    "limoux": "11",
    # ---- 12 Aveyron ----
    "rodez": "12",
    "millau": "12",
    "villefranche de rouergue": "12",
    "decazeville": "12",
    # ---- 13 Bouches-du-Rhône ----
    "marseille": "13",
    "aix en provence": "13",
    "arles": "13",
    "martigues": "13",
    "aubagne": "13",
    "salon de provence": "13",
    "istres": "13",
    "vitrolles": "13",
    "miramas": "13",
    "la ciotat": "13",
    "cassis": "13",
    "port de bouc": "13",
    "fos sur mer": "13",
    "gardanne": "13",
    "trets": "13",
    "rognac": "13",
    "bouc bel air": "13",
    "septemes les vallons": "13",
    "plan de cuques": "13",
    "allauch": "13",
    "carnoux en provence": "13",
    "gemenos": "13",
    "saint mitre les remparts": "13",
    "chateauneuf les martigues": "13",
    "marignane": "13",
    "gignac la nerthe": "13",
    "saint victoret": "13",
    "les pennes mirabeau": "13",
    "ensues la redonne": "13",
    "carry le rouet": "13",
    "sausset les pins": "13",
    "berre l etang": "13",
    "lancon provence": "13",
    "pelissanne": "13",
    "eyguieres": "13",
    "chateauneuf le rouge": "13",
    "roquevaire": "13",
    # ---- 14 Calvados ----
    "caen": "14",
    "herouville saint clair": "14",
    "bayeux": "14",
    "lisieux": "14",
    "vire": "14",
    "mondeville": "14",
    "colombelles": "14",
    "ouistreham": "14",
    # ---- 15 Cantal ----
    "aurillac": "15",
    "saint flour": "15",
    # ---- 16 Charente ----
    "angouleme": "16",
    "soyaux": "16",
    "l isle d espagnac": "16",
    "cognac": "16",
    "la couronne": "16",
    # ---- 17 Charente-Maritime ----
    "la rochelle": "17",
    "saintes": "17",
    "rochefort": "17",
    "royan": "17",
    "chatelaillon plage": "17",
    "perigny": "17",
    # ---- 18 Cher ----
    "bourges": "18",
    "vierzon": "18",
    "saint amand montrond": "18",
    # ---- 19 Corrèze ----
    "tulle": "19",
    "brive la gaillarde": "19",
    "ussel": "19",
    # ---- 2A Corse-du-Sud ----
    "ajaccio": "2A",
    "porto vecchio": "2A",
    "propriano": "2A",
    # ---- 2B Haute-Corse ----
    "bastia": "2B",
    "corte": "2B",
    "calvi": "2B",
    # ---- 21 Côte-d'Or ----
    "dijon": "21",
    "chenove": "21",
    "quetigny": "21",
    "longvic": "21",
    "fontaine les dijon": "21",
    "talant": "21",
    "beaune": "21",
    "montbard": "21",
    # ---- 22 Côtes-d'Armor ----
    "saint brieuc": "22",
    "lannion": "22",
    "guingamp": "22",
    "lamballe": "22",
    "ploufragan": "22",
    "plerin": "22",
    "loudeac": "22",
    # ---- 23 Creuse ----
    "gueret": "23",
    "la souterraine": "23",
    # ---- 24 Dordogne ----
    "perigueux": "24",
    "bergerac": "24",
    "sarlat la caneda": "24",
    # ---- 25 Doubs ----
    "besancon": "25",
    "montbeliard": "25",
    "sochaux": "25",
    "valentigney": "25",
    "grand charmont": "25",
    "audincourt": "25",
    # ---- 26 Drôme ----
    "valence": "26",
    "romans sur isere": "26",
    "pierrelatte": "26",
    "bourg les valence": "26",
    "guilherand": "26",
    "crest": "26",
    # ---- 27 Eure ----
    "evreux": "27",
    "vernon": "27",
    "louviers": "27",
    "val de reuil": "27",
    "bernay": "27",
    "pont audemer": "27",
    # ---- 28 Eure-et-Loir ----
    "chartres": "28",
    "dreux": "28",
    "chateaudun": "28",
    "nogent le rotrou": "28",
    # ---- 29 Finistère ----
    "brest": "29",
    "quimper": "29",
    "concarneau": "29",
    "quimperle": "29",
    "landerneau": "29",
    "douarnenez": "29",
    "chateaulin": "29",
    "morlaix": "29",
    "plougastel daoulas": "29",
    "guipavas": "29",
    "plouzane": "29",
    # ---- 30 Gard ----
    "nimes": "30",
    "ales": "30",
    "bagnols sur ceze": "30",
    "vauvert": "30",
    "saint gilles": "30",
    "beaucaire": "30",
    "le vigan": "30",
    "pont saint esprit": "30",
    # ---- 31 Haute-Garonne ----
    "toulouse": "31",
    "blagnac": "31",
    "colomiers": "31",
    "muret": "31",
    "ramonville saint agne": "31",
    "tournefeuille": "31",
    "cugnaux": "31",
    "balma": "31",
    "l union": "31",
    "castanet tolosan": "31",
    "portet sur garonne": "31",
    "labege": "31",
    "aucamville": "31",
    "fenouillet": "31",
    "saint orens de gameville": "31",
    "quint fonsegrives": "31",
    "flourens": "31",
    "escalquens": "31",
    "lacroix falgarde": "31",
    "fonsorbes": "31",
    "leguevin": "31",
    "plaisance du touch": "31",
    "saint jean": "31",
    # ---- 32 Gers ----
    "auch": "32",
    "condom": "32",
    # ---- 33 Gironde ----
    "bordeaux": "33",
    "merignac": "33",
    "pessac": "33",
    "talence": "33",
    "gradignan": "33",
    "begles": "33",
    "villenave d ornon": "33",
    "lormont": "33",
    "le bouscat": "33",
    "bruges": "33",
    "eysines": "33",
    "saint medard en jalles": "33",
    "libourne": "33",
    "arcachon": "33",
    "la teste de buch": "33",
    "gujan mestras": "33",
    "cenon": "33",
    "artigues pres bordeaux": "33",
    "floirac": "33",
    "carbon blanc": "33",
    "sainte eulalie": "33",
    "ambares et lagrave": "33",
    "ambes": "33",
    "blanquefort": "33",
    "le taillan medoc": "33",
    "saint aubin de medoc": "33",
    # ---- 34 Hérault ----
    "montpellier": "34",
    "beziers": "34",
    "sete": "34",
    "frontignan": "34",
    "lunel": "34",
    "agde": "34",
    "mauguio": "34",
    "castelnau le lez": "34",
    "lattes": "34",
    "perols": "34",
    "baillargues": "34",
    "saint jean de vedas": "34",
    "clermont l herault": "34",
    "lodeve": "34",
    "gigean": "34",
    # ---- 35 Ille-et-Vilaine ----
    "rennes": "35",
    "saint malo": "35",
    "fougeres": "35",
    "vitre": "35",
    "cesson sevigne": "35",
    "bruz": "35",
    "betton": "35",
    "chantepie": "35",
    "pace": "35",
    "vezin le coquet": "35",
    "orgeres": "35",
    "mordelles": "35",
    # ---- 36 Indre ----
    "chateauroux": "36",
    "issoudun": "36",
    "argenton sur creuse": "36",
    # ---- 37 Indre-et-Loire ----
    "tours": "37",
    "joue les tours": "37",
    "saint pierre des corps": "37",
    "saint avertin": "37",
    "la riche": "37",
    "amboise": "37",
    "chinon": "37",
    # ---- 38 Isère ----
    "grenoble": "38",
    "echirolles": "38",
    "saint martin d heres": "38",
    "bourgoin jallieu": "38",
    "vienne": "38",
    "meylan": "38",
    "fontaine": "38",
    "claix": "38",
    "crolles": "38",
    "villard bonnot": "38",
    "seyssinet pariset": "38",
    "voreppe": "38",
    "la tour du pin": "38",
    "pont de claix": "38",
    "sassenage": "38",
    "voiron": "38",
    "gieres": "38",
    "saint egreve": "38",
    "corenc": "38",
    "saint ismier": "38",
    "bernin": "38",
    "montbonnot saint martin": "38",
    "domene": "38",
    "la mure": "38",
    # ---- 39 Jura ----
    "lons le saunier": "39",
    "dole": "39",
    "saint claude": "39",
    # ---- 40 Landes ----
    "mont de marsan": "40",
    "dax": "40",
    "saint paul les dax": "40",
    "biscarrosse": "40",
    "tarnos": "40",
    # ---- 41 Loir-et-Cher ----
    "blois": "41",
    "vendome": "41",
    "romorantin lanthenay": "41",
    "vineuil": "41",
    # ---- 42 Loire ----
    "saint etienne": "42",
    "roanne": "42",
    "firminy": "42",
    "andrezeieux boutheon": "42",
    "saint chamond": "42",
    "rive de gier": "42",
    "le chambon feugerolles": "42",
    "roche la moliere": "42",
    "sorbiers": "42",
    "la ricamarie": "42",
    # ---- 43 Haute-Loire ----
    "le puy en velay": "43",
    "brioude": "43",
    "yssingeaux": "43",
    # ---- 44 Loire-Atlantique ----
    "nantes": "44",
    "saint nazaire": "44",
    "saint herblain": "44",
    "reze": "44",
    "orvault": "44",
    "carquefou": "44",
    "vertou": "44",
    "la chapelle sur erdre": "44",
    "bouguenais": "44",
    "coueron": "44",
    "sainte luce sur loire": "44",
    "trignac": "44",
    "saint sebastien sur loire": "44",
    "la montagne": "44",
    "indre": "44",
    "bouaye": "44",
    "pont saint martin": "44",
    # ---- 45 Loiret ----
    "orleans": "45",
    "montargis": "45",
    "saint jean de braye": "45",
    "olivet": "45",
    "saint jean de la ruelle": "45",
    "fleury les aubrais": "45",
    "saran": "45",
    "gien": "45",
    "pithiviers": "45",
    # ---- 46 Lot ----
    "cahors": "46",
    "figeac": "46",
    # ---- 47 Lot-et-Garonne ----
    "agen": "47",
    "marmande": "47",
    "villeneuve sur lot": "47",
    "tonneins": "47",
    # ---- 48 Lozère ----
    "mende": "48",
    "marvejols": "48",
    # ---- 49 Maine-et-Loire ----
    "angers": "49",
    "cholet": "49",
    "saumur": "49",
    "trelaze": "49",
    "avrille": "49",
    "les ponts de ce": "49",
    "saint barthelemy d anjou": "49",
    # ---- 50 Manche ----
    "cherbourg": "50",
    "saint lo": "50",
    "avranches": "50",
    "granville": "50",
    "equeurdreville hainneville": "50",
    "tourlaville": "50",
    # ---- 51 Marne ----
    "reims": "51",
    "epernay": "51",
    "chalons en champagne": "51",
    "vitry le francois": "51",
    # ---- 52 Haute-Marne ----
    "chaumont": "52",
    "saint dizier": "52",
    # ---- 53 Mayenne ----
    "laval": "53",
    "change": "53",
    "saint berthevin": "53",
    "mayenne": "53",
    # ---- 54 Meurthe-et-Moselle ----
    "nancy": "54",
    "vandoeuvre les nancy": "54",
    "luneville": "54",
    "maxeville": "54",
    "laxou": "54",
    "tomblaine": "54",
    "essey les nancy": "54",
    "pont a mousson": "54",
    "toul": "54",
    # ---- 55 Meuse ----
    "verdun": "55",
    "bar le duc": "55",
    # ---- 56 Morbihan ----
    "lorient": "56",
    "vannes": "56",
    "pontivy": "56",
    "auray": "56",
    "lanester": "56",
    "ploemeur": "56",
    # ---- 57 Moselle ----
    "metz": "57",
    "thionville": "57",
    "forbach": "57",
    "sarreguemines": "57",
    "sarrebourg": "57",
    "creutzwald": "57",
    "saint avold": "57",
    "fameck": "57",
    "florange": "57",
    "hayange": "57",
    "woippy": "57",
    "montigny les metz": "57",
    "talange": "57",
    "amneville": "57",
    "yutz": "57",
    "algrange": "57",
    # ---- 58 Nièvre ----
    "nevers": "58",
    "cosne cours sur loire": "58",
    # ---- 59 Nord ----
    "lille": "59",
    "roubaix": "59",
    "tourcoing": "59",
    "villeneuve d ascq": "59",
    "dunkerque": "59",
    "valenciennes": "59",
    "douai": "59",
    "maubeuge": "59",
    "cambrai": "59",
    "denain": "59",
    "armentieres": "59",
    "lomme": "59",
    "faches thumesnil": "59",
    "lambersart": "59",
    "hellemmes lille": "59",
    "lezennes": "59",
    "marcq en baroeul": "59",
    "wattrelos": "59",
    "mouvaux": "59",
    "la madeleine": "59",
    "saint andre lez lille": "59",
    "mons en baroeul": "59",
    "wambrechies": "59",
    "comines": "59",
    "bailleul": "59",
    "hazebrouck": "59",
    "gravelines": "59",
    "grande synthe": "59",
    "coudekerque branche": "59",
    "wattignies": "59",
    "lesquin": "59",
    "fretin": "59",
    "wasquehal": "59",
    "croix": "59",
    "hem": "59",
    "loos": "59",
    "seclin": "59",
    # ---- 60 Oise ----
    "beauvais": "60",
    "creil": "60",
    "compiegne": "60",
    "nogent sur oise": "60",
    "montataire": "60",
    "meru": "60",
    "senlis": "60",
    "chantilly": "60",
    # ---- 61 Orne ----
    "alencon": "61",
    "flers": "61",
    "argentan": "61",
    # ---- 62 Pas-de-Calais ----
    "arras": "62",
    "boulogne sur mer": "62",
    "calais": "62",
    "lens": "62",
    "lievin": "62",
    "bethune": "62",
    "bruay la buissiere": "62",
    "henin beaumont": "62",
    "carvin": "62",
    "noeux les mines": "62",
    "saint martin boulogne": "62",
    "longuenesse": "62",
    "auchel": "62",
    "saint omer": "62",
    "marck": "62",
    # ---- 63 Puy-de-Dôme ----
    "clermont ferrand": "63",
    "aubiere": "63",
    "chamalières": "63",
    "chamilares": "63",
    "riom": "63",
    "issoire": "63",
    "thiers": "63",
    "cournon d auvergne": "63",
    "cebazat": "63",
    "pont du chateau": "63",
    "gerzat": "63",
    # ---- 64 Pyrénées-Atlantiques ----
    "bayonne": "64",
    "pau": "64",
    "biarritz": "64",
    "anglet": "64",
    "hendaye": "64",
    "saint jean de luz": "64",
    "orthez": "64",
    "oloron sainte marie": "64",
    "lons": "64",
    "billere": "64",
    "jurançon": "64",
    "jurancon": "64",
    # ---- 65 Hautes-Pyrénées ----
    "tarbes": "65",
    "lourdes": "65",
    "vic en bigorre": "65",
    # ---- 66 Pyrénées-Orientales ----
    "perpignan": "66",
    "canet en roussillon": "66",
    "rivesaltes": "66",
    "saint esteve": "66",
    "cabestany": "66",
    "prades": "66",
    # ---- 67 Bas-Rhin ----
    "strasbourg": "67",
    "schiltigheim": "67",
    "illkirch graffenstaden": "67",
    "haguenau": "67",
    "selestat": "67",
    "obernai": "67",
    "saverne": "67",
    "molsheim": "67",
    "lingolsheim": "67",
    "ostwald": "67",
    "bischheim": "67",
    "geispolsheim": "67",
    "lampertheim": "67",
    "mundolsheim": "67",
    "truchtersheim": "67",
    "reichstett": "67",
    # ---- 68 Haut-Rhin ----
    "mulhouse": "68",
    "colmar": "68",
    "saint louis": "68",
    "huningue": "68",
    "illzach": "68",
    "wittenheim": "68",
    "rixheim": "68",
    "kingersheim": "68",
    "thann": "68",
    "guebwiller": "68",
    "cernay": "68",
    "wintzenheim": "68",
    "herrlisheim pres colmar": "68",
    # ---- 69 Rhône ----
    "lyon": "69",
    "villeurbanne": "69",
    "venissieux": "69",
    "vaulx en velin": "69",
    "caluire et cuire": "69",
    "bron": "69",
    "saint priest": "69",
    "decines charpieu": "69",
    "meyzieu": "69",
    "rillieux la pape": "69",
    "givors": "69",
    "oullins": "69",
    "pierre benite": "69",
    "sainte foy les lyon": "69",
    "tassin la demi lune": "69",
    "ecully": "69",
    "chassieu": "69",
    "corbas": "69",
    "feyzin": "69",
    "mions": "69",
    "genas": "69",
    "saint genis laval": "69",
    "brignais": "69",
    "francheville": "69",
    "limonest": "69",
    "dardilly": "69",
    "craponne": "69",
    "neuville sur saone": "69",
    "saint fons": "69",
    "marcy l etoile": "69",
    "la tour de salvagny": "69",
    "tarare": "69",
    "l arbresle": "69",
    "saint symphorien d ozon": "69",
    "irigny": "69",
    "solaize": "69",
    "communay": "69",
    "ternay": "69",
    "vernaison": "69",
    "grigny": "69",
    "mornant": "69",
    "saint martin en haut": "69",
    "chazay d azergues": "69",
    "couzon au mont d or": "69",
    "collonges au mont d or": "69",
    "albigny sur saone": "69",
    "genay": "69",
    "fleurieu sur saone": "69",
    "saint germain au mont d or": "69",
    "fontaines sur saone": "69",
    "sathonay camp": "69",
    "sathonay village": "69",
    "champagne au mont d or": "69",
    "charbonnieres les bains": "69",
    "la mulatiere": "69",
    "lentilly": "69",
    "messimy": "69",
    "saint genis les ollieres": "69",
    "chaponost": "69",
    "vourles": "69",
    "charly": "69",
    "toussieu": "69",
    "simandres": "69",
    "serezin du rhone": "69",
    "les halles": "69",
    # ---- 70 Haute-Saône ----
    "vesoul": "70",
    "gray": "70",
    "hericourt": "70",
    "lure": "70",
    "luxeuil les bains": "70",
    # ---- 71 Saône-et-Loire ----
    "chalon sur saone": "71",
    "macon": "71",
    "le creusot": "71",
    "montceau les mines": "71",
    "louhans": "71",
    "autun": "71",
    "paray le monial": "71",
    # ---- 72 Sarthe ----
    "le mans": "72",
    "la fleche": "72",
    "allonnes": "72",
    # ---- 73 Savoie ----
    "chambery": "73",
    "aix les bains": "73",
    "albertville": "73",
    "bourg saint maurice": "73",
    "moutiers": "73",
    "la motte servolex": "73",
    "jacob bellecombette": "73",
    "montmelian": "73",
    "saint jean de maurienne": "73",
    # ---- 74 Haute-Savoie ----
    "annecy": "74",
    "annemasse": "74",
    "bonneville": "74",
    "thonon les bains": "74",
    "cluses": "74",
    "sallanches": "74",
    "chamonix mont blanc": "74",
    "seynod": "74",
    "poisy": "74",
    "cran gevrier": "74",
    "pringy": "74",
    "meythet": "74",
    "marnaz": "74",
    "scionzier": "74",
    "saint julien en genevois": "74",
    "archamps": "74",
    "gaillard": "74",
    "ville la grand": "74",
    "ambilly": "74",
    "vetraz monthoux": "74",
    "argonay": "74",
    # ---- 75 Paris ----
    "paris": "75",
    # ---- 76 Seine-Maritime ----
    "rouen": "76",
    "le havre": "76",
    "sotteville les rouen": "76",
    "grand quevilly": "76",
    "petit quevilly": "76",
    "dieppe": "76",
    "fecamp": "76",
    "mont saint aignan": "76",
    "maromme": "76",
    "bihorel": "76",
    "saint etienne du rouvray": "76",
    "oissel": "76",
    "elbeuf": "76",
    "caudebec les elbeuf": "76",
    "canteleu": "76",
    # ---- 77 Seine-et-Marne ----
    "meaux": "77",
    "chelles": "77",
    "melun": "77",
    "pontault combault": "77",
    "torcy": "77",
    "roissy en brie": "77",
    "lognes": "77",
    "savigny le temple": "77",
    "combs la ville": "77",
    "dammarie les lys": "77",
    "moissy cramayel": "77",
    "saint thibault des vignes": "77",
    "vaires sur marne": "77",
    "emerainville": "77",
    "noisiel": "77",
    "bussy saint georges": "77",
    # ---- 78 Yvelines ----
    "versailles": "78",
    "saint quentin en yvelines": "78",
    "guyancourt": "78",
    "sartrouville": "78",
    "mantes la jolie": "78",
    "les mureaux": "78",
    "poissy": "78",
    "saint germain en laye": "78",
    "plaisir": "78",
    "elancourt": "78",
    "trappes": "78",
    "rambouillet": "78",
    "velizy villacoublay": "78",
    "montigny le bretonneux": "78",
    "maurepas": "78",
    "la verriere": "78",
    "chatou": "78",
    "conflans sainte honorine": "78",
    "houilles": "78",
    "marly le roi": "78",
    "le vesinet": "78",
    "maisons laffitte": "78",
    # ---- 79 Deux-Sèvres ----
    "niort": "79",
    "parthenay": "79",
    "bressuire": "79",
    # ---- 80 Somme ----
    "amiens": "80",
    "abbeville": "80",
    "albert": "80",
    "longueau": "80",
    # ---- 81 Tarn ----
    "albi": "81",
    "castres": "81",
    "gaillac": "81",
    "mazamet": "81",
    # ---- 82 Tarn-et-Garonne ----
    "montauban": "82",
    "castelsarrasin": "82",
    # ---- 83 Var ----
    "toulon": "83",
    "hyeres": "83",
    "la seyne sur mer": "83",
    "frejus": "83",
    "saint raphael": "83",
    "brignoles": "83",
    "draguignan": "83",
    "sanary sur mer": "83",
    "six fours les plages": "83",
    "bandol": "83",
    "la valette du var": "83",
    "cuers": "83",
    "ollioules": "83",
    "le pradet": "83",
    "carqueiranne": "83",
    "la crau": "83",
    "le luc": "83",
    "roquebrune sur argens": "83",
    "sainte maxime": "83",
    "saint tropez": "83",
    "cogolin": "83",
    "grimaud": "83",
    "la garde": "83",
    "sollies pont": "83",
    "pierrefeu du var": "83",
    "la londe les maures": "83",
    "bormes les mimosas": "83",
    "le lavandou": "83",
    "collobrières": "83",
    "collobrieres": "83",
    "nans les pins": "83",
    "rocbaron": "83",
    "puget ville": "83",
    "pignans": "83",
    "gonfaron": "83",
    "vidauban": "83",
    "les arcs": "83",
    "flayosc": "83",
    "montauroux": "83",
    "fayence": "83",
    "callian": "83",
    "seillans": "83",
    "bagnols en foret": "83",
    "la motte": "83",
    "le muy": "83",
    "tourves": "83",
    "saint maximin la sainte baume": "83",
    "aups": "83",
    "vinon sur verdon": "83",
    "rians": "83",
    "barjols": "83",
    "cotignac": "83",
    "la seyne": "83",
    "var": "83",
    # ---- 84 Vaucluse ----
    "avignon": "84",
    "orange": "84",
    "carpentras": "84",
    "cavaillon": "84",
    "l isle sur la sorgue": "84",
    "pertuis": "84",
    "sorgues": "84",
    "le pontet": "84",
    "vedene": "84",
    "monteux": "84",
    "bollene": "84",
    # ---- 85 Vendée ----
    "la roche sur yon": "85",
    "les sables d olonne": "85",
    "fontenay le comte": "85",
    "saint jean de monts": "85",
    "challans": "85",
    # ---- 86 Vienne ----
    "poitiers": "86",
    "chatellerault": "86",
    "buxerolles": "86",
    "saint benoit": "86",
    # ---- 87 Haute-Vienne ----
    "limoges": "87",
    "saint junien": "87",
    "panazol": "87",
    # ---- 88 Vosges ----
    "epinal": "88",
    "saint die des vosges": "88",
    "remiremont": "88",
    # ---- 89 Yonne ----
    "auxerre": "89",
    "sens": "89",
    "avallon": "89",
    # ---- 90 Territoire de Belfort ----
    "belfort": "90",
    "offemont": "90",
    "bavilliers": "90",
    "valdoie": "90",
    "beaucourt": "90",
    # ---- 91 Essonne ----
    "massy": "91",
    "evry": "91",
    "evry courcouronnes": "91",
    "corbeil essonnes": "91",
    "longjumeau": "91",
    "palaiseau": "91",
    "les ulis": "91",
    "bretigny sur orge": "91",
    "dourdan": "91",
    "athis mons": "91",
    "viry chatillon": "91",
    "sainte genevieve des bois": "91",
    "ris orangis": "91",
    "savigny sur orge": "91",
    "fleury merogis": "91",
    "etampes": "91",
    "juvisy sur orge": "91",
    "brunoy": "91",
    "montgeron": "91",
    "yerres": "91",
    "courcouronnes": "91",
    "lisses": "91",
    # ---- 92 Hauts-de-Seine ----
    "boulogne billancourt": "92",
    "nanterre": "92",
    "neuilly sur seine": "92",
    "levallois perret": "92",
    "courbevoie": "92",
    "issy les moulineaux": "92",
    "asnieres sur seine": "92",
    "colombes": "92",
    "rueil malmaison": "92",
    "antony": "92",
    "clamart": "92",
    "chatillon": "92",
    "montrouge": "92",
    "malakoff": "92",
    "vanves": "92",
    "bagneux": "92",
    "fontenay aux roses": "92",
    "le plessis robinson": "92",
    "sceaux": "92",
    "bourg la reine": "92",
    "la garenne colombes": "92",
    "garches": "92",
    "sevres": "92",
    "meudon": "92",
    "chaville": "92",
    "gennevilliers": "92",
    "clichy": "92",
    "puteaux": "92",
    "suresnes": "92",
    "chatenay malabry": "92",
    "vaucresson": "92",
    # ---- 93 Seine-Saint-Denis ----
    "saint denis": "93",
    "montreuil": "93",
    "aubervilliers": "93",
    "saint ouen": "93",
    "noisy le grand": "93",
    "bondy": "93",
    "bobigny": "93",
    "aulnay sous bois": "93",
    "pantin": "93",
    "rosny sous bois": "93",
    "epinay sur seine": "93",
    "stains": "93",
    "la courneuve": "93",
    "villepinte": "93",
    "tremblay en france": "93",
    "drancy": "93",
    "clichy sous bois": "93",
    "montfermeil": "93",
    "sevran": "93",
    "livry gargan": "93",
    "neuilly sur marne": "93",
    "gagny": "93",
    "neuilly plaisance": "93",
    "les lilas": "93",
    "le pre saint gervais": "93",
    "noisy le sec": "93",
    "romainville": "93",
    "bagnolet": "93",
    # ---- 94 Val-de-Marne ----
    "vincennes": "94",
    "creteil": "94",
    "ivry sur seine": "94",
    "vitry sur seine": "94",
    "alfortville": "94",
    "saint maur des fosses": "94",
    "champigny sur marne": "94",
    "maisons alfort": "94",
    "l hay les roses": "94",
    "nogent sur marne": "94",
    "fontenay sous bois": "94",
    "charenton le pont": "94",
    "joinville le pont": "94",
    "saint maurice": "94",
    "orly": "94",
    "valenton": "94",
    "sucy en brie": "94",
    "boissy saint leger": "94",
    "villeneuve saint georges": "94",
    "villeneuve le roi": "94",
    # ---- 95 Val-d'Oise ----
    "argenteuil": "95",
    "cergy": "95",
    "pontoise": "95",
    "sarcelles": "95",
    "garges les gonesse": "95",
    "gonesse": "95",
    "enghien les bains": "95",
    "saint leu la foret": "95",
    "taverny": "95",
    "montmorency": "95",
    "soisy sous montmorency": "95",
    "deuil la barre": "95",
    "ecouen": "95",
    "roissy en france": "95",
    "villiers le bel": "95",
    "franconville": "95",
    "saint gratien": "95",
    "cormeilles en parisis": "95",
    "herblay sur seine": "95",
    "ermont": "95",
    "eaubonne": "95",
    "osny": "95",
    "domont": "95",
    # ---- Overseas ----
    "pointe a pitre": "971",
    "basse terre": "971",
    "les abymes": "971",
    "baie mahault": "971",
    "petit bourg": "971",
    "fort de france": "972",
    "le lamentin": "972",
    "le robert": "972",
    "le francois": "972",
    "cayenne": "973",
    "kourou": "973",
    "saint laurent du maroni": "973",
    "saint denis de la reunion": "974",
    "saint pierre de la reunion": "974",
    "le tampon": "974",
    "saint paul de la reunion": "974",
    "le port": "974",
    "mamoudzou": "976",
    "koungou": "976",
    "bandraboua": "976",
}


# Pre-sort city entries longest-first so that a more specific name (e.g.
# "toulon") is checked before any shorter name that is its prefix (e.g. "toul").
# This prevents "Toulon, France" from matching "toul" → 54 instead of "toulon" → 83.
_CITY_TO_DEPT_SORTED: list[tuple[str, str]] = sorted(
    _CITY_TO_DEPT.items(), key=lambda kv: len(kv[0]), reverse=True
)


# ---------------------------------------------------------------------------
# Department code extraction
# ---------------------------------------------------------------------------

_POSTAL_5_RE = re.compile(r"\b(\d{5})\b")
_DEPT_PARENS_RE = re.compile(r"\((\d{1,3}|2[AB])\)", re.I)
_DEPT_BARE_RE = re.compile(r"(?<!\d)(\d{2,3})(?!\d)")
# France Travail format: "69 - Meyzieu" or "2A - Ajaccio" at start of location string
_DEPT_LEAD_RE = re.compile(r"^\s*(\d{2,3}|2[AB])\s*[-–\s]", re.I)


# Departments bordering Var (83) within the PACA region — used by the
# scoring layer's location-fit tier to give a "near Var" bonus below the
# full Var bonus. Var itself is excluded (it has its own top tier).
PACA_NEIGHBOR_DEPARTMENTS: frozenset[str] = frozenset({"13", "04", "05", "06", "84"})


def _postal_to_dept(code: str) -> str | None:
    """Convert a 5-digit French postal code to a department code."""
    if len(code) != 5:
        return None
    if code.startswith("97"):
        overseas = code[:3]
        if overseas in _BY_CODE:
            return overseas
        return None
    prefix2 = code[:2]
    # Legacy Corsica: 20xxx split into 2A / 2B (20000–20199 → 2A, rest → 2B)
    if prefix2 == "20":
        try:
            num = int(code[2:])
            return "2A" if num <= 199 else "2B"
        except ValueError:
            return None
    if prefix2 in _BY_CODE:
        return prefix2
    return None


def extract_department_code(location: str, countries: list[str] | None = None) -> str | None:
    """Try to determine the French department code from a location string.

    Returns the department code (e.g. "83") or None when it cannot be
    reliably determined.  Never guesses — only returns a code when there
    is strong evidence.

    Args:
        location:  Raw location string from the job (e.g. "Toulon (83)").
        countries: Optional list of known countries for the job.

    Decision order:
        1. Explicit "(83)" pattern.
        2. 5-digit postal code.
        3. Known French city name.
    """
    if not location:
        return None

    loc = location.strip()

    # 1. Explicit department code in parentheses, e.g. "Toulon (83)"
    m = _DEPT_PARENS_RE.search(loc)
    if m:
        code = m.group(1).upper()
        if code in _BY_CODE:
            return code

    # 2. Leading department code, e.g. France Travail format "69 - Meyzieu"
    m2 = _DEPT_LEAD_RE.match(loc)
    if m2:
        code = m2.group(1).upper()
        if code in _BY_CODE:
            return code

    # 3. Postal code
    postal_matches = _POSTAL_5_RE.findall(loc)
    for postal in postal_matches:
        dept = _postal_to_dept(postal)
        if dept is not None:
            return dept

    # 4. City name lookup (accent-insensitive, longest match first)
    loc_norm = _normalize_name(loc)
    for city_norm, dept_code in _CITY_TO_DEPT_SORTED:
        if city_norm in loc_norm:
            return dept_code

    return None


# ---------------------------------------------------------------------------
# Search / autocomplete
# ---------------------------------------------------------------------------

def search_departments(query: str, limit: int = 10) -> list[Department]:
    """Return departments matching a query.

    Matches against:
    - Department code (exact or prefix): "83" → Var
    - Department name (normalized, accent-insensitive): "var" → Var
    - Major city name: "lyon" → Rhône (69)

    Args:
        query: User-typed search string.
        limit: Maximum results.

    Returns:
        Matching departments, most specific match first.
    """
    if not query or not query.strip():
        return []

    q = query.strip()
    q_norm = _normalize_name(q)
    results: list[Department] = []
    seen: set[str] = set()

    def _add(d: Department) -> None:
        if d.code not in seen:
            seen.add(d.code)
            results.append(d)

    # 1. Exact code match
    q_upper = q.upper()
    if q_upper in _BY_CODE:
        _add(_BY_CODE[q_upper])

    # 2. Code prefix match (e.g. "8" → 80, 81, 82, 83, 84, 85, 86, 87, 88, 89)
    for d in _DEPARTMENTS:
        if d.code.upper().startswith(q_upper) and d.code.upper() != q_upper:
            _add(d)
            if len(results) >= limit:
                return results

    # 3. Normalized name prefix match
    for name_norm, d in _BY_NAME_NORM.items():
        if name_norm.startswith(q_norm):
            _add(d)
            if len(results) >= limit:
                return results

    # 4. Normalized name substring match
    for name_norm, d in _BY_NAME_NORM.items():
        if q_norm in name_norm and not name_norm.startswith(q_norm):
            _add(d)
            if len(results) >= limit:
                return results

    # 5. City name lookup
    for city_norm, dept_code in _CITY_TO_DEPT_SORTED:
        if q_norm in city_norm and dept_code in _BY_CODE:
            _add(_BY_CODE[dept_code])
            if len(results) >= limit:
                return results

    return results[:limit]


def get_department(code: str) -> Department | None:
    """Look up a single department by its canonical code."""
    return _BY_CODE.get(code.upper())


def all_departments() -> list[Department]:
    """Return the full list of departments."""
    return list(_DEPARTMENTS)
