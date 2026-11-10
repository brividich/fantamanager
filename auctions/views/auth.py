"""Authentication & Onboarding views for the SaaS platform."""
import logging
import re
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse

from .. import legal, throttle

from ..models import AccountPrivacy, Auction, League, Participant
from ..models.participant import AMBIGUOUS_CODE_MESSAGE, find_team_by_code
from ..services import onboarding, privacy
from .common import SESSION_LEAGUE_KEY, _session_participant, manageable_leagues, safe_next

logger = logging.getLogger(__name__)


def _login_usernames(identifier):
    """The existing accounts ``identifier`` may name, best match first.

    Phones capitalise the first letter of a text field, so "Mario" must find
    "mario": an exact username wins, then the email (any case), then the
    username in any case.
    """
    names = list(User.objects.filter(username=identifier).values_list("username", flat=True))
    if "@" in identifier:
        names += User.objects.filter(email__iexact=identifier).values_list("username", flat=True)[:3]
    if not names:
        names += User.objects.filter(username__iexact=identifier).values_list("username", flat=True)[:3]
    return list(dict.fromkeys(names))


def authenticate_identifier(request, identifier, password):
    """The account for username/email ``identifier`` and ``password``, or None.

    Shared by the console login and the app login. The password is tried as
    typed and, if different, without the spaces a keyboard's autocomplete adds
    at the ends.
    """
    passwords = list(dict.fromkeys([password, password.strip()]))
    for username in _login_usernames(identifier.strip()):
        for pw in passwords:
            user = authenticate(request, username=username, password=pw)
            if user is not None:
                return user
    return None


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

        # Check if user runs leagues, as owner or co-admin (Admin / Regia)
        if manageable_leagues(request.user).exists():
            if is_mobile:
                return redirect("app_regia")
            return redirect("dashboard")

        # Check if user has teams
        teams = Participant.objects.filter(user=request.user, is_active=True)
        if teams.count() > 1:
            # Più squadre (in leghe diverse): sceglie l'allenatore, mai l'ordine del database.
            from urllib.parse import urlencode
            nxt = reverse("app_home") if is_mobile else reverse("home_portal")
            return redirect(f"{reverse('app_login')}?{urlencode({'switch': 1, 'next': nxt})}")
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

    # Primo avvio dell'app del PC: nessun account ancora. Prima schermata:
    # «Crea l'amministratore di questo PC», poi «Cosa fai con questo PC?».
    if settings.DESKTOP_APP and not User.objects.exists():
        return redirect("register")

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
            **_legal_form_ctx(),
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
        password = request.POST.get("password") or ""

        if not identifier or not password.strip():
            error = "Inserisci nome utente / email e password."
        elif throttle.blocked(request, "login"):
            error = throttle.MESSAGE
        else:
            user = authenticate_identifier(request, identifier, password)
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
            **_legal_form_ctx(),
        },
    )


def _password_problem(password, username="", email="", first_name=""):
    """Django's password validators (length, common, numeric, too close to
    the name), as one Italian sentence; "" when the password is fine."""
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    try:
        validate_password(password, User(username=username, email=email, first_name=first_name))
    except ValidationError as exc:
        return " ".join(exc.messages)
    return ""


