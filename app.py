import os
import sqlite3
import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

# -----------------------------
# Configuration
# -----------------------------
APP_TITLE = "Family Tipp Game"
DB_PATH = os.getenv("TIPPGAME_DB", "tippgame.db")
TZ = ZoneInfo(os.getenv("TIPPGAME_TIMEZONE", "Europe/Berlin"))
INITIAL_JACKPOT_CENTS = 800
NEXT_GAME_JACKPOT_CENTS = 2400

# Edit these defaults before the first deployment, or use the Admin page after launch.
DEFAULT_SETTINGS = {
    "team_name": "Elche",
    # OpenLigaDB shortcuts. Examples: bl1 (1. Bundesliga), bl2, bl3, dfb.
    "leagues": "es1",
    "family_pin": "1234",
    # OpenLigaDB season: 2026 means 2026/27.
    "season": "2026",
    "admin_pin": "9999",
}
DEFAULT_USERS = [
    ("Person 1", "1234"),
    ("Person 2", "2345"),
    ("Person 3", "3456"),
]


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            pin_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id TEXT NOT NULL UNIQUE,
            league TEXT NOT NULL,
            season INTEGER NOT NULL,
            kickoff_utc TEXT,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_score INTEGER,
            away_score INTEGER,
            status TEXT NOT NULL DEFAULT 'scheduled',
            result_processed INTEGER NOT NULL DEFAULT 0,
            jackpot_paid_cents INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            home_score INTEGER NOT NULL,
            away_score INTEGER NOT NULL,
            saved_at TEXT NOT NULL,
            UNIQUE(match_id, user_id),
            FOREIGN KEY(match_id) REFERENCES matches(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS jackpot (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cents INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            user_id INTEGER,
            amount_cents INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(match_id),
            FOREIGN KEY(match_id) REFERENCES matches(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )
    for key, value in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (key, value))
    # One-time migration: set the requested jackpot for the next game.
    marker = db.execute("SELECT value FROM settings WHERE key='jackpot_v2_initialized'").fetchone()
    if marker is None:
        db.execute("UPDATE jackpot SET cents=? WHERE id=1", (NEXT_GAME_JACKPOT_CENTS,))
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('jackpot_v2_initialized','1')")
    for name, pin in DEFAULT_USERS:
        db.execute(
            "INSERT OR IGNORE INTO users(name,pin_hash) VALUES (?,?)",
            (name, hash_pin(pin)),
        )
    db.execute("INSERT OR IGNORE INTO jackpot(id,cents) VALUES (1,?)", (INITIAL_JACKPOT_CENTS,))
    db.commit()
    db.close()


def setting(key: str) -> str:
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    db.close()
    return row["value"] if row else DEFAULT_SETTINGS[key]


def set_setting(key: str, value: str):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, value))
    db.commit()
    db.close()


def users():
    db = get_db()
    rows = db.execute("SELECT id,name FROM users WHERE active=1 ORDER BY name").fetchall()
    db.close()
    return rows


def authenticate(name: str, pin: str):
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE name=? AND active=1", (name,)).fetchone()
    shared_pin = db.execute("SELECT value FROM settings WHERE key='family_pin'").fetchone()
    db.close()
    if row and shared_pin and hash_pin(pin) == hash_pin(shared_pin["value"]):
        return dict(row)
    return None


def fetch_openligadb(shortcut: str, season: int, team_filter: str):
    url = f"https://api.openligadb.de/getmatchdata/{shortcut}/{season}/{requests.utils.quote(team_filter)}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()


def parse_openligadb_match(raw, league, season):
    kickoff = raw.get("matchDateTimeUTC") or raw.get("matchDateTime")
    home = raw.get("team1", {}).get("teamName", "")
    away = raw.get("team2", {}).get("teamName", "")
    results = raw.get("matchResults") or []
    final = None
    for result in results:
        # OpenLigaDB's result types include After90Minutes / AfterExtraTime / AfterPenalties.
        if result.get("resultTypeId") in (2, 3, 4) or result.get("resultType", {}).get("name") in (
            "Endergebnis", "End Result", "After90Minutes", "AfterExtraTime", "AfterPenalties"
        ):
            final = result
    if final is None and results:
        # The last result is normally the current/final score.
        final = results[-1]
    hs = final.get("pointsTeam1") if final else None
    as_ = final.get("pointsTeam2") if final else None
    finished = raw.get("matchIsFinished", False)
    return {
        "provider_id": f"{league}:{raw.get('matchID')}",
        "league": league,
        "season": season,
        "kickoff_utc": kickoff,
        "home_team": home,
        "away_team": away,
        "home_score": hs if finished else None,
        "away_score": as_ if finished else None,
        "status": "final" if finished else "scheduled",
    }


