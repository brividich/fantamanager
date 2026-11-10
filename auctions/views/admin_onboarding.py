"""Le azioni dell'onboarding lato presidente: la card «Prepara la lega»
(regole controllate, nascondi), i co-admin (invita, ritira, togli) e
«Reinvia a chi non è ancora entrato». Ogni form manda ``next``: si torna
alla pagina da cui parte (console o app)."""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import remote
from ..models import CoAdminInvite, League
from ..services import mail, onboarding
from .common import FORBIDDEN_LEAGUE_MSG, safe_next, staff_member_required, user_can_manage_league
from .invite import new_coadmin_invite, send_coadmin_invite


def _league_or_403(request, league_id):
    league = get_object_or_404(League, pk=league_id)
    if not user_can_manage_league(request.user, league):
        return None, HttpResponseForbidden(FORBIDDEN_LEAGUE_MSG)
    return league, None


@staff_member_required
@require_POST
def admin_setup_card(request, league_id):
    """«Prepara la lega»: segna le regole come controllate, o nasconde la card."""
    league, denied = _league_or_403(request, league_id)
    if denied:
        return denied
    back = safe_next(request, reverse("dashboard_league", args=[league.id]))
    action = request.POST.get("action")
    if action == "rules_checked":
        onboarding.set_setup_flag(league, "rules_checked")
        messages.success(request, "Regole segnate come controllate.")
    elif action == "hide":
        onboarding.set_setup_flag(league, "hidden")
        messages.info(request, "Card «Prepara la lega» nascosta. I passi restano nelle pagine di sempre.")
    elif action == "show":
        onboarding.set_setup_flag(league, "hidden", False)
    return redirect(back)


@staff_member_required
@require_POST
def admin_invite_missing(request, league_id):
    """Rimanda l'invito via email solo a chi non è ancora entrato."""
    league, denied = _league_or_403(request, league_id)
    if denied:
        return denied
    back = safe_next(request, reverse("admin_participants") + f"?league={league.id}")
    missing = [p for p in onboarding.not_joined(league).select_related("user") if p.contact_email]
    if not missing:
        messages.info(request, "Nessuna squadra da reinvitare per email: chi manca non ha un indirizzo. "
                               "Usa «Condividi» o «Copia link» sulla sua riga.")
        return redirect(back)
    if not mail.is_ready():
        messages.error(request, mail_off_message(request))
        return redirect(back)
    report = mail.send_team_invites(request, league, missing)
    (messages.success if report["sent"] and not report["failed"] else messages.warning)(
        request, "Inviti a chi manca: " + mail.report_message(report))
    return redirect(back)


def mail_off_message(request, instead="usa «Condividi» o «Copia link» per ogni squadra"):
    """Posta non configurata: al presidente cosa fare adesso; solo al
    superuser dove si configura (la pagina Posta è sua)."""
    if request.user.is_superuser:
        return f"La posta non è configurata: impostala in Impostazioni → Posta. Intanto {instead}."
    return f"L'invio email non è attivo su questo sito: {instead}."


@staff_member_required
@require_POST
def admin_coadmin(request, league_id):
    """I co-admin di una lega: ``invite`` (email, facoltativa: senza c'è il
    link da passare), ``revoke`` (un invito aperto), ``remove`` (un co-admin;
    il presidente no)."""
    league, denied = _league_or_403(request, league_id)
    if denied:
        return denied
    back = safe_next(request, reverse("admin_config") + f"?league={league.id}")
    action = request.POST.get("action")
    if action == "invite":
        email = (request.POST.get("email") or "").strip()
        if email:
            from django.core.exceptions import ValidationError
            from django.core.validators import validate_email
            try:
                validate_email(email)
            except ValidationError:
                messages.error(request, f"«{email}» non è un'email valida.")
                return redirect(back)
        inv = new_coadmin_invite(league, request.user, email)
        link = remote.best_base_url(request).rstrip("/") + reverse("coadmin_invite", args=[inv.token])
        ok, err = send_coadmin_invite(request, inv) if email else (False, "")
        if ok:
            messages.success(request, f"Invito da co-admin mandato a {email}.")
        else:
            why = f" ({err})" if err else ""
            messages.info(request, f"Invito creato{why}: passa questo link a chi vuoi come co-admin — {link}")
    elif action == "revoke":
        inv = CoAdminInvite.objects.filter(pk=request.POST.get("invite_id"), league=league).first()
        if inv is not None and inv.is_open:
            inv.revoked = True
            inv.save(update_fields=["revoked"])
            messages.success(request, "Invito ritirato: il link non apre più niente.")
    elif action == "remove":
        user = get_user_model().objects.filter(pk=request.POST.get("user_id")).first()
        if user is None:
            messages.error(request, "Account non trovato.")
        elif user.pk == league.owner_id:
            messages.error(request, "Il presidente non si toglie: prima passa la presidenza a un altro.")
        else:
            league.admins.remove(user)
            messages.success(request, f"«{user.username}» non è più co-admin di {league.name}.")
    return redirect(back)


def coadmin_context(request, league):
    """I dati del riquadro co-admin (Impostazioni e wizard)."""
    if league is None:
        return {}
    return {
        "coadmins": list(league.admins.exclude(pk=league.owner_id).order_by("username")),
        "coadmin_invites": [i for i in league.coadmin_invites.all() if i.is_open],
        "coadmin_base": remote.best_base_url(request).rstrip("/"),
    }
