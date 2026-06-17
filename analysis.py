"""
analysis.py
===========
Kernstueck: aus den Roh-Fills einer Wallet werden Kennzahlen berechnet, die
(a) zwischen Swing-Tradern und algorithmischen Bots unterscheiden und
(b) ein Hyperdash-aehnliches Trader-Profil ergeben (Win-Rate, PnL, Coins, ...).

WICHTIG: Wir scrapen Hyperdash NICHT. Alle Kennzahlen werden aus den oeffentlichen
Hyperliquid-Rohdaten selbst berechnet. Hyperdash dient in der UI nur als Klick-Link
zur manuellen Gegenpruefung.
"""

import time
import statistics
from collections import defaultdict

import api

# =============================================================================
# KONFIGURIERBARE SCHWELLENWERTE (hier zentral anpassen)
# =============================================================================
LOOKBACK_DAYS = 30                 # Analysezeitraum (30-90 sinnvoll; mehr = mehr API-Last)

# --- Algo-/HFT-Ausschlusskriterien ---
MAX_TRADES_PER_DAY = 60            # mehr Fills/Tag -> Algo
MIN_MEDIAN_HOLD_MINUTES = 30       # kuerzere mediane Haltedauer -> Scalping/HFT -> Algo
MIN_MEDIAN_INTERFILL_SEC = 20      # sehr kurze, regelmaessige Abstaende -> Bot-Verdacht
MAX_CONCURRENT_MARKETS = 12        # sehr viele parallele Maerkte + hohe Freq -> Market-Maker

# --- Swing-Bestaetigung ---
SWING_MIN_HOLD_HOURS = 4           # mediane Haltedauer hierueber -> klar Swing
REST_WINDOW_HOURS = 4              # taegliches Inaktivitaetsfenster (Mensch schlaeft)

# Toleranz, ab wann eine Position als "flat" (geschlossen) gilt.
FLAT_EPS = 1e-6

# =============================================================================
# QUALITAETS-GATES (harte Ausschlusskriterien) - hier zentral anpassen
# =============================================================================
# Eine Wallet muss ALLE folgenden Huerden bestehen, sonst gilt sie als nicht
# copy-tradewuerdig (quality_pass = False).
MIN_CLOSED_TRIPS = 8               # zu wenige abgeschlossene Trades -> Stichprobe wertlos
MIN_ACTIVE_DAYS = 7                # Historie muss mind. so viele Tage abdecken (= 1 Woche)
MIN_ACCOUNT_VALUE = 5000.0         # kleinere Konten ignorieren
MIN_PROFIT_FACTOR = 1.2            # Brutto-Gewinn / Brutto-Verlust muss klar > 1 sein
MAX_SINGLE_TRADE_SHARE = 0.5       # ein einzelner Trade darf nicht > 50% des Gewinns ausmachen
REQUIRE_POSITIVE_WINDOW = True     # Netto-PnL im Analysezeitraum muss positiv sein

# =============================================================================
# QUALITAETS-SCORE (0-100) - Gewichte nach Nutzer-Prioritaet
# Reihenfolge des Nutzers: 1. Win-Rate, 2. PnL, 3. Risiko, 4. Konstanz
# =============================================================================
WEIGHT_WINRATE = 0.40
WEIGHT_PNL = 0.30
WEIGHT_RISK = 0.20
WEIGHT_CONSISTENCY = 0.10

# Ab so vielen abgeschlossenen Trades gilt die Win-Rate als statistisch belastbar.
# Darunter wird der Win-Rate-Beitrag anteilig gedaempft (Stichproben-Konfidenz).
CONFIDENCE_TRADES = 30
# Bei dieser Rendite (% auf Account-Value im Zeitraum) ist der PnL-Beitrag bei 100.
PNL_ROI_CAP_PCT = 50.0
# Bei diesem Drawdown (% auf Account-Value) faellt der Risiko-Beitrag auf 0.
RISK_DD_CAP_PCT = 50.0


# =============================================================================
# Hilfsfunktionen zum Auswerten der Fills
# =============================================================================