def sync_matches():
    team = setting("team_name").strip()
    leagues = [x.strip() for x in setting("leagues").split(",") if x.strip()]
    season = int(setting("season"))
    if not team or team == "YOUR TEAM":
        raise ValueError("Set the team name in Admin first.")
    if not leagues:
        raise ValueError("Set at least one OpenLigaDB league shortcut in Admin.")

    fetched = []
    errors = []
    for league in leagues:
        try:
            data = fetch_openligadb(league, season, team)
            fetched.extend(parse_openligadb_match(x, league, season) for x in data)
        except Exception as exc:
            errors.append(f"{league}: {exc}")

    db = get_db()
    for m in fetched:
        db.execute(
            """
            INSERT INTO matches(provider_id,league,season,kickoff_utc,home_team,away_team,home_score,away_score,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(provider_id) DO UPDATE SET
              kickoff_utc=excluded.kickoff_utc,
              home_team=excluded.home_team,
              away_team=excluded.away_team,
              home_score=excluded.home_score,
              away_score=excluded.away_score,
              status=excluded.status,
              updated_at=excluded.updated_at
            """,
            (
                m["provider_id"], m["league"], m["season"], m["kickoff_utc"],
                m["home_team"], m["away_team"], m["home_score"], m["away_score"],
                m["status"], now_iso(), now_iso(),
            ),
        )
    db.commit()
    db.close()
    process_finished_matches()
    return len(fetched), errors


def process_finished_matches():
    db = get_db()
    matches = db.execute("SELECT * FROM matches WHERE status='final' AND result_processed=0").fetchall()
    for m in matches:
        preds = db.execute(
            "SELECT p.*, u.name FROM predictions p JOIN users u ON u.id=p.user_id WHERE p.match_id=?",
            (m["id"],),
        ).fetchall()
        winners = [p for p in preds if p["home_score"] == m["home_score"] and p["away_score"] == m["away_score"]]
        jackpot = db.execute("SELECT cents FROM jackpot WHERE id=1").fetchone()["cents"]

        # A single exact-score winner gets the whole jackpot. If multiple people
        # have the exact same score, they split it evenly (integer cents).
        if winners:
            share = jackpot // len(winners)
            db.execute("UPDATE jackpot SET cents=? WHERE id=1", (INITIAL_JACKPOT_CENTS,))
            db.execute(
                "UPDATE matches SET result_processed=1,jackpot_paid_cents=?,updated_at=? WHERE id=?",
                (jackpot, now_iso(), m["id"]),
            )
            for p in winners:
                db.execute(
                    "INSERT OR IGNORE INTO payouts(match_id,user_id,amount_cents,created_at) VALUES (?,?,?,?)",
                    (m["id"], p["user_id"], share, now_iso()),
                )
        else:
            new_jackpot = jackpot + INITIAL_JACKPOT_CENTS
            db.execute("UPDATE jackpot SET cents=? WHERE id=1", (new_jackpot,))
            db.execute(
                "UPDATE matches SET result_processed=1,jackpot_paid_cents=0,updated_at=? WHERE id=?",
                (now_iso(), m["id"]),
            )
    db.commit()
    db.close()


def save_prediction(match_id, user_id, hs, as_):
    db = get_db()
    db.execute(
        """
        INSERT INTO predictions(match_id,user_id,home_score,away_score,saved_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(match_id,user_id) DO UPDATE SET
          home_score=excluded.home_score,
          away_score=excluded.away_score,
          saved_at=excluded.saved_at
        """,
        (match_id, user_id, hs, as_, now_iso()),
    )
    db.commit()
    db.close()


def get_matches():
    db = get_db()
    rows = db.execute("SELECT * FROM matches ORDER BY kickoff_utc").fetchall()
    db.close()
    return rows


