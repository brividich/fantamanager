"""Fantapazz roster provider.

Encapsulates the Drupal-7 login, cookie sessions, team discovery and the
rosa-squadra modal parsing that previously lived inline in ``views.py``. The
request/session/cache glue (cross-origin cookie sync, Playwright subprocess)
stays in the views because it needs the Django request; this module owns the
pure network + parsing logic.
"""
import re
import time
from decimal import Decimal

from .base import ProviderError, RosterProvider, register

BASE = "https://www.fantapazz.com"

# How long a synced cookie stays available (seconds).
COOKIE_TTL = 3600

ROLE_MAP = {
    "p": "P", "portiere": "P", "goalkeeper": "P",
    "d": "D", "difensore": "D", "defender": "D", "terzino": "D",
    "c": "C", "centrocampista": "C", "mezzala": "C", "mediano": "C", "trequartista": "C",
    "a": "A", "attaccante": "A", "forward": "A", "ala": "A", "punta": "A", "centravanti": "A",
    "1": "P", "2": "D", "3": "C", "4": "A",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
}

# Numeric ID_Ruolo → role code used by the rosa-squadra modal.
_ID_ROLE = {"1": "P", "2": "D", "3": "C", "4": "A"}


@register
class FantapazzProvider(RosterProvider):
    name = "fantapazz"
    label = "Fantapazz"

    # --- Sessions / auth ----------------------------------------------------

    @staticmethod
    def base_session():
        import requests as req
        s = req.Session()
        s.headers.update(HEADERS)
        # Cookies that Fantapazz expects even before login
        s.cookies.set("has_js",        "1", domain="www.fantapazz.com")
        s.cookies.set("cookie-agreed", "2", domain="www.fantapazz.com")
        return s

    @classmethod
    def session_from_cookie(cls, cookie_str):
        """Build an authenticated session from a pasted Cookie header string."""
        s = cls.base_session()
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                s.cookies.set(k.strip(), v.strip(), domain="www.fantapazz.com")
        return s

    @classmethod
    def login(cls, username, password):
        """Authenticate using credentials; return an authenticated Session.

        Drupal 7 login flow:
        1. GET /user/login  →  extract form_build_id token
        2. POST /user/login with credentials + token
        3. Verify DRUPAL_UID cookie is set
        """
        from bs4 import BeautifulSoup
        s = cls.base_session()
        s.headers["Accept"] = "text/html,application/xhtml+xml,*/*;q=0.8"

        r = s.get(f"{BASE}/user/login", timeout=12)
        if r.status_code != 200:
            raise ProviderError(f"Impossibile raggiungere la pagina di login (HTTP {r.status_code}).")

        soup = BeautifulSoup(r.text, "html.parser")
        build_id_el = soup.find("input", {"name": "form_build_id"})
        build_id    = build_id_el["value"] if build_id_el else ""

        payload = {
            "name":          username,
            "pass":          password,
            "form_build_id": build_id,
            "form_id":       "user_login",
            "op":            "Accedi",
        }
        s.post(f"{BASE}/user/login", data=payload, allow_redirects=True, timeout=15)

        if "DRUPAL_UID" not in {c.name for c in s.cookies}:
            raise ProviderError("Login fallito — controlla username e password.")
        return s

    def authenticate(self, *, cookie=None, username=None, password=None):
        if username and password:
            return self.login(username, password)
        if cookie:
            return self.session_from_cookie(cookie)
        raise ProviderError("Credenziali o cookie Fantapazz mancanti.")

    # --- Parsing ------------------------------------------------------------

    @staticmethod
    def parse_rosters(html):
        """Parse a Fantapazz rosa-squadra modal into normalised data.

        HTML structure (confirmed):
          <div class='nome-squadra'>GELSI UNITED</div>
          <span class='credito-residuo'>2168</span>
          <div class="card-calciatore" id="1449" ID_Ruolo="1" quotazione="7">
              <div class="nomeCalciatore">Falcone</div>
              <div class="nomeClub">Lecce</div>
              <div class="costo">9</div>
          </div>
        ID_Ruolo: 1=P 2=D 3=C 4=A
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        team_el    = soup.find(class_="nome-squadra")
        credits_el = soup.find(class_="credito-residuo")
        team_name  = team_el.get_text(strip=True) if team_el else ""
        rem_credits = credits_el.get_text(strip=True) if credits_el else None

        players = []
        for card in soup.find_all("div", class_="card-calciatore"):
            nome_el  = card.find(class_="nomeCalciatore")
            club_el  = card.find(class_="nomeClub")
            costo_el = card.find(class_="costo")
            name  = nome_el.get_text(strip=True) if nome_el else ""
            club  = club_el.get_text(strip=True)  if club_el else ""
            costo = costo_el.get_text(strip=True)  if costo_el else "0"
            # BeautifulSoup's html.parser lowercases attribute names, so the
            # raw "ID_Ruolo" key is absent — read it case-insensitively.
            role  = _ID_ROLE.get(_attr_ci(card, "ID_Ruolo") or "4", "A")
            quota = _attr_ci(card, "quotazione") or "1"
            if name:
                players.append({
                    "name": name, "role": role, "team": club,
                    "quotazione": quota, "costo": costo,
                })

        return {"fantapazz_team": team_name, "remaining_credits": rem_credits, "players": players}

    # --- Team discovery -----------------------------------------------------

    @classmethod
    def discover_team_ids(cls, session, league_id, manual_ids=None):
        """Find team IDs in a league. Returns ``(team_ids, league_response)``.

        ``team_ids`` is a list of ``{"id", "name"}`` dicts. ``manual_ids`` (if
        given) short-circuits auto-discovery. Raises ProviderError on HTTP
        problems with the league page.
        """
        league_url = f"{BASE}/fantacalcio/squadre-lega/{league_id}/0"
        r = session.get(league_url, timeout=15)
        if r.status_code == 403:
            raise ProviderError("Accesso negato — cookie scaduto o non valido.")
        if r.status_code != 200:
            raise ProviderError(f"Errore HTTP {r.status_code} sulla pagina lega.")

        seen = set()
        team_ids = []

        ajax_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "text/html, */*; q=0.01",
            "Referer": league_url,
        }

        if manual_ids:
            for tid in manual_ids:
                seen.add(tid)
                team_ids.append({"id": tid, "name": f"Squadra {tid}"})
        else:
            found_any = True
            page = 1
            while found_any:
                found_any = False
                try:
                    ts = int(time.time() * 1000)
                    pr = session.get(
                        f"{league_url}?expanded=0&ignore=20&pageNumber={page}&_={ts}",
                        headers=ajax_headers, timeout=10,
                    )
                    for match in re.finditer(r"rosa-squadra[/=](\d+)", pr.text):
                        tid = match.group(1)
                        if tid not in seen:
                            seen.add(tid); found_any = True
                            team_ids.append({"id": tid, "name": f"Squadra {tid}"})
                    for match in re.finditer(r"squadra_(\d+)_", pr.text):
                        tid = match.group(1)
                        if tid not in seen:
                            seen.add(tid); found_any = True
                            team_ids.append({"id": tid, "name": f"Squadra {tid}"})
                    page += 1
                    if page > 10:
                        break
                except Exception:
                    break

            for match in re.finditer(r"rosa-squadra[/=](\d+)", r.text):
                tid = match.group(1)
                if tid not in seen:
                    seen.add(tid)
                    team_ids.append({"id": tid, "name": f"Squadra {tid}"})

        return team_ids, r

    # --- Roster fetch -------------------------------------------------------

    def fetch_rosters(self, session, league_id, team_ids=None):
        """Fetch every team's rosa. Returns ``(teams, errors)`` where ``teams``
        carry both flat ``players`` (with ``fantapazz_team``) and per-team data.

        Kept compatible with the previous view, which used a flat player list.
        """
        all_players = []
        teams       = []
        errors      = []
        for team in team_ids or []:
            url = f"{BASE}/modal/fantacalcio/rosa-squadra/{team['id']}"
            try:
                rr = session.get(url, timeout=10)
                if rr.status_code == 200:
                    parsed = self.parse_rosters(rr.text)
                    if parsed["fantapazz_team"]:
                        team["name"] = parsed["fantapazz_team"]
                    team["remaining_credits"] = parsed["remaining_credits"]
                    for p in parsed["players"]:
                        p["fantapazz_team"] = team["name"]
                    all_players.extend(parsed["players"])
                    # Normalised per-team shape for importers.import_rose_data
                    teams.append({
                        "name": team["name"],
                        "external_id": str(team["id"]),
                        "credits": _to_number(parsed["remaining_credits"]),
                        "players": [
                            {"role": p["role"], "name": p["name"],
                             "cost": _to_number(p.get("costo", 0)) or 0, "club": p.get("team", "")}
                            for p in parsed["players"]
                        ],
                    })
                else:
                    errors.append(f"Squadra {team['id']}: HTTP {rr.status_code}")
            except Exception as e:
                errors.append(f"Squadra {team['id']}: {e}")
        return teams, errors, all_players


def _attr_ci(el, name):
    """Case-insensitive attribute lookup on a BeautifulSoup tag.

    Needed because html.parser folds attribute names to lowercase, so
    ``el.get("ID_Ruolo")`` misses the ``id_ruolo`` key that actually exists.
    """
    target = name.lower()
    for k, v in el.attrs.items():
        if k.lower() == target:
            return v
    return None


def _to_number(v):
    try:
        return float(Decimal(str(v).strip()))
    except Exception:
        return None
