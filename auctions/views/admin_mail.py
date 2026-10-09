"""Impostazioni → Posta: the SMTP provider the platform sends email through."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render

from ..models import MailSettings
from ..services import mail
from .common import current_league, manageable_leagues, staff_member_required


def _clean_email(raw):
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    try:
        validate_email(raw)
    except ValidationError:
        return raw, f"«{raw}» non è un indirizzo email valido."
    return raw, ""


def _save(request, cfg):
    """Apply the form to ``cfg``; returns the list of problems (empty = saved)."""
    post = request.POST
    errors = []
    provider = post.get("provider") or MailSettings.Provider.SMTP
    if provider not in MailSettings.Provider.values:
        provider = MailSettings.Provider.SMTP
    security = post.get("security") or MailSettings.Security.STARTTLS
    if security not in MailSettings.Security.values:
        security = MailSettings.Security.STARTTLS
    try:
        port = int(post.get("port") or 0)
    except ValueError:
        port = 0
    try:
        timeout = min(120, max(3, int(post.get("timeout") or 15)))
    except ValueError:
        timeout = 15

    from_email, err = _clean_email(post.get("from_email"))
    if err:
        errors.append(err)
    reply_to, err = _clean_email(post.get("reply_to"))
    if err:
        errors.append(err)

    cfg.enabled = post.get("enabled") == "1"
    cfg.provider = provider
    cfg.host = (post.get("host") or "").strip()[:200]
    cfg.port = port if 0 < port < 65536 else (465 if security == "ssl" else 587)
    cfg.security = security
    cfg.username = (post.get("username") or "").strip()[:200]
    # The password is never sent back to the page: empty keeps the saved one.
    if post.get("clear_password") == "1":
        cfg.password = ""
    elif post.get("password"):
        cfg.password = post.get("password")[:300]
    cfg.from_email = from_email
    cfg.from_name = (post.get("from_name") or "").strip()[:80]
    cfg.reply_to = reply_to
    cfg.timeout = timeout

    if cfg.enabled and provider != MailSettings.Provider.CONSOLE:
        if not cfg.host:
            errors.append("Indica il server SMTP (host).")
        if not cfg.from_email:
            errors.append("Indica l'indirizzo del mittente.")
    if not errors:
        cfg.updated_by = request.user
        cfg.save()
    return errors


@staff_member_required
def admin_mail_settings(request):
    """The page where a superuser picks the provider, saves it and sends a test."""
    if not request.user.is_superuser:
        return HttpResponseForbidden("Solo l'amministratore della piattaforma configura la posta.")
    cfg = MailSettings.get()

    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "save":
            errors = _save(request, cfg)
            if errors:
                for e in errors:
                    messages.error(request, e)
            else:
                ready, _src = mail.status(cfg)
                if cfg.enabled and ready:
                    messages.success(request, "Posta salvata e attiva. Manda un'email di prova per essere sicuro che parta.")
                elif cfg.enabled:
                    messages.warning(request, "Salvata, ma mancano dei dati: la posta non partirà.")
                else:
                    messages.info(request, "Impostazioni salvate. La posta dalla console è spenta.")
        elif action == "test":
            to, err = _clean_email(request.POST.get("test_to"))
            if err or not to:
                messages.error(request, err or "Indica a chi mandare l'email di prova.")
            else:
                ok, error = mail.send_test(to, cfg)
                if ok:
                    messages.success(request, f"Email di prova inviata a {to}. Controlla la posta (anche lo spam).")
                else:
                    messages.error(request, f"Email di prova non partita: {error}")
        return redirect("admin_mail_settings")

    ready, source = mail.status(cfg)
    return render(request, "auctions/admin_mail.html", {
        "cfg": cfg,
        "ready": ready,
        "source": source,
        "providers": MailSettings.Provider.choices,
        "securities": MailSettings.Security.choices,
        "presets": mail.PRESETS,
        "link_base": mail.link_base(request),
        "current_league": current_league(request),
        "leagues": manageable_leagues(request.user),
        "console_section": "Posta",
        "console_active": "mail",
    })