def prediction_map(match_id):
    db = get_db()
    rows = db.execute(
        "SELECT p.*,u.name FROM predictions p JOIN users u ON u.id=p.user_id WHERE p.match_id=?",
        (match_id,),
    ).fetchall()
    db.close()
    return {r["name"]: r for r in rows}


def jackpot():
    db = get_db()
    value = db.execute("SELECT cents FROM jackpot WHERE id=1").fetchone()["cents"]
    db.close()
    return value


def format_dt(value):
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return value


def format_score(hs, as_):
    return "–" if hs is None or as_ is None else f"{hs}:{as_}"


st.set_page_config(page_title=APP_TITLE, page_icon="⚽", layout="wide")
init_db()

# Best-effort automatic result/fixture update on each session. The button below can force it.
try:
    sync_matches()
except Exception as exc:
    st.session_state["sync_error"] = str(exc)

st.title("⚽ Family Tipp Game")
st.caption(f"{setting('team_name')} · Jackpot: **€{jackpot()/100:.2f}**")

with st.sidebar:
    st.header("Login")
    active_users = users()
    names = [u["name"] for u in active_users]
    if "user" not in st.session_state:
        st.session_state["user"] = None
    selected = st.selectbox("Name", names if names else ["No users configured"])
    pin = st.text_input("Family PIN", type="password", help="The same PIN is used by everyone.")
    if st.button("Log in", use_container_width=True):
        user = authenticate(selected, pin)
        if user:
            st.session_state["user"] = user
            st.success("Logged in")
        else:
            st.error("Wrong PIN")
    if st.session_state.get("user"):
        st.info(f"Logged in as **{st.session_state['user']['name']}**")
        if st.button("Log out", use_container_width=True):
            st.session_state["user"] = None
            st.rerun()

    st.divider()
    if st.button("🔄 Update games/results", use_container_width=True):
        try:
            count, errors = sync_matches()
            st.success(f"Updated {count} games.")
            for e in errors:
                st.warning(e)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

    st.divider()
    admin_pin = st.text_input("Admin PIN", type="password", key="admin_pin")
    is_admin = admin_pin == setting("admin_pin")

matches = get_matches()
if st.session_state.get("sync_error") and not matches:
    st.warning(f"Game sync is not configured yet: {st.session_state['sync_error']}")

# -----------------------------
# Main game list
# -----------------------------
if not matches:
    st.info("No games have been imported yet. Configure the team and league in Admin.")
else:
    upcoming = [m for m in matches if m["status"] != "final"]
    finished = [m for m in matches if m["status"] == "final"]

    tab_upcoming, tab_finished = st.tabs([f"Upcoming ({len(upcoming)})", f"Completed ({len(finished)})"])

    with tab_upcoming:
        for m in upcoming:
            preds = prediction_map(m["id"])
            st.subheader(f"{m['home_team']} – {m['away_team']}")
            st.caption(f"{format_dt(m['kickoff_utc'])} · {m['league']}")
            data = []
            for u in active_users:
                p = preds.get(u["name"])
                data.append({
                    "Name": u["name"],
                    "Prediction": format_score(p["home_score"], p["away_score"]) if p else "—",
                    "Saved": format_dt(p["saved_at"]) if p else "—",
                })
            st.dataframe(pd.DataFrame(data), hide_index=True, use_container_width=True)

            st.markdown("**Your prediction**")
            if st.session_state.get("user"):
                me = st.session_state["user"]
                existing = preds.get(me["name"])
                c1, c2, c3 = st.columns([1, 1, 1])
                with c1:
                    hs = st.number_input(
                        "Home goals", min_value=0, max_value=30,
                        value=int(existing["home_score"]) if existing else 0,
                        key=f"h_{m['id']}"
                    )
                with c2:
                    aas = st.number_input(
                        "Away goals", min_value=0, max_value=30,
                        value=int(existing["away_score"]) if existing else 0,
                        key=f"a_{m['id']}"
                    )
                with c3:
                    st.write("")
                    st.write("")
                    button_text = "Change prediction" if existing else "Save prediction"
                    if st.button(button_text, key=f"save_{m['id']}", type="primary"):
                        save_prediction(m["id"], me["id"], int(hs), int(aas))
                        st.success(f"Prediction saved: {hs}:{aas}")
                        st.rerun()
            else:
                st.info("To enter your prediction, select your name and enter your PIN in the **Login** section in the sidebar. After logging in, the prediction fields will appear here.")
            st.divider()

    with tab_finished:
        for m in reversed(finished):
            preds = prediction_map(m["id"])
            st.subheader(f"{m['home_team']} {m['home_score']}:{m['away_score']} {m['away_team']}")
            payout_rows = []
            db = get_db()
            payouts = db.execute(
                "SELECT p.amount_cents,u.name FROM payouts p JOIN users u ON u.id=p.user_id WHERE p.match_id=?",
                (m["id"],),
            ).fetchall()
            db.close()
            winners = {p["name"]: p["amount_cents"] for p in payouts}
            for u in active_users:
                p = preds.get(u["name"])
                payout_rows.append({
                    "Name": u["name"],
                    "Prediction": format_score(p["home_score"], p["away_score"]) if p else "—",
                    "Result": f"🏆 €{winners[u['name']]/100:.2f}" if u["name"] in winners else "",
                })
            st.dataframe(pd.DataFrame(payout_rows), hide_index=True, use_container_width=True)
            if winners:
                st.success("Winner: " + ", ".join(f"{n} (€{v/100:.2f})" for n, v in winners.items()))
            else:
                st.caption("No exact-score winner. The jackpot carried over.")
            st.divider()

