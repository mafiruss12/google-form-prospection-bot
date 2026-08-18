"""
Bot Google Form — prospection
Envoi séquentiel, anti-doublon, historique JSON, garde-fous anti-blocage Google.
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import streamlit as st

# --- Config formulaire ---
FORM_ID = "1FAIpQLSeu2db441waSJVxcePzPTBbmyHBdJUGRU7debGCJwD4rlZh7w"
FORM_VIEW_URL = f"https://docs.google.com/forms/d/e/{FORM_ID}/viewform"
FORM_RESPONSE_URL = f"https://docs.google.com/forms/d/e/{FORM_ID}/formResponse"

ENTRY_COMMERCIAL = "entry.2005620554"
ENTRY_DATE = "entry.1065046570"
ENTRY_CLIENT = "entry.1166974658"

HISTORY_FILE = Path(__file__).resolve().parent / "historique_envois.json"
STATE_FILE = Path(__file__).resolve().parent / "bot_state.json"

# --- Garde-fous (réduire risque de blocage Google) ---
DEFAULT_DELAY_MIN = 22
DEFAULT_DELAY_MAX = 38
MAX_PER_HOUR = 40
MAX_PER_DAY = 200
MAX_CONSECUTIVE_ERRORS = 3
BACKOFF_BASE_SEC = 60

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
]

PHONE_RE = re.compile(r"^(?:0|\+?225)?([0-9]{8,10})$")

BLOCK_MARKERS = (
    "unusual traffic",
    "detected unusual",
    "captcha",
    "recaptcha",
    "sorry, we have detected",
    "automated queries",
    "our systems have detected",
)


def normalize_phone(raw: str) -> str | None:
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


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"pause_until": None, "consec_errors": 0}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"pause_until": None, "consec_errors": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def successful_client_numbers(history: list[dict]) -> set[str]:
    return {
        str(h.get("client", "")).strip()
        for h in history
        if h.get("status") == "success" and h.get("client")
    }


def count_recent_success(history: list[dict], hours: float | None = None, days: float | None = None) -> int:
    now = datetime.now()
    n = 0
    for h in history:
        if h.get("status") != "success":
            continue
        try:
            ts = datetime.fromisoformat(str(h.get("ts", "")))
        except ValueError:
            continue
        if hours is not None and now - ts > timedelta(hours=hours):
            continue
        if days is not None and now - ts > timedelta(days=days):
            continue
        if hours is None and days is None:
            n += 1
            continue
        n += 1
    return n


def looks_blocked(response: requests.Response) -> bool:
    text = (response.text or "").lower()
    if any(m in text for m in BLOCK_MARKERS):
        return True
    if response.status_code in (403, 429, 503):
        return True
    return False


def build_session() -> requests.Session:
    session = requests.Session()
    ua = random.choice(USER_AGENTS)
    session.headers.update(
        {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
    )
    # Warm-up : ouvrir le formulaire pour cookies / session réaliste
    try:
        session.get(FORM_VIEW_URL, timeout=30)
        time.sleep(random.uniform(1.2, 2.8))
    except requests.RequestException:
        pass
    return session


def submit_form(
    session: requests.Session,
    commercial: str,
    client: str,
    prospection_date: str,
) -> tuple[bool, str, bool]:
    """
    Returns (ok, detail, blocked)
    """
    payload = {
        ENTRY_COMMERCIAL: commercial,
        ENTRY_DATE: prospection_date,
        ENTRY_CLIENT: client,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://docs.google.com",
        "Referer": FORM_VIEW_URL,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }
    try:
        r = session.post(
            FORM_RESPONSE_URL,
            data=payload,
            headers=headers,
            timeout=30,
            allow_redirects=True,
        )
        if looks_blocked(r):
            return False, f"Possible blocage Google (HTTP {r.status_code})", True
        if r.status_code in (200, 302):
            return True, f"HTTP {r.status_code}", False
        return False, f"HTTP {r.status_code}", r.status_code in (403, 429, 503)
    except requests.RequestException as e:
        return False, str(e), False


def parse_client_list(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        n = normalize_phone(line)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def sleep_interruptible(seconds: float, stop_flag_key: str = "stop_requested") -> bool:
    """Sleep by 1s steps. Returns True if stop requested."""
    end = time.time() + seconds
    while time.time() < end:
        if st.session_state.get(stop_flag_key):
            return True
        time.sleep(min(1.0, end - time.time()))
    return bool(st.session_state.get(stop_flag_key))


# --- UI ---
st.set_page_config(
    page_title="Bot Formulaire Prospection",
    page_icon="📋",
    layout="centered",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
    .block-container { padding-top: 1.2rem; max-width: 720px; }
    textarea { font-size: 16px !important; }
    input { font-size: 16px !important; }
</style>
""",
    unsafe_allow_html=True,
)

