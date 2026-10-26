Real Exchange Rate Explorer

A Streamlit app that pulls monthly IMF consumer price indexes and exchange rates for any two IMF countries (or the Euro area) and plots the bilateral real exchange rate alongside the nominal rate.

Definitions
Symbol	Meaning
E1(t), E2(t)	IMF exchange rates: local currency units per USD for Country 1 and Country 2
S(t)	E2(t) / E1(t) = units of Country 2 currency per 1 unit of Country 1 currency
P1(t), P2(t)	Monthly CPI of Country 1 and Country 2
RER(t)	S(t) × P1(t) / P2(t)
RER*	Average of RER(t) over valid months in the selected sample
Real Index(t)	100 × RER(t) / RER*

A month is valid when S(t), P1(t) and P2(t) are all present and positive. RER* is recomputed whenever the start or end month changes. Nominal S(t) is plotted in actual currency units and is never normalized.

A Real Index above 100 means Country 1's currency is stronger in real terms than its average over the sample (Country 1 goods are relatively expensive).

Data source

IMF SDMX 3.0 API: https://api.imf.org/external/sdmx/3.0

CPI: dataflow IMF.STA/CPI, key {ISO3}.CPI._T.IX.M
Exchange rate: dataflow IMF.STA/ER, key {ISO3}.XDC_USD.PA_RT.M (period-average, local currency per USD)
Euro area code: G163
The United States is the numeraire, so its rate is set to 1.

Data are requested from January 1976 where available. Responses are cached for 12 hours.

Install and run (Python 3.12)
bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py

The app opens at http://localhost:8501.

Using the app
Pick Country 1 (base currency) and Country 2 (quote currency) in the sidebar.
Set the start and end months with the slider. The range is limited to months where all four inputs overlap.
Choose Real, Nominal or Both. Both puts the Real Index on the left axis and S(t) on the right axis.
Click Download CSV to export every month in the sample with E1, E2, S, P1, P2, RER, the valid flag, RER* and the Real Index.
Notes and limitations
IMF coverage varies by country. Some series start well after 1976, end early, or have gaps. Months with any missing input are excluded from RER* and appear as breaks in the chart. The Data coverage panel lists each series' first and last month and missing-month count.
The RER(t) level depends on each CPI's base year, which is why the app normalizes it to the sample mean. Only the Real Index is comparable across samples.
Currency redenominations, euro adoption, or pegs and devaluations can create level shifts in S(t). The app uses the IMF series as published.
The Euro area exchange-rate series may only begin in 1999.
The country list is loaded from the IMF CPI codelist and filtered to countries with monthly CPI. If that request fails, the app falls back to the ISO 3166 list from pycountry, and some of those codes may return no data.
If the IMF API is down or rate-limited, the app retries three times, then shows an error.
