"""Admin participant management: roster listing, participant creation/edit, QR codes,
and the coaches' portal accounts."""
import io
import secrets
from decimal import Decimal, InvalidOperation

try:
    import qrcode
except ImportError:
    qrcode = None

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import Auction, League, ManagedAccount, Participant, Player
from .. import remote, team_sheets
from ..services import mail
from .common import (
    FORBIDDEN_LEAGUE_MSG,
    current_auction,
    linkable_users,
    manageable_leagues,
    league_scope_or_403,
    managed_or_403,
    mixed_leagues,
    participant_join_url,
    participant_lan_join_url,
    safe_next,
    staff_member_required,
    target_league,
    user_can_manage_league,
    user_can_manage_scope,
)


@staff_member_required
def admin_participants(request):
    """Teams of one league: credits, roster size and the join link/QR to hand out.

    Every row carries the team's tokenised join link, which signs whoever
    opens it in as that team: only a user who manages the league sees them.
    """
    leagues = manageable_leagues(request.user)
    current_league = None
    raw = (request.GET.get("league") or "").strip()
    if raw.isdigit():
        current_league = League.objects.filter(pk=int(raw)).first()
        if current_league is not None and not user_can_manage_league(request.user, current_league):
            return HttpResponseForbidden(FORBIDDEN_LEAGUE_MSG)
    if current_league is None:
        current_league = target_league(request)

    participants = Participant.objects.all().order_by("display_name")
    auctions = Auction.objects.exclude(status=Auction.Status.DRAFT)
    if current_league is not None:
        participants = participants.filter(league=current_league)
        auctions = auctions.filter(league=current_league)
    elif not user_can_manage_scope(request.user, None):
        # No league picked: listing every team of every league is for superusers.
        participants = participants.none()
        auctions = auctions.none()

    # Bake the league's current auction into the links/QR so scanning drops the
    # manager straight into it. A running auction wins over one still to start.
    target = (auctions.filter(status=Auction.Status.LIVE).first()
              or auctions.filter(status=Auction.Status.READY).first())

    # The wifi/internet twin links only matter while an auction is running:
    # that is the one evening the phones in the room need the local door.
    live = target is not None and target.status == Auction.Status.LIVE

    accounts_ok = current_league is not None and can_manage_accounts(request.user, current_league)
    participants = participants.select_related("user", "user__managed_account")
    honour_labels = team_sheets.league_honour_labels(current_league)

    rows = []
    for p in participants:
        owned = Player.objects.filter(owner=p)
        counts = {r: 0 for r in ("P", "D", "C", "A")}
        for role in owned.values_list("role", flat=True):
            if role in counts:
                counts[role] += 1
        slots = current_league.total_slots if current_league else 0
        rows.append({
            "p": p,
            "roster": sum(counts.values()),
            "roster_pct": int(100 * sum(counts.values()) / slots) if slots else 0,
            "counts": counts,
            "join_url": participant_join_url(request, p, target),
            "lan_join_url": participant_lan_join_url(request, p, target) if live else "",
            "account": p.user if accounts_ok else None,
            "account_lock": account_lock_reason(request.user, p.user) if accounts_ok and p.user else "",
            "account_deletable": accounts_ok and p.user is not None and account_deletable(request.user, p.user),
            "honours": team_sheets.honours_for(p, honour_labels),
            "images": [("logo", "Logo", p.logo), ("kit_home", "Prima maglia", p.kit_home),
                       ("kit_away", "Seconda maglia", p.kit_away)],
        })

    # A password the server generated is shown once, on the page the action
    # lands on, and then forgotten: only the login's hash is stored.
    secret = request.session.pop(SESSION_ACCOUNT_SECRET_KEY, None) if accounts_ok else None

    return render(request, "auctions/admin_participants.html", {
        "leagues": leagues,
        "current_league": current_league,
        "rows": rows,
        "accounts_ok": accounts_ok,
        "account_secret": secret,
        "portal_login_url": remote.best_base_url(request).rstrip("/") + reverse("app_login"),
        "target_auction": target,
        "remote_on": live and remote.is_on(),
        "console_section": "Squadre",
        "console_active": "teams",
        "selected": current_auction(request, current_league),
        "mail_ready": mail.is_ready(),
        "reachable": sum(1 for r in rows if r["p"].contact_email and r["p"].is_active),
    })


