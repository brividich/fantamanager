"""Privacy: informativa e termini, consenso, verifica dell'email, «Il mio
account» (export, cambio email, eliminazione) e disiscrizione dalle email
della lega. La logica sta in services/privacy.py."""
import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, logout
from django.contrib.auth.models import User
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import legal, throttle
from ..models import AccountPrivacy, League
from ..services import mail, privacy
from .common import in_app, manageable_leagues, page_frame, safe_next

logger = logging.getLogger("auctions.privacy")


def _legal_ctx():
    return {
        "privacy_version": legal.PRIVACY_VERSION,
        "terms_version": legal.TERMS_VERSION,
        "legal_date": legal.LEGAL_DATE,
        "min_age": legal.MIN_AGE,
        "owner": settings.FM_PRIVACY_OWNER,
        "privacy_email": settings.FM_PRIVACY_EMAIL,
        "bid_ip_days": settings.FM_RETENTION_BID_IP_DAYS,
        "unverified_days": settings.FM_RETENTION_UNVERIFIED_DAYS,
        "backup_keep": settings.BACKUP_KEEP,
        "backup_hours": settings.BACKUP_EVERY_HOURS,
        "log_files": 6,
    }


def privacy_page(request):
    return render(request, "auctions/legal_privacy.html", _legal_ctx())


def terms_page(request):
    return render(request, "auctions/legal_terms.html", _legal_ctx())


def legal_reaccept(request):
    """I testi sono cambiati (o l'account non li ha mai accettati): una pagina
    breve con le due caselle e un pulsante."""
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next={request.path}")
    next_url = safe_next(request, reverse("home"))
    error = None
    if request.method == "POST":
        if not (request.POST.get("accept_terms") and request.POST.get("accept_age")):
            error = "Per continuare spunta tutte e due le caselle."
        else:
            privacy.record_acceptance(request.user, throttle.client_ip(request))
            messages.success(request, "Grazie: accettazione registrata.")
            return redirect(next_url)
    return render(request, "auctions/legal_reaccept.html", {
        **_legal_ctx(), "next": next_url, "error": error,
        "state": privacy.acceptance_state(request.user),
    })


def account_verify(request, token):
    """Il link dell'email di conferma (registrazione o cambio email)."""
    user, email, problem = privacy.check_verify_token(token)
    ok_message, error = "", ""
    if problem == "expired":
        error = ("Il link è scaduto (vale 48 ore). Entra nel tuo account e premi «Rimanda il link» "
                 "nell'avviso in alto o in «Il mio account».")
    elif problem:
        error = "Il link non è valido. Copialo per intero dall'email, oppure chiedine uno nuovo da «Il mio account»."
    else:
        ok_message, error = privacy.apply_verification(user, email)
    return render(request, "auctions/account_verify.html", {"ok_message": ok_message, "error": error})


@require_POST
def account_resend(request):
    if not request.user.is_authenticated:
        return redirect("login")
    back = safe_next(request, reverse("account"))
    row = privacy.privacy_row(request.user)
    target = (row.pending_email if row and row.pending_email else request.user.email)
    if not target:
        messages.error(request, "Il tuo account non ha un'email: aggiungila da «Il mio account».")
        return redirect(back)
    if throttle.blocked(request, "login"):
        messages.error(request, throttle.MESSAGE)
        return redirect(back)
    throttle.failure(request, "login")          # un tetto anche ai reinvii
    ok, err = privacy.send_verification(request, request.user, target)
    if ok:
        messages.success(request, f"Ti abbiamo mandato un nuovo link a {target}: vale 48 ore. Controlla anche lo spam.")
    else:
        messages.error(request, f"Link non inviato: {err}")
    return redirect(back)


# --- Il mio account -----------------------------------------------------------

def _account_frame(request):
    """La cornice: l'app da /app/…, la console per chi gestisce leghe, una
    pagina semplice per gli altri."""
    if in_app(request):
        ctx = page_frame(request, None)
        ctx.update({"active_tab": "altro", "frame_back_url": reverse("app_altro"), "frame_back_label": "Altro"})
        return ctx
    if manageable_leagues(request.user).exists():
        return page_frame(request, None)
    return {"page_frame": "auctions/_frame_plain.html"}


def account_page(request):
    if not request.user.is_authenticated:
        login = reverse("app_login") if in_app(request) else reverse("login")
        return redirect(f"{login}?next={request.path}")
    user = request.user
    row = privacy.privacy_row(user)
    blockers = privacy.deletion_blockers(user)
    here = request.get_full_path()
    ctx = {
        **_account_frame(request),
        **_legal_ctx(),
        "account_user": user,
        "privacy_row": row,
        "email_unverified": privacy.email_unverified(user),
        "pending_email": row.pending_email if row else "",
        "acceptances": user.legal_acceptances.all()[:6],
        "acceptance_state": privacy.acceptance_state(user),
        "teams": user.teams.select_related("league").order_by("league__name", "display_name"),
        "managed": League.objects.filter(owner=user).order_by("name"),
        "co_managed": user.managed_leagues.order_by("name"),
        "blockers": blockers,
        "audit_rows": privacy.audit_for(user)[:30],
        "mail_ready": mail.is_ready(),
        "here": here,
        "page_messages": True,
    }
    return render(request, "auctions/account.html", ctx)


