"""
ASGI entrypoint.

Routes plain HTTP through Django and WebSocket traffic through the Channels
consumer stack (with session/auth available in the consumer scope).
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "liveauction.settings")

# Initialise Django (loads apps/models) before importing anything that
# touches the ORM, such as our WebSocket routing/consumers.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

import auctions.routing  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(
            URLRouter(auctions.routing.websocket_urlpatterns)
        ),
    }
)
