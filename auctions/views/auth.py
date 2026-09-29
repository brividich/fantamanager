"""Authentication & Onboarding views for the SaaS platform."""
import logging
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse

from .. import throttle

from ..models import Auction, League, Participant
from .common import SESSION_LEAGUE_KEY, _session_participant, safe_next, target_league

logger = logging.getLogger(__name__)


def _spectator_auctions(*statuses):
    """Running auctions listed on the sign-in page as quick links to their
    screen. Only on a trusted LAN: online the screen wants its token, and a
    stranger has no business seeing which leagues are playing tonight."""
    if settings.PUBLIC_TOKENS_REQUIRED:
        return Auction.objects.none()
    return Auction.objects.filter(status__in=statuses).select_related("league")[:6]


def portal_view(request):
    """Unified SaaS Gateway with automatic device routing.

    - Smartphone: always routes to the mobile app experience (/app/ or /app/login/)
    - PC / Desktop: routes to Web portal or Web Regia/Dashboard
    - Admin (Regia): always has access to Regia (Dashboard on PC, App Regia on mobile)
    """
    is_mobile = getattr(request, "is_mobile", False)

    if request.user.is_authenticated:
        if request.user.is_superuser:
            return redirect("supervisor_dashboard")

        # Check if user owns leagues (Admin / Regia)
        owned_leagues = League.objects.filter(owner=request.user)
        if owned_leagues.exists():
            if is_mobile:
                return redirect("app_regia")
            return redirect("dashboard")

        # Check if user has teams
        teams = Participant.objects.filter(user=request.user, is_active=True)
        if teams.exists():
            team = teams.first()
            request.session["participant_id"] = team.id
            request.session["display_name"] = team.display_name
            if team.league_id:
                request.session[SESSION_LEAGUE_KEY] = team.league_id
            if is_mobile:
                return redirect("app_home")
            return redirect("home_portal")

        return redirect("onboarding")

    # Unauthenticated visitor:
    if is_mobile:
        participant = _session_participant(request)
        if participant:
            return redirect("app_home")
        return redirect("app_login")

    # Unauthenticated visitor: collect joinable auctions for spectator quick links
    live_auctions = _spectator_auctions(Auction.Status.LIVE, Auction.Status.PAUSED, Auction.Status.READY)

    return render(
        request,
        "auctions/auth_portal.html",
        {
            "current_league": None,
            "active_auction": None,
            "live_auctions": live_auctions,
            "next": request.GET.get("next", ""),
        },
    )


def login_view(request):
    """Handle standard username/email + password login."""
    if request.user.is_authenticated:
        return redirect("home")

    error = None
    next_url = safe_next(request, "")

    if request.method == "POST":
        identifier = (request.POST.get("identifier") or "").strip()
        password = (request.POST.get("password") or "").strip()

        if not identifier or not password:
            error = "Inserisci nome utente / email e password."
        elif throttle.blocked(request, "login"):
            error = throttle.MESSAGE
        else:
            # Look up username if email was entered
            username = identifier
            if "@" in identifier:
                user_obj = User.objects.filter(email__iexact=identifier).first()
                if user_obj:
                    username = user_obj.username

            user = authenticate(request, username=username, password=password)
            if user is not None:
                if not user.is_active:
                    error = "Questo account è disattivato. Contatta l'amministratore."
                else:
                    login(request, user)
                    logger.info("Utente autenticato: %s (id=%s)", user.username, user.id)

                    # Bootstrap for old desktop databases that have accounts but no
                    # superadmin. Desktop only: on a server, "no superadmin left"
                    # must not hand the whole platform to whoever logs in next.
                    if settings.DESKTOP_APP and not User.objects.filter(is_superuser=True).exists():
                        user.is_superuser = True
                        user.is_staff = True
                        user.save(update_fields=["is_superuser", "is_staff"])

                    if next_url:
                        return redirect(next_url)
                    return redirect("home")
            else:
                throttle.failure(request, "login")
                error = "Credenziali non valide. Verifica username/email e password."

    return render(
        request,
        "auctions/auth_portal.html",
        {
            "error": error,
            "next": next_url,
            "active_tab": "login",
            "live_auctions": _spectator_auctions(Auction.Status.LIVE, Auction.Status.PAUSED),
        },
    )


