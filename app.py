"""
Real Exchange Rate Explorer (IMF data)

S(t)   = units of Country 2 currency per 1 unit of Country 1 currency
RER(t) = S(t) * P1(t) / P2(t)
RER*   = mean of RER(t) over valid months in the selected sample
Real Index(t) = 100 * RER(t) / RER*

Data: IMF SDMX 3.0 API (https://api.imf.org/external/sdmx/3.0)
  CPI:  dataflow IMF.STA/CPI, key {ISO3}.CPI._T.IX.M
  FX:   dataflow IMF.STA/ER,  key {ISO3}.XDC_USD.PA_RT.M  (local currency per USD)
"""

from __future__ import annotations

import io
import re
import time
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
IMF_BASE = "https://api.imf.org/external/sdmx/3.0"
AGENCY = "IMF.STA"
CPI_FLOW = "CPI"
FX_FLOW = "ER"
CPI_KEY = "{iso}.CPI._T.IX.M"
FX_KEY = "{iso}.XDC_USD.PA_RT.M"
START_MONTH = pd.Timestamp("1976-01-01")
EURO_AREA = "G163"
TIMEOUT = 60
CACHE_TTL = 60 * 60 * 12  # 12 hours

HEADERS_JSON = {
    "Accept": "application/vnd.sdmx.data+json;version=2.0.0, application/json;q=0.9",
    "User-Agent": "rer-explorer/1.0 (python-requests)",
}
HEADERS_CSV = {
    "Accept": "text/csv",
    "User-Agent": "rer-explorer/1.0 (python-requests)",
}
HEADERS_STRUCT = {
    "Accept": "application/vnd.sdmx.structure+json;version=2.0.0, application/json;q=0.9",
    "User-Agent": "rer-explorer/1.0 (python-requests)",
}


class IMFError(Exception):
    """Raised when the IMF API cannot be reached or returns an error."""


def _empty() -> pd.Series:
    """Empty monthly series with a DatetimeIndex (safe to compare with Timestamps)."""
    return pd.Series(dtype=float, index=pd.DatetimeIndex([], name="month"))


def _series(d: dict) -> pd.Series:
    if not d:
        return _empty()
    out = pd.Series(d, dtype=float)
    out.index = pd.DatetimeIndex(out.index)
    return out.sort_index()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _get(url: str, headers: dict, params: dict | None = None, retries: int = 3) -> requests.Response | None:
    """GET with retries. Returns None for 404 (no data); raises IMFError otherwise."""
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
            if r.status_code == 404 or (400 <= r.status_code < 500 and r.status_code != 429):
                return None  # no data / invalid key for this country
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code}"
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last_err = str(exc)
            time.sleep(1.5 * (attempt + 1))
    raise IMFError(f"IMF API request failed ({last_err}): {url}")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
_PERIOD_PATTERNS = [
    re.compile(r"^(\d{4})-?M(\d{1,2})$", re.I),  # 1976-M01, 1976M01
    re.compile(r"^(\d{4})-(\d{1,2})(?:-\d{1,2})?$"),  # 1976-01, 1976-01-01
]


def parse_period(p) -> pd.Timestamp | None:
    """Convert IMF time period strings to a month-start Timestamp."""
    if p is None:
        return None
    s = str(p).strip()
    for pat in _PERIOD_PATTERNS:
        m = pat.match(s)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12:
                return pd.Timestamp(year=y, month=mo, day=1)
    return None


def _to_float(v) -> float | None:
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("value")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if pd.notna(f) else None


def _localized(name) -> str:
    if isinstance(name, dict):
        return name.get("en") or next(iter(name.values()), "")
    return str(name or "")


