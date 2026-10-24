"""
Bilateral Real Exchange Rate (RER) calculator
=============================================

Data source: IMF Data portal SDMX 3.0 REST API (https://api.imf.org/external/sdmx/3.0)
  * CPI  -> dataflow IMF.STA:CPI, key {ISO3}.CPI._T.IX.M
            (Consumer Price Index, all items, index, monthly)
  * FX   -> dataflow IMF.STA:ER,  key {ISO3}.XDC_USD.PA_RT.M
            (local currency per US dollar, period average, monthly)
  * Euro area uses the code G163.

Definitions
  S(t)          = units of Country 2 currency per 1 unit of Country 1 currency
                = [C2 per USD](t) / [C1 per USD](t)
  RER(t)        = S(t) * P1(t) / P2(t)
  RER*          = mean of RER(t) over the user-selected months
  Real Index(t) = 100 * RER(t) / RER*

Run:  streamlit run app.py
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

# --------------------------------------------------------------------------- #
# Configuration (override with environment variables if the IMF changes keys)
# --------------------------------------------------------------------------- #
IMF_BASE_URL = os.getenv("IMF_BASE_URL", "https://api.imf.org/external/sdmx/3.0")
IMF_API_KEY = os.getenv("IMF_API_KEY", "")  # optional subscription key
AGENCY = "IMF.STA"
FLOW_VERSION = os.getenv("IMF_FLOW_VERSION", "+")  # "+" = latest stable version
CPI_FLOW = os.getenv("IMF_CPI_FLOW", "CPI")
ER_FLOW = os.getenv("IMF_ER_FLOW", "ER")
# Key templates: {c} is replaced by the country code
CPI_KEY = os.getenv("IMF_CPI_KEY", "{c}.CPI._T.IX.M")
ER_KEY = os.getenv("IMF_ER_KEY", "{c}.XDC_USD.PA_RT.M")       # domestic per USD
ER_KEY_INV = os.getenv("IMF_ER_KEY_INV", "{c}.USD_XDC.PA_RT.M")  # USD per domestic
START_PERIOD = "1976-01"
EURO_AREA_CODE = os.getenv("IMF_EURO_AREA_CODE", "G163")
TIMEOUT = 60
CACHE_TTL = 24 * 3600

# Fallback list used only if the IMF codelist cannot be downloaded.
FALLBACK_COUNTRIES: dict[str, str] = {
    "G163": "Euro Area", "ARG": "Argentina", "AUS": "Australia", "AUT": "Austria",
    "BEL": "Belgium", "BRA": "Brazil", "CAN": "Canada", "CHE": "Switzerland",
    "CHL": "Chile", "CHN": "China", "COL": "Colombia", "CZE": "Czech Republic",
    "DEU": "Germany", "DNK": "Denmark", "EGY": "Egypt", "ESP": "Spain",
    "FIN": "Finland", "FRA": "France", "GBR": "United Kingdom", "GRC": "Greece",
    "HKG": "Hong Kong SAR", "HUN": "Hungary", "IDN": "Indonesia", "IND": "India",
    "IRL": "Ireland", "ISL": "Iceland", "ISR": "Israel", "ITA": "Italy",
    "JPN": "Japan", "KOR": "Korea", "MEX": "Mexico", "MYS": "Malaysia",
    "NGA": "Nigeria", "NLD": "Netherlands", "NOR": "Norway", "NZL": "New Zealand",
    "PAK": "Pakistan", "PER": "Peru", "PHL": "Philippines", "POL": "Poland",
    "PRT": "Portugal", "ROU": "Romania", "RUS": "Russian Federation",
    "SAU": "Saudi Arabia", "SGP": "Singapore", "SWE": "Sweden", "THA": "Thailand",
    "TUR": "Türkiye", "UKR": "Ukraine", "USA": "United States", "VNM": "Vietnam",
    "ZAF": "South Africa",
}

# Currency labels for axis titles (anything missing falls back to a generic label).
CURRENCIES: dict[str, str] = {
    "G163": "EUR", "AUT": "EUR", "BEL": "EUR", "CYP": "EUR", "DEU": "EUR",
    "EST": "EUR", "ESP": "EUR", "FIN": "EUR", "FRA": "EUR", "GRC": "EUR",
    "HRV": "EUR", "IRL": "EUR", "ITA": "EUR", "LTU": "EUR", "LUX": "EUR",
    "LVA": "EUR", "MLT": "EUR", "NLD": "EUR", "PRT": "EUR", "SVK": "EUR",
    "SVN": "EUR", "USA": "USD", "GBR": "GBP", "JPN": "JPY", "CHE": "CHF",
    "CAN": "CAD", "AUS": "AUD", "NZL": "NZD", "SWE": "SEK", "NOR": "NOK",
    "DNK": "DKK", "ISL": "ISK", "CHN": "CNY", "HKG": "HKD", "IND": "INR",
    "KOR": "KRW", "SGP": "SGD", "MYS": "MYR", "THA": "THB", "IDN": "IDR",
    "PHL": "PHP", "VNM": "VND", "PAK": "PKR", "BGD": "BDT", "LKA": "LKR",
    "BRA": "BRL", "MEX": "MXN", "ARG": "ARS", "CHL": "CLP", "COL": "COP",
    "PER": "PEN", "URY": "UYU", "ZAF": "ZAR", "NGA": "NGN", "EGY": "EGP",
    "KEN": "KES", "MAR": "MAD", "TUR": "TRY", "ISR": "ILS", "SAU": "SAR",
    "ARE": "AED", "RUS": "RUB", "UKR": "UAH", "POL": "PLN", "CZE": "CZK",
    "HUN": "HUF", "ROU": "RON", "BGR": "BGN",
}

# --------------------------------------------------------------------------- #
# SDMX helpers
# --------------------------------------------------------------------------- #
_PERIOD_RE = re.compile(r"^(\d{4})-?M?(\d{1,2})$")


def _local(tag: str) -> str:
    """Strip an XML namespace: '{ns}Obs' -> 'Obs'."""
    return tag.rsplit("}", 1)[-1]


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.sdmx.data+xml;version=3.0.0, application/xml;q=0.9",
        "User-Agent": "rer-calculator/1.0 (+streamlit)",
        "Cache-Control": "no-cache",
    }
    if IMF_API_KEY:
        h["Ocp-Apim-Subscription-Key"] = IMF_API_KEY
    return h


def _get(url: str, params: dict | None = None, retries: int = 3) -> requests.Response | None:
    """GET with simple retry/backoff. Returns None for 404 / 'no results'."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=_headers(), timeout=TIMEOUT)
            if r.status_code == 404:
                return None  # SDMX: no data for this key
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except requests.RequestException as err:  # noqa: PERF203
            last_err = err
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"IMF API request failed: {url} ({last_err})")


