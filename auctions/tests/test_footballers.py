"""Anagrafica comune dei calciatori (API-Football) e collegamento dei listoni."""
import os
import re
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from ..models import Footballer, League, Participant, Player
from ..providers import apifootball
from ..providers.importers import sync_players
from ..services import footballers
from ..services.lifecycle import release_player

JUVE, INTER = (496, "Juventus", "https://media.api-sports.io/football/teams/496.png"), \
    (505, "Inter", "https://media.api-sports.io/football/teams/505.png")


class Resp:
    def __init__(self, data, headers=None):
        self.data = data
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def squad_player(pid, name, position, age=25, number=None):
    return {"id": pid, "name": name, "age": age, "number": number, "position": position,
            "photo": f"https://media.api-sports.io/football/players/{pid}.png"}


SQUADS = {
    496: [squad_player(1, "D. Vlahović", "Attacker", 25, 9), squad_player(2, "K. Yıldız", "Attacker", 20, 10),
          squad_player(3, "M. Locatelli", "Midfielder", 27, 5)],
    505: [squad_player(4, "Lautaro Martínez", "Attacker", 28, 10), squad_player(5, "Josep Martínez", "Goalkeeper", 27, 13),
          squad_player(6, "N. Barella", "Midfielder", 28, 23)],
}


class ApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        apifootball._refused_seasons.clear()
        apifootball._limits.update(minute=None, day=None)
        self.addCleanup(apifootball._refused_seasons.clear)
        self.addCleanup(apifootball._limits.update, minute=None, day=None)
        patcher = mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": "test", "APIFOOTBALL_SEASON": "2026"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def fake_get(self, squads=None, teams_refused=False, fail_on=None):
        squads = SQUADS if squads is None else squads

        def get(url, params=None, **kw):
            path = url.split("v3.football.api-sports.io", 1)[-1]
            self.calls.append((path, dict(params)))
            if path == "/teams" and "league" in params:
                if teams_refused:
                    return Resp({"errors": {"plan": "Free plans do not have access to this season."}})
                return Resp({"response": [{"team": {"id": t[0], "name": t[1], "logo": t[2]}} for t in (JUVE, INTER)]})
            if path == "/teams":
                return Resp({"response": [{"team": {"id": 496, "name": "Juventus"}},
                                          {"team": {"id": 505, "name": "Inter"}}]})
            if path == "/players/squads":
                if params["team"] == fail_on:
                    return Resp({"errors": {"requests": "You have reached the request limit for the day"}})
                return Resp({"response": [{"team": {"id": params["team"]}, "players": squads.get(params["team"], [])}]})
            raise AssertionError(path)
        return get

    def sync(self, **kw):
        return footballers.sync_registry(get=self.fake_get(**kw), sleep=lambda s: None)


class RegistrySyncTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        # Leghe con un listone proprio: qui conta il collegamento, non la lista generale.
        self.league = League.objects.create(name="L", own_listone=True)
        self.other = League.objects.create(name="Altra", own_listone=True)
        mk = lambda name, role, team, league=self.league: Player.objects.create(
            name=name, role=role, team=team, league=league)
        self.vlahovic = mk("Vlahovic", "A", "Juventus")
        self.yildiz = mk("Yildiz", "A", "Juventus")
        self.lautaro = mk("Martinez L.", "A", "Inter")
        self.josep = mk("Martinez Jo.", "P", "Inter")
        self.barella_other = mk("Barella", "C", "Inter", self.other)

    def test_downloads_every_serie_a_squad_once(self):
        report = self.sync()
        self.assertEqual(report["error"], "")
        self.assertEqual((report["clubs"], report["players"], report["created"]), (2, 6, 6))
        self.assertEqual([c[0] for c in self.calls], ["/teams", "/players/squads", "/players/squads"])
        lautaro = Footballer.objects.get(api_id=4)
        self.assertEqual((lautaro.position, lautaro.club_name, lautaro.club_api_id, lautaro.number, lautaro.age),
                         ("A", "Inter", 505, 10, 28))
        self.assertEqual(lautaro.club_logo, INTER[2])
        self.assertTrue(lautaro.in_serie_a)

    def test_links_the_listone_of_every_league(self):
        self.sync()
        for player, api_id in ((self.vlahovic, 1), (self.yildiz, 2), (self.lautaro, 4), (self.josep, 5),
                               (self.barella_other, 6)):
            player.refresh_from_db()
            self.assertEqual(player.footballer.api_id, api_id, player.name)
        # La foto arriva dall'anagrafica solo se il giocatore non ne aveva.
        self.assertEqual(self.vlahovic.photo_url, "https://media.api-sports.io/football/players/1.png")

    def test_keeps_an_existing_photo(self):
        Player.objects.filter(pk=self.vlahovic.pk).update(photo_url="https://example.com/v.png")
        self.sync()
        self.vlahovic.refresh_from_db()
        self.assertEqual(self.vlahovic.photo_url, "https://example.com/v.png")

    def test_a_second_sync_updates_and_marks_who_left(self):
        self.sync()
        moved = {496: SQUADS[496][:2] + [squad_player(6, "N. Barella", "Midfielder", 29, 23)], 505: SQUADS[505][:2]}
        report = self.sync(squads=moved)
        # Locatelli manca per la prima volta: si aspetta il prossimo giro.
        self.assertEqual((report["created"], report["left"], report["missing"]), (0, 0, 1))
        self.assertIn("se mancano anche al prossimo", footballers.sync_summary(report))
        barella = Footballer.objects.get(api_id=6)
        self.assertEqual((barella.club_name, barella.age, barella.in_serie_a), ("Juventus", 29, True))
        self.assertTrue(Footballer.objects.get(api_id=3).in_serie_a)
        report = self.sync(squads=moved)
        self.assertEqual((report["left"], report["missing"]), (1, 0))
        self.assertFalse(Footballer.objects.get(api_id=3).in_serie_a)

    def test_who_comes_back_starts_counting_again(self):
        self.sync()
        self.sync(squads={496: SQUADS[496][:2], 505: SQUADS[505]})   # Locatelli manca una volta
        self.sync()                                                   # torna
        self.sync(squads={496: SQUADS[496][:2], 505: SQUADS[505]})   # manca di nuovo, una volta
        locatelli = Footballer.objects.get(api_id=3)
        self.assertEqual((locatelli.in_serie_a, locatelli.missed_syncs), (True, 1))

    def test_an_interrupted_sync_makes_nobody_leave(self):
        self.sync()
        report = self.sync(squads={496: SQUADS[496], 505: []}, fail_on=505)
        self.assertIn("limite giornaliero", report["error"])
        self.assertEqual(report["left"], 0)
        self.assertEqual(Footballer.objects.filter(in_serie_a=True).count(), 6)

    def test_an_empty_squad_from_the_api_makes_nobody_leave(self):
        """API-Football risponde 200 con una rosa vuota (piano, limite): non è
        un club senza giocatori, nessuno esce dalla Serie A."""
        self.sync()
        report = self.sync(squads={496: SQUADS[496], 505: []})
        self.assertEqual(report["left"], 0)
        self.assertEqual(report["empty_clubs"], ["Inter"])
        self.assertIn("rose vuote", footballers.sync_summary(report))
        self.assertEqual(Footballer.objects.filter(in_serie_a=True).count(), 6)

    def test_too_many_leaving_at_once_is_a_bad_answer(self):
        self.sync()
        moved = {496: SQUADS[496][:1], 505: SQUADS[505][:1]}  # 4 su 6 «usciti»
        with mock.patch.object(footballers, "MAX_LEFT_MIN", 0):
            report = self.sync(squads=moved)
        self.assertEqual(report["left"], 0)
        self.assertIn("risposta incompleta", report["error"])
        self.assertEqual(Footballer.objects.filter(in_serie_a=True).count(), 6)

    def test_free_plan_recognises_clubs_from_the_listone(self):
        report = self.sync(teams_refused=True)
        self.assertEqual(report["error"], "")
        self.assertEqual(report["clubs"], 2)
        self.assertEqual(Footballer.objects.count(), 6)

    def test_without_key_nothing_is_called(self):
        with mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": ""}):
            report = self.sync()
        self.assertIn("APIFOOTBALL_KEY", report["error"])
        self.assertEqual(self.calls, [])


