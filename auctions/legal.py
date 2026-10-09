"""Versioni dei testi legali (informativa privacy e termini d'uso).

I testi stanno nei template ``legal_privacy.html`` e ``legal_terms.html``.
Quando uno dei due cambia in modo sostanziale, si alza qui la sua versione e
la data: chi aveva accettato la versione precedente, al primo accesso, vede
la pagina breve di riaccettazione (``privacy_reaccept``).

BOZZA: i testi sono una bozza da far rivedere a un professionista prima di
aprire il servizio a utenti esterni.
"""
import datetime

PRIVACY_VERSION = "1.0-bozza"
TERMS_VERSION = "1.0-bozza"
LEGAL_DATE = datetime.date(2026, 10, 9)

# Art. 2-quinquies del Codice privacy (d.lgs. 196/2003): in Italia il consenso
# ai servizi della società dell'informazione vale da 14 anni.
MIN_AGE = 14

CURRENT = {"privacy": PRIVACY_VERSION, "terms": TERMS_VERSION}
