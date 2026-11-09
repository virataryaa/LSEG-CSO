# CSO — Calendar Spread Options (LSEG)

ICE calendar spread options, pulled from the LSEG Data API (`lseg.data`). The folder layout matches `LSEG/Options`
(Code / Database / Dashboard / Automator). **Status (2026-11-09):** the KC ingest (`Code/kc_cso_ingest_lseg.py`) and the dashboard (`Dashboard/app.py`) are built. No scheduler and no Streamlit Cloud deploy yet.

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

## KC ingest - `Code/kc_cso_ingest_lseg.py`
`python kc_cso_ingest_lseg.py --days 30` -> `Database/KC_cso.parquet` (one row per date x RIC)
1. Discover KC1C, keep only unexpired RICs (about 650). KC2C (2-month) was dropped on 2026-11-09: only 2 of its 700 RICs had OI.
2. Snapshot `OPINT_1` in batches of 100 and keep **OI > 0 only** (55 RICs on 2026-11-09).
3. Fetch `get_history` (SETTLE, OPINT_1, ACVOL_UNS) 10 RICs per batch, with a 1.5s pause between batches and a backoff on 429s. It takes about 55s, with zero retries.
4. OI is published a day after prices, so the newest day has no OPINT_1 until the next run. Do not fill it from `get_data`: that snapshot OI is also the previous day's (a fill copied 5 Nov OI onto 6 Nov, which was fixed on 2026-11-09). The dashboard uses the latest OI date on or before each selected date.
5. Fetch the futures spread settle (LSEG RIC `KCZ6-H7`) -> `Database/KC_cso_underlying.parquet`.
6. Upsert on (date, ric). Stored rows are never deleted.

Columns: date, ric, settle, oi, volume, spread_months (1/2), put_call, strike (signed), contract_month/year,
front_leg, back_leg (e.g. KCZ6/KCH7), expiry.

## Dashboard - `Dashboard/app.py`
`streamlit run Dashboard/app.py`. It uses the same theme and login as `LSEG/Options` (password in `.streamlit/secrets.toml`).
- Butterfly matrix: calls on the left, signed spread strike in the centre, puts on the right. Each column is one spread series (for example Z6-H7).
- Amber header row = futures spread settle. The outlined cell is each series' at-the-money strike.
- Strike step (sidebar, default 1): evenly spaced rows. Off-step strikes are added to the nearest row. "Exact" shows the traded strikes only.
- Hovering a cell shows its LSEG RIC(s), and clicking copies them (`st.html` with JS).
- Paired tables share one strike ladder, so their rows and heights match. The KPI row sits inside the table, with call figures under the call columns and put figures under the put columns.
- Views: Positioning (OI change + volume, OI levels, history) and Prices (settle, settle change).