def _signed_size(fill: dict) -> float:
    """Vorzeichenbehaftete Groesse: Kauf (side 'B') = +, Verkauf (side 'A') = -."""
    sz = float(fill.get("sz", 0) or 0)
    return sz if fill.get("side") == "B" else -sz


def _reconstruct_trips(fills: list[dict]) -> list[dict]:
    """
    Rekonstruiert abgeschlossene Round-Trips (Position von flat -> ... -> flat) je Coin.

    Rueckgabe: Liste von Trips mit:
        coin, open_ts, close_ts, duration_sec, pnl (Summe closedPnl im Trip)

    Bei einem direkten Wechsel Long<->Short (Durchgang durch 0) wird der alte Trip
    geschlossen und sofort ein neuer eroeffnet.
    """
    by_coin: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        by_coin[f.get("coin", "?")].append(f)

    trips: list[dict] = []

    for coin, coin_fills in by_coin.items():
        coin_fills.sort(key=lambda x: int(x.get("time", 0)))
        pos = 0.0
        open_ts = None
        trip_pnl = 0.0

        for f in coin_fills:
            prev = pos
            pos += _signed_size(f)
            trip_pnl += float(f.get("closedPnl", 0) or 0)
            ts = int(f.get("time", 0))

            # Position aus flat eroeffnet.
            if abs(prev) < FLAT_EPS and abs(pos) >= FLAT_EPS:
                open_ts = ts
                trip_pnl = float(f.get("closedPnl", 0) or 0)
                continue

            # Position wieder flat -> Trip abgeschlossen.
            if abs(prev) >= FLAT_EPS and abs(pos) < FLAT_EPS:
                if open_ts is not None:
                    trips.append({
                        "coin": coin,
                        "open_ts": open_ts,
                        "close_ts": ts,
                        "duration_sec": max(0, (ts - open_ts) / 1000.0),
                        "pnl": trip_pnl,
                    })
                open_ts = None
                trip_pnl = 0.0
                continue

            # Vorzeichenwechsel (Long->Short oder umgekehrt, Durchgang durch 0).
            if prev * pos < 0:
                if open_ts is not None:
                    trips.append({
                        "coin": coin,
                        "open_ts": open_ts,
                        "close_ts": ts,
                        "duration_sec": max(0, (ts - open_ts) / 1000.0),
                        "pnl": trip_pnl,
                    })
                open_ts = ts  # neuer Trip beginnt sofort
                trip_pnl = 0.0

    return trips


def _max_concurrent_markets(trips: list[dict]) -> int:
    """Maximale Anzahl gleichzeitig offener Maerkte (Ueberlappung der Trip-Intervalle)."""
    if not trips:
        return 0
    events = []
    for t in trips:
        events.append((t["open_ts"], 1))
        events.append((t["close_ts"], -1))
    # Bei gleichem Zeitstempel zuerst schliessen (-1) vor oeffnen (+1).
    events.sort(key=lambda e: (e[0], e[1]))
    cur = 0
    peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def _has_daily_rest_window(fills: list[dict]) -> bool:
    """
    Prueft, ob es ueber den Tag (UTC-Stunden) ein zusammenhaengendes Inaktivitaetsfenster
    von mind. REST_WINDOW_HOURS gibt -> spricht fuer einen Menschen statt 24/7-Bot.
    """
    if not fills:
        return True
    active_hours = set()
    for f in fills:
        ts = int(f.get("time", 0)) / 1000.0
        hour = time.gmtime(ts).tm_hour
        active_hours.add(hour)

    # Laengsten zusammenhaengenden Block inaktiver Stunden im 24h-Kreis finden.
    inactive = [h for h in range(24) if h not in active_hours]
    if not inactive:
        return False
    longest = 0
    run = 0
    # Kreis doppelt durchlaufen, um Uebergang 23->0 abzudecken.
    for h in range(48):
        if (h % 24) in active_hours:
            run = 0
        else:
            run += 1
            longest = max(longest, run)
    longest = min(longest, 24)
    return longest >= REST_WINDOW_HOURS


# =============================================================================
# Klassifizierung Swing vs. Algo
# =============================================================================

