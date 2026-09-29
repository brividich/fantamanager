"""Configuration and session management views."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import services
from ..models import Auction, AuctionSession, League
from .admin_wizards import _game_mode
from .common import (
    current_auction,
    manageable_leagues,
    managed_or_403,
    staff_member_required,
    target_league,
    user_can_manage_league,
    user_can_manage_scope,
)


def _config_leagues(user):
    """Leagues the config page shows to ``user``: all for a superuser, else the
    ones they may manage (owned, plus legacy leagues without an owner)."""
    return manageable_leagues(user)


def _can_delete_league(user, league):
    """Deleting takes a whole league with it: only its owner or a superuser,
    never "any staff user" on an ownerless legacy league."""
    return league is not None and (user.is_superuser or league.owner_id == user.id)


@staff_member_required
def admin_config(request):
    """Le impostazioni delle leghe in un posto solo: nome, budget, rosa,
    sistema di gioco e regole (scambi, contratti, tetto), più la manutenzione
    — aste e sessioni salvate da ripulire, leghe vuote da togliere.

    Mostra solo le leghe che l'utente gestisce: un admin di lega non vede (né
    può toccare) quelle degli altri."""
    user = request.user
    leagues_qs = _config_leagues(user)
    leagues = list(leagues_qs)
    league_ids = [lg.id for lg in leagues]
    current_league = target_league(request)
    if current_league is not None and current_league.id not in league_ids:
        current_league = None

    rows = services.league_overview(leagues_qs)
    for r in rows:
        r["can_delete"] = _can_delete_league(user, r["league"])
        r["is_current"] = current_league is not None and r["league"].id == current_league.id
    # The league the user came from goes first: it's the one they want to edit.
    rows.sort(key=lambda r: (not r["is_current"], r["league"].name.lower()))

    scope = Q(league_id__in=league_ids)
    if user.is_superuser:
        scope |= Q(league__isnull=True)
    auctions = list(Auction.objects.filter(scope).select_related("league")
                    .annotate(bid_count=Count("bids")).order_by("-id"))
    sessions = []
    for s in AuctionSession.objects.filter(scope).select_related("league").order_by("-created_at"):
        data = s.data or {}
        s.n_teams = len(data.get("participants") or [])
        s.n_pool = len(data.get("pool") or [])
        sessions.append(s)

    empties = [r["league"].id for r in rows if not r["pool"] and not r["auctions"] and r["can_delete"]]

    return render(request, "auctions/admin_config.html", {
        "leagues": leagues,
        "current_league": current_league,
        "rows": rows,
        "auctions": auctions,
        "sessions": sessions,
        "empties": empties,
        "game_modes": League.GameMode.choices,
        "console_section": "Impostazioni",
        "console_active": "config",
        "selected": current_auction(request, current_league),
    })


def _config_back(request, league_id=None):
    """Back to the config page, on the tab/league the form came from."""
    url = reverse("admin_config")
    tab = request.POST.get("tab") or ""
    if league_id:
        url += f"?league={league_id}"
    return redirect(url + (f"#{tab}" if tab else (f"#lg-{league_id}" if league_id else "")))


@staff_member_required
@require_POST
def admin_config_action(request):
    """One POST endpoint for the config page: edit / delete / clean up."""
    user = request.user
    action = request.POST.get("action", "")

    if action == "delete_league":
        league = League.objects.filter(pk=request.POST.get("league_id")).first()
        if league is None:
            messages.error(request, "Lega non trovata.")
        elif not _can_delete_league(user, league):
            messages.error(request, "Puoi eliminare solo le leghe di cui sei proprietario.")
        else:
            report = services.delete_league(league.id)
            messages.success(request, (
                f"Lega «{report['name']}» eliminata: {report['auctions']} aste, "
                f"{report['teams']} squadre, {report['players']} giocatori, "
                f"{report['sessions']} sessioni."))
        return _config_back(request)

    if action == "delete_auction":
        auction = Auction.objects.filter(pk=request.POST.get("auction_id")).select_related("league").first()
        allowed = auction is not None and (
            user_can_manage_league(user, auction.league) if auction.league_id else user.is_superuser)
        if auction is None:
            messages.error(request, "Asta non trovata.")
        elif not allowed:
            messages.error(request, "Non hai i permessi per eliminare questa asta.")
        else:
            report = services.delete_auction(auction.id)
            messages.success(request, (
                f"Asta «{report['title']}» eliminata ({report['bids']} offerte). "
                "Lega, squadre e listone restano."))
        return _config_back(request)

    if action == "delete_session":
        session = AuctionSession.objects.filter(pk=request.POST.get("session_id")).select_related("league").first()
        allowed = session is not None and (
            user_can_manage_league(user, session.league) if session.league_id else user.is_superuser)
        if session is None:
            messages.error(request, "Sessione non trovata.")
        elif not allowed:
            messages.error(request, "Non hai i permessi per eliminare questa sessione.")
        else:
            name = session.name
            session.delete()
            messages.success(request, f"Sessione «{name}» eliminata.")
        return _config_back(request)

    if action == "clean_empty":
        gone = []
        for row in services.league_overview(_config_leagues(user)):
            if not row["pool"] and not row["auctions"] and _can_delete_league(user, row["league"]):
                report = services.delete_league(row["league"].id)
                if report:
                    gone.append(report["name"])
        messages.success(
            request,
            f"Ripulite {len(gone)} leghe vuote: {', '.join(gone)}." if gone
            else "Nessuna lega vuota da ripulire.")
        return _config_back(request)

    if action in ("rename_league", "update_league"):
        league = League.objects.filter(pk=request.POST.get("league_id")).first()
        if league is None:
            messages.error(request, "Lega non trovata.")
            return _config_back(request)
        if not user_can_manage_league(user, league):
            messages.error(request, "Non hai i permessi per modificare questa lega.")
            return _config_back(request)

        def pint(name, default):
            try:
                return max(0, int(request.POST.get(name) or default))
            except (TypeError, ValueError):
                return default
        league.name = (request.POST.get("name") or league.name).strip()[:120] or league.name
        try:
            budget = Decimal(str(request.POST.get("budget") or league.budget).replace(",", "."))
            if budget >= 0:
                league.budget = budget
        except (InvalidOperation, ValueError):
            pass
        league.slot_limits = request.POST.get("slot_limits", "1") != "0"
        league.slots_p = pint("slots_p", league.slots_p)
        league.slots_d = pint("slots_d", league.slots_d)
        league.slots_c = pint("slots_c", league.slots_c)
        league.slots_a = pint("slots_a", league.slots_a)
        new_mode = _game_mode(request.POST.get("game_mode"), league.game_mode)
        if new_mode != league.game_mode:
            if getattr(league, "is_locked_style", False):
                messages.error(request, "Non è possibile cambiare stile (Classic/Mantra) a stagione o asta avviata.")
            else:
                league.game_mode = new_mode
        league.slots_out = pint("slots_out", league.slots_out)
        # The rule switches only move when the form actually carried them (an
        # unchecked box is simply absent from a POST).
        if request.POST.get("rules_present") == "1":
            for flag in ("trades_enabled", "trades_need_approval", "trades_same_roles",
                         "contracts_enabled", "salary_cap_enabled"):
                setattr(league, flag, request.POST.get(flag) == "1")
        league.save()
        messages.success(request, f"Lega «{league.name}» aggiornata.")
        return _config_back(request, league.id)

    messages.error(request, "Azione sconosciuta.")
    return _config_back(request)


def _manageable_sessions(user):
    """Saved sessions ``user`` may browse and resume: those of the leagues they
    manage, plus — for a superuser — the ones whose league is gone."""
    scope = Q(league__in=manageable_leagues(user))
    if user.is_superuser:
        scope |= Q(league__isnull=True)
    return AuctionSession.objects.filter(scope)


@staff_member_required
def admin_sessions(request):
    """List saved sessions (GET) for browsing and resuming."""
    leagues = list(manageable_leagues(request.user))
    current_league = leagues[0] if leagues else None
    return render(request, "auctions/admin_sessions.html", {
        "sessions": _manageable_sessions(request.user).select_related("league", "source_auction"),
        "leagues": leagues,
        "current_league": current_league,
        "selected": current_auction(request, current_league),
        "console_section": "Sessioni salvate",
        "console_active": "",
    })


@staff_member_required
@require_POST
def admin_save_session(request, auction_id):
    """Snapshot the current league standings of an auction into a session."""
    auction, denied = managed_or_403(request, Auction, auction_id)
    if denied:
        return denied
    session = services.save_session(
        auction.id,
        name=request.POST.get("name", "").strip(),
        created_by=request.user.get_username(),
        notes=request.POST.get("notes", "").strip(),
    )
    return redirect(f"/dashboard/sessions/?saved={session.id}")


def _after_resume(auction):
    """Where to land after rebuilding an auction from a session."""
    if not services.listone_loaded(auction):
        return redirect(
            f"{reverse('admin_players')}?league={auction.league_id}&need_listone=1&from=resume"
        )
    return redirect(f"/dashboard/?auction={auction.id}")


def _resume(request, session):
    """Rebuild ``session``; the new league keeps the source league's owner, or
    goes to whoever resumed it — never ownerless, open to every account."""
    owner = session.league.owner if session.league_id and session.league.owner_id else request.user
    return services.resume_session(
        session.id, created_by=request.user.get_username(), owner=owner)


@staff_member_required
@require_POST
def admin_resume_session(request, session_id):
    """Rebuild a fresh, playable auction from a saved session."""
    session = get_object_or_404(AuctionSession.objects.select_related("league"), pk=session_id)
    # A session whose league is gone (league=None) belongs to superusers only.
    if not user_can_manage_scope(request.user, session.league):
        return HttpResponseForbidden("Non hai i permessi per riprendere questa sessione.")
    return _after_resume(_resume(request, session))


@staff_member_required
@require_POST
def admin_resume_latest(request):
    """One-click 'Riprendi sessione' — resume the most recent saved session
    among those the user may manage."""
    latest = (_manageable_sessions(request.user).select_related("league")
              .order_by("-created_at").first())
    if latest is None:
        return redirect("admin_sessions")
    return _after_resume(_resume(request, latest))