class DefaultListoneTests(ApiTestCase):
    """La lista generale è il listone di chi non ne carica uno suo."""

    def test_roles_come_from_api_until_the_superuser_edits_them(self):
        self.sync()
        josep = Footballer.objects.get(api_id=5)
        self.assertEqual((josep.position, josep.role, josep.role_edited), ("P", "P", False))
        barella = Footballer.objects.get(api_id=6)
        ok, _ = footballers.edit_roles(barella, "c", "M;C;x")
        self.assertTrue(ok)
        barella.refresh_from_db()
        self.assertEqual((barella.role, barella.mantra_roles, barella.role_edited), ("C", "M;C", True))
        moved = {496: SQUADS[496], 505: SQUADS[505][:2] + [squad_player(6, "N. Barella", "Defender", 29, 23)]}
        self.sync(squads=moved)
        barella.refresh_from_db()
        self.assertEqual((barella.position, barella.role), ("D", "C"))

    def test_mantra_roles_alone_give_the_classic_role(self):
        f = Footballer.objects.create(api_id=9, name="F. Dimarco", position="D", role="D", club_name="Inter")
        footballers.edit_roles(f, "", "E;Ds")
        f.refresh_from_db()
        self.assertEqual((f.role, f.mantra_roles), ("D", "E;Ds"))
        ok, msg = footballers.edit_roles(f, "X", "")
        self.assertFalse(ok)

    def test_a_new_league_starts_with_the_general_list(self):
        self.sync()
        league = League.objects.create(name="Nuova")
        players = {p.name: p for p in Player.objects.filter(league=league)}
        self.assertEqual(len(players), 6)
        lautaro = players["Lautaro Martínez"]
        self.assertEqual((lautaro.role, lautaro.team, lautaro.footballer.api_id), ("A", "Inter", 4))
        self.assertEqual(lautaro.photo_url, "https://media.api-sports.io/football/players/4.png")

    def test_sync_fills_leagues_without_their_own_listone_only(self):
        default = League.objects.create(name="Generale")
        own = League.objects.create(name="Propria", own_listone=True)
        Player.objects.create(name="Martinez L.", role="A", team="Inter", league=own, initial_price=Decimal("40"))
        report = self.sync()
        self.assertEqual(report["leagues"], 1)
        self.assertEqual(Player.objects.filter(league=default).count(), 6)
        self.assertEqual(list(Player.objects.filter(league=own).values_list("name", flat=True)), ["Martinez L."])

    def test_edits_reach_the_leagues_on_the_general_list(self):
        self.sync()
        default = League.objects.create(name="Generale")
        own = League.objects.create(name="Propria", own_listone=True)
        own_barella = Player.objects.create(name="Barella", role="C", team="Inter", league=own)
        footballers.link_players()
        footballers.edit_roles(Footballer.objects.get(api_id=6), "A", "T")
        self.assertEqual(Player.objects.get(league=default, footballer__api_id=6).role, "A")
        own_barella.refresh_from_db()
        self.assertEqual((own_barella.role, own_barella.mantra_roles), ("C", ""))

    def test_an_owned_player_keeps_his_team_and_who_left_goes(self):
        self.sync()
        league = League.objects.create(name="Generale")
        team = Participant.objects.create(display_name="Alfa", league=league)
        Player.objects.filter(league=league, footballer__api_id=4).update(owner=team, cost=Decimal("50"))
        moved = {496: SQUADS[496][:2], 505: SQUADS[505][1:]}  # escono Locatelli (libero) e Lautaro (in rosa)
        self.sync(squads=moved)
        self.assertIn("M. Locatelli", Player.objects.filter(league=league).values_list("name", flat=True))
        self.sync(squads=moved)  # assenti due volte di fila: escono
        names = set(Player.objects.filter(league=league).values_list("name", flat=True))
        self.assertNotIn("M. Locatelli", names)
        lautaro = Player.objects.get(league=league, footballer__api_id=4)
        self.assertEqual((lautaro.owner, lautaro.cost), (team, Decimal("50")))
        self.assertIsNotNone(lautaro.left_serie_a_at)

    def test_who_left_with_votes_stays_in_the_listone(self):
        """Uno svincolato uscito dalla Serie A con voti della lega non si
        cancella: si porterebbe via lo storico delle giornate."""
        from ..models import Giornata, PlayerPerformance, Season
        self.sync()
        league = League.objects.create(name="Generale")
        season = Season.objects.create(league=league, name="2026/27", is_current=True)
        giornata = Giornata.objects.create(season=season, number=1)
        locatelli = Player.objects.get(league=league, footballer__api_id=3)
        PlayerPerformance.objects.create(giornata=giornata, player=locatelli, vote=Decimal("6.5"))
        for _ in range(2):
            self.sync(squads={496: SQUADS[496][:2], 505: SQUADS[505]})  # esce Locatelli
        self.assertFalse(Footballer.objects.get(api_id=3).in_serie_a)
        self.assertTrue(Player.objects.filter(pk=locatelli.pk).exists())
        self.assertEqual(PlayerPerformance.objects.filter(player=locatelli).count(), 1)
        self.assertFalse(Player.objects.free_agents().filter(pk=locatelli.pk).exists())

    def test_who_left_serie_a_does_not_come_back_as_free_agent(self):
        self.sync()
        league = League.objects.create(name="Generale")
        team = Participant.objects.create(display_name="Alfa", league=league)
        Player.objects.filter(league=league, footballer__api_id=4).update(owner=team, cost=Decimal("50"))
        for _ in range(2):                                           # Lautaro esce, ma è in rosa
            self.sync(squads={496: SQUADS[496], 505: SQUADS[505][1:]})
        lautaro = Player.objects.get(league=league, footballer__api_id=4)
        self.assertFalse(lautaro.in_listone)
        release_player(lautaro.id, by_admin=True)
        self.assertFalse(Player.objects.free_agents().filter(pk=lautaro.pk).exists())
        self.sync()                                                  # torna in Serie A
        lautaro.refresh_from_db()
        self.assertTrue(lautaro.in_listone)
        self.assertTrue(Player.objects.free_agents().filter(pk=lautaro.pk).exists())

    def test_uploading_a_listone_makes_it_the_league_own(self):
        self.sync()
        league = League.objects.create(name="Generale")
        sync_players([{"name": "Martinez L.", "role": "A", "team": "Inter", "price": "40"}], league=league,
                     prune=True)
        league.refresh_from_db()
        self.assertTrue(league.own_listone)
        self.assertEqual(list(Player.objects.filter(league=league).values_list("name", "initial_price")),
                         [("Martinez L.", Decimal("40"))])
        footballers.use_default_listone(league)
        league.refresh_from_db()
        self.assertFalse(league.own_listone)
        self.assertEqual(Player.objects.filter(league=league).count(), 6)


class LinkingTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="L")
        mk = lambda api_id, name, pos, club: Footballer.objects.create(
            api_id=api_id, name=name, position=pos, club_name=club, club_api_id=1)
        mk(1, "M. Thuram", "A", "Inter")
        mk(2, "K. Thuram", "C", "Juventus")
        mk(3, "F. Esposito", "A", "Inter")
        mk(4, "S. Esposito", "A", "Inter")
        mk(5, "R. Leão", "A", "AC Milan")

    def player(self, name, role, team):
        return Player.objects.create(name=name, role=role, team=team, league=self.league)

    def test_same_club_and_surname(self):
        thuram = self.player("Thuram", "A", "Inter")
        footballers.link_players()
        thuram.refresh_from_db()
        self.assertEqual(thuram.footballer.api_id, 1)

    def test_doubtful_matches_stay_unlinked(self):
        esposito = self.player("Esposito", "A", "Inter")
        footballers.link_players()
        esposito.refresh_from_db()
        self.assertIsNone(esposito.footballer)

    def test_initials_choose_between_namesakes(self):
        esposito = self.player("Esposito F.P.", "A", "Inter")
        footballers.link_players()
        esposito.refresh_from_db()
        self.assertEqual(esposito.footballer.api_id, 3)

    def test_club_code_and_accents(self):
        leao = self.player("Leao", "A", "MIL")
        footballers.link_players()
        leao.refresh_from_db()
        self.assertEqual(leao.footballer.api_id, 5)

    def test_moved_club_with_a_unique_surname_and_role(self):
        # Il listone lo dà ancora al Milan, l'anagrafica già alla Juventus.
        thuram = self.player("Thuram K.", "C", "Milan")
        footballers.link_players()
        thuram.refresh_from_db()
        self.assertEqual(thuram.footballer.api_id, 2)

    def test_listone_import_links_new_players(self):
        sync_players([{"name": "Leao", "role": "A", "team": "Milan", "price": "30"}], league=self.league)
        self.assertEqual(Player.objects.get(league=self.league, name="Leao").footballer.api_id, 5)


class FootballersPageTests(TestCase):
    """La stessa anagrafica in console e nell'app, con accanto la propria lega."""

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user("owner_fb", password="pw")
        self.stranger = User.objects.create_user("stranger_fb", password="pw")
        self.league = League.objects.create(name="Lega Calciatori", owner=self.owner)
        self.other = League.objects.create(name="Lega Altrui", owner=self.stranger)
        self.team = Participant.objects.create(display_name="Owner FC", league=self.league, user=self.owner,
                                               credits=Decimal("300"))
        rival = Participant.objects.create(display_name="Rivali FC", league=self.other, credits=Decimal("300"))
        self.lautaro = Footballer.objects.create(api_id=4, name="Lautaro Martínez", position="A",
                                                 club_name="Inter", club_api_id=505, age=28)
        self.barella = Footballer.objects.create(api_id=6, name="N. Barella", position="C",
                                                 club_name="Inter", club_api_id=505)
        Footballer.objects.create(api_id=7, name="A. Gone", position="D", club_name="Inter", in_serie_a=False)
        Player.objects.create(name="Martinez L.", role="A", team="Inter", league=self.league,
                              owner=self.team, cost=Decimal("45"), footballer=self.lautaro)
        Player.objects.create(name="Barella", role="C", team="Inter", league=self.other,
                              owner=rival, cost=Decimal("30"), footballer=self.barella)

    @staticmethod
    def _part(resp):
        html = resp.content.decode()
        part = html[html.index("<!-- footballers:start -->"):html.index("<!-- footballers:end -->")]
        part = re.sub(r'name="(next|csrfmiddlewaretoken)" value="[^"]*"', "", part)
        # I link fra pagine ({% purl %}) puntano alla console o all'app: forma canonica.
        from ..views.common import APP_PAGES
        for console_name, app_name in APP_PAGES.items():
            try:
                urls = (reverse(console_name), reverse(app_name))
            except Exception:  # noqa: BLE001 — pagine con argomenti
                continue
            for url in urls:
                part = re.sub('"' + re.escape(url) + r'(?=["?#])', f'"PAGE:{console_name}', part)
        return part

    def test_same_screen_in_console_and_app(self):
        self.client.force_login(self.owner)
        for vista in ("listone", "anagrafica"):
            with self.subTest(vista=vista):
                q = f"?league={self.league.id}&vista={vista}"
                console = self.client.get(reverse("admin_footballers") + q)
                app = self.client.get(reverse("app_footballers") + q)
                self.assertEqual(console.status_code, 200)
                self.assertEqual(app.status_code, 200)
                self.assertEqual(self._part(console), self._part(app))

    def test_shows_who_owns_the_player_in_your_league_only(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("app_footballers") + "?vista=anagrafica")
        self.assertContains(resp, "Lautaro Martínez")
        self.assertContains(resp, "Owner FC")
        self.assertContains(resp, "Non nel listone")  # Barella è di un'altra lega
        self.assertNotContains(resp, "Rivali FC")
        self.assertNotContains(resp, "A. Gone")

    def test_console_hides_leagues_you_do_not_manage(self):
        self.client.force_login(self.stranger)
        resp = self.client.get(reverse("admin_footballers") + f"?league={self.league.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Lautaro Martínez")
        self.assertNotContains(resp, "Owner FC")

    def test_filters(self):
        self.client.force_login(self.owner)
        url = reverse("app_footballers") + "?vista=anagrafica&"
        self.assertNotContains(self.client.get(url + "role=C"), "Lautaro Martínez")
        self.assertNotContains(self.client.get(url + "lega=owned"), "N. Barella")
        self.assertNotContains(self.client.get(url + "lega=out"), "Lautaro Martínez")
        self.assertContains(self.client.get(url + "q=martinez l"), "Lautaro Martínez")  # nome del listone
        self.assertContains(self.client.get(url + "usciti=1"), "A. Gone")

    def test_only_a_superuser_edits_the_roles(self):
        url = reverse("admin_footballer_edit", args=[self.barella.id])
        self.client.force_login(self.owner)
        self.client.post(url, {"role": "A"})
        self.barella.refresh_from_db()
        self.assertEqual(self.barella.role, "")
        self.assertNotContains(self.client.get(reverse("app_footballers") + "?vista=anagrafica"), "Ruoli Mantra")

        root = User.objects.create_superuser("root_fb", password="pw")
        self.client.force_login(root)
        self.assertContains(self.client.get(reverse("admin_footballers") + "?vista=anagrafica"), "Ruoli Mantra")
        back = reverse("admin_footballers")
        resp = self.client.post(url, {"role": "C", "mantra_roles": "M;C", "next": back})
        self.assertRedirects(resp, back, fetch_redirect_response=False)
        self.barella.refresh_from_db()
        self.assertEqual((self.barella.role, self.barella.mantra_roles), ("C", "M;C"))

    def test_league_admin_goes_back_to_the_general_list(self):
        League.objects.filter(pk=self.league.pk).update(own_listone=True)
        url = reverse("admin_league_default_listone", args=[self.league.id])
        self.client.force_login(self.stranger)
        self.client.post(url)
        self.league.refresh_from_db()
        self.assertTrue(self.league.own_listone)

        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("app_footballers") + "?vista=anagrafica"), "Usa la lista generale")
        self.client.post(url, {"next": reverse("app_footballers")})
        self.league.refresh_from_db()
        self.assertFalse(self.league.own_listone)
        # Lautaro resta in rosa, Barella entra nel listone dalla lista generale.
        self.assertEqual(Player.objects.get(league=self.league, footballer=self.lautaro).owner, self.team)
        self.assertTrue(Player.objects.filter(league=self.league, footballer=self.barella, owner=None).exists())

    def test_only_a_superuser_starts_the_sync(self):
        self.client.force_login(self.owner)
        with mock.patch.object(footballers, "start_sync") as start:
            self.client.post(reverse("admin_footballers_sync"))
        start.assert_not_called()

        root = User.objects.create_superuser("root_sync", password="pw")
        self.client.force_login(root)
        back = reverse("app_footballers")
        with mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": "test"}), \
                mock.patch.object(footballers, "start_sync", return_value=True) as start:
            resp = self.client.post(reverse("admin_footballers_sync"), {"next": back})
        start.assert_called_once()
        self.assertRedirects(resp, back, fetch_redirect_response=False)


