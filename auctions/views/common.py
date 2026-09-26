"""Shared helpers, decorators, and context builders for HTTP views."""
from functools import wraps

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from .. import remote, services
from ..models import Auction, League, Participant, Player


def staff_member_required(view):
    """Enforces user authentication for administrative and regia views.

    1. Unauthenticated users are redirected to login (or receive HTTP 401 for AJAX/POST).
    2. Requests arriving through an internet tunnel additionally require the regia PIN gate.
    """
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not getattr(request, "user", None) or not request.user.is_authenticated:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest"                     or request.method == "POST":
                return JsonResponse({"ok": False, "error": "unauthenticated"}, status=401)
            return redirect(f"{reverse('login')}?next={request.get_full_path()}")

        if remote.request_is_remote(request) and not request.session.get("regia_unlocked"):
            if request.headers.get("X-Requested-With") == "XMLHttpRequest"                     or request.method == "POST":
                return JsonResponse({"ok": False, "error": "regia_locked"}, status=403)
            return redirect(f"{reverse('regia_unlock')}?next={request.get_full_path()}")
        return view(request, *args, **kwargs)
    return wrapped


# --- Shared helpers ---------------------------------------------------------

def participant_join_url(request, participant, auction=None, base=None):
    """Absolute, tokenised join link that recognises the team automatically.

    Built on the most useful address available (see ``remote.best_base_url``):
    the public tunnel when it is open, otherwise this machine's LAN IP — the
    regia opens the console on ``localhost``, and a ``localhost`` link is useless
    on somebody else's phone.

    Naming an ``auction`` makes the link land straight on that auction's bidding
    page: scanning a team's QR should put that manager in the auction, not on a
    menu.
    """
    return (base or remote.best_base_url(request)) + _join_path(participant, auction)


def _join_path(participant, auction=None):
    path = reverse("join") + "?t=" + (participant.public_token or "")
    if auction is not None:
        path += f"&a={auction.id if hasattr(auction, 'id') else auction}"
    return path


def participant_lan_join_url(request, participant, auction=None):
    """The same join link on the *wifi* address, or "" when it adds nothing.

    With the tunnel open every handed-out link points at the public address —
    correct for managers who are elsewhere, a needless detour for the phones in
    the room. This is the local twin to give them instead.
    """
    lan = remote.lan_url(request)
    if not lan or lan.rstrip("/") == remote.best_base_url(request).rstrip("/"):
        return ""
    return lan + _join_path(participant, auction)


def broadcast_state(auction):
    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(
        f"auction_{auction.id}",
        {"type": "state.update", "state": services.serialize_state(auction)},
    )


def _refund_mode(request, default=Auction.RefundMode.PURCHASE):
    """Read+validate the svincolo refund policy from a POST (creation/edit)."""
    m = request.POST.get("release_refund_mode", default)
    return m if m in Auction.RefundMode.values else default


def _opening_price_mode(request, default=Auction.OpeningPriceMode.QUOTAZIONE):
    """Read+validate the per-player opening price policy from a POST."""
    m = request.POST.get("opening_price_mode", default)
    return m if m in Auction.OpeningPriceMode.values else default


def _flow_mode(request, default=Auction.FlowMode.CALL):
    m = request.POST.get("flow_mode", default)
    return m if m in Auction.FlowMode.values else default


def _call_order(request, default=Auction.CallOrder.PDCA):
    m = request.POST.get("call_order", default)
    return m if m in Auction.CallOrder.values else default


def _within_role(request, default=Auction.WithinRole.QUOTA):
    m = request.POST.get("within_role_order", default)
    return m if m in Auction.WithinRole.values else default


# --- Current auction (the console's thread through the whole product) -------

SESSION_AUCTION_KEY = "current_auction_id"
SESSION_LEAGUE_KEY  = "selected_league_id"


