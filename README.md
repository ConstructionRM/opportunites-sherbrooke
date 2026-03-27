# Opportunités immobilières — Sherbrooke

Outil de détection automatique d'opportunités immobilières à Sherbrooke (Québec), basé sur les données publiques du gouvernement du Québec.

## Sources de données

- **Rôle d'évaluation foncière 2026** — MAMH / Données Québec
- **Zonage municipal** — Ville de Sherbrooke / ArcGIS REST

## Deux types d'opportunités détectées

**Opportunité 1 — Terrain résiduel constructible**
Propriétés résidentielles dont la superficie de terrain permet potentiellement la construction d'une seconde résidence.

**Opportunité 2 — Propriété sous-évaluée en zone multi-logement**
Propriétés situées en zone autorisant le multi-logement où la valeur du bâtiment est inférieure à la valeur du terrain.

## Mise à jour automatique

L'analyse se relance automatiquement chaque trimestre (1er janvier, avril, juillet, octobre).

Pour déclencher une mise à jour manuelle :
1. Aller dans l'onglet **Actions** du dépôt
2. Cliquer sur **Analyse opportunités Sherbrooke**
3. Cliquer sur **Run workflow**

## Utilisation du rapport

Pour obtenir l'adresse civique d'une propriété :
1. Copier le **matricule** affiché dans la fiche
2. Aller sur [espace-evaluation.sherbrooke.ca](https://espace-evaluation.sherbrooke.ca/consultation-du-role/recherche)
3. Coller dans le champ « Par matricule »

> ⚠️ Cet outil est un aide à la présélection. Toujours valider le zonage exact sur [cartes.ville.sherbrooke.qc.ca](https://cartes.ville.sherbrooke.qc.ca) avant tout engagement.

## Licence

Données gouvernementales sous licence CC-BY 4.0 — Gouvernement du Québec.