class ListoneTabTests(TestCase):
    """Calciatori → Listone: i giocatori della lega, anche senza API-Football;
    gli strumenti solo a chi gestisce la lega."""

    def setUp(self):
        self.owner = User.objects.create_user("pres_lst", password="pw")
        self.mgr = User.objects.create_user("mgr_lst", password="pw")
        self.league = League.objects.create(name="Lega Listone", owner=self.owner)
        self.team = Participant.objects.create(display_name="Alfa", league=self.league, user=self.mgr,
                                               credits=Decimal("300"))
        Player.objects.create(name="Lautaro", role="A", team="Inter", league=self.league,
                              owner=self.team, cost=Decimal("40"), initial_price=Decimal("35"))
        Player.objects.create(name="Barella", role="C", team="Inter", league=self.league,
                              initial_price=Decimal("20"))

    def test_the_listone_is_the_first_tab_even_with_an_empty_registry(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("admin_footballers") + f"?league={self.league.id}")
        self.assertEqual(Footballer.objects.count(), 0)
        self.assertEqual(resp.context["fb_view"], "listone")
        self.assertContains(resp, "Lautaro")
        self.assertContains(resp, "Strumenti listone")
        self.assertContains(resp, reverse("admin_import_players"))
        # Nella scheda Anagrafica vuota si dice dove andare.
        resp = self.client.get(reverse("admin_footballers") + f"?league={self.league.id}&vista=anagrafica")
        self.assertContains(resp, "Listone della lega</a>")

    def test_a_manager_sees_the_listone_without_tools(self):
        self.client.force_login(self.mgr)
        resp = self.client.get(reverse("app_footballers"))
        self.assertContains(resp, "Barella")
        self.assertContains(resp, "Svincolato")
        self.assertNotContains(resp, "Strumenti listone")
        self.assertNotContains(resp, reverse("admin_import_players"))
        self.assertNotContains(resp, "openAssign")

    def test_status_and_role_filters(self):
        self.client.force_login(self.owner)
        url = reverse("app_footballers") + f"?league={self.league.id}&"
        rows = lambda q: [p.name for p in self.client.get(url + q).context["lst_rows"]]
        self.assertEqual(rows("status=free"), ["Barella"])
        self.assertEqual(rows("status=owned"), ["Lautaro"])
        self.assertEqual(rows("role=A"), ["Lautaro"])
        self.assertEqual(rows("sort=-quota"), ["Lautaro", "Barella"])


    def test_rose_import_is_the_same_on_importa_and_listone(self):
        """Un solo «Importa rose squadre» (_rose_import.html) per la pagina
        Importa e per gli strumenti del Listone."""
        self.client.force_login(self.owner)
        q = f"?league={self.league.id}"

        def section(url):
            html = self.client.get(url + q).content.decode()
            start = html.index('<section class="fm-card ri-card" id="rose-import"')
            return html[start:html.index("</section>", start)]
        importa = section(reverse("admin_fantapazz"))
        self.assertEqual(importa, section(reverse("admin_footballers")))
        self.assertIn(f'data-league="{self.league.id}"', importa)
        self.assertIn(reverse("admin_import_rose"), importa)


