"""Link fra le pagine della console che girano anche nell'app (Regia).

Una pagina della console aperta da /app/regia/… usa la cornice dell'app; i
suoi link verso altre pagine devono restare nell'app quando la pagina di
arrivo ce l'ha. ``{% purl 'admin_players' %}`` è ``{% url %}`` in console e
l'indirizzo dell'app (``APP_PAGES``) dentro l'app.
"""
from django import template
from django.urls import reverse

from ..views.common import app_page_url

register = template.Library()


@register.simple_tag(takes_context=True)
def purl(context, name, *args):
    return app_page_url(context.get("request"), name, *args)
