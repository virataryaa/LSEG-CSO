"""
app.py - Calendar Spread Options dashboard (LSEG data). KC for now.

Sidebar   : commodity, Old / New Date, strike step, min OI, data status
Sections  : Positioning - OI change & volume butterflies, OI levels, OI history
            Prices      - settle levels, settle change
Every butterfly has calls on the left, the signed spread strike in the centre and puts on the right.
Columns are spread series (for example 1M Z6-H7). The amber row under the headers is the futures
spread settle, and the outlined cell in each column is that series' at-the-money strike.
Theme and login are the same as LSEG/Options/Dashboard.
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

for _t in ("plotly", "plotly_white"):
    pio.templates[_t].layout.font.family = "Plus Jakarta Sans, sans-serif"

import common as c

st.set_page_config(page_title="CSO", layout="wide")

# ---------------------------------------------------------------------------------------------
# Password gate. Password lives in .streamlit/secrets.toml locally, and in this app's own
# "Secrets" box on Streamlit Cloud (Settings > Secrets) - never committed, never hardcoded.
# A correct login is remembered for 24h in the browser's localStorage (see _check_password).
# ---------------------------------------------------------------------------------------------
@st.cache_resource
def _dm_login_bridge():
    """Invisible component that keeps the signed 24h login token in the browser's localStorage and hands it
    back to Python. Works through any proxy: it only uses the normal browser <-> app connection."""
    import tempfile
    from pathlib import Path
    import streamlit.components.v1 as components
    page = """<!doctype html><html><body><script>
