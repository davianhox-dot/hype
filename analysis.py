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
        }

    except Exception as exc:  # noqa: BLE001 - pro Wallet kapseln, Scan nie abbrechen
        return {"address": address, "error": str(exc)}
