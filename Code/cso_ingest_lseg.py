"""
cso_ingest_lseg.py - ICE 1-month Calendar Spread Options from LSEG -> Database/<KEY>_cso.parquet

    python cso_ingest_lseg.py --commodity KC|LRC|LCC [--days 30] [--full]

RIC schemes (confirmed live with discovery.search, 2026-11-09):
    KC   KC1C<|strike|*100><code><yy><P|N>   KC1C1500L26P = +15.00 c/lb Call Dec26 (KCZ6-H7)
    LRC  LRC1<|strike|><code><yy><P|N>       LRC150A27P   = +50 $/t  Call Jan27 (LRCF7-H7)
    LCC  LCC1<|strike|><code><yy><P|N>       LCC185L26P   = +85 GBP/t Call Dec26 (LCCZ6-H7)
code A-L = Jan-Dec call, M-X = Jan-Dec put (FRONT leg contract month); P/N = sign of the strike.
Strike = front leg minus back leg; the back leg is the next listed futures month.
The LRC1/LCC1 prefixes also match IV-surface RICs (access denied) and, for LCC, plain options
(no P/N suffix): the regex plus a "Calendar/Calender Spread" name filter keep only the CSOs
(LSEG spells KC "Calender").

Universe:
  --full (or no parquet yet): history for EVERY live CSO RIC, keep those with OI > 0 on any day in the window.
  daily: RICs with OPINT_1 > 0 on today's snapshot, plus every live RIC already in the parquet, so a contract
         that closes out is recorded falling to 0 instead of vanishing.

OI top-up: get_history publishes OPINT_1 a day after prices, so the newest price day comes back with
no OI. The live snapshot (get_data OPINT_1) carries that day's OI once the exchange publishes it, and
the previous day's until then. The day is filled only when it is provably published:
  - a contract with no volume that day cannot have changed OI, so it gets its previous OI;
  - among contracts that DID trade, at least half must show a snapshot OI different from their
    previous OI. Otherwise the snapshot is still the previous day's, and the day stays empty.
(The Options top-up's "majority of all RICs moved" test cannot work here: most CSO strikes never trade.)

Rate limiting: get_history is one HTTP call per RIC, so batches of 10 with a pause and a 429 backoff.
Upsert keeps the newest NON-NULL value per column, so an empty field never wipes a stored value.
"""
import argparse
import logging
import re
import sys
import time
from pathlib import Path

import pandas as pd
pd.set_option("future.no_silent_downcasting", True)
import lseg.data as ld
from lseg.data import discovery

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["SETTLE", "OPINT_1", "ACVOL_UNS"]   # IMP_VOLT is empty for CSOs on LSEG
SNAP_BATCH, HIST_BATCH = 100, 10
PAUSE, RETRIES, BACKOFF = 1.5, 3, 10
CALL, PUT, FUT = "ABCDEFGHIJKL", "MNOPQRSTUVWX", "FGHJKMNQUVXZ"

CONFIG = {
    "KC":  dict(prefix="KC1C", scale=100, months="HKNUZ"),    # 2-month KC2C dropped: ~no OI
    "LRC": dict(prefix="LRC1", scale=1,   months="FHKNUX"),
    "LCC": dict(prefix="LCC1", scale=1,   months="HKNUZ"),
}

log = logging.getLogger("cso")


def setup_logging(key: str):
    path = ROOT / "Code" / "logs" / f"{key.lower()}_cso_ingest_lseg.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)])
    logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_ric(ric: str, key: str):
    cfg = CONFIG[key]
    m = re.match(rf"^{cfg['prefix']}(\d+)([A-X])(\d{{2}})([PN])$", ric)
    if not m:
        return None
    k, code, yy, sign = m.groups()
    is_call = code in CALL
    month = (CALL if is_call else PUT).index(code) + 1
    year = 2000 + int(yy)
    strike = int(k) / cfg["scale"] * (1 if sign == "P" else -1)
    months = cfg["months"]
    fl = FUT[month - 1]
    back = None
    if fl in months:
        j = months.index(fl) + 1
        back = f"{months[j % len(months)]}{(year + j // len(months)) % 10}"
    return dict(ric=ric, spread_months=1, put_call="C" if is_call else "P", strike=strike,
                contract_month=month, contract_year=year,
                front_leg=f"{key}{fl}{year % 10}", back_leg=f"{key}{back}" if back else None)