var KEY = "_dm_auth", last = null;
function post(m) { m.isStreamlitMessage = true; window.parent.postMessage(m, "*"); }
window.addEventListener("message", function (ev) {
  if (!ev.data || ev.data.type !== "streamlit:render") return;
  var a = ev.data.args || {};
  try { if (a.write) localStorage.setItem(KEY, a.write); } catch (e) {}
  var v = ""; try { v = localStorage.getItem(KEY) || ""; } catch (e) {}
  if (v !== last) { last = v; post({type: "streamlit:setComponentValue", value: v, dataType: "json"}); }
});
post({type: "streamlit:componentReady", apiVersion: 1});
post({type: "streamlit:setFrameHeight", height: 0});
</script></body></html>"""
    d = Path(tempfile.gettempdir()) / "dm_auth_bridge"
    d.mkdir(exist_ok=True)
    (d / "index.html").write_text(page, encoding="utf-8")
    return components.declare_component("dm_auth_bridge", path=str(d))


def _check_password() -> bool:
    if st.session_state.get("_authed"):
        return True
    import hashlib, hmac, time
    real = None
    try:
        real = st.secrets.get("auth", {}).get("password")
    except Exception:
        pass
    typed = st.session_state.get("_pw_input") or ""
    ok = bool(real) and typed == real

    def _sig(exp):
        return hmac.new(real.encode(), f"v1|{exp}".encode(), hashlib.sha256).hexdigest()

    # A correct login is remembered for 24h as a signed token (HMAC of the expiry, keyed on the password
    # itself - changing the password logs everyone out) in the browser's localStorage. The invisible
    # component below reads it back on every visit, so refresh / new tab / reconnect don't ask again.
    exp = int(time.time()) + 86400
    stored = None
    try:
        stored = _dm_login_bridge()(write=(f"{exp}.{_sig(exp)}" if ok else None), key="_dm_login_bridge", default=None)
    except Exception:
        pass
    try:
        e, _, s = (stored or "").partition(".")
        if real and e.isdigit() and int(e) > time.time() and hmac.compare_digest(s, _sig(e)):
            ok = True
    except Exception:
        pass
    if ok:
        st.session_state["_authed"] = True
        return True
    st.markdown(
        "<div style='max-width:340px;margin:120px auto 0;text-align:center'>"
        "<div style=\"font-family:Plus Jakarta Sans, sans-serif;font-size:26px;font-weight:600;color:#0a2463;"
        "margin-bottom:18px\">CSO</div></div>", unsafe_allow_html=True)
    _l, _c, _r = st.columns([1, 1, 1])
    with _c:
        st.text_input("Password", type="password", label_visibility="collapsed",
                      placeholder="Password", key="_pw_input")
        if typed:
            st.error("Incorrect password.")
    return False


st.markdown(c.CSS, unsafe_allow_html=True)
if not _check_password():
    st.stop()


def md(html: str):
    st.markdown(html, unsafe_allow_html=True)


def label(text: str, top: int = 0):
    md(f"<div class='sb-label' style='margin:{top}px 0 2px'>{text}</div>")


def pills(name, options, key, box, index=0):
    with st.container(key=box):
        return st.radio(name, options, index=index, horizontal=True, label_visibility="collapsed", key=key)


# =============================================================================================
# Sidebar
# =============================================================================================
LABELS = [cm["label"] for cm in c.COMMODITIES]
with st.sidebar:
    md("<div class='sb-title'>CSO</div><div class='sb-sub'>Calendar spread options &middot; LSEG data</div>")
    md("<div class='sb-label'>Commodity</div>")
    picked = st.radio("Commodity", LABELS, label_visibility="collapsed", key="commodity")

cm = next(x for x in c.COMMODITIES if x["label"] == picked)
KEY, KP = cm["key"], cm["key"].lower()
df = c.load_cso(KEY)
if df.empty:
    st.error(f"No CSO data for {cm['name']} (Database/{KEY}_cso.parquet missing or empty).")
    st.stop()
UND = c.load_underlying(KEY)
DATES = sorted(pd.Timestamp(d) for d in df["date"].unique())
OI_DATES = sorted(pd.Timestamp(d) for d in df.loc[df["oi"].notna(), "date"].unique())
def oi_on(d):
    """Latest date on or before d that has OI (OI lags prices by a day)."""
    x = [o for o in OI_DATES if o <= d]
    return x[-1] if x else None


_def_new = OI_DATES[-1] if OI_DATES else DATES[-1]   # OI comes out a day after prices
_def_old = DATES[max(0, DATES.index(_def_new) - 5)]   # 5 sessions back

with st.sidebar:
    label("Old date", 10)
    old_date = st.selectbox("Old date", DATES, index=DATES.index(_def_old), key=f"{KP}_old",
                            format_func=c.fmt_date, label_visibility="collapsed")
    label("New date", 4)
    new_date = st.selectbox("New date", DATES, index=DATES.index(_def_new), key=f"{KP}_new",
                            format_func=c.fmt_date, label_visibility="collapsed")
    if old_date == new_date:
        md("<div class='note warn'>Old and New date are the same.</div>")
    elif old_date > new_date:
        md("<div class='note warn'>Old date is after New date, so changes are reversed.</div>")

    label("Strike step", 10)
    _step = st.selectbox("Strike step", ["Exact", 0.25, 0.5, 1.0, 2.5, 5.0], index=3, key=f"{KP}_step",
                         label_visibility="collapsed", format_func=lambda x: x if x == "Exact" else c.fmt_strike(x))
    STEP = None if _step == "Exact" else float(_step)
    label("Min OI (New Date)", 10)
    min_oi = st.number_input("Min OI", value=0, min_value=0, step=50, key=f"{KP}_min_oi", label_visibility="collapsed")

    md("<div class='sb-label' style='margin-top:16px'>Data status</div>")
    last = df[df["date"] == (OI_DATES[-1] if OI_DATES else DATES[-1])]
    md(c.compact_table(["", "Value"], [
        [("Prices to", "l d"), (c.fmt_date(DATES[-1], False), "")],
        [("OI to", "l d"), (c.fmt_date(OI_DATES[-1], False) if OI_DATES else "&mdash;", "")],
        [("From", "l d"), (c.fmt_date(DATES[0], False), "")],
        [("Contracts", "l d"), (f"{df['ric'].nunique()}", "")],
        [("OI latest", "l d"), (c.fmt_num(last["oi"].sum()), "")],
    ], cls="rpt sb").replace("class='rwrap'", "class='rwrap sb'"))
    md("<div class='note'>Only contracts with open interest are fetched.</div>")

# series shown: those with any row on either date
_both = df[df["date"].isin([old_date, new_date])]
META = c.series_meta(_both if not _both.empty else df)
SERIES = list(META["series"])
RICS = c.ric_lookup(df)
LADDER = c.strike_ladder(df, SERIES)
if STEP is None:
    ROWS, RIC_ROWS = LADDER, RICS
else:
    ROWS = c.uniform_ladder(LADDER, STEP)
    RIC_ROWS = c.project_rics(RICS, ROWS)
OFF_STEP = [k for k in LADDER if k not in set(ROWS)]
UND_NEW = c.und_on(UND, new_date)
OI_NEW, OI_OLD = oi_on(new_date), oi_on(old_date)
UND_OLD = c.und_on(UND, old_date)

md(f"<div class='ph'><span class='t'>{cm['name']}</span>"
   f"<span class='m'>{cm['exch']} &middot; {KEY} calendar spread options &middot; {cm['unit']} &middot; "
   f"<b>{c.fmt_date(old_date)}</b> &rarr; <b>{c.fmt_date(new_date)}</b></span></div>")

section = pills("Section", ["Positioning", "Prices"], "section", "sec_box")


def pair():
    return st.columns(2, gap="medium")


def bfly(cp, pp, color, fmt="{:,.0f}", und=None, how="sum", **kw):
    if STEP is not None:
        cp, pp = c.project(cp, ROWS, how), c.project(pp, ROWS, how)
    html = c.butterfly(cp, pp, META, UND_NEW if und is None else und, color, fmt=fmt, title=KEY,
                       ric_lookup=RIC_ROWS, rows=ROWS, **kw)
    st.html(html + c.COPY_JS, unsafe_allow_javascript=True)


def mo(piv, pc):
    return c.apply_min_oi(piv, df, OI_NEW, pc, min_oi) if OI_NEW is not None else piv


def ratio(a, b):
    return f"{a / b:.2f}" if b and not np.isnan(a) and not np.isnan(b) else "&mdash;"


# =============================================================================================
# Positioning
# =============================================================================================
def oi_change_view():
    if OI_NEW is None or OI_OLD is None:
        md("<div class='empty'>No OI on or before the selected dates.</div>")
        return
    und = c.und_on(UND, OI_NEW)
    coi, poi = mo(c.change(df, OI_OLD, OI_NEW, "C", "oi"), "C"), mo(c.change(df, OI_OLD, OI_NEW, "P", "oi"), "P")
    # volume over the same window as the OI change, so the two tables compare like with like
    cvol, pvol = mo(c.volume(df, OI_OLD, OI_NEW, "C"), "C"), mo(c.volume(df, OI_OLD, OI_NEW, "P"), "P")
    sess = c.sessions(df, OI_OLD, OI_NEW)
    c_oi, p_oi = c.total(coi, SERIES), c.total(poi, SERIES)
    c_v, p_v = c.total(cvol, SERIES), c.total(pvol, SERIES)
    lc = c.total(mo(c.level(df, OI_NEW, "C", "oi"), "C"), SERIES)
    lp = c.total(mo(c.level(df, OI_NEW, "P", "oi"), "P"), SERIES)
    net = np.nansum([c_oi, p_oi]) if not (np.isnan(c_oi) and np.isnan(p_oi)) else np.nan
    oi_kpi = dict(calls=("Calls OI change", c.signed(c_oi), f"OI now {c.fmt_num(lc)}"),
                  mid=("Net", c.signed(net), f"P/C {ratio(lp, lc)}"),
                  puts=("Puts OI change", c.signed(p_oi), f"OI now {c.fmt_num(lp)}"))
    vol_kpi = dict(calls=("Calls volume", c.fmt_num(c_v), "lots"),
                   mid=("P/C", ratio(p_v, c_v)),
                   puts=("Puts volume", c.fmt_num(p_v), "lots"))
    l, r = pair()
    with l:
        md(f"<div class='mt'>Open Interest Change<span>{c.fmt_date(OI_OLD, False)} &rarr; {c.fmt_date(OI_NEW)}</span></div>")
        bfly(coi, poi, c.heat_signed, fmt="{:+,.0f}", kpi=oi_kpi, und=und)
    with r:
        span = (c.fmt_date(sess[0], False) + " &ndash; " if len(sess) > 1 else "") + c.fmt_date(sess[-1]) if sess else "&mdash;"
        md(f"<div class='mt'>Volume<span>{len(sess)} session{'s' if len(sess) != 1 else ''}, {span}</span></div>")
        bfly(cvol, pvol, c.heat_volume, kpi=vol_kpi, und=und)


def oi_levels_view():
    if OI_NEW is None or OI_OLD is None:
        md("<div class='empty'>No OI on or before the selected dates.</div>")
        return
    l, r = pair()
    for col, d, tag in [(l, OI_OLD, "Old"), (r, OI_NEW, "New")]:
        und = c.und_on(UND, d)
        cp, pp = mo(c.level(df, d, "C", "oi"), "C"), mo(c.level(df, d, "P", "oi"), "P")
        tc, tp = c.total(cp, SERIES), c.total(pp, SERIES)
        kpi = dict(calls=("Calls OI", c.fmt_num(tc), "lots"), mid=("P/C", ratio(tp, tc)),
                   puts=("Puts OI", c.fmt_num(tp), "lots"))
        with col:
            md(f"<div class='mt'>Open Interest &middot; {tag}<span>{c.fmt_date(d)}</span></div>")
            bfly(cp, pp, c.heat_volume, und=und, kpi=kpi)


def history_view():
    sub = df[df["series"].isin(SERIES) & df["oi"].notna()]   # newest price day has no OI yet: not a 0
    daily = sub.groupby(["date", "series"], as_index=False)["oi"].sum()
    order = list(META["series"])
    fig = go.Figure()
    for i, s in enumerate(order):
        d = daily[daily["series"] == s]
        if d.empty:
            continue
        fig.add_trace(go.Scatter(x=d["date"], y=d["oi"], name=s, mode="lines+markers",
                                 line=dict(width=2, color=c.SERIES_COLORS[i % len(c.SERIES_COLORS)]), marker=dict(size=4)))
    c.chart_layout(fig, "Open interest by spread series", height=380)
    st.plotly_chart(fig, width="stretch", key="hist_oi", config={"displaylogo": False})

    cp = sub.groupby(["date", "put_call"], as_index=False)["oi"].sum().pivot(index="date", columns="put_call", values="oi")
    fig2 = go.Figure()
    for pc, name, colr in [("C", "Calls", c.NAVY), ("P", "Puts", c.RED)]:
        if pc in cp:
            fig2.add_trace(go.Bar(x=cp.index, y=cp[pc], name=name, marker_color=colr))
    c.chart_layout(fig2, "Open interest, calls vs puts", height=320)
    fig2.update_layout(barmode="stack")
    st.plotly_chart(fig2, width="stretch", key="hist_cp", config={"displaylogo": False})
    md("<div class='note'>Only contracts that had open interest when the data was last fetched are stored, "
       "so contracts already closed out before that don't show up in earlier dates.</div>")


# =============================================================================================
# Prices
# =============================================================================================
def settle_view():
    l, r = pair()
    for col, d, tag, und in [(l, old_date, "Old", UND_OLD), (r, new_date, "New", UND_NEW)]:
        with col:
            md(f"<div class='mt'>Settle &middot; {tag}<span>{c.fmt_date(d)}</span></div>")
            bfly(mo(c.level(df, d, "C", "settle"), "C"), mo(c.level(df, d, "P", "settle"), "P"),
                 c.heat_level, fmt="{:,.2f}", und=und, footer=False, how="nearest")


def settle_change_view():
    cs = mo(c.change(df, old_date, new_date, "C", "settle", new_from_zero=False), "C")
    ps = mo(c.change(df, old_date, new_date, "P", "settle", new_from_zero=False), "P")
    md(f"<div class='mt'>Settle Change<span>{c.fmt_date(old_date, False)} &rarr; {c.fmt_date(new_date)} &middot; {cm['unit']}</span></div>")
    bfly(cs, ps, c.heat_signed, fmt="{:+,.2f}", footer=False, how="nearest")
    rows = []
    for sp in sorted(set(META["spread"]), key=lambda s: META.loc[META["spread"] == s, "sort"].min()):
        o, n = UND_OLD.get(sp), UND_NEW.get(sp)
        ch = (n - o) if o is not None and n is not None else np.nan
        rows.append([(sp, "l d"), (c.fmt_num(o, "{:.2f}"), ""), (c.fmt_num(n, "{:.2f}"), ""), (c.signed(ch, "{:+.2f}"), "")])
    md("<div class='mt'>Futures spreads<span>front minus back</span></div>")
    md(c.compact_table(["Spread", c.fmt_date(old_date, False), c.fmt_date(new_date, False), "Change"], rows))


if not SERIES:
    md("<div class='empty'>No series for this filter.</div>")
elif section == "Positioning":
    view = pills("View", ["OI change & volume", "OI levels", "History"], "view", "view_box")
    {"OI change & volume": oi_change_view, "OI levels": oi_levels_view, "History": history_view}[view]()
else:
    view = pills("View", ["Settle", "Settle change"], "pview", "view_box")
    {"Settle": settle_view, "Settle change": settle_change_view}[view]()
