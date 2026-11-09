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
DEC = cm["dec"]
PX, PXC = f"{{:,.{DEC}f}}", f"{{:+,.{DEC}f}}"   # price / price-change formats
df = c.load_cso(KEY)
if df.empty:
    st.error(f"No CSO data for {cm['name']} (Database/{KEY}_cso.parquet missing or empty).")
    st.stop()
UND = c.load_underlying(KEY)
# Only COMPLETE days are offered: a day whose OI isn't published yet (prices only) is left out entirely.
OI_DATES = sorted(pd.Timestamp(d) for d in df.loc[df["oi"].notna(), "date"].unique())
DATES = OI_DATES or sorted(pd.Timestamp(d) for d in df["date"].unique())
df = df[df["date"].isin(DATES)]
def oi_on(d):
    """Latest date on or before d that has OI (OI lags prices by a day)."""
    x = [o for o in OI_DATES if o <= d]
    return x[-1] if x else None


_def_new = DATES[-1]
_def_old = DATES[max(0, DATES.index(_def_new) - 5)]   # 5 sessions back

# A date remembered from earlier in the session that is no longer offered falls back to the default.
for _k in (f"{KP}_old", f"{KP}_new"):
    if _k in st.session_state and st.session_state[_k] not in DATES:
        del st.session_state[_k]

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
    _step = st.selectbox("Strike step", cm["steps"], index=cm["steps"].index(cm["step"]), key=f"{KP}_step",
                         label_visibility="collapsed", format_func=lambda x: x if x == "Exact" else c.fmt_strike(x))
    STEP = None if _step == "Exact" else float(_step)
    label("Min OI (New Date)", 10)
    min_oi = st.number_input("Min OI", value=0, min_value=0, step=50, key=f"{KP}_min_oi", label_visibility="collapsed")

    md("<div class='sb-label' style='margin-top:16px'>Data status</div>")
    last = df[df["date"] == DATES[-1]]
    md(c.compact_table(["", "Value"], [
        [("Latest", "l d"), (c.fmt_date(DATES[-1], False), "")],
        [("From", "l d"), (c.fmt_date(DATES[0], False), "")],
        [("Contracts", "l d"), (f"{df['ric'].nunique()}", "")],
        [("OI latest", "l d"), (c.fmt_num(last["oi"].sum()), "")],
    ], cls="rpt sb").replace("class='rwrap'", "class='rwrap sb'"))

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
                       ric_lookup=RIC_ROWS, rows=ROWS, und_dec=DEC, **kw)
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
    oi_kpi = dict(calls=("Calls OI change", c.signed(c_oi), f"<span class='kt'>OI now</span>{c.fmt_num(lc)}"),
                  mid=("Net", c.signed(net), f"<span class='kt'>P/C</span>{ratio(lp, lc)}"),
                  puts=("Puts OI change", c.signed(p_oi), f"<span class='kt'>OI now</span>{c.fmt_num(lp)}"))
    vol_kpi = dict(calls=("Calls volume", c.fmt_num(c_v), "lots"),
                   mid=("Vol", "", f"<span class='kt'>P/C</span>{ratio(p_v, c_v)}"),
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
        kpi = dict(calls=("Calls OI", c.fmt_num(tc), "lots"), mid=("OI", "", f"<span class='kt'>P/C</span>{ratio(tp, tc)}"),
                   puts=("Puts OI", c.fmt_num(tp), "lots"))
        with col:
            md(f"<div class='mt'>Open Interest &middot; {tag}<span>{c.fmt_date(d)}</span></div>")
            bfly(cp, pp, c.heat_volume, und=und, kpi=kpi)


MAX_DRILL = 8


def drilldown_view():
    """Contract board (OI, OI change, volume, settle) -> tick up to MAX_DRILL contracts -> their history.
    Ported from LSEG/Options' Drill Down, with two fixes: volume covers the same window as the OI change,
    and an empty board can't produce a NaN progress-bar maximum."""
    import plotly.graph_objects as go
    if OI_NEW is None:
        md("<div class='empty'>No OI on or before the selected dates.</div>")
        return
    board = df[(df["date"] == OI_NEW) & (df["oi"] > 0) & df["series"].isin(SERIES)]
    if min_oi > 0:
        board = board[board["oi"] >= min_oi]
    board = board[["ric", "strike", "put_call", "series", "spread", "sort", "oi", "settle"]].copy()
    prev = df[df["date"] == OI_OLD].set_index("ric")["oi"] if OI_OLD is not None else pd.Series(dtype=float)
    board["chg"] = board["oi"] - board["ric"].map(prev).fillna(0)   # no Old-Date row -> counted from 0
    sess = c.sessions(df, OI_OLD, OI_NEW) if OI_OLD is not None else [OI_NEW]
    vol = df[df["date"].isin(sess)].groupby("ric")["volume"].sum(min_count=1)
    board["vol"] = board["ric"].map(vol)
    info = board.set_index("ric")

    SEL, NON, SEEN = f"{KP}_dd_sel", f"{KP}_dd_nonce", f"{KP}_dd_seen"
    sel = [r for r in st.session_state.get(SEL, []) if r in info.index]
    nonce = st.session_state.get(NON, 0)

    def name(r):
        return f"{c.fmt_strike(info.at[r, 'strike'])} {'Call' if info.at[r, 'put_call'] == 'C' else 'Put'} {info.at[r, 'series']}"

    a_, b_, _ = st.columns([1.1, 1.8, 3])
    with a_:
        label("Spread")
        exp = st.selectbox("Spread", ["All"] + SERIES, key=f"{KP}_dd_exp", label_visibility="collapsed")
    with b_:
        label("Sort by")
        with st.container(key="dd_side_box"):
            sort = st.radio("Sort", ["OI", "OI change", "Strike"], horizontal=True, key=f"{KP}_dd_sort",
                            label_visibility="collapsed")
    shown = board if exp == "All" else board[board["series"] == exp]
    if sort == "OI":
        shown = shown.sort_values(["oi", "strike"], ascending=[False, True])
    elif sort == "OI change":
        shown = shown.assign(_a=shown["chg"].abs()).sort_values(["_a", "oi"], ascending=False, na_position="last")
    else:
        shown = shown.sort_values(["strike", "sort"])

    # Each table view gets its own widget key: a dataframe's selection is a list of ROW POSITIONS, so a key
    # that outlived a re-sort or re-filter would point those positions at different contracts. Changes are
    # applied as a diff against what this exact view last reported, so picks made in other views are kept.
    sig = f"{exp}_{sort}_{OI_NEW:%Y%m%d}_{OI_OLD:%Y%m%d}_{min_oi}_{nonce}"
    seen = st.session_state.setdefault(SEEN, {})
    l, r = st.columns(2, gap="medium")
    for col, pc, opt in [(l, "C", "Call"), (r, "P", "Put")]:
        frame = shown[shown["put_call"] == pc].reset_index(drop=True)
        view = pd.DataFrame({
            " ": np.where(frame["ric"].isin(sel), "●", ""),
            "Strike": frame["strike"].map(c.fmt_strike), "Spread": frame["series"],
            "OI": frame["oi"], "OI chg": frame["chg"], "Volume": frame["vol"], "Settle": frame["settle"],
            "RIC": frame["ric"]})
        side = board.loc[board["put_call"] == pc, "oi"]
        oi_max = float(side.max()) if len(side) and side.max() > 0 else 1.0
        sty = (view.style
               .map(lambda v: "" if pd.isna(v) or v == 0 else f"color:{'#1f9d6f' if v > 0 else '#c94a4a'};font-weight:600",
                    subset=["OI chg"])
               .map(lambda v: "color:#c98a1f;font-weight:700", subset=[" "])
               .format({"OI chg": lambda v: "" if pd.isna(v) else ("0" if v == 0 else f"{v:+,.0f}"),
                        "Volume": lambda v: "" if pd.isna(v) else f"{v:,.0f}",
                        "Settle": lambda v: "" if pd.isna(v) else PX.format(v)}))
        with col:
            md(f"<div class='mt'>{opt}s<span>{len(frame)} contracts</span></div>")
            key = f"{KP}_dd_{opt}_{sig}"
            ev = st.dataframe(
                sty, key=key, on_select="rerun", selection_mode="multi-row", hide_index=True,
                width="stretch", height=420,
                column_config={
                    " ": st.column_config.TextColumn(" ", width=28),
                    "Strike": st.column_config.TextColumn("Strike", width="small"),
                    "Spread": st.column_config.TextColumn("Spread", width="small"),
                    "OI": st.column_config.ProgressColumn("OI", min_value=0, max_value=oi_max, format="%d",
                                                          color="#0a2463" if opt == "Call" else "#c94a4a"),
                    "OI chg": st.column_config.TextColumn("OI chg"),
                    "Volume": st.column_config.TextColumn("Volume"),
                    "Settle": st.column_config.TextColumn("Settle"),
                    "RIC": st.column_config.TextColumn("RIC"),
                })
            now = [frame.at[i, "ric"] for i in ev.selection.get("rows", []) if i < len(frame)]
            was = seen.get(key, [])
            if now != was:
                added = [x for x in now if x not in was]
                removed = [x for x in was if x not in now]
                cur = [x for x in st.session_state.get(SEL, []) if x not in removed]
                cur += [x for x in added if x not in cur]
                if len(cur) > MAX_DRILL:
                    st.toast(f"Up to {MAX_DRILL} contracts; the extra ones were not added.")
                st.session_state[SEL] = cur[:MAX_DRILL]
                seen[key] = now
                st.rerun()

    if not sel:
        md("<div class='empty'>Nothing selected yet. Tick contracts in the tables above.</div>")
        return

    def refresh_tables():
        st.session_state[NON] = nonce + 1          # fresh tables so no stale tick survives a removal
        st.session_state[SEEN] = {}

    def on_chosen():
        keep = set(st.session_state[f"{KP}_dd_chosen"] or [])
        st.session_state[SEL] = [x for x in st.session_state[SEL] if x in keep]
        refresh_tables()

    def on_clear():
        st.session_state[SEL] = []
        refresh_tables()

    st.session_state[f"{KP}_dd_chosen"] = list(sel)
    l, r = st.columns([6, 1])
    with l:
        md(f"<div class='mt'>Selected<span>{len(sel)} of {MAX_DRILL}, click one to remove it</span></div>")
        with st.container(key="dd_chosen_box"):
            st.pills("Selected", sel, selection_mode="multi", key=f"{KP}_dd_chosen", on_change=on_chosen,
                     format_func=lambda x: f"{name(x)}  ·  {info.at[x, 'oi']:,.0f} OI", label_visibility="collapsed")
    with r:
        md("<div style='height:34px'></div>")
        st.button("Clear all", key=f"{KP}_dd_clear", on_click=on_clear)

    picks = [dict(ric=x, label=name(x), spread=info.at[x, "spread"]) for x in sel]
    series = {p["ric"]: df[df["ric"] == p["ric"]].set_index("date").sort_index() for p in picks}
    yf = f",.{DEC}f"
    figs = []
    for field, title, kind, fmt in [("oi", "Open interest", "line", ",.0f"), ("volume", "Volume", "bar", ",.0f"),
                                    ("settle", "Settle", "line", yf)]:
        fig = go.Figure()
        for i, p in enumerate(picks):
            s = series[p["ric"]][field].dropna()
            if kind == "bar":
                s = s[s != 0]
            if s.empty:
                continue
            colr = c.SERIES_COLORS[i % len(c.SERIES_COLORS)]
            ht = f"{p['label']}: %{{y:{fmt}}}<extra></extra>"
            fig.add_trace(go.Bar(x=s.index, y=s.values, name=p["label"], marker_color=colr, hovertemplate=ht)
                          if kind == "bar" else
                          go.Scatter(x=s.index, y=s.values, mode="lines", name=p["label"],
                                     line=dict(color=colr, width=1.8), hovertemplate=ht))
        c.chart_layout(fig, title, height=300)
        fig.update_layout(barmode="stack", bargap=0.1)
        figs.append((fig, title))
    # 4th chart: the futures spread(s) underneath the picks (CSOs have no implied vol on LSEG)
    fig = go.Figure()
    for i, sp in enumerate(dict.fromkeys(p["spread"] for p in picks)):
        u = UND[(UND["spread"] == sp) & UND["date"].isin(DATES)].sort_values("date")
        if not u.empty:
            fig.add_trace(go.Scatter(x=u["date"], y=u["settle"], mode="lines", name=sp,
                                     line=dict(color=c.SERIES_COLORS[i % len(c.SERIES_COLORS)], width=1.8),
                                     hovertemplate=f"{sp}: %{{y:{yf}}}<extra></extra>"))
    c.chart_layout(fig, "Futures spread", height=300)
    figs.append((fig, "Futures spread"))
    for pair_ in (figs[:2], figs[2:]):
        cols = st.columns(2, gap="medium")
        for col, (fig, title) in zip(cols, pair_):
            with col:
                if fig.data:
                    st.plotly_chart(fig, width="stretch", key=f"{KP}_dd_fig_{title}", config={"displaylogo": False})
                else:
                    md(f"<div class='empty'>No {title.lower()} data for the selected contracts.</div>")


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


# =============================================================================================
# Prices
# =============================================================================================
def settle_view():
    l, r = pair()
    for col, d, tag, und in [(l, old_date, "Old", UND_OLD), (r, new_date, "New", UND_NEW)]:
        with col:
            md(f"<div class='mt'>Settle &middot; {tag}<span>{c.fmt_date(d)}</span></div>")
            bfly(mo(c.level(df, d, "C", "settle"), "C"), mo(c.level(df, d, "P", "settle"), "P"),
                 c.heat_level, fmt=PX, und=und, footer=False, how="nearest")


def settle_change_view():
    cs = mo(c.change(df, old_date, new_date, "C", "settle", new_from_zero=False), "C")
    ps = mo(c.change(df, old_date, new_date, "P", "settle", new_from_zero=False), "P")
    md(f"<div class='mt'>Settle Change<span>{c.fmt_date(old_date, False)} &rarr; {c.fmt_date(new_date)} &middot; {cm['unit']}</span></div>")
    bfly(cs, ps, c.heat_signed, fmt=PXC, footer=False, how="nearest")
    rows = []
    for sp in sorted(set(META["spread"]), key=lambda s: META.loc[META["spread"] == s, "sort"].min()):
        o, n = UND_OLD.get(sp), UND_NEW.get(sp)
        ch = (n - o) if o is not None and n is not None else np.nan
        rows.append([(sp, "l d"), (c.fmt_num(o, PX), ""), (c.fmt_num(n, PX), ""), (c.signed(ch, PXC), "")])
    md("<div class='mt'>Futures spreads<span>front minus back</span></div>")
    md(c.compact_table(["Spread", c.fmt_date(old_date, False), c.fmt_date(new_date, False), "Change"], rows))


if not SERIES:
    md("<div class='empty'>No series for this filter.</div>")
elif section == "Positioning":
    view = pills("View", ["OI change & volume", "OI levels", "Drill Down", "History"], "view", "view_box")
    {"OI change & volume": oi_change_view, "OI levels": oi_levels_view, "Drill Down": drilldown_view,
     "History": history_view}[view]()
else:
    view = pills("View", ["Settle", "Settle change"], "pview", "view_box")
    {"Settle": settle_view, "Settle change": settle_change_view}[view]()
