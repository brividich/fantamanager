"""Outgoing email through the provider set in Impostazioni → Posta.

The provider lives in the database (``MailSettings``), so the league admin
sets it from the console without touching files. When it is off, the classic
Django ``EMAIL_*`` settings from the environment are used, if an
``EMAIL_HOST`` is given. With neither, nothing is sent: callers ask
``is_ready()`` first and say so on the page.

Every sender returns ``(ok, error)`` instead of raising: a mail server that
is down must never break the action the email only reports on.
"""
import logging
from email.utils import formataddr

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.mail.backends.smtp import EmailBackend
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger("auctions.mail")

# Host, port and security of the providers the settings page offers.
PRESETS = {
    "gmail":    {"host": "smtp.gmail.com", "port": 587, "security": "starttls",
                 "hint": "Serve una «password per le app» (Account Google → Sicurezza → Verifica in due passaggi → Password per le app)."},
    "outlook":  {"host": "smtp.office365.com", "port": 587, "security": "starttls",
                 "hint": "Utente = indirizzo completo. Sugli account aziendali l'SMTP autenticato va abilitato dall'amministratore."},
    "brevo":    {"host": "smtp-relay.brevo.com", "port": 587, "security": "starttls",
                 "hint": "Utente e chiave SMTP si trovano in Brevo → SMTP & API. Il mittente deve essere verificato."},
    "sendgrid": {"host": "smtp.sendgrid.net", "port": 587, "security": "starttls",
                 "hint": "Utente: apikey (proprio questa parola). Password: la tua API key di SendGrid."},
    "mailgun":  {"host": "smtp.mailgun.org", "port": 587, "security": "starttls",
                 "hint": "Credenziali SMTP del dominio (Mailgun → Sending → Domain settings). Per l'UE: smtp.eu.mailgun.org."},
    "aruba":    {"host": "smtps.aruba.it", "port": 465, "security": "ssl",
                 "hint": "Utente = indirizzo email completo della casella Aruba."},
    "smtp":     {"host": "", "port": 587, "security": "starttls",
                 "hint": "Qualsiasi server SMTP: chiedi host, porta e sicurezza al tuo provider."},
    "console":  {"host": "", "port": 0, "security": "none",
                 "hint": "Le email non partono: finiscono nel log del server. Utile per provare."},
}


def get_settings():
    from ..models import MailSettings
    return MailSettings.get()


def _env_ready():
    return bool(getattr(settings, "EMAIL_HOST", "") and settings.EMAIL_HOST != "localhost")


def status(cfg=None):
    """``(ready, source)``: can we send, and through what ("db", "env", "")."""
    cfg = cfg or get_settings()
    if cfg.enabled:
        if cfg.provider == "console":
            return True, "db"
        if cfg.host and cfg.from_email:
            return True, "db"
        return False, ""
    if _env_ready():
        return True, "env"
    return False, ""


def is_ready():
    try:
        return status()[0]
    except Exception:  # the table may not exist yet (migrations pending)
        return False


def connection(cfg=None):
    """A mail connection for the configured provider."""
    cfg = cfg or get_settings()
    if not cfg.enabled:
        return get_connection()
    if cfg.provider == "console":
        return get_connection("django.core.mail.backends.console.EmailBackend")
    return EmailBackend(
        host=cfg.host,
        port=cfg.port or 587,
        username=cfg.username or None,
        password=cfg.password or None,
        use_tls=cfg.security == "starttls",
        use_ssl=cfg.security == "ssl",
        timeout=cfg.timeout or 15,
        fail_silently=False,
    )


def from_address(cfg=None):
    cfg = cfg or get_settings()
    if cfg.enabled and cfg.from_email:
        return formataddr((cfg.from_name, cfg.from_email)) if cfg.from_name else cfg.from_email
    return settings.DEFAULT_FROM_EMAIL


def send(subject, to, text, html=None, reply_to=None, cfg=None, conn=None):
    """Send one email. ``to`` is an address or a list. Returns ``(ok, error)``."""
    cfg = cfg or get_settings()
    ready, _source = status(cfg)
    if not ready:
        return False, "Posta non configurata."
    recipients = [to] if isinstance(to, str) else [t for t in to if t]
    if not recipients:
        return False, "Nessun destinatario."
    reply = [reply_to] if reply_to else ([cfg.reply_to] if cfg.enabled and cfg.reply_to else None)
    msg = EmailMultiAlternatives(
        subject=subject, body=text, from_email=from_address(cfg), to=recipients,
        reply_to=reply, connection=conn or connection(cfg),
    )
    if html:
        msg.attach_alternative(html, "text/html")
    try:
        msg.send(fail_silently=False)
    except Exception as exc:  # smtplib, socket, ssl… all end up here
        logger.warning("Invio email fallito a %s: %s", ", ".join(recipients), exc)
        return False, _explain(exc)
    logger.info("Email «%s» inviata a %s", subject, ", ".join(recipients))
    return True, ""


