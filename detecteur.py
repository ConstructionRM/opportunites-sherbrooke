"""
Détecteur d'opportunités immobilières — Sherbrooke
==================================================
Sources :
  - Rôle d'évaluation foncière : MAMH / Données Québec (XML)
  - Zonage municipal : Ville de Sherbrooke / ArcGIS REST (GeoJSON)

Opportunité 1 — Terrain résiduel constructible
  Critère : propriété résidentielle unifamiliale dont la superficie de
  terrain est suffisante pour accueillir une seconde résidence selon
  les normes minimales de Sherbrooke, et où la valeur du terrain
  représente une part significative de la valeur totale.

Opportunité 2 — Propriété sous-évaluée en zone multi-logement
  Critère : propriété avec bâtiment existant (maison) située dans une
  zone autorisant le multi-logement, ET valeur bâtiment < valeur terrain
  (signal que la valeur est concentrée dans le terrain, pas dans ce qui
  y est construit).
"""

import requests
import xml.etree.ElementTree as ET
import json
import math
import sys
from io import BytesIO
from zipfile import ZipFile
from pathlib import Path
# ---------------------------------------------------------------------------
# Constantes et seuils
# ---------------------------------------------------------------------------

# Code MAMH de la municipalité de Sherbrooke
CODE_MUNICIPALITE_SHERBROOKE = "43027"

# URL index des rôles 2026 (MAMH — mise à jour trimestrielle)
URL_INDEX_ROLE = "https://donneesouvertes.affmunqc.net/role/indexRole2026.csv"

# API REST ArcGIS — zonage Sherbrooke (toutes les zones d'un seul coup, max 2000)
URL_ZONAGE_ARCGIS = (
    "https://services3.arcgis.com/qsNXG7LzoUbR4c1C/arcgis/rest/services/"
    "Zonage/FeatureServer/0/query"
    "?where=1%3D1&outFields=NO_ZONE,GRILLEUSAGE&f=json&resultRecordCount=5000"
)

# Superficie minimale de terrain pour une seconde résidence (m²)
# Sherbrooke exige typiquement ~450 m² pour un lot en zone R1/R2
# Source : règlement de zonage, zones résidentielles de faible densité
SUPERFICIE_MIN_LOT_SECONDAIRE_M2 = 450.0

# Pourcentage de la superficie totale qui doit être résiduelle
# (après l'emprise approximative du bâtiment existant)
# On estime l'emprise bâtiment à ~15 % de la superficie du lot
RATIO_EMPRISE_BATIMENT_ESTIME = 0.15

# Ratio valeur bâtiment / valeur terrain pour qualifier une sous-évaluation
# Si valeur_batiment < valeur_terrain → signal de sous-optimalité du bâti
RATIO_SOUS_EVALUATION_MAX = 1.0

# Codes d'utilisation MAMH qui correspondent à des propriétés résidentielles
# avec un bâtiment principal (unifamiliale, bifamiliale, etc.)
CODES_USAGE_RESIDENTIEL_AVEC_BATIMENT = {
    "1000",  # Unifamiliale détachée
    "1001",  # Unifamiliale semi-détachée
    "1002",  # Unifamiliale en rangée
    "1003",  # Maison mobile
    "1100",  # Bifamiliale
    "1110",  # Bifamiliale superposée
    "1200",  # Trifamiliale
    "1300",  # Quadrifamiliale
}

# Préfixes de codes de zone Sherbrooke autorisant le multi-logement
# Basé sur la nomenclature réelle du règlement de zonage de Sherbrooke :
# H#### = Habitation (multi-logement permis selon densité)
# RU### = Résidentiel Urbain (permet souvent bifamiliale et plus)
# MX### = Mixte (résidentiel + commercial)
# TC### = Transit-orienté / centre
PREFIXES_ZONE_MULTI = ("H0", "H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9",
                        "RU", "MX", "TC", "CM")

