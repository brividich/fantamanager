"""Filtri per non far uscire dal sito i dati di chi guarda una pagina.

``{{ url|local_img }}``: un'immagine su un server di terzi (foto e stemmi di
API-Football) passa dal sito (``views/images.py``), così il browser non manda
l'IP del visitatore a quel server.
"""
from django import template

from ..views.images import local_url

register = template.Library()


@register.filter
def local_img(url):
    return local_url(url)