st.title("📋 Bot Google Form")
st.caption("Prospection · anti-doublon · délais aléatoires · limites horaires · anti-blocage")

if "running" not in st.session_state:
    st.session_state.running = False
if "stop_requested" not in st.session_state:
    st.session_state.stop_requested = False
if "last_run_log" not in st.session_state:
    st.session_state.last_run_log = []

history = load_history()
already = successful_client_numbers(history)
bot_state = load_state()

with st.sidebar:
    st.header("🛡️ Sécurité / rythme")
    delay_min = st.slider("Délai min (s)", 15, 60, DEFAULT_DELAY_MIN)
    delay_max = st.slider("Délai max (s)", 20, 90, DEFAULT_DELAY_MAX)
    if delay_max < delay_min:
        delay_max = delay_min
    max_hour = st.number_input("Max succès / heure", 5, 120, MAX_PER_HOUR)
    max_day = st.number_input("Max succès / jour", 10, 500, MAX_PER_DAY)
    st.markdown(
        """
**Règles anti-blocage**
- Session + cookies (ouverture du form avant POST)
- User-Agent aléatoire
- Délai **aléatoire** entre envois
- Pause auto si erreurs / soupçon de blocage
- Plafonds horaires et journaliers
- Anti-doublon (jamais 2× le même client en succès)
        """
    )
    if bot_state.get("pause_until"):
        st.warning(f"Pause bot jusqu’à : {bot_state['pause_until']}")
        if st.button("Lever la pause"):
            bot_state["pause_until"] = None
            bot_state["consec_errors"] = 0
            save_state(bot_state)
            st.rerun()

with st.form("config_form", clear_on_submit=False):
    commercial_raw = st.text_input(
        "Numéro commercial",
        value="07",
        help="Format 07XXXXXXXX",
        placeholder="0700000000",
    )
    clients_text = st.text_area(
        "Numéros clients (un par ligne)",
        height=180,
        placeholder="0701111111\n0702222222\n0703333333",
    )
    today_str = date.today().isoformat()
    st.info(f"📅 Date prospection (auto) : **{today_str}**")
    start = st.form_submit_button("▶ Démarrer les envois", type="primary", use_container_width=True)

col_stop, col_clear = st.columns(2)
with col_stop:
    if st.button("⏹ Arrêter", use_container_width=True, disabled=not st.session_state.running):
        st.session_state.stop_requested = True
        st.warning("Arrêt demandé…")
with col_clear:
    if st.button("🗑 Vider l’historique local", use_container_width=True):
        save_history([])
        st.success("Historique vidé.")
        st.rerun()

# Limites actuelles
ok_hour = count_recent_success(history, hours=1)
ok_day = count_recent_success(history, days=1)
st.write(
    f"Quota actuel — **heure :** {ok_hour}/{int(max_hour)} · **jour :** {ok_day}/{int(max_day)} · "
    f"**clients déjà OK :** {len(already)}"
)

