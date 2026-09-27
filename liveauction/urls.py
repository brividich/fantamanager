"""Project URL configuration."""
import re

from django.conf import settings
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path, re_path
from django.views.static import serve

from auctions import throttle


def media(request, path):
    """Uploaded team logos. ``static()`` only serves them with DEBUG on, which is
    why deployments used to keep DEBUG on; a NAS has no separate web server in
    front of Daphne, so the app serves them itself. ``serve`` resolves the path
    safely under MEDIA_ROOT and never lists directories."""
    return serve(request, path, document_root=settings.MEDIA_ROOT)


_admin_login = admin.site.login


def _throttled_admin_login(request, extra_context=None):
    """Django's admin login under the same failed-attempt ceiling as the app's
    own: it is a second door to the same superadmin password."""
    if request.method == "POST" and throttle.blocked(request, "login"):
        return HttpResponse(throttle.MESSAGE, status=429)
    response = _admin_login(request, extra_context)
    if request.method == "POST" and response.status_code == 200:   # the form came back: wrong credentials
        throttle.failure(request, "login")
    return response


admin.site.login = _throttled_admin_login

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("", include("auctions.urls")),
    re_path(r"^%s(?P<path>.*)$" % re.escape(settings.MEDIA_URL.lstrip("/")), media),
]
