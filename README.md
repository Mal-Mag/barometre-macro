# Baromètre macro-financier

Score de 0 à 100 qui synthétise l'environnement économique et financier en 4 piliers
(croissance 30 %, inflation 20 %, marchés 30 %, risque 20 %) à partir de 10 indicateurs
collectés automatiquement sur FRED.

## Contenu du dossier

| Fichier | Rôle |
|---|---|
| `barometre.py` | Collecte les données FRED, les normalise, calcule le score → écrit `data.js` |
| `index.html` | Le tableau de bord (lit `data.js`) |
| `data.js` | Les données calculées (la version livrée est en **mode démo**, à régénérer) |
| `requirements.txt` | Les librairies Python nécessaires |
| `.github/workflows/update.yml` | Mise à jour automatique chaque lundi (optionnel) |

## 1. Générer les vraies données (5 minutes)

1. Installer Python 3.10 ou plus récent (python.org), en cochant « Add Python to PATH ».
2. Ouvrir un terminal dans ce dossier, puis :
   ```
   pip install -r requirements.txt
   python barometre.py
   ```
3. Le terminal affiche le score du mois et écrit `data.js`.
4. Ouvrir `index.html` dans le navigateur (double-clic) : le bandeau « Mode démo » a disparu.

Aucune clé API n'est nécessaire : le script télécharge directement les CSV publics de FRED.
Si une série est indisponible, le score est calculé avec les autres et la page l'indique.

## 2. Mettre le site en ligne (lien URL demandé par le prof)

**Option rapide : Netlify Drop (2 minutes, sans compte GitHub)**
1. Aller sur https://app.netlify.com/drop et créer un compte gratuit.
2. Glisser-déposer **le dossier entier** sur la page.
3. Netlify donne une URL (renommable dans *Site configuration → Change site name*).
4. Pour mettre à jour : relancer `python barometre.py`, puis re-déposer le dossier.

**Option complète : GitHub + Netlify (mise à jour automatique, plus valorisable)**
1. Créer un dépôt GitHub et y envoyer tout le dossier.
2. Dans *Settings → Actions → General*, autoriser « Read and write permissions ».
3. Sur Netlify : *Add new site → Import an existing project → GitHub*, choisir le dépôt
   (pas de commande de build, dossier de publication : `/`).
4. Chaque lundi, GitHub Actions relance le script, met `data.js` à jour, et Netlify republie.
   On peut aussi le lancer à la main : onglet *Actions → Mise à jour du baromètre → Run workflow*.

## 3. Méthode (à reprendre dans le PDF)

- **Collecte** : 10 séries FRED, alignées en mensuel (fin de mois). Séries journalières :
  moyenne du mois. PIB trimestriel : reporté sur 3 mois maximum.
- **Transformations** : glissement annuel (PIB, CPI, IPCH, Brent), écart absolu à la cible de
  2 % (inflations), écart du S&P 500 à sa moyenne mobile 200 jours.
- **Normalisation** : Z-score glissant sur 10 ans (uniquement le passé, pas de biais
  d'anticipation), borné à ±3, puis note = (Z + 3) / 6 × 100.
- **Inversion** : chômage, inflations (écart à la cible), Brent, VIX et spread de crédit,
  pour que note haute = bonne santé.
- **Agrégation** : poids égaux au sein d'un pilier, puis 30/20/30/20 entre piliers.
- **Robustesse** : comparaison avec une variante Min-Max et avec des piliers à poids égaux.
- **Lecture** : 0-30 Stress, 30-45 Prudence, 45-55 Neutre, 55-70 Favorable, 70-100 Euphorie.
- **Dates de publication** : le PIB (daté au 1er jour du trimestre par FRED) est décalé de 3 mois,
  pour n'être « connu » qu'à sa publication. Une donnée publiée en retard est reportée 3 mois au plus.
- **Test sur le passé** : 4 épisodes de crise fixés à l'avance (bulle internet, crise financière,
  Covid, inflation 2022). Alerte = score < 45 (bas de la bande Neutre, non optimisé), décalé de 1 mois.
  Comparaison à trois règles simples (courbe inversée, VIX > 25, indice sous sa MM 200 j) et
  sensibilité au seuil. Limites : séries révisées (pas de temps réel), 3-4 crises seulement.
- **Série actions** : FRED ne diffuse que quelques années de `SP500` ; si l'historique commence
  après 2000, le script bascule automatiquement sur `NASDAQCOM` (historique complet) et le signale
  sur le site. Le terminal affiche la première date de chaque série : à vérifier.

Tous les paramètres (poids, fenêtre, bornes, indicateurs) sont en haut de `barometre.py`.

## Sources

| Indicateur | Série FRED |
|---|---|
| PIB réel US | GDPC1 |
| Confiance des consommateurs (Michigan) | UMCSENT |
| Taux de chômage US | UNRATE |
| Inflation US (CPI) | CPIAUCSL |
| Inflation zone euro (IPCH) | CP0000EZ19M086NEST |
| S&P 500 | SP500 |
| Pente 10 ans − 2 ans | T10Y2Y |
| Pétrole Brent | DCOILBRENTEU |
| VIX | VIXCLS |
| Spread Baa − Treasury 10 ans | BAA10Y |


## Console de pondérations

La page contient des curseurs (poids des 4 piliers, seuil d'alerte, poids de chaque indicateur). Le score, les graphiques et le test sur le passé sont recalculés dans le navigateur, sans relancer le script. Le réglage est conservé dans l'adresse de la page (#p=...), donc partageable. Ajuster les poids après avoir regardé le test sur le passé revient à optimiser sur l'échantillon : la page l'indique.
