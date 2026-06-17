# Hyperliquid Swing-Trader-Scanner

Eine Streamlit-App, die **live** aktive Hyperliquid-Wallets aus dem öffentlichen
Trade-Feed sammelt, algorithmische/HFT-Bots herausfiltert und potenzielle
**Swing-Trader** als Copy-Trading-Kandidaten anzeigt – inklusive selbst berechnetem
Trader-Profil (Win-Rate, PnL, Coins, Haltedauer) und einem Verifikations-Link zu
Hyperdash.

> ⚠️ **Keine Anlageberatung.** Copy-Trading ist riskant. Auch bislang profitable
> Wallets können schnell ins Minus drehen. Eigene Prüfung ist Pflicht.

---

## Wie Wallets bewertet werden (Qualitäts-Score)

Damit nicht zufällige Glückstrades oder Verlust-Wallets oben landen, bekommt jede
Wallet einen **Qualitäts-Score (0–100)**. Sortiert wird danach – nicht nach rohem PnL.

**Harte Ausschluss-Gates** (Wallet wird aussortiert, wenn eines verletzt ist):
- weniger als `MIN_CLOSED_TRIPS` abgeschlossene Trades (Stichprobe zu klein)
- Historie kürzer als `MIN_ACTIVE_DAYS` (Default 7 Tage)
- Account-Value unter `MIN_ACCOUNT_VALUE` (Default 5.000 $)
- netto im Minus über den Zeitraum
- Profit-Faktor unter `MIN_PROFIT_FACTOR`
- ein einzelner Trade macht mehr als `MAX_SINGLE_TRADE_SHARE` des Gewinns aus
  (genau der „500 $ aus einem Trade"-Fall)

**Gewichteter Score** (Reihenfolge nach Priorität: Win-Rate > PnL > Risiko > Konstanz):
- Win-Rate 40 % – **multipliziert mit einer Stichproben-Konfidenz**, damit eine
  90 %-Quote aus 6 Trades NICHT über einer soliden 65 %-Quote aus 40 Trades landet
- Rendite/PnL 30 % (als % auf den Account-Value, gedeckelt)
- Risiko 20 % (weniger Drawdown = mehr Punkte; erkannte Liquidation drückt hart)
- Konstanz 10 % (Gewinn breit gestreut + genug Trades + beide Zeitfenster positiv)

Alle Schwellen und Gewichte stehen gebündelt oben in `analysis.py` und lassen sich
auch live in der Sidebar nachjustieren (Min. Score, Min. Trades, Min. Profit-Faktor,
„Nur qualitätsgeprüfte Wallets").



1. **GitHub-Repo anlegen** und alle Dateien dieses Ordners hochladen
   (`app.py`, `collector.py`, `analysis.py`, `api.py`, `requirements.txt`,
   `README.md`, `.gitignore`, `.streamlit/config.toml`).
2. Auf **https://share.streamlit.io** mit dem GitHub-Account einloggen.
3. **"Create app" → "Deploy a public app from GitHub"** wählen.
4. Repo auswählen, Branch `main`, **Main file path:** `app.py`.
5. Optional unter *Advanced settings* die Python-Version auf **3.11** setzen.
6. **Deploy** klicken. Streamlit liest automatisch `requirements.txt` und startet.

Danach in der App auf **„Live sammeln & scannen"** klicken.

### Wichtig zur Cloud-Tauglichkeit
Die App nutzt einen **Burst-Modus**: Beim Klick wird der Live-Trade-Feed kurz
(einstellbar, Default 30 Sek.) mitgehört, die aktiven Wallets gesammelt und sofort
analysiert. Das läuft in einem einzigen Request und braucht **keinen** dauerhaften
Hintergrundprozess – genau deshalb funktioniert es auf Streamlit Cloud, wo Apps bei
Inaktivität schlafen gelegt und neu gestartet werden.

Tradeoff: Ein 30-Sekunden-Snapshot findet vor allem gerade aktive Wallets. Seltener
handelnde Swing-Trader tauchen evtl. nicht in jedem Burst auf → Sammeldauer erhöhen
oder mehrfach scannen.

---

## Lokal starten

```bash
pip install -r requirements.txt
streamlit run app.py
```

---

## Optional: dauerhafter Hintergrund-Collector (eigener Server / VPS)

Für eine **always-on**-Umgebung (eigener Server, der nicht schläft) enthält
`collector.py` zusätzlich die Klasse `WalletCollector`. Diese hält die WebSocket-
Verbindung dauerhaft offen und schreibt alle gesehenen Adressen über Stunden/Tage
in eine SQLite-DB (`wallets.db`). So werden auch seltener handelnde Swing-Trader
zuverlässig eingesammelt. Auf Streamlit Cloud ist diese Variante **nicht** zu
empfehlen (flüchtiges Dateisystem, Schlafmodus) – dort den Burst-Modus nutzen.

---

## Wichtig vor dem ersten Einsatz

- **Hyperdash-Link:** In `app.py` ist `HYPERDASH_PROFILE_URL` ein Platzhalter
  (`https://hyperdash.com/trader/{address}`). Bitte einmal eine beliebige Adresse
  auf hyperdash.com aufrufen und das exakte Pfadformat aus der Browserzeile
  übernehmen. Es wird **nicht gescraped**, nur verlinkt.
- **Schwellenwerte:** Alle Klassifizierungs-Grenzen stehen gebündelt oben in
  `analysis.py` (`MAX_TRADES_PER_DAY`, `MIN_MEDIAN_HOLD_MINUTES`,
  `SWING_MIN_HOLD_HOURS`, …). Nach den ersten echten Scans an deine Beobachtungen
  anpassen – die Swing-vs-Algo-Erkennung ist eine **Heuristik**, kein perfekter Filter.
- **Analysezeitraum:** `LOOKBACK_DAYS` in `analysis.py` (Default 30). Höher = stabilere
  Statistik, aber mehr API-Calls pro Wallet.

## Dateien

| Datei                      | Zweck                                          |
|----------------------------|------------------------------------------------|
| `app.py`                   | Streamlit-UI, Burst-Sammeln, Scan, Anzeige     |
| `collector.py`             | Burst-Collect + optionaler Dauer-Collector     |
| `api.py`                   | REST-Wrapper für die Hyperliquid Info-API      |
| `analysis.py`              | Klassifizierung + Trader-Profil aus Rohdaten   |
| `requirements.txt`         | Abhängigkeiten                                 |
| `.streamlit/config.toml`   | Theme/Server-Einstellungen                     |
| `.gitignore`               | hält lokale Artefakte aus dem Repo             |

## Hinweise zur Heuristik

- Beide Adressen eines Trades sind Gegenparteien (Maker + Taker); für die Discovery
  egal, da jede Adresse anhand ihrer eigenen Historie klassifiziert wird.
- Viele `Feed-Hits` (Wallet taucht oft im Feed auf) deuten eher auf einen
  Market-Maker/HFT-Bot hin – wird daher **nicht** als Qualitätssignal genutzt.
- HIP-3-Märkte (tokenisierte Aktien/Rohstoffe) erscheinen automatisch in den
  gehandelten Coins mit.
