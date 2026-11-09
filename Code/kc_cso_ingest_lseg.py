"""
kc_cso_ingest_lseg.py - KC (Arabica) Calendar Spread Options from LSEG -> Database/KC_cso.parquet

Universe (KC1C = 1-month spreads; KC2C 2-month is left out):
  --full (or no parquet yet): history for EVERY live RIC, keep those with OI > 0 on any day in the window.
  daily: RICs with OPINT_1 > 0 on today's snapshot, plus every live RIC already in the parquet. Contracts
         that close out keep being fetched, so their OI is recorded falling to 0 instead of vanishing.

RIC: KC<n>C<|strike|*100><code><yy><P|N>  e.g. KC1C1500L26P = 1M spread +15.00 Call Dec26 (Z6-H7)
  code A-L = Jan-Dec call, M-X = Jan-Dec put (FRONT leg contract month); P/N = strike sign.

Rate limiting: get_history is one HTTP call per RIC, so we fetch in small batches with a pause
between them, and back off on 429s. Upsert never deletes stored rows.

Usage:  python kc_cso_ingest_lseg.py [--days 30] [--full]
"""
import argparse, logging, re, sys, time
from pathlib import Path
import pandas as pd
pd.set_option("future.no_silent_downcasting", True)
import lseg.data as ld
from lseg.data import discovery

ROOT      = Path(__file__).resolve().parent.parent
OUT       = ROOT / "Database" / "KC_cso.parquet"
UND_OUT   = ROOT / "Database" / "KC_cso_underlying.parquet"   # futures spread settles, e.g. KCZ6-H7
LOG       = ROOT / "Code" / "logs" / "kc_cso_ingest_lseg.log"
PREFIXES  = {"KC1C": 1}                         # 1-month spreads only (KC2C dropped 2026-11-09: ~no OI)
FIELDS    = ["SETTLE", "OPINT_1", "ACVOL_UNS"]  # IMP_VOLT is empty for CSOs on LSEG
SNAP_BATCH, HIST_BATCH = 100, 10
PAUSE, RETRIES, BACKOFF = 1.5, 3, 10            # seconds between batches / retry policy

KC_MONTHS = ["H", "K", "N", "U", "Z"]           # KC listed futures months
CALL = "ABCDEFGHIJKL"; PUT = "MNOPQRSTUVWX"; FUT = "FGHJKMNQUVXZ"
RIC_RE = re.compile(r"^KC([12])C(\d+)([A-X])(\d{2})([PN])$")

LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(LOG, encoding="utf-8"), logging.StreamHandler(sys.stdout)])
log = logging.getLogger("kc_cso")
logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_ric(ric: str):
    m = RIC_RE.match(ric)
    if not m:
        return None
    width, k, code, yy, sign = m.groups()
    width = int(width)
    is_call = code in CALL
    month = (CALL if is_call else PUT).index(code) + 1
    year = 2000 + int(yy)
    strike = int(k) / 100 * (1 if sign == "P" else -1)
    # legs: front = contract month, back = `width` listed KC months later
    fl = FUT[month - 1]
    i = KC_MONTHS.index(fl) if fl in KC_MONTHS else None
    back = None
    if i is not None:
        j = i + width
        back = f"{KC_MONTHS[j % 5]}{(year + j // 5) % 10}"
    return dict(ric=ric, spread_months=width, put_call="C" if is_call else "P", strike=strike,
                contract_month=month, contract_year=year,
                front_leg=f"KC{fl}{year % 10}", back_leg=f"KC{back}" if back else None)


def discover() -> pd.DataFrame:
    rows = []
    for p in PREFIXES:
        df = discovery.search(view=discovery.Views.DERIVATIVE_QUOTES, top=10000,
                              select="RIC,ExpiryDate,StrikePrice", filter=f"startswith(RIC,'{p}')")
        log.info("discovered %s: %d RICs", p, len(df))
        rows.append(df)
    df = pd.concat(rows, ignore_index=True)
    meta = pd.DataFrame([x for x in map(parse_ric, df.RIC) if x])
    meta = meta.merge(df[["RIC", "ExpiryDate"]].rename(columns={"RIC": "ric", "ExpiryDate": "expiry"}), on="ric")
    meta = meta[meta.expiry.isna() | (pd.to_datetime(meta.expiry) >= pd.Timestamp.today().normalize())]
    log.info("parsed %d live RICs", len(meta))
    return meta