def parse_sdmx_json(js: dict) -> pd.Series:
    """Parse an SDMX-JSON (1.0 or 2.0) data message into a monthly Series."""
    data = js.get("data", js)
    datasets = data.get("dataSets") or []
    if not datasets:
        return _empty()

    structs = data.get("structures")
    if structs is None and "structure" in data:
        structs = [data["structure"]]
    if structs is None and "structure" in js:
        structs = [js["structure"]]
    structs = structs or [{}]

    best: dict[pd.Timestamp, float] = {}
    for ds in datasets:
        sidx = ds.get("structure", 0) if isinstance(ds.get("structure", 0), int) else 0
        struct = structs[sidx] if sidx < len(structs) else structs[0]
        dims = struct.get("dimensions", {})
        series_dims = dims.get("series", []) or []
        obs_dims = dims.get("observation", []) or []

        def time_values(dim_list):
            for i, d in enumerate(dim_list):
                if d.get("id") in ("TIME_PERIOD", "TIME") or d.get("role") == "time" or "time" in (d.get("roles") or []):
                    return i, d.get("values", [])
            return None, []

        # Case 1: series-keyed observations
        if ds.get("series"):
            t_pos, t_vals = time_values(obs_dims)
            if t_pos is None:
                t_pos, t_vals = 0, (obs_dims[0].get("values", []) if obs_dims else [])
            for s in ds["series"].values():
                out = {}
                for okey, val in (s.get("observations") or {}).items():
                    parts = str(okey).split(":")
                    idx = int(parts[t_pos]) if t_pos < len(parts) else int(parts[0])
                    if idx >= len(t_vals):
                        continue
                    tv = t_vals[idx]
                    ts = parse_period(tv.get("id") or tv.get("value") or tv.get("start"))
                    f = _to_float(val)
                    if ts is not None and f is not None:
                        out[ts] = f
                if len(out) > len(best):
                    best = out
        # Case 2: flat observations (dimensionAtObservation=AllDimensions)
        elif ds.get("observations"):
            all_dims = series_dims + obs_dims
            t_pos, t_vals = time_values(all_dims)
            if t_pos is None:
                continue
            out = {}
            for okey, val in ds["observations"].items():
                parts = str(okey).split(":")
                idx = int(parts[t_pos])
                if idx >= len(t_vals):
                    continue
                tv = t_vals[idx]
                ts = parse_period(tv.get("id") or tv.get("value") or tv.get("start"))
                f = _to_float(val)
                if ts is not None and f is not None:
                    out[ts] = f
            if len(out) > len(best):
                best = out

    return _series(best)


def parse_sdmx_csv(text: str) -> pd.Series:
    """Parse an SDMX-CSV data message into a monthly Series."""
    try:
        df = pd.read_csv(io.StringIO(text), dtype=str)
    except Exception:
        return _empty()
    cols = {c.upper(): c for c in df.columns}
    tcol, vcol = cols.get("TIME_PERIOD"), cols.get("OBS_VALUE")
    if tcol is None or vcol is None:
        return _empty()
    out = {}
    for t, v in zip(df[tcol], df[vcol]):
        ts, f = parse_period(t), _to_float(v)
        if ts is not None and f is not None:
            out[ts] = f
    return _series(out)


# ---------------------------------------------------------------------------
# Data retrieval (cached)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_series(flow: str, key: str) -> pd.Series:
    """Fetch one monthly IMF series. Returns an empty Series if no data exists."""
    url = f"{IMF_BASE}/data/dataflow/{AGENCY}/{flow}/~/{key}"

    # 1) SDMX-CSV (the format the IMF documents in its examples)
    s = _empty()
    r = _get(url, HEADERS_CSV, {"c[TIME_PERIOD]": "ge:1976-M01"})
    if r is not None:
        s = parse_sdmx_csv(r.text)
    # 2) Same request without the time filter, in case the filter is rejected
    if s.empty:
        r = _get(url, HEADERS_CSV)
        if r is not None:
            s = parse_sdmx_csv(r.text)
    # 3) SDMX-JSON fallback
    if s.empty:
        r = _get(url, HEADERS_JSON)
        if r is not None:
            try:
                s = parse_sdmx_json(r.json())
            except ValueError:
                s = parse_sdmx_csv(r.text)

    if s.empty:
        return _empty()
    s = s[s.index >= START_MONTH]
    s = s[~s.index.duplicated(keep="last")]
    return s.sort_index()


def _codes_from_structure(js: dict) -> dict[str, str]:
    data = js.get("data", js)
    for cl in data.get("codelists", []) or []:
        cid = str(cl.get("id", "")).upper()
        if "COUNTRY" in cid or "AREA" in cid:
            return {c["id"]: _localized(c.get("name") or c.get("names")) for c in cl.get("codes", []) if c.get("id")}
    return {}


