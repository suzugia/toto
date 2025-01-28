import streamlit as st
import pandas as pd
# import matplotlib.pyplot as plt
import requests
from predico import PredicoAPI
import downloader
import os


st.set_page_config(
    page_title="Predico monitoring",
    layout="wide",  # Enable wide mode
    initial_sidebar_state="expanded"  # Sidebar is expanded by default
)

# Set dark theme for the app
st.markdown(
    """
    <style>
    body {
        color: #fff;
        background-color: #1e1e1e;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# !!!!!!!!!!!!!!!!

user_env = os.getenv("USER") 
pwd_env = os.getenv("PWD")

# !!!!!!!!!!!!!!!!!!


def get_api():
    api = PredicoAPI(user_env,pwd_env)
    api.authenticate()
    return api

# 2. Cache all submission metadata
def list_submissions(_api):
    """Return submission info sorted by most recent submission time."""
    url = "https://predico-elia.inesctec.pt/api/v1/market/challenge/submission"
    resp = requests.get(url, headers=_api._headers())
    data = resp.json()["data"]

    df_subs = pd.DataFrame(data)
    df_subs["registered_at"] = pd.to_datetime(df_subs["registered_at"])
    # Sort descending by submission time
    df_subs = df_subs.sort_values("registered_at", ascending=False)
    df_subs["registered_at"] = df_subs["registered_at"].dt.round("T")
    df_subs = df_subs.drop_duplicates(subset=["registered_at"], keep="last")
    return df_subs

# 3. Fetch data (scores + forecasts) only for the selected submission
def fetch_submission_data(_api, _challenge_id, _submission_time):
    # --- Scores ---
    url_sc = "https://predico-elia.inesctec.pt/api/v1/market/challenge/submission-scores"
    sc_resp = requests.get(url_sc, params={"challenge": _challenge_id}, headers=_api._headers())
    sc_data = sc_resp.json()["data"]["personal_metrics"]
    df_scores = pd.DataFrame(sc_data)
    # Add 'market_date' as day after submission_time
    df_scores["market_date"] = (_submission_time + pd.Timedelta(days=1)).date()

    # --- Forecasts ---
    url_fc = "https://predico-elia.inesctec.pt/api/v1/market/challenge/submission/forecasts"
    fc_resp = requests.get(url_fc, params={"challenge": _challenge_id}, headers=_api._headers())
    fc = pd.DataFrame(fc_resp.json()["data"])
    fc = fc.sort_values("registered_at").drop_duplicates(subset=["datetime","variable"], keep="last")
    pivoted = fc.pivot(index="datetime", columns="variable", values="value")
    pivoted.index = pd.to_datetime(pivoted.index)

    # Merge with actual
    act = downloader.get_actual_wind_offshore_from_date(pivoted.index[0])
    act.index = act.index
    pivoted = pd.concat([act[["actual elia","DA elia (11AM)","latest elia"]], pivoted], axis=1)

    return df_scores, pivoted

def add_daily_payout(df, daily_pool=225.8065):
    # Mapping of rank to fraction
    distribution = {1: 0.40, 2: 0.27, 3: 0.18, 4: 0.10, 5: 0.05}
    
    def calc_payout(r_rmse, r_winkler):
        frac_rmse = distribution.get(r_rmse, 0)
        frac_winkler = distribution.get(r_winkler, 0)
        return round((0.5 * frac_rmse + 0.5 * frac_winkler) * daily_pool, 2)

    payouts = {}
    for date, group in df.groupby("market_date"):
        rank_rmse = group.loc[group["metric"] == "rmse", "rank"]
        rank_winkler = group.loc[group["metric"] == "winkler", "rank"]
        r_rmse = rank_rmse.iloc[0] if not rank_rmse.empty else 99
        r_winkler = rank_winkler.iloc[0] if not rank_winkler.empty else 99
        payouts[date] = calc_payout(r_rmse, r_winkler)

    df["daily_payout"] = df["market_date"].map(payouts)
    return df




def fetch_last_50_scores(_api):
    """
    Fetch the last 50 submissions (from your sorted submission list)
    and retrieve their scores from the submission-scores endpoint.
    Returns a combined DataFrame of all personal_metrics found.
    """
    # 1. Get your existing list of submissions (assumes sorted descending by 'registered_at')
    df_subs = list_submissions(_api)
    
    # 2. Take the latest 50
    latest_50 = df_subs.head(60)  # top 50 rows if sorted by descending time
    
    all_scores = []
    for _, row in latest_50.iterrows():
        challenge_id = row["market_session_challenge"]
        sub_time = row["registered_at"].tz_convert('CET')
        sub_id = row["id"]

        # 3. Request scores for this specific challenge
        url_sc = "https://predico-elia.inesctec.pt/api/v1/market/challenge/submission-scores"
        resp = requests.get(url_sc, params={"challenge": challenge_id}, headers=_api._headers())
        if resp.status_code != 200:
            continue  # skip if no data

        sc_data = resp.json()["data"]["personal_metrics"]
        df_scores = pd.DataFrame(sc_data)

        # 4. Add helpful columns (submission_time, submission_id, market_date, etc.)
        df_scores["submission_id"] = sub_id
        df_scores["submission_time"] = sub_time
        # Example: market_date is day after submission_time
        df_scores["market_date"] = (sub_time + pd.Timedelta(days=1)).date()

        all_scores.append(df_scores)

    # Combine into one DataFrame
    if all_scores:
        final_scores = pd.concat(all_scores, ignore_index=True)
    else:
        final_scores = pd.DataFrame()  # empty if none returned
        
    
    filter_score = final_scores.loc[final_scores.metric.isin(['winkler','rmse']),['variable','metric','value','rank','market_date']]
    df_lowest = (filter_score.loc[filter_score.groupby(["market_date", "metric"])["rank"].idxmin()]
    .drop_duplicates().reset_index(drop=True)
    .sort_values('market_date')
    )


    award = add_daily_payout(df_lowest)


    return award

from plotly.subplots import make_subplots

import plotly.graph_objects as go
def plot_rank_and_payout_separate(df):
    # Prepare data
    df_rmse = df[df["metric"] == "rmse"].copy()
    df_winkler = df[df["metric"] == "winkler"].copy()
    df_payout = df.groupby("market_date", as_index=False)["daily_payout"].first()

    # Compute monthly payout
    df_payout["market_date"] = pd.to_datetime(df_payout["market_date"])
    df_payout["month"] = df_payout["market_date"].dt.to_period("M")
    df_monthly = df_payout.groupby("month", as_index=False)["daily_payout"].sum()
    df_monthly["month_dt"] = df_monthly["month"].dt.to_timestamp()  # Convert to timestamp for plotting

    # Create 3 subplots with spacing
    fig = make_subplots(
        rows=3, cols=1,
        subplot_titles=["", "", ""],
        vertical_spacing=0.1
    )

    # 1) Rank subplot
    fig.add_trace(go.Bar(x=df_rmse["market_date"], y=df_rmse["rank"], name="RMSE Rank"), row=1, col=1)
    fig.add_trace(go.Bar(x=df_winkler["market_date"], y=df_winkler["rank"], name="Winkler Rank"), row=1, col=1)

    # 2) Daily payout subplot
    fig.add_trace(go.Bar(x=df_payout["market_date"], y=df_payout["daily_payout"], name="Daily Payout"), row=2, col=1)

    # 3) Monthly payout subplot (only months on x-axis)
    fig.add_trace(go.Bar(x=df_monthly["month_dt"], y=df_monthly["daily_payout"], name="Monthly Payout"), row=3, col=1)

    # Update layout and axes
    fig.update_layout(barmode="group")
    # fig.update_xaxes(title_text="Market Date", row=1, col=1)
    # fig.update_xaxes(title_text="Market Date", row=2, col=1)
    # fig.update_xaxes(title_text="Month", tickformat="%Y-%m", row=3, col=1)
    fig.update_yaxes(title_text="Rank", row=1, col=1)
    fig.update_yaxes(title_text="PnL (€)", row=2, col=1)
    fig.update_yaxes(title_text="PnL (€)", row=3, col=1)
    fig.update_layout(
        barmode="group",
        width=800,  # set plot width
        height=900 , # set plot height
        showlegend=False
    )

    return fig
# ---------------- Main App ----------------

def submission_viewer():
    st.subheader("Submission Viewer")

    # 1. Authenticate & get submissions
    api = get_api()
    df_subs = list_submissions(api)

    df_subs["registered_at"] = df_subs["registered_at"].dt.tz_convert('CET')
    # 2. Let user select submission
    df_subs["label"] = (
        "Market date " + ((df_subs["registered_at"]+pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d"))
        +" | ID: " + df_subs["id"].astype(str)
        + " | Time: " + df_subs["registered_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
        
    )
    selected_label = st.selectbox("Select submission", df_subs["label"])

    # Extract the row matching the chosen label
    chosen_row = df_subs.loc[df_subs["label"] == selected_label].iloc[0]
    submission_time = chosen_row["registered_at"]
    challenge_id = chosen_row["market_session_challenge"]

    # 3. Fetch data for the chosen submission (scores + forecast)
    scores, forecasts = fetch_submission_data(api, challenge_id, submission_time)

    # Force the forecast index to be timezone-naive for consistent comparisons
    forecasts.index = forecasts.index.tz_localize(None)

    # 4. Display data
    st.subheader("Scores for This Submission")
    if scores.empty:
        st.warning("No scoring data for this submission. Showing forecasts only.")
    else:
        st.dataframe(scores)

    st.subheader("Forecast vs Actual")

    if scores.empty:
        # st.warning("No scoring data for this submission. Showing forecasts only.")
        data_slice = forecasts
        market_date = forecasts["q10"].dropna().index[0]
    else:
        market_date = scores["market_date"].iloc[0]
    # mask = (
    #     (forecasts.index >= pd.to_datetime(market_date))
    #     & (forecasts.index < pd.to_datetime(market_date) + pd.Timedelta(days=1))
    # )
    data_slice = forecasts.dropna(subset='q10')
    # data_slice = forecasts.loc[mask]


    fig = go.Figure()

    # --- 1) Add the uncertainty band (q10 - q90) ---
    if "q90" in data_slice.columns and "q10" in data_slice.columns:
        # Upper boundary (q90)
        fig.add_trace(
            go.Scatter(
                x=data_slice.index,
                y=data_slice["q90"],
                name="q90",
                mode="lines",
                line_color="rgba(0,0,0,0)",  # Make the boundary line transparent
                showlegend=True
            )
        )

        # Lower boundary (q10) with fill to the previous trace
        fig.add_trace(
            go.Scatter(
                x=data_slice.index,
                y=data_slice["q10"],
                name="Uncertainty [q10–q90]",
                mode="lines",
                fill="tonexty",  # Fill to previous trace
                fillcolor="rgba(0, 100, 80, 0.4)",  # A soft green fill
                line_color="rgba(0,0,0,0)",
                showlegend=True
            )
        )

    # --- 2) Optionally add q50, DA elia (11AM), latest elia, etc. ---
    # Add q50 as a normal line if present
    if "q50" in data_slice.columns:
        fig.add_trace(
            go.Scatter(
                x=data_slice.index,
                y=data_slice["q50"],
                name="q50",
                mode="lines",
                line_color="rgb(5, 222, 255)"
            )
        )
    # Add DA elia (11AM)
    if "DA elia (11AM)" in data_slice.columns:
        fig.add_trace(
            go.Scatter(
                x=data_slice.index,
                y=data_slice["DA elia (11AM)"],
                name="DA elia (11AM)",
                mode="lines",
                line_color="orange"
            )
        )
    # Add latest elia
    if "latest elia" in data_slice.columns:
        fig.add_trace(
            go.Scatter(
                x=data_slice.index,
                y=data_slice["latest elia"],
                name="latest elia",
                mode="lines",
                line_color="red",
                visible='legendonly'
            )
        )

    # --- 3) Add the actual elia as a white line ---
    if "actual elia" in data_slice.columns:
        fig.add_trace(
            go.Scatter(
                x=data_slice.index,
                y=data_slice["actual elia"],
                name="actual elia",
                mode="lines",
                line_color="white"
            )
        )

    # --- 4) Final styling of the plot ---
    fig.update_layout(
        xaxis_title="Datetime",
        yaxis_title="MW",
        yaxis=dict(range=[0, 2300]),
        template="plotly_dark",  # Optional: in dark mode to let white stand out
        showlegend=False
    )

    st.plotly_chart(fig)



def overview():
    """
    Let user pick a start/end date, then fetch & plot only that range.
    """
    # Add password protection
    PASSWORD = "Eli@2025!"  # Set your password
    if "authenticated_overview" not in st.session_state:
        st.session_state.authenticated_overview = False

    if not st.session_state.authenticated_overview:
        password = st.text_input("Enter Password to Access Overview:", type="password")
        if st.button("Login"):
            if password == PASSWORD:
                st.session_state.authenticated_overview = True
                st.success("Access granted!")
            else:
                st.error("Invalid password!")
        return

    # If authenticated, proceed with the overview logic
    st.title("Ranking & PnL")
    api = get_api()
    data = fetch_last_50_scores(api)
    st.plotly_chart(plot_rank_and_payout_separate(data))
    st.dataframe(data.sort_values(by='market_date', ascending=False).drop(columns='variable'))
    

# ---------------- Main App with Navigation ----------------
def main():
    st.sidebar.title("Navigation")
    page_choice = st.sidebar.radio("Go to page:", ["Submission Viewer", "Overview"])
    if page_choice == "Submission Viewer":
        submission_viewer()
    else:
        overview()
    

if __name__ == "__main__":
    main()

