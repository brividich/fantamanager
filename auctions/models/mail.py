"""Outgoing mail: the provider the platform sends its emails through."""
from django.conf import settings
from django.db import models

from ..crypto import EncryptedTextField


class MailSettings(models.Model):
    """The one SMTP provider of the platform (a singleton, ``pk=1``).

    Set from the console (Impostazioni → Posta) by a superuser. Presets fill
    host, port and security for the common providers; «Solo log» writes the
    emails into the server log instead of sending them, to try things out
    without an account. The password is write-only: the page never shows it.
    """

    class Provider(models.TextChoices):
        SMTP = "smtp", "SMTP personalizzato"
        GMAIL = "gmail", "Gmail / Google Workspace"
        OUTLOOK = "outlook", "Outlook / Microsoft 365"
        BREVO = "brevo", "Brevo (Sendinblue)"
        SENDGRID = "sendgrid", "SendGrid"
        MAILGUN = "mailgun", "Mailgun"
        ARUBA = "aruba", "Aruba"
        CONSOLE = "console", "Solo log (non invia)"

    class Security(models.TextChoices):
        STARTTLS = "starttls", "STARTTLS"
        SSL = "ssl", "SSL/TLS"
        NONE = "none", "Nessuna"

    enabled   = models.BooleanField(default=False)
    provider  = models.CharField(max_length=12, choices=Provider.choices, default=Provider.SMTP)
    host      = models.CharField(max_length=200, blank=True)
    port      = models.PositiveIntegerField(default=587)
    security  = models.CharField(max_length=10, choices=Security.choices, default=Security.STARTTLS)
    username  = models.CharField(max_length=200, blank=True)
    # Encrypted in the database (see ``crypto``); the page never shows it.
    password  = EncryptedTextField(blank=True)
    from_email = models.EmailField(blank=True)
    from_name = models.CharField(max_length=80, blank=True, default="FantaManager")
    reply_to  = models.EmailField(blank=True)
    timeout   = models.PositiveSmallIntegerField(default=15)

    last_test_at    = models.DateTimeField(null=True, blank=True)
    last_test_ok    = models.BooleanField(default=False)
    last_test_error = models.CharField(max_length=300, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "Impostazioni posta"
        verbose_name_plural = "Impostazioni posta"

    def __str__(self):
        return f"{self.get_provider_display()} ({'attiva' if self.enabled else 'spenta'})"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