def _available_countries(flow: str, key_tail: str) -> set[str]:
    """Ask the availability endpoint which countries have data for a key pattern."""
    url = f"{IMF_BASE}/availability/dataflow/{AGENCY}/{flow}/~/*.{key_tail}"
    r = _get(url, HEADERS_STRUCT, {"mode": "exact"}, retries=2)
    if r is None:
        return set()
    data = r.json().get("data", {})
    out: set[str] = set()
    for cc in data.get("dataConstraints", []) or data.get("contentConstraints", []) or []:
        for region in cc.get("cubeRegions", []) or []:
            for kv in region.get("keyValues", []) or region.get("components", []) or []:
                if str(kv.get("id", "")).upper() in ("COUNTRY", "REF_AREA"):
                    for v in kv.get("values", []):
                        out.add(v if isinstance(v, str) else v.get("value") or v.get("id"))
    return {c for c in out if c}


def _fallback_countries() -> dict[str, str]:
    try:
        import pycountry

        return {c.alpha_3: c.name for c in pycountry.countries}
    except Exception:
        return {"USA": "United States", "GBR": "United Kingdom", "JPN": "Japan", "CAN": "Canada",
                "CHE": "Switzerland", "DEU": "Germany", "FRA": "France", "ITA": "Italy",
                "CHN": "China", "IND": "India", "BRA": "Brazil", "MEX": "Mexico",
                "AUS": "Australia", "KOR": "Korea, Republic of", "ZAF": "South Africa"}


@st.cache_data(ttl=CACHE_TTL * 2, show_spinner=False)
def fetch_country_list() -> tuple[dict[str, str], str]:
    """Return ({code: name}, source_note). Countries with CPI data are preferred."""
    names: dict[str, str] = {}
    note = "IMF CPI codelist"
    try:
        url = f"{IMF_BASE}/structure/dataflow/{AGENCY}/{CPI_FLOW}/~"
        r = _get(url, HEADERS_STRUCT, {"references": "descendants", "detail": "full"}, retries=2)
        if r is not None:
            names = _codes_from_structure(r.json())
    except Exception:
        names = {}
    if not names:
        names, note = _fallback_countries(), "built-in ISO country list (IMF codelist unavailable)"

    try:
        avail = _available_countries(CPI_FLOW, CPI_KEY.split(".", 1)[1])
        if avail:
            filtered = {k: v for k, v in names.items() if k in avail}
            for k in avail - filtered.keys():
                filtered[k] = k
            names, note = filtered, note + ", filtered to countries with monthly CPI"
    except Exception:
        pass

    # Keep 3-letter country codes plus the Euro area aggregate.
    names = {k: v for k, v in names.items() if re.fullmatch(r"[A-Z]{3}", k) or k == EURO_AREA}
    names[EURO_AREA] = "Euro area"
    return dict(sorted(names.items(), key=lambda kv: kv[1])), note


def fetch_fx_per_usd(iso: str) -> tuple[pd.Series, str]:
    """Local currency units per USD, plus a note on the source. The US is 1 by definition."""
    if iso == "USA":
        idx = pd.date_range(START_MONTH, pd.Timestamp(date.today()).to_period("M").to_timestamp(), freq="MS")
        return pd.Series(1.0, index=idx), "USD (fixed at 1)"
    s = fetch_series(FX_FLOW, FX_KEY.format(iso=iso))
    if s.empty and iso == EURO_AREA:
        # The aggregate may have no ER series. Germany's rate is in euros per USD from 1999 onward.
        de = fetch_series(FX_FLOW, FX_KEY.format(iso="DEU"))
        de = de[de.index >= pd.Timestamp("1999-01-01")]
        if not de.empty:
            return de, f"EUR per USD from Germany's IMF series (Euro area series unavailable): {coverage(de)}"
    return s, coverage(s)


def fetch_cpi(iso: str) -> pd.Series:
    return fetch_series(CPI_FLOW, CPI_KEY.format(iso=iso))


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------
def build_panel(e1: pd.Series, e2: pd.Series, p1: pd.Series, p2: pd.Series) -> pd.DataFrame:
    """Align inputs monthly and compute S(t) and RER(t)."""
    parts = [s for s in (e1, e2, p1, p2) if not s.empty]
    if not parts:
        return pd.DataFrame()
    start = max(START_MONTH, min(s.index.min() for s in parts))
    end = max(s.index.max() for s in parts)
    idx = pd.date_range(start, end, freq="MS")

    df = pd.DataFrame(index=idx)
    df.index.name = "month"
    df["E1_lcu_per_usd"] = e1.reindex(idx)
    df["E2_lcu_per_usd"] = e2.reindex(idx)
    df["P1_cpi"] = p1.reindex(idx)
    df["P2_cpi"] = p2.reindex(idx)

    # Non-positive values are invalid for rates and price indexes.
    for c in df.columns:
        df.loc[df[c] <= 0, c] = pd.NA
    df = df.astype(float)

    # S = (Country 2 per USD) / (Country 1 per USD) = Country 2 per Country 1
    df["S_c2_per_c1"] = df["E2_lcu_per_usd"] / df["E1_lcu_per_usd"]
    df["RER"] = df["S_c2_per_c1"] * df["P1_cpi"] / df["P2_cpi"]
    df["valid"] = df[["S_c2_per_c1", "P1_cpi", "P2_cpi", "RER"]].notna().all(axis=1)
    return df


