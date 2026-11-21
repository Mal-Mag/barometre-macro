"""
Baromètre synthétique macro-financier
=====================================
Collecte automatique (FRED, sans clé API), normalisation, pondération et score global.
Produit data.js, lu par index.html.

Usage :
    python barometre.py          # données réelles (connexion internet requise)
    python barometre.py --demo   # données simulées, pour tester le site hors ligne
"""

import io
import json
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------------------
# 1. PARAMÈTRES
# ---------------------------------------------------------------------------

START = "1995-01-01"          # début de la collecte
SCORE_START = "2000-01-31"    # début de l'affichage du score
WINDOW = 120                  # fenêtre glissante du Z-score : 120 mois = 10 ans
MIN_PERIODS = 36              # au moins 3 ans d'historique pour calculer un Z-score
Z_CAP = 3.0                   # Z-score borné à ±3 pour neutraliser les chocs extrêmes
INFLATION_TARGET = 2.0        # cible des banques centrales, en %
CAL = None                    # calendrier mensuel commun, défini dans build()

PILLARS = {                   # poids des piliers (grille du cours)
    "croissance": {"label": "Croissance", "weight": 0.30},
    "inflation": {"label": "Inflation", "weight": 0.20},
    "marches": {"label": "Marchés", "weight": 0.30},
    "risque": {"label": "Risque", "weight": 0.20},
}

# sign : +1 = une valeur haute est bonne ; -1 = une valeur haute est un risque (on inverse)
# transform : comment on passe de la série brute à la variable notée
INDICATORS = [
    {"id": "pib", "label": "PIB réel US (glissement annuel)", "pillar": "croissance",
     "fred": "GDPC1", "freq": "Q", "transform": "yoy", "sign": +1, "unit": "%",
     "why": "Mesure directe de l'activité économique."},
    {"id": "confiance", "label": "Confiance des consommateurs (Michigan)", "pillar": "croissance",
     "fred": "UMCSENT", "freq": "M", "transform": "level", "sign": +1, "unit": "pts",
     "why": "Indicateur avancé : la confiance précède la consommation."},
    {"id": "chomage", "label": "Taux de chômage US", "pillar": "croissance",
     "fred": "UNRATE", "freq": "M", "transform": "level", "sign": -1, "unit": "%",
     "why": "Santé du marché du travail ; un chômage élevé est un signal négatif."},
    {"id": "infl_us", "label": "Inflation US (CPI, glissement annuel)", "pillar": "inflation",
     "fred": "CPIAUCSL", "freq": "M", "transform": "yoy_gap", "sign": -1, "unit": "%",
     "why": "Écart à la cible de 2 % de la Fed : plus il est grand, plus la politique monétaire devra réagir."},
    {"id": "infl_ez", "label": "Inflation zone euro (IPCH, glissement annuel)", "pillar": "inflation",
     "fred": "CP0000EZ19M086NEST", "freq": "M", "transform": "yoy_gap", "sign": -1, "unit": "%",
     "why": "Écart à la cible de 2 % de la BCE."},
    {"id": "sp500", "label": "S&P 500 vs moyenne mobile 200 jours", "pillar": "marches",
     "fred": "SP500", "freq": "D", "transform": "ma200_gap", "sign": +1, "unit": "%",
     "fallback": "NASDAQCOM", "fallback_label": "Nasdaq Composite vs moyenne mobile 200 jours",
     "why": "Tendance du marché actions : au-dessus de sa moyenne 200 j, le marché est haussier."},
    {"id": "pente", "label": "Pente de la courbe des taux (10 ans − 2 ans)", "pillar": "marches",
     "fred": "T10Y2Y", "freq": "D", "transform": "level", "sign": +1, "unit": "pts",
     "why": "Une courbe inversée (pente négative) a précédé les récessions US."},
    {"id": "brent", "label": "Pétrole Brent (variation sur 1 an)", "pillar": "marches",
     "fred": "DCOILBRENTEU", "freq": "D", "transform": "yoy", "sign": -1, "unit": "%",
     "why": "Une flambée du pétrole pèse sur les coûts et l'inflation."},
    {"id": "vix", "label": "Volatilité implicite (VIX)", "pillar": "risque",
     "fred": "VIXCLS", "freq": "D", "transform": "level", "sign": -1, "unit": "pts",
     "why": "« Indice de la peur » : stress anticipé sur 30 jours."},
    {"id": "spread", "label": "Spread de crédit (Baa − Treasury 10 ans)", "pillar": "risque",
     "fred": "BAA10Y", "freq": "D", "transform": "level", "sign": -1, "unit": "pts",
     "why": "Prime de risque exigée sur la dette d'entreprise : elle s'écarte quand le risque monte."},
]

