import os
from datetime import datetime
import pandas as pd
import requests
from bs4 import BeautifulSoup
import streamlit as st

# --- Persistent Storage Setup ---
PREDICTIONS_FILE = "predictions.csv"
if not os.path.exists(PREDICTIONS_FILE):
    pd.DataFrame(columns=["username", "match", "home_pred", "away_pred", "locked"]).to_csv(PREDICTIONS_FILE, index=False)

def load_predictions():
    return pd.read_csv(PREDICTIONS_FILE)

def save_predictions(df):
    df.to_csv(PREDICTIONS_FILE, index=False)

# --- Helper for safe int conversion ---
def to_int(val):
    try:
        return int(float(val))
    except:
        return None

# --- Players ---
players = [
    "Celina",
    "Gerlinde",
    "Oma",
    "Mechthild",
    "Tobias",
    "Sebastian",
    "Ansgar",
    "John"
]

# --- Universal password ---
UNIVERSAL_PASSWORD = "borussia"

# --- Login ---
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    pw = st.text_input("Enter password", type="password")
    if st.button("Login"):
        if pw == UNIVERSAL_PASSWORD:
            st.session_state.authenticated = True
            st.success("Login successful!")
        else:
            st.error("Incorrect password")
    st.stop()

# --- Team name mapping (English → German) ---
team_name_mapping = {
    "Bayern Munich": "Bayern München",
    "FC Cologne": "1. FC Köln",
    "Mainz": "FSV Mainz 05",
    "Hamburg SV": "Hamburger SV",
    # Add more as needed
}

def apply_team_name_mapping(df):
    df["homeTeam"] = df["homeTeam"].map(team_name_mapping).fillna(df["homeTeam"])
    df["awayTeam"] = df["awayTeam"].map(team_name_mapping).fillna(df["awayTeam"])
    return df