def apply_sample(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, float]:
    """Restrict to the sample, compute RER* from valid months, and the Real Index."""
    w = df.loc[start:end].copy()
    valid = w["valid"]
    rer_star = float(w.loc[valid, "RER"].mean()) if valid.any() else float("nan")
    w["RER_star"] = rer_star
    w["Real_Index"] = 100.0 * w["RER"] / rer_star
    w.loc[~valid, "Real_Index"] = float("nan")
    return w, rer_star


def make_chart(w: pd.DataFrame, mode: str, c1: str, c2: str) -> go.Figure:
    x = w.index
    s_label = f"S(t): {c2} currency per 1 {c1} currency"
    r_label = "Real Index (sample mean = 100)"

    if mode == "Both":
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Scatter(x=x, y=w["Real_Index"], name="Real Index", mode="lines",
                                 connectgaps=False), secondary_y=False)
        fig.add_trace(go.Scatter(x=x, y=w["S_c2_per_c1"], name="Nominal S(t)", mode="lines",
                                 line=dict(dash="dot"), connectgaps=False), secondary_y=True)
        fig.add_hline(y=100, line_dash="dash", line_width=1, opacity=0.5)
        fig.update_yaxes(title_text=r_label, secondary_y=False)
        fig.update_yaxes(title_text=s_label, secondary_y=True, showgrid=False)
    else:
        fig = go.Figure()
        if mode == "Real":
            fig.add_trace(go.Scatter(x=x, y=w["Real_Index"], name="Real Index", mode="lines", connectgaps=False))
            fig.add_hline(y=100, line_dash="dash", line_width=1, opacity=0.5)
            fig.update_yaxes(title_text=r_label)
        else:
            fig.add_trace(go.Scatter(x=x, y=w["S_c2_per_c1"], name="Nominal S(t)", mode="lines", connectgaps=False))
            fig.update_yaxes(title_text=s_label)

    fig.update_layout(
        height=520,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        xaxis=dict(title="Month", rangeslider=dict(visible=False)),
    )
    return fig


def to_csv(w: pd.DataFrame, c1: str, c2: str, iso1: str, iso2: str) -> bytes:
    out = w.reset_index().rename(columns={
        "month": "month",
        "E1_lcu_per_usd": f"E1_{iso1}_per_USD",
        "E2_lcu_per_usd": f"E2_{iso2}_per_USD",
        "P1_cpi": f"P1_CPI_{iso1}",
        "P2_cpi": f"P2_CPI_{iso2}",
        "S_c2_per_c1": f"S_{iso2}_per_{iso1}",
    })
    out["month"] = out["month"].dt.strftime("%Y-%m")
    cols = ["month", f"E1_{iso1}_per_USD", f"E2_{iso2}_per_USD", f"S_{iso2}_per_{iso1}",
            f"P1_CPI_{iso1}", f"P2_CPI_{iso2}", "RER", "valid", "RER_star", "Real_Index"]
    return out[cols].to_csv(index=False, float_format="%.10g").encode("utf-8")


