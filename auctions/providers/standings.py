"""Classifica di lega letta da una pagina web (Fantapazz, Fantacalcio, …).

Non c'è un'API ufficiale: si scarica la pagina della classifica e si cerca la
tabella le cui righe contengono i nomi delle squadre della lega. L'ordine in cui
compaiono è la classifica. Se qualcosa non torna (pagina irraggiungibile, nomi
non riconosciuti, squadre mancanti) si restituisce None e l'admin la inserisce a
mano: meglio nessuna classifica che una sbagliata.
"""
import logging
import re
import unicodedata

import requests
from bs4 import BeautifulSoup

from ..models import Participant

logger = logging.getLogger("auctions.standings")


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _order_in(rows, wanted):
    order = []
    for row in rows:
        text = _norm(row.get_text(" "))
        hits = [pid for pid, name in wanted.items() if name and re.search(rf"(^| ){re.escape(name)}( |$)", text)]
        # Una riga che nomina più squadre (es. una partita) non è una riga di classifica.
        if len(hits) == 1 and hits[0] not in order:
            order.append(hits[0])
    return order


def parse_ranking(html, teams):
    """``teams`` = {participant_id: display_name}. Ritorna gli id in ordine o None.

    Prima cerca una tabella con tutte le squadre (una per riga); se la pagina
    usa liste o div, prova con i figli diretti di ogni contenitore.
    """
    wanted = {pid: _norm(name) for pid, name in teams.items()}
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        order = _order_in(table.find_all("tr"), wanted)
        if len(order) == len(wanted):
            return order
    for container in soup.find_all(["tbody", "ul", "ol", "div", "section"]):
        children = [c for c in container.find_all(recursive=False) if getattr(c, "get_text", None)]
        if len(children) < len(wanted):
            continue
        order = _order_in(children, wanted)
        if len(order) == len(wanted):
            return order
    return None


def fetch_remote_ranking(league, *, get=requests.get):
    url = getattr(league, "standings_url", "")
    if not url:
        return None
    teams = dict(Participant.objects.filter(league=league, is_active=True).values_list("id", "display_name"))
    try:
        resp = get(url, timeout=15, headers={"User-Agent": "FantaManager/1.0"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Classifica remota non raggiungibile (%s): %s", url, exc)
        return None
    order = parse_ranking(resp.text, teams)
    if order is None:
        logger.warning("Classifica remota: squadre non riconosciute in %s", url)
    return order