def _remember_auction(request, auction):
    """Pin the auction the console is working on to the browser session.

    An auction evening is one long task: once it is running, walking off to the
    listone or to the teams page and coming back must land on that auction, not
    on the start screen. The pin is dropped when the auction is closed (or gone),
    which is exactly when the start screen is the right place to be again.
    """
    if auction is None:
        request.session.pop(SESSION_AUCTION_KEY, None)
    elif auction.status == Auction.Status.CLOSED:
        request.session.pop(SESSION_AUCTION_KEY, None)
    else:
        request.session[SESSION_AUCTION_KEY] = auction.id
        if auction.league_id:
            request.session[SESSION_LEAGUE_KEY] = auction.league_id


def target_league(request):
    """The league an action writes into or the console is scoped to.

    Resolution order:
    1. Explicit GET/POST: ?league=all, 0 or none resets the session tenant (returns None).
    2. Explicit GET/POST numeric id: updates session[SESSION_LEAGUE_KEY] and returns the League.
    3. Session: request.session[SESSION_LEAGUE_KEY].
    4. Auction pinned in session: its league (if still valid).
    5. Single league fallback: if and only if exactly 1 league exists in DB.
    6. Returns None (multi-tenant hub / unselected state).
    """
    for raw in (request.POST.get("league_id"), request.GET.get("league")):
        raw = (raw or "").strip()
        if raw.lower() in ("all", "0", "none"):
            request.session.pop(SESSION_LEAGUE_KEY, None)
            request.session.pop(SESSION_AUCTION_KEY, None)
            return None
        if raw.isdigit():
            league = League.objects.filter(pk=int(raw)).first()
            if league is not None:
                # Restrict to owned leagues if user is an authenticated normal league admin
                user = getattr(request, "user", None)
                if user and user.is_authenticated and not user.is_superuser:
                    if league.owner_id is not None and league.owner_id != user.id:
                        return None
                request.session[SESSION_LEAGUE_KEY] = league.id
                # Check if pinned auction belongs to a different league
                pinned = request.session.get(SESSION_AUCTION_KEY)
                if pinned:
                    auc = Auction.objects.filter(pk=pinned).first()
                    if auc is not None and auc.league_id != league.id:
                        request.session.pop(SESSION_AUCTION_KEY, None)
                return league

    sess_lg_id = request.session.get(SESSION_LEAGUE_KEY)
    if sess_lg_id:
        league = League.objects.filter(pk=sess_lg_id).first()
        if league is not None:
            user = getattr(request, "user", None)
            if user and user.is_authenticated and not user.is_superuser:
                if league.owner_id is not None and league.owner_id != user.id:
                    request.session.pop(SESSION_LEAGUE_KEY, None)
                    return None
            return league
        request.session.pop(SESSION_LEAGUE_KEY, None)

    pinned = request.session.get(SESSION_AUCTION_KEY)
    if pinned:
        auction = Auction.objects.filter(pk=pinned).select_related("league").first()
        if auction is not None and auction.league_id:
            user = getattr(request, "user", None)
            if user and user.is_authenticated and not user.is_superuser:
                if auction.league and auction.league.owner_id is not None and auction.league.owner_id != user.id:
                    request.session.pop(SESSION_AUCTION_KEY, None)
                    return None
            request.session[SESSION_LEAGUE_KEY] = auction.league_id
            return auction.league

    user = getattr(request, "user", None)
    if user and user.is_authenticated and not user.is_superuser:
        owned = list(League.objects.filter(owner=user)[:2])
        if len(owned) == 1:
            request.session[SESSION_LEAGUE_KEY] = owned[0].id
            return owned[0]
        return None

    leagues = list(League.objects.all()[:2])
    if len(leagues) == 1:
        request.session[SESSION_LEAGUE_KEY] = leagues[0].id
        return leagues[0]
    return None


def user_can_manage_league(user, league):
    """True when ``user`` may administer ``league``.

    Superusers manage everything; a league admin manages the leagues they own.
    Legacy leagues without an owner stay manageable by any staff user, the same
    rule ``target_league`` applies.
    """
    if league is None or user is None or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return league.owner_id is None or league.owner_id == user.id