def classify(metrics: dict) -> str:
    """
    Regel-Kaskade auf Basis der berechneten Metriken.
    Rueckgabe: "Algo" | "Swing" | "Unklar".
    """
    # Ohne genug Daten keine Aussage.
    if metrics["num_fills"] < 5 or metrics["num_trips"] < 2:
        return "Unklar"

    # --- harte Ausschlusskriterien fuer Algo/HFT ---
    if metrics["trades_per_day"] > MAX_TRADES_PER_DAY:
        return "Algo"
    if metrics["median_hold_minutes"] is not None and \
            metrics["median_hold_minutes"] < MIN_MEDIAN_HOLD_MINUTES:
        return "Algo"
    if metrics["median_interfill_sec"] is not None and \
            metrics["median_interfill_sec"] < MIN_MEDIAN_INTERFILL_SEC:
        return "Algo"
    if metrics["max_concurrent_markets"] > MAX_CONCURRENT_MARKETS and \
            metrics["trades_per_day"] > MAX_TRADES_PER_DAY / 2:
        return "Algo"

    # --- Swing-Bestaetigung ---
    if metrics["median_hold_minutes"] is not None and \
            metrics["median_hold_minutes"] >= SWING_MIN_HOLD_HOURS * 60 and \
            metrics["has_rest_window"]:
        return "Swing"

    return "Unklar"


# =============================================================================
# Risiko + Qualitaet
# =============================================================================

def _max_drawdown_pct(trips: list[dict], account_value: float) -> float:
    """
    Maximaler Drawdown der realisierten Equity-Kurve (aus geschlossenen Trips),
    ausgedrueckt in % des aktuellen Account-Value. Naeherung, da wir nur
    realisierte PnL kennen, kein laufendes Equity-Tracking.
    """
    if not trips or account_value <= 0:
        return 0.0
    ordered = sorted(trips, key=lambda t: t["close_ts"])
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in ordered:
        cum += t["pnl"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)  # absoluter Ruecksetzer vom Hoch
    return max_dd / account_value * 100.0


def _single_trade_share(trips: list[dict]) -> float:
    """
    Anteil des groessten Einzelgewinns am gesamten Brutto-Gewinn.
    Nahe 1.0 -> ein einziger Trade traegt fast den ganzen Gewinn (Glueckstreffer).
    """
    wins = [t["pnl"] for t in trips if t["pnl"] > 0]
    if not wins:
        return 1.0
    return max(wins) / sum(wins)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def quality_gate(m: dict) -> tuple[bool, list[str]]:
    """
    Harte Ausschlusskriterien. Rueckgabe: (besteht, [gruende_fuer_ausschluss]).
    Eine Wallet, die hier durchfaellt, ist NICHT copy-tradewuerdig.
    """
    reasons = []
    if m["num_closed_trips"] < MIN_CLOSED_TRIPS:
        reasons.append(f"zu wenige Trades ({m['num_closed_trips']} < {MIN_CLOSED_TRIPS})")
    if m["active_days"] < MIN_ACTIVE_DAYS:
        reasons.append(f"Historie zu kurz ({m['active_days']:.1f}d < {MIN_ACTIVE_DAYS}d)")
    if m["account_value"] < MIN_ACCOUNT_VALUE:
        reasons.append(f"Konto zu klein ({m['account_value']:.0f}$ < {MIN_ACCOUNT_VALUE:.0f}$)")
    if REQUIRE_POSITIVE_WINDOW and m["pnl_window"] <= 0:
        reasons.append("im Minus / nicht profitabel")
    if m["profit_factor"] is None or m["profit_factor"] < MIN_PROFIT_FACTOR:
        pf = "n/a" if m["profit_factor"] is None else f"{m['profit_factor']:.2f}"
        reasons.append(f"Profit-Faktor zu niedrig ({pf} < {MIN_PROFIT_FACTOR})")
    if m["single_trade_share"] > MAX_SINGLE_TRADE_SHARE:
        reasons.append(f"ein Trade dominiert ({m['single_trade_share']*100:.0f}% des Gewinns)")
    return (len(reasons) == 0, reasons)


