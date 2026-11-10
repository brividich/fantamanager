"""Gli inviti: una squadra (``/invito/<token>/``) o il ruolo di co-admin di
una lega (``/invito-admin/<token>/``).

Una sola pagina per chi riceve il link, da telefono o dal PC: dice chi ti ha
invitato, in quale lega e con quale squadra, e offre le tre strade —
crea l'account ed entra, entra con l'account che hai, entra solo per l'asta.
Il collegamento della squadra all'account passa da ``onboarding.link_team``.
"""
import logging
import secrets

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from .. import legal, throttle
from ..models import AccountPrivacy, Auction, CoAdminInvite, Participant
from ..services import mail, onboarding, privacy
from .auth import _password_problem, _valid_email, authenticate_identifier
from .common import safe_next, user_can_manage_league

logger = logging.getLogger(__name__)


def _suggest_username(name):
    base = slugify(name).replace("-", "_")[:24] or "allenatore"
    if len(base) < 3:
        base = f"{base}_fm"
    candidate, n = base, 1
    while User.objects.filter(username__iexact=candidate).exists():
        n += 1
        candidate = f"{base}{n}"
    return candidate


def _live_auction(team):
    if team.league_id is None:
        return None
    return (Auction.objects.filter(league_id=team.league_id, status__in=[Auction.Status.LIVE, Auction.Status.PAUSED])
            .order_by("-id").first())


def _inviter_name(league):
    owner = getattr(league, "owner", None) if league is not None else None
    if owner is None:
        return "Il presidente della lega"
    return owner.get_full_name() or owner.first_name or owner.username


def _signup(request, *, email_hint="", verified_email=""):
    """Crea l'account dai campi del modulo d'invito. ``(user, errore)``."""
    username = (request.POST.get("username") or "").strip()
    email = (request.POST.get("email") or "").strip()
    password = request.POST.get("password") or ""
    need_legal = privacy.legal_required()
    if len(username) < 3:
        return None, "Scegli un nome utente di almeno 3 caratteri."
    if User.objects.filter(username__iexact=username).exists():
        return None, "Questo nome utente è già in uso: scegline un altro."
    if need_legal and not email:
        return None, "Scrivi la tua email: serve a recuperare la password."
    if email and not _valid_email(email):
        return None, "L'email non sembra valida: controllala."
    if email and User.objects.filter(email__iexact=email).exists():
        return None, "Questa email ha già un account: usa «Ho già un account»."
    if need_legal and not (request.POST.get("accept_terms") and request.POST.get("accept_age")):
        return None, "Per creare l'account spunta le due caselle (informativa e età)."
    if password != (request.POST.get("password_confirm") or password):
        return None, "Le due password non coincidono."
    if (weak := _password_problem(password, username, email)):
        return None, weak
    user = User.objects.create_user(username=username, email=email, password=password)
    if request.POST.get("accept_terms") and request.POST.get("accept_age"):
        privacy.record_acceptance(user, throttle.client_ip(request))
    if need_legal:
        # L'invito è arrivato a questa email: se è la stessa, è già confermata.
        same = bool(email and verified_email and email.lower() == verified_email.lower())
        AccountPrivacy.objects.create(user=user, self_registered=True,
                                      email_verified_at=timezone.now() if same else None)
        if email and not same:
            privacy.send_verification(request, user, email)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    logger.info("Nuovo account da invito: %s", user.username)
    return user, ""


def _login(request):
    """Entra con l'account che hai. ``(user, errore)``."""
    identifier = (request.POST.get("identifier") or "").strip()
    password = request.POST.get("password") or ""
    if not identifier or not password.strip():
        return None, "Scrivi nome utente (o email) e password."
    if throttle.blocked(request, "login"):
        return None, throttle.MESSAGE
    user = authenticate_identifier(request, identifier, password)
    if user is None:
        throttle.failure(request, "login")
        return None, "Nome utente o password non validi."
    if not user.is_active:
        return None, "Questo account è disattivato."
    login(request, user)
    return user, ""


LINK_MESSAGES = {
    "linked": "Fatto: {team} è collegata al tuo account. Da adesso entri con nome utente e password, da qualsiasi dispositivo.",
    "mine": "{team} è già collegata al tuo account.",
    "visit": "Gestisci questa lega (o hai già una squadra qui): entri in {team} senza collegarla al tuo account.",
}