if start:
    # Pause globale ?
    if bot_state.get("pause_until"):
        try:
            until = datetime.fromisoformat(bot_state["pause_until"])
            if datetime.now() < until:
                st.error(f"Bot en pause de sécurité jusqu’à {until.strftime('%H:%M:%S')}. Lever la pause dans la barre latérale si besoin.")
                st.stop()
            else:
                bot_state["pause_until"] = None
                bot_state["consec_errors"] = 0
                save_state(bot_state)
        except ValueError:
            pass

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
            remaining_hour = max(0, int(max_hour) - ok_hour)
            remaining_day = max(0, int(max_day) - ok_day)
            quota = min(remaining_hour, remaining_day, len(to_send))
            deferred = to_send[quota:]
            to_send = to_send[:quota]

            st.write(f"**Commercial :** `{commercial}`")
            st.write(
                f"**À envoyer maintenant :** {len(to_send)} · **Ignorés doublon :** {len(skipped)} · "
                f"**Reportés (quota) :** {len(deferred)}"
            )
            if skipped:
                with st.expander("Numéros ignorés (anti-doublon)"):
                    st.code("\n".join(skipped))
            if deferred:
                with st.expander("Reportés (plafond heure/jour)"):
                    st.code("\n".join(deferred))
                    st.caption("Relance plus tard — les plafonds protègent contre un blocage Google.")

            if not to_send:
                st.success("Rien à envoyer (doublons et/ou quota atteint).")
            else:
                st.session_state.running = True
                st.session_state.stop_requested = False
                progress = st.progress(0.0, text="Préparation session…")
                status = st.empty()
                log_box = st.empty()
                run_log: list[str] = []
                hist = load_history()
                consec = int(bot_state.get("consec_errors") or 0)

                status.info("Ouverture du formulaire (cookies / session)…")
                session = build_session()
                progress.progress(0.02, text="Session prête")

                for i, client in enumerate(to_send):
                    if st.session_state.stop_requested:
                        run_log.append(f"⏹ Arrêt avant {client}")
                        break

                    status.info(f"Envoi {i + 1}/{len(to_send)} → `{client}` …")
                    ok, detail, blocked = submit_form(session, commercial, client, today_str)
                    entry = {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "commercial": commercial,
                        "client": client,
                        "date": today_str,
                        "status": "success" if ok else "error",
                        "detail": detail,
                        "blocked": blocked,
                    }
                    hist.append(entry)
                    save_history(hist)

                    if ok:
                        already.add(client)
                        consec = 0
                        run_log.append(f"✅ {client} — {detail}")
                    else:
                        consec += 1
                        run_log.append(f"❌ {client} — {detail}")

                    bot_state["consec_errors"] = consec
                    save_state(bot_state)

                    progress.progress((i + 1) / len(to_send), text=f"{i + 1}/{len(to_send)}")
                    log_box.code("\n".join(run_log[-40:]))

                    if blocked or consec >= MAX_CONSECUTIVE_ERRORS:
                        pause_sec = BACKOFF_BASE_SEC * (2 ** min(consec, 4)) + random.randint(10, 40)
                        until = datetime.now() + timedelta(seconds=pause_sec)
                        bot_state["pause_until"] = until.isoformat(timespec="seconds")
                        save_state(bot_state)
                        run_log.append(
                            f"🛡️ Pause sécurité {int(pause_sec)}s (erreurs/blocage). Reprendre après {until.strftime('%H:%M:%S')}."
                        )
                        log_box.code("\n".join(run_log[-40:]))
                        status.error("Pause anti-blocage activée — envois interrompus.")
                        break

                    if i < len(to_send) - 1 and not st.session_state.stop_requested:
                        wait = random.uniform(float(delay_min), float(delay_max))
                        # micro-jitter
                        wait += random.uniform(0.3, 1.7)
                        status.warning(f"⏳ Prochain envoi dans ~{int(wait)} s…")
                        if sleep_interruptible(wait):
                            run_log.append("⏹ Arrêt pendant l’attente")
                            break
                        # périodiquement rafraîchir la page form (cookies)
                        if (i + 1) % 8 == 0:
                            try:
                                session.get(FORM_VIEW_URL, timeout=20)
                                time.sleep(random.uniform(0.8, 1.5))
                            except requests.RequestException:
                                pass

                st.session_state.running = False
                st.session_state.last_run_log = run_log
                if not st.session_state.stop_requested and consec < MAX_CONSECUTIVE_ERRORS:
                    status.success("Terminé.")
                st.session_state.stop_requested = False

st.divider()
st.subheader("Historique local")
history = load_history()
st.caption(f"{len(history)} ligne(s) · `{HISTORY_FILE.name}`")
if history:
    ok_n = sum(1 for h in history if h.get("status") == "success")
    err_n = len(history) - ok_n
    st.write(
        f"Succès : **{ok_n}** · Erreurs : **{err_n}** · Clients uniques OK : **{len(successful_client_numbers(history))}**"
    )
    st.dataframe(list(reversed(history[-100:])), use_container_width=True)
else:
    st.info("Aucun envoi enregistré pour le moment.")

st.caption(
    "Les délais aléatoires et plafonds réduisent le risque de blocage ; "
    "ils ne garantissent pas l’absence de captcha côté Google."
)