def quality_score(m: dict) -> float:
    """
    Zusammengesetzter Qualitaets-Score 0-100, gewichtet nach Nutzer-Prioritaet:
    Win-Rate > PnL > Risiko > Konstanz.

    Wichtig: Die Win-Rate wird mit einer Stichproben-Konfidenz multipliziert, damit
    eine 90%-Quote aus nur 6 Trades NICHT ueber einer soliden 65%-Quote aus 40
    Trades landet. So bleibt die Win-Rate die wichtigste Groesse, ohne dass
    Gluecksstichproben gewinnen.
    """
    # 1) Win-Rate mit Konfidenz
    wr = m["win_rate"] if m["win_rate"] is not None else 0.0
    confidence = min(1.0, m["num_closed_trips"] / CONFIDENCE_TRADES)
    win_component = _clamp(wr * confidence)

    # 2) PnL als Rendite (% auf Account-Value), gedeckelt
    roi = m["roi_pct"]
    pnl_component = _clamp(roi / PNL_ROI_CAP_PCT * 100.0)

    # 3) Risiko: weniger Drawdown = mehr Punkte; Liquidation deckelt hart
    risk_component = _clamp(100.0 - m["max_drawdown_pct"] / RISK_DD_CAP_PCT * 100.0)
    if m["had_liquidation"]:
        risk_component = min(risk_component, 20.0)

    # 4) Konstanz: Gewinn breit gestreut + genug Trades + beide Zeitfenster positiv
    spread = _clamp((1.0 - m["single_trade_share"]) * 100.0)
    count = _clamp(m["num_closed_trips"] / CONFIDENCE_TRADES * 100.0)
    both_windows = 100.0 if (m["pnl_7d"] >= 0 and m["pnl_30d"] > 0) else 50.0
    consistency_component = _clamp(0.5 * spread + 0.3 * count + 0.2 * both_windows)

    total = (
        WEIGHT_WINRATE * win_component
        + WEIGHT_PNL * pnl_component
        + WEIGHT_RISK * risk_component
        + WEIGHT_CONSISTENCY * consistency_component
    )
    return round(total, 1)


# =============================================================================
# Vollstaendiges Trader-Profil (Hyperdash-artig)
# =============================================================================

