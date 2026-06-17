"""
collector.py
============
Live-Wallet-Discovery ueber den oeffentlichen Trade-Feed von Hyperliquid.

Idee:
- Wir abonnieren per WebSocket den "trades"-Channel fuer JEDEN Perp-Coin.
- Jede Trade-Nachricht enthaelt das Feld "users" = [adresse_a, adresse_b]
  (beide Seiten des Trades: Maker + Taker).
- Beide Adressen + der Zeitstempel werden in eine SQLite-Tabelle geschrieben.
- Die Streamlit-UI liest spaeter aus dieser Tabelle ("zuletzt aktive Wallets").

Der Collector laeuft in einem eigenen Daemon-Thread und ist von der UI ueber die
SQLite-Datei sauber entkoppelt.
"""

import json
import time
import sqlite3
import threading

import websocket  # aus dem Paket "websocket-client"

import api

WS_URL = "wss://api.hyperliquid.xyz/ws"
DB_PATH = "wallets.db"

# Hyperliquid erlaubt max. 1000 WS-Subscriptions pro IP. Wir bleiben sicher darunter.
MAX_SUBSCRIPTIONS = 950


# =============================================================================
# SQLite-Hilfsfunktionen (thread-sicher durch Verbindung-pro-Aufruf + WAL-Modus)
# =============================================================================

