"""
app.py
======
Streamlit-Oberflaeche fuer den Hyperliquid Swing-Trader-Scanner.

Cloud-tauglicher Ablauf (Burst-Modus):
1. Auf Knopfdruck oeffnet die App kurz eine WebSocket-Verbindung, hoert
   `dauer` Sekunden den Live-Trade-Feed mit und sammelt die in diesem Fenster
   aktiven Wallet-Adressen. Das laeuft synchron in EINEM Request -> ideal fuer
   Streamlit Community Cloud (kein dauerhafter Hintergrund-Thread noetig).
2. Fuer jede gefundene Adresse wird ein Profil + Klassifizierung berechnet
   (analysis.py).
3. Ergebnis als gefilterte Tabelle + Detailansicht + Hyperdash-Link + CSV-Export.

Hinweis: Fuer einen eigenen, dauerhaft laufenden Server enthaelt collector.py
zusaetzlich die Klasse WalletCollector (Hintergrund-Thread + SQLite), die ueber
laengere Zeit auch seltener handelnde Swing-Trader einsammelt. Auf Cloud ist
der Burst-Modus die robuste Wahl.
"""

import time
import datetime as dt

import pandas as pd
import streamlit as st

import collector
import analysis

# =============================================================================
# KONSTANTEN
# =============================================================================
# WICHTIG: Das exakte Pfadformat einmal manuell auf hyperdash.com pruefen
# (eine Adresse aufrufen und das Format aus der Browserzeile uebernehmen) und
# hier eintragen. Wir scrapen Hyperdash NICHT, sondern verlinken nur.
HYPERDASH_PROFILE_URL = "https://hyperdash.com/trader/{address}"


# =============================================================================
# Hilfsfunktionen
# =============================================================================
@st.cache_data(ttl=60, show_spinner=False)
def cached_profile(address: str) -> dict:
    """Baut das Profil einer Wallet und cached es 60s (Cache-Key = Adresse)."""
    return analysis.build_profile(address)


def hyperdash_url(address: str) -> str:
    return HYPERDASH_PROFILE_URL.format(address=address)


def minutes_ago(ts_ms: int) -> int:
    if not ts_ms:
        return 0
    return int((time.time() * 1000 - ts_ms) / 60000)


# =============================================================================
# UI
# =============================================================================
st.set_page_config(page_title="HL Swing-Trader-Scanner", layout="wide")

st.title("Hyperliquid Swing-Trader-Scanner")
st.caption(
    "Sammelt live aktive Wallets aus dem Trade-Feed, filtert algorithmische Bots "
    "heraus und zeigt Swing-Trader als moegliche Copy-Trading-Kandidaten."
)
st.warning(
    "Keine Anlageberatung. Copy-Trading ist riskant - auch bislang profitable "
    "Wallets koennen schnell ins Minus drehen. Eigene Pruefung ist Pflicht.",
    icon="⚠️",
)

# --- Sidebar-Filter -----------------------------------------------------------
st.sidebar.header("Sammeln")
burst_seconds = st.sidebar.slider(
    "Live-Trades sammeln (Sekunden)", min_value=10, max_value=120, value=30, step=5,
    help="Wie lange der Live-Feed mitgehoert wird. Laenger = mehr Wallets, aber laengere Wartezeit.",
)
max_wallets = st.sidebar.number_input(
    "Max. Wallets pro Scan (begrenzt API-Last)", min_value=5, max_value=500, value=50, step=5
)

st.sidebar.header("Filter")
max_trades_day = st.sidebar.number_input(
    "Max. Trades/Tag", min_value=1, max_value=2000, value=analysis.MAX_TRADES_PER_DAY
)
min_hold_minutes = st.sidebar.number_input(
    "Min. mediane Haltedauer (Minuten)", min_value=0, max_value=100000,
    value=analysis.MIN_MEDIAN_HOLD_MINUTES
)
min_account_value = st.sidebar.number_input(
    "Min. Account-Value (USDC)", min_value=0, max_value=100_000_000, value=1000, step=500
)
min_pnl_30d = st.sidebar.number_input(
    "Min. 30d-PnL (USDC)", min_value=-1_000_000, max_value=100_000_000, value=0, step=100
)
only_swing = st.sidebar.toggle("Nur als Swing klassifizierte anzeigen", value=True)