def register_view(request):
    """Handle new user registration.

    First registered user on an empty platform automatically becomes Superadmin.
    """
    if request.user.is_authenticated:
        return redirect("home")

    error = None
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        email = (request.POST.get("email") or "").strip()
        password = request.POST.get("password") or ""
        password_confirm = request.POST.get("password_confirm") or ""
        first_name = (request.POST.get("first_name") or "").strip()

        if not username or not password:
            error = "Nome utente e password sono obbligatori."
        elif len(username) < 3:
            error = "Il nome utente deve contenere almeno 3 caratteri."
        elif len(password) < 6:
            error = "La password deve contenere almeno 6 caratteri."
        elif password != password_confirm:
            error = "Le due password non coincidono."
        elif User.objects.filter(username__iexact=username).exists():
            error = "Questo nome utente è già in uso. Scegline un altro."
        elif email and User.objects.filter(email__iexact=email).exists():
            error = "Questa email è già associata a un account."
        else:
            try:
                # First user becomes Superadmin
                is_first = User.objects.count() == 0
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    password=password,
                    first_name=first_name,
                    is_superuser=is_first,
                    is_staff=is_first,
                )
                login(request, user)
                logger.info("Nuovo utente registrato: %s (superuser=%s)", user.username, is_first)
                messages.success(request, f"Benvenuto, {user.first_name or user.username}!")

                if is_first:
                    return redirect("supervisor_dashboard")
                return redirect("onboarding")
            except Exception as e:
                logger.exception("Errore durante la registrazione: %s", e)
                error = "Impossibile completare la registrazione. Riprova."

    return render(
        request,
        "auctions/auth_portal.html",
        {
            "error": error,
            "active_tab": "register",
            "live_auctions": _spectator_auctions(Auction.Status.LIVE, Auction.Status.PAUSED),
        },
    )


def logout_view(request):
    """Log out cleanly and redirect to the portal."""
    logout(request)
    request.session.flush()
    return redirect("home")


@login_required(login_url="login")
def onboarding_view(request):
    """Onboarding guide for new users: Create a League OR Join an existing League."""
    error = None
    success = None

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create_league":
            name = (request.POST.get("name") or "").strip()
            game_mode = request.POST.get("game_mode", League.GameMode.CLASSIC)
            try:
                budget = int(request.POST.get("budget", 500))
            except (ValueError, TypeError):
                budget = 500

            if not name:
                error = "Inserisci un nome valido per la tua lega."
            else:
                league = League.objects.create(
                    name=name,
                    owner=request.user,
                    game_mode=game_mode,
                    budget=budget,
                )
                request.session[SESSION_LEAGUE_KEY] = league.id
                logger.info("Lega creata dall'utente %s: %s (id=%s)", request.user.username, league.name, league.id)
                messages.success(request, f"Lega '{league.name}' creata con successo! Benvenuto nella regia.")
                return redirect("dashboard_league", league_id=league.id)

        elif action == "join_team":
            code = (request.POST.get("access_code") or "").strip()
            if not code:
                error = "Inserisci il codice squadra ricevuto dal presidente di lega."
            elif throttle.blocked(request, "code"):
                error = throttle.MESSAGE
            else:
                participant = Participant.objects.filter(
                    Q(access_code__iexact=code) | Q(public_token=code),
                    is_active=True,
                ).first()
                if not participant:
                    throttle.failure(request, "code")
                    error = "Codice squadra non valido o non riconosciuto."
                elif participant.user_id is not None and participant.user_id != request.user.id:
                    # Same rule as the app login: a code does not take a team
                    # away from the account it is already linked to.
                    error = "Questa squadra è già associata a un altro account utente."
                else:
                    # Link participant to user
                    participant.user = request.user
                    participant.save(update_fields=["user"])

                    request.session["participant_id"] = participant.id
                    request.session["display_name"] = participant.display_name
                    if participant.league_id:
                        request.session[SESSION_LEAGUE_KEY] = participant.league_id

                    logger.info("Squadra '%s' associata all'utente %s", participant.display_name, request.user.username)
                    messages.success(request, f"Sei ora al comando di {participant.display_name}!")
                    return redirect("app_home")

    return render(
        request,
        "auctions/onboarding.html",
        {
            "error": error,
            "success": success,
        },
    )