def parse_period(p: str) -> pd.Timestamp | None:
    m = _PERIOD_RE.match(p.strip())
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return pd.Timestamp(year=year, month=month, day=1)


def parse_sdmx_series(xml_bytes: bytes) -> pd.Series:
    """
    Parse an SDMX data message (SDMX-ML 3.0/2.1 structure-specific or generic,
    or SDMX-JSON 2.0/1.0)
    into a monthly float Series indexed by month-start Timestamps.
    If several series come back, the first non-missing value per month is used.
    """
    if xml_bytes.lstrip()[:1] in (b"{", b"["):
        return parse_sdmx_json(xml_bytes)
    records: dict[pd.Timestamp, float] = {}
    for _event, el in ET.iterparse(io.BytesIO(xml_bytes), events=("end",)):
        if _local(el.tag) != "Obs":
            continue
        period = el.attrib.get("TIME_PERIOD")
        value = el.attrib.get("OBS_VALUE")
        if period is None or value is None:  # generic format
            for child in el:
                name = _local(child.tag)
                if name == "ObsDimension":
                    period = child.attrib.get("value", period)
                elif name == "ObsValue":
                    value = child.attrib.get("value", value)
        el.clear()
        if not period or value in (None, "", "NaN", "NA"):
            continue
        ts = parse_period(period)
        if ts is None:
            continue
        try:
            v = float(value)
        except ValueError:
            continue
        records.setdefault(ts, v)
    if not records:
        return pd.Series(dtype="float64")
    return pd.Series(records, dtype="float64").sort_index()


