"""
Bot Google Form — prospection
Anti-doublon, historique, anti-blocage, liens « Modifier votre réponse ».
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import streamlit as st

FORM_ID = "1FAIpQLSeu2db441waSJVxcePzPTBbmyHBdJUGRU7debGCJwD4rlZh7w"
FORM_VIEW_URL = f"https://docs.google.com/forms/d/e/{FORM_ID}/viewform"
FORM_RESPONSE_URL = f"https://docs.google.com/forms/d/e/{FORM_ID}/formResponse"

ENTRY_COMMERCIAL = "entry.2005620554"
ENTRY_DATE = "entry.1065046570"
ENTRY_CLIENT = "entry.1166974658"

HISTORY_FILE = Path(__file__).resolve().parent / "historique_envois.json"
STATE_FILE = Path(__file__).resolve().parent / "bot_state.json"

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
EDIT2_RE = re.compile(
    r"(?:edit2=|/formResponse\?[^\"'\s]*edit2=)([A-Za-z0-9_\-\.]+)",
    re.I,
)
EDIT_HREF_RE = re.compile(
    r'href=["\'](https://docs\.google\.com/forms/[^"\']*edit2=[^"\']+)["\']',
    re.I,
)

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
        n += 1
    return n


def looks_blocked(response: requests.Response) -> bool:
    text = (response.text or "").lower()
    if any(m in text for m in BLOCK_MARKERS):
        return True
    if response.status_code in (403, 429, 503):
        return True
    return False


def extract_edit_link(response: requests.Response) -> tuple[str | None, str | None]:
    """
    Récupère le token edit2 depuis la page « Merci » (lien Edit your response / Modifier).
    """
    text = (response.text or "").replace("&amp;", "&")
    final_url = (response.url or "").replace("&amp;", "&")

    for candidate in (final_url, text):
        if "edit2=" not in candidate:
            continue
        # tous les tokens edit2=
        found = re.findall(r"edit2=([A-Za-z0-9_\-\.]+)", candidate)
        for tok in found:
            if len(tok) >= 10:
                edit_url = f"{FORM_VIEW_URL}?usp=form_confirm&edit2={tok}"
                return tok, edit_url

    # Fallback : URL viewform complète avec edit2
    pat = (
        r"https://docs\.google\.com/forms/d/e/"
        + re.escape(FORM_ID)
        + r"/viewform\?[^\s\"\'>]*edit2=([A-Za-z0-9_\-\.]+)"
    )
    m = re.search(pat, text)
    if m:
        tok = m.group(1)
        return tok, f"{FORM_VIEW_URL}?usp=form_confirm&edit2={tok}"

    return None, None


def extract_fbzx(html: str) -> str | None:
    for pat in [
        r'name="fbzx"\s+value="([^"]+)"',
        r'name="fbzx"\s+value=\"([^"]+)\"',
        r'["\']fbzx["\']\s*[:=]\s*["\']?(-?\d+)',
    ]:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def build_session() -> tuple[requests.Session, str | None]:
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
    fbzx = None
    try:
        r = session.get(FORM_VIEW_URL, timeout=30)
        fbzx = extract_fbzx(r.text or "")
        time.sleep(random.uniform(1.0, 2.2))
    except requests.RequestException:
        pass
    return session, fbzx


def submit_form(
    session: requests.Session,
    commercial: str,
    client: str,
    prospection_date: str,
    edit2: str | None = None,
    fbzx: str | None = None,
) -> tuple[bool, str, bool, str | None, str | None]:
    """
    Returns (ok, detail, blocked, edit2_token, edit_url)
    Si edit2 est fourni → modification d’une réponse existante.
    """
    payload = {
        ENTRY_COMMERCIAL: commercial,
        ENTRY_DATE: prospection_date,
        ENTRY_CLIENT: client,
        "fvv": "1",
        "pageHistory": "0",
    }
    if fbzx:
        payload["fbzx"] = fbzx
    if edit2:
        payload["edit2"] = edit2

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://docs.google.com",
        "Referer": f"{FORM_VIEW_URL}?edit2={edit2}" if edit2 else FORM_VIEW_URL,
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
            return False, f"Possible blocage Google (HTTP {r.status_code})", True, None, None

        tok, edit_url = extract_edit_link(r)
        if edit2 and not tok:
            tok = edit2
            edit_url = f"{FORM_VIEW_URL}?edit2={edit2}"

        if r.status_code in (200, 302):
            # Page confirmation type « Merci d'avoir renseigné »
            body = (r.text or "").lower()
            if "merci" in body or "response" in body or tok or edit2:
                return True, f"HTTP {r.status_code}", False, tok, edit_url
            return True, f"HTTP {r.status_code}", False, tok, edit_url
        return False, f"HTTP {r.status_code}", r.status_code in (403, 429, 503), None, None
    except requests.RequestException as e:
        return False, str(e), False, None, None


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


def sleep_interruptible(seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if st.session_state.get("stop_requested"):
            return True
        time.sleep(min(1.0, end - time.time()))
    return bool(st.session_state.get("stop_requested"))


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
st.caption("Prospection · historique · Modifier votre réponse · anti-blocage")

if "running" not in st.session_state:
    st.session_state.running = False
if "stop_requested" not in st.session_state:
    st.session_state.stop_requested = False
if "edit_row_idx" not in st.session_state:
    st.session_state.edit_row_idx = None

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
**Anti-blocage**
- Session + cookies
- User-Agent aléatoire
- Délai aléatoire
- Quotas heure / jour
- Pause si erreurs

**Modifier une réponse**
Le lien *Modifier votre réponse* n’existe que si le propriétaire du Google Form a coché  
**« Autoriser la modification de la réponse après l’envoi »**.
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
        placeholder="0700000000",
    )
    clients_text = st.text_area(
        "Numéros clients (un par ligne)",
        height=180,
        placeholder="0701111111\n0702222222",
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
        st.session_state.edit_row_idx = None
        st.success("Historique vidé.")
        st.rerun()

ok_hour = count_recent_success(history, hours=1)
ok_day = count_recent_success(history, days=1)
st.write(
    f"Quota — **heure :** {ok_hour}/{int(max_hour)} · **jour :** {ok_day}/{int(max_day)} · "
    f"**clients OK :** {len(already)}"
)

if start:
    if bot_state.get("pause_until"):
        try:
            until = datetime.fromisoformat(bot_state["pause_until"])
            if datetime.now() < until:
                st.error(f"Bot en pause jusqu’à {until.strftime('%H:%M:%S')}.")
                st.stop()
            bot_state["pause_until"] = None
            bot_state["consec_errors"] = 0
            save_state(bot_state)
        except ValueError:
            pass

    commercial = normalize_phone(commercial_raw)
    if not commercial:
        st.error("Numéro commercial invalide.")
    else:
        clients = parse_client_list(clients_text)
        if not clients:
            st.error("Aucun numéro client valide.")
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
                f"**À envoyer :** {len(to_send)} · **Doublons :** {len(skipped)} · **Quota :** {len(deferred)} reporté(s)"
            )
            if skipped:
                with st.expander("Ignorés (anti-doublon)"):
                    st.code("\n".join(skipped))
            if deferred:
                with st.expander("Reportés (plafond)"):
                    st.code("\n".join(deferred))

            if not to_send:
                st.success("Rien à envoyer.")
            else:
                st.session_state.running = True
                st.session_state.stop_requested = False
                progress = st.progress(0.0, text="Session…")
                status = st.empty()
                log_box = st.empty()
                run_log: list[str] = []
                hist = load_history()
                consec = int(bot_state.get("consec_errors") or 0)

                status.info("Ouverture du formulaire…")
                session, fbzx = build_session()

                for i, client in enumerate(to_send):
                    if st.session_state.stop_requested:
                        run_log.append(f"⏹ Arrêt avant {client}")
                        break

                    status.info(f"Envoi {i + 1}/{len(to_send)} → `{client}`")
                    ok, detail, blocked, edit2, edit_url = submit_form(
                        session, commercial, client, today_str, fbzx=fbzx
                    )
                    entry = {
                        "id": f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{client}",
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "commercial": commercial,
                        "client": client,
                        "date": today_str,
                        "status": "success" if ok else "error",
                        "detail": detail,
                        "blocked": blocked,
                        "edit2": edit2,
                        "edit_url": edit_url,
                    }
                    hist.append(entry)
                    save_history(hist)

                    if ok:
                        already.add(client)
                        consec = 0
                        edit_note = " · lien modifier OK" if edit_url else " · (pas de lien modifier)"
                        run_log.append(f"✅ {client} — {detail}{edit_note}")
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
                        run_log.append(f"🛡️ Pause {int(pause_sec)}s jusqu’à {until.strftime('%H:%M:%S')}")
                        log_box.code("\n".join(run_log[-40:]))
                        status.error("Pause anti-blocage.")
                        break

                    if i < len(to_send) - 1 and not st.session_state.stop_requested:
                        wait = random.uniform(float(delay_min), float(delay_max)) + random.uniform(0.3, 1.7)
                        status.warning(f"⏳ Prochain envoi ~{int(wait)} s")
                        if sleep_interruptible(wait):
                            run_log.append("⏹ Arrêt pendant l’attente")
                            break
                        if (i + 1) % 5 == 0:
                            try:
                                rr = session.get(FORM_VIEW_URL, timeout=20)
                                fbzx = extract_fbzx(rr.text or "") or fbzx
                                time.sleep(random.uniform(0.8, 1.5))
                            except requests.RequestException:
                                pass

                st.session_state.running = False
                st.session_state.stop_requested = False
                if consec < MAX_CONSECUTIVE_ERRORS:
                    status.success("Terminé.")

st.divider()
st.subheader("📜 Historique local — travail du bot")
history = load_history()
st.caption(
    f"{len(history)} envoi(s) · fichier `{HISTORY_FILE.name}` · "
    "bouton **Modifier** = même action que « Modifier votre réponse » sur Google"
)

if not history:
    st.info("Aucun envoi enregistré.")
else:
    ok_n = sum(1 for h in history if h.get("status") == "success")
    st.write(
        f"Succès : **{ok_n}** · Erreurs : **{len(history) - ok_n}** · "
        f"Avec lien modifier : **{sum(1 for h in history if h.get('edit_url') or h.get('edit2'))}**"
    )

    # Liste inversée (plus récent en haut)
    for rev_i, row in enumerate(reversed(history[-80:])):
        real_index = len(history) - 1 - rev_i
        with st.container():
            c1, c2, c3 = st.columns([3, 1, 1])
            status_icon = "✅" if row.get("status") == "success" else "❌"
            with c1:
                st.markdown(
                    f"{status_icon} **{row.get('client', '—')}** · com. `{row.get('commercial', '—')}`  \n"
                    f"<span style='color:#888;font-size:0.85rem'>{row.get('ts', '')} · date form {row.get('date', '')}</span>",
                    unsafe_allow_html=True,
                )
            with c2:
                if row.get("edit_url"):
                    st.link_button("Ouvrir Google", row["edit_url"], use_container_width=True)
                elif row.get("edit2"):
                    st.link_button(
                        "Ouvrir Google",
                        f"{FORM_VIEW_URL}?edit2={row['edit2']}",
                        use_container_width=True,
                    )
                else:
                    st.caption("Pas de lien")
            with c3:
                if row.get("status") == "success" and (row.get("edit2") or row.get("edit_url")):
                    if st.button("✏️ Modifier", key=f"edit_btn_{real_index}", use_container_width=True):
                        st.session_state.edit_row_idx = real_index
                        st.rerun()
                elif row.get("status") == "success":
                    st.caption("Édition N/A")

            if st.session_state.edit_row_idx == real_index:
                st.markdown("---")
                st.markdown(f"### Modifier la réponse — client `{row.get('client')}`")
                st.caption("Équivalent de « Modifier votre réponse » sur la page Google Forms.")
                with st.form(key=f"edit_form_{real_index}"):
                    new_commercial = st.text_input(
                        "Numéro commercial",
                        value=str(row.get("commercial") or ""),
                    )
                    new_client = st.text_input(
                        "Numéro client",
                        value=str(row.get("client") or ""),
                    )
                    new_date = st.text_input(
                        "Date prospection (AAAA-MM-JJ)",
                        value=str(row.get("date") or date.today().isoformat()),
                    )
                    submitted = st.form_submit_button("Enregistrer la modification sur Google", type="primary")
                    cancel = st.form_submit_button("Annuler")

                if cancel:
                    st.session_state.edit_row_idx = None
                    st.rerun()

                if submitted:
                    nc = normalize_phone(new_commercial)
                    ncl = normalize_phone(new_client)
                    edit2 = row.get("edit2")
                    if not edit2 and row.get("edit_url"):
                        qs = parse_qs(urlparse(row["edit_url"]).query)
                        edit2 = (qs.get("edit2") or [None])[0]
                    if not nc or not ncl:
                        st.error("Numéros invalides.")
                    elif not edit2:
                        st.error(
                            "Pas de token edit2 pour cette ligne. "
                            "Vérifie que le formulaire autorise la modification après envoi, "
                            "puis refais un envoi test."
                        )
                    else:
                        with st.spinner("Mise à jour Google Forms…"):
                            session, fbzx = build_session()
                            ok, detail, blocked, new_tok, new_url = submit_form(
                                session, nc, ncl, new_date, edit2=edit2, fbzx=fbzx
                            )
                        hist = load_history()
                        if 0 <= real_index < len(hist):
                            hist[real_index]["commercial"] = nc
                            hist[real_index]["client"] = ncl
                            hist[real_index]["date"] = new_date
                            hist[real_index]["detail"] = f"modifié: {detail}"
                            hist[real_index]["ts_modified"] = datetime.now().isoformat(timespec="seconds")
                            if new_tok:
                                hist[real_index]["edit2"] = new_tok
                            if new_url:
                                hist[real_index]["edit_url"] = new_url
                            # journal
                            hist.append(
                                {
                                    "id": f"edit_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                                    "ts": datetime.now().isoformat(timespec="seconds"),
                                    "commercial": nc,
                                    "client": ncl,
                                    "date": new_date,
                                    "status": "success" if ok else "error",
                                    "detail": f"UPDATE via bot: {detail}",
                                    "edit2": new_tok or edit2,
                                    "edit_url": new_url or row.get("edit_url"),
                                    "parent_id": row.get("id"),
                                }
                            )
                            save_history(hist)
                        if ok:
                            st.success("Réponse modifiée sur Google Forms.")
                            st.session_state.edit_row_idx = None
                            time.sleep(0.8)
                            st.rerun()
                        else:
                            st.error(f"Échec modification : {detail}")
            st.divider()

st.caption(
    "Si aucun bouton Modifier n’apparaît : dans Google Forms → Paramètres → "
    "Réponses → active « Autoriser la modification de la réponse après l’envoi »."
)