@staff_member_required
@require_POST
def admin_participant_email(request, participant_id):
    """Set (or clear) the address the league writes to for a team; with
    ``invite=1`` also send the team its personal link right away."""
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    back = safe_next(request, reverse("admin_participants") + (f"?league={p.league_id}" if p.league_id else ""))
    if "email" in request.POST:
        email = (request.POST.get("email") or "").strip()
        if email:
            try:
                validate_email(email)
            except ValidationError:
                messages.error(request, f"«{email}» non è un indirizzo email valido.")
                return redirect(back)
        if email != p.email:
            p.email = email
            p.save(update_fields=["email"])
            messages.success(request, f"Email di «{p.display_name}» {'salvata' if email else 'rimossa'}.")
    if request.POST.get("invite") == "1":
        if not p.contact_email:
            messages.error(request, f"«{p.display_name}» non ha un indirizzo email.")
        elif not mail.is_ready():
            messages.error(request, "La posta non è configurata: impostala in Impostazioni → Posta.")
        elif p.league is None:
            messages.error(request, "La squadra non è in nessuna lega.")
        else:
            report = mail.send_team_invites(request, p.league, [p])
            (messages.success if report["sent"] else messages.error)(
                request, f"Invito a «{p.display_name}»: " + mail.report_message(report, "invito").replace("invito inviata", "invito inviato"))
    return redirect(back)


@staff_member_required
@require_POST
def admin_invite_teams(request):
    """Email every team of the league its personal app link and code."""
    league, denied = league_scope_or_403(request, request.POST.get("league_id"))
    if denied:
        return denied
    if league is None:
        messages.error(request, "Scegli prima la lega da invitare.")
        return redirect("admin_participants")
    back = safe_next(request, reverse("admin_participants") + f"?league={league.id}")
    if not mail.is_ready():
        messages.error(request, "La posta non è configurata: impostala in Impostazioni → Posta.")
        return redirect(back)
    report = mail.send_team_invites(request, league)
    (messages.success if report["sent"] and not report["failed"] else messages.warning)(
        request, "Inviti: " + mail.report_message(report))
    return redirect(back)