def _explain(exc):
    """A short Italian reason for a failed send, readable by a league admin."""
    import smtplib
    import socket
    import ssl
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "Credenziali rifiutate dal server (utente o password errati)."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "Il server non accetta questo mittente: usa un indirizzo verificato del tuo account."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "Il server ha rifiutato il destinatario."
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Il server non risponde (timeout): controlla host, porta e rete."
    if isinstance(exc, ssl.SSLError):
        return "Errore SSL/TLS: prova l'altra modalità di sicurezza o la porta giusta (587 STARTTLS, 465 SSL)."
    if isinstance(exc, (ConnectionRefusedError, socket.gaierror, OSError)):
        return f"Impossibile raggiungere il server: {exc}"
    return str(exc)[:280] or exc.__class__.__name__


def send_test(to, cfg=None):
    """Send the test email and remember how it went on the settings."""
    cfg = cfg or get_settings()
    text = (
        "Ciao!\n\nSe leggi questo messaggio la posta di FantaManager funziona: "
        "inviti alle squadre e avvisi del mercato partiranno da qui.\n\n"
        f"Provider: {cfg.get_provider_display() if cfg.enabled else 'impostazioni del server'}\n"
        f"Inviata il {timezone.localtime():%d/%m/%Y alle %H:%M}.\n"
    )
    ok, error = send("FantaManager · email di prova", to, text, cfg=cfg)
    if cfg.pk:
        cfg.last_test_at = timezone.now()
        cfg.last_test_ok = ok
        cfg.last_test_error = error[:300]
        cfg.save(update_fields=["last_test_at", "last_test_ok", "last_test_error"])
    return ok, error


# --- Emails of the league ----------------------------------------------------

def league_recipients(league):
    """The league's active teams that have an address to write to."""
    from ..models import Participant
    teams = Participant.objects.filter(league=league, is_active=True).select_related("user")
    return [p for p in teams if p.contact_email]


def _app_link(request, participant, path_name="app_home"):
    """Personal link that signs the team into the app and opens ``path_name``."""
    from .. import remote
    base = remote.best_base_url(request).rstrip("/")
    nxt = reverse(path_name)
    return f"{base}{reverse('app_login')}?t={participant.public_token}&next={nxt}"


def _send_to_teams(request, teams, subject, template, extra):
    """Render ``template`` (.txt and .html) for each team and send it through
    one connection. Returns ``{"sent", "failed", "skipped", "errors"}``."""
    report = {"sent": 0, "failed": 0, "skipped": 0, "errors": []}
    cfg = get_settings()
    if not status(cfg)[0]:
        report["errors"].append("Posta non configurata.")
        report["skipped"] = len(teams)
        return report
    conn = connection(cfg)
    try:
        conn.open()
    except Exception as exc:
        report["failed"] = len(teams)
        report["errors"].append(_explain(exc))
        return report
    try:
        for p in teams:
            address = p.contact_email
            if not address:
                report["skipped"] += 1
                continue
            ctx = {"team": p, "league": p.league, **extra(p)}
            text = render_to_string(f"auctions/email/{template}.txt", ctx)
            html = render_to_string(f"auctions/email/{template}.html", ctx)
            ok, error = send(subject, address, text, html=html, cfg=cfg, conn=conn)
            if ok:
                report["sent"] += 1
            else:
                report["failed"] += 1
                if error not in report["errors"]:
                    report["errors"].append(error)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return report


def send_team_invites(request, league, teams=None):
    """Each team gets its personal app link and access code."""
    teams = league_recipients(league) if teams is None else teams
    return _send_to_teams(
        request, teams, f"Benvenuto in {league.name}", "invite",
        lambda p: {"link": _app_link(request, p), "code": p.access_code},
    )


def send_market_notice(request, session):
    """Tell the league's teams a buste session is open (or scheduled)."""
    league = session.league
    return _send_to_teams(
        request, league_recipients(league), f"{league.name} · mercato a buste: {session.title}", "market_open",
        lambda p: {"session": session, "link": _app_link(request, p, "app_mercato")},
    )


def report_message(report, what="email"):
    """One line for a Django message out of a ``_send_to_teams`` report."""
    parts = []
    if report["sent"]:
        parts.append(f"{report['sent']} {what} inviat{'a' if report['sent'] == 1 else 'e'}")
    if report["failed"]:
        parts.append(f"{report['failed']} non partit{'a' if report['failed'] == 1 else 'e'}")
    if report["skipped"]:
        parts.append(f"{report['skipped']} squadr{'a' if report['skipped'] == 1 else 'e'} senza indirizzo")
    line = ", ".join(parts) or "Nessuna email da inviare"
    if report["errors"]:
        line += f" — {report['errors'][0]}"
    return line