class OutageTests(ApiTestCase):
    """API-Football ferma: dopo un errore che vale per tutti le chiamate vere
    falliscono subito (niente 15 s di attesa ciascuna) fino a quando riprova
    da sola o un tasto dell'admin la fa riprovare."""

    def _real(self, side_effect):
        calls = []

        def get(*args, **kwargs):
            calls.append(args)
            return side_effect()
        return calls, mock.patch.object(apifootball.requests, "get", get)

    def call(self):
        # get=requests.get come le chiamate vere: solo queste passano dall'interruttore.
        return apifootball._get("/teams", {"league": 135}, get=apifootball.requests.get)

    def test_a_network_failure_fails_fast_until_retry(self):
        import requests

        def boom():
            raise requests.ConnectionError("rete giù")
        calls, patch = self._real(boom)
        with patch:
            with self.assertRaisesRegex(apifootball.ApiFootballError, "non raggiungibile"):
                self.call()
            with self.assertRaisesRegex(apifootball.ApiFootballError, "riprova da sola dopo le"):
                self.call()
            self.assertEqual(len(calls), 1)
            self.assertIn("non raggiungibile", apifootball.status()["outage"]["reason"])
            apifootball.retry_now()
            with self.assertRaises(apifootball.ApiFootballError):
                self.call()
        self.assertEqual(len(calls), 2)

    def test_the_daily_limit_lasts_until_midnight_utc(self):
        calls, patch = self._real(lambda: Resp({"errors": {"requests": "limit reached"}}))
        with patch, self.assertRaisesRegex(apifootball.ApiFootballError, "limite giornaliero"):
            self.call()
        state = apifootball.outage()
        self.assertEqual(state["until"].astimezone(__import__("datetime").timezone.utc).hour, 0)

    def test_an_answer_clears_it_and_a_season_refusal_does_not_trip_it(self):
        apifootball._trip("prova", 60)
        apifootball.retry_now()
        calls, patch = self._real(lambda: Resp({"errors": {"plan": "Free plans do not have access"}}))
        with patch, self.assertRaisesRegex(apifootball.ApiFootballError, "non copre la stagione"):
            self.call()
        self.assertIsNone(apifootball.outage())
        calls, patch = self._real(lambda: Resp({"response": [{"team": {"id": 1}}]}))
        apifootball._trip("prova", 60)
        apifootball.retry_now()
        with patch:
            self.assertEqual(len(self.call()), 1)
        self.assertIsNone(apifootball.outage())

    def test_test_doubles_are_not_guarded(self):
        """Le prove passano il loro ``get``: l'interruttore non le tocca."""
        get = self.fake_get(fail_on=496)
        for _ in range(2):
            with self.assertRaises(apifootball.ApiFootballError):
                apifootball._get("/players/squads", {"team": 496}, get=get)
        self.assertIsNone(apifootball.outage())

    def test_a_page_never_waits_a_minute_for_the_rate_limit(self):
        apifootball._limits["minute"] = 0
        with self.assertRaisesRegex(apifootball.RateLimited, "riprova tra un minuto"):
            apifootball.paced(lambda: None, sleep=apifootball.impatient)

    def test_pages_say_it_is_down_and_what_to_do(self):
        owner = User.objects.create_user("pres_out", password="pw")
        league = League.objects.create(name="Lega Ferma", owner=owner)
        self.client.force_login(owner)
        apifootball._trip("API-Football non raggiungibile da questo server", 300)
        resp = self.client.get(reverse("admin_footballers") + f"?league={league.id}&vista=anagrafica")
        self.assertContains(resp, "API-Football ferma")
        self.assertContains(resp, "file Quotazioni")
        resp = self.client.get(reverse("admin_contracts") + f"?league={league.id}")
        self.assertContains(resp, "API-Football ferma")