def with_retry(fn, what):
    delay = BACKOFF
    for a in range(1, RETRIES + 1):
        try:
            return fn()
        except Exception as e:
            err = str(e)
            if a == RETRIES:
                log.error("%s FAILED: %s", what, err[:150]); return None
            wait = max(delay, 30) if ("429" in err or "too many" in err.lower()) else delay
            log.warning("%s error (try %d/%d): %s - waiting %ds", what, a, RETRIES, err[:100], wait)
            time.sleep(wait); delay = wait * 2


def oi_filter(rics: list) -> dict:
    """{ric: snapshot OI} for RICs with OI > 0."""
    keep = {}
    for i in range(0, len(rics), SNAP_BATCH):
        b = rics[i:i + SNAP_BATCH]
        df = with_retry(lambda: ld.get_data(universe=b, fields=["OPINT_1"]), f"snapshot {i}")
        if df is not None and len(df):
            oi = pd.to_numeric(df["OPINT_1"], errors="coerce")
            keep.update(dict(zip(df.loc[oi > 0, "Instrument"], oi[oi > 0].astype(int))))
        time.sleep(PAUSE)
    return keep


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
    df = pd.concat(out, ignore_index=True)
    df = df.rename(columns={"SETTLE": "settle", "OPINT_1": "oi", "ACVOL_UNS": "volume"})
    for c in ["settle", "oi", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    return df.dropna(subset=["settle", "oi", "volume"], how="all")


def fetch_underlying(spreads: list, start: str, end: str) -> pd.DataFrame:
    """Settle of the futures calendar spread itself (LSEG RIC 'KCZ6-H7' = front minus back)."""
    rics = [f"KC{s}" for s in spreads]
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
    u["spread"] = u["ric"].str[2:]
    u["date"] = pd.to_datetime(u["date"]).dt.normalize()
    u["settle"] = pd.to_numeric(u["settle"], errors="coerce")
    return u.dropna(subset=["settle"])[["date", "spread", "settle"]]


def upsert_underlying(new: pd.DataFrame):
    if new.empty:
        return
    if UND_OUT.exists():
        new = pd.concat([pd.read_parquet(UND_OUT), new], ignore_index=True).groupby(["date", "spread"], as_index=False).last()
    new.sort_values(["date", "spread"]).to_parquet(UND_OUT, index=False)


def upsert(new: pd.DataFrame):
    if OUT.exists():
        old = pd.read_parquet(OUT)
        # groupby().last() takes the newest NON-NULL value per column, so an empty field in a
        # new fetch never wipes a value already stored
        new = pd.concat([old, new], ignore_index=True).groupby(["date", "ric"], as_index=False, sort=False).last()
    new = new.sort_values(["date", "spread_months", "contract_year", "contract_month", "put_call", "strike"])
    new.to_parquet(OUT, index=False)
    return new


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--full", action="store_true", help="fetch every live RIC, keep any with OI > 0 in the window")
    args = ap.parse_args()
    full = args.full or not OUT.exists()
    end = pd.Timestamp.today().normalize()
    start = end - pd.Timedelta(days=args.days)
    t0 = time.time()
    ld.open_session()
    try:
        meta = discover()
        if full:
            universe = meta.ric.tolist()
            log.info("FULL: fetching all %d live RICs", len(universe))
        else:
            snap = oi_filter(meta.ric.tolist())
            stored = set(pd.read_parquet(OUT, columns=["ric"]).ric) & set(meta.ric)
            universe = sorted(set(snap) | stored)
            log.info("daily: %d with OI>0 today + %d already stored -> %d RICs", len(snap), len(stored), len(universe))
        hist = fetch_history(universe, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        if full and not hist.empty:
            keep = hist.groupby("ric")["oi"].max()
            keep = set(keep[keep > 0].index)
            hist = hist[hist.ric.isin(keep)]
            log.info("FULL: %d RICs had OI > 0 in the window", len(keep))
        lm = meta[meta.ric.isin(set(hist.ric) if not hist.empty else set())]
        spreads = sorted({f"{f[2:]}-{b[2:]}" for f, b in zip(lm.front_leg, lm.back_leg) if b})
        und = fetch_underlying(spreads, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        log.info("underlying spreads %s: %d rows", spreads, len(und))
    finally:
        ld.close_session()
    if hist.empty:
        log.error("no history fetched"); sys.exit(1)
    # OI is published a day after prices: the newest day's OPINT_1 stays empty until the next run.
    # (Do NOT fill it from get_data - that snapshot OI is the previous day's too.)
    hist = hist.merge(meta, on="ric", how="left")
    full = upsert(hist)
    upsert_underlying(und)
    log.info("wrote %d new rows (%d RICs, %s -> %s); parquet now %d rows; %.0fs",
             len(hist), hist.ric.nunique(), hist.date.min().date(), hist.date.max().date(), len(full), time.time() - t0)


if __name__ == "__main__":
    main()
