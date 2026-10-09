"""«Password dimenticata?»: a link by email that lets the owner of an account
choose a new password.

The link carries Django's one-time token (it stops working once the password
changes, and after PASSWORD_RESET_TIMEOUT). The page always answers the same
way whether the account exists or not, so it can't be used to find out who is
registered; requests count against the same ceiling as wrong logins. The
email goes through the platform's mail settings (Impostazioni → Posta)."""
import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.db.models import Q
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .. import throttle
from ..services import mail
from .common import password_problem

logger = logging.getLogger("auctions.auth")

SENT_MESSAGE = ("Se l'account esiste e ha un'email, ti abbiamo scritto un link per scegliere una nuova "
                "password. Controlla anche lo spam.")


def password_reset_request(request):
    if request.method == "POST":
        if throttle.blocked(request, "login"):
            return render(request, "auctions/password_reset.html", {"error": throttle.MESSAGE})
        identifier = (request.POST.get("identifier") or "").strip()
        if not mail.is_ready():
            return render(request, "auctions/password_reset.html", {
                "error": "La posta della piattaforma non è configurata: chiedi all'amministratore di "
                         "reimpostare la password."})
        base = mail.link_base(request)
        if not base:
            # The link would be built from whatever Host the request claims.
            logger.warning("Reset password rifiutato: %s", mail.NO_LINK_BASE_MESSAGE)
            return render(request, "auctions/password_reset.html", {
                "error": "Il reset via email non è attivo su questa installazione: chi la gestisce "
                         "deve impostare FM_SITE_URL (l'indirizzo pubblico del sito). Intanto chiedi "
                         "all'amministratore di reimpostarti la password."})
        throttle.failure(request, "login")        # every request counts: no free probing
        if identifier:
            User = get_user_model()
            users = User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier),
                                        is_active=True).exclude(email="")
            for user in users[:3]:
                _send_link(base, user)
        return render(request, "auctions/password_reset.html", {"sent": True, "message": SENT_MESSAGE})
    return render(request, "auctions/password_reset.html", {})


def _send_link(base, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    link = base + reverse("password_reset_confirm", args=[uid, token])
    ctx = {"user": user, "link": link}
    ok, error = mail.send("Nuova password — FantaManager", user.email,
                          render_to_string("auctions/email/password_reset.txt", ctx))
    if not ok:
        logger.warning("Link di reset password non inviato a %s: %s", user.username, error)


def password_reset_confirm(request, uidb64, token):
    User = get_user_model()
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uidb64)), is_active=True)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        user = None
    if user is None or not default_token_generator.check_token(user, token):
        return render(request, "auctions/password_reset.html", {
            "invalid": True, "error": "Il link non è valido o è scaduto: chiedine uno nuovo."})

    error = None
    if request.method == "POST":
        password = request.POST.get("password") or ""
        if password != (request.POST.get("password_confirm") or ""):
            error = "Le due password non coincidono."
        elif (weak := password_problem(password, user)):
            error = weak
        else:
            user.set_password(password)
            user.save(update_fields=["password"])
            logger.info("Password reimpostata via email per %s", user.username)
            messages.success(request, "Password cambiata: ora puoi accedere.")
            return redirect("login")
    return render(request, "auctions/password_reset.html", {"confirm": True, "error": error,
                                                             "account": user.username})