def invite(request, token):
    """La pagina d'invito di una squadra."""
    team = Participant.objects.filter(public_token=token, is_active=True).select_related("league", "user").first() \
        if token else None
    if team is None:
        return render(request, "auctions/invite.html", {"invalid": True}, status=404)
    league = team.league
    next_url = safe_next(request, reverse("app_home"))
    live = _live_auction(team)
    user = request.user if request.user.is_authenticated else None
    if user is None or team.user_id != user.id:
        onboarding.mark_invite_opened(team)

    error = ""
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "guest":
            onboarding.enter_team(request, team)
            if live is not None:
                return redirect("bid", auction_id=live.id)
            return redirect(next_url)
        if action in ("signup", "login", "link"):
            if action == "signup":
                user, error = _signup(request, verified_email=team.email)
            elif action == "login":
                user, error = _login(request)
            elif user is None:
                error = "Entra prima con il tuo account."
            if user is not None and not error:
                outcome = onboarding.link_team(user, team)
                if outcome == "taken":
                    error = onboarding.TAKEN_MESSAGE
                else:
                    onboarding.enter_team(request, team)
                    messages.success(request, LINK_MESSAGES[outcome].format(team=team.display_name))
                    return redirect(next_url)

    taken = team.user_id is not None and (user is None or team.user_id != user.id)
    if user is not None and team.user_id == user.id and request.method == "GET":
        onboarding.enter_team(request, team)
        return redirect(next_url)
    return render(request, "auctions/invite.html", {
        "team": team,
        "league": league,
        "inviter": _inviter_name(league),
        "live": live,
        "taken": taken,
        "error": error,
        "next": request.GET.get("next") or request.POST.get("next") or "",
        "suggested_username": (request.POST.get("username") or _suggest_username(team.display_name)),
        "suggested_email": request.POST.get("email") or team.email,
        "account": user,
        "legal_required": privacy.legal_required(),
        "min_age": legal.MIN_AGE,
        "active_mode": request.POST.get("action") or ("login" if taken else "signup"),
    })


# --- Co-admin -----------------------------------------------------------------

def new_coadmin_invite(league, created_by, email=""):
    return CoAdminInvite.objects.create(league=league, email=(email or "").strip()[:254],
                                        token=secrets.token_urlsafe(24), created_by=created_by)


def send_coadmin_invite(request, inv):
    """L'email di invito a co-admin. ``(ok, errore)``."""
    from django.template.loader import render_to_string

    if not inv.email:
        return False, "Nessuna email."
    if not mail.is_ready():
        return False, "L'invio email non è attivo su questo sito."
    base = mail.link_base(request)
    if not base:
        return False, mail.NO_LINK_BASE_MESSAGE
    ctx = {"league": inv.league, "sender": mail.sender_name(request, inv.league),
           "link": base + reverse("coadmin_invite", args=[inv.token]), **privacy.legal_links(base)}
    return mail.send(f"Co-admin di {inv.league.name} — FantaManager", inv.email,
                     render_to_string("auctions/email/coadmin_invite.txt", ctx))


def coadmin_invite(request, token):
    inv = CoAdminInvite.objects.filter(token=token).select_related("league", "league__owner").first() if token else None
    if inv is None or not inv.is_open:
        return render(request, "auctions/coadmin_invite.html", {"invalid": True, "used": inv is not None},
                      status=404)
    league = inv.league
    user = request.user if request.user.is_authenticated else None
    error = ""
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "signup":
            user, error = _signup(request, verified_email=inv.email)
        elif action == "login":
            user, error = _login(request)
        elif action == "accept" and user is None:
            error = "Entra prima con il tuo account."
        if user is not None and not error:
            if league.owner_id == user.id:
                error = "Sei già il presidente di questa lega."
            else:
                league.admins.add(user)
                inv.accepted_at = timezone.now()
                inv.accepted_by = user
                inv.save(update_fields=["accepted_at", "accepted_by"])
                onboarding.refresh_ready(league)
                messages.success(request, f"Ora sei co-admin di «{league.name}»: la trovi nella Regia.")
                return redirect(f"{reverse('app_regia')}?league={league.id}" if getattr(request, "is_mobile", False)
                                else reverse("dashboard_league", args=[league.id]))
    return render(request, "auctions/coadmin_invite.html", {
        "inv": inv, "league": league, "inviter": _inviter_name(league), "account": user, "error": error,
        "already": user is not None and user_can_manage_league(user, league),
        "suggested_username": request.POST.get("username") or "",
        "suggested_email": request.POST.get("email") or inv.email,
        "legal_required": privacy.legal_required(), "min_age": legal.MIN_AGE,
        "active_mode": request.POST.get("action") or ("accept" if user else "signup"),
    })
