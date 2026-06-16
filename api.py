"""
api.py
======
Gekapselter Zugriff auf die oeffentliche Hyperliquid Info-API.

Alle Lese-Operationen laufen ueber einen einzigen POST-Endpoint:
    POST https://api.hyperliquid.xyz/info   (Content-Type: application/json)

Es ist kein API-Key noetig. Wir muessen aber die Rate-Limits respektieren:
- Requests werden global gethrottlet (Mindestabstand zwischen zwei Calls).
- Bei HTTP 429 (Too Many Requests) wird mit exponentiellem Backoff erneut versucht.
"""

import time
import threading
import requests

INFO_URL = "https://api.hyperliquid.xyz/info"

# --- Throttling-Konfiguration -------------------------------------------------
# Mindestabstand zwischen zwei Requests (Sekunden). ~0.15s => ca. 6-7 Req/s.
MIN_REQUEST_INTERVAL = 0.15
# Backoff-Einstellungen bei 429 / Netzwerkfehlern.
MAX_RETRIES = 5
BACKOFF_BASE = 0.8          # Sekunden, wird je Versuch verdoppelt
REQUEST_TIMEOUT = 15        # Sekunden

# Globaler Lock + Zeitstempel, damit alle Threads denselben Throttle teilen.
_throttle_lock = threading.Lock()
_last_request_ts = 0.0

# Eine wiederverwendbare Session (Connection-Pooling).
_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})


def _throttle() -> None:
    """Sorgt dafuer, dass zwischen zwei Requests mindestens MIN_REQUEST_INTERVAL liegt."""
    global _last_request_ts
    with _throttle_lock:
        now = time.monotonic()
        wait = MIN_REQUEST_INTERVAL - (now - _last_request_ts)
        if wait > 0:
            time.sleep(wait)
        _last_request_ts = time.monotonic()


def _post(payload: dict):
    """
    Fuehrt einen POST gegen den Info-Endpoint aus.
    Mit Throttling und exponentiellem Backoff bei 429/Netzwerkfehlern.
    Gibt das geparste JSON zurueck oder wirft nach MAX_RETRIES eine Exception.
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            resp = _session.post(INFO_URL, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                # Rate-Limit getroffen -> warten und erneut versuchen.
                wait = BACKOFF_BASE * (2 ** attempt)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            wait = BACKOFF_BASE * (2 ** attempt)
            time.sleep(wait)
    # Alle Versuche fehlgeschlagen.
    raise RuntimeError(f"Hyperliquid-API-Request fehlgeschlagen ({payload.get('type')}): {last_exc}")


# --- Konkrete Endpoints -------------------------------------------------------

def get_meta() -> dict:
    """Metadaten aller Perp-Maerkte. universe -> Liste der Coins."""
    return _post({"type": "meta"})


def get_all_coins() -> list[str]:
    """Bequeme Hilfsfunktion: gibt die Liste aller Perp-Coin-Namen zurueck."""
    meta = get_meta()
    return [u["name"] for u in meta.get("universe", [])]


def get_clearinghouse_state(address: str) -> dict:
    """Offene Positionen + Margin-Zusammenfassung einer Wallet (Perps)."""
    return _post({"type": "clearinghouseState", "user": address})


def get_portfolio(address: str):
    """
    Portfolio-/PnL-Historie einer Wallet. Format ist je nach Account unterschiedlich,
    daher wird das Roh-JSON zurueckgegeben und in analysis.py defensiv ausgewertet.
    """
    return _post({"type": "portfolio", "user": address})


def get_user_fills_by_time(address: str, start_ms: int, end_ms: int,
                           max_fills: int = 5000) -> list[dict]:
    """
    Holt alle Fills (Teilausfuehrungen) einer Wallet im Zeitfenster [start_ms, end_ms].

    Der Endpoint liefert pro Aufruf nur einen Block (max. ~500-2000 Eintraege),
    deshalb paginieren wir: der Zeitstempel des letzten Fills wird als naechste
    startTime verwendet, bis keine neuen Fills mehr kommen oder max_fills erreicht ist.
    """
    fills: list[dict] = []
    cursor = start_ms
    seen_keys: set = set()  # gegen Endlosschleifen / Duplikate an Blockgrenzen

    while cursor <= end_ms and len(fills) < max_fills:
        batch = _post({
            "type": "userFillsByTime",
            "user": address,
            "startTime": cursor,
            "endTime": end_ms,
        })
        if not isinstance(batch, list) or not batch:
            break

        new_in_batch = 0
        last_time = cursor
        for fill in batch:
            # Eindeutiger Schluessel zur Deduplizierung (hash + tid + time).
            key = (fill.get("hash"), fill.get("tid"), fill.get("time"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            fills.append(fill)
            new_in_batch += 1
            last_time = max(last_time, int(fill.get("time", cursor)))

        # Keine neuen Eintraege mehr -> fertig.
        if new_in_batch == 0:
            break
        # Cursor um 1ms hinter den letzten Fill setzen, um Ueberlappung zu vermeiden.
        next_cursor = last_time + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor

    return fills[:max_fills]