def manageable_leagues(user):
    """Leagues listed in the console pickers for ``user``."""
    qs = League.objects.all()
    if not user.is_superuser:
        qs = qs.filter(Q(owner=user) | Q(owner__isnull=True))
    return qs.order_by("name")


FORBIDDEN_LEAGUE_MSG = "Non hai i permessi per gestire questa lega."


def user_can_manage_scope(user, league):
    """``user_can_manage_league``, where ``league`` None means the global pool.

    Players and teams without a league (legacy single-league data, or left
    behind by a deleted league) have no owner to delegate them to, so only
    superusers may read or write them.
    """
    if league is None:
        return bool(user is not None and user.is_authenticated and user.is_superuser)
    return user_can_manage_league(user, league)


def league_scope_or_403(request, raw_id, fallback=None):
    """The league a form writes into, checked: ``(league, None)`` or
    ``(None, error response)``.

    ``raw_id`` is the posted league pk. A blank one means ``fallback`` — the
    global pool when that is None, which only a superuser may touch. A pk that
    names no league is a 404, never a silent fall back to the global pool.
    """
    raw_id = str(raw_id or "").strip()
    if raw_id:
        league = League.objects.filter(pk=int(raw_id)).first() if raw_id.isdigit() else None
        if league is None:
            return None, JsonResponse({"ok": False, "error": "Lega non trovata."}, status=404)
    else:
        league = fallback
    if not user_can_manage_scope(request.user, league):
        return None, JsonResponse({"ok": False, "error": FORBIDDEN_LEAGUE_MSG}, status=403)
    return league, None


def current_league(request):
    """The league the console is showing. In a multi-tenant setup with >1 league
    and no selection made, returns None so callers can display the tenant hub/picker."""
    return target_league(request)


def current_auction(request, league=None):
    """The auction the console is on: ?auction= wins, else the pinned one.

    Falls back to the league's live/ready auction only when a league is explicitly
    given or scoped, preventing cross-tenant auction bleed.
    """
    raw = (request.GET.get("auction") or "").strip()
    if raw.isdigit():
        auc = Auction.objects.filter(pk=int(raw)).first()
        if auc is not None and (league is None or auc.league_id == league.id):
            return auc

    pinned = request.session.get(SESSION_AUCTION_KEY)
    if pinned:
        auction = Auction.objects.filter(pk=pinned).exclude(
            status=Auction.Status.CLOSED).first()
        if auction is not None and (league is None or auction.league_id == league.id):
            return auction
        elif auction is not None and league is not None and auction.league_id != league.id:
            request.session.pop(SESSION_AUCTION_KEY, None)

    if league is not None:
        return (Auction.objects.filter(league=league, status=Auction.Status.LIVE).first()
                or Auction.objects.filter(league=league, status=Auction.Status.PAUSED).first())
    return None


def _session_participant(request):
    pid = request.session.get("participant_id")
    if pid:
        p = Participant.objects.filter(pk=pid, is_active=True).first()
        if p:
            return p
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        p = Participant.objects.filter(user=user, is_active=True).first()
        if p:
            request.session["participant_id"] = p.id
            request.session["display_name"] = p.display_name
            if p.league_id is not None:
                request.session[SESSION_LEAGUE_KEY] = p.league_id
            return p
    return None


_ROLE_LABELS = [("P", "Portieri"), ("D", "Difensori"), ("C", "Centrocampisti"), ("A", "Attaccanti")]


def _app_active_auction(league):
    """The auction a manager can jump into for this league: LIVE/PAUSED first,
    then READY. Returns None when there's nothing joinable."""
    qs = Auction.objects.all()
    if league is not None:
        qs = qs.filter(league=league)
    else:
        qs = qs.filter(league__isnull=True)
    order = {"LIVE": 0, "PAUSED": 1, "READY": 2}
    joinable = sorted(
        (a for a in qs if a.status in order),
        key=lambda a: (order[a.status], -a.id),
    )
    return joinable[0] if joinable else None