@staff_member_required
@require_POST
def admin_create_participant(request):
    """Create a team **inside the league the console is on**.

    The form posts ``league_id``; without it a team used to be born with
    ``league=None``, which made it invisible to every league-scoped view — the
    team simply disappeared. When the id is missing we fall back to the only
    league that exists, and refuse (with a message) when the choice would be a
    guess between several.
    """
    def dec(name, default):
        try:
            return Decimal(request.POST.get(name) or default)
        except (InvalidOperation, ValueError):
            return Decimal(default)

    league = target_league(request)
    fallback = f"/dashboard/{league.id}/#rose" if league else "/dashboard/"
    if league is None and League.objects.exists():
        messages.error(request, "Scegli prima la lega in cui creare la squadra.")
        return redirect(safe_next(request, fallback))
    if not user_can_manage_scope(request.user, league):     # the global pool: superadmin only
        return HttpResponseForbidden(FORBIDDEN_LEAGUE_MSG)

    p = Participant(
        league=league,
        display_name=request.POST.get("display_name", "").strip()[:80] or "Squadra",
        access_code=request.POST.get("access_code", "").strip()[:20],
        credits=dec("credits", str(league.budget) if league else "500"),
        is_active=True,
    )
    if "logo" in request.FILES:
        p.logo = request.FILES["logo"]

    new_user_username = (request.POST.get("new_user_username") or "").strip()
    if new_user_username and can_manage_accounts(request.user, league):
        username, error = _clean_username(new_user_username)
        if error:
            messages.error(request, error)
            return redirect(safe_next(request, fallback))
        email, error = _clean_email(request.POST.get("new_user_email"))
        if error:
            messages.error(request, error)
            return redirect(safe_next(request, fallback))
        password = request.POST.get("new_user_password") or ""
        generated = not password
        if generated:
            password = generate_password()
        elif len(password) < MIN_PASSWORD_LENGTH:
            messages.error(request, f"La password deve contenere almeno {MIN_PASSWORD_LENGTH} caratteri.")
            return redirect(safe_next(request, fallback))

        User = get_user_model()
        with transaction.atomic():
            account = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                first_name=(request.POST.get("new_user_first_name") or "").strip()[:150],
            )
            ManagedAccount.objects.create(user=account, created_by=request.user)
            p.user = account
            p.save()
        _remember_secret(request, p, account, password)
        messages.success(request, f"Squadra «{p.display_name}» creata e associata al nuovo account «{account.username}».")
        return redirect(safe_next(request, fallback))

    user_id = request.POST.get("user_id")
    if user_id and user_id.isdigit():
        usr = linkable_users(request.user).filter(pk=int(user_id)).first()
        if usr:
            p.user = usr

    p.save()
    if league is not None:
        messages.success(request, f"Squadra «{p.display_name}» aggiunta a {league.name}.")
    return redirect(safe_next(request, fallback))


@staff_member_required
@require_POST
def admin_edit_participant(request, participant_id):
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied

    def dec(name, default):
        try:
            return Decimal(request.POST.get(name) or default)
        except (InvalidOperation, ValueError):
            return Decimal(default)

    p.display_name = request.POST.get("display_name", p.display_name).strip()[:80]
    p.access_code  = request.POST.get("access_code", p.access_code).strip()[:20]
    p.credits      = dec("credits", str(p.credits))
    p.is_active    = request.POST.get("is_active") == "1"
    
    user_id = request.POST.get("user_id")
    if user_id == "none" or user_id == "":
        p.user = None
    elif user_id and user_id.isdigit():
        usr = linkable_users(request.user).filter(pk=int(user_id)).first()
        if usr:
            p.user = usr

    if "logo" in request.FILES:
        p.logo = request.FILES["logo"]
    elif request.POST.get("clear_logo") == "1":
        p.logo = None
    p.save()
    messages.success(request, f"Squadra «{p.display_name}» aggiornata con successo.")
    fallback = f"/dashboard/{p.league_id}/#rose" if p.league_id else "/dashboard/"
    return redirect(safe_next(request, fallback))


@staff_member_required
@require_POST
def admin_delete_participant(request, participant_id):
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    league_id = p.league_id
    team_name = p.display_name
    p.delete()
    messages.success(request, f"Squadra «{team_name}» eliminata.")
    fallback = f"/dashboard/{league_id}/#rose" if league_id else "/dashboard/"
    return redirect(safe_next(request, fallback))


@staff_member_required
@require_POST
def admin_adjust_team_credits(request, participant_id):
    """Adjust credits for a team: add bonus, subtract malus, or set absolute budget."""
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    mode = request.POST.get("mode", "add")  # "add", "sub", "set"
    raw_amount = request.POST.get("amount", "0")
    try:
        val = Decimal(raw_amount)
    except (InvalidOperation, ValueError):
        val = Decimal("0")

    if mode == "add":
        p.credits += val
    elif mode == "sub":
        p.credits = max(Decimal("0"), p.credits - val)
    elif mode == "set":
        p.credits = max(Decimal("0"), val)

    p.save(update_fields=["credits"])
    msg = f"Crediti di «{p.display_name}» aggiornati: {p.remaining_credits:.0f} FM rimanenti (Totale: {p.credits:.0f} FM)"
    messages.success(request, msg)

    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json":
        return JsonResponse({
            "ok": True,
            "participant_id": p.id,
            "credits": float(p.credits),
            "spent_credits": float(p.spent_credits),
            "remaining_credits": float(p.remaining_credits),
            "message": msg,
        })
    fallback = f"/dashboard/{p.league_id}/#rose" if p.league_id else "/dashboard/"
    return redirect(safe_next(request, fallback))