def register_view(request):
    """Handle new user registration.

    In the desktop app the first registered user on an empty platform becomes
    Superadmin. On a server the superadmin comes from the install (entrypoint,
    ``createsuperuser``): whoever reached the page first must not get it.
    """
    if request.user.is_authenticated:
        return redirect("home")

    error = None
    form = {}
    need_legal = privacy.legal_required()
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        email = (request.POST.get("email") or "").strip()
        password = request.POST.get("password") or ""
        password_confirm = request.POST.get("password_confirm") or ""
        first_name = (request.POST.get("first_name") or "").strip()
        form = {"username": username, "email": email, "first_name": first_name}

        if not username or not password:
            error = "Nome utente e password sono obbligatori."
        elif len(username) < 3:
            error = "Il nome utente deve contenere almeno 3 caratteri."
        elif need_legal and not email:
            error = "Scrivi la tua email: serve a confermare l'account e a recuperare la password."
        elif email and not _valid_email(email):
            error = "L'email non sembra valida: controllala (es. nome@esempio.it)."
        elif need_legal and not request.POST.get("accept_terms"):
            error = "Per registrarti spunta «Ho letto l'informativa privacy e accetto i termini»."
        elif need_legal and not request.POST.get("accept_age"):
            error = f"Per registrarti devi avere almeno {legal.MIN_AGE} anni: spunta la casella."
        elif password != password_confirm:
            error = "Le due password non coincidono."
        elif (weak := _password_problem(password, username, email, first_name)):
            error = weak
        elif User.objects.filter(username__iexact=username).exists():
            error = "Questo nome utente è già in uso. Scegline un altro."
        elif email and User.objects.filter(email__iexact=email).exists():
            error = "Questa email è già associata a un account."
        else:
            try:
                # First user becomes Superadmin (desktop app only).
                is_first = settings.DESKTOP_APP and User.objects.count() == 0
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    password=password,
                    first_name=first_name,
                    is_superuser=is_first,
                    is_staff=is_first,
                )
                if request.POST.get("accept_terms") and request.POST.get("accept_age"):
                    privacy.record_acceptance(user, throttle.client_ip(request))
                if need_legal:
                    # Registrato da solo: l'email va confermata (link di 48 ore).
                    AccountPrivacy.objects.create(user=user, self_registered=True)
                login(request, user)
                logger.info("Nuovo utente registrato: %s (superuser=%s)", user.username, is_first)
                welcome = f"Benvenuto, {user.first_name or user.username}!"
                if need_legal and email:
                    sent, _err = privacy.send_verification(request, user, email)
                    if sent:
                        welcome += f" Ti abbiamo scritto a {email}: apri il link per confermare l'email."
                messages.success(request, welcome)

                # Anche il primo account del PC (superadmin) parte da «Cosa
                # vuoi fare?»: nuova lega, lega scaricata dal sito o backup.
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
            "form": form,
            "first_run": settings.DESKTOP_APP and not User.objects.exists(),
            "live_auctions": _spectator_auctions(Auction.Status.LIVE, Auction.Status.PAUSED),
            **_legal_form_ctx(),
        },
    )


def _legal_form_ctx():
    return {"legal_required": privacy.legal_required(), "min_age": legal.MIN_AGE}


def _valid_email(email):
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    try:
        validate_email(email)
    except ValidationError:
        return False
    return True


def logout_view(request):
    """Log out cleanly and redirect to the portal."""
    logout(request)
    request.session.flush()
    return redirect("home")


_TOKEN_IN_LINK = re.compile(r"(?:/invito/|[?&]t=)([A-Za-z0-9_\-]{16,})")


@login_required(login_url="login")
def onboarding_view(request):
    """«Cosa vuoi fare?»: la prima schermata dopo la registrazione.

    1. Creo una lega → il wizard (nell'app da telefono).
    2. Mi hanno invitato → incolla il link o scrivi il codice: il link apre la
       pagina d'invito, il codice collega la squadra (``onboarding.link_team``).
    3. Solo nell'app del PC: scarico una lega dal sito per l'asta in sala.
    """
    error = None
    is_mobile = getattr(request, "is_mobile", False)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create_league":
            # Il vecchio mini-form: oggi la lega nasce solo dal wizard.
            return redirect("app_setup" if is_mobile else "admin_setup")

        if action == "join_team":
            raw = (request.POST.get("access_code") or "").strip()
            m = _TOKEN_IN_LINK.search(raw)
            if m:
                return redirect("invite", token=m.group(1))
            if not raw:
                error = "Incolla il link che ti ha mandato il presidente, o scrivi il codice della squadra."
            elif throttle.blocked(request, "code"):
                error = throttle.MESSAGE
            else:
                participant, ambiguous = find_team_by_code(raw)
                if ambiguous:
                    error = AMBIGUOUS_CODE_MESSAGE
                elif not participant:
                    throttle.failure(request, "code")
                    error = "Codice squadra non valido o non riconosciuto: controllalo, o usa il link dell'invito."
                else:
                    outcome = onboarding.link_team(request.user, participant)
                    if outcome == "taken":
                        error = onboarding.TAKEN_MESSAGE
                    else:
                        onboarding.enter_team(request, participant)
                        logger.info("Squadra '%s' aperta da %s (%s)", participant.display_name,
                                    request.user.username, outcome)
                        messages.success(request, f"Sei ora al comando di {participant.display_name}!")
                        return redirect("app_home")

    return render(
        request,
        "auctions/onboarding.html",
        {
            "error": error,
            "setup_url": reverse("app_setup") if is_mobile else reverse("admin_setup"),
            "desktop_app": settings.DESKTOP_APP,
        },
    )
