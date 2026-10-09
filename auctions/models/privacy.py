"""Privacy: accettazioni dei testi legali, stato dell'email di un account e
registro delle azioni sensibili (vedi services/privacy.py e docs/PRIVACY.md)."""
from django.conf import settings
from django.db import models
from django.utils import timezone


class LegalAcceptance(models.Model):
    """Chi ha accettato quale versione dell'informativa o dei termini, e quando.

    Una riga per accettazione: la storia resta (serve a dimostrare il
    consenso), l'ultima per documento è quella che conta.
    """

    class Doc(models.TextChoices):
        PRIVACY = "privacy", "Informativa privacy"
        TERMS = "terms", "Termini d'uso"
        AGE = "age", "Dichiarazione di età (almeno 14 anni)"

    user        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                    related_name="legal_acceptances")
    doc         = models.CharField(max_length=10, choices=Doc.choices)
    version     = models.CharField(max_length=30)
    accepted_at = models.DateTimeField(default=timezone.now)
    ip          = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-accepted_at", "-id"]
        indexes = [models.Index(fields=["user", "doc"])]

    def __str__(self):
        return f"{self.user} · {self.doc} {self.version}"


class AccountPrivacy(models.Model):
    """Lo stato privacy di un account: se si è registrato da solo, se ha
    verificato l'email, quale nuova email aspetta conferma.

    Gli account nati prima di questa tabella non hanno la riga: valgono come
    verificati (nessun cambio per chi c'era già) e la pulizia automatica non li
    tocca mai.
    """
    user              = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                             related_name="privacy")
    self_registered   = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    pending_email     = models.EmailField(blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} ({'verificata' if self.email_verified_at else 'da verificare'})"


class AuditLog(models.Model):
    """Una riga per ogni azione sensibile: «Vedi come», credenziali cambiate da
    un admin per conto di un altro, export dei dati. Il superuser vede tutto
    (Supervisor), l'interessato le righe che lo riguardano (Il mio account)."""

    class Action(models.TextChoices):
        IMPERSONATE = "impersonate", "Vedi come (Supervisor)"
        VIEW_AS = "view_as", "Vedi come (squadra)"
        CREDENTIALS = "credentials", "Credenziali cambiate da un admin"
        EXPORT = "export", "Export dei dati personali"
        EMAIL_REMOVED = "email_removed", "Email della squadra tolta"
        UNLINK = "unlink", "Account scollegato dalla squadra"
        UNSUBSCRIBE = "unsubscribe", "Disiscrizione dalle email della lega"
        ACCOUNT_DELETED = "account_deleted", "Account eliminato"

    actor       = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    actor_name  = models.CharField(max_length=150, blank=True)
    action      = models.CharField(max_length=20, choices=Action.choices)
    target_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    target_name = models.CharField(max_length=150, blank=True)
    league      = models.ForeignKey("League", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    detail      = models.CharField(max_length=200, blank=True)
    created_at  = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.created_at:%d/%m/%Y %H:%M} {self.actor_name} {self.action} {self.target_name}"