@staff_member_required
@require_POST
def admin_reset_team_pin(request, participant_id):
    """Set custom PIN or generate a new random PIN, and optionally regenerate public token."""
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    pin = request.POST.get("pin", "").strip()
    if not pin:
        import random
        pin = f"{random.randint(1000, 9999)}"
    p.access_code = pin
    if request.POST.get("regenerate_token") == "1":
        from ..models.core import generate_public_token
        p.public_token = generate_public_token()
    p.save(update_fields=["access_code", "public_token"])
    msg = f"PIN di accesso per «{p.display_name}» impostato su: {p.access_code}"
    messages.success(request, msg)

    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json":
        return JsonResponse({
            "ok": True,
            "participant_id": p.id,
            "access_code": p.access_code,
            "public_token": p.public_token,
            "message": msg,
        })
    fallback = f"/dashboard/{p.league_id}/#rose" if p.league_id else "/dashboard/"
    return redirect(safe_next(request, fallback))


@staff_member_required
@require_POST
def admin_quick_assign_player(request):
    """Directly assign a player from the listone to a team without running an auction."""
    from .. import services
    participant_id = request.POST.get("participant_id")
    player_id = request.POST.get("player_id")
    price = request.POST.get("price")
    if not participant_id or not player_id:
        return JsonResponse({"ok": False, "error": "Squadra e calciatore obbligatori"}, status=400)
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    player, denied = managed_or_403(request, Player, player_id)
    if denied:
        return denied
    if mixed_leagues(p, player):
        res = {"ok": False, "error": services.ERROR_LABELS["league_mismatch"]}
    else:
        res = services.assign_player(player_id, participant_id, price=price, by_admin=True)
    if res.get("ok"):
        messages.success(request, f"Calciatore assegnato a «{p.display_name}».")
    else:
        messages.error(request, res.get("error", "Errore durante l'assegnazione"))

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(res)
    fallback = f"/dashboard/{p.league_id}/#rose" if p.league_id else "/dashboard/"
    return redirect(safe_next(request, fallback))


def _qr_console_ok(request, participant):
    """The console's pass: the user manages the team's league, and — through
    the internet tunnel — has unlocked the regia, as ``staff_member_required``
    asks of every console page."""
    if not user_can_manage_scope(request.user, participant.league):
        return False
    return not remote.request_is_remote(request) or bool(request.session.get("regia_unlocked"))


def _qr_screen_token_ok(request, participant, auction):
    """The big screen's pass: ``?t=`` is the screen token of ``auction``
    (``?a=``), and the team plays in that auction's league."""
    token = (request.GET.get("t") or "").strip()
    return (auction is not None and bool(auction.public_token)
            and token == auction.public_token
            and auction.league_id == participant.league_id)