def build_profile(address: str) -> dict:
    """
    Holt Fills + Positionen + Portfolio einer Wallet und berechnet das komplette
    Profil inkl. Klassifizierung. Wirft KEINE Exception nach aussen, sondern
    liefert bei Fehlern ein dict mit "error".
    """
    try:
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - LOOKBACK_DAYS * 24 * 3600 * 1000

        fills = api.get_user_fills_by_time(address, start_ms, now_ms)
        ch_state = api.get_clearinghouse_state(address)

        # --- Basismetriken ---
        num_fills = len(fills)
        days = max(1.0, LOOKBACK_DAYS)
        trades_per_day = num_fills / days

        # Zeit zwischen aufeinanderfolgenden Fills (Median).
        times = sorted(int(f.get("time", 0)) for f in fills)
        interfill = [(b - a) / 1000.0 for a, b in zip(times, times[1:]) if b > a]
        median_interfill = statistics.median(interfill) if interfill else None

        # --- Round-Trips fuer Haltedauer, Win-Rate, PnL ---
        trips = _reconstruct_trips(fills)
        durations_min = [t["duration_sec"] / 60.0 for t in trips]
        median_hold = statistics.median(durations_min) if durations_min else None

        wins = [t for t in trips if t["pnl"] > 0]
        losses = [t for t in trips if t["pnl"] < 0]
        closed_trips = [t for t in trips if abs(t["pnl"]) > 0]
        win_rate = (len(wins) / len(closed_trips) * 100.0) if closed_trips else None

        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

        # --- realisierter PnL aus closedPnl der Fills ---
        def realized_since(ms_ago: int) -> float:
            cutoff = now_ms - ms_ago
            return sum(float(f.get("closedPnl", 0) or 0)
                       for f in fills if int(f.get("time", 0)) >= cutoff)

        pnl_7d = realized_since(7 * 24 * 3600 * 1000)
        pnl_30d = realized_since(30 * 24 * 3600 * 1000)
        pnl_total_window = sum(float(f.get("closedPnl", 0) or 0) for f in fills)

        # --- gehandelte Coins (Haeufigkeit) ---
        coin_counts: dict[str, int] = defaultdict(int)
        for f in fills:
            coin_counts[f.get("coin", "?")] += 1
        favorite_coins = sorted(coin_counts.items(), key=lambda x: x[1], reverse=True)

        # --- aktuelle offene Positionen + Account-Value ---
        account_value = float(
            ch_state.get("marginSummary", {}).get("accountValue", 0) or 0
        )
        open_positions = []
        for ap in ch_state.get("assetPositions", []):
            pos = ap.get("position", {})
            szi = float(pos.get("szi", 0) or 0)
            if abs(szi) < FLAT_EPS:
                continue
            open_positions.append({
                "coin": pos.get("coin", "?"),
                "richtung": "Long" if szi > 0 else "Short",
                "groesse": abs(szi),
                "entry": float(pos.get("entryPx", 0) or 0),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
            })

        max_concurrent = _max_concurrent_markets(trips)
        has_rest = _has_daily_rest_window(fills)

        # --- Qualitaets-Metriken ---
        # Abgedeckte Historie in Tagen (erster bis letzter Fill).
        if times and times[-1] > times[0]:
            active_days = (times[-1] - times[0]) / (24 * 3600 * 1000.0)
        else:
            active_days = 0.0

        # Rendite im Zeitraum relativ zum aktuellen Account-Value.
        roi_pct = (pnl_total_window / account_value * 100.0) if account_value > 0 else 0.0

        max_dd_pct = _max_drawdown_pct(trips, account_value)
        single_share = _single_trade_share(trips)

        # Wurde die Wallet im Zeitraum liquidiert? (Heuristik auf dem dir-Feld)
        had_liquidation = any(
            "liquidat" in str(f.get("dir", "")).lower() for f in fills
        )

        metrics = {
            "num_fills": num_fills,
            "num_trips": len(trips),
            "trades_per_day": trades_per_day,
            "median_hold_minutes": median_hold,
            "median_interfill_sec": median_interfill,
            "max_concurrent_markets": max_concurrent,
            "has_rest_window": has_rest,
        }
        klassifizierung = classify(metrics)

        # --- Qualitaets-Gate + Score ---
        qm = {
            "num_closed_trips": len(closed_trips),
            "active_days": active_days,
            "account_value": account_value,
            "pnl_window": pnl_total_window,
            "pnl_7d": pnl_7d,
            "pnl_30d": pnl_30d,
            "profit_factor": profit_factor,
            "single_trade_share": single_share,
            "win_rate": win_rate,
            "roi_pct": roi_pct,
            "max_drawdown_pct": max_dd_pct,
            "had_liquidation": had_liquidation,
        }
        quality_pass, quality_reasons = quality_gate(qm)
        score = quality_score(qm)

        return {
            "address": address,
            "error": None,
            "klassifizierung": klassifizierung,
            "account_value": account_value,
            "trades_per_day": round(trades_per_day, 1),
            "median_hold_minutes": round(median_hold, 1) if median_hold is not None else None,
            "median_interfill_sec": round(median_interfill, 1) if median_interfill is not None else None,
            "max_concurrent_markets": max_concurrent,
            "has_rest_window": has_rest,
            "win_rate": round(win_rate, 1) if win_rate is not None else None,
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
            "pnl_7d": round(pnl_7d, 2),
            "pnl_30d": round(pnl_30d, 2),
            "pnl_window": round(pnl_total_window, 2),
            "num_trips": len(trips),
            "num_closed_trips": len(closed_trips),
            "favorite_coins": favorite_coins,
            "open_positions": open_positions,
            # --- Qualitaet ---
            "score": score,
            "quality_pass": quality_pass,
            "quality_reasons": quality_reasons,
            "active_days": round(active_days, 1),
            "roi_pct": round(roi_pct, 1),
            "max_drawdown_pct": round(max_dd_pct, 1),
            "single_trade_share": round(single_share, 2),
            "had_liquidation": had_liquidation,
        }

    except Exception as exc:  # noqa: BLE001 - pro Wallet kapseln, Scan nie abbrechen
        return {"address": address, "error": str(exc)}