# Chaque indicateur pèse le même poids au sein de son pilier
for ind in INDICATORS:
    n = sum(1 for i in INDICATORS if i["pillar"] == ind["pillar"])
    ind["weight"] = PILLARS[ind["pillar"]]["weight"] / n

# Test sur le passé : épisodes de crise définis À L'AVANCE à partir de repères officiels
# (récessions NBER, baisses de plus de 20 % du S&P 500), jamais ajustés après coup.
CRISES = [
    {"id": "bulle", "label": "Éclatement de la bulle internet", "start": "2000-03", "end": "2002-10",
     "type": "Baisse du S&P 500 > 20 % (récession NBER 2001)"},
    {"id": "gfc", "label": "Crise financière mondiale", "start": "2007-10", "end": "2009-06",
     "type": "Récession NBER + baisse du S&P 500 > 50 %"},
    {"id": "covid", "label": "Choc Covid-19", "start": "2020-02", "end": "2020-04",
     "type": "Récession NBER + baisse du S&P 500 > 30 %"},
    {"id": "inflation", "label": "Inflation et remontée des taux", "start": "2022-01", "end": "2022-10",
     "type": "Baisse du S&P 500 > 20 %"},
]
BT_LAG = 1              # le score d'un mois n'est connu qu'un mois plus tard : on décale de 1 mois
BT_HORIZON = 12         # une alerte est « juste » si une crise démarre dans les 12 mois
BT_THRESHOLD = 45       # seuil d'alerte = bas de la bande « Neutre » (non optimisé sur le passé)
BT_THRESHOLDS = [30, 35, 40, 45, 50]
BT_MIN_MONTHS = 6       # fenêtre d'avant-crise exploitable si au moins 6 mois de score disponibles

BANDS = [  # grille de lecture du score (0-100)
    (0, 30, "Stress", "Sous-pondérer les actions, privilégier les actifs défensifs"),
    (30, 45, "Prudence", "Réduire le risque du portefeuille"),
    (45, 55, "Neutre", "Rester sur l'allocation cible"),
    (55, 70, "Favorable", "Surpondérer les actions"),
    (70, 101, "Euphorie", "Prendre des profits : gare au retour à la moyenne"),
]


# ---------------------------------------------------------------------------
# 2. COLLECTE
# ---------------------------------------------------------------------------

MANUAL_DIR = "csv_manuels"   # secours : y déposer SERIE.csv téléchargé à la main depuis FRED
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _parse_fred_csv(text: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(text)).iloc[:, :2]
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")  # les "." de FRED deviennent NaN
    s = df.dropna().set_index("date")["value"]
    return s[s.index >= START]


CACHE_USED = []   # séries pour lesquelles le téléchargement a échoué et où la dernière copie enregistrée a servi


def fetch_fred(series_id: str) -> pd.Series:
    """Télécharge une série FRED au format CSV (aucune clé API nécessaire).

    Ordre : téléchargement (2 essais, en-têtes de navigateur) ; en cas d'échec, dernière copie
    enregistrée dans csv_manuels/ (mise à jour à chaque téléchargement réussi, ou déposée à la main).
    """
    import os
    import time
    os.makedirs(MANUAL_DIR, exist_ok=True)
    path = os.path.join(MANUAL_DIR, f"{series_id}.csv")
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    last_err = None
    for attempt in range(1, 3):
        try:
            r = requests.get(url, timeout=(10, 40), headers={"User-Agent": BROWSER_UA, "Accept": "text/csv,*/*"})
            r.raise_for_status()
            s = _parse_fred_csv(r.text)
            if len(s) < 10:
                raise ValueError("série vide")
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(r.text)
            return s
        except Exception as e:
            last_err = e
            print(f"      essai {attempt}/2 échoué pour {series_id} ({type(e).__name__})", flush=True)
            time.sleep(2 * attempt)
    if os.path.exists(path):
        print(f"      copie enregistrée utilisée pour {series_id}", flush=True)
        CACHE_USED.append(series_id)
        with open(path, encoding="utf-8") as f:
            return _parse_fred_csv(f.read())
    raise last_err