# --- Fetch Matches from ESPN ---
def fetch_espn_matches():
    start_date = datetime(2025, 7, 1)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/118.0.5993.0 Safari/537.36"
    }

    # ---- Fetch upcoming fixtures ----
    fixtures_url = "https://www.espn.com/soccer/team/fixtures/_/id/268"
    future_matches = []
    try:
        response = requests.get(fixtures_url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        for row in soup.find_all("tr", class_="Table__TR"):
            home_div = row.find("div", {"data-testid": "localTeam"})
            away_div = row.find("div", {"data-testid": "awayTeam"})
            if not home_div or not away_div:
                continue

            home = home_div.get_text(strip=True)
            away = away_div.get_text(strip=True)

            date_div = row.find("div", {"data-testid": "date"})
            date_text = date_div.text.strip() if date_div else ""
            try:
                date = datetime.strptime(date_text, "%a, %b %d").replace(year=datetime.now().year)
            except:
                date = None

            if date and date < start_date:
                continue

            league_td = row.find_all("td")[-1]
            league = league_td.get_text(strip=True) if league_td else ""
            if "Club Friendly" in league:
                continue

            future_matches.append([date, home, away, None, None, league])

    except Exception as e:
        st.error(f"Error fetching ESPN fixtures: {e}")

    # ---- Fetch past results ----
    results_url = "https://www.espn.com/soccer/team/results/_/id/268/ger.borussia_mgladbach"
    past_matches = []
    try:
        response = requests.get(results_url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        for row in soup.find_all("tr", class_="Table__TR"):
            home_div = row.find("div", {"data-testid": "localTeam"})
            away_div = row.find("div", {"data-testid": "awayTeam"})
            if not home_div or not away_div:
                continue

            home = home_div.get_text(strip=True)
            away = away_div.get_text(strip=True)

            date_div = row.find("div", {"data-testid": "date"})
            date_text = date_div.text.strip() if date_div else ""
            try:
                date = datetime.strptime(date_text, "%a, %b %d").replace(year=datetime.now().year)
            except:
                date = None

            if date and date < start_date:
                continue

            league_td = row.find_all("td")[-1]
            league = league_td.get_text(strip=True) if league_td else ""
            if "Club Friendly" in league:
                continue

            score_link = row.find("a", href=True, text=lambda t: t and " - " in t)
            if score_link:
                try:
                    home_score, away_score = [int(s.strip()) for s in score_link.text.split("-")]
                except:
                    home_score, away_score = None, None
            else:
                home_score, away_score = None, None

            past_matches.append([date, home, away, home_score, away_score, league])

    except Exception as e:
        st.error(f"Error fetching ESPN results: {e}")

    df_past = pd.DataFrame(past_matches, columns=["date", "homeTeam", "awayTeam", "homeScore", "awayScore", "league"])
    df_future = pd.DataFrame(future_matches, columns=["date", "homeTeam", "awayTeam", "homeScore", "awayScore", "league"])

    # Apply German team names
    df_past = apply_team_name_mapping(df_past)
    df_future = apply_team_name_mapping(df_future)

    return df_past.sort_values(by="date"), df_future.sort_values(by="date")

# --- Main App ---
df_past, df_future = fetch_espn_matches()
all_predictions = load_predictions()

# --- Calculate jackpot history ---
jackpot = 40
first_game = True
jackpot_history = []

if not df_past.empty:
    df_past["match"] = df_past.apply(
        lambda r: f"{r['homeTeam']} vs {r['awayTeam']} ({r['date'].date() if pd.notnull(r['date']) else 'TBD'})", axis=1
    )

    for _, row in df_past.iterrows():
        preds = all_predictions[
            (all_predictions["match"] == row["match"]) &
            (all_predictions["locked"] == True)
        ]

        exact_winners = preds[
            (preds["home_pred"].apply(to_int) == row["homeScore"]) &
            (preds["away_pred"].apply(to_int) == row["awayScore"])
        ]["username"].tolist()

        jackpot_history.append(jackpot if first_game else jackpot)
        first_game = False

        if exact_winners:
            jackpot = 8
        else:
            jackpot += 8

    df_past["Winners"] = [
        ", ".join(all_predictions[
            (all_predictions["match"] == row["match"]) &
            (all_predictions["locked"] == True) &
            (all_predictions["home_pred"].apply(to_int) == row["homeScore"]) &
            (all_predictions["away_pred"].apply(to_int) == row["awayScore"])
        ]["username"].tolist())
        for _, row in df_past.iterrows()
    ]
    df_past["Jackpot (€)"] = jackpot_history

# --- Show current jackpot ---
st.subheader("🎰 Current Jackpot")
st.markdown(f"## 💰 {jackpot} €")

# --- Display past games/results with German date format ---
if not df_past.empty:
    st.subheader("Ergebnisse")
    df_past_display = df_past.copy()
    df_past_display["date"] = df_past_display["date"].dt.strftime("%d.%m.%Y")
    st.dataframe(df_past_display[["date", "homeTeam", "awayTeam", "homeScore", "awayScore", "Winners", "Jackpot (€)","league"]])

# --- Display upcoming games with German date format ---
if not df_future.empty:
    st.subheader("Tipps")
    for idx, row in df_future.iterrows():
        date_str = row['date'].strftime("%d.%m.%Y") if pd.notnull(row['date']) else "TBD"
        match_key = f"{row['homeTeam']} vs {row['awayTeam']} ({date_str})"
        st.markdown(f"### {match_key}")

        match_preds = all_predictions[all_predictions['match'] == match_key]
        df_edit = pd.DataFrame({"username": players})
        df_edit["home_pred"] = df_edit["username"].apply(
            lambda u: match_preds[match_preds["username"] == u]["home_pred"].values[0]
            if not match_preds[match_preds["username"] == u].empty else ""
        )
        df_edit["away_pred"] = df_edit["username"].apply(
            lambda u: match_preds[match_preds["username"] == u]["away_pred"].values[0]
            if not match_preds[match_preds["username"] == u].empty else ""
        )

        edited_df = st.data_editor(df_edit, num_rows="fixed", key=f"data_editor_{idx}")

        for i, player in enumerate(players):
            home_val = edited_df.loc[i, "home_pred"]
            away_val = edited_df.loc[i, "away_pred"]

            prev_row = all_predictions[
                (all_predictions["username"] == player) &
                (all_predictions["match"] == match_key)
            ]
            if not prev_row.empty:
                all_predictions.loc[prev_row.index, ["home_pred", "away_pred"]] = [home_val, away_val]
            else:
                all_predictions = pd.concat([
                    all_predictions,
                    pd.DataFrame([[player, match_key, home_val, away_val, False]],
                                 columns=["username", "match", "home_pred", "away_pred", "locked"])
                ], ignore_index=True)

        if st.button("Speichern", key=f"lock_{idx}"):
            all_predictions.loc[all_predictions["match"] == match_key, "locked"] = True
            st.success(f"Tipps für '{match_key}' gespeichert!")

# --- Display past/locked predictions at the bottom ---
locked_preds = all_predictions[all_predictions["locked"] == True]
if not locked_preds.empty:
    st.subheader("Locked / Past Predictions")
    st.dataframe(locked_preds.sort_values(by="match")[["username", "match", "home_pred", "away_pred"]])

# --- Save predictions ---
save_predictions(all_predictions)
st.success("All predictions saved!")