def _connect() -> sqlite3.Connection:
    """Oeffnet eine SQLite-Verbindung im WAL-Modus (erlaubt parallel Lesen/Schreiben)."""
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    """Legt die Tabelle seen_wallets an, falls noch nicht vorhanden."""
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_wallets (
                address      TEXT PRIMARY KEY,
                last_seen_ts INTEGER NOT NULL,
                hit_count    INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _upsert_wallet(conn: sqlite3.Connection, address: str, ts_ms: int) -> None:
    """
    Fuegt eine Adresse ein oder aktualisiert sie:
    - last_seen_ts auf den neuesten Zeitstempel setzen
    - hit_count um 1 erhoehen
    """
    conn.execute(
        """
        INSERT INTO seen_wallets (address, last_seen_ts, hit_count)
        VALUES (?, ?, 1)
        ON CONFLICT(address) DO UPDATE SET
            last_seen_ts = MAX(last_seen_ts, excluded.last_seen_ts),
            hit_count    = hit_count + 1
        """,
        (address.lower(), ts_ms),
    )


def get_recent_wallets(within_minutes: int) -> list[dict]:
    """
    Liest alle Wallets, die innerhalb der letzten `within_minutes` aktiv waren.
    Rueckgabe: Liste von dicts mit address, last_seen_ts, hit_count.
    """
    cutoff_ms = int((time.time() - within_minutes * 60) * 1000)
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT address, last_seen_ts, hit_count
            FROM seen_wallets
            WHERE last_seen_ts >= ?
            ORDER BY last_seen_ts DESC
            """,
            (cutoff_ms,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"address": r[0], "last_seen_ts": r[1], "hit_count": r[2]} for r in rows
    ]


def count_wallets() -> int:
    """Gesamtzahl bisher gesehener Wallets (fuer Status-Anzeige in der UI)."""
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM seen_wallets").fetchone()[0]
    finally:
        conn.close()


# =============================================================================
# WebSocket-Collector
# =============================================================================

def burst_collect(duration_seconds: int = 30, coins: list[str] | None = None,
                  progress_cb=None) -> dict[str, dict]:
    """
    Sammelt EINMALIG fuer `duration_seconds` Sekunden Adressen aus dem Trade-Feed.

    Im Gegensatz zum WalletCollector laeuft das synchron in einem einzigen Aufruf
    (oeffnen -> lauschen -> schliessen) und braucht KEINEN dauerhaften Hintergrund-
    Thread und KEINE persistente DB. Genau das passt zu Streamlit Community Cloud,
    wo Apps schlafen gelegt/neu gestartet werden.

    Rueckgabe: {address: {"count": int, "last_ts": int}}
    progress_cb(fortschritt_0_1, anzahl_wallets) wird optional regelmaessig aufgerufen.
    """
    if coins is None:
        coins = api.get_all_coins()[:MAX_SUBSCRIPTIONS]

    seen: dict[str, dict] = {}
    ws = websocket.create_connection(WS_URL, timeout=10)
    try:
        # Alle Coins abonnieren.
        for coin in coins:
            ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": coin},
            }))

        deadline = time.time() + duration_seconds
        while time.time() < deadline:
            remaining = deadline - time.time()
            ws.settimeout(max(0.1, min(remaining, 3)))
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                if progress_cb:
                    done = 1 - (deadline - time.time()) / duration_seconds
                    progress_cb(min(1.0, max(0.0, done)), len(seen))
                continue
            except Exception:
                break

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if data.get("channel") != "trades":
                continue

            for trade in data.get("data", []) or []:
                ts = int(trade.get("time", int(time.time() * 1000)))
                for addr in trade.get("users") or []:
                    if isinstance(addr, str) and addr.startswith("0x"):
                        a = addr.lower()
                        rec = seen.setdefault(a, {"count": 0, "last_ts": 0})
                        rec["count"] += 1
                        rec["last_ts"] = max(rec["last_ts"], ts)

            if progress_cb:
                done = 1 - (deadline - time.time()) / duration_seconds
                progress_cb(min(1.0, max(0.0, done)), len(seen))
    finally:
        try:
            ws.close()
        except Exception:
            pass

    return seen


class WalletCollector:
    """
    Haelt eine WebSocket-Verbindung offen, abonniert alle Coins und schreibt
    entdeckte Adressen in die SQLite-DB. Reconnectet automatisch bei Abbruch.
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._coins: list[str] = []
        # Eigene Schreib-Verbindung fuer den Collector-Thread.
        self._write_conn: sqlite3.Connection | None = None
        self._write_lock = threading.Lock()
        self.connected = False
        self.last_message_ts = 0.0

    # ---- oeffentliche Steuerung ----------------------------------------------

    def start(self) -> None:
        """Startet den Collector EINMAL in einem Daemon-Thread."""
        if self._thread and self._thread.is_alive():
            return
        init_db()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="WalletCollector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---- interner Ablauf ------------------------------------------------------

    def _run(self) -> None:
        """Endlosschleife: verbinden, lauschen, bei Abbruch nach kurzer Pause neu verbinden."""
        self._write_conn = _connect()
        # Coins einmalig laden (Reconnects nutzen die gecachte Liste weiter).
        try:
            self._coins = api.get_all_coins()[:MAX_SUBSCRIPTIONS]
        except Exception as exc:  # noqa: BLE001 - Collector soll nie hart sterben
            print(f"[Collector] Konnte Coin-Liste nicht laden: {exc}")
            self._coins = []

        while not self._stop.is_set():
            try:
                self._connect_and_listen()
            except Exception as exc:  # noqa: BLE001
                print(f"[Collector] Verbindungsfehler: {exc}")
            self.connected = False
            if self._stop.is_set():
                break
            # Laut Doku koennen Disconnects jederzeit ohne Ankuendigung passieren.
            time.sleep(3)  # kurz warten, dann reconnecten

    def _connect_and_listen(self) -> None:
        """Baut die WS-Verbindung auf und blockiert, bis sie abbricht."""

        def on_open(ws):
            self.connected = True
            print(f"[Collector] Verbunden, abonniere {len(self._coins)} Coins ...")
            for coin in self._coins:
                sub = {
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": coin},
                }
                ws.send(json.dumps(sub))

        def on_message(ws, message):
            self.last_message_ts = time.time()
            self._handle_message(message)

        def on_error(ws, error):
            print(f"[Collector] WS-Fehler: {error}")

        def on_close(ws, status_code, msg):
            self.connected = False

        ws_app = websocket.WebSocketApp(
            WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        # run_forever blockiert bis zum Disconnect; ping haelt die Verbindung lebendig.
        ws_app.run_forever(ping_interval=20, ping_timeout=10)

    def _handle_message(self, message: str) -> None:
        """Parst eine WS-Nachricht und schreibt enthaltene Adressen in die DB."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        if data.get("channel") != "trades":
            # subscriptionResponse, error, etc. ignorieren.
            return

        trades = data.get("data", [])
        if not isinstance(trades, list):
            return

        with self._write_lock:
            for trade in trades:
                ts = int(trade.get("time", int(time.time() * 1000)))
                users = trade.get("users") or []
                for addr in users:
                    if isinstance(addr, str) and addr.startswith("0x"):
                        _upsert_wallet(self._write_conn, addr, ts)
            self._write_conn.commit()