def participant_qr(request, participant_id):
    """PNG QR code of a team's tokenised join link.

    The code signs whoever scans it in as the team, so it is not handed out by
    id alone: the caller either manages the team's league (the console), or
    shows the screen token of an auction of that league (``?a=<id>&t=<token>``
    — the big screen, where each manager scans to join as their team). 403
    otherwise; 404 when the team is missing; 503 if the optional ``qrcode``
    dependency is not installed.

    ``?a=<auction_id>`` bakes the auction into the code, so scanning lands
    directly on that auction's bidding page. ``?net=lan`` encodes the wifi
    address instead of the public one — the code to show the room while the
    internet tunnel is open.
    """
    p = get_object_or_404(Participant.objects.select_related("league"), pk=participant_id)
    auction = None
    wanted = (request.GET.get("a") or "").strip()
    if wanted.isdigit():
        auction = Auction.objects.filter(pk=int(wanted)).first()
    if not (_qr_console_ok(request, p) or _qr_screen_token_ok(request, p, auction)):
        return HttpResponseForbidden("Non autorizzato.")
    if auction is not None and auction.league_id != p.league_id:
        auction = None   # never point a team at another league's auction
    if qrcode is None:
        return HttpResponse("qrcode non installato", status=503)
    url = ""
    if request.GET.get("net") == "lan":
        url = participant_lan_join_url(request, p, auction)
    img = qrcode.make(url or participant_join_url(request, p, auction), box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    resp = HttpResponse(buf.getvalue(), content_type="image/png")
    # Private: the image is a credential, no shared cache may keep it.
    resp["Cache-Control"] = "private, max-age=300"
    return resp


@staff_member_required
def admin_participant_roster(request, participant_id):
    """One team's roster as JSON — the console modal loads it when opened.

    Rendering every team's roster into the page cost thousands of DOM nodes on
    a 10-team league, for panels that are opened one at a time (if at all).
    """
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    roster = [
        {"id": pl.id, "name": pl.name, "role": pl.role, "team": pl.team,
         "cost": float(pl.cost or 0), "quotation": float(pl.initial_price or 0)}
        for pl in Player.objects.filter(owner=p).order_by("role", "name")
    ]
    return JsonResponse({"ok": True, "team": p.display_name, "roster": roster})


# --- Coaches' portal accounts -------------------------------------------------
#
# A team may be tied to a portal login (``Participant.user``): username or email
# plus password, the way into the app from any device. The league's president
# hands those logins out from the Squadre page — create one for a coach, link
# one the coach already has, reset a forgotten password, switch it off.

SESSION_ACCOUNT_SECRET_KEY = "fm_account_secret"
MIN_PASSWORD_LENGTH = 6          # the same floor the registration form asks for
ACCOUNTS_FORBIDDEN_MSG = (
    "Gli account degli allenatori li gestisce il presidente della lega (o il superadmin)."
)
# No 0/O, 1/l/I: the password is read off a screen and typed on a phone.
_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"


def can_manage_accounts(user, league):
    """True when ``user`` may handle the portal accounts of ``league``'s coaches.

    Stricter than ``user_can_manage_league``: an ownerless legacy league lets
    any logged-in user into its console, but an account is a person's login,
    not a team setting — only the league's owner, co-admins or a superuser touch those.
    """
    if user is None or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if league is not None:
        if league.owner_id == user.id:
            return True
        if hasattr(league, "admins") and league.admins.filter(pk=user.id).exists():
            return True
    return False


def _is_managed(account):
    try:
        return account.managed_account is not None
    except ManagedAccount.DoesNotExist:
        return False


def account_lock_reason(actor, account, action=None):
    """Why ``actor`` may not change ``account``'s credentials, "" when they may.

    Superusers change any account. A league president or co-admin changes an account
    participating in their leagues, provided it holds no superadmin status or foreign
    league presidency.
    """
    if actor.is_superuser:
        return ""
    if account.is_superuser or account.is_staff:
        return "È l'account di un amministratore della piattaforma."
    if account.pk == actor.pk:
        if action == "manage":
            return ""
        return "È il tuo account."

    actor_leagues = League.objects.filter(
        Q(owner=actor) | Q(admins=actor)
    ).distinct()

    # If the target account owns any leagues not administered by actor
    if League.objects.filter(owner=account).exclude(id__in=actor_leagues.values_list("id", flat=True)).exists():
        return "È l'account del presidente di un'altra lega."

    # If the target account plays in leagues not administered by actor
    if Participant.objects.filter(user=account).exclude(league__in=actor_leagues).exists():
        return "Guida anche squadre di leghe che non gestisci."

    if not _is_managed(account):
        if action == "manage":
            return ""
        return ("L'allenatore se l'è registrato da solo: password, nome utente ed email "
                "li può cambiare solo il superadmin.")
    return ""


def account_deletable(actor, account):
    """An account may be deleted when it can be edited and deleting it strands
    nothing: never your own, never an admin's, never a league president's —
    their leagues would be left without an owner, open to any logged-in user."""
    if account_lock_reason(actor, account) or account.pk == actor.pk:
        return False
    if account.is_superuser or account.is_staff:
        return False
    return not League.objects.filter(owner=account).exists()


def generate_password(length=10):
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def _clean_username(raw, exclude=None):
    """``(username, error)`` for a posted username, checked like Django's own."""
    User = get_user_model()
    username = (raw or "").strip()
    if len(username) < 3:
        return username, "Il nome utente deve contenere almeno 3 caratteri."
    field = User._meta.get_field(User.USERNAME_FIELD)
    try:
        field.run_validators(username)
    except ValidationError:
        return username, ("Nome utente non valido: usa lettere, numeri e i simboli @ . + - _ "
                          f"(massimo {field.max_length} caratteri).")
    taken = User.objects.filter(username__iexact=username)
    if exclude is not None:
        taken = taken.exclude(pk=exclude.pk)
    if taken.exists():
        return username, f"Il nome utente «{username}» è già in uso."
    return username, ""


def _clean_email(raw, exclude=None):
    """``(email, error)``: optional, but valid and not someone else's."""
    email = (raw or "").strip()
    if not email:
        return "", ""
    try:
        validate_email(email)
    except ValidationError:
        return email, "Indirizzo email non valido."
    taken = get_user_model().objects.filter(email__iexact=email)
    if exclude is not None:
        taken = taken.exclude(pk=exclude.pk)
    if taken.exists():
        return email, "Questa email è già associata a un altro account."
    return email, ""


def _participants_url(p):
    url = reverse("admin_participants")
    return f"{url}?league={p.league_id}" if p.league_id else url


def _remember_secret(request, p, account, password):
    request.session[SESSION_ACCOUNT_SECRET_KEY] = {
        "team": p.display_name,
        "username": account.username,
        "password": password,
    }


@staff_member_required
@require_POST
def admin_participant_account(request, participant_id):
    """Handle the portal account of one team (``action`` in the POST).

    ``create`` makes a new login for the coach and links it; ``link`` ties an
    existing login (username or email) to the team; ``unlink`` unties it.
    ``update`` (username, email, name), ``password``, ``toggle_active`` and
    ``delete`` change the login itself: see ``account_lock_reason``.
    """
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    if not can_manage_accounts(request.user, p.league):
        return HttpResponseForbidden(ACCOUNTS_FORBIDDEN_MSG)

    User = get_user_model()
    back = safe_next(request, _participants_url(p))
    action = request.POST.get("action", "")
    account = p.user

    def fail(msg):
        messages.error(request, msg)
        return redirect(back)

    if action == "create":
        if account is not None:
            return fail(f"«{p.display_name}» ha già un account: scollegalo prima di crearne un altro.")
        username, error = _clean_username(request.POST.get("username"))
        if error:
            return fail(error)
        email, error = _clean_email(request.POST.get("email"))
        if error:
            return fail(error)
        password = request.POST.get("password") or ""
        generated = not password
        if generated:
            password = generate_password()
        elif len(password) < MIN_PASSWORD_LENGTH:
            return fail(f"La password deve contenere almeno {MIN_PASSWORD_LENGTH} caratteri.")
        with transaction.atomic():
            account = User.objects.create_user(
                username=username, email=email, password=password,
                first_name=(request.POST.get("first_name") or "").strip()[:150],
            )
            ManagedAccount.objects.create(user=account, created_by=request.user)
            p.user = account
            p.save(update_fields=["user"])
        if generated:
            _remember_secret(request, p, account, password)
        messages.success(request, f"Account «{account.username}» creato e collegato a «{p.display_name}».")
        return redirect(back)

    if action == "link":
        if account is not None:
            return fail(f"«{p.display_name}» ha già un account: scollegalo prima di collegarne un altro.")
        ident = (request.POST.get("identifier") or "").strip()
        found = None
        if ident:
            found = User.objects.filter(username__iexact=ident).first()
            if found is None and "@" in ident:
                found = User.objects.filter(email__iexact=ident).first()
        if found is None:
            return fail("Nessun account con questo nome utente o email.")
        p.user = found
        p.save(update_fields=["user"])
        messages.success(request, f"Account «{found.username}» collegato a «{p.display_name}».")
        return redirect(back)

    if action == "manage":
        # Handle unlinking directly if requested
        if request.POST.get("unlink_account") == "1":
            if account is not None:
                uname = account.username
                p.user = None
                p.save(update_fields=["user"])
                messages.success(request, f"Account «{uname}» scollegato da «{p.display_name}».")
            return redirect(back)

        # Team has no account linked yet: create or link
        if account is None:
            subaction = request.POST.get("manage_subaction", "")
            link_uid = request.POST.get("link_user_id")
            link_ident = (request.POST.get("link_identifier") or "").strip()
            if subaction == "link" or link_uid or link_ident:
                found = None
                if link_uid:
                    found = User.objects.filter(pk=link_uid).first()
                elif link_ident:
                    found = User.objects.filter(username__iexact=link_ident).first()
                    if found is None and "@" in link_ident:
                        found = User.objects.filter(email__iexact=link_ident).first()
                if found is None:
                    return fail("Nessun account utente valido trovato da collegare.")
                p.user = found
                p.save(update_fields=["user"])
                account = found
            else:
                # Create brand-new user
                username, error = _clean_username(request.POST.get("username"))
                if error:
                    return fail(error)
                email, error = _clean_email(request.POST.get("email"))
                if error:
                    return fail(error)
                password = request.POST.get("password") or ""
                gen_pwd = (request.POST.get("generate_password") == "1") or not password
                if gen_pwd and not password:
                    password = generate_password()
                elif len(password) < MIN_PASSWORD_LENGTH:
                    return fail(f"La password deve contenere almeno {MIN_PASSWORD_LENGTH} caratteri.")
                with transaction.atomic():
                    account = User.objects.create_user(
                        username=username,
                        email=email,
                        password=password,
                        first_name=(request.POST.get("first_name") or "").strip()[:150],
                    )
                    ManagedAccount.objects.create(user=account, created_by=request.user)
                    p.user = account
                    p.save(update_fields=["user"])
                if gen_pwd:
                    _remember_secret(request, p, account, password)

        # Verify permissions on this account
        lock = account_lock_reason(request.user, account, action="manage")
        if lock:
            return fail(f"Non puoi modificare l'account «{account.username}». {lock}")

        # Update username
        req_username = request.POST.get("username")
        if req_username and req_username.strip() != account.username:
            new_u, err = _clean_username(req_username, exclude=account)
            if err:
                return fail(err)
            account.username = new_u

        # Update email
        req_email = request.POST.get("email")
        if req_email is not None and req_email.strip() != account.email:
            new_e, err = _clean_email(req_email, exclude=account)
            if err:
                return fail(err)
            account.email = new_e

        # Update first_name
        req_first_name = request.POST.get("first_name")
        if req_first_name is not None:
            account.first_name = req_first_name.strip()[:150]

        # Update is_active
        req_active = request.POST.get("is_active")
        if req_active is not None:
            new_active = (req_active in ("1", "on", "true", True))
            if account.pk == request.user.pk and not new_active:
                messages.warning(request, "Non puoi disattivare il tuo stesso account.")
            else:
                account.is_active = new_active

        # Reset password if requested or generated
        new_password = (request.POST.get("password") or "").strip()
        gen_pwd = request.POST.get("generate_password") == "1"
        pwd_reset_done = False
        if gen_pwd and not new_password:
            new_password = generate_password()
        if new_password:
            if len(new_password) < MIN_PASSWORD_LENGTH:
                return fail(f"La password deve contenere almeno {MIN_PASSWORD_LENGTH} caratteri.")
            account.set_password(new_password)
            pwd_reset_done = True
            if account.pk == request.user.pk:
                update_session_auth_hash(request, account)
            if gen_pwd:
                _remember_secret(request, p, account, new_password)

        account.save()

        # Update League Role
        league_role = request.POST.get("league_role")
        if league_role and p.league:
            if league_role == "owner":
                if request.user.is_superuser or p.league.owner_id == request.user.id:
                    p.league.owner = account
                    p.league.admins.remove(account)
                    p.league.save(update_fields=["owner"])
                else:
                    messages.warning(request, "Solo il presidente attuale o un superadmin può trasferire la presidenza della lega.")
            elif league_role == "admin":
                p.league.admins.add(account)
            elif league_role == "manager":
                p.league.admins.remove(account)
                if p.league.owner_id == account.id and (request.user.is_superuser or p.league.owner_id == request.user.id):
                    p.league.owner = None
                    p.league.save(update_fields=["owner"])

        pwd_msg = " (password aggiornata)" if pwd_reset_done else ""
        messages.success(request, f"Dati e ruolo di «{account.username}» aggiornati con successo{pwd_msg}.")
        return redirect(back)

    if account is None:
        return fail(f"«{p.display_name}» non ha un account collegato.")

    if action == "unlink":
        p.user = None
        p.save(update_fields=["user"])
        messages.success(
            request,
            f"Account «{account.username}» scollegato da «{p.display_name}»: "
            "la squadra resta raggiungibile con il suo link e il PIN.")
        return redirect(back)

    lock = account_lock_reason(request.user, account)
    if lock:
        return fail(f"Non puoi modificare l'account «{account.username}». {lock}")

    if action == "update":
        username, error = _clean_username(request.POST.get("username"), exclude=account)
        if error:
            return fail(error)
        email, error = _clean_email(request.POST.get("email"), exclude=account)
        if error:
            return fail(error)
        account.username = username
        account.email = email
        account.first_name = (request.POST.get("first_name") or "").strip()[:150]
        account.save(update_fields=["username", "email", "first_name"])
        messages.success(request, f"Account «{account.username}» aggiornato.")
        return redirect(back)

    if action == "password":
        password = request.POST.get("password") or ""
        generated = not password
        if generated:
            password = generate_password()
        elif len(password) < MIN_PASSWORD_LENGTH:
            return fail(f"La password deve contenere almeno {MIN_PASSWORD_LENGTH} caratteri.")
        account.set_password(password)
        account.save(update_fields=["password"])
        if account.pk == request.user.pk:
            update_session_auth_hash(request, account)   # don't log yourself out
        if generated:
            _remember_secret(request, p, account, password)
        messages.success(
            request,
            f"Password di «{account.username}» reimpostata: i dispositivi già collegati "
            "con la vecchia password dovranno rientrare.")
        return redirect(back)

    if action == "toggle_active":
        if account.pk == request.user.pk:
            return fail("Non puoi disattivare il tuo stesso account.")
        account.is_active = not account.is_active
        account.save(update_fields=["is_active"])
        if account.is_active:
            messages.success(request, f"Account «{account.username}» riattivato.")
        else:
            messages.success(
                request,
                f"Account «{account.username}» disattivato: non entra più con la password "
                "(il link e il PIN della squadra restano validi).")
        return redirect(back)

    if action == "delete":
        if not account_deletable(request.user, account):
            return fail(f"L'account «{account.username}» non si può eliminare da qui.")
        username = account.username
        account.delete()   # every team it guided is unlinked (SET_NULL)
        messages.success(request, f"Account «{username}» eliminato.")
        return redirect(back)

    return fail("Azione non riconosciuta.")