def demo_series(ind: dict, rng: np.random.Generator) -> pd.Series:
    """Série simulée plausible, uniquement pour tester le site sans connexion."""
    if ind["freq"] == "D":
        idx = pd.bdate_range(START, "2026-10-27")
    elif ind["freq"] == "Q":
        idx = pd.date_range(START, "2026-07-01", freq="QS")
    else:
        idx = pd.date_range(START, "2026-09-01", freq="MS")
    n = len(idx)
    t = np.arange(n) / n
    crisis = np.zeros(n)
    for c in ("2001-09-01", "2008-10-01", "2020-03-15", "2022-06-15"):
        crisis += np.exp(-((idx - pd.Timestamp(c)).days.values / 120.0) ** 2)
    noise = rng.normal(0, 1, n).cumsum() / np.sqrt(n) * 3
    base = {
        "GDPC1": lambda: 15000 * np.exp(0.022 * t * 31 - 0.06 * np.convolve(crisis, np.ones(4) / 4, "same")),
        "UMCSENT": lambda: 85 - 25 * crisis + noise * 3,
        "UNRATE": lambda: 5.2 + 4 * crisis + noise * 0.4,
        "CPIAUCSL": lambda: 150 * np.exp(0.025 * t * 31 + 0.05 * (idx.year >= 2022)),
        "CP0000EZ19M086NEST": lambda: 70 * np.exp(0.02 * t * 31 + 0.06 * (idx.year >= 2022)),
        "SP500": lambda: 1000 * np.exp(0.07 * t * 31 - 0.45 * crisis + noise * 0.05),
        "T10Y2Y": lambda: 1.0 + noise * 0.4 - 0.8 * (idx.year >= 2022) * (idx.year <= 2024),
        "DCOILBRENTEU": lambda: 20 * np.exp(0.05 * t * 31 - 0.5 * crisis + noise * 0.1),
        "VIXCLS": lambda: 17 + 35 * crisis + np.abs(noise) * 2,
        "BAA10Y": lambda: 1.9 + 2.5 * crisis + noise * 0.15,
    }[ind["fred"]]()
    return pd.Series(base, index=idx)


# ---------------------------------------------------------------------------
# 3. TRANSFORMATION, NORMALISATION, AGRÉGATION
# ---------------------------------------------------------------------------

def to_monthly(raw: pd.Series, ind: dict) -> pd.Series:
    """Ramène chaque série au rythme mensuel (fin de mois) et applique la transformation."""
    s = raw.copy()
    if ind["freq"] == "Q":
        # FRED date le PIB au 1er jour du trimestre, mais il n'est publié qu'après la fin du trimestre :
        # on le décale de 3 mois pour qu'il ne soit « connu » qu'à partir de sa publication.
        s.index = s.index + pd.DateOffset(months=3)
    if ind["transform"] == "ma200_gap":            # calculé sur les données journalières
        s = (s / s.rolling(200, min_periods=150).mean() - 1) * 100
    if ind["freq"] == "D":
        s = s.resample("ME").mean() if ind["transform"] != "ma200_gap" else s.resample("ME").last()
    else:
        s = s.resample("ME").last()
    if ind["transform"] in ("yoy", "yoy_gap"):
        lag = 4 if ind["freq"] == "Q" else 12
        if ind["freq"] == "Q":
            s = s.dropna()
            s = (s / s.shift(lag) - 1) * 100
            s = s.resample("ME").last()
        else:
            s = (s / s.shift(lag) - 1) * 100
    if ind["transform"] == "yoy_gap":
        s = (s - INFLATION_TARGET).abs()             # écart absolu à la cible de 2 %
    # report de la dernière valeur connue (PIB trimestriel, publications en retard), 3 mois max
    return s.ffill(limit=3)


def zscore(s: pd.Series) -> pd.Series:
    """Z-score glissant : on ne compare qu'au passé, jamais aux données futures."""
    mu = s.rolling(WINDOW, min_periods=MIN_PERIODS).mean()
    sd = s.rolling(WINDOW, min_periods=MIN_PERIODS).std()
    return ((s - mu) / sd).clip(-Z_CAP, Z_CAP)