class TwinTests(TestCase):
    """Lo stesso calciatore in rosa e fra gli svincolati: le rose arrivano da
    un file («Pulisic», MIL) e il listone dall'anagrafica («C. Pulisic», AC Milan)."""

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user("owner_tw", password="pw")
        self.league = League.objects.create(name="Lega Doppioni", owner=self.owner)
        mk = lambda api_id, name, pos, club: Footballer.objects.create(
            api_id=api_id, name=name, position=pos, club_name=club, club_api_id=api_id)
        mk(1, "C. Pulisic", "C", "AC Milan")
        mk(2, "M. Soulé", "C", "AS Roma")
        mk(3, "G. Orban", "D", "Hellas Verona")
        mk(4, "F. Esposito", "A", "Inter")
        mk(5, "S. Esposito", "A", "Inter")
        footballers.apply_default_listone(self.league)   # la lega nasce con la lista generale
        self.team = Participant.objects.create(display_name="Squadra A", league=self.league,
                                               credits=Decimal("100"))

    def pool(self):
        return Player.objects.filter(league=self.league)

    def make_twin(self, name, role, team):
        """Lo stato lasciato dalle versioni di prima: la copia in rosa, non collegata."""
        return Player.objects.create(name=name, role=role, team=team, league=self.league,
                                     owner=self.team, cost=Decimal("20"))

    def test_rose_import_on_the_default_listone_makes_no_duplicates(self):
        from ..providers.importers import import_rose_data
        import_rose_data([{"name": "Squadra A", "credits": 10, "players": [
            {"name": "Pulisic", "role": "A", "club": "Mil", "cost": 30},   # club «AC Milan», ruolo API C
            {"name": "Soule'", "role": "A", "club": "Rom", "cost": 20},
            {"name": "Orban G.", "role": "A", "club": "Ver", "cost": 10},
        ]}], league=self.league)
        self.assertEqual(self.pool().count(), 5)
        self.assertEqual(self.pool().filter(owner=self.team).count(), 3)
        self.assertEqual(sorted(self.pool().free_agents().values_list("name", flat=True)),
                         ["F. Esposito", "S. Esposito"])

    def test_merge_removes_the_free_copy_and_keeps_the_roster(self):
        pulisic = self.make_twin("Pulisic", "A", "Mil")
        twins = footballers.find_twins(self.league)
        self.assertEqual([(k.pk, f.name) for k, f in twins], [(pulisic.pk, "C. Pulisic")])
        self.assertEqual(footballers.merge_twins(self.league), 1)
        pulisic.refresh_from_db()
        self.assertEqual((pulisic.owner, pulisic.footballer.api_id), (self.team, 1))
        self.assertFalse(self.pool().filter(name="C. Pulisic").exists())
        self.assertEqual(footballers.find_twins(self.league), [])

    def test_the_listone_data_stays_with_the_roster_copy(self):
        pulisic = self.make_twin("Pulisic", "A", "Mil")          # dalla rosa: quotazione = costo
        pulisic.initial_price = pulisic.cost
        pulisic.save()
        Player.objects.filter(league=self.league, footballer__api_id=1).update(
            initial_price=Decimal("28"), ext_id="4321", mantra_roles="T;A", fanta_avg=Decimal("7.10"))
        footballers.merge_twins(self.league)
        pulisic.refresh_from_db()
        self.assertEqual((pulisic.owner, pulisic.cost), (self.team, Decimal("20")))
        self.assertEqual((pulisic.initial_price, pulisic.ext_id, pulisic.mantra_roles, pulisic.fanta_avg),
                         (Decimal("28"), "4321", "T;A", Decimal("7.10")))

    def test_the_anagrafica_update_repairs_the_listone(self):
        self.make_twin("Pulisic", "A", "Mil")
        self.make_twin("Orban G.", "A", "Ver")
        result = footballers.apply_default_listone(self.league)
        self.assertEqual(result["merged"], 2)
        self.assertEqual(self.pool().count(), 5)
        self.assertEqual(self.pool().filter(footballer__api_id=1).get().owner, self.team)
        self.assertEqual(self.pool().free_agents().count(), 3)

    def test_a_free_copy_with_votes_stays_out_of_the_listone(self):
        from ..models import Giornata, PlayerPerformance, Season
        season = Season.objects.create(league=self.league, name="2026/27", is_current=True)
        giornata = Giornata.objects.create(season=season, number=1)
        free = self.pool().get(footballer__api_id=1)
        PlayerPerformance.objects.create(giornata=giornata, player=free, vote=Decimal("6.5"))
        self.make_twin("Pulisic", "A", "Mil")
        footballers.apply_default_listone(self.league)
        free.refresh_from_db()
        self.assertFalse(free.in_listone)
        self.assertFalse(self.pool().free_agents().filter(pk=free.pk).exists())

    def test_doubtful_names_are_not_merged(self):
        self.make_twin("Esposito", "A", "Int")      # F. o S.? Non si sa
        self.assertEqual(footballers.find_twins(self.league), [])

    def test_the_page_says_it_and_the_button_fixes_it(self):
        self.make_twin("Pulisic", "A", "Mil")
        self.client.force_login(self.owner)
        for page in ("admin_footballers", "app_footballers"):
            with self.subTest(page=page):
                resp = self.client.get(reverse(page) + f"?league={self.league.id}")
                self.assertContains(resp, "1 doppione nel listone")
                self.assertContains(resp, "anche se è già in una rosa")
                self.assertContains(resp, "Togli i doppioni")
        resp = self.client.post(reverse("admin_merge_twins"), {"league_id": self.league.id})
        self.assertEqual(resp.json(), {"ok": True, "merged": 1})
        resp = self.client.get(reverse("admin_footballers") + f"?league={self.league.id}")
        self.assertNotContains(resp, "Togli i doppioni")

    def test_only_who_manages_the_league_can_merge(self):
        self.make_twin("Pulisic", "A", "Mil")
        User.objects.create_user("stranger_tw", password="pw")
        self.client.login(username="stranger_tw", password="pw")
        resp = self.client.post(reverse("admin_merge_twins"), {"league_id": self.league.id})
        self.assertNotEqual(resp.status_code, 200)
        self.assertEqual(len(footballers.find_twins(self.league)), 1)

    def test_the_same_free_player_twice_keeps_the_first(self):
        first = self.pool().get(footballer__api_id=2)
        Player.objects.create(name="M. Soulé", role="C", team="AS Roma", league=self.league)   # import doppio
        Player.objects.create(name="Soule' M.", role="C", team="Roma", league=self.league, footballer_id=first.footballer_id)
        self.assertEqual(footballers.merge_twins(self.league), 2)
        self.assertEqual(list(self.pool().filter(team__icontains="roma")), [first])

    def test_namesakes_the_registry_tells_apart_are_not_doubles(self):
        # Un portiere e un attaccante «Martinez» dell'Inter: l'anagrafica li distingue.
        gk = Footballer.objects.create(api_id=20, name="Josep Martínez", position="P", club_name="Inter", club_api_id=4)
        fw = Footballer.objects.create(api_id=21, name="Lautaro Martínez", position="A", club_name="Inter", club_api_id=4)
        Player.objects.create(name="Martinez", role="P", team="Inter", league=self.league, footballer=gk)
        Player.objects.create(name="Martinez", role="A", team="Inter", league=self.league, footballer=fw)
        Player.objects.create(name="Martinez", role="C", team="Inter", league=self.league)   # senza anagrafica, altro ruolo
        self.assertEqual(footballers.find_twins(self.league), [])
        self.assertEqual(footballers.merge_twins(self.league), 0)
        self.assertEqual(self.pool().filter(name="Martinez").count(), 3)

    def test_namesakes_of_different_clubs_are_not_doubles(self):
        Player.objects.create(name="F. Esposito", role="A", team="Spezia", league=self.league)
        self.assertEqual(footballers.find_twins(self.league), [])


