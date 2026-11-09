"""probe_lrc_lcc_cso.py - discover Robusta (LRC) and London Cocoa (LCC) 1-month CSO boards on LSEG."""
import re, logging, pandas as pd, lseg.data as ld
from lseg.data import discovery
logging.getLogger("httpx").setLevel(logging.WARNING)
pd.set_option("display.width", 250)
ld.open_session()
for p in ["LCC1", "LCC2"]:
    b = discovery.search(view=discovery.Views.DERIVATIVE_QUOTES, top=10000,
                         select="RIC,DTSubjectName,ExpiryDate,StrikePrice,UnderlyingQuoteRIC", filter=f"startswith(RIC,'{p}')")
    b = b[~b.RIC.str.startswith("0#") & b.DTSubjectName.str.contains("Calendar Spread Option", na=False) & ~b.DTSubjectName.str.contains("Volatility", na=False)]
    b = b[pd.to_datetime(b.ExpiryDate) >= pd.Timestamp.today().normalize()]
    print(f"\n=== {p}: {len(b)} live RICs")
    if b.empty: continue
    pat = re.compile(rf"^{p}(\d+)([A-X])(\d{{1,2}})([PN])$")
    print("regex match:", b.RIC.str.match(pat).sum(), "/", len(b), " non-match:", b.loc[~b.RIC.str.match(pat), "RIC"].head(5).tolist())
    print(b.sample(min(5, len(b)), random_state=3)[["RIC", "DTSubjectName", "StrikePrice", "UnderlyingQuoteRIC"]].to_string())
    b = b[b.RIC.str.match(pat)]
    if p.endswith("1"):
        snap = pd.concat([ld.get_data(universe=b.RIC.iloc[i:i+100].tolist(), fields=["OPINT_1", "SETTLE"]) for i in range(0, len(b), 100)])
        s = snap.merge(b, left_on="Instrument", right_on="RIC")
        s["OPINT_1"] = pd.to_numeric(s.OPINT_1, errors="coerce"); s["exp"] = s.ExpiryDate.astype(str).str[:10]
        print(s.groupby("exp").agg(rics=("RIC", "count"), oi=("OPINT_1", "sum"), oi_rics=("OPINT_1", lambda x: (x > 0).sum()),
                                   kmin=("StrikePrice", "min"), kmax=("StrikePrice", "max"), und=("UnderlyingQuoteRIC", lambda x: str(x.iloc[0]))).to_string())
for u in ["LRCF7-H7", "LRCX6-F7", "LCCH7-K7", "LCCZ6-H7"]:
    try:
        h = ld.get_history(universe=u, fields=["SETTLE"], start="2026-11-01", end="2026-11-09", count=50)
        print(u, h.tail(2).to_dict())
    except Exception as e:
        print(u, "ERR", str(e)[:80])
ld.close_session()