def coverage(s: pd.Series) -> str:
    if s.empty:
        return "no data"
    expected = len(pd.date_range(s.index.min(), s.index.max(), freq="MS"))
    gaps = expected - len(s)
    return f"{s.index.min():%Y-%m} to {s.index.max():%Y-%m} ({len(s)} obs{', ' + str(gaps) + ' missing months' if gaps else ''})"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="Real Exchange Rate Explorer", layout="wide")
    st.title("Real Exchange Rate Explorer")
    st.caption("Monthly IMF CPI and exchange-rate data. "
               "S(t) = Country 2 currency per 1 unit of Country 1 currency; "
               "RER(t) = S(t) × P1(t) / P2(t).")

    with st.spinner("Loading IMF country list..."):
        try:
            countries, list_note = fetch_country_list()
        except Exception as exc:  # should not happen: function has fallbacks
            st.error(f"Could not load the country list: {exc}")
            return

    codes = list(countries.keys())
    label = lambda c: f"{countries.get(c, c)} ({c})"

    with st.sidebar:
        st.header("Countries")
        d1 = codes.index("USA") if "USA" in codes else 0
        d2 = codes.index(EURO_AREA) if EURO_AREA in codes else min(1, len(codes) - 1)
        iso1 = st.selectbox("Country 1 (base currency)", codes, index=d1, format_func=label)
        iso2 = st.selectbox("Country 2 (quote currency)", codes, index=d2, format_func=label)
        st.caption(f"Country list source: {list_note}.")

    if iso1 == iso2:
        st.warning("Choose two different countries.")
        return

    c1, c2 = countries.get(iso1, iso1), countries.get(iso2, iso2)

    with st.spinner(f"Retrieving IMF data for {c1} and {c2}..."):
        try:
            (e1, e1_note), (e2, e2_note) = fetch_fx_per_usd(iso1), fetch_fx_per_usd(iso2)
            p1, p2 = fetch_cpi(iso1), fetch_cpi(iso2)
        except IMFError as exc:
            st.error(f"{exc}\n\nThe IMF API may be temporarily unavailable. Try again shortly.")
            return

    with st.expander("Data coverage", expanded=False):
        st.markdown(
            f"- Exchange rate, {c1} per USD: {e1_note}\n"
            f"- Exchange rate, {c2} per USD: {e2_note}\n"
            f"- CPI, {c1}: {coverage(p1)}\n"
            f"- CPI, {c2}: {coverage(p2)}"
        )

    missing = [n for n, s in [(f"{c1} exchange rate", e1), (f"{c2} exchange rate", e2),
                              (f"{c1} CPI", p1), (f"{c2} CPI", p2)] if s.empty]
    if missing:
        st.error("The IMF returned no monthly data for: " + ", ".join(missing) + ". Choose other countries.")
        return

    df = build_panel(e1, e2, p1, p2)
    valid_months = df.index[df["valid"]]
    if len(valid_months) == 0:
        st.error("The four input series never overlap, so RER(t) cannot be computed for this pair.")
        return

    first_valid, last_valid = valid_months.min(), valid_months.max()
    months = list(pd.date_range(first_valid, last_valid, freq="MS"))

    with st.sidebar:
        st.header("Sample period")
        start, end = st.select_slider(
            "Start and end month",
            options=months,
            value=(months[0], months[-1]),
            format_func=lambda d: d.strftime("%Y-%m"),
            key=f"range_{iso1}_{iso2}",  # reset when the pair changes
        )
        st.header("Chart")
        mode = st.radio("Series", ["Real", "Nominal", "Both"], index=2, horizontal=True)

    if start > end:
        start, end = end, start

    w, rer_star = apply_sample(df, start, end)
    n_valid, n_total = int(w["valid"].sum()), len(w)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("RER* (sample mean of RER)", f"{rer_star:,.6g}" if pd.notna(rer_star) else "n/a")
    m2.metric("Valid months", f"{n_valid} of {n_total}")
    last = w.loc[w["valid"]].iloc[-1] if n_valid else None
    m3.metric("Latest Real Index", f"{last['Real_Index']:.1f}" if last is not None else "n/a",
              help=f"Month: {w.index[w['valid']][-1]:%Y-%m}" if last is not None else None)
    m4.metric(f"Latest S ({iso2} per {iso1})", f"{last['S_c2_per_c1']:,.6g}" if last is not None else "n/a")

    if n_valid < n_total:
        st.info(f"{n_total - n_valid} month(s) in the sample are missing at least one input. "
                "They are excluded from RER* and shown as gaps in the chart.")
    if n_valid == 0:
        st.warning("No valid months in the selected sample.")
        return

    st.plotly_chart(make_chart(w, mode, c1, c2), width="stretch")
    st.caption("Real Index > 100: Country 1's currency is stronger in real terms than its sample average "
               f"(a real appreciation of {c1} against {c2}). Nominal S(t) is in actual currency units.")

    st.download_button(
        "Download CSV (selected sample)",
        data=to_csv(w, c1, c2, iso1, iso2),
        file_name=f"rer_{iso1}_{iso2}_{start:%Y%m}_{end:%Y%m}.csv",
        mime="text/csv",
    )

    with st.expander("Data table"):
        st.dataframe(w, width="stretch")


if __name__ == "__main__":
    main()