def minmax(s: pd.Series) -> pd.Series:
    """Variante Min-Max glissante (0-1), pour le test de robustesse."""
    lo = s.rolling(WINDOW, min_periods=MIN_PERIODS).min()
    hi = s.rolling(WINDOW, min_periods=MIN_PERIODS).max()
    return (s - lo) / (hi - lo)


def weighted(scores: pd.DataFrame, weights: dict) -> pd.Series:
    """Moyenne pondérée ligne par ligne ; si un indicateur manque, les poids restants sont renormalisés."""
    w = pd.Series(weights)[scores.columns]
    mask = scores.notna()
    num = (scores.fillna(0) * w).sum(axis=1)
    den = (mask * w).sum(axis=1)
    out = num / den
    out[den < 0.5 * w.sum()] = np.nan   # on exige au moins la moitié des poids
    return out


def band(score: float) -> dict:
    for lo, hi, name, action in BANDS:
        if lo <= score < hi:
            return {"name": name, "action": action}
    return {"name": "n.d.", "action": ""}


def backtest(glob: pd.Series, R: pd.DataFrame) -> dict:
    """Le score aurait-il alerté avant les crises ? Comparaison à des règles simples.

    Le signal d'un mois n'utilise que des données connues un mois plus tôt (BT_LAG).
    Limite assumée : les séries FRED sont les versions révisées (pas de données « temps réel »).
    """
    idx = glob.index
    months = idx.strftime("%Y-%m")
    crisis = pd.Series(False, index=idx)
    for c in CRISES:
        crisis.loc[(months >= c["start"]) & (months <= c["end"])] = True
    # crise en cours, ou qui démarre dans les BT_HORIZON prochains mois
    fwd = crisis.astype(float)[::-1].rolling(BT_HORIZON + 1, min_periods=1).max()[::-1].astype(bool)
    resolved = pd.Series(False, index=idx)             # on écarte les 12 derniers mois : avenir inconnu
    resolved.iloc[: max(0, len(idx) - BT_HORIZON)] = True

    def score_signal(th):
        s = glob.shift(BT_LAG)
        return s < th, s.notna()

    signals = {"score": (f"Score du baromètre (< {BT_THRESHOLD})",) + score_signal(BT_THRESHOLD)}
    if "pente" in R.columns:
        p = R["pente"].shift(BT_LAG)
        signals["pente"] = ("Courbe des taux inversée (10 ans − 2 ans < 0)", p < 0, p.notna())
    if "vix" in R.columns:
        v = R["vix"].shift(BT_LAG)
        signals["vix"] = ("VIX moyen du mois > 25", v > 25, v.notna())
    if "sp500" in R.columns:
        t = R["sp500"].shift(BT_LAG)
        signals["tendance"] = ("Indice actions sous sa moyenne mobile 200 j", t < 0, t.notna())

    def episode_result(alert, avail, c):
        start = pd.Timestamp(c["start"] + "-01") + pd.offsets.MonthEnd(0)
        win = (idx > start - pd.DateOffset(months=BT_HORIZON)) & (idx <= start) & avail.values
        evaluable = int(win.sum()) >= BT_MIN_MONTHS
        hits = alert.values & win
        lead = None
        if evaluable and hits.any():
            first = idx[hits][0]
            lead = int((start.year - first.year) * 12 + start.month - first.month)
        return {"evaluable": bool(evaluable), "hit": bool(evaluable and hits.any()), "lead": lead}

    def summary(alert, avail):
        ok = (avail & resolved).values
        a = alert.values & ok
        n_alert = int(a.sum())
        false = int((a & ~fwd.values).sum())
        return {
            "n_alert": n_alert,
            "alert_share": round(float(n_alert / ok.sum()), 3) if ok.sum() else None,
            "false_rate": round(false / n_alert, 3) if n_alert else None,
        }

    episodes = [{**c, "signals": {k: episode_result(a, av, c) for k, (_, a, av) in signals.items()}} for c in CRISES]

    rules = []
    for k, (label, a, av) in signals.items():
        ev = [e["signals"][k] for e in episodes if e["signals"][k]["evaluable"]]
        rules.append({"id": k, "label": label, "hits": sum(1 for e in ev if e["hit"]), "n": len(ev), **summary(a, av)})

    sens = []
    for th in BT_THRESHOLDS:
        a, av = score_signal(th)
        ev = [episode_result(a, av, c) for c in CRISES]
        ev = [e for e in ev if e["evaluable"]]
        sens.append({"threshold": th, "hits": sum(1 for e in ev if e["hit"]), "n": len(ev), **summary(a, av)})

    return {"lag": BT_LAG, "horizon": BT_HORIZON, "min_months": BT_MIN_MONTHS, "threshold": BT_THRESHOLD,
            "episodes": episodes, "rules": rules, "sensitivity": sens}


