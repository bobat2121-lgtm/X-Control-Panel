"""The Par Keeper: how a monthly rate reset pulls STRC back to $100

A toy simulation of a variable-rate perpetual preferred. The market's required yield wanders and gets hit by
a BTC-drawdown shock. Compare a fixed dividend with a 'thermostat' that nudges the rate every month to steer
the price back toward par. Illustrative model for intuition only, not Strategy's actual pricing or policy.
"""
import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

st.title("🌡️ The Par Keeper")
st.caption("How a monthly dividend-rate reset works like a thermostat for a $100 preferred. "
           "Toy model for intuition, not a forecast or Strategy's actual policy.")

with st.sidebar:
    st.header("Knobs")
    months = st.slider("Months simulated", 6, 48, 24)
    start_rate = st.slider("Starting dividend rate (%)", 6.0, 14.0, 10.0, 0.25)
    base_yield = st.slider("Market's normal required yield (%)", 6.0, 14.0, 10.0, 0.25)
    vol = st.slider("Required-yield volatility", 0.0, 1.5, 0.5, 0.05)
    shock_month = st.slider("BTC drawdown shock hits in month", 1, months, min(6, months))
    shock_size = st.slider("Shock size (+ pts of required yield)", 0.0, 6.0, 3.0, 0.25)
    step = st.slider("Rate step per month (bps)", 0, 100, 50, 5) / 100
    stickiness = st.slider("How long shocks linger (months)", 1, 24, 8)
    band = st.slider("Dead band around par ($)", 0.0, 3.0, 1.0, 0.25)
    seed = st.number_input("Random seed", 0, 9999, 21)

rng = np.random.default_rng(int(seed))
days_per_month = 21
n = months * days_per_month

# Required yield: mean-reverting walk plus a slowly decaying shock
pull = 1 / (stickiness * days_per_month)  # daily mean-reversion speed
y = np.empty(n)
y[0] = base_yield
for t in range(1, n):
    shock = shock_size if t == (shock_month - 1) * days_per_month else 0.0
    y[t] = y[t - 1] + pull * (base_yield - y[t - 1]) + vol * 0.05 * rng.standard_normal() + shock
y = np.clip(y, 2.0, 30.0)


def simulate(adjust: bool):
    rate = start_rate
    prices, rates = np.empty(n), np.empty(n)
    for t in range(n):
        if adjust and t > 0 and t % days_per_month == 0:
            avg = prices[t - days_per_month:t].mean()
            if avg < 100 - band:
                rate += step
            elif avg > 100 + band:
                rate -= step
            rate = float(np.clip(rate, 4.0, 20.0))
        # perpetual: price ~ coupon / required yield, with upside capped near par (issuance supply)
        prices[t] = min(100 * rate / y[t], 101.5) + 0.15 * rng.standard_normal()
        rates[t] = rate
    return prices, rates


p_fixed, r_fixed = simulate(False)
p_thermo, r_thermo = simulate(True)

month = np.arange(n) / days_per_month
COLORS = {"Thermostat (monthly reset)": "#F7931A", "Fixed rate": "#8899A6", "$100 par": "#14171A",
          "Thermostat dividend rate %": "#F7931A", "Fixed dividend rate %": "#8899A6",
          "Market required yield %": "#7C4DFF"}


def chart(data: dict, height: int, title: str):
    df = pd.DataFrame(data).assign(month=month).melt("month", var_name="series", value_name="value")
    return (alt.Chart(df, height=height, title=title).mark_line(strokeWidth=2.2)
            .encode(x=alt.X("month:Q", title="month"),
                    y=alt.Y("value:Q", scale=alt.Scale(zero=False), title=None),
                    color=alt.Color("series:N", scale=alt.Scale(domain=list(data), range=[COLORS[k] for k in data]),
                                    legend=alt.Legend(orient="bottom", title=None))))


st.altair_chart(chart({"Thermostat (monthly reset)": p_thermo, "Fixed rate": p_fixed, "$100 par": np.full(n, 100.0)},
                      360, "Price"), use_container_width=True)

c = st.columns(4)
near = lambda p: (np.abs(p - 100) <= 2).mean() * 100  # noqa: E731
c[0].metric("Days within $2 of par: thermostat", f"{near(p_thermo):.0f}%")
c[1].metric("Days within $2 of par: fixed", f"{near(p_fixed):.0f}%")
c[2].metric("Lowest price: thermostat", f"${p_thermo.min():.2f}")
c[3].metric("Lowest price: fixed", f"${p_fixed.min():.2f}")

st.altair_chart(chart({"Thermostat dividend rate %": r_thermo, "Fixed dividend rate %": r_fixed,
                       "Market required yield %": y}, 280, "Dividend rate vs the market's required yield"),
                use_container_width=True)

with st.expander("How the model works"):
    st.markdown(
        "- The market's **required yield** drifts around its normal level, and a BTC drawdown **shock** "
        "pushes it up, then it decays.\n"
        "- A perpetual preferred trades roughly at **coupon ÷ required yield**. When required yield rises, the price falls.\n"
        "- **Thermostat:** each month the issuer looks at the average price and raises the rate one step if it "
        "traded below par (outside the dead band), or lowers it if above.\n"
        "- Upside is capped near par because new issuance meets demand there.\n\n"
        "Real-world behavior depends on the issuer's actual rate decisions, credit perception, BTC, liquidity and "
        "more. This is a teaching toy.")