def _app_standings(league, me_id):
    """League teams ranked by roster size then remaining credits — a colpo
    d'occhio until real league scoring exists."""
    qs = Participant.objects.filter(is_active=True)
    qs = qs.filter(league=league) if league is not None else qs.filter(league__isnull=True)
    owned_counts = {}
    for pid in Player.objects.filter(owner__in=qs).values_list("owner_id", flat=True):
        owned_counts[pid] = owned_counts.get(pid, 0) + 1
    rows = [{
        "name": p.display_name,
        "owned": owned_counts.get(p.id, 0),
        "spent": p.spent_credits,
        "remaining": p.remaining_credits,
        "is_me": p.id == me_id,
    } for p in qs]
    rows.sort(key=lambda r: (-r["owned"], -float(r["remaining"]), r["name"].lower()))
    return rows


def app_admin_leagues(user):
    """Leagues ``user`` runs from the console — the same set /dashboard/ lists:
    every league for a superuser, the ones they own for a league admin."""
    if user is None or not user.is_authenticated:
        return []
    qs = League.objects.all() if user.is_superuser else League.objects.filter(owner=user)
    return list(qs.order_by("name"))


def app_admin_league(request, admin_leagues, participant=None):
    """The league the app's Regia works on, or None when the user runs none.

    ``?league=`` wins and is remembered, so the console follows the app (and
    vice versa: both read the same session key). Then the league the console
    was last on, then the one of the team being viewed, then the first.
    """
    if not admin_leagues:
        return None
    by_id = {lg.id: lg for lg in admin_leagues}
    raw = (request.GET.get("league") or "").strip()
    if raw.isdigit() and int(raw) in by_id:
        request.session[SESSION_LEAGUE_KEY] = int(raw)
        return by_id[int(raw)]
    sess = request.session.get(SESSION_LEAGUE_KEY)
    if sess in by_id:
        return by_id[sess]
    if participant is not None and participant.league_id in by_id:
        return by_id[participant.league_id]
    return admin_leagues[0]


def _app_ctx(request, active_tab):
    """Shared shell context. Returns (participant, ctx).

    ``ctx`` is None only when there's nobody to show the app to (caller sends
    them to the login). A league admin without a team of their own still gets a
    context — with ``participant`` None — so the app can open on the Regia
    instead of asking them to log in as somebody's team.
    """
    participant = _session_participant(request)
    user = getattr(request, "user", None)
    admin_leagues = app_admin_leagues(user)
    if participant is None and not admin_leagues:
        return None, None
    if participant is not None:
        league = participant.league
    else:
        league = app_admin_league(request, admin_leagues)
    admin_ids = {lg.id for lg in admin_leagues}
    return participant, {
        "participant": participant,
        "app_league": league,
        "active_tab": active_tab,
        "active_auction": _app_active_auction(league),
        # The Regia tab and the "Console" door show up for league admins only.
        "is_app_admin": bool(admin_leagues),
        "admin_leagues": admin_leagues,
        "manages_app_league": league is not None and league.id in admin_ids,
        # An admin looking at the app through a team that isn't theirs: the
        # shell says so, and offers the way back to the Regia.
        "viewing_as": bool(
            admin_leagues and participant is not None
            and participant.league_id in admin_ids
            and participant.user_id != getattr(user, "id", None)
        ),
    }


def version_status_api(request):
    """Public API endpoint returning the running build version and active engine details."""
    from liveauction import __version__
    from django.db import connection
    return JsonResponse({
        "app": "FantaManager",
        "version": __version__,
        "database_engine": connection.vendor,
        "features": [
            "hybrid_league_cockpit",
            "clean_league_urls",
            "roster_by_role_pdca",
            "team_quick_actions",
            "postgresql_support",
        ],
    })
