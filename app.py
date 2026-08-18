"""
Bot Google Form — prospection
Envoi séquentiel avec anti-doublon, historique JSON, arrêt possible.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from pathlib import Path

import requests
import streamlit as st

# --- Config formulaire ---
FORM_ID = "1FAIpQLSeu2db441waSJVxcePzPTBbmyHBdJUGRU7debGCJwD4rlZh7w"
FORM_RESPONSE_URL = f"https://docs.google.com/forms/d/e/{FORM_ID}/formResponse"

ENTRY_COMMERCIAL = "entry.2005620554"
ENTRY_DATE = "entry.1065046570"
ENTRY_CLIENT = "entry.1166974658"

HISTORY_FILE = Path(__file__).resolve().parent / "historique_envois.json"
DELAY_SECONDS = 20

PHONE_RE = re.compile(r"^(?:0|\+?225)?([0-9]{8,10})$")


def normalize_phone(raw: str) -> str | None:
    """Normalise vers 0XXXXXXXXX (10 chiffres CI typiques)."""
    s = re.sub(r"[\s\-\.]", "", raw.strip())
    if not s:
        return None
    m = PHONE_RE.match(s)
    if not m:
        digits = re.sub(r"\D", "", s)
        if len(digits) == 10 and digits.startswith("0"):
            return digits
        if len(digits) == 9:
            return "0" + digits
        if len(digits) == 13 and digits.startswith("225"):
            return "0" + digits[3:]
        if len(digits) == 12 and digits.startswith("225"):
            return "0" + digits[3:]
        return None
    local = m.group(1)
    if len(local) == 9:
        return "0" + local
    if len(local) == 10 and local.startswith("0"):
        return local
    if len(local) == 8:
        return "0" + local
    return None


def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_history(rows: list[dict]) -> None:
    HISTORY_FILE.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def successful_client_numbers(history: list[dict]) -> set[str]:
    return {
        str(h.get("client", "")).strip()
        for h in history
        if h.get("status") == "success" and h.get("client")
    }


def submit_form(commercial: str, client: str, prospection_date: str) -> tuple[bool, str]:
    payload = {
        ENTRY_COMMERCIAL: commercial,
        ENTRY_DATE: prospection_date,
        ENTRY_CLIENT: client,
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12; Mobile) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        ),
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://docs.google.com",
        "Referer": f"https://docs.google.com/forms/d/e/{FORM_ID}/viewform",
    }
    try:
        r = requests.post(
            FORM_RESPONSE_URL,
            data=payload,
            headers=headers,
            timeout=30,
            allow_redirects=True,
        )
        # Google renvoie souvent 200 même après succès
        if r.status_code in (200, 302):
            return True, f"HTTP {r.status_code}"
        return False, f"HTTP {r.status_code}"
    except requests.RequestException as e:
        return False, str(e)


def parse_client_list(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        n = normalize_phone(line)
        if not n:
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


# --- UI ---
st.set_page_config(
    page_title="Bot Formulaire Prospection",
    page_icon="📋",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
    .block-container { padding-top: 1.2rem; max-width: 720px; }
    textarea { font-size: 16px !important; } /* mobile */
    input { font-size: 16px !important; }
</style>
""",
    unsafe_allow_html=True,
)

st.title("📋 Bot Google Form")
st.caption("Prospection — 1 envoi / 20 s · anti-doublon · historique local")

if "running" not in st.session_state:
    st.session_state.running = False
if "stop_requested" not in st.session_state:
    st.session_state.stop_requested = False
if "last_run_log" not in st.session_state:
    st.session_state.last_run_log = []

history = load_history()
already = successful_client_numbers(history)

