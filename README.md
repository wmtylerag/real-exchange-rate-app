# Bilateral Real Exchange Rate Calculator

A Streamlit web app that computes the real exchange rate between any two IMF economies
(or the Euro Area) from monthly IMF CPI and exchange-rate data, January 1976 onward
where available.

## Quick start (Python 3.12)

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

The app opens at http://localhost:8501.

## What it calculates

| Symbol | Definition |
|---|---|
| S(t) | Units of Country 2's currency per 1 unit of Country 1's currency |
| P1(t), P2(t) | Monthly CPI (all items, index) of Country 1 and Country 2 |
| RER(t) | S(t) × P1(t) / P2(t) |
| RER* | Average of RER(t) over the selected start–end months |
| Real Index(t) | 100 × RER(t) / RER* |

S(t) is built as a cross rate through the US dollar:
S(t) = [Country 2 currency per USD](t) / [Country 1 currency per USD](t).
For the United States the USD rate is 1.

A Real Index above 100 means Country 1's currency is stronger in real terms than its
average over the selected period.

## Using the app

- **Sidebar → Countries:** pick Country 1 and Country 2. The list comes from the IMF
  country codelist, filtered to economies with CPI or exchange-rate data, plus the Euro Area.
- **Period for RER\*:** a two-handled slider over the months where both countries have
  complete data. Moving either handle recomputes RER\* and the index immediately.
- **Show full history in chart:** optional. Plots the whole sample (normalized to the
  selected RER\*) and shades the selected period.
- **Series:** Real, Nominal, or Both. "Both" uses separate left (Real Index) and right
  (nominal S, in actual currency units) axes. An optional log scale helps for
  high-inflation currencies.
- **Download CSV:** all observations and calculations for the selected months:
  month, each currency per USD, P1, P2, S, RER, RER\*, Real Index.
- **Refresh IMF data:** data are cached for 24 hours; this button clears the cache.

## Data source

IMF Data portal SDMX 3.0 REST API, `https://api.imf.org/external/sdmx/3.0`.
Data requests take the form

```
{base}/data/dataflow/IMF.STA/{dataflow}/+/{key}?c[TIME_PERIOD]=ge:1976-01
```

where `+` means the latest version of the dataflow.

| Input | Dataflow | Series key |
|---|---|---|
| CPI | `IMF.STA:CPI` | `{ISO3}.CPI._T.IX.M` (CPI, all items, index, monthly) |
| Exchange rate | `IMF.STA:ER` | `{ISO3}.XDC_USD.PA_RT.M` (local currency per USD, period average, monthly) |

`{ISO3}` is the ISO 3-letter country code; the Euro Area uses `G163`.
The IMF publishes rates as local currency per USD, so the app converts them to
S(t) = [Country 2 per USD] / [Country 1 per USD]. If `XDC_USD` is unavailable it falls
back to `USD_XDC` and inverts it. The country list comes from the CPI dataflow's
structure (`/structure/dataflow/...`) and the `/availability/...` endpoint. Responses
are parsed as SDMX-ML or SDMX-JSON, whichever the API returns.

### Configuration

No API key is required for public IMF data. Everything can be overridden with
environment variables if your IMF access differs:

| Variable | Default |
|---|---|
| `IMF_BASE_URL` | `https://api.imf.org/external/sdmx/3.0` |
| `IMF_FLOW_VERSION` | `+` (latest dataflow version) |
| `IMF_EURO_AREA_CODE` | `G163` |
| `IMF_API_KEY` | *(empty; sent as `Ocp-Apim-Subscription-Key` if set)* |
| `IMF_CPI_FLOW` / `IMF_ER_FLOW` | `CPI` / `ER` |
| `IMF_CPI_KEY` | `{c}.CPI._T.IX.M` |
| `IMF_ER_KEY` / `IMF_ER_KEY_INV` | `{c}.XDC_USD.PA_RT.M` / `{c}.USD_XDC.PA_RT.M` |

Example: `IMF_CPI_KEY="{c}.HICP._T.IX.M" streamlit run app.py`

## Data caveats

- **Coverage varies.** Months where any input is missing are excluded from RER\* (the app
  says how many). Some economies (e.g. Australia, New Zealand) publish CPI only
  quarterly, so they have no monthly series and the app reports this.
- **Euro Area** exchange-rate and CPI data generally begin in 1999.
- **Currency redenominations.** The IMF usually expresses historical exchange rates in
  the current currency unit (e.g. euro-legacy countries before 1999, or currencies that
  dropped zeros). Check the IMF metadata when interpreting long nominal histories.
- **CPI base years** differ by country. This does not affect the Real Index, which is
  normalized by RER\*, but the level of RER(t) itself has no direct interpretation.

## Deployment

Works on Streamlit Community Cloud, or any host with Python 3.12:
`streamlit run app.py --server.port $PORT --server.address 0.0.0.0`.