def discover(key: str) -> pd.DataFrame:
    p = CONFIG[key]["prefix"]
    df = discovery.search(view=discovery.Views.DERIVATIVE_QUOTES, top=10000,
                          select="RIC,DTSubjectName,ExpiryDate", filter=f"startswith(RIC,'{p}')")
    name = df["DTSubjectName"].fillna("")
    df = df[name.str.contains("Calend[ae]r Spread", case=False, regex=True) & ~name.str.contains("Volatility|IV Surface", case=False)]
    meta = pd.DataFrame([x for x in (parse_ric(r, key) for r in df.RIC) if x])
    if meta.empty:
        return meta
    meta = meta.merge(df[["RIC", "ExpiryDate"]].rename(columns={"RIC": "ric", "ExpiryDate": "expiry"}), on="ric")
    meta = meta[meta.expiry.isna() | (pd.to_datetime(meta.expiry) >= pd.Timestamp.today().normalize())]
    log.info("discovered %s: %d live CSO RICs", p, len(meta))
    return meta.reset_index(drop=True)


def with_retry(fn, what):
    delay = BACKOFF
    for a in range(1, RETRIES + 1):
        try:
            return fn()
        except Exception as e:
            err = str(e)
            if a == RETRIES:
                log.error("%s FAILED: %s", what, err[:150])
                return None
            wait = max(delay, 30) if ("429" in err or "too many" in err.lower()) else delay
            log.warning("%s error (try %d/%d): %s - waiting %ds", what, a, RETRIES, err[:100], wait)
            time.sleep(wait)
            delay = wait * 2


def snapshot_oi(rics: list) -> pd.Series:
    """Live OPINT_1 per RIC (the last PUBLISHED session's OI)."""
    out = []
    for i in range(0, len(rics), SNAP_BATCH):
        b = rics[i:i + SNAP_BATCH]
        df = with_retry(lambda: ld.get_data(universe=b, fields=["OPINT_1"]), f"snapshot {i}")
        if df is not None and len(df):
            out.append(df.set_index("Instrument")["OPINT_1"])
        time.sleep(PAUSE)
    return pd.to_numeric(pd.concat(out), errors="coerce") if out else pd.Series(dtype=float)


