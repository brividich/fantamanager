"""Master Supervisor Cockpit: Server Health, User/League Management, Logs & Reporting."""
from datetime import datetime
from decimal import Decimal
from functools import wraps
import json
import logging
import os
import platform
import resource
import shutil
import sys
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.db import connection, transaction
from django.db.models import Count, Sum
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

import django
from ..models import Auction, Bid, League, Participant, Player
from ..consumers import _ROOM_TICKERS

logger = logging.getLogger(__name__)

# Track process start time for uptime calculation
_PROCESS_START_TIME = time.time()


def supervisor_required(view_func):
    """Ensure the user is logged in as a platform Superadmin."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={request.get_full_path()}")
        if not request.user.is_superuser:
            return HttpResponseForbidden("Accesso negato: quest'area è riservata al Superadmin Master di piattaforma.")
        return view_func(request, *args, **kwargs)
    return _wrapped


def _get_server_metrics():
    """Gather low-overhead server health & process metrics using python standard library."""
    now = time.time()
    uptime_seconds = int(now - _PROCESS_START_TIME)
    uptime_str = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m {uptime_seconds % 60}s"

    # Memory RSS
    rusage = resource.getrusage(resource.RUSAGE_SELF)
    # macOS reports in bytes, Linux in KB
    if sys.platform == "darwin":
        rss_mb = rusage.ru_maxrss / (1024 * 1024)
    else:
        rss_mb = rusage.ru_maxrss / 1024

    # Disk usage
    disk_total_gb = 0
    disk_used_gb = 0
    disk_free_gb = 0
    disk_percent = 0
    try:
        du = shutil.disk_usage(settings.BASE_DIR)
        disk_total_gb = round(du.total / (1024 ** 3), 1)
        disk_used_gb = round(du.used / (1024 ** 3), 1)
        disk_free_gb = round(du.free / (1024 ** 3), 1)
        disk_percent = round((du.used / du.total) * 100, 1)
    except Exception:
        pass

    # Database file size
    db_size_mb = 0
    db_path = settings.DATABASES.get("default", {}).get("NAME")
    if db_path and os.path.exists(str(db_path)):
        try:
            db_size_mb = round(os.path.getsize(str(db_path)) / (1024 * 1024), 2)
        except Exception:
            pass

    # Load average (Unix)
    load_avg = "N/A"
    try:
        lavg = os.getloadavg()
        load_avg = f"{lavg[0]:.2f}, {lavg[1]:.2f}, {lavg[2]:.2f}"
    except (AttributeError, OSError):
        pass

    # WebSockets room tickers
    active_tickers_count = len(_ROOM_TICKERS)

    return {
        "os_name": platform.platform(),
        "python_version": sys.version.split()[0],
        "django_version": django.get_version(),
        "uptime": uptime_str,
        "rss_mb": round(rss_mb, 1),
        "disk_total_gb": disk_total_gb,
        "disk_used_gb": disk_used_gb,
        "disk_free_gb": disk_free_gb,
        "disk_percent": disk_percent,
        "db_size_mb": db_size_mb,
        "load_avg": load_avg,
        "active_rooms": active_tickers_count,
        "channels_status": "Daphne / ASGI Online",
    }


def _read_recent_logs(max_lines=250, level_filter=None, query_filter=None):
    """Read and parse recent log lines from server.log."""
    log_file = settings.BASE_DIR / "server.log"
    if not os.path.exists(log_file):
        return []

    lines = []
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            recent = all_lines[-max_lines:]
            recent.reverse()

            for line in recent:
                line_str = line.strip()
                if not line_str:
                    continue

                # Filter by log level if requested
                if level_filter and level_filter != "ALL":
                    if f"[{level_filter}]" not in line_str:
                        continue

                # Filter by keyword if requested
                if query_filter:
                    if query_filter.lower() not in line_str.lower():
                        continue

                lines.append(line_str)
    except Exception as e:
        logger.exception("Errore durante la lettura dei log: %s", e)
        lines.append(f"[ERROR] Impossibile leggere server.log: {e}")

    return lines


@supervisor_required
def supervisor_dashboard(request):
    """Comprehensive Master Supervisor Cockpit."""
    tab = request.GET.get("tab", "health")

    # Handle administrative actions
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "toggle_user_active":
            uid = request.POST.get("user_id")
            user = get_object_or_404(User, pk=uid)
            if user == request.user:
                messages.error(request, "Non puoi disattivare il tuo stesso account superadmin.")
            else:
                user.is_active = not user.is_active
                user.save(update_fields=["is_active"])
                messages.success(request, f"Stato account '{user.username}' aggiornato: {'Attivo' if user.is_active else 'Disattivato'}.")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=users")

        elif action == "toggle_user_admin":
            uid = request.POST.get("user_id")
            user = get_object_or_404(User, pk=uid)
            if user == request.user:
                messages.error(request, "Non puoi modificare il tuo stesso ruolo superadmin.")
            else:
                user.is_superuser = not user.is_superuser
                user.is_staff = user.is_superuser
                user.save(update_fields=["is_superuser", "is_staff"])
                messages.success(request, f"Ruolo di '{user.username}' aggiornato: {'Superadmin' if user.is_superuser else 'Utente Standard'}.")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=users")

        elif action == "create_user":
            username = (request.POST.get("username") or "").strip()
            email = (request.POST.get("email") or "").strip()
            password = request.POST.get("password") or ""
            is_super = request.POST.get("is_superuser") == "on"
            league_id = (request.POST.get("league_id") or "").strip()
            is_league_owner = request.POST.get("is_league_owner") == "on"
            assign_team = request.POST.get("assign_team") == "on"
            team_mode = request.POST.get("team_mode", "new")
            team_name = (request.POST.get("team_name") or "").strip()
            team_credits_raw = (request.POST.get("team_credits") or "").strip()
            existing_team_id = (request.POST.get("existing_team_id") or "").strip()

            if not username or not password:
                messages.error(request, "Username e password sono obbligatori.")
                return redirect(f"{reverse('supervisor_dashboard')}?tab=users")

            if User.objects.filter(username__iexact=username).exists():
                messages.error(request, f"Username '{username}' già esistente.")
                return redirect(f"{reverse('supervisor_dashboard')}?tab=users")

            try:
                with transaction.atomic():
                    new_user = User.objects.create_user(
                        username=username,
                        email=email,
                        password=password,
                        is_superuser=is_super,
                        is_staff=is_super,
                    )

                    status_parts = []

                    if league_id:
                        league = League.objects.filter(pk=league_id).first()
                        if not league:
                            raise ValueError(f"Lega selezionata (ID {league_id}) non trovata.")

                        if is_league_owner:
                            league.owner = new_user
                            league.save(update_fields=["owner"])
                            status_parts.append(f"Admin della lega '{league.name}'")

                        if assign_team:
                            if team_mode == "new":
                                final_team_name = team_name or f"Team {username}"
                                try:
                                    credits_val = Decimal(team_credits_raw) if team_credits_raw else league.budget
                                except Exception:
                                    credits_val = league.budget

                                participant = Participant.objects.create(
                                    league=league,
                                    user=new_user,
                                    display_name=final_team_name[:80],
                                    credits=credits_val,
                                    is_active=True,
                                )
                                status_parts.append(f"Squadra '{participant.display_name}' ({credits_val} FM)")
                            elif team_mode == "existing":
                                if not existing_team_id:
                                    raise ValueError("Seleziona una squadra valida da assegnare.")
                                participant = Participant.objects.filter(pk=existing_team_id, league=league).first()
                                if not participant:
                                    raise ValueError("Squadra selezionata non valida per la lega indicata.")
                                participant.user = new_user
                                participant.save(update_fields=["user"])
                                status_parts.append(f"Squadra '{participant.display_name}'")

                    if is_super:
                        status_parts.append("Superadmin Master")

                    desc = f" ({', '.join(status_parts)})" if status_parts else ""
                    messages.success(request, f"Utente '{username}' creato con successo{desc}.")
                    logger.info("Supervisor ha creato l'utente %s%s", username, desc)
            except Exception as e:
                logger.exception("Errore durante la creazione dell'utente: %s", e)
                messages.error(request, f"Errore durante la creazione dell'utente: {e}")

            return redirect(f"{reverse('supervisor_dashboard')}?tab=users")

        elif action == "delete_league":
            lid = request.POST.get("league_id")
            league = get_object_or_404(League, pk=lid)
            name = league.name
            league.delete()
            messages.success(request, f"Lega '{name}' eliminata definitivamente.")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=leagues")

        elif action == "assign_league_owner":
            lid = request.POST.get("league_id")
            uid = request.POST.get("owner_id")
            league = get_object_or_404(League, pk=lid)
            new_owner = User.objects.filter(pk=uid).first() if uid else None
            league.owner = new_owner
            league.save(update_fields=["owner"])
            messages.success(request, f"Proprietario della lega '{league.name}' aggiornato a: {new_owner.username if new_owner else 'Nessuno'}.")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=leagues")

        elif action == "vacuum_db":
            try:
                with connection.cursor() as cursor:
                    cursor.execute("VACUUM;")
                messages.success(request, "Database ottimizzato con successo (VACUUM eseguito).")
            except Exception as e:
                messages.error(request, f"Errore durante l'ottimizzazione del database: {e}")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=reports")

        elif action == "clear_sessions":
            try:
                deleted_count, _ = Session.objects.filter(expire_date__lt=timezone.now()).delete()
                messages.success(request, f"Pulizia completata: {deleted_count} sessioni scadute rimosse.")
            except Exception as e:
                messages.error(request, f"Errore durante la pulizia delle sessioni: {e}")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=reports")

    # Metrics & System Health
    metrics = _get_server_metrics()

    # User Management Data with eager loading to prevent N+1 queries
    users = list(
        User.objects.prefetch_related(
            "leagues",
            "teams__league",
        ).annotate(
            owned_leagues_count=Count("leagues", distinct=True),
            teams_count=Count("teams", distinct=True),
        ).order_by("-date_joined")
    )

    # League Management Data
    leagues = list(
        League.objects.select_related("owner")
        .prefetch_related("participants__user")
        .annotate(
            teams_count=Count("participants", distinct=True),
            auctions_count=Count("auctions", distinct=True),
            players_count=Count("players", distinct=True),
        )
        .order_by("name")
    )

    # Serialize league participants map for frontend dynamic modals
    league_teams_map = {
        str(lg.id): [
            {
                "id": p.id,
                "display_name": p.display_name,
                "has_user": bool(p.user_id),
                "username": p.user.username if p.user else None,
            }
            for p in lg.participants.all()
        ]
        for lg in leagues
    }
    league_teams_json = json.dumps(league_teams_map)

    # Enrich leagues with active auction status
    for lg in leagues:
        active_a = Auction.objects.filter(
            league=lg, status__in=[Auction.Status.LIVE, Auction.Status.PAUSED]
        ).first()
        lg.active_auction = active_a

    # Logs Data
    level_filter = request.GET.get("level", "ALL")
    query_filter = request.GET.get("q", "").strip()
    recent_logs = _read_recent_logs(max_lines=200, level_filter=level_filter, query_filter=query_filter)

    # Reporting & Global KPIs
    total_users = User.objects.count()
    total_leagues = len(leagues)
    total_teams = Participant.objects.count()
    total_players = Player.objects.count()
    total_auctions = Auction.objects.count()
    live_auctions_count = Auction.objects.filter(status=Auction.Status.LIVE).count()
    total_bids = Bid.objects.count()
    total_credits_spent = Participant.objects.aggregate(total=Sum("spent_credits"))["total"] or 0

    return render(
        request,
        "auctions/supervisor.html",
        {
            "tab": tab,
            "metrics": metrics,
            "users": users,
            "leagues": leagues,
            "league_teams_json": league_teams_json,
            "recent_logs": recent_logs,
            "level_filter": level_filter,
            "query_filter": query_filter,
            "kpis": {
                "total_users": total_users,
                "total_leagues": total_leagues,
                "total_teams": total_teams,
                "total_players": total_players,
                "total_auctions": total_auctions,
                "live_auctions_count": live_auctions_count,
                "total_bids": total_bids,
                "total_credits_spent": total_credits_spent,
            },
        },
    )