def drivers_3m(S: pd.DataFrame, last, prev) -> dict:
    """Ce qui explique la variation du score sur 3 mois : variation de poids × note, par indicateur."""
    rows = []
    for i in INDICATORS:
        if i["id"] in S.columns:
            d = i["weight"] * (S[i["id"]].get(last, np.nan) - S[i["id"]].get(prev, np.nan))
            if not pd.isna(d):
                rows.append({"label": i["label"], "delta": round(float(d), 2)})
    rows.sort(key=lambda r: r["delta"])
    return {"up": [r for r in rows[::-1][:2] if r["delta"] >= 0.05],
            "down": [r for r in rows[:2] if r["delta"] <= -0.05]}


def build(demo: bool) -> dict:
    rng = np.random.default_rng(7)
    raw_m, z, mm, errors, notes = {}, {}, {}, [], []
    global CAL
    CAL = pd.date_range(START, pd.Timestamp.today().normalize() + pd.offsets.MonthEnd(0), freq="ME")
    for ind in INDICATORS:
        try:
            raw = demo_series(ind, rng) if demo else fetch_fred(ind["fred"])
            # Historique trop court (ex. S&P 500 sur FRED) : on bascule sur la série de secours
            if not demo and ind.get("fallback") and raw.index[0] > pd.Timestamp("2000-01-01"):
                short = raw.index[0]
                try:
                    alt = fetch_fred(ind["fallback"])
                    notes.append(f"{ind['fred']} ne remonte qu'à {short:%m/%Y} : remplacé par {ind['fallback']} "
                                 f"pour disposer de l'historique complet (tests sur 2000, 2008, 2020).")
                    ind["fred"], ind["label"] = ind["fallback"], ind["fallback_label"]
                    raw = alt
                except Exception as e:
                    notes.append(f"{ind['fred']} ne remonte qu'à {short:%m/%Y} sur FRED et la série de secours "
                                 f"{ind['fallback']} est indisponible : la tendance actions n'est donc calculée "
                                 f"que depuis {short.year + 1}. Les crises antérieures sont marquées « non évaluable » "
                                 f"pour cet indicateur.")
            m = to_monthly(raw, ind)
            # calendrier commun jusqu'au mois en cours : une donnée publiée en retard (chômage, CPI, PIB)
            # est reportée au plus 3 mois, sinon le dernier mois serait calculé avec des piliers manquants
            m = m.reindex(CAL).ffill(limit=3)
            raw_m[ind["id"]] = m
            z[ind["id"]] = zscore(m) * ind["sign"]          # inversion des indicateurs de risque
            mm_s = minmax(m)
            mm[ind["id"]] = mm_s if ind["sign"] > 0 else 1 - mm_s
            print(f"  ok  {ind['fred']:<20} {raw.index[0]:%Y-%m} -> {m.dropna().index[-1]:%Y-%m}")
        except Exception as e:  # une source en panne ne bloque pas tout le baromètre
            errors.append(f"{ind['fred']} : {e}")
            print(f"  ERREUR {ind['fred']} : {e}")

    if CACHE_USED:
        notes.append("Téléchargement FRED impossible lors de cette mise à jour pour : " + ", ".join(sorted(set(CACHE_USED))) +
                     ". La dernière copie enregistrée de ces séries a été utilisée.")
    Z = pd.DataFrame(z)
    idx = Z.index[Z.index >= SCORE_START]
    Z = Z.loc[idx]
    S = (Z + Z_CAP) / (2 * Z_CAP) * 100                      # passage sur 0-100
    MM = pd.DataFrame(mm).loc[idx] * 100
    R = pd.DataFrame(raw_m).loc[idx]

    w_ind = {i["id"]: i["weight"] for i in INDICATORS if i["id"] in S.columns}
    pillar_scores = {}
    for p in PILLARS:
        cols = [i["id"] for i in INDICATORS if i["pillar"] == p and i["id"] in S.columns]
        pillar_scores[p] = weighted(S[cols], {c: 1 for c in cols}) if cols else pd.Series(np.nan, index=idx)
    P = pd.DataFrame(pillar_scores)
    glob = weighted(P, {p: v["weight"] for p, v in PILLARS.items()})
    # on n'affiche un score que si tous les piliers disponibles sont renseignés
    need = [p for p in P.columns if P[p].notna().any()]
    glob[~P[need].notna().all(axis=1)] = np.nan

    # tests de robustesse
    glob_mm = weighted(MM, w_ind)
    glob_eq = weighted(S, {c: 1 for c in S.columns})

    valid = glob.dropna()
    last = valid.index[-1]
    prev = valid.index[max(0, len(valid) - 4)]
    contrib = []
    for i in INDICATORS:
        if i["id"] not in S.columns:
            continue
        sc = S[i["id"]].get(last, np.nan)
        contrib.append({
            "id": i["id"], "label": i["label"], "pillar": PILLARS[i["pillar"]]["label"],
            "score": None if pd.isna(sc) else round(float(sc), 1),
            "raw": None if pd.isna(R[i["id"]].get(last, np.nan)) else round(float(R[i["id"]][last]), 2),
            "contribution": None if pd.isna(sc) else round(float(i["weight"] * (sc - 50)), 2),
        })

    def ser(s):
        return [None if pd.isna(v) else round(float(v), 2) for v in s.reindex(idx)]

    corr = pd.DataFrame({"zscore": glob, "minmax": glob_mm, "egal": glob_eq}).dropna().corr()

    return {
        "meta": {
            "updated": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
            "last_month": last.strftime("%m/%Y"),
            "demo": demo,
            "errors": errors,
            "notes": notes,
            "window_years": WINDOW // 12,
            "z_cap": Z_CAP,
        },
        "dates": [d.strftime("%Y-%m") for d in idx],
        "global": ser(glob),
        "pillars": {p: {"label": v["label"], "weight": v["weight"], "values": ser(P[p])} for p, v in PILLARS.items()},
        "indicators": [{
            "id": i["id"], "label": i["label"], "pillar": PILLARS[i["pillar"]]["label"],
            "source": f"FRED {i['fred']}", "url": f"https://fred.stlouisfed.org/series/{i['fred']}",
            "transform": i["transform"], "sign": i["sign"], "unit": i["unit"], "weight": round(i["weight"], 4),
            "why": i["why"],
            "scores": ser(S[i["id"]]) if i["id"] in S.columns else [],
        } for i in INDICATORS],
        "robustness": {
            "minmax": ser(glob_mm), "egal": ser(glob_eq),
            "corr_minmax": round(float(corr.loc["zscore", "minmax"]), 2),
            "corr_egal": round(float(corr.loc["zscore", "egal"]), 2),
        },
        "current": {
            "score": round(float(glob[last]), 1),
            "change_3m": round(float(glob[last] - glob[prev]), 1),
            "band": band(float(glob[last])),
            "pillars": {p: None if pd.isna(P[p][last]) else round(float(P[p][last]), 1) for p in PILLARS},
            "contributions": sorted(contrib, key=lambda c: c["contribution"] if c["contribution"] is not None else 0),
            "drivers_3m": drivers_3m(S, last, prev),
        },
        "backtest": backtest(glob, R),
        "bands": [{"lo": lo, "hi": min(hi, 100), "name": n, "action": a} for lo, hi, n, a in BANDS],
    }


if __name__ == "__main__":
    demo = "--demo" in sys.argv
    print("Collecte des données" + (" (mode démo)" if demo else " depuis FRED") + "…")
    data = build(demo)
    with open("data.js", "w", encoding="utf-8") as f:
        f.write("window.BAROMETRE = ")
        json.dump(data, f, ensure_ascii=False)
        f.write(";\n")
    c = data["current"]
    print(f"\nScore global ({data['meta']['last_month']}) : {c['score']} / 100 -> {c['band']['name']}")
    print("Piliers :", ", ".join(f"{PILLARS[p]['label']} {v}" for p, v in c["pillars"].items()))
    print("data.js écrit. Ouvre index.html pour voir le tableau de bord.")