class ListoneImportTests(TestCase):
    """Il listone di Fantapazz: chi è all'estero o svincolato ha la squadra vuota."""

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user("owner_li", password="pw")
        self.league = League.objects.create(name="Lega Import", owner=self.owner)

    ROWS = [{"name": "Pulisic", "role": "C", "team": "Milan", "price": "20"},
            {"name": "Lukaku", "role": "A", "team": "Napoli", "price": "15"},
            {"name": "Barella", "role": "C", "team": "Inter", "price": "18"},
            {"name": "Immobile", "role": "A", "team": "", "price": "1"},
            {"name": "Zaniolo", "role": "C", "team": "-", "price": "1"}]

    def test_players_without_a_serie_a_club_are_not_free_agents(self):
        report = sync_players(self.ROWS, league=self.league)
        self.assertEqual((report["created"], report["no_club"]), (3, 2))
        self.assertFalse(Player.objects.filter(league=self.league, name="Immobile").exists())

    def test_an_owned_player_gone_abroad_stays_in_the_roster(self):
        team = Participant.objects.create(display_name="T", league=self.league, credits=Decimal("100"))
        Player.objects.create(name="Immobile", role="A", team="Lazio", league=self.league, owner=team)
        sync_players(self.ROWS, league=self.league)
        immobile = Player.objects.get(league=self.league, name="Immobile")
        self.assertEqual(immobile.owner, team)
        self.assertFalse(immobile.in_listone)

    def test_a_file_without_clubs_is_taken_whole(self):
        rows = [{"name": r["name"], "role": r["role"], "team": "", "price": "1"} for r in self.ROWS]
        self.assertEqual(sync_players(rows, league=self.league)["created"], 5)

    def test_importing_twice_makes_no_doubles(self):
        sync_players(self.ROWS, league=self.league)
        sync_players(self.ROWS, league=self.league)
        self.assertEqual(Player.objects.filter(league=self.league).count(), 3)

    def test_a_second_import_while_the_first_runs_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.force_login(self.owner)
        cache.add(f"listone-import-{self.league.id}", 1, 60)   # il primo è ancora in corso
        upload = SimpleUploadedFile("listone.csv", b"Nome;Ruolo;Squadra;Quotazione\nPulisic;C;Milan;20\n")
        resp = self.client.post(reverse("admin_import_players"), {"csv_file": upload, "league_id": self.league.id})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("già in corso", resp.json()["error"])
        self.assertFalse(Player.objects.filter(league=self.league).exists())
        cache.delete(f"listone-import-{self.league.id}")
        upload.seek(0)
        resp = self.client.post(reverse("admin_import_players"), {"csv_file": upload, "league_id": self.league.id})
        self.assertEqual((resp.json()["ok"], resp.json()["created"]), (True, 1))