with st.form("config_form", clear_on_submit=False):
    commercial_raw = st.text_input(
        "Numéro commercial",
        value="07",
        help="Format 07XXXXXXXX — utilisé pour tous les clients de la liste",
        placeholder="0700000000",
    )
    clients_text = st.text_area(
        "Numéros clients (un par ligne)",
        height=180,
        placeholder="0701111111\n0702222222\n0703333333",
        help="Les numéros déjà envoyés avec succès seront ignorés",
    )
    today_str = date.today().isoformat()
    st.info(f"📅 Date prospection (auto) : **{today_str}**")
    start = st.form_submit_button("▶ Démarrer les envois", type="primary", use_container_width=True)

col_stop, col_clear = st.columns(2)
with col_stop:
    if st.button("⏹ Arrêter", use_container_width=True, disabled=not st.session_state.running):
        st.session_state.stop_requested = True
        st.warning("Arrêt demandé… fin de l’envoi en cours puis stop.")
with col_clear:
    if st.button("🗑 Vider l’historique local", use_container_width=True):
        save_history([])
        st.success("Historique vidé.")
        st.rerun()

if start:
    commercial = normalize_phone(commercial_raw)
    if not commercial:
        st.error("Numéro commercial invalide (ex. 0708091011).")
    else:
        clients = parse_client_list(clients_text)
        if not clients:
            st.error("Aucun numéro client valide dans la liste.")
        else:
            to_send = [c for c in clients if c not in already]
            skipped = [c for c in clients if c in already]

            st.write(f"**Commercial :** `{commercial}`")
            st.write(f"**À envoyer :** {len(to_send)} · **Ignorés (déjà OK) :** {len(skipped)}")
            if skipped:
                with st.expander("Numéros ignorés (anti-doublon)"):
                    st.code("\n".join(skipped))

            if not to_send:
                st.success("Rien à envoyer — tous les numéros sont déjà dans l’historique succès.")
            else:
                st.session_state.running = True
                st.session_state.stop_requested = False
                progress = st.progress(0.0, text="Démarrage…")
                status = st.empty()
                log_box = st.empty()
                run_log: list[str] = []
                hist = load_history()

                for i, client in enumerate(to_send):
                    if st.session_state.stop_requested:
                        run_log.append(f"⏹ Arrêt demandé avant {client}")
                        break

                    status.info(f"Envoi {i + 1}/{len(to_send)} → client `{client}` …")
                    ok, detail = submit_form(commercial, client, today_str)
                    entry = {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "commercial": commercial,
                        "client": client,
                        "date": today_str,
                        "status": "success" if ok else "error",
                        "detail": detail,
                    }
                    hist.append(entry)
                    save_history(hist)
                    if ok:
                        already.add(client)
                        run_log.append(f"✅ {client} — {detail}")
                    else:
                        run_log.append(f"❌ {client} — {detail}")

                    progress.progress((i + 1) / len(to_send), text=f"{i + 1}/{len(to_send)}")
                    log_box.code("\n".join(run_log[-30:]))

                    # pause 20 s sauf après le dernier ou si arrêt
                    if i < len(to_send) - 1 and not st.session_state.stop_requested:
                        for sec in range(DELAY_SECONDS, 0, -1):
                            if st.session_state.stop_requested:
                                break
                            status.warning(f"⏳ Prochain envoi dans {sec} s… (Arrêter possible)")
                            time.sleep(1)

                st.session_state.running = False
                st.session_state.last_run_log = run_log
                status.success("Terminé." if not st.session_state.stop_requested else "Arrêté.")
                st.balloons()

st.divider()
st.subheader("Historique local")
history = load_history()
st.caption(f"{len(history)} ligne(s) · fichier `{HISTORY_FILE.name}`")
if history:
    # résumé succès
    ok_n = sum(1 for h in history if h.get("status") == "success")
    err_n = len(history) - ok_n
    st.write(f"Succès : **{ok_n}** · Erreurs : **{err_n}** · Clients uniques OK : **{len(successful_client_numbers(history))}**")
    st.dataframe(list(reversed(history[-100:])), use_container_width=True)
else:
    st.info("Aucun envoi enregistré pour le moment.")

st.caption("Utilisable sur téléphone via Streamlit Cloud · délai fixe 20 s entre chaque POST formResponse")
