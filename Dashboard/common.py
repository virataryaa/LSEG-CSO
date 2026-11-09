"""
common.py - data loading, pivots, formatting and theme for the CSO dashboard.

The theme, heat colours, KPI strip and table CSS (everything below the "shared" marker) are copied
from LSEG/Options/Dashboard/common.py, so the two apps look the same. The butterfly is adapted:
- columns are SPREAD SERIES (for example 1M Z6-H7) instead of option months
- rows are SIGNED spread strikes
- every column marks its own at-the-money cell, because each series has a different underlying
  (the ATM cell is the strike nearest that futures spread's settle)
"""
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent.parent / "Database"
CALL_CODES = "ABCDEFGHIJKL"
PUT_CODES = "MNOPQRSTUVWX"
NAVY, TEAL, GREEN, RED, AMBER, GREY = "#0a2463", "#1f8a9c", "#1f9d6f", "#c94a4a", "#c98a1f", "#8a94a8"
SERIES_COLORS = [NAVY, RED, TEAL, AMBER, GREEN, "#9b6bb3", "#6b7fb5", "#e0763c"]

COMMODITIES = [
    dict(key="KC", label="Arabica", name="Arabica Coffee CSO", exch="ICE US", unit="c/lb"),
]


# ---- formatting ---------------------------------------------------------------------------------
def fmt_strike(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{int(x)}" if x.is_integer() else f"{x:g}"


def fmt_num(v, f="{:,.0f}") -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "&mdash;"
    return "&mdash;" if np.isnan(v) else f.format(v)


def fmt_date(d, year=True) -> str:
    return pd.Timestamp(d).strftime("%d %b %Y" if year else "%d %b")


# ---- loading ------------------------------------------------------------------------------------
def _mtime(p: Path) -> float:
    return p.stat().st_mtime if p.exists() else 0.0


@st.cache_data(show_spinner=False)
def _load(path: str, mtime: float) -> pd.DataFrame:
    p = Path(path)
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def load_cso(key: str) -> pd.DataFrame:
    p = DB_PATH / f"{key}_cso.parquet"
    df = _load(str(p), _mtime(p)).copy()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    for col in ("settle", "oi", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    df["spread"] = df["front_leg"].str[2:] + "-" + df["back_leg"].str[2:]
    df["series"] = df["spread_months"].astype(str) + "M " + df["spread"]
    df["sort"] = df["contract_year"] * 100 + df["contract_month"] + df["spread_months"] / 10
    return df


def load_underlying(key: str) -> pd.DataFrame:
    p = DB_PATH / f"{key}_cso_underlying.parquet"
    u = _load(str(p), _mtime(p)).copy()
    if not u.empty:
        u["date"] = pd.to_datetime(u["date"])
    return u


# ---- series / pivots ----------------------------------------------------------------------------
def series_meta(df: pd.DataFrame) -> pd.DataFrame:
    """One row per spread series, ordered by front-leg month, then width."""
    m = (df.drop_duplicates("series")[["series", "spread", "spread_months", "contract_month", "contract_year", "sort", "expiry"]]
         .sort_values("sort").reset_index(drop=True))
    m["ccode"] = [f"{CALL_CODES[mo - 1]}{str(y)[-2:]}" for mo, y in zip(m.contract_month, m.contract_year)]
    m["pcode"] = [f"{PUT_CODES[mo - 1]}{str(y)[-2:]}" for mo, y in zip(m.contract_month, m.contract_year)]
    return m


def _day(df, d, pc):
    return df[(df["date"] == d) & (df["put_call"] == pc)]


def pivot(frame: pd.DataFrame, value: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    return frame.pivot_table(index="strike", columns="series", values=value, aggfunc="first")


def level(df, d, pc, field):
    return pivot(_day(df, d, pc), field)


def change(df, old, new, pc, field, new_from_zero=True):
    """New minus Old. With new_from_zero, a contract that has no Old-Date OI counts from 0."""
    a, b = level(df, old, pc, field), level(df, new, pc, field)
    if a.empty and b.empty:
        return b
    ix, cols = a.index.union(b.index), a.columns.union(b.columns)
    a, b = a.reindex(index=ix, columns=cols), b.reindex(index=ix, columns=cols)
    if new_from_zero and field == "oi":
        a = a.where(a.notna() | b.isna(), 0.0)
        b = b.where(b.notna() | a.isna(), 0.0)   # contract gone by New Date -> its OI fell to 0
    return b - a


def volume(df, old, new, pc):
    """Volume summed over the sessions after Old Date up to and including New Date."""
    if old == new:
        w = _day(df, new, pc)
    else:
        w = df[(df["date"] > min(old, new)) & (df["date"] <= max(old, new)) & (df["put_call"] == pc)]
    if w.empty:
        return pd.DataFrame()
    return w.groupby(["strike", "series"])["volume"].sum(min_count=1).unstack("series")


def sessions(df, old, new):
    if old == new:
        return [pd.Timestamp(new)]
    return [pd.Timestamp(d) for d in sorted(df["date"].unique()) if min(old, new) < d <= max(old, new)]


def apply_min_oi(piv, df, new, pc, min_oi):
    """Blank the cells whose OI on the New Date is below min_oi."""
    if not min_oi or piv is None or piv.empty:
        return piv
    oi = level(df, new, pc, "oi").reindex(index=piv.index, columns=piv.columns)
    return piv.where(oi.fillna(0) >= min_oi)


def und_on(u: pd.DataFrame, d) -> dict:
    """{spread: futures spread settle} on the latest date on or before d."""
    if u.empty:
        return {}
    x = u[u["date"] <= d].sort_values("date").groupby("spread").last()
    return x["settle"].to_dict()


def total(piv, series):
    if piv is None or piv.empty:
        return float("nan")
    sub = piv.reindex(columns=[s for s in series if s in piv.columns])
    return float(np.nansum(sub.to_numpy(dtype=float))) if sub.notna().to_numpy().any() else float("nan")


# ---- butterfly ----------------------------------------------------------------------------------
def ric_lookup(df: pd.DataFrame) -> dict:
    """{"C"|"P": {(strike, series): ric}} - a RIC doesn't depend on the date, so one map serves every table."""
    u = df.drop_duplicates(["put_call", "strike", "series"])
    return {pc: {(float(k), s): r for k, s, r in zip(g["strike"], g["series"], g["ric"])}
            for pc, g in u.groupby("put_call")}


# Click a cell -> copy its RIC. Attached once per page (guarded), delegated so it covers every table.
COPY_JS = """<script>
if (!window._csoCopy) {
  window._csoCopy = true;
  document.addEventListener('click', function (e) {
    var td = e.target.closest && e.target.closest('td[data-ric]');
    if (!td) return;
    var ric = td.getAttribute('data-ric');
    var done = function () { td.classList.add('copied'); setTimeout(function () { td.classList.remove('copied'); }, 900); };
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(ric).then(done); }
    else { var t = document.createElement('textarea'); t.value = ric; document.body.appendChild(t); t.select();
           try { document.execCommand('copy'); done(); } catch (err) {} document.body.removeChild(t); }
  });
}
</script>"""


def strike_ladder(df: pd.DataFrame, series) -> list:
    """Every strike stored for the shown series, on any date. All tables use this one ladder,
    so no strike is ever hidden and paired tables have identical rows and height."""
    return sorted(float(k) for k in df.loc[df["series"].isin(list(series)), "strike"].unique())


def uniform_ladder(strikes, step: float) -> list:
    """Evenly spaced rows at `step`, covering every stored strike."""
    s = np.asarray(sorted(strikes), dtype=float)
    if not s.size:
        return []
    lo, hi = np.floor(s[0] / step) * step, np.ceil(s[-1] / step) * step
    return [round(float(x), 6) for x in np.arange(lo, hi + step / 2, step)]


def snap(strikes, rows) -> np.ndarray:
    """Each strike -> its nearest row (a tie goes to the lower row)."""
    s, r = np.asarray(strikes, dtype=float), np.asarray(rows, dtype=float)
    return r[np.abs(s[:, None] - r[None, :]).argmin(axis=1)]


def project(piv, rows, how="sum"):
    """Put a strike x series pivot onto `rows`. Off-row strikes are summed into the nearest row
    (how="sum": OI, volume, OI change) or the closest strike wins (how="nearest": settles)."""
    if piv is None or piv.empty:
        return piv
    src = np.asarray(piv.index, dtype=float)
    tgt = snap(src, rows)
    p = piv.astype(float).copy()
    if how == "sum":
        out = p.groupby(tgt).sum(min_count=1)
    else:
        order = np.argsort(np.abs(src - tgt), kind="stable")
        out = p.iloc[order].groupby(tgt[order]).first()
    return out.reindex([float(x) for x in rows])


def project_rics(lookup: dict, rows) -> dict:
    """{"C"|"P": {(row, series): [ric, ...]}} after snapping strikes to `rows`."""
    out = {}
    for pc, m in lookup.items():
        keys = list(m)
        if not keys:
            out[pc] = {}
            continue
        rs = snap([k for k, _ in keys], rows)
        d = {}
        for (k, s), r in zip(keys, rs):
            d.setdefault((float(r), s), []).append(m[(k, s)])
        out[pc] = d
    return out


def butterfly(cpiv, ppiv, meta, und, color, fmt="{:,.0f}", title="KC", show_zero=False, footer=True,
              ric_lookup=None, rows=None, kpi=None):
    """Calls on the left (series mirrored), signed strike in the centre, puts on the right.
    `meta` = the series_meta rows to show. `rows` = strike ladder (default: strikes with a value).
    `kpi` = {"calls": (label, value, sub), "mid": (label, value), "puts": (label, value, sub)} drawn as
    a last row, so the call figures sit under the call columns and the put figures under the puts."""
    series = list(meta["series"])
    if not series:
        return "<div class='empty'>No series to display.</div>"
    if rows is None:
        idx = set()
        for p in (cpiv, ppiv):
            if p is None or p.empty:
                continue
            sub = p.reindex(columns=[s for s in series if s in p.columns]).astype(float)
            has = sub.notna() if show_zero else (sub.notna() & (sub != 0))
            idx |= set(sub.index[has.any(axis=1)])
        rows = sorted(float(k) for k in idx)
    rows = [float(k) for k in rows]
    if not rows:
        return "<div class='empty'>Nothing to show for these dates and filters.</div>"
    M = meta.set_index("series")
    ccols, pcols = list(reversed(series)), series

    def col(piv, s):
        if piv is None or piv.empty or s not in piv.columns:
            return np.full(len(rows), np.nan)
        return piv[s].reindex(rows).to_numpy(dtype=float)

    cm = {s: col(cpiv, s) for s in ccols}
    pm = {s: col(ppiv, s) for s in pcols}
    allv = np.concatenate([np.ravel(list(cm.values())), np.ravel(list(pm.values()))])
    allv = allv[~np.isnan(allv)]
    lo, hi = (float(allv.min()), float(allv.max())) if allv.size else (0.0, 1.0)
    r_arr = np.asarray(rows, dtype=float)
    atm_i = {s: (int(np.abs(r_arr - und[M.loc[s, "spread"]]).argmin()) if M.loc[s, "spread"] in und else None)
             for s in series}

    rmap = {pc: (ric_lookup.get(pc, {}) if ric_lookup else {}) for pc in ("C", "P")}

    def td(v, atm=False, ric=None):
        cls = " class='atmc'" if atm else ""
        if isinstance(ric, (list, tuple)):
            ric = ", ".join(ric) if ric else None
        attr = f" data-ric='{escape(ric)}' title='{escape(ric)} (click to copy)'" if ric else ""
        if v is None or np.isnan(v) or (v == 0 and not show_zero):
            return f"<td{cls}{attr}></td>"
        return f"<td{cls}{attr} style='{color(v, lo, hi)}'>{fmt.format(v)}</td>"

    def uf(s):
        sp = M.loc[s, "spread"]
        return f"{und[sp]:.2f}" if sp in und else "&ndash;"

    nc, npt = len(ccols), len(pcols)
    h = [f"<thead><tr class='b1'><th colspan='{nc}' class='cb'>Calls</th>"
         f"<th class='kh' rowspan='4'>{escape(title)}<br><span>Strike</span></th>"
         f"<th colspan='{npt}' class='pb'>Puts</th></tr><tr class='b2'>"]
    h += [f"<th class='cb2'>{M.loc[s, 'ccode']} &middot; {M.loc[s, 'spread_months']}M</th>" for s in ccols]
    h += [f"<th class='pb2'>{M.loc[s, 'pcode']} &middot; {M.loc[s, 'spread_months']}M</th>" for s in pcols]
    h += ["</tr><tr class='b3'>"]
    h += [f"<th class='cb3'>{M.loc[s, 'spread']}</th>" for s in ccols]
    h += [f"<th class='pb3'>{M.loc[s, 'spread']}</th>" for s in pcols]
    h += ["</tr><tr class='b4'>"]
    h += [f"<th title='Futures spread settle'>{uf(s)}</th>" for s in ccols]
    h += [f"<th title='Futures spread settle'>{uf(s)}</th>" for s in pcols]
    h += ["</tr></thead>"]

    body = ["<tbody>"]
    for i, k in enumerate(rows):
        cells = [td(cm[s][i], atm_i[s] == i, rmap["C"].get((k, s))) for s in ccols]
        cells.append(f"<td class='k{' z' if k == 0 else ''}'>{fmt_strike(k)}</td>")
        cells += [td(pm[s][i], atm_i[s] == i, rmap["P"].get((k, s))) for s in pcols]
        body.append(f"<tr>{''.join(cells)}</tr>")
    body.append("</tbody>")

    foot = ""
    if footer:
        def ftd(arr):
            if np.all(np.isnan(arr)):
                return "<td></td>"
            v = float(np.nansum(arr))
            cls = "pos" if v > 0 else "neg" if v < 0 else ""
            return f"<td><span class='{cls}'>{fmt.format(v)}</span></td>"
        foot = ("<tr>" + "".join(ftd(cm[s]) for s in ccols) + "<td class='k'>Total</td>"
                + "".join(ftd(pm[s]) for s in pcols) + "</tr>")
    if kpi:
        def side(x):
            lbl, val, sub = (list(x) + [""])[:3]
            return (f"<div class='kl'>{escape(lbl)}</div><div class='kv'>{val}</div>"
                    + (f"<div class='ks'>{sub}</div>" if sub else ""))
        ml, mv, ms = (list(kpi.get("mid", ("", ""))) + [""])[:3]
        foot += (f"<tr class='kr'><td colspan='{nc}'>{side(kpi['calls'])}</td>"
                 f"<td class='k km'><div class='kl'>{escape(ml)}</div>" + (f"<div class='kv'>{mv}</div>" if mv else "")
                 + (f"<div class='kp'>{ms}</div>" if ms else "") + "</td>"
                 f"<td colspan='{npt}'>{side(kpi['puts'])}</td></tr>")
    foot = f"<tfoot>{foot}</tfoot>" if foot else ""
    n = nc + npt
    size = "fl" if n <= 12 else "fm" if n <= 16 else "fs"
    return f"<div class='bwrap'><table class='bft {size}'>{''.join(h)}{''.join(body)}{foot}</table></div>"


# ==== shared with LSEG/Options (heat colours, KPI strip, tables, chart layout, theme) ===========
def _a(x, lo=0.07, hi=0.62):
    return round(lo + max(0.0, min(1.0, x)) * (hi - lo), 3)


def heat_signed(v, lo, hi):
    """Green up / red down, intensity by |v| against the largest |value| on the table."""
    m = max(abs(lo), abs(hi), 1e-12)
    rgb = "31,157,111" if v > 0 else "201,74,74"
    return f"background:rgba({rgb},{_a(abs(v) / m)})"


def heat_volume(v, lo, hi):
    return f"background:rgba(10,36,99,{_a(v / max(hi, 1e-12), 0.05, 0.42)})"


def heat_level(v, lo, hi):
    """Sequential (amber) between the table's own min and max: IV levels sit in a narrow band,
    so scaling from zero would paint every cell the same."""
    x = (v - lo) / (hi - lo) if hi > lo else 0.5
    return f"background:rgba(201,138,31,{_a(x, 0.06, 0.6)})"


def kpi_row(items) -> str:
    """items: [(label, value_html, sub_html or '')]"""
    cards = "".join(
        f"<div class='kpi'><div class='kl'>{escape(lbl)}</div><div class='kv'>{val}</div>"
        + (f"<div class='ks'>{sub}</div>" if sub else "") + "</div>"
        for lbl, val, sub in items)
    return f"<div class='kpis'>{cards}</div>"


def signed(v, f="{:+,.0f}") -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "&mdash;"
    if np.isnan(v):
        return "&mdash;"
    cls = "pos" if v > 0 else "neg" if v < 0 else ""
    return f"<span class='{cls}'>{f.format(v)}</span>"


def compact_table(head: list, rows: list, cls="rpt") -> str:
    """rows: list of lists of (html, td_class)."""
    th = "".join(f"<th class='{'l' if i == 0 else ''}'>{h}</th>" for i, h in enumerate(head))
    tr = "".join("<tr>" + "".join(f"<td class='{c}'>{v}</td>" for v, c in r) + "</tr>" for r in rows)
    return f"<div class='rwrap'><table class='{cls}'><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>"


def chart_layout(fig, title="", height=360, legend_y=-0.16):
    fig.update_layout(
        template="plotly_white", height=height, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#1a1a2e", size=12, family="Plus Jakarta Sans, sans-serif"),
        title=dict(text=title.upper(), font=dict(size=12, color="#1e3a6e",
                   family="Plus Jakarta Sans, sans-serif"), x=0.5, xanchor="center"),
        margin=dict(t=40 if title else 12, b=8, l=8, r=8), hovermode="x unified",
        legend=dict(orientation="h", y=legend_y, x=0, xanchor="left", yanchor="top",
                    bgcolor="rgba(0,0,0,0)", font=dict(size=11)),
        xaxis=dict(gridcolor="rgba(10,36,99,0.06)", color="#4a5578", showline=True, linecolor="#dfe3ee"),
        yaxis=dict(gridcolor="rgba(10,36,99,0.08)", color="#4a5578", zeroline=False),
        hoverlabel=dict(bgcolor="#ffffff", bordercolor="#dfe3ee", font=dict(color="#1a1a2e", size=12)),
    )
    return fig



CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');
[data-testid="stAppViewContainer"], [data-testid="stMain"], .main { background: #fafafa !important; }
[data-testid="stHeader"] { background: #fafafa !important; }
.block-container { padding: 2.6rem 1.6rem 1.2rem !important; max-width: 100% !important; }
[data-testid="stSidebarUserContent"] { padding-top: 1rem !important; }
[data-testid="stSidebar"] { background: #f0f2f8 !important; border-right: 1px solid #dfe3ee; }
h1, h2, h3, h4, h5, h6 { color: #0a2463 !important; }
body, .main, [data-testid="stSidebar"] { color: #1a1a2e; }
[data-testid="stCaptionContainer"] { color: #7a86a8 !important; }

/* Pill radios (segmented controls). Selected = muted grey for filters / third-level views. */
div[role="radiogroup"] { background: #eef0f6; padding: 3px; border-radius: 999px; gap: 2px; display: inline-flex; flex-wrap: wrap; }
div[role="radiogroup"] label { background: transparent !important; border-radius: 999px !important; padding: 2px 10px !important; margin: 0 !important; }
div[role="radiogroup"] label[data-baseweb="radio"] > div:first-child { display: none; }
div[role="radiogroup"] label div[data-testid="stMarkdownContainer"] p { font-size: 11.5px !important; color: #5a6688; }
div[role="radiogroup"] label:has(input:checked) { background: #6b7690 !important; }
div[role="radiogroup"] label:has(input:checked) div[data-testid="stMarkdownContainer"] p { color: #ffffff !important; font-weight: 600; }

/* Level 1: section switch - navy, larger */
.st-key-sec_box div[role="radiogroup"] label:has(input:checked) { background: #0a2463 !important; }
.st-key-sec_box div[role="radiogroup"] label { padding: 7px 22px !important; }
.st-key-sec_box div[role="radiogroup"] label p { font-size: 14px !important; font-weight: 600; }
/* Level 2: view switch */
.st-key-view_box div[role="radiogroup"] label, .st-key-dd_side_box div[role="radiogroup"] label { padding: 4px 13px !important; }
.st-key-view_box div[role="radiogroup"] label p, .st-key-dd_side_box div[role="radiogroup"] label p { font-size: 13px !important; }

/* Sidebar commodity list: stacked navy pills */
[data-testid="stSidebar"] [data-testid="stRadio"], [data-testid="stSidebar"] [data-testid="stRadio"] > div { width: 100%; }
[data-testid="stSidebar"] div[role="radiogroup"] { flex-direction: column; align-items: stretch; width: 100%; border-radius: 22px; padding: 5px; gap: 3px; }
[data-testid="stSidebar"] div[role="radiogroup"] label { padding: 7px 18px !important; }
[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) { background: #0a2463 !important; }
[data-testid="stSidebar"] div[role="radiogroup"] label div[data-testid="stMarkdownContainer"] p { font-size: 14.5px !important; }
.sb-title { font-family: Plus Jakarta Sans, sans-serif; font-size: 1.5rem; font-weight: 600; color: #0a2463; margin-bottom: 2px; }
.sb-sub { font-size: 11.5px; color: #7a86a8; margin-bottom: 10px; }
.sb-label { font-size: 11px; color: #7a86a8; text-transform: uppercase; letter-spacing: .06em; margin: 12px 0 4px; }
[data-testid="stSidebar"] [data-baseweb="select"] > div { min-height: 34px; }

/* Inputs */
span[data-baseweb="tag"], span[data-baseweb="tag"] div, span[data-baseweb="tag"] span { background-color: #0a2463 !important; color: #ffffff !important; }
span[data-baseweb="tag"] svg { fill: #ffffff !important; }
span[data-baseweb="tag"] { max-width: none !important; } span[data-baseweb="tag"] > span:first-child { max-width: none !important; }
[data-baseweb="popover"] [data-baseweb="menu"] { background: #ffffff !important; }
[data-baseweb="popover"] [data-baseweb="menu"] li, [data-baseweb="popover"] [data-baseweb="menu"] li * { color: #1a1a2e !important; }
[data-baseweb="select"] > div { background: #ffffff !important; color: #1a1a2e !important; }
div[data-testid="stExpander"] { border: 1px solid #dfe3ee !important; border-radius: 12px !important; background: #ffffff; }
div[data-testid="stExpander"] summary p { font-size: 13px; font-weight: 600; color: #0a2463; }
.stButton button { border-radius: 999px; border: 1px solid #dfe3ee; color: #0a2463; font-size: 12.5px; }

/* Page header */
.ph { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin: 0 0 10px; }
.ph .t { font-size: 22px; font-weight: 700; color: #0a2463; letter-spacing: -.01em; }
.ph .m { font-size: 12px; color: #7a86a8; }
.ph .m b { color: #1a1a2e; font-weight: 600; }
.mt { font-size: 14px; font-weight: 600; color: #0a2463; margin: 14px 0 6px; }
.mt span { font-size: 11.5px; font-weight: 400; color: #7a86a8; margin-left: 6px; }
.sec { font-size: 11px; font-weight: 700; color: #7a86a8; text-transform: uppercase; letter-spacing: .09em; margin: 22px 0 6px; padding-bottom: 5px; border-bottom: 1px solid #dfe3ee; }
.note { font-size: 11.5px; color: #7a86a8; margin: 6px 0 2px; line-height: 1.45; }
.note.warn { color: #b4590c; }
.note b { color: #4a5578; }
.empty { padding: 14px; color: #7a86a8; font-size: 12px; border: 1px dashed #dfe3ee; border-radius: 12px; background: #ffffff; }
.pos { color: #1f9d6f; } .neg { color: #c94a4a; }

/* KPI cards */
.kpis { display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0 4px; }
.kpi { background: #ffffff; border: 1px solid #dfe3ee; border-radius: 12px; padding: 9px 14px; min-width: 118px; }
.kpi .kl { font-size: 10px; color: #7a86a8; text-transform: uppercase; letter-spacing: .07em; }
.kpi .kv { font-size: 17px; font-weight: 700; color: #0a2463; margin-top: 2px; font-variant-numeric: tabular-nums; }
.kpi .ks { font-size: 10.5px; color: #7a86a8; margin-top: 1px; }

/* Butterfly tables */
.bwrap { overflow-x: auto; overflow-y: visible; border: 1px solid #dfe3ee; border-radius: 12px; background: #ffffff; width: fit-content; max-width: 100%; }
.bft { border-collapse: separate; border-spacing: 0; font-size: 9px; line-height: 1.15; font-variant-numeric: tabular-nums;
       font-family: Plus Jakarta Sans, sans-serif; }
.bft thead th { z-index: 2; color: #ffffff; font-weight: 600; text-align: center; white-space: nowrap; padding: 0 2px; }
.bft tr.b1 th { height: 2em; font-size: .95em; letter-spacing: .1em; text-transform: uppercase; }
.bft tr.b2 th { height: 1.6em; font-size: .8em; font-weight: 500; }
.bft tr.b3 th { height: 1.7em; font-size: .9em; }
.bft th.cb { background: #0a2463; } .bft th.cb2 { background: #14357f; color: #c9d3ec; } .bft th.cb3 { background: #0f2c70; }
.bft th.pb { background: #0a2463; } .bft th.pb2 { background: #14357f; color: #c9d3ec; } .bft th.pb3 { background: #0f2c70; }
.bft th.kh { z-index: 3; background: #1a1a2e; font-size: 1em; letter-spacing: .04em; min-width: 40px; }
.bft th.kh span { font-size: .8em; font-weight: 400; color: #b8c0d6; letter-spacing: .06em; text-transform: uppercase; }
.bft td { padding: 0 3px; text-align: center; border-bottom: 1px solid #f0f2f8; color: #1a1a2e; white-space: nowrap; }
.bft td.k { text-align: center; font-weight: 700; color: #0a2463; background: #f0f2f8; border-left: 1px solid #dfe3ee; border-right: 1px solid #dfe3ee; }
.bft td.k.atm { background: #c98a1f; color: #ffffff; }
.bft tr.atmr td { border-top: 1.5px solid #c98a1f; border-bottom: 1.5px solid #c98a1f; }
.bft tbody tr:hover td { filter: brightness(.96); }
.bft.fl { font-size: 12px; } .bft.fl td { padding: 1px 7px; } .bft.fl thead th { padding: 0 6px; }
.bft.fm { font-size: 11px; } .bft.fm td { padding: 1px 5px; } .bft.fm thead th { padding: 0 4px; }
.bft.fs { font-size: 10px; } .bft.fs td { padding: 0 3px; } .bft.fs thead th { padding: 0 2px; }
.bft tfoot td { background: #f0f2f8; font-weight: 700; border-top: 1.5px solid #0a2463; z-index: 1; }
.bft tfoot td.k { color: #7a86a8; font-size: 9.5px; text-transform: uppercase; letter-spacing: .06em; }

/* Compact report tables */
.rwrap { overflow: auto; border: 1px solid #dfe3ee; border-radius: 12px; background: #ffffff; width: fit-content; max-width: 100%; }
.rpt { border-collapse: separate; border-spacing: 0; font-size: 11px; line-height: 1.3; font-variant-numeric: tabular-nums; }
.rpt thead th { position: sticky; top: 0; background: #0a2463; color: #ffffff; font-weight: 600; padding: 5px 10px; font-size: 10.5px; text-align: right; white-space: nowrap; }
.rpt td { padding: 3px 10px; text-align: right; border-bottom: 1px solid #eef0f6; color: #1a1a2e; white-space: nowrap; }
.rpt th.l, .rpt td.l { text-align: left; }
.rpt td.d { color: #5a6688; font-weight: 500; }
.rpt td.tot { font-weight: 700; color: #0a2463; background: #f0f2f8; }
.rpt tr:hover td { background: #f3f6ff; }
.rpt.sb { font-size: 10.5px; width: 100%; }
.rpt.sb thead th { padding: 4px 6px; font-size: 9.5px; }
.rpt.sb td { padding: 2px 6px; }
.rwrap.sb { width: 100%; }

/* ── Ice theme (same as Futures OI Progression / COT Comprehensive): overrides everything above ── */
[data-testid="stElementContainer"]:has([data-testid="stMarkdownContainer"] > style:only-child) { display: none !important; }
[data-testid="stHeader"] { background: transparent !important; pointer-events: none; }
[data-testid="stHeader"] button, [data-testid="stHeader"] a, [data-testid="stToolbar"] { pointer-events: auto; }
html, body, [class*="css"], [data-testid="stAppViewContainer"], .stMarkdown, button, input, textarea {
  font-family: Plus Jakarta Sans, sans-serif !important; -webkit-font-smoothing: antialiased; }
div[role="radiogroup"] label p, [data-testid="stWidgetLabel"] p, .ph .t, .mt, .sec, .sb-label, .kpi .kl, .rpt thead th, .bft thead th,
[data-testid="stCaptionContainer"] { font-family: Plus Jakarta Sans, sans-serif !important; }
.bft, .rpt, .kpi .kv { font-family: Plus Jakarta Sans, sans-serif !important; }

/* sidebar: frosted glass, plain title, clean vertical commodity menu, glass status card */
[data-testid="stSidebar"] { background: linear-gradient(160deg, rgba(246,248,253,.92) 0%, rgba(233,238,248,.88) 55%, rgba(240,236,246,.9) 100%) !important;
  -webkit-backdrop-filter: blur(24px) saturate(160%); backdrop-filter: blur(24px) saturate(160%);
  border-right: 1px solid rgba(255,255,255,.75) !important; box-shadow: inset -1px 0 0 rgba(10,36,99,.06), 6px 0 28px rgba(10,36,99,.06); }
[data-testid="stSidebarHeader"] { height: 2.2rem !important; min-height: 0 !important; padding: .5rem .75rem 0 !important; }
[data-testid="stSidebarUserContent"] { padding-top: .25rem !important; }
.sb-title { font-family: Plus Jakarta Sans, sans-serif !important; font-size: 1.6rem !important; color: #0a2463 !important; line-height: 1.15; }
.sb-sub { font-family: Plus Jakarta Sans, sans-serif; font-size: 11.5px; color: #8a94a8; letter-spacing: .04em;
  padding-bottom: 12px; margin-bottom: 12px !important; border-bottom: 1px solid rgba(43,74,138,.12); }
.sb-label { font-size: 10.5px !important; font-weight: 700; letter-spacing: .14em !important; color: #7a86a8 !important; }
.st-key-commodity div[role="radiogroup"] { background: none !important; border: 0 !important; box-shadow: none !important; padding: 0 !important;
  gap: 4px !important; border-radius: 0 !important; }
.st-key-commodity div[role="radiogroup"] label { border-radius: 12px !important; padding: 9px 16px !important; transition: background .15s, transform .15s; }
.st-key-commodity div[role="radiogroup"] label div[data-testid="stMarkdownContainer"] p { font-size: 14px !important; font-weight: 600 !important; color: #4a5578 !important; }
.st-key-commodity div[role="radiogroup"] label:not(:has(input:checked)):hover { background: rgba(43,74,138,.07) !important; transform: translateX(2px); }
.st-key-commodity div[role="radiogroup"] label:has(input:checked) { background: linear-gradient(135deg, #2b4a8a, #1e3a6e) !important;
  box-shadow: 0 6px 16px rgba(30,58,110,.25), inset 0 1px 0 rgba(255,255,255,.18) !important; }
.st-key-commodity div[role="radiogroup"] label:has(input:checked) div[data-testid="stMarkdownContainer"] p { color: #ffffff !important; }
[data-testid="stSidebar"] .rwrap.sb { background: rgba(255,255,255,.55) !important; border: 1px solid rgba(255,255,255,.95) !important; border-radius: 14px !important;
  box-shadow: 0 1px 2px rgba(10,36,99,.05), 0 8px 24px rgba(10,36,99,.07), inset 0 1px 0 #ffffff; }
[data-testid="stSidebar"] .rpt.sb thead th { background: transparent !important; color: #7a86a8 !important; letter-spacing: .1em; text-transform: uppercase;
  border-bottom: 1px solid rgba(43,74,138,.10); }
[data-testid="stSidebar"] .rpt.sb td { background: transparent; border-bottom-color: rgba(43,74,138,.06); }

/* pill tracks: frosted glass; section/view pick = navy gradient, third-level filters = soft slate gradient */
div[role="radiogroup"], .stTabs [data-baseweb="tab-list"] {
  background: linear-gradient(160deg, rgba(250,252,255,.97), rgba(236,242,251,.93) 60%, rgba(229,237,249,.9)) !important;
  border: 1px solid rgba(43,74,138,.14) !important; box-shadow: 0 4px 16px rgba(30,58,110,.07), inset 0 1px 0 rgba(255,255,255,.95) !important; }
div[role="radiogroup"] label:has(input:checked) { background: linear-gradient(180deg, #8a94ad, #6b7690) !important;
  box-shadow: 0 2px 6px rgba(74,85,120,.22), inset 0 1px 0 rgba(255,255,255,.2) !important; }
.st-key-sec_box div[role="radiogroup"] label:has(input:checked), .st-key-view_box div[role="radiogroup"] label:has(input:checked) {
  background: linear-gradient(180deg, #2b4a8a, #1e3a6e) !important; box-shadow: 0 2px 8px rgba(30,58,110,.28), inset 0 1px 0 rgba(255,255,255,.18) !important; }
div[role="radiogroup"] label { transition: background .15s, transform .15s; }
div[role="radiogroup"] label:not(:has(input:checked)):hover { background: rgba(43,74,138,.07) !important; transform: translateY(-1px); }
/* vertical option lists inside popovers: a plain menu, not a pill track */
[data-testid="stPopoverBody"] div[role="radiogroup"] { background: none !important; border: none !important; box-shadow: none !important;
  border-radius: 0 !important; padding: 0 !important; display: flex !important; flex-direction: column; gap: 2px; width: 100%; }

/* dropdowns: glass pill, lift on hover, wine focus ring + caret; frosted menus; navy tags */
[data-baseweb="select"] { background: transparent !important; box-shadow: none !important; }
[data-baseweb="select"] > div { background: linear-gradient(180deg, rgba(255,255,255,.78), rgba(238,242,250,.62)) !important;
  -webkit-backdrop-filter: blur(14px) saturate(170%); backdrop-filter: blur(14px) saturate(170%);
  border: 1px solid rgba(255,255,255,.9) !important; border-radius: 999px !important; min-height: 34px;
  box-shadow: 0 1px 2px rgba(10,36,99,.06), 0 6px 18px rgba(10,36,99,.08), inset 0 1px 0 rgba(255,255,255,.95); transition: box-shadow .18s, transform .18s; }
[data-baseweb="select"] > div:hover { box-shadow: 0 0 0 3px rgba(10,36,99,.07), 0 8px 22px rgba(10,36,99,.14), inset 0 1px 0 rgba(255,255,255,.95); transform: translateY(-1px); }
[data-baseweb="select"] > div:focus-within { box-shadow: 0 0 0 3px rgba(123,30,58,.16), 0 8px 22px rgba(10,36,99,.14) !important; }
[data-baseweb="select"] svg { fill: #7b1e3a !important; }
[data-baseweb="select"] > div:has([data-baseweb="tag"]) { border-radius: 16px !important; padding: 4px 6px !important; }
span[data-baseweb="tag"] { border-radius: 8px !important; }
span[data-baseweb="tag"] svg { fill: #ffffff !important; }
[data-baseweb="popover"] [data-baseweb="menu"], [data-baseweb="popover"] ul {
  background: rgba(255,255,255,.82) !important; -webkit-backdrop-filter: blur(18px) saturate(170%); backdrop-filter: blur(18px) saturate(170%);
  border: 1px solid rgba(255,255,255,.9) !important; border-radius: 14px !important; box-shadow: 0 14px 40px rgba(10,36,99,.18) !important; }
[data-baseweb="popover"] li:hover { background: rgba(43,74,138,.07) !important; }

/* cards: charts, KPIs, expanders, buttons */
[data-testid="stMain"] [data-testid="stPlotlyChart"] { background: #ffffff; border: 1px solid rgba(43,74,138,.10); border-radius: 18px; padding: 6px 8px;
  overflow: hidden; box-shadow: 0 1px 2px rgba(10,36,99,.04), 0 4px 14px rgba(30,58,110,.06), inset 0 1px 0 #ffffff; transition: box-shadow .2s; }
[data-testid="stMain"] [data-testid="stPlotlyChart"] .main-svg { border-radius: 16px; }
[data-testid="stMain"] [data-testid="stPlotlyChart"]:hover { box-shadow: 0 1px 2px rgba(10,36,99,.05), 0 8px 20px rgba(30,58,110,.09), inset 0 1px 0 #ffffff; }
.kpi { border: 1px solid rgba(43,74,138,.10) !important; border-radius: 14px !important;
  background: linear-gradient(160deg, rgba(255,255,255,.98), rgba(244,247,252,.95)) !important;
  box-shadow: 0 1px 2px rgba(10,36,99,.04), 0 6px 18px rgba(30,58,110,.07), inset 0 1px 0 #ffffff; }
.kpi .kl { letter-spacing: .12em !important; font-weight: 700; }
div[data-testid="stExpander"] { border: 1px solid rgba(43,74,138,.10) !important; border-radius: 14px !important; box-shadow: 0 1px 3px rgba(10,36,99,.06); }
.stButton > button, .stDownloadButton > button { border-radius: 999px !important; border: 1px solid rgba(43,74,138,.18) !important;
  background: linear-gradient(180deg, #ffffff, #eef2fa) !important; color: #0a2463 !important; font-weight: 600;
  box-shadow: 0 1px 2px rgba(10,36,99,.06); transition: box-shadow .18s, transform .18s; }
.stButton > button:hover, .stDownloadButton > button:hover { transform: translateY(-1px); box-shadow: 0 8px 20px rgba(10,36,99,.14) !important; }
.empty { border-radius: 14px !important; border-color: rgba(43,74,138,.18) !important; text-align: center; }

/* headings: page title tighter; table/chart titles (.mt) and section labels (.sec) centred caps on a royal-grey wash bar */
.ph .t { font-size: 20px !important; }
.mt, .sec { display: block; text-align: center; text-transform: uppercase; font-size: 12px !important; font-weight: 700 !important;
  letter-spacing: .14em !important; color: #1e3a6e !important; margin: 14px 0 8px !important; padding: 5px 12px !important;
  border: none !important; border-radius: 8px;
  background: linear-gradient(90deg, rgba(92,104,136,0) 0%, rgba(92,104,136,.11) 20%, rgba(92,104,136,.11) 80%, rgba(92,104,136,0) 100%); }
.mt span { text-transform: none; letter-spacing: .02em; font-weight: 500 !important; color: #7a86a8 !important; margin-left: 8px !important; }
.note { text-align: left; }

/* icy tables: pale glass headers instead of solid navy, hairline rows, soft hover; ATM stays amber */
.rwrap, .bwrap { border: 1px solid rgba(43,74,138,.12) !important; border-radius: 14px !important;
  box-shadow: 0 1px 2px rgba(10,36,99,.04), 0 8px 24px rgba(10,36,99,.06); }
.rpt thead th { background: linear-gradient(180deg, #f7f9fd, #e8eef9) !important; color: #1e3a6e !important; font-weight: 700 !important;
  letter-spacing: .06em; text-transform: uppercase; border-bottom: 1px solid rgba(43,74,138,.18); }
.rpt td { border-bottom-color: rgba(43,74,138,.06) !important; }
.rpt td.tot { background: #edf1f9 !important; }
.rpt tr:hover td { background: rgba(43,74,138,.05) !important; }
.bft th.cb, .bft th.pb { background: linear-gradient(180deg, #e9eff9, #dfe7f5) !important; color: #0a2463 !important; }
.bft th.cb2, .bft th.pb2 { background: #f0f4fb !important; color: #4a5a85 !important; }
.bft th.cb3, .bft th.pb3 { background: linear-gradient(180deg, #f7f9fd, #eef2fa) !important; color: #1e3a6e !important;
  border-bottom: 1px solid rgba(43,74,138,.18); }
.bft th.kh { background: linear-gradient(180deg, #2b4a8a, #1e3a6e) !important; color: #ffffff !important; }
.bft th.kh span { color: #c9d3ec !important; }
.bft td { border-bottom-color: rgba(43,74,138,.06) !important; }
.bft td.k { background: #f1f5fb !important; border-left-color: rgba(43,74,138,.12) !important; border-right-color: rgba(43,74,138,.12) !important; }
.bft td.k.atm { background: #c98a1f !important; color: #ffffff !important; }
.bft tfoot td { background: #f1f5fb !important; border-top: 1px solid rgba(43,74,138,.25) !important; }
/* KPI strip: one slim glass bar, cells split by hairlines, small caps label, value, faint detail */
.kpis { display: inline-flex !important; flex-wrap: wrap; gap: 0 !important; margin: 12px 0 6px !important; padding: 2px 0;
  background: linear-gradient(160deg, rgba(255,255,255,.98), rgba(244,247,252,.95)); border: 1px solid rgba(43,74,138,.10); border-radius: 14px;
  box-shadow: 0 1px 2px rgba(10,36,99,.04), 0 6px 18px rgba(30,58,110,.06), inset 0 1px 0 #ffffff; }
.kpi { background: none !important; border: none !important; border-left: 1px solid rgba(43,74,138,.10) !important; border-radius: 0 !important;
  box-shadow: none !important; padding: 6px 18px !important; min-width: 0 !important; text-align: center; }
.kpi:first-child { border-left: none !important; }
.kpi .kl { font-size: 9.5px !important; letter-spacing: .14em !important; }
.kpi .kv { font-size: 15.5px !important; margin-top: 1px !important; }
.kpi .ks { font-size: 10px !important; color: #9aa3b8 !important; }
/* tables are fit-content: centre them under their centred header */
[data-testid="stMain"] .bwrap, [data-testid="stMain"] .rwrap { margin-left: auto !important; margin-right: auto !important; }
/* CSO: each series' at-the-money cell = strike nearest that futures spread's settle */
.bft td.atmc { box-shadow: inset 0 0 0 2px #c98a1f; font-weight: 700; }
.bft tr.b4 th { height: 1.6em; font-size: .85em; font-weight: 600; background: #fbf4e6 !important; color: #8a5a0c !important; }
.bft td.k.z { background: #e4ebf7 !important; }
.bft tr.kr td { background: linear-gradient(180deg, #fbfcfe, #f1f5fb) !important; border-top: 1px solid rgba(43,74,138,.16) !important;
  padding: 7px 4px 8px !important; text-align: center; vertical-align: middle; white-space: normal !important; }
.bft tr.kr .kl { font-size: 8.5px; color: #8a94ad; text-transform: uppercase; letter-spacing: .1em; font-weight: 700; }
.bft tr.kr .kv { font-size: 17px; font-weight: 700; color: #0a2463; margin-top: 3px; line-height: 1.1; font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
.bft tr.kr .ks { margin-top: 4px; font-size: 10px; color: #4a5578; font-weight: 600; font-variant-numeric: tabular-nums; }
.bft tr.kr .ks .kt { font-size: 8px; color: #9aa3b8; font-weight: 700; text-transform: uppercase; letter-spacing: .14em; margin-right: 4px; }
.bft tr.kr td.km { background: linear-gradient(180deg, #eef3fb, #e3eaf6) !important; padding: 7px 2px 8px !important; }
.bft tr.kr td.km .kv { font-size: 13px; }
.bft tr.kr td.km .kp { display: inline-block; margin-top: 4px; padding: 1px 4px; border-radius: 999px; background: #ffffff;
  border: 1px solid rgba(43,74,138,.18); box-shadow: 0 1px 2px rgba(10,36,99,.06); font-size: 9px; font-weight: 700; color: #1e3a6e;
  white-space: nowrap; font-variant-numeric: tabular-nums; }
.bft tr.kr td.km .kp .kt { font-size: 7px; color: #8a94ad; letter-spacing: .06em; margin-right: 2px; font-weight: 700; }
.bft td[data-ric] { cursor: copy; }
.bft td[data-ric]:hover { outline: 1px solid rgba(43,74,138,.45); outline-offset: -1px; }
.bft td.copied { outline: 2px solid #1f9d6f !important; outline-offset: -2px; }
.bft td.copied::after { content: " copied"; font-size: .8em; color: #1f9d6f; font-weight: 700; }
</style>
"""