def parse_sdmx_json(raw: bytes) -> pd.Series:
    """Parse SDMX-JSON (2.0 for SDMX 3.0, or 1.0) into a monthly float Series."""
    msg = json.loads(raw)
    data = msg.get("data", msg)
    structure = data.get("structure") or (data.get("structures") or [{}])[0]
    obs_dims = structure.get("dimensions", {}).get("observation", [])
    time_dim = next((d for d in obs_dims if d.get("id") == "TIME_PERIOD"),
                    obs_dims[0] if obs_dims else {})
    periods = [v.get("id") or v.get("value") for v in time_dim.get("values", [])]
    records: dict[pd.Timestamp, float] = {}
    for ds in data.get("dataSets", []):
        for series in (ds.get("series") or {}).values():
            for idx, obs in (series.get("observations") or {}).items():
                i = int(idx.split(":")[0])
                if i >= len(periods) or not obs or obs[0] in (None, "", "NaN"):
                    continue
                ts = parse_period(str(periods[i]))
                if ts is None:
                    continue
                try:
                    records.setdefault(ts, float(obs[0]))
                except (TypeError, ValueError):
                    continue
    if not records:
        return pd.Series(dtype="float64")
    return pd.Series(records, dtype="float64").sort_index()


def fetch_series(flow: str, key: str) -> pd.Series:
    """SDMX 3.0: /data/dataflow/{agency}/{flow}/{version}/{key}?c[TIME_PERIOD]=ge:YYYY-MM"""
    url = f"{IMF_BASE_URL}/data/dataflow/{AGENCY}/{flow}/{FLOW_VERSION}/{key}"
    r = _get(url, params={"c[TIME_PERIOD]": f"ge:{START_PERIOD}",
                          "dimensionAtObservation": "TIME_PERIOD"})
    if r is None or not r.content.strip():
        return pd.Series(dtype="float64")
    return parse_sdmx_series(r.content)


# --------------------------------------------------------------------------- #
# Cached data access
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def get_cpi(country: str) -> pd.Series:
    return fetch_series(CPI_FLOW, CPI_KEY.format(c=country))


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def get_per_usd(country: str) -> pd.Series:
    """Units of the country's currency per 1 USD (monthly period average)."""
    # IMF publishes local currency per USD; S(t) is derived from these in build_panel.
    s = fetch_series(ER_FLOW, ER_KEY.format(c=country))
    if s.empty:
        inv = fetch_series(ER_FLOW, ER_KEY_INV.format(c=country))
        inv = inv[inv != 0]
        s = 1.0 / inv if not inv.empty else s
    if s.empty and country == "USA":
        # USD per USD is identically 1; build it over the full monthly range.
        idx = pd.date_range(START_PERIOD, pd.Timestamp.today().normalize(), freq="MS")
        s = pd.Series(1.0, index=idx)
    return s


def _parse_codelists(xml_bytes: bytes) -> dict[str, dict[str, str]]:
    """Return {codelist_id: {code: english_name}} from an SDMX structure message."""
    out: dict[str, dict[str, str]] = {}
    root = ET.fromstring(xml_bytes)
    for cl in root.iter():
        if _local(cl.tag) != "Codelist":
            continue
        codes: dict[str, str] = {}
        for code in cl:
            if _local(code.tag) != "Code":
                continue
            name = None
            for child in code:
                if _local(child.tag) == "Name":
                    lang = child.attrib.get("{http://www.w3.org/XML/1998/namespace}lang", "en")
                    if lang.startswith("en") or name is None:
                        name = (child.text or "").strip()
            codes[code.attrib.get("id", "")] = name or code.attrib.get("id", "")
        out[cl.attrib.get("id", "")] = codes
    return out