def fetch_history(rics: list, start: str, end: str) -> pd.DataFrame:
    out = []
    for i in range(0, len(rics), HIST_BATCH):
        b = rics[i:i + HIST_BATCH]
        h = with_retry(lambda: ld.get_history(universe=b, fields=FIELDS, start=start, end=end,
                                              interval="daily", count=2000), f"history {i}")
        if h is not None and len(h):
            if len(b) == 1:   # single RIC -> flat columns
                h.columns = pd.MultiIndex.from_product([b, h.columns])
            long = h.stack(level=0, future_stack=True).reset_index()
            long.columns = ["date", "ric"] + list(long.columns[2:])
            out.append(long)
        log.info("  history %d/%d", min(i + HIST_BATCH, len(rics)), len(rics))
        time.sleep(PAUSE)
    if not out:
        return pd.DataFrame()
    df = pd.concat(out, ignore_index=True).rename(columns={"SETTLE": "settle", "OPINT_1": "oi", "ACVOL_UNS": "volume"})
    for c in ["settle", "oi", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    return df.dropna(subset=["settle", "oi", "volume"], how="all")


def fetch_underlying(key: str, spreads: list, start: str, end: str) -> pd.DataFrame:
    """Settle of the futures calendar spread itself (LSEG RIC e.g. 'KCZ6-H7' = front minus back)."""
    if not spreads:
        return pd.DataFrame()
    rics = [f"{key}{s}" for s in spreads]
    h = with_retry(lambda: ld.get_history(universe=rics, fields=["SETTLE"], start=start, end=end,
                                          interval="daily", count=2000), "underlying")
    if h is None or h.empty:
        return pd.DataFrame()
    if len(rics) == 1:
        h.columns = pd.Index(rics)
    elif isinstance(h.columns, pd.MultiIndex):
        h.columns = h.columns.get_level_values(0)
    u = h.reset_index().melt(id_vars=h.index.name or "Date", var_name="ric", value_name="settle")
    u.columns = ["date", "ric", "settle"]
    u["spread"] = u["ric"].str[len(key):]
    u["date"] = pd.to_datetime(u["date"]).dt.normalize()
    u["settle"] = pd.to_numeric(u["settle"], errors="coerce")
    return u.dropna(subset=["settle"])[["date", "spread", "settle"]]


def topup_oi(full: pd.DataFrame, snap: pd.Series) -> pd.DataFrame:
    """Fill the newest price day's OI when it is provably published (see module docstring)."""
    today = pd.Timestamp.today().normalize()
    priced = full[(full["date"] < today) & full["settle"].notna()]
    if priced.empty:
        return full
    target = priced["date"].max()
    day = full[full["date"] == target]
    if day["oi"].notna().all():
        log.info("OI top-up: %s already complete", target.date())
        return full
    prev = full[(full["date"] < target) & full["oi"].notna()].sort_values("date").groupby("ric")["oi"].last()
    traded = day.loc[day["volume"].fillna(0) > 0, "ric"]
    comparable = [r for r in traded if pd.notna(prev.get(r)) and pd.notna(snap.get(r))]
    moved = [r for r in comparable if abs(float(snap[r]) - float(prev[r])) >= 1]
    if comparable and len(moved) * 2 < len(comparable):
        log.info("OI top-up: %s not published yet (%d of %d traded contracts moved) - left empty",
                 target.date(), len(moved), len(comparable))
        return full
    fills = 0
    vol = day.set_index("ric")["volume"].fillna(0)
    for r in day.loc[day["oi"].isna(), "ric"]:
        if vol.get(r, 0) > 0:
            v = snap.get(r)                       # traded -> the published snapshot
        else:
            v = prev.get(r, snap.get(r))          # no trade -> OI cannot have changed
        if pd.notna(v):
            full.loc[(full["date"] == target) & (full["ric"] == r), "oi"] = float(v)
            fills += 1
    log.info("OI top-up: %s filled for %d RICs (%d of %d traded contracts moved)",
             target.date(), fills, len(moved), len(comparable))
    return full


def upsert(path: Path, new: pd.DataFrame, keys: list) -> pd.DataFrame:
    if path.exists():
        # groupby().last() = newest NON-NULL value per column: an empty field never wipes a stored value
        new = pd.concat([pd.read_parquet(path), new], ignore_index=True).groupby(keys, as_index=False, sort=False).last()
    return new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commodity", required=True, choices=sorted(CONFIG))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--full", action="store_true", help="fetch every live RIC, keep any with OI > 0 in the window")
    args = ap.parse_args()
    key = args.commodity
    setup_logging(key)
    out, und_out = ROOT / "Database" / f"{key}_cso.parquet", ROOT / "Database" / f"{key}_cso_underlying.parquet"
    full_mode = args.full or not out.exists()
    end = pd.Timestamp.today().normalize()
    start = (end - pd.Timedelta(days=args.days)).strftime("%Y-%m-%d")
    end_s = end.strftime("%Y-%m-%d")
    t0 = time.time()

    ld.open_session()
    try:
        meta = discover(key)
        if meta.empty:
            log.error("no live CSO RICs for %s", key)
            sys.exit(1)
        snap = snapshot_oi(meta.ric.tolist())
        if full_mode:
            universe = meta.ric.tolist()
            log.info("FULL: fetching all %d live RICs", len(universe))
        else:
            live = set(snap[snap > 0].index)
            stored = set(pd.read_parquet(out, columns=["ric"]).ric) & set(meta.ric)
            universe = sorted(live | stored)
            log.info("daily: %d with OI>0 today + %d already stored -> %d RICs", len(live), len(stored), len(universe))
        hist = fetch_history(universe, start, end_s)
        if full_mode and not hist.empty:
            mx = hist.groupby("ric")["oi"].max()
            hist = hist[hist.ric.isin(set(mx[mx > 0].index) | set(snap[snap > 0].index))]
            log.info("FULL: %d RICs had OI > 0 in the window", hist.ric.nunique())
        lm = meta[meta.ric.isin(set(hist.ric) if not hist.empty else set())]
        spreads = sorted({f"{f[len(key):]}-{b[len(key):]}" for f, b in zip(lm.front_leg, lm.back_leg) if b})
        und = fetch_underlying(key, spreads, start, end_s)
        log.info("underlying spreads %s: %d rows", spreads, len(und))
    finally:
        ld.close_session()

    if hist.empty:
        log.error("no history fetched")
        sys.exit(1)
    hist = hist.merge(meta, on="ric", how="left")
    full = upsert(out, hist, ["date", "ric"])
    full = topup_oi(full, snap)
    full = full.sort_values(["date", "contract_year", "contract_month", "put_call", "strike"])
    full.to_parquet(out, index=False)
    if not und.empty:
        upsert(und_out, und, ["date", "spread"]).sort_values(["date", "spread"]).to_parquet(und_out, index=False)
    log.info("%s: wrote %d rows (%d RICs, %s -> %s); parquet now %d rows; %.0fs", key, len(hist), hist.ric.nunique(),
             hist.date.min().date(), hist.date.max().date(), len(full), time.time() - t0)


if __name__ == "__main__":
    main()
