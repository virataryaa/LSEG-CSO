"""probe_kc_cso.py - discover KC Calendar Spread Option RICs on LSEG and fetch a handful."""
import pandas as pd, lseg.data as ld
from lseg.data import discovery
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 20)
ld.open_session()
SEL = "RIC,DTSubjectName,ExpiryDate,StrikePrice,PutCallIndicator,UnderlyingQuoteRIC,ExchangeCode"
def srch(label, **kw):
    try:
        df = discovery.search(view=discovery.Views.DERIVATIVE_QUOTES, select=SEL, top=50, **kw)
        print(f"\n=== {label}: {len(df)} rows"); print(df.head(25).to_string())
        return df
    except Exception as e:
        print(f"\n=== {label}: ERR {e}"); return pd.DataFrame()
srch("query coffee calendar spread", query="coffee calendar spread option")
for p in ["1KC1C", "KC1C", "1KCS", "KCS", "1KC2C", "1KC1S"]:
    srch(f"startswith {p}", filter=f"startswith(RIC,'{p}')")
srch("name contains Calendar & Coffee", query="Coffee CSO")
# image nomenclature guesses
tries = ["KC1C2200E27P","KC1C100E27N","KC1C0E27P","1KC1C0E27P","KC1C200L26N","KC1C0X26P"]
try:
    print("\n=== get_data on image-pattern RICs"); print(ld.get_data(universe=tries, fields=["SETTLE","OPINT_1","ACVOL_UNS","DSPLY_NAME","EXPIR_DATE"]).to_string())
except Exception as e: print("ERR", e)
ld.close_session()
