"""probe_kc_cso_2.py - full KC CSO board count by spread type + history fetch for a handful."""
import re, pandas as pd, lseg.data as ld
from lseg.data import discovery
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 20)
ld.open_session()
SEL = "RIC,DTSubjectName,ExpiryDate,StrikePrice,PutCallIndicator,UnderlyingQuoteRIC"
frames = {}
for p in ["KC1C", "KC2C", "KC3C", "KC4C", "KC6C", "KC1S", "KCS"]:
    try:
        df = discovery.search(view=discovery.Views.DERIVATIVE_QUOTES, select=SEL, top=10000, filter=f"startswith(RIC,'{p}')")
    except Exception as e:
        print(p, "ERR", e); continue
    frames[p] = df
    print(f"\n=== {p}: {len(df)} rows")
    if len(df): print(df.sample(min(6, len(df)), random_state=1).to_string())
kc = frames.get("KC1C", pd.DataFrame())
if len(kc):
    pat = re.compile(r"^KC1C(\d+)([A-X])(\d{2})([PN])$")
    kc["ok"] = kc.RIC.str.match(pat)
    print("\nKC1C regex match:", kc.ok.sum(), "/", len(kc), "  non-matching sample:", kc.loc[~kc.ok, "RIC"].head(10).tolist())
    kc["und"] = kc.UnderlyingQuoteRIC.astype(str)
    print("\nKC1C by expiry / underlying:"); print(kc.groupby(["ExpiryDate", "und"]).size().to_string())
    print("\nstrike range:", kc.StrikePrice.min(), kc.StrikePrice.max(), " distinct strikes:", kc.StrikePrice.nunique())
    rics = kc.loc[kc.ok].RIC.tolist()
    snap = ld.get_data(universe=rics, fields=["SETTLE", "OPINT_1", "ACVOL_UNS", "IMP_VOLT"])
    snap = snap.merge(kc[["RIC", "StrikePrice", "ExpiryDate", "und"]], left_on="Instrument", right_on="RIC")
    snap["OPINT_1"] = pd.to_numeric(snap.OPINT_1, errors="coerce")
    print("\nRICs with OI>0:", (snap.OPINT_1 > 0).sum(), " total OI:", snap.OPINT_1.sum())
    print("\nOI by expiry:"); print(snap.groupby("ExpiryDate").OPINT_1.sum().to_string())
    top = snap.sort_values("OPINT_1", ascending=False).head(12)
    print("\nTop OI:"); print(top[["Instrument", "StrikePrice", "ExpiryDate", "und", "SETTLE", "OPINT_1", "ACVOL_UNS", "IMP_VOLT"]].to_string())
    h = ld.get_history(universe=top.Instrument.head(4).tolist(), fields=["SETTLE", "OPINT_1", "ACVOL_UNS", "IMP_VOLT"], start="2026-09-01", interval="daily")
    print("\nHistory (4 top-OI RICs):", h.shape); print(h.tail(10).to_string())
    h1 = ld.get_history(universe=top.Instrument.iloc[0], fields=["SETTLE", "OPINT_1"], start="2020-01-01", interval="daily")
    print("\nDepth for", top.Instrument.iloc[0], ":", h1.index.min(), "->", h1.index.max(), len(h1), "rows")
    kc.to_csv("../Database/_probe_kc1c_board.csv", index=False)
ld.close_session()
