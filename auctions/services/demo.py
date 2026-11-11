"""Leghe di prova: una lega finta, separata dalle vere, con squadre, rose e
crediti, e le mosse delle squadre già simulate per un tipo di mercato fino
alla fase scelta. Servono per vedere e provare un mercato prima di farlo con
la lega vera (Supervisor → Prove).

Ogni mossa passa dagli stessi servizi che usano le pagine (buste, liste,
acquisti, reclami, clausole, rinnovi, asta live): se un servizio rifiuta una
mossa, la prova lo scrive nel suo resoconto invece di forzarla.

Una lega di prova ha ``created_via = "demo"``: non è di nessuno se non del
superuser che l'ha creata e si cancella col suo tasto (``delete_demo``)."""
from datetime import timedelta
from decimal import Decimal
import random

from django.db import transaction
from django.utils import timezone

from ..models import Auction, League, MarketSession, Participant, Player
from ..models.participant import generate_access_code

DEMO = "demo"

KINDS = [
    ("buste", "Buste di riparazione, chi esce si sceglie dopo lo spoglio (lista «Chi lasceresti andare»)"),
    ("buste_taglio", "Buste con il taglio indicato nella busta"),
    ("free_agency", "Mercato libero (free agency): acquisti subito"),
    ("waiver", "Draft di riparazione (waiver wire a turni)"),
    ("clausole", "Clausole rescissorie"),
    ("rinnovi", "Rinnovi dei contratti"),
    ("asta_live", "Asta live di riparazione"),
]
KIND_LABELS = dict(KINDS)
SHORT = {"buste": "Buste", "buste_taglio": "Buste con taglio", "free_agency": "Free agency",
         "waiver": "Waiver", "clausole": "Clausole", "rinnovi": "Rinnovi", "asta_live": "Asta live"}

STAGES = [
    ("aperto", "Aperto: le squadre hanno già fatto le loro mosse, tocca a te (e alla tua squadra)"),
    ("concluso", "Concluso: spoglio o chiusura già fatti, restano le ultime cose (conferme, pareggi, dadi)"),
    ("finito", "Tutto finito: rose, crediti e verbale come a fine mercato"),
]
STAGE_LABELS = dict(STAGES)

TEAM_NAMES = ["La tua squadra (prova)", "Atletico Ma Non Troppo", "Real Madrink", "Borussia Porcmund",
              "Inter Nos", "Sporting Lesbona", "Dinamo Losca", "Bayer Neverkusen", "Paris San Gennaro",
              "Olympique Morsiglia"]
SURNAMES = ["Rossi", "Bianchi", "Esposito", "Colombo", "Ricci", "Marino", "Greco", "Bruno", "Gallo", "Conti",
            "De Luca", "Mancini", "Costa", "Giordano", "Rizzo", "Lombardi", "Moretti", "Barbieri", "Fontana",
            "Santoro", "Mariani", "Rinaldi", "Caruso", "Ferrara", "Galli", "Martini", "Leone", "Longo",
            "Gentile", "Martinelli", "Vitale", "Lombardo", "Serra", "Coppola", "De Santis", "D'Angelo",
            "Marchetti", "Parisi", "Villa", "Conte", "Ferraro", "Fabbri", "Bianco", "Marini", "Grasso"]
CLUBS = ["Atalanta", "Bologna", "Cagliari", "Como", "Fiorentina", "Genoa", "Inter", "Juventus", "Lazio",
         "Lecce", "Milan", "Napoli", "Parma", "Roma", "Sassuolo", "Torino", "Udinese", "Verona"]
# Rosa iniziale di ogni squadra: un po' sotto i posti (3/8/8/6), così chi
# compra senza tagliare ha spazio; le buste con sostituzione tagliano comunque.
DEAL = {"P": 2, "D": 7, "C": 7, "A": 5}
FREE_PER_ROLE = {"P": 8, "D": 20, "C": 20, "A": 16}