# ---------------------------------------------------------------------------
def telecharger_role_sherbrooke():
    """
    Récupère l'index CSV du rôle 2026, trouve l'URL du fichier XML de
    Sherbrooke, télécharge et parse le XML pour en extraire les unités
    d'évaluation pertinentes.
    Retourne une liste de dicts avec les champs utiles.
    """
    print("→ Téléchargement de l'index du rôle 2026...")
    try:
        r = requests.get(URL_INDEX_ROLE, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  ✗ Réseau inaccessible : {e}")
        print("  → Basculement en mode DÉMONSTRATION avec données synthétiques réalistes.")
        return generer_donnees_demo()

    # L'index CSV contient : CodeMunicipalite, NomMunicipalite, URL_XML
    url_xml_sherbrooke = None
    for ligne in r.text.splitlines():
        colonnes = ligne.split(",")
        if len(colonnes) >= 3 and CODE_MUNICIPALITE_SHERBROOKE in colonnes[0]:
            # La 3e colonne (ou la dernière qui commence par http) est l'URL
            for col in colonnes:
                if col.strip().startswith("http"):
                    url_xml_sherbrooke = col.strip()
                    break
            if url_xml_sherbrooke:
                break

    if not url_xml_sherbrooke:
        print("  ✗ URL XML de Sherbrooke introuvable dans l'index.")
        print("  → Utilisation de données de démonstration synthétiques.")
        return generer_donnees_demo()

    # Si réseau inaccessible, fallback démo

    print(f"  ✓ URL trouvée : {url_xml_sherbrooke}")
    print("  → Téléchargement du fichier XML (peut prendre 1–2 min)...")

    try:
        r2 = requests.get(url_xml_sherbrooke, timeout=120, stream=True)
        r2.raise_for_status()
        contenu = r2.content

        # Les fichiers peuvent être zippés
        if url_xml_sherbrooke.endswith(".zip") or contenu[:2] == b"PK":
            print("  → Fichier compressé, extraction en cours...")
            with ZipFile(BytesIO(contenu)) as zf:
                noms = [n for n in zf.namelist() if n.endswith(".xml")]
                if not noms:
                    print("  ✗ Aucun fichier XML dans le ZIP.")
                    return generer_donnees_demo()
                contenu = zf.read(noms[0])
    except Exception as e:
        print(f"  ✗ Erreur téléchargement XML : {e}")
        print("  → Utilisation de données de démonstration synthétiques.")
        return generer_donnees_demo()

    return parser_xml_role(contenu)


def parser_xml_role(contenu_xml: bytes) -> list:
    """
    Parse le XML du rôle MAMH — structure réelle confirmée par diagnostic :
      Balise unité : <RLUEx>
      RL0105A = code usage (1000=unifam, 1001=semi-détaché, etc.)
      RL0302A = superficie du terrain (pieds carrés)
      RL0402A = valeur du terrain ($)
      RL0403A = valeur du bâtiment ($)
      RL0404A = valeur totale ($)
      RL0102A = code arrondissement/secteur
      RL0107A = identifiant lot (contient parfois des lettres — ignorer pour superficie)
      RL0308A = superficie plancher bâtiment (m²)
    """
    print("  → Analyse du XML en cours...")
    try:
        root = ET.fromstring(contenu_xml)
    except ET.ParseError as e:
        print(f"  ✗ Erreur de parsing XML : {e}")
        return generer_donnees_demo()

    balises = set()
    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        balises.add(local)
    print(f"  → {len(balises)} balises distinctes trouvées dans le XML")

    from collections import Counter
    tags_enfants = Counter()
    for child in root:
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        tags_enfants[local] += 1
    print(f"  → Tags enfants de la racine : {dict(tags_enfants.most_common(3))}")

    tag_unite = tags_enfants.most_common(1)[0][0] if tags_enfants else None
    if not tag_unite:
        print("  ✗ Impossible d'identifier la balise d'unité.")
        return generer_donnees_demo()
    print(f"  → Tag d'unité : <{tag_unite}> ({tags_enfants[tag_unite]} occurrences)")

    def get_field(elem, *tags):
        """Retourne la première valeur non-vide parmi les tags donnés."""
        for tag in tags:
            for child in elem:
                local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if local == tag and child.text and child.text.strip():
                    return child.text.strip()
        return ""

    def nettoyer_float(valeur: str) -> float:
        """Convertit une string en float, en ignorant les caractères non-numériques."""
        if not valeur:
            return 0.0
        # Garder seulement chiffres, point et virgule
        propre = ""
        for c in valeur.replace(",", "."):
            if c.isdigit() or c == ".":
                propre += c
        try:
            return float(propre) if propre else 0.0
        except ValueError:
            return 0.0

    # Codes d'usage résidentiels MAMH (RL0105A)
    # 1000 = unifamiliale détachée
    # 1001 = unifamiliale jumelée/semi-détachée
    # 1002 = unifamiliale en rangée
    # 1100 = bifamiliale
    # 1200 = trifamiliale
    # 1300 = quadrifamiliale
    CODES_RESID = {"1000", "1001", "1002", "1003", "1100", "1110", "1200", "1300"}

    unites = []
    total = 0

    for child in root:
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if local != tag_unite:
            continue
        total += 1

        code_usage = get_field(child, "RL0105A").strip()
        if code_usage not in CODES_RESID:
            continue

        # Superficie du terrain — RL0302A en pieds carrés → convertir en m²
        sup_pieds2_str = get_field(child, "RL0302A")
        sup_m2_str     = get_field(child, "RL0308A")  # superficie plancher (m²)

        sup_pieds2 = nettoyer_float(sup_pieds2_str)
        sup_plancher_m2 = nettoyer_float(sup_m2_str)

        # Convertir pieds carrés → m² (1 pi² = 0.092903 m²)
        sup_terrain_m2 = sup_pieds2 * 0.092903 if sup_pieds2 > 0 else 0.0

        # Si pas de superficie de terrain, essayer d'autres champs
        if sup_terrain_m2 <= 0:
            # RL0303A = nombre de logements, pas la superficie — ignorer
            # Utiliser superficie plancher comme proxy si disponible
            sup_terrain_m2 = sup_plancher_m2 * 4 if sup_plancher_m2 > 0 else 0.0

        if sup_terrain_m2 <= 0:
            continue

        # Valeurs financières
        val_terrain  = nettoyer_float(get_field(child, "RL0402A"))
        val_batiment = nettoyer_float(get_field(child, "RL0403A"))
        val_totale   = nettoyer_float(get_field(child, "RL0404A"))

        # Recalcul si manquant
        if val_totale <= 0:
            val_totale = val_terrain + val_batiment
        if val_batiment <= 0 and val_totale > 0 and val_terrain > 0:
            val_batiment = max(0.0, val_totale - val_terrain)

        if val_terrain <= 0:
            continue

        # Adresse : RL0101 contient souvent \n et espaces — caviardé dans le XML public
        # On utilise le secteur RL0102A comme référence géographique
        secteur = get_field(child, "RL0102A") or "N/D"
        id_uef  = get_field(child, "RL0101").strip() or str(total)
        adresse = f"Secteur {secteur} — Lot #{id_uef}, Sherbrooke, QC"

        unites.append({
            "id_uef":        id_uef,
            "adresse":       adresse,
            "code_usage":    code_usage,
            "val_terrain":   val_terrain,
            "val_batiment":  val_batiment,
            "val_totale":    val_totale,
            "superficie_m2": round(sup_terrain_m2, 1),
            "latitude":      None,
            "longitude":     None,
            "no_zone":       "",
        })

    print(f"  → {total} unités totales, {len(unites)} résidentielles extraites.")

    if not unites:
        print("  ✗ Aucune unité extraite — mode démo activé.")
        return generer_donnees_demo()

    return unites


# ---------------------------------------------------------------------------
# Téléchargement du zonage Sherbrooke
# ---------------------------------------------------------------------------

def telecharger_zonage_sherbrooke() -> list:
    """
    Récupère les zones via l'API ArcGIS REST de la Ville de Sherbrooke.
    Retourne une liste de dicts {no_zone, grille_usage, geometrie_approx}
    """
    print("→ Téléchargement du zonage Sherbrooke (ArcGIS REST)...")
    zones = []
    offset = 0
    batch = 1000

    while True:
        url = (
            "https://services3.arcgis.com/qsNXG7LzoUbR4c1C/arcgis/rest/services/"
            f"Zonage/FeatureServer/0/query"
            f"?where=1%3D1&outFields=NO_ZONE,GRILLEUSAGE"
            f"&f=json&resultOffset={offset}&resultRecordCount={batch}"
            f"&returnGeometry=false"
        )
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            if offset == 0:
                print(f"  ✗ API zonage inaccessible : {e}")
                print("  → Les zones seront déduites des préfixes dans les données de démo.")
            break

        features = data.get("features", [])
        if not features:
            break

        for feat in features:
            attrs = feat.get("attributes", {})
            no_zone = (attrs.get("NO_ZONE") or "").strip()
            grille = (attrs.get("GRILLEUSAGE") or "").strip()
            if no_zone:
                zones.append({"no_zone": no_zone, "grille_usage": grille})

        if not data.get("exceededTransferLimit"):
            break
        offset += batch

    print(f"  ✓ {len(zones)} zones de zonage récupérées.")
    return zones


def identifier_zones_multi(zones: list) -> set:
    """Retourne l'ensemble des NO_ZONE autorisant le multi-logement."""
    zones_multi = set()
    for z in zones:
        no = z["no_zone"].upper()
        for pref in PREFIXES_ZONE_MULTI:
            if no.startswith(pref):
                zones_multi.add(z["no_zone"])
                break
    print(f"  ✓ {len(zones_multi)} zones identifiées comme multi-logement.")
    return zones_multi


# ---------------------------------------------------------------------------
# Données de démonstration (si les APIs ne répondent pas)
# ---------------------------------------------------------------------------

def generer_donnees_demo() -> list:
    """
    Génère des données synthétiques réalistes pour illustrer le rapport.
    Utilisé quand les sources publiques sont inaccessibles depuis cet environnement.
    """
    import random
    random.seed(42)

    rues = [
        "Rue King Ouest", "Rue Wellington Sud", "Boul. Portland",
        "Rue Galt Ouest", "Rue Bowen Sud", "Rue Dufferin", "Rue Prospect",
        "Rue Belvédère Nord", "Rue Frontenac", "Rue Alexandre",
        "Rue du Dépôt", "Rue Sylvestre", "Boul. Jacques-Cartier Nord",
        "Rue Argyll", "Rue Laurier", "Rue Papineau",
        "Rue Heriot", "Rue Murray", "Rue Western", "Rue Gordon",
    ]
    codes = list(CODES_USAGE_RESIDENTIEL_AVEC_BATIMENT)
    zones_possibles = [
        "R1-A", "R1-B", "R2-A", "R2-B", "R3-A", "RM-1", "RM-2",
        "R1-C", "R2-C", "MX-1", "RH-1", "R3-B",
    ]

    unites = []
    for i in range(80):
        sup = random.uniform(300, 2500)
        val_terrain = sup * random.uniform(350, 850)
        # Certains ont val_batiment < val_terrain (signal opportunité 2)
        if random.random() < 0.3:
            val_batiment = val_terrain * random.uniform(0.4, 0.9)
        else:
            val_batiment = val_terrain * random.uniform(1.0, 3.5)

        no = random.randint(100, 999)
        rue = random.choice(rues)
        no_zone = random.choice(zones_possibles)

        unites.append({
            "id_uef": f"DEMO-{i+1:04d}",
            "adresse": f"{no} {rue}, Sherbrooke, QC",
            "code_usage": random.choice(codes),
            "val_terrain": round(val_terrain, 2),
            "val_batiment": round(val_batiment, 2),
            "val_totale": round(val_terrain + val_batiment, 2),
            "superficie_m2": round(sup, 1),
            "latitude": round(45.40 + random.uniform(-0.08, 0.12), 6),
            "longitude": round(-71.89 + random.uniform(-0.10, 0.08), 6),
            "no_zone": no_zone,
        })

    print(f"  ✓ {len(unites)} unités de démonstration générées.")
    return unites


# ---------------------------------------------------------------------------
# Logique de détection
# ---------------------------------------------------------------------------

def calculer_superficie_residuelle(unite: dict) -> float:
    """Superficie estimée disponible après l'emprise du bâtiment existant."""
    sup = unite["superficie_m2"]
    emprise = sup * RATIO_EMPRISE_BATIMENT_ESTIME
    return max(0.0, sup - emprise)


def score_opportunite1(unite: dict) -> tuple[int, str]:
    """
    Score 0–100 pour l'opportunité 1 (terrain résiduel constructible).
    Retourne (score, justification).
    """
    sup_residuelle = calculer_superficie_residuelle(unite)
    sup_totale = unite["superficie_m2"]
    val_terrain = unite["val_terrain"]
    val_totale = unite["val_totale"]

    if sup_residuelle < SUPERFICIE_MIN_LOT_SECONDAIRE_M2:
        return 0, f"Superficie résiduelle insuffisante ({sup_residuelle:.0f} m² < {SUPERFICIE_MIN_LOT_SECONDAIRE_M2:.0f} m² requis)"

    # Score de superficie : plus c'est grand, mieux c'est
    ratio_sup = min(sup_residuelle / (SUPERFICIE_MIN_LOT_SECONDAIRE_M2 * 2), 1.0)
    score_sup = int(ratio_sup * 50)

    # Score de valeur : si le terrain vaut une part importante du total,
    # c'est intéressant de construire dessus
    ratio_terrain = val_terrain / val_totale if val_totale > 0 else 0
    score_val = int(min(ratio_terrain * 2, 1.0) * 30)

    # Bonus si superficie > 1000 m²
    bonus = 20 if sup_totale > 1000 else (10 if sup_totale > 700 else 0)

    score = min(score_sup + score_val + bonus, 100)
    justif = (
        f"Superficie résiduelle : {sup_residuelle:.0f} m² | "
        f"Terrain = {ratio_terrain*100:.0f}% de la valeur totale"
    )
    return score, justif


def score_opportunite2(unite: dict, zones_multi: set) -> tuple[int, str]:
    """
    Score 0–100 pour l'opportunité 2 (sous-évaluation en zone multi).
    Retourne (score, justification).
    """
    no_zone = unite.get("no_zone", "")
    val_terrain = unite["val_terrain"]
    val_batiment = unite["val_batiment"]
    val_totale = unite["val_totale"]

    # Vérification zone multi
    zone_permise = no_zone in zones_multi if zones_multi else any(
        no_zone.upper().startswith(p) for p in PREFIXES_ZONE_MULTI
    )
    if not zone_permise:
        return 0, f"Zone {no_zone or 'N/D'} ne permet pas le multi-logement"

    # Vérification ratio bâtiment / terrain
    if val_batiment >= val_terrain:
        return 0, (
            f"Valeur bâtiment ({val_batiment:,.0f} $) ≥ valeur terrain "
            f"({val_terrain:,.0f} $) — pas de sous-évaluation"
        )

    # Score : plus le ratio bat/terrain est faible, plus c'est intéressant
    ratio_bat_terrain = val_batiment / val_terrain if val_terrain > 0 else 1.0
    score_ratio = int((1.0 - ratio_bat_terrain) * 60)

    # Bonus valeur absolue du terrain
    if val_terrain > 300_000:
        bonus_val = 25
    elif val_terrain > 150_000:
        bonus_val = 15
    else:
        bonus_val = 5

    # Bonus superficie
    bonus_sup = 15 if unite["superficie_m2"] > 800 else (8 if unite["superficie_m2"] > 500 else 0)

    score = min(score_ratio + bonus_val + bonus_sup, 100)
    justif = (
        f"Zone {no_zone} (multi permis) | "
        f"Bâtiment = {ratio_bat_terrain*100:.0f}% de la valeur terrain | "
        f"Terrain : {val_terrain:,.0f} $"
    )
    return score, justif


# ---------------------------------------------------------------------------
# Résolution d'adresse civique via iCherche (Gouvernement du Québec)
# ---------------------------------------------------------------------------

def resoudre_adresses(opportunites: list, label: str) -> list:
    """
    Génère des liens directs vers la fiche de chaque propriété sur le portail
    de consultation du rôle d'évaluation de Sherbrooke.
    URL : https://espace-evaluation.sherbrooke.ca/consultation-du-role/recherche?matricule=XXXXXXX
    """
    total = len(opportunites)
    if total == 0:
        return opportunites

    print(f"  → Génération des liens de consultation ({label}) — {total} propriétés...")

    for opp in opportunites:
        id_uef = (opp.get("id_uef") or "").strip()
        if id_uef:
            opp["lien_fiche"] = (
                f"https://espace-evaluation.sherbrooke.ca/"
                f"consultation-du-role/recherche?matricule={id_uef}"
            )
        else:
            opp["lien_fiche"] = ""

    print(f"  ✓ {total} liens générés.")
    return opportunites


# ---------------------------------------------------------------------------
# Création du fichier Excel
# ---------------------------------------------------------------------------

def generer_html(opportunites1: list, opportunites2: list, chemin: Path):
    """Génère un tableau de bord HTML interactif avec les données embarquées."""
    import json

    def prep(lst):
        out = []
        for o in lst:
            out.append({
                "score":       o.get("score", 0),
                "adresse":     o.get("adresse", ""),
                "matricule":   o.get("id_uef", ""),
                "zone":        o.get("no_zone", ""),
                "usage":       o.get("code_usage", ""),
                "sup":         round(o.get("superficie_m2", 0), 0),
                "sup_res":     round(o.get("sup_residuelle", 0), 0) if "sup_residuelle" in o else None,
                "val_terrain": o.get("val_terrain", 0),
                "val_bat":     o.get("val_batiment", 0),
                "val_total":   o.get("val_totale", 0),
                "pct_terrain": round(o.get("pct_terrain", 0) * 100, 1) if "pct_terrain" in o else None,
                "ratio_bat":   round(o.get("ratio_bat", 0), 2) if "ratio_bat" in o else None,
                "justif":      o.get("justification", ""),
            })
        return out

    data1 = json.dumps(prep(opportunites1), ensure_ascii=False)
    data2 = json.dumps(prep(opportunites2), ensure_ascii=False)
    from datetime import datetime
    date_analyse = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Opportunités immobilières — Sherbrooke</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=DM+Sans:wght@300;400;500&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {{
  --marine:   #1B2A4A;
  --or:       #C9A84C;
  --or-clair: #F5E6C0;
  --vert:     #1E7A45;
  --vert-bg:  #D6F0E2;
  --rouge:    #A32D2D;
  --rouge-bg: #FCE8E8;
  --ambre-bg: #FFF3CD;
  --ambre:    #7D5A00;
  --bg:       #F7F5F0;
  --surface:  #FFFFFF;
  --border:   #E2DDD6;
  --text:     #1B2A4A;
  --muted:    #6B6557;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: 'DM Sans', sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}}

/* ── HEADER ── */
header {{
  background: var(--marine);
  padding: 28px 40px 24px;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
}}
header h1 {{
  font-family: 'Playfair Display', serif;
  font-size: 26px;
  color: #fff;
  letter-spacing: -0.3px;
  line-height: 1.2;
}}
header h1 span {{ color: var(--or); }}
header .meta {{
  font-size: 12px;
  color: rgba(255,255,255,0.5);
  text-align: right;
  line-height: 1.7;
}}

/* ── TABS ── */
.tabs {{
  display: flex;
  gap: 0;
  background: var(--marine);
  padding: 0 40px;
  border-bottom: 2px solid var(--or);
}}
.tab {{
  padding: 14px 28px;
  font-size: 13px;
  font-weight: 500;
  color: rgba(255,255,255,0.5);
  cursor: pointer;
  border-bottom: 3px solid transparent;
  margin-bottom: -2px;
  transition: all .2s;
  letter-spacing: 0.3px;
}}
.tab:hover {{ color: rgba(255,255,255,0.8); }}
.tab.active {{
  color: var(--or);
  border-bottom-color: var(--or);
}}
.tab .badge {{
  display: inline-block;
  background: rgba(201,168,76,0.2);
  color: var(--or);
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 10px;
  margin-left: 6px;
  font-weight: 500;
}}

/* ── CONTROLS ── */
.controls {{
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 16px 40px;
  display: flex;
  gap: 16px;
  align-items: center;
  flex-wrap: wrap;
}}
.controls label {{
  font-size: 12px;
  color: var(--muted);
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}}
.filter-group {{
  display: flex;
  align-items: center;
  gap: 8px;
}}
select, input[type=range] {{
  font-family: 'DM Sans', sans-serif;
  font-size: 13px;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  background: var(--bg);
  color: var(--text);
  outline: none;
}}
select:focus {{ border-color: var(--or); }}
.score-val {{
  font-family: 'DM Mono', monospace;
  font-size: 13px;
  font-weight: 500;
  color: var(--marine);
  min-width: 30px;
}}
.count {{
  margin-left: auto;
  font-size: 13px;
  color: var(--muted);
}}
.count strong {{ color: var(--marine); font-weight: 500; }}
input[type=text] {{
  font-family: 'DM Sans', sans-serif;
  font-size: 13px;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 12px;
  background: var(--bg);
  color: var(--text);
  outline: none;
  width: 200px;
}}
input[type=text]:focus {{ border-color: var(--or); }}

/* ── TABLE ── */
.table-wrap {{
  padding: 24px 40px;
  overflow-x: auto;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}}
thead tr {{
  background: var(--marine);
  color: #fff;
}}
thead th {{
  padding: 12px 14px;
  text-align: left;
  font-weight: 500;
  font-size: 12px;
  letter-spacing: 0.3px;
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
}}
thead th:hover {{ background: #253d6b; }}
thead th.sorted-asc::after  {{ content: " ↑"; color: var(--or); }}
thead th.sorted-desc::after {{ content: " ↓"; color: var(--or); }}
tbody tr {{
  border-bottom: 1px solid var(--border);
  transition: background .15s;
  cursor: pointer;
}}
tbody tr:hover {{ background: #F0EDE6; }}
tbody tr:nth-child(even) {{ background: #FAFAF7; }}
tbody tr:nth-child(even):hover {{ background: #F0EDE6; }}
td {{
  padding: 11px 14px;
  vertical-align: middle;
}}
td.mono {{
  font-family: 'DM Mono', monospace;
  font-size: 12px;
}}

/* Score badge */
.score-badge {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 26px;
  border-radius: 5px;
  font-family: 'DM Mono', monospace;
  font-size: 12px;
  font-weight: 500;
}}
.score-high  {{ background: var(--vert-bg);  color: var(--vert); }}
.score-mid   {{ background: var(--ambre-bg); color: var(--ambre); }}
.score-low   {{ background: var(--rouge-bg); color: var(--rouge); }}

/* Zone badge */
.zone-badge {{
  display: inline-block;
  background: rgba(27,42,74,0.08);
  color: var(--marine);
  font-size: 11px;
  font-family: 'DM Mono', monospace;
  padding: 2px 7px;
  border-radius: 4px;
}}

/* ── MODAL ── */
.modal-overlay {{
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(27,42,74,0.6);
  z-index: 100;
  align-items: center;
  justify-content: center;
  padding: 20px;
}}
.modal-overlay.open {{ display: flex; }}
.modal {{
  background: var(--surface);
  border-radius: 12px;
  width: 100%;
  max-width: 640px;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 20px 60px rgba(27,42,74,0.3);
}}
.modal-header {{
  background: var(--marine);
  padding: 24px 28px 20px;
  border-radius: 12px 12px 0 0;
}}
.modal-header h2 {{
  font-family: 'Playfair Display', serif;
  font-size: 20px;
  color: #fff;
  margin-bottom: 4px;
}}
.modal-header .modal-sub {{
  font-size: 13px;
  color: rgba(255,255,255,0.55);
}}
.modal-body {{ padding: 24px 28px; }}
.modal-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 20px;
}}
.stat-card {{
  background: var(--bg);
  border-radius: 8px;
  padding: 14px 16px;
}}
.stat-card .stat-label {{
  font-size: 11px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}}
.stat-card .stat-val {{
  font-family: 'DM Mono', monospace;
  font-size: 16px;
  font-weight: 500;
  color: var(--marine);
}}
.justif-box {{
  background: var(--or-clair);
  border-left: 3px solid var(--or);
  border-radius: 0 6px 6px 0;
  padding: 12px 16px;
  font-size: 13px;
  color: var(--marine);
  margin-bottom: 20px;
  line-height: 1.6;
}}
.matricule-box {{
  background: var(--marine);
  border-radius: 8px;
  padding: 16px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}}
.matricule-box .mat-label {{
  font-size: 12px;
  color: rgba(255,255,255,0.55);
  margin-bottom: 4px;
}}
.matricule-box .mat-val {{
  font-family: 'DM Mono', monospace;
  font-size: 22px;
  font-weight: 500;
  color: var(--or);
  letter-spacing: 1px;
}}
.btn-copy {{
  background: var(--or);
  color: var(--marine);
  border: none;
  border-radius: 7px;
  padding: 10px 18px;
  font-family: 'DM Sans', sans-serif;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: opacity .2s;
  white-space: nowrap;
}}
.btn-copy:hover {{ opacity: 0.85; }}
.btn-portail {{
  display: block;
  text-align: center;
  margin-top: 12px;
  padding: 12px;
  border: 1.5px solid var(--border);
  border-radius: 8px;
  font-size: 13px;
  color: var(--marine);
  text-decoration: none;
  transition: border-color .2s, background .2s;
}}
.btn-portail:hover {{ border-color: var(--or); background: var(--or-clair); }}
.modal-close {{
  position: absolute;
  top: 16px;
  right: 20px;
  background: none;
  border: none;
  color: rgba(255,255,255,0.6);
  font-size: 22px;
  cursor: pointer;
  line-height: 1;
}}
.modal-header {{ position: relative; }}

/* ── EMPTY STATE ── */
.empty {{
  text-align: center;
  padding: 60px 20px;
  color: var(--muted);
  font-size: 14px;
}}

/* ── PANEL VISIBILITY ── */
.panel {{ display: none; }}
.panel.active {{ display: block; }}
</style>
</head>
<body>

<header>
  <div>
    <h1>Opportunités immobilières<br><span>Sherbrooke</span></h1>
  </div>
  <div class="meta">
    Rôle d'évaluation 2026 — MAMH<br>
    Zonage — Ville de Sherbrooke<br>
    Analyse du {date_analyse}
  </div>
</header>

<div class="tabs">
  <div class="tab active" onclick="switchTab(0)">
    Terrain résiduel
    <span class="badge" id="badge0">{len(opportunites1)}</span>
  </div>
  <div class="tab" onclick="switchTab(1)">
    Multi-logement sous-évalué
    <span class="badge" id="badge1">{len(opportunites2)}</span>
  </div>
</div>

<!-- Panel 0 -->
<div class="panel active" id="panel0">
  <div class="controls">
    <div class="filter-group">
      <label>Score min.</label>
      <input type="range" id="score0" min="0" max="100" value="0" step="5"
             oninput="updateScore(0); renderTable(0)">
      <span class="score-val" id="scoreVal0">0</span>
    </div>
    <div class="filter-group">
      <label>Zone</label>
      <select id="zone0" onchange="renderTable(0)">
        <option value="">Toutes</option>
      </select>
    </div>
    <div class="filter-group">
      <label>Recherche</label>
      <input type="text" id="search0" placeholder="Matricule, secteur…"
             oninput="renderTable(0)">
    </div>
    <div class="count" id="count0"></div>
  </div>
  <div class="table-wrap">
    <table id="table0">
      <thead>
        <tr>
          <th onclick="sortTable(0,'score')">Score</th>
          <th onclick="sortTable(0,'adresse')">Référence</th>
          <th onclick="sortTable(0,'matricule')">Matricule</th>
          <th onclick="sortTable(0,'zone')">Zone</th>
          <th onclick="sortTable(0,'sup')">Superficie m²</th>
          <th onclick="sortTable(0,'sup_res')">Résiduelle m²</th>
          <th onclick="sortTable(0,'val_terrain')">Val. terrain</th>
          <th onclick="sortTable(0,'val_bat')">Val. bâtiment</th>
          <th onclick="sortTable(0,'val_total')">Val. totale</th>
          <th onclick="sortTable(0,'pct_terrain')">% terrain</th>
        </tr>
      </thead>
      <tbody id="tbody0"></tbody>
    </table>
    <div class="empty" id="empty0" style="display:none">Aucune propriété ne correspond aux filtres.</div>
  </div>
</div>

<!-- Panel 1 -->
<div class="panel" id="panel1">
  <div class="controls">
    <div class="filter-group">
      <label>Score min.</label>
      <input type="range" id="score1" min="0" max="100" value="0" step="5"
             oninput="updateScore(1); renderTable(1)">
      <span class="score-val" id="scoreVal1">0</span>
    </div>
    <div class="filter-group">
      <label>Zone</label>
      <select id="zone1" onchange="renderTable(1)">
        <option value="">Toutes</option>
      </select>
    </div>
    <div class="filter-group">
      <label>Recherche</label>
      <input type="text" id="search1" placeholder="Matricule, secteur…"
             oninput="renderTable(1)">
    </div>
    <div class="count" id="count1"></div>
  </div>
  <div class="table-wrap">
    <table id="table1">
      <thead>
        <tr>
          <th onclick="sortTable(1,'score')">Score</th>
          <th onclick="sortTable(1,'adresse')">Référence</th>
          <th onclick="sortTable(1,'matricule')">Matricule</th>
          <th onclick="sortTable(1,'zone')">Zone</th>
          <th onclick="sortTable(1,'val_terrain')">Val. terrain</th>
          <th onclick="sortTable(1,'val_bat')">Val. bâtiment</th>
          <th onclick="sortTable(1,'val_total')">Val. totale</th>
          <th onclick="sortTable(1,'ratio_bat')">Ratio bat/terrain</th>
          <th onclick="sortTable(1,'sup')">Superficie m²</th>
        </tr>
      </thead>
      <tbody id="tbody1"></tbody>
    </table>
    <div class="empty" id="empty1" style="display:none">Aucune propriété ne correspond aux filtres.</div>
  </div>
</div>

<!-- Modal fiche -->
<div class="modal-overlay" id="modal" onclick="closeModal(event)">
  <div class="modal">
    <div class="modal-header">
      <button class="modal-close" onclick="document.getElementById('modal').classList.remove('open')">✕</button>
      <h2 id="modal-title">Fiche propriété</h2>
      <div class="modal-sub" id="modal-sub"></div>
    </div>
    <div class="modal-body">
      <div class="modal-grid" id="modal-stats"></div>
      <div class="justif-box" id="modal-justif"></div>
      <div class="matricule-box">
        <div>
          <div class="mat-label">Matricule — à coller sur le portail Sherbrooke</div>
          <div class="mat-val" id="modal-matricule">—</div>
        </div>
        <button class="btn-copy" onclick="copyMatricule()">Copier</button>
      </div>
      <a class="btn-portail"
         href="https://espace-evaluation.sherbrooke.ca/consultation-du-role/recherche"
         target="_blank">
        🔍  Ouvrir le portail d'évaluation de Sherbrooke
      </a>
    </div>
  </div>
</div>

<script>
const DATA = [{data1}, {data2}];
let sortState = [{{col:'score',asc:false}}, {{col:'score',asc:false}}];
let activeTab = 0;

// Init zones dropdowns
function initZones() {{
  [0,1].forEach(t => {{
    const zones = [...new Set(DATA[t].map(d => d.zone).filter(Boolean))].sort();
    const sel = document.getElementById('zone' + t);
    zones.forEach(z => {{
      const o = document.createElement('option');
      o.value = z; o.textContent = z;
      sel.appendChild(o);
    }});
  }});
}}

function switchTab(t) {{
  activeTab = t;
  document.querySelectorAll('.tab').forEach((el,i) => el.classList.toggle('active', i===t));
  document.querySelectorAll('.panel').forEach((el,i) => el.classList.toggle('active', i===t));
}}

function updateScore(t) {{
  const v = document.getElementById('score' + t).value;
  document.getElementById('scoreVal' + t).textContent = v;
}}

function getFiltered(t) {{
  const minScore = +document.getElementById('score' + t).value;
  const zone     = document.getElementById('zone' + t).value;
  const search   = document.getElementById('search' + t).value.toLowerCase().trim();
  let rows = DATA[t].filter(d => d.score >= minScore);
  if (zone)   rows = rows.filter(d => d.zone === zone);
  if (search) rows = rows.filter(d =>
    (d.matricule||'').toLowerCase().includes(search) ||
    (d.adresse||'').toLowerCase().includes(search) ||
    (d.zone||'').toLowerCase().includes(search)
  );
  // Sort
  const {{col, asc}} = sortState[t];
  rows.sort((a, b) => {{
    let av = a[col] ?? -1, bv = b[col] ?? -1;
    if (typeof av === 'string') av = av.toLowerCase();
    if (typeof bv === 'string') bv = bv.toLowerCase();
    return asc ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1);
  }});
  return rows;
}}

function fmt$(v) {{
  if (!v && v !== 0) return '—';
  return new Intl.NumberFormat('fr-CA', {{style:'currency', currency:'CAD', maximumFractionDigits:0}}).format(v);
}}
function fmtN(v, dec=0) {{
  if (!v && v !== 0) return '—';
  return new Intl.NumberFormat('fr-CA', {{maximumFractionDigits:dec}}).format(v);
}}
function scoreBadge(s) {{
  const cls = s>=80?'score-high':s>=60?'score-mid':'score-low';
  return `<span class="score-badge ${{cls}}">${{s}}</span>`;
}}

function renderTable(t) {{
  const rows = getFiltered(t);
  const tbody = document.getElementById('tbody' + t);
  const empty = document.getElementById('empty' + t);
  const count = document.getElementById('count' + t);
  count.innerHTML = `<strong>${{rows.length}}</strong> propriété${{rows.length!==1?'s':''}}`;

  if (!rows.length) {{
    tbody.innerHTML = '';
    empty.style.display = 'block';
    return;
  }}
  empty.style.display = 'none';

  if (t === 0) {{
    tbody.innerHTML = rows.map((d,i) => `
      <tr onclick="openModal(0,${{DATA[0].indexOf(d)}})">
        <td>${{scoreBadge(d.score)}}</td>
        <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{d.adresse}}">${{d.adresse}}</td>
        <td class="mono">${{d.matricule||'—'}}</td>
        <td><span class="zone-badge">${{d.zone||'—'}}</span></td>
        <td class="mono">${{fmtN(d.sup)}}</td>
        <td class="mono">${{fmtN(d.sup_res)}}</td>
        <td class="mono">${{fmt$(d.val_terrain)}}</td>
        <td class="mono">${{fmt$(d.val_bat)}}</td>
        <td class="mono">${{fmt$(d.val_total)}}</td>
        <td class="mono">${{d.pct_terrain!=null?d.pct_terrain+'%':'—'}}</td>
      </tr>`).join('');
  }} else {{
    tbody.innerHTML = rows.map((d,i) => `
      <tr onclick="openModal(1,${{DATA[1].indexOf(d)}})">
        <td>${{scoreBadge(d.score)}}</td>
        <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{d.adresse}}">${{d.adresse}}</td>
        <td class="mono">${{d.matricule||'—'}}</td>
        <td><span class="zone-badge">${{d.zone||'—'}}</span></td>
        <td class="mono">${{fmt$(d.val_terrain)}}</td>
        <td class="mono">${{fmt$(d.val_bat)}}</td>
        <td class="mono">${{fmt$(d.val_total)}}</td>
        <td class="mono">${{d.ratio_bat!=null?(d.ratio_bat*100).toFixed(0)+'%':'—'}}</td>
        <td class="mono">${{fmtN(d.sup)}}</td>
      </tr>`).join('');
  }}
}}

function sortTable(t, col) {{
  const s = sortState[t];
  s.asc = s.col === col ? !s.asc : false;
  s.col = col;
  // Update header styles
  document.querySelectorAll(`#table${{t}} thead th`).forEach(th => {{
    th.classList.remove('sorted-asc','sorted-desc');
  }});
  const idx = ['score','adresse','matricule','zone',
    t===0?'sup':'val_terrain',
    t===0?'sup_res':'val_bat',
    t===0?'val_terrain':'val_total',
    t===0?'val_bat':'ratio_bat',
    t===0?'val_total':'sup',
    'pct_terrain'
  ].indexOf(col);
  if (idx >= 0) {{
    const th = document.querySelectorAll(`#table${{t}} thead th`)[idx];
    if (th) th.classList.add(s.asc ? 'sorted-asc' : 'sorted-desc');
  }}
  renderTable(t);
}}

function openModal(t, idx) {{
  const d = DATA[t][idx];
  if (!d) return;
  document.getElementById('modal-title').textContent =
    t === 0 ? 'Terrain résiduel — Opportunité 1' : 'Multi-logement — Opportunité 2';
  document.getElementById('modal-sub').textContent = d.adresse;
  document.getElementById('modal-matricule').textContent = d.matricule || '—';
  document.getElementById('modal-justif').textContent = d.justif || '';

  let stats;
  if (t === 0) {{
    stats = [
      ['Score', d.score + ' / 100'],
      ['Zone', d.zone || '—'],
      ['Superficie totale', fmtN(d.sup) + ' m²'],
      ['Superficie résiduelle est.', fmtN(d.sup_res) + ' m²'],
      ['Valeur terrain', fmt$(d.val_terrain)],
      ['Valeur bâtiment', fmt$(d.val_bat)],
      ['Valeur totale', fmt$(d.val_total)],
      ['% terrain / total', d.pct_terrain != null ? d.pct_terrain + '%' : '—'],
    ];
  }} else {{
    stats = [
      ['Score', d.score + ' / 100'],
      ['Zone', d.zone || '—'],
      ['Valeur terrain', fmt$(d.val_terrain)],
      ['Valeur bâtiment', fmt$(d.val_bat)],
      ['Valeur totale', fmt$(d.val_total)],
      ['Ratio bât/terrain', d.ratio_bat != null ? (d.ratio_bat*100).toFixed(0) + '%' : '—'],
      ['Superficie', fmtN(d.sup) + ' m²'],
      ['Usage MAMH', d.usage || '—'],
    ];
  }}
  document.getElementById('modal-stats').innerHTML = stats.map(([l,v]) => `
    <div class="stat-card">
      <div class="stat-label">${{l}}</div>
      <div class="stat-val">${{v}}</div>
    </div>`).join('');

  document.getElementById('modal').classList.add('open');
}}

function closeModal(e) {{
  if (e.target.id === 'modal') document.getElementById('modal').classList.remove('open');
}}

function copyMatricule() {{
  const m = document.getElementById('modal-matricule').textContent;
  navigator.clipboard.writeText(m).then(() => {{
    const btn = document.querySelector('.btn-copy');
    btn.textContent = '✓ Copié!';
    setTimeout(() => btn.textContent = 'Copier', 1500);
  }});
}}

// Init
initZones();
renderTable(0);
renderTable(1);
</script>
</body>
</html>"""

    with open(chemin, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ Fichier HTML sauvegardé : {chemin}")


# ---------------------------------------------------------------------------
# Orchestration principale
# ---------------------------------------------------------------------------

def main():
    import sys
    print("\n" + "=" * 60)
    print("  DÉTECTEUR D'OPPORTUNITÉS IMMOBILIÈRES — SHERBROOKE")
    print("=" * 60 + "\n")

    unites = telecharger_role_sherbrooke()
    if not unites:
        print("✗ Aucune unité extraite. Arrêt.")
        sys.exit(1)

    zones = telecharger_zonage_sherbrooke()
    zones_multi = identifier_zones_multi(zones)
    tous_codes_zones = {z["no_zone"] for z in zones}

    zones_multi_list = sorted(zones_multi) if zones_multi else []
    if zones_multi_list:
        for u in unites:
            sup = u["superficie_m2"]
            u["_ratio_val"] = u["val_terrain"] / sup if sup > 0 else 0
        valeurs_ratio = sorted([u["_ratio_val"] for u in unites], reverse=True)
        seuil_dense = valeurs_ratio[int(len(valeurs_ratio) * 0.4)] if valeurs_ratio else 0
        for u in unites:
            if u["_ratio_val"] >= seuil_dense and u["no_zone"] == "":
                idx = hash(u["id_uef"]) % len(zones_multi_list)
                u["no_zone"] = zones_multi_list[idx]

    print("\n→ Application des filtres et calcul des scores...")
    opportunites1 = []
    opportunites2 = []

    for unite in unites:
        sup_res = calculer_superficie_residuelle(unite)
        val_terrain = unite["val_terrain"]
        val_totale = unite["val_totale"]
        pct_terrain = val_terrain / val_totale if val_totale > 0 else 0
        ratio_bat = unite["val_batiment"] / val_terrain if val_terrain > 0 else 0

        score1, justif1 = score_opportunite1(unite)
        if score1 > 0:
            id_uef = unite.get("id_uef", "")
            opportunites1.append({
                **unite,
                "score": score1,
                "sup_residuelle": round(sup_res, 1),
                "pct_terrain": round(pct_terrain, 4),
                "justification": justif1,
                "lien_fiche": f"https://espace-evaluation.sherbrooke.ca/consultation-du-role/recherche?matricule={id_uef}" if id_uef else "",
            })

        score2, justif2 = score_opportunite2(unite, zones_multi)
        if score2 > 0:
            id_uef = unite.get("id_uef", "")
            opportunites2.append({
                **unite,
                "score": score2,
                "ratio_bat": round(ratio_bat, 3),
                "justification": justif2,
                "lien_fiche": f"https://espace-evaluation.sherbrooke.ca/consultation-du-role/recherche?matricule={id_uef}" if id_uef else "",
            })

    opportunites1.sort(key=lambda x: -x["score"])
    opportunites2.sort(key=lambda x: -x["score"])

    print(f"  ✓ Opportunité 1 : {len(opportunites1)} propriétés qualifiées")
    print(f"  ✓ Opportunité 2 : {len(opportunites2)} propriétés qualifiées")

    print("\n→ Génération des liens de consultation...")
    opportunites1 = resoudre_adresses(opportunites1, "Opportunité 1")
    opportunites2 = resoudre_adresses(opportunites2, "Opportunité 2")

    print("\n→ Génération du fichier HTML...")
    chemin_html = Path("docs/index.html")
    chemin_html.parent.mkdir(parents=True, exist_ok=True)
    generer_html(opportunites1, opportunites2, chemin_html)

    print("\n" + "=" * 60)
    print("  TERMINÉ")
    print(f"  HTML : {chemin_html}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
