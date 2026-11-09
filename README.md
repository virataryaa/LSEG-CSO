# CSO — Calendar Spread Options (LSEG)

ICE calendar spread options, pulled from the LSEG Data API (`lseg.data`). The folder layout matches `LSEG/Options`
(Code / Database / Dashboard / Automator). **Status (2026-11-09):** the ingest, dashboard and automator are built for KC (Arabica), LRC (Robusta) and LCC (London Cocoa). The dashboard is live on Streamlit Cloud and in HardMiner as "CSOs". The automator is not scheduled yet.

## KC RIC scheme (checked against live LSEG data)

`KC<n>C<|strike|*100><code><yy><P|N>`, for example `KC1C1500L26P` = 1-month CSO, +15.00 Call, Dec 2026.

| Part | Meaning |
|---|---|
| `KC1C` / `KC2C` | 1-month (consecutive, e.g. Z6-H7) / 2-month (skip one, e.g. Z6-K7) spread. KC3C/4C/6C do not exist. |
| strike | abs(strike) x 100, so 0.75 -> `75`, 15.75 -> `1575` |
| code | A-L = Jan-Dec call, M-X = Jan-Dec put. The month is the FRONT leg's contract month, the same as standard options |
| `P` / `N` | sign of the strike (positive / negative spread level) |

The strike is the spread level front minus back (cts/lb). Strikes run from -7 to +40, with 0.25 steps near the money.

## Probe results (2026-11-09)
- Discovery: `discovery.search(DERIVATIVE_QUOTES, filter="startswith(RIC,'KC1C')")` returns 656 KC1C and 700 KC2C RICs. All 656 KC1C RICs match the regex.
- KC1C OI is 51,424 lots across 55 RICs, and 43.8k of it sits in the Dec-26 expiry (Z6-H7, expiring 2026-11-12).
- Fields: SETTLE, OPINT_1 and ACVOL_UNS are populated. **IMP_VOLT is empty.**
- History: `get_history` returns only 20 rows by default. Pass `end=` and `count=2000` to get the whole contract life (about 250-320 rows, back to listing in 2025).
- Quirk: 6 RICs at the Dec-26 expiry report underlying `KCH7-K7` instead of `KCZ6-H7`, which is an LSEG metadata bug. Use the RIC code, not `UnderlyingQuoteRIC`.
- Chains also exist (`0#LRC1X7+` for Robusta), and LRC CSOs use `LRC1<strike><code><yy><P|N>` without the `C`.

Scripts: `Code/probe_kc_cso.py` (search plus the image naming guesses) and `Code/probe_kc_cso_2.py` (full board, OI, history).
`Database/_probe_kc1c_board.csv` holds the discovered KC1C board.

## Ingest - `Code/cso_ingest_lseg.py`
`python cso_ingest_lseg.py --commodity KC|LRC|LCC [--days 30] [--full]` -> `Database/<KEY>_cso.parquet` + `<KEY>_cso_underlying.parquet`

| | RIC | Strike | Futures months | OI>0 contracts (2026-11-09) |
|---|---|---|---|---|
| KC | `KC1C1500L26P` | abs x100, c/lb | H K N U Z | 56 |
| LRC | `LRC150A27P` | abs, $/t | F H K N U X | 1 (2,000 lots) |
| LCC | `LCC185L26P` | abs, GBP/t | H K N U Z | 8 (8,750 lots) |

- The year is always 2 digits (`LRC1...C7P`, with a 1-digit year, does not exist).
- The `LRC1`/`LCC1` prefixes also match IV-surface RICs (access denied) and plain LCC options. A regex plus a name filter keep only the CSOs. LSEG spells the KC name "Calender".
- Universe: `--full` (or no parquet yet) fetches every live RIC and keeps any with OI > 0 in the window. A daily run fetches today's OI>0 RICs plus every RIC already stored, so a contract that closes out is recorded falling to 0.
- OI top-up: history publishes OPINT_1 a day after prices. The newest price day is filled from the live snapshot only when it is provably published:
  - an untraded contract keeps its previous OI (OI cannot move without volume);
  - at least half of the traded contracts must show a changed snapshot OI.
  - On 2026-11-09 this filled 6 Nov for KC (8 of 8 traded contracts moved). A blind fill earlier had copied 5 Nov onto 6 Nov.
- Batches of 10 RICs with a 1.5s pause and a 429 backoff. The upsert keeps the newest non-null value per column.
- The futures spread settle (`KCZ6-H7`, `LRCF7-H7`, `LCCZ6-H7`) is fetched for the amber header row.

## Dashboard - `Dashboard/app.py`
`streamlit run Dashboard/app.py`. It uses the same theme and login as `LSEG/Options` (password in `.streamlit/secrets.toml`).
- Butterfly matrix: calls on the left, signed spread strike in the centre, puts on the right. Each column is one spread series (for example Z6-H7).
- Amber header row = futures spread settle. The outlined cell is each series' at-the-money strike.
- Strike step (sidebar, default 1): evenly spaced rows. Off-step strikes are added to the nearest row. "Exact" shows the traded strikes only.
- Hovering a cell shows its LSEG RIC(s), and clicking copies them (`st.html` with JS).
- Paired tables share one strike ladder, so their rows and heights match. The KPI row sits inside the table, with call figures under the call columns and put figures under the put columns.
- Views:
  - Positioning: OI change + volume, OI levels, Drill Down (contract board, tick up to 8, then OI / volume / settle / futures spread charts), History
  - Prices: settle, settle change
- Only complete days (OI published) appear in the date pickers.
- Drill Down is ported from LSEG/Options, with 2 fixes: volume covers the OI-change window, and an empty board can't produce a NaN bar maximum.

## Automator
`Automator/run.bat` -> `run_cso.py`: runs the daily ingest for KC, LRC and LCC, then commits and pushes the parquets (so Streamlit Cloud updates). Log: `Automator/run_log.txt`.
**Not scheduled yet** (on hold at the user's request, 2026-11-09). For now, run `Automator/run.bat` by hand. The planned task is "0000.6.3) LSEG - CSO", daily at 16:45 (after the NYC Options task at 16:30). LSEG Workspace must be open.