def _available_countries(flow: str, key_rest: str) -> set[str]:
    """Country codes with data in a dataflow, via the SDMX availability endpoint."""
    url = f"{IMF_BASE_URL}/availability/dataflow/{AGENCY}/{flow}/{FLOW_VERSION}/{key_rest}/COUNTRY"
    r = _get(url, retries=2)
    if r is None:
        return set()
    root = ET.fromstring(r.content)
    codes: set[str] = set()
    for kv in root.iter():
        if _local(kv.tag) == "KeyValue" and kv.attrib.get("id") == "COUNTRY":
            codes.update((v.text or "").strip() for v in kv if _local(v.tag) == "Value")
    codes.discard("")
    return codes


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def get_country_list() -> tuple[dict[str, str], str, str]:
    """
    Returns (code -> name, euro_area_code, status_message).
    Keeps individual economies (ISO-3 codes) plus the Euro Area aggregate.
    """
    names: dict[str, str] = {}
    status = "Country list loaded from the IMF CPI codelist."
    try:
        r = _get(f"{IMF_BASE_URL}/structure/dataflow/{AGENCY}/{CPI_FLOW}/{FLOW_VERSION}",
                 params={"references": "all", "detail": "full"})
        if r is not None:
            lists = _parse_codelists(r.content)
            cl_id = next((k for k in lists if "COUNTRY" in k.upper()), None)
            if cl_id:
                names = lists[cl_id]
    except Exception:  # noqa: BLE001
        names = {}
    if not names:
        return dict(FALLBACK_COUNTRIES), EURO_AREA_CODE, (
            "Could not download the IMF country codelist; using a built-in list "
            "of major economies."
        )

    euro_code = EURO_AREA_CODE

    # Restrict to countries that actually have CPI or ER data, when we can tell.
    available: set[str] = set()
    try:
        available |= _available_countries(CPI_FLOW, CPI_KEY.format(c="*"))
        available |= _available_countries(ER_FLOW, ER_KEY.format(c="*"))
    except Exception:  # noqa: BLE001
        available = set()

    keep = {
        c: n for c, n in names.items()
        if (re.fullmatch(r"[A-Z]{3}", c) or c == euro_code)
        and (not available or c in available)
    }
    if euro_code not in keep:
        keep[euro_code] = names.get(euro_code, "Euro Area")
    keep[euro_code] = keep[euro_code] or "Euro Area"
    return keep, euro_code, status


# --------------------------------------------------------------------------- #
# Calculations
# --------------------------------------------------------------------------- #
def build_panel(c1: str, c2: str) -> pd.DataFrame:
    """Monthly panel with raw inputs, S(t) and RER(t) for all months with complete data."""
    p1, p2 = get_cpi(c1), get_cpi(c2)
    e1, e2 = get_per_usd(c1), get_per_usd(c2)
    if c1 == c2:
        e2 = e1
    df = pd.concat(
        {"C1_per_USD": e1, "C2_per_USD": e2, "P1": p1, "P2": p2}, axis=1, sort=True
    ).sort_index()
    df.index.name = "date"
    df["S"] = df["C2_per_USD"] / df["C1_per_USD"]
    df["RER"] = df["S"] * df["P1"] / df["P2"]
    return df


def compute_window(panel: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp
                   ) -> tuple[pd.DataFrame, float]:
    """Slice to [start, end], compute RER* over that window and the real index."""
    window = panel.loc[start:end].copy()
    rer_star = float(window["RER"].mean(skipna=True))
    window["RER_star"] = rer_star
    window["Real_Index"] = 100.0 * window["RER"] / rer_star
    return window, rer_star


def add_index(panel: pd.DataFrame, rer_star: float) -> pd.DataFrame:
    out = panel.copy()
    out["RER_star"] = rer_star
    out["Real_Index"] = 100.0 * out["RER"] / rer_star
    return out


def currency_label(code: str, names: dict[str, str]) -> str:
    return CURRENCIES.get(code, f"{names.get(code, code)} currency")


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
REAL_COLOR = "#2a6f97"
NOMINAL_COLOR = "#c8553d"