# --- Scan-Button --------------------------------------------------------------
if st.button("Live sammeln & scannen", type="primary"):
    # Schritt 1: Burst-Sammellauf am Live-Feed.
    collect_progress = st.progress(0.0, text="Verbinde mit Live-Feed ...")

    def _cb(frac, n):
        collect_progress.progress(
            min(1.0, frac), text=f"Sammle Live-Trades ... {n} Wallets gefunden"
        )

    try:
        seen = collector.burst_collect(duration_seconds=int(burst_seconds), progress_cb=_cb)
    except Exception as exc:  # noqa: BLE001
        collect_progress.empty()
        st.error(f"Konnte den Live-Feed nicht erreichen: {exc}")
        st.stop()
    collect_progress.empty()

    if not seen:
        st.info("In diesem Zeitfenster keine Trades empfangen. Einfach erneut versuchen "
                "oder die Sammeldauer erhoehen.")
        st.stop()

    # Adressen in Einfuege-Reihenfolge (mischt vielhandelnde und seltene Wallets,
    # damit nicht nur Market-Maker im max_wallets-Limit landen).
    addresses = list(seen.keys())[: int(max_wallets)]

    # Schritt 2: Profile berechnen (mit Fortschritt).
    scan_progress = st.progress(0.0, text="Analysiere Wallets ...")
    total = len(addresses)
    profiles = []
    for i, addr in enumerate(addresses):
        profiles.append(cached_profile(addr))
        scan_progress.progress((i + 1) / total, text=f"Analysiere {i + 1}/{total} ...")
    scan_progress.empty()

    # Schritt 3: Tabelle aufbauen.
    rows = []
    for p in profiles:
        if p.get("error"):
            continue
        addr = p["address"]
        rows.append({
            "Adresse": addr,
            "Klassifizierung": p["klassifizierung"],
            "Account-Value": p["account_value"],
            "Trades/Tag": p["trades_per_day"],
            "Med. Haltedauer (Min)": p["median_hold_minutes"],
            "30d-PnL": p["pnl_30d"],
            "7d-PnL": p["pnl_7d"],
            "Win-Rate %": p["win_rate"],
            "Profit-Faktor": p["profit_factor"],
            "Aktiv vor (Min)": minutes_ago(seen.get(addr, {}).get("last_ts", 0)),
            "Feed-Hits": seen.get(addr, {}).get("count", 0),
            "Hyperdash": hyperdash_url(addr),
            "_profile": p,
        })

    if not rows:
        st.info("Keine auswertbaren Wallets gefunden (evtl. zu wenig Handelshistorie).")
        st.stop()

    df = pd.DataFrame(rows)

    # --- Filter anwenden ---
    mask = (
        (df["Trades/Tag"] <= max_trades_day)
        & (df["Account-Value"] >= min_account_value)
        & (df["30d-PnL"] >= min_pnl_30d)
    )
    mask &= df["Med. Haltedauer (Min)"].fillna(0) >= min_hold_minutes
    if only_swing:
        mask &= df["Klassifizierung"] == "Swing"
    df_view = df[mask].copy()
    df_view.sort_values("30d-PnL", ascending=False, inplace=True)

    st.subheader(f"{len(df_view)} Wallet(s) nach Filter "
                 f"(von {len(rows)} analysierten, {len(seen)} live gesehen)")

    show_cols = [c for c in df_view.columns if c != "_profile"]
    st.dataframe(
        df_view[show_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Hyperdash": st.column_config.LinkColumn(
                "Auf Hyperdash pruefen", display_text="Prüfen"
            ),
            "Account-Value": st.column_config.NumberColumn(format="%.2f"),
            "30d-PnL": st.column_config.NumberColumn(format="%.2f"),
            "7d-PnL": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    # --- CSV-Export ---
    csv = df_view[show_cols].to_csv(index=False).encode("utf-8")
    st.download_button(
        "Als CSV exportieren", data=csv,
        file_name=f"swing_wallets_{dt.date.today()}.csv", mime="text/csv"
    )

    # --- Detailansicht pro Wallet ---
    st.subheader("Details")
    for _, row in df_view.iterrows():
        p = row["_profile"]
        with st.expander(f"{p['address']}  ·  {p['klassifizierung']}  ·  "
                         f"30d-PnL {p['pnl_30d']:,.2f} USDC"):
            c1, c2, c3 = st.columns(3)
            c1.metric("Account-Value", f"{p['account_value']:,.2f}")
            c1.metric("Win-Rate", f"{p['win_rate']}%" if p['win_rate'] is not None else "—")
            c2.metric("Trades/Tag", p["trades_per_day"])
            c2.metric("Profit-Faktor", p["profit_factor"] if p["profit_factor"] is not None else "—")
            c3.metric("Med. Haltedauer (Min)",
                      p["median_hold_minutes"] if p["median_hold_minutes"] is not None else "—")
            c3.metric("Abgeschl. Trades", p["num_closed_trips"])

            rest = "Ja" if p["has_rest_window"] else "Nein (24/7-Verdacht)"
            interfill = (f"{p['median_interfill_sec']}s"
                         if p["median_interfill_sec"] is not None else "—")
            st.markdown(
                f"**Tagespause vorhanden:** {rest}  ·  "
                f"**Max. parallele Maerkte:** {p['max_concurrent_markets']}  ·  "
                f"**Median Abstand Fills:** {interfill}"
            )

            fav = ", ".join(f"{c} ({n})" for c, n in p["favorite_coins"][:8]) or "—"
            st.markdown(f"**Meistgehandelte Coins:** {fav}")

            if p["open_positions"]:
                st.markdown("**Aktuelle offene Positionen:**")
                st.dataframe(
                    pd.DataFrame(p["open_positions"]),
                    use_container_width=True, hide_index=True,
                )
            else:
                st.markdown("**Aktuelle offene Positionen:** keine")

            st.markdown(f"[Auf Hyperdash pruefen]({hyperdash_url(p['address'])})")
else:
    st.info(
        "Klicke auf **Live sammeln & scannen**. Die App hoert dann fuer die "
        "eingestellte Dauer den Live-Trade-Feed mit, sammelt aktive Wallets und "
        "analysiert sie. Tipp: Wenn zu wenige Swing-Trader auftauchen, Sammeldauer "
        "erhoehen oder den Swing-Filter lockern."
    )