class _Sim:
    def __init__(self, owner, kind, stage, teams, seed):
        self.owner, self.kind, self.stage = owner, kind, stage
        self.n_teams = max(4, min(int(teams or 8), len(TEAM_NAMES)))
        self.rng = random.Random(seed)
        self.log = []

    def note(self, text):
        self.log.append(text)

    # --- lega, squadre, listone, rose --------------------------------------

    def league(self):
        stamp = timezone.localtime().strftime("%d/%m %H:%M")
        league = League.objects.create(
            name=f"PROVA – {SHORT[self.kind]} {stamp}", owner=self.owner, created_via=DEMO,
            own_listone=True, budget=Decimal("500"),
            contracts_enabled=self.kind in ("buste", "buste_taglio", "rinnovi"))
        self.lg = league
        self.teams = [Participant.objects.create(
            league=league, display_name=name, credits=Decimal("500"), access_code=generate_access_code(),
            is_active=True) for name in TEAM_NAMES[:self.n_teams]]
        self.mine, self.others = self.teams[0], self.teams[1:]
        self._listone()
        self._rosters()
        return league

    def _listone(self):
        need = {r: DEAL[r] * self.n_teams + FREE_PER_ROLE[r] for r in DEAL}
        from ..models import Footballer
        from .footballers import default_role
        real = {r: [] for r in DEAL}
        for f in Footballer.objects.filter(in_serie_a=True).order_by("name"):
            role = default_role(f)
            if role in real:
                real[role].append(f)
        rows = []
        for role, n in need.items():
            pool = real[role]
            if len(pool) >= n:
                self.rng.shuffle(pool)
                rows += [Player(league=self.lg, name=f.name, role=role, team=f.club_name or "", footballer=f,
                                initial_price=Decimal(self.rng.randint(1, 25))) for f in pool[:n]]
            else:
                rows += [Player(league=self.lg, name=f"{self.rng.choice(SURNAMES)} {role}{i + 1}", role=role,
                                team=self.rng.choice(CLUBS), initial_price=Decimal(self.rng.randint(1, 25)))
                         for i in range(n)]
        Player.objects.bulk_create(rows)

    def _rosters(self):
        now = timezone.now()
        for role, per_team in DEAL.items():
            pool = list(Player.objects.filter(league=self.lg, role=role, owner=None))
            self.rng.shuffle(pool)
            for t in self.teams:
                for _ in range(per_team):
                    pl = pool.pop()
                    pl.owner = t
                    pl.cost = Decimal(self.rng.choice([1, 2, 3, 5, 8, 10, 12, 15, 20, 25, 30, 40]))
                    pl.acquired_at = now - timedelta(days=60)
                    if self.lg.contracts_enabled:
                        pl.contract_years = self.rng.choice([1, 2, 3])
                    pl.save(update_fields=["owner", "cost", "acquired_at", "contract_years"])
        for t in self.teams:
            spent = sum(p.cost for p in Player.objects.filter(owner=t))
            Participant.objects.filter(pk=t.pk).update(spent_credits=spent)
            t.refresh_from_db()

    # --- aiuti ---------------------------------------------------------------

    def free(self, role=None):
        qs = Player.objects.filter(league=self.lg, owner=None)
        if role:
            qs = qs.filter(role=role)
        return list(qs.order_by("name"))

    def owned(self, team, role=None):
        qs = Player.objects.filter(owner=team, abroad_list=False)
        if role:
            qs = qs.filter(role=role)
        return list(qs.order_by("cost", "name"))

    def session(self, session_type, title, **fields):
        s = MarketSession.objects.create(
            league=self.lg, session_type=session_type, title=title, status=MarketSession.Status.OPEN,
            closes_at=timezone.now() + timedelta(days=2), **fields)
        self.s = s
        return s

    def check(self, res, what):
        if res.get("ok"):
            self.note(what)
        else:
            self.note(f"⚠ {what}: rifiutato — {res.get('message') or res.get('error')}")
        return res.get("ok")

    def close_session(self):
        MarketSession.objects.filter(pk=self.s.pk).update(status=MarketSession.Status.CLOSED)
        self.s.refresh_from_db()

    # --- buste ---------------------------------------------------------------

    def buste(self, after):
        from .market import (confirm_market_releases, place_market_bid, proposed_releases, resolve_market_session,
                             set_release_list, settle_market_tie)
        s = self.session(MarketSession.SessionType.REPAIR, "Buste di riparazione (prova)",
                         require_same_role_release=True, release_after=after, allow_conditional_release=True,
                         release_confirm_hours=24 if after else 0, max_bids=0,
                         budget_rule=MarketSession.BudgetRule.PRIORITY, tie_break=MarketSession.TieBreak.MANUAL)
        roles = ["D", "C", "A", "C", "D", "A", "P", "C", "A"]
        contested = None
        for i, team in enumerate(self.others):
            targets = []
            for role in (roles[i % len(roles)], roles[(i + 3) % len(roles)]):
                options = [p for p in self.free(role) if p.id not in {t.id for t in targets}]
                if options:
                    targets.append(self.rng.choice(options))
            if contested is None and targets:
                contested = targets[0]
            elif i == 2 and contested is not None:
                targets[0] = contested                       # un pareggio da risolvere
            if after:
                listed = []
                for role in {t.role for t in targets}:
                    n = sum(1 for t in targets if t.role == role)
                    listed += self.owned(team, role)[:n + 1]
                self.check(set_release_list(s.id, team.id, [p.id for p in listed]),
                           f"{team.display_name}: lista «Chi lasceresti andare» con {len(listed)} giocatori")
            for prio, target in enumerate(targets, start=1):
                amount = 12 if target == contested else self.rng.randint(3, 30)
                release = None
                if not after:
                    cuts = [p for p in self.owned(team, target.role)
                            if p.id not in {b.release_player_id for b in s.bids.filter(participant=team)}]
                    release = cuts[0].id if cuts else None
                self.check(place_market_bid(s.id, team.id, target.id, amount, priority=prio,
                                            release_player_id=release),
                           f"{team.display_name}: busta su {target.name} ({target.role}) da {amount} FM")
        if self.stage == "aperto":
            self.note("La tua squadra non ha ancora consegnato: entra dall'app e prova lista e buste.")
            return
        self.close_session()
        resolve_market_session(s.id)
        s.refresh_from_db()
        summary = s.results_summary or {}
        self.note(f"Spoglio fatto: {summary.get('total_acquisitions', 0)} acquisti, "
                  f"{summary.get('open_ties', 0)} pareggi da risolvere.")
        winners = list(dict.fromkeys(w["winner_id"] for w in summary.get("won", []) if w.get("release_pending")))
        confirm_now = winners if self.stage == "finito" else winners[: len(winners) // 2]
        if self.stage == "finito":
            for t in summary.get("tied", []):
                if not t.get("settled"):
                    self.check(settle_market_tie(s.id, t["player_id"], rng=self.rng),
                               f"Pareggio su {t['player_name']}: sorteggio")
            s.refresh_from_db()
            winners = list(dict.fromkeys(w["winner_id"] for w in (s.results_summary or {}).get("won", [])
                                         if w.get("release_pending")))
            confirm_now = winners
        for wid in confirm_now:
            choices = proposed_releases(s, wid)
            name = next(t.display_name for t in self.teams if t.id == wid)
            self.check(confirm_market_releases(s.id, wid, choices, roll_dice=self.rng.random() < 0.6,
                                               participant_id=wid),
                       f"{name}: conferma chi esce (dalla lista)")
            s.refresh_from_db()
        if self.stage == "concluso" and after:
            self.note("Restano da confermare alcune sostituzioni e i pareggi: falli tu dalla pagina della sessione.")
        if self.stage == "finito" and self.lg.contracts_enabled:
            # A mercato finito anche i dadi del contratto sono tirati.
            from .contracts import roll_contract
            for pl in Player.objects.filter(league=self.lg, owner__isnull=False, contract_years__isnull=True):
                res = roll_contract(pl.id, by_admin=True, rng=self.rng)
                if res.get("ok"):
                    self.note(f"Dado contratto di {pl.name}: {res['years']} ann{'o' if res['years'] == 1 else 'i'}")

    # --- free agency, waiver, clausole ---------------------------------------

    def free_agency(self):
        from .market import acquire_free_agent, resolve_market_session
        s = self.session(MarketSession.SessionType.FREE_AGENCY, "Free agency (prova)",
                         config={"fa_max_moves": 2, "fa_cost_type": "quotation", "fa_period": "rolling"},
                         require_same_role_release=False, allow_conditional_release=True)
        for team in self.others[: max(3, len(self.others) - 2)]:
            target = self.rng.choice(self.free(self.rng.choice(["D", "C", "A"])))
            self.check(acquire_free_agent(s.id, team.id, target.id),
                       f"{team.display_name}: compra {target.name} ({target.role})")
        if self.stage == "aperto":
            self.note("La finestra è aperta: compra anche tu dall'app (massimo 2 acquisti).")
            return
        resolve_market_session(s.id)
        self.note("Finestra conclusa: resta il riepilogo degli acquisti.")

    def waiver(self):
        from .market import place_waiver_claim, resolve_market_session
        s = self.session(MarketSession.SessionType.WAIVER_WIRE, "Waiver wire (prova)",
                         config={"waiver_order_type": "inverse_standing", "waiver_claim_hours": 24},
                         require_same_role_release=False, allow_conditional_release=True)
        hot = self.rng.choice(self.free("A"))
        for i, team in enumerate(self.others):
            first = hot if i < 3 else self.rng.choice(self.free("C"))
            second = self.rng.choice(self.free("D"))
            for prio, target in enumerate((first, second), start=1):
                self.check(place_waiver_claim(s.id, team.id, target.id, priority=prio),
                           f"{team.display_name}: reclamo n.{prio} su {target.name} ({target.role})")
        if self.stage == "aperto":
            self.note(f"Tre squadre vogliono {hot.name}: vince chi viene prima nell'ordine. Fai anche tu un reclamo.")
            return
        self.close_session()
        resolve_market_session(s.id)
        self.note("Draft eseguito: ogni giocatore va alla squadra che lo chiedeva per prima nell'ordine.")

    def clausole(self):
        from .market import execute_buyout, resolve_market_session
        s = self.session(MarketSession.SessionType.BUYOUT_CLAUSE, "Clausole rescissorie (prova)",
                         config={"buyout_multiplier": 1.5, "buyout_min_hold_days": 0},
                         require_same_role_release=False, allow_conditional_release=True)
        for team in self.others[:3]:
            seller = self.rng.choice([t for t in self.teams if t.id != team.id])
            options = [p for p in self.owned(seller) if p.cost <= 15]
            if not options:
                continue
            target = self.rng.choice(options)
            self.check(execute_buyout(s.id, team.id, target.id),
                       f"{team.display_name}: paga la clausola di {target.name} a {seller.display_name}")
        if self.stage == "aperto":
            self.note("La finestra è aperta: prova a pagare una clausola dall'app.")
            return
        resolve_market_session(s.id)
        self.note("Finestra conclusa: resta il riepilogo delle clausole pagate.")

    # --- rinnovi ---------------------------------------------------------------

    def rinnovi(self):
        from .contracts import declare_renewals, roll_renewal
        from .market import resolve_market_session, sync_renewals_window
        for team in self.teams:
            expiring = self.owned(team)[:3]
            Player.objects.filter(pk__in=[p.id for p in expiring]).update(contract_years=0)
        s = self.session(MarketSession.SessionType.RENEWALS, "Rinnovi contratti (prova)")
        sync_renewals_window({self.lg.id})
        acting = self.others if self.stage != "aperto" else self.others[: len(self.others) // 2]
        for team in acting:
            expiring = list(Player.objects.filter(owner=team, contract_years=0))
            keep = [p.id for p in expiring[:2]]
            self.check(declare_renewals(team.id, keep), f"{team.display_name}: dichiara {len(keep)} rinnovi")
            for pid in keep:
                res = roll_renewal(pid, participant_id=team.id, rng=self.rng)
                if res.get("ok"):
                    self.note(f"   dado rinnovo {res['player_name']}: "
                              + (f"verde, {res['years']} anni" if res.get("green") else "rosso, rescisso"))
        if self.stage == "aperto":
            self.note("Metà delle squadre ha già dichiarato e tirato: tocca alle altre (e alla tua) dall'app.")
            return
        if self.stage == "finito":
            resolve_market_session(s.id)
            self.note("Rinnovi chiusi: chi non è stato dichiarato è svincolato.")
        else:
            self.note("Le squadre hanno dichiarato: chiudi tu i rinnovi dalla pagina della sessione.")

    # --- asta live -------------------------------------------------------------

    def asta_live(self):
        from . import build_queue, place_bid, reset_if_closed, start_auction
        from .scheduler import auction_tick
        auction = Auction.objects.create(
            league=self.lg, title="Asta live di riparazione (prova)", mode=Auction.Mode.REPAIR_AUCTION,
            starting_price=Decimal("1"), current_price=Decimal("1"), min_increment=Decimal("1"),
            quick_increments="1,2,5,10", duration_seconds=30, antisnipe_seconds=10, cycle_break_seconds=0,
            flow_mode=Auction.FlowMode.CONTINUOUS, call_order=Auction.CallOrder.PDCA,
            within_role_order=Auction.WithinRole.RANDOM, status=Auction.Status.READY)
        self.auction = auction
        build_queue(auction)
        if self.stage == "aperto":
            self.note("Asta pronta con gli svincolati in coda: falla partire dalla regia, le squadre entrano dai loro link.")
            return
        start_auction(auction.id)
        lots = 3 if self.stage == "finito" else 1
        used = set()
        for _ in range(lots):
            auction.refresh_from_db()
            if auction.player_id is None:
                break
            name, role = auction.player.name, auction.player.role
            # Squadre con un posto libero nel ruolo e che non hanno appena
            # rilanciato (fra due rilanci della stessa squadra passa un attimo).
            slots = getattr(self.lg, f"slots_{role.lower()}")
            room = [t for t in self.others if t.id not in used
                    and Player.objects.filter(owner=t, role=role).count() < slots]
            bidders = self.rng.sample(room, k=min(2, len(room)))
            used |= {t.id for t in bidders}
            for team in bidders:
                res = place_bid(auction.id, team.id, Decimal(self.rng.choice([1, 2, 5])))
                self.check({"ok": res.accepted, "message": res.reason},
                           f"{team.display_name}: rilancio su {name}")
            if self.stage == "concluso":
                self.note("Asta in corso: un lotto è già conteso, entra dalla regia o come squadra e rilancia.")
                return
            Auction.objects.filter(pk=auction.id).update(ends_at=timezone.now() - timedelta(seconds=10))
            auction_tick(auction.id)                     # chiude e assegna il lotto
            reset_if_closed(auction.id)                  # come «Prosegui» in regia
            winner = Player.objects.filter(pk=auction.player_id).values_list("owner__display_name", flat=True).first()
            self.note(f"Lotto {name} aggiudicato a {winner or '—'}.")
        Auction.objects.filter(pk=auction.id).update(status=Auction.Status.PAUSED)
        self.note("Asta in pausa dopo qualche lotto: rose e crediti mostrano gli acquisti.")


@transaction.atomic
def create_demo(owner, kind, stage, *, teams=8, seed=None):
    """Crea una lega di prova per ``kind`` (vedi KINDS) fino a ``stage``
    (STAGES). Restituisce ``{"league", "session" o "auction", "log"}``."""
    if kind not in KIND_LABELS:
        raise ValueError(f"Tipo di mercato sconosciuto: {kind}")
    if stage not in STAGE_LABELS:
        raise ValueError(f"Fase sconosciuta: {stage}")
    sim = _Sim(owner, kind, stage, teams, seed)
    league = sim.league()
    sim.note(f"Lega «{league.name}»: {sim.n_teams} squadre, rose da {sum(DEAL.values())} giocatori, 500 crediti.")
    {"buste": lambda: sim.buste(True), "buste_taglio": lambda: sim.buste(False),
     "free_agency": sim.free_agency, "waiver": sim.waiver, "clausole": sim.clausole,
     "rinnovi": sim.rinnovi, "asta_live": sim.asta_live}[kind]()
    for obj in (getattr(sim, "s", None), getattr(sim, "auction", None)):
        if obj is not None:
            obj.refresh_from_db()
    return {"league": league, "session": getattr(sim, "s", None), "auction": getattr(sim, "auction", None),
            "log": sim.log}


def demo_leagues():
    return League.objects.filter(created_via=DEMO).order_by("-created_at")


def delete_demo(league_id):
    """Cancella una lega di prova con tutto quello che ha (solo le prove)."""
    from .session import delete_league
    league = League.objects.filter(pk=league_id, created_via=DEMO).first()
    if league is None:
        return None
    return delete_league(league.id)