def _back(request):
    return safe_next(request, reverse("account"))


@require_POST
def account_export(request):
    if not request.user.is_authenticated:
        return redirect("login")
    data = privacy.export_user_data(request.user)
    privacy.audit(request.user, "export", target_user=request.user, detail="Scaricati i propri dati")
    body = json.dumps(data, ensure_ascii=False, indent=2)
    resp = HttpResponse(body, content_type="application/json; charset=utf-8")
    stamp = timezone.localtime().strftime("%Y%m%d")
    resp["Content-Disposition"] = f'attachment; filename="fantamanager-dati-{stamp}.json"'
    return resp


@require_POST
def account_email(request):
    """Cambia email: il nuovo indirizzo vale solo dopo il link di conferma.
    Senza posta sul sito (o se l'account non aveva un'email) si cambia subito."""
    if not request.user.is_authenticated:
        return redirect("login")
    user = request.user
    back = _back(request)
    new = (request.POST.get("email") or "").strip()
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    try:
        validate_email(new)
    except ValidationError:
        messages.error(request, "Scrivi un indirizzo email valido (es. nome@esempio.it).")
        return redirect(back)
    if new.lower() == (user.email or "").lower():
        messages.info(request, "È già la tua email.")
        return redirect(back)
    if User.objects.filter(email__iexact=new).exclude(pk=user.pk).exists():
        messages.error(request, "Questa email è già associata a un altro account.")
        return redirect(back)
    row, _ = AccountPrivacy.objects.get_or_create(user=user)
    if mail.is_ready() and mail.link_base(request):
        row.pending_email = new
        row.save(update_fields=["pending_email"])
        ok, err = privacy.send_verification(request, user, new)
        if ok:
            messages.success(request, f"Ti abbiamo scritto a {new}: apri il link entro 48 ore e l'email cambia. "
                                      "Fino ad allora resta quella di prima.")
        else:
            messages.error(request, f"Link non inviato ({err}): l'email resta quella di prima.")
        return redirect(back)
    old = user.email
    user.email = new
    user.save(update_fields=["email"])
    if old:
        user.teams.filter(email__iexact=old).update(email=new)
    row.pending_email = ""
    row.email_verified_at = None if row.self_registered else row.email_verified_at
    row.save(update_fields=["pending_email", "email_verified_at"])
    messages.success(request, "Email cambiata. L'invio email non è attivo su questo sito: la conferma "
                              "arriverà quando lo sarà.")
    return redirect(back)


@require_POST
def account_delete(request):
    if not request.user.is_authenticated:
        return redirect("login")
    user = request.user
    back = _back(request)
    if throttle.blocked(request, "login"):
        messages.error(request, throttle.MESSAGE)
        return redirect(back)
    typed = (request.POST.get("confirm_username") or "").strip()
    password = request.POST.get("password") or ""
    if typed != user.username:
        messages.error(request, "Per confermare scrivi il tuo nome utente esattamente come appare qui sopra.")
        return redirect(back)
    if authenticate(request, username=user.username, password=password) is None:
        throttle.failure(request, "login")
        messages.error(request, "Password errata: l'account non è stato eliminato.")
        return redirect(back)
    if user.is_superuser and not User.objects.filter(is_superuser=True, is_active=True).exclude(pk=user.pk).exists():
        messages.error(request, "Sei l'unico superadmin della piattaforma: nomina prima un altro superadmin.")
        return redirect(back)
    if privacy.deletion_blockers(user):
        messages.error(request, "Prima passa le tue leghe a un altro presidente o eliminale (vedi l'elenco qui sotto).")
        return redirect(back)
    email, name = user.email, user.first_name or user.username
    base = mail.link_base(request)
    report = privacy.delete_account(user)
    logout(request)
    request.session.flush()
    if email and mail.is_ready():
        from django.template.loader import render_to_string

        ctx = {"name": name, "report": report, **(privacy.legal_links(base) if base else {})}
        mail.send("Account eliminato — FantaManager", email,
                  render_to_string("auctions/email/account_deleted.txt", ctx))
    return render(request, "auctions/account_deleted.html", {"report": report})


# --- Disiscrizione dalle email della lega -------------------------------------

def email_unsubscribe(request, token):
    """Il link «Non voglio più ricevere email da questa lega». Si conferma con
    un pulsante: i filtri antispam aprono i link delle email da soli."""
    participant = privacy.check_unsubscribe_token(token)
    if participant is None:
        return render(request, "auctions/email_unsubscribe.html", {"invalid": True}, status=404)
    done = False
    if request.method == "POST":
        privacy.unsubscribe(participant)
        done = True
    return render(request, "auctions/email_unsubscribe.html", {
        "team": participant, "league": participant.league, "done": done,
        "already": participant.email_opt_out_at is not None and not done,
    })