def make_chart(df: pd.DataFrame, view: str, s_label: str, title: str,
               base: tuple[pd.Timestamp, pd.Timestamp] | None, log_nominal: bool
               ) -> go.Figure:
    real_name = "Real Index (RER* = 100)"
    nom_name = f"Nominal S ({s_label})"
    if view == "Both":
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Scatter(x=df.index, y=df["Real_Index"], name=real_name,
                                 line=dict(color=REAL_COLOR, width=2)), secondary_y=False)
        fig.add_trace(go.Scatter(x=df.index, y=df["S"], name=nom_name,
                                 line=dict(color=NOMINAL_COLOR, width=1.6)), secondary_y=True)
        fig.update_yaxes(title_text="Real Index", secondary_y=False,
                         title_font_color=REAL_COLOR, tickfont_color=REAL_COLOR)
        fig.update_yaxes(title_text=s_label, secondary_y=True, showgrid=False,
                         title_font_color=NOMINAL_COLOR, tickfont_color=NOMINAL_COLOR,
                         type="log" if log_nominal else "linear")
        fig.add_hline(y=100, line_dash="dot", line_color="gray", secondary_y=False)
    elif view == "Real":
        fig = go.Figure(go.Scatter(x=df.index, y=df["Real_Index"], name=real_name,
                                   line=dict(color=REAL_COLOR, width=2)))
        fig.update_yaxes(title_text="Real Index (RER* = 100)")
        fig.add_hline(y=100, line_dash="dot", line_color="gray")
    else:
        fig = go.Figure(go.Scatter(x=df.index, y=df["S"], name=nom_name,
                                   line=dict(color=NOMINAL_COLOR, width=2)))
        fig.update_yaxes(title_text=s_label, type="log" if log_nominal else "linear")

    if base is not None:
        fig.add_vrect(x0=base[0], x1=base[1] + pd.offsets.MonthEnd(0),
                      fillcolor="gray", opacity=0.12, line_width=0,
                      annotation_text="RER* period", annotation_position="top left")
    fig.update_layout(
        title=title, hovermode="x unified", height=520,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=80, b=10),
    )
    fig.update_xaxes(rangeslider_visible=True)
    return fig


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def main() -> None:
    st.set_page_config(page_title="Real Exchange Rate Calculator", layout="wide")
    st.title("Bilateral Real Exchange Rate")
    st.caption(
        "RER(t) = S(t) × P1(t) / P2(t), where S(t) is units of Country 2 currency per "
        "unit of Country 1 currency. Source: IMF CPI and Exchange Rates datasets, "
        "monthly, from January 1976 where available."
    )

    with st.spinner("Loading IMF country list…"):
        countries, euro_code, status = get_country_list()
    if "built-in" in status:
        st.warning(status)

    codes = sorted(countries, key=lambda c: (c != euro_code, countries[c]))
    fmt = lambda c: f"{countries.get(c, c)} ({c})"  # noqa: E731

    with st.sidebar:
        st.header("Countries")
        default1 = codes.index("USA") if "USA" in codes else 0
        default2 = codes.index(euro_code) if euro_code in codes else min(1, len(codes) - 1)
        c1 = st.selectbox("Country 1", codes, index=default1, format_func=fmt)
        c2 = st.selectbox("Country 2", codes, index=default2, format_func=fmt)
        if st.button("Refresh IMF data", help="Clear the 24-hour cache and re-download"):
            st.cache_data.clear()
            st.rerun()

    try:
        with st.spinner(f"Downloading IMF data for {countries[c1]} and {countries[c2]}…"):
            panel = build_panel(c1, c2)
    except RuntimeError as err:
        st.error(f"Could not reach the IMF API. {err}")
        st.stop()

    # Report which inputs are missing
    missing = [lbl for col, lbl in [
        ("P1", f"CPI for {countries[c1]}"), ("P2", f"CPI for {countries[c2]}"),
        ("C1_per_USD", f"exchange rate for {countries[c1]}"),
        ("C2_per_USD", f"exchange rate for {countries[c2]}"),
    ] if panel[col].dropna().empty]
    if missing:
        st.error("The IMF returned no monthly data for: " + "; ".join(missing) + ". "
                 "Some economies publish CPI only quarterly, or the series may not exist.")
        st.stop()

    valid = panel.dropna(subset=["RER"])
    if valid.empty:
        st.error("The CPI and exchange rate series for these two countries do not overlap.")
        st.stop()

    months = list(valid.index)
    first, last = months[0], months[-1]

    with st.sidebar:
        st.header("Period for RER*")
        start, end = st.select_slider(
            "Start and end month", options=months, value=(first, last),
            format_func=lambda d: d.strftime("%b %Y"),
        )
        full_history = st.checkbox(
            "Show full history in chart", value=False,
            help="RER* is still the average over the selected months; the selected "
                 "period is shaded. The CSV always covers the selected period.",
        )
        st.header("Chart")
        view = st.radio("Series", ["Real", "Nominal", "Both"], index=2, horizontal=True)
        log_nominal = st.checkbox("Log scale for nominal S", value=False)

    window, rer_star = compute_window(panel, start, end)
    window_valid = window.dropna(subset=["RER"])

    cur1, cur2 = currency_label(c1, countries), currency_label(c2, countries)
    s_label = f"{cur2} per {cur1}"

    # Metrics
    n_sel = len(pd.date_range(start, end, freq="MS"))
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("RER*", f"{rer_star:,.6g}")
    m2.metric("Months in RER*", f"{len(window_valid)} of {n_sel}")
    latest = add_index(valid, rer_star).iloc[-1]
    m3.metric(f"Real Index, {valid.index[-1]:%b %Y}", f"{latest['Real_Index']:.1f}",
              f"{latest['Real_Index'] - 100:+.1f} vs RER*")
    m4.metric(f"Nominal S, {valid.index[-1]:%b %Y}", f"{latest['S']:,.6g}", s_label,
              delta_color="off")

    if len(window_valid) < n_sel:
        st.info(f"{n_sel - len(window_valid)} month(s) in the selected period lack one "
                "or more inputs and are excluded from RER*.")

    plot_df = add_index(valid, rer_star) if full_history else window_valid
    title = f"{countries[c1]} vs {countries[c2]}: " + {
        "Real": "Real exchange rate index",
        "Nominal": f"Nominal exchange rate ({s_label})",
        "Both": "Real index and nominal exchange rate",
    }[view]
    fig = make_chart(plot_df, view, s_label, title,
                     base=(start, end) if full_history else None, log_nominal=log_nominal)
    st.plotly_chart(fig, width="stretch")

    st.caption(
        "Real Index > 100: Country 1's currency is stronger in real terms than its "
        "average over the selected period (Country 1 goods relatively more expensive)."
    )

    # CSV export of the selected period
    export = window.reset_index().rename(columns={
        "date": "month",
        "C1_per_USD": f"{c1}_currency_per_USD",
        "C2_per_USD": f"{c2}_currency_per_USD",
        "P1": f"P1_CPI_{c1}",
        "P2": f"P2_CPI_{c2}",
        "S": f"S_{c2}_per_{c1}",
        "RER": "RER",
        "RER_star": "RER_star",
        "Real_Index": "Real_Index",
    })
    export["month"] = export["month"].dt.strftime("%Y-%m")
    csv = export.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV for selected period", data=csv, mime="text/csv",
        file_name=f"rer_{c1}_{c2}_{start:%Y%m}_{end:%Y%m}.csv", type="primary",
    )

    with st.expander("Data for selected period"):
        st.dataframe(export, width="stretch", hide_index=True)

    with st.expander("Method and sources"):
        st.markdown(
            f"""
- **API:** `{IMF_BASE_URL}` (SDMX 3.0). Euro area code: `{EURO_AREA_CODE}`.
- **CPI:** IMF `{AGENCY}:{CPI_FLOW}`, key `{CPI_KEY}` (all items, index, monthly).
- **Exchange rates:** IMF `{AGENCY}:{ER_FLOW}`, key `{ER_KEY}` (local currency per
  USD, period average, monthly). S(t) is the cross rate
  [{c2} per USD] / [{c1} per USD].
- **RER(t)** = S(t) × P1(t) / P2(t); **RER\\*** = mean RER(t) over
  {start:%b %Y}–{end:%b %Y}; **Real Index(t)** = 100 × RER(t) / RER\\*.
- Data are cached for 24 hours. Retrieved {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.
"""
        )


if __name__ == "__main__":
    main()