# -----------------------------
# Admin
# -----------------------------
if is_admin:
    st.header("Admin")
    st.caption("Admin changes are stored in the database. The family only needs one shared PIN; there are no individual accounts.")
    with st.form("settings"):
        team = st.text_input("Team name", value=setting("team_name"))
        leagues = st.text_input("OpenLigaDB league shortcuts (comma-separated)", value=setting("leagues"))
        season = st.text_input("Season", value=setting("season"), help="2026 means the 2026/27 season.")
        family_pin = st.text_input("Family PIN", value=setting("family_pin"), type="password")
        new_admin_pin = st.text_input("Admin PIN", value=setting("admin_pin"), type="password")
        if st.form_submit_button("Save settings"):
            set_setting("team_name", team.strip())
            set_setting("leagues", leagues.strip())
            set_setting("season", season.strip())
            set_setting("family_pin", family_pin)
            set_setting("admin_pin", new_admin_pin)
            st.success("Settings saved. Click Update games/results.")
            st.rerun()

    st.subheader("Family members")
    db = get_db()
    existing_users = db.execute("SELECT id,name,active FROM users ORDER BY name").fetchall()
    db.close()
    for u in existing_users:
        c1, c2, c3 = st.columns([2, 1, 1])
        c1.write(u["name"])
        if c2.button("Deactivate" if u["active"] else "Activate", key=f"act_{u['id']}"):
            db = get_db()
            db.execute("UPDATE users SET active=? WHERE id=?", (0 if u["active"] else 1, u["id"]))
            db.commit(); db.close(); st.rerun()
    with st.form("new_user"):
        name = st.text_input("New name")
        if st.form_submit_button("Add family member"):
            if not name.strip():
                st.error("Enter a name.")
            else:
                try:
                    db = get_db()
                    db.execute("INSERT INTO users(name,pin_hash) VALUES (?,?)", (name.strip(), hash_pin(setting("family_pin"))))
                    db.commit(); db.close()
                    st.success("Added.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("That name already exists.")

    st.subheader("Manual match import")
    st.caption("Useful for testing the app before configuring OpenLigaDB or for competitions not covered by the selected league.")
    with st.form("manual_match"):
        home = st.text_input("Home team")
        away = st.text_input("Away team")
        kickoff = st.text_input("Kickoff UTC", value=datetime.now(timezone.utc).isoformat())
        if st.form_submit_button("Add test match"):
            db = get_db()
            db.execute(
                "INSERT OR IGNORE INTO matches(provider_id,league,season,kickoff_utc,home_team,away_team,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"manual:{home}:{away}:{kickoff}", "manual", int(setting("season")), kickoff, home, away, "scheduled", now_iso(), now_iso()),
            )
            db.commit(); db.close(); st.success("Match added."); st.rerun()

st.caption("Results and fixtures are supplied by OpenLigaDB. The API is free and does not require authentication.")
