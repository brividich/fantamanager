"""Master Supervisor Cockpit: Server Health, User/League Management, Logs & Reporting."""
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from functools import wraps
import json
import logging
import os
import platform
try:
    import resource
except ImportError:
    resource = None
import shutil
import sys
import time
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.db.models import Count, Sum
from django.http import FileResponse, Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils import timezone

import django
from .. import backup
from ..models import Auction, Bid, League, MailSettings, Participant, Player
from ..consumers import _ROOM_TICKERS
from ..services import mail
from ..services.voti_live import LiveSyncManager

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
    rss_mb = 0
    if resource:
        try:
            rusage = resource.getrusage(resource.RUSAGE_SELF)
            # macOS reports in bytes, Linux in KB
            if sys.platform == "darwin":
                rss_mb = rusage.ru_maxrss / (1024 * 1024)
            else:
                rss_mb = rusage.ru_maxrss / 1024
        except Exception:
            pass

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

    # Database: the engine actually in use (PostgreSQL in Docker, SQLite on the
    # desktop), its size, and the newest backup on disk.
    db = settings.DATABASES.get("default", {})
    db_size_mb = 0
    if connection.vendor == "postgresql":
        db_engine, db_where = "PostgreSQL", f"{db.get('NAME')} su {db.get('HOST') or 'localhost'}"
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_database_size(current_database())")
                db_size_mb = round(cursor.fetchone()[0] / (1024 * 1024), 2)
        except Exception:
            pass
    else:
        db_path = db.get("NAME")
        db_engine, db_where = "SQLite", f"File: {os.path.basename(str(db_path or ''))}"
        if db_path and os.path.exists(str(db_path)):
            try:
                db_size_mb = round(os.path.getsize(str(db_path)) / (1024 * 1024), 2)
            except Exception:
                pass
    last_backup = backup.latest_backup()
    if last_backup:
        # A day without a backup means the backup service is not running.
        last_backup["stale"] = timezone.now() - last_backup["at"] > timedelta(hours=24)

    # Load average (Unix)
    load_avg = "N/A"
    try:
        lavg = os.getloadavg()
        load_avg = f"{lavg[0]:.2f}, {lavg[1]:.2f}, {lavg[2]:.2f}"
    except (AttributeError, OSError):
        pass

    # WebSockets room tickers
    active_tickers_count = len(_ROOM_TICKERS)

    # Outgoing mail status
    mail_summary = "Non configurata"
    mail_ready_flag = False
    try:
        m_cfg = MailSettings.get()
        mail_ready_flag, _ = mail.status(m_cfg)
        if m_cfg.enabled and mail_ready_flag:
            mail_summary = f"Attiva ({m_cfg.get_provider_display()})"
        elif mail_ready_flag:
            mail_summary = "Attiva (Variabili ENV)"
        elif m_cfg.enabled:
            mail_summary = "Incompleta"
        else:
            mail_summary = "Disattivata"
    except Exception:
        pass

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
        "db_engine": db_engine,
        "db_where": db_where,
        "last_backup": last_backup,
        "load_avg": load_avg,
        "active_rooms": active_tickers_count,
        "channels_status": "Daphne / ASGI Online",
        "mail_status": mail_summary,
        "mail_ready": mail_ready_flag,
    }


def _get_recent_backups(limit=8):
    """List recent backup snapshots on disk for the supervisor cockpit."""
    if backup._is_postgres():
        folder = backup._backup_dir()
        pattern = "pg-*.sql.gz"
    else:
        src = backup._db_path()
        if src is None:
            return [], ""
        folder = src.parent / "backups"
        pattern = "db-*.sqlite3"
    try:
        files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    results = []
    for f in files[:limit]:
        try:
            sz = f.stat().st_size
            sz_str = f"{sz / 1024:.1f} KB" if sz < 1024 * 1024 else f"{sz / (1024 * 1024):.2f} MB"
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=dt_timezone.utc)
            results.append({
                "name": f.name,
                "size_str": sz_str,
                "mtime": mtime,
                "path": str(f),
            })
        except OSError:
            pass
    return results, str(folder)


def _get_system_settings_info():
    """Extract live Django configuration flags and paths for superadmin audit."""
    db_cfg = settings.DATABASES.get("default", {})
    return {
        "debug": settings.DEBUG,
        "secret_key_safe": settings.SECRET_KEY != "dev-insecure-change-me-before-anything-public",
        "allowed_hosts": list(settings.ALLOWED_HOSTS),
        "csrf_trusted_origins": getattr(settings, "CSRF_TRUSTED_ORIGINS", []),
        "session_engine": getattr(settings, "SESSION_ENGINE", "django.contrib.sessions.backends.db").split(".")[-1],
        "time_zone": getattr(settings, "TIME_ZONE", "UTC"),
        "language_code": getattr(settings, "LANGUAGE_CODE", "it-it"),
        "db_engine_name": db_cfg.get("ENGINE", "").split(".")[-1],
        "static_root": str(getattr(settings, "STATIC_ROOT", settings.BASE_DIR / "staticfiles")),
        "media_root": str(getattr(settings, "MEDIA_ROOT", settings.BASE_DIR / "media")),
        "backup_dir": str(getattr(settings, "BACKUP_DIR", settings.BASE_DIR / "backups")),
        "has_whitenoise": getattr(settings, "_HAS_WHITENOISE", False),
    }


def _get_active_log_file():
    """Detect the active log file location."""
    candidates = []
    if hasattr(settings, "LOG_FILE_PATH") and settings.LOG_FILE_PATH:
        candidates.append(Path(settings.LOG_FILE_PATH))
    candidates.extend([
        settings.BASE_DIR / "logs" / "fantamanager.log",
        settings.BASE_DIR / "server.log",
    ])
    for c in candidates:
        if c and c.exists() and c.is_file():
            return c
    # Fallback to any .log file in logs directory
    logs_dir = settings.BASE_DIR / "logs"
    if logs_dir.exists() and logs_dir.is_dir():
        log_files = sorted(logs_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        if log_files:
            return log_files[0]
    return candidates[0] if candidates else settings.BASE_DIR / "logs" / "fantamanager.log"


def _read_recent_logs(max_lines=250, level_filter=None, query_filter=None):
    """Read and parse recent log lines from the active application log file."""
    log_file = _get_active_log_file()
    log_name = log_file.name if log_file else "fantamanager.log"
    if not log_file or not os.path.exists(log_file):
        return [], log_name

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
        lines.append(f"[ERROR] Impossibile leggere {log_name}: {e}")

    return lines, log_name


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

        elif action == "edit_user":
            uid = request.POST.get("user_id")
            user_obj = get_object_or_404(User, pk=uid)
            username = (request.POST.get("username") or "").strip()
            email = (request.POST.get("email") or "").strip()
            first_name = (request.POST.get("first_name") or "").strip()[:150]
            last_name = (request.POST.get("last_name") or "").strip()[:150]
            new_password = (request.POST.get("password") or "").strip()
            role = request.POST.get("role", "user")  # "superadmin", "staff", "user"
            is_active = request.POST.get("is_active") == "on" or request.POST.get("is_active") == "1"

            if not username:
                messages.error(request, "Il nome utente non può essere vuoto.")
                return redirect(f"{reverse('supervisor_dashboard')}?tab=users")

            if User.objects.filter(username__iexact=username).exclude(pk=user_obj.pk).exists():
                messages.error(request, f"Lo username '{username}' è già utilizzato da un altro utente.")
                return redirect(f"{reverse('supervisor_dashboard')}?tab=users")

            if email:
                try:
                    validate_email(email)
                except ValidationError:
                    messages.error(request, "L'indirizzo email specificato non è valido.")
                    return redirect(f"{reverse('supervisor_dashboard')}?tab=users")
                if User.objects.filter(email__iexact=email).exclude(pk=user_obj.pk).exists():
                    messages.error(request, f"L'email '{email}' è già associata a un altro account.")
                    return redirect(f"{reverse('supervisor_dashboard')}?tab=users")

            # Protezione auto-disattivazione o auto-revoca superadmin
            if user_obj == request.user:
                if not is_active:
                    messages.error(request, "Non puoi disattivare il tuo stesso account superadmin.")
                    is_active = True
                if role != "superadmin":
                    messages.error(request, "Non puoi revocare il ruolo Superadmin dal tuo stesso account.")
                    role = "superadmin"

            user_obj.username = username
            user_obj.email = email
            user_obj.first_name = first_name
            user_obj.last_name = last_name
            user_obj.is_active = is_active

            if role == "superadmin":
                user_obj.is_superuser = True
                user_obj.is_staff = True
            elif role == "staff":
                user_obj.is_superuser = False
                user_obj.is_staff = True
            else:
                user_obj.is_superuser = False
                user_obj.is_staff = False

            password_changed = False
            if new_password:
                if len(new_password) < 4:
                    messages.error(request, "La password deve contenere almeno 4 caratteri.")
                    return redirect(f"{reverse('supervisor_dashboard')}?tab=users")
                user_obj.set_password(new_password)
                password_changed = True

            user_obj.save()

            if password_changed and user_obj == request.user:
                update_session_auth_hash(request, user_obj)

            # Assegnazione o rimozione presidenza lega (opzionale da supervisor)
            assign_owner_lid = request.POST.get("assign_owner_league_id")
            if assign_owner_lid and assign_owner_lid.isdigit():
                target_lg = League.objects.filter(pk=int(assign_owner_lid)).first()
                if target_lg:
                    target_lg.owner = user_obj
                    target_lg.save(update_fields=["owner"])

            pwd_note = " (con nuova password)" if password_changed else ""
            messages.success(request, f"Profilo e credenziali di '{user_obj.username}' aggiornati con successo{pwd_note}.")
            logger.info("Supervisor ha modificato l'utente %s (id=%s)", user_obj.username, user_obj.id)
            return redirect(f"{reverse('supervisor_dashboard')}?tab=users")

        elif action == "delete_user":
            uid = request.POST.get("user_id")
            user_to_del = get_object_or_404(User, pk=uid)
            if user_to_del == request.user:
                messages.error(request, "Non puoi eliminare il tuo stesso account Superadmin.")
            elif user_to_del.is_superuser and User.objects.filter(is_superuser=True).count() <= 1:
                messages.error(request, "Impossibile eliminare l'unico Superadmin della piattaforma.")
            else:
                uname = user_to_del.username
                League.objects.filter(owner=user_to_del).update(owner=None)
                Participant.objects.filter(user=user_to_del).update(user=None)
                user_to_del.delete()
                messages.success(request, f"Account '{uname}' eliminato definitivamente.")
                logger.info("Supervisor ha eliminato l'utente %s (id=%s)", uname, uid)
            return redirect(f"{reverse('supervisor_dashboard')}?tab=users")

        elif action == "impersonate_user":
            uid = request.POST.get("user_id")
            target_user = get_object_or_404(User, pk=uid)
            if target_user == request.user:
                messages.info(request, "Sei già collegato con questo account.")
                return redirect(f"{reverse('supervisor_dashboard')}?tab=users")

            admin_id = request.user.id
            admin_name = request.user.username
            login(request, target_user)
            request.session["supervisor_impersonator_id"] = admin_id
            request.session["supervisor_impersonator_name"] = admin_name
            messages.success(request, f"Stai ora visualizzando la piattaforma come '{target_user.username}'.")

            # Route to target user's context
            first_team = target_user.teams.filter(is_active=True).first()
            if first_team:
                request.session["participant_id"] = first_team.id
                request.session["display_name"] = first_team.display_name
                request.session["app_league_id"] = first_team.league_id
                return redirect("app_home")

            owned_league = target_user.leagues.first() or target_user.managed_leagues.first()
            if owned_league:
                return redirect(f"/dashboard/{owned_league.id}/")

            return redirect("home")

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

        elif action == "start_live_sync":
            interval = int(request.POST.get("interval_seconds") or 60)
            provider = request.POST.get("provider") or "fantacalcio_web"
            target_g = int(request.POST.get("target_giornata") or 0) or None
            mgr = LiveSyncManager.get_instance()
            mgr.active_giornata_num = target_g
            mgr.start_background(interval=interval, provider=provider)
            messages.success(request, f"Sincronizzazione Live in background avviata (ogni {interval}s con {provider}).")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=live_sync")

        elif action == "stop_live_sync":
            LiveSyncManager.get_instance().stop_background()
            messages.info(request, "Sincronizzazione Live in background arrestata.")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=live_sync")

        elif action == "trigger_live_sync":
            target_g = int(request.POST.get("target_giornata") or 0) or None
            provider = request.POST.get("provider") or "fantacalcio_web"
            mgr = LiveSyncManager.get_instance()
            mgr.provider = provider
            res = mgr.sync_now(giornata_num=target_g, is_provisional=True)
            if res.get("status") == "SUCCESS":
                messages.success(request, f"Sync Live completato: {res.get('total_updated')} calciatori aggiornati per G{res.get('giornata')} ({provider}).")
            else:
                messages.warning(request, f"Sync Live: {res.get('status')} - nessun dato disponibile per G{target_g}.")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=live_sync")

        elif action == "consolidate_live_sync":
            target_g = int(request.POST.get("target_giornata") or 0) or 1
            res = LiveSyncManager.get_instance().consolidate_official(target_g)
            messages.success(request, f"Giornata {target_g} consolidata ufficialmente ({res.get('giornate_count')} leghe chiuse su voti definitivi).")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=live_sync")

        elif action == "save_mail":
            cfg = MailSettings.get()
            provider = request.POST.get("provider") or MailSettings.Provider.SMTP
            if provider not in MailSettings.Provider.values:
                provider = MailSettings.Provider.SMTP
            security = request.POST.get("security") or MailSettings.Security.STARTTLS
            if security not in MailSettings.Security.values:
                security = MailSettings.Security.STARTTLS
            try:
                port = int(request.POST.get("port") or 0)
            except ValueError:
                port = 0
            try:
                timeout = min(120, max(3, int(request.POST.get("timeout") or 15)))
            except ValueError:
                timeout = 15

            from_email_raw = (request.POST.get("from_email") or "").strip()
            reply_to_raw = (request.POST.get("reply_to") or "").strip()
            errors = []
            if from_email_raw:
                try:
                    validate_email(from_email_raw)
                except ValidationError:
                    errors.append(f"«{from_email_raw}» non è un indirizzo email valido.")
            if reply_to_raw:
                try:
                    validate_email(reply_to_raw)
                except ValidationError:
                    errors.append(f"«{reply_to_raw}» non è un indirizzo email valido per Reply-To.")

            enabled = request.POST.get("enabled") in ("1", "on", "true")
            cfg.enabled = enabled
            cfg.provider = provider
            cfg.host = (request.POST.get("host") or "").strip()[:200]
            cfg.port = port if 0 < port < 65536 else (465 if security == "ssl" else 587)
            cfg.security = security
            cfg.username = (request.POST.get("username") or "").strip()[:200]
            if request.POST.get("clear_password") == "1":
                cfg.password = ""
            elif request.POST.get("password"):
                cfg.password = request.POST.get("password")[:300]
            cfg.from_email = from_email_raw
            cfg.from_name = (request.POST.get("from_name") or "").strip()[:80]
            cfg.reply_to = reply_to_raw
            cfg.timeout = timeout

            if cfg.enabled and provider != MailSettings.Provider.CONSOLE:
                if not cfg.host:
                    errors.append("Indica il server SMTP (host).")
                if not cfg.from_email:
                    errors.append("Indica l'indirizzo email del mittente.")

            if errors:
                for e in errors:
                    messages.error(request, e)
            else:
                cfg.updated_by = request.user
                cfg.save()
                ready, _ = mail.status(cfg)
                if cfg.enabled and ready:
                    messages.success(request, f"Configurazione SMTP ({cfg.get_provider_display()}) salvata e attiva! Consigliato: invia un'email di prova.")
                elif cfg.enabled:
                    messages.warning(request, "Impostazioni salvate, ma mancano host o mittente: l'invio non è operativo.")
                else:
                    messages.info(request, "Impostazioni salvate: l'invio email da piattaforma è attualmente disattivato.")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=mail")

        elif action == "test_mail":
            cfg = MailSettings.get()
            test_to = (request.POST.get("test_to") or "").strip()
            if not test_to:
                messages.error(request, "Indica a chi recapitare l'email di prova.")
            else:
                try:
                    validate_email(test_to)
                    ok, error = mail.send_test(test_to, cfg)
                    if ok:
                        messages.success(request, f"Email di prova inviata con successo a {test_to}! Controlla la casella (incluso spam).")
                    else:
                        messages.error(request, f"Invio email di prova non riuscito: {error}")
                except ValidationError:
                    messages.error(request, f"«{test_to}» non è un indirizzo email valido.")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=mail")

        elif action == "manual_backup":
            try:
                dst = backup.backup_database(reason="Richiesta manuale da Supervisor")
                if dst:
                    messages.success(request, f"Backup del database completato con successo: {dst.name}")
                else:
                    messages.error(request, "Impossibile creare il backup: controlla i permessi su disco o il servizio di backup.")
            except Exception as e:
                logger.exception("Errore durante il backup manuale: %s", e)
                messages.error(request, f"Errore durante il backup: {e}")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=reports")

        elif action == "clear_cache":
            try:
                cache.clear()
                messages.success(request, "Cache di piattaforma svuotata con successo.")
            except Exception as e:
                messages.error(request, f"Errore durante lo svuotamento della cache: {e}")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=reports")

    # Metrics & System Health
    metrics = _get_server_metrics()

    # User Management Data with eager loading to prevent N+1 queries
    users = list(
        User.objects.prefetch_related(
            "leagues",
            "managed_leagues",
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
    recent_logs, log_filename = _read_recent_logs(max_lines=200, level_filter=level_filter, query_filter=query_filter)

    # Reporting & Global KPIs
    total_users = User.objects.count()
    total_leagues = len(leagues)
    total_teams = Participant.objects.count()
    raw_players_count = Player.objects.count()
    unique_players_count = (
        Player.objects.values("name", "team").distinct().count()
        if raw_players_count > 0 else 0
    )
    total_auctions = Auction.objects.count()
    live_auctions_count = Auction.objects.filter(status=Auction.Status.LIVE).count()
    total_bids = Bid.objects.count()
    total_credits_spent = Participant.objects.aggregate(total=Sum("spent_credits"))["total"] or 0

    # Outgoing mail configuration & status
    mail_cfg = MailSettings.get()
    mail_ready, mail_source = mail.status(mail_cfg)

    # Backup & System settings for maintenance tab
    recent_backups, backups_folder = _get_recent_backups()
    system_settings = _get_system_settings_info()

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
            "log_filename": log_filename,
            "level_filter": level_filter,
            "query_filter": query_filter,
            "kpis": {
                "total_users": total_users,
                "total_leagues": total_leagues,
                "total_teams": total_teams,
                "total_players": unique_players_count or raw_players_count,
                "raw_players_count": raw_players_count,
                "total_auctions": total_auctions,
                "live_auctions_count": live_auctions_count,
                "total_bids": total_bids,
                "total_credits_spent": total_credits_spent,
            },
            "live_sync": LiveSyncManager.get_instance().get_status(),
            "mail_cfg": mail_cfg,
            "mail_ready": mail_ready,
            "mail_source": mail_source,
            "mail_presets": mail.PRESETS,
            "mail_providers": MailSettings.Provider.choices,
            "mail_securities": MailSettings.Security.choices,
            "recent_backups": recent_backups,
            "backups_folder": backups_folder,
            "system_settings": system_settings,
        },
    )


def supervisor_impersonate_exit(request):
    """Exit impersonation and restore original superadmin account."""
    orig_id = request.session.pop("supervisor_impersonator_id", None)
    request.session.pop("supervisor_impersonator_name", None)
    if orig_id:
        orig_user = User.objects.filter(pk=orig_id, is_superuser=True).first()
        if orig_user:
            login(request, orig_user)
            messages.success(request, f"Sessione ripristinata: sei tornato come Superadmin ({orig_user.username}).")
            return redirect(f"{reverse('supervisor_dashboard')}?tab=users")
    return redirect("supervisor_dashboard")


@supervisor_required
def supervisor_backup_download(request):
    """Download a database backup snapshot safely directly from the supervisor console."""
    filename = request.GET.get("file", "").strip()
    if backup._is_postgres():
        folder = backup._backup_dir()
    else:
        src = backup._db_path()
        if src is None:
            raise Http404("Nessun database locale rilevato.")
        folder = src.parent / "backups"

    if filename:
        safe_path = (folder / filename).resolve()
        # Security check: must remain inside backup folder
        if not str(safe_path).startswith(str(folder.resolve())):
            return HttpResponseForbidden("Accesso al file non consentito.")
        if not safe_path.exists() or not safe_path.is_file():
            raise Http404("File di backup non trovato.")
        target_file = safe_path
    else:
        # Generate fresh snapshot or download latest
        target_file = backup.backup_database(reason="Download da Supervisor Cockpit")
        if not target_file or not Path(target_file).exists():
            latest = backup.latest_backup()
            if latest and (folder / latest["name"]).exists():
                target_file = folder / latest["name"]
            else:
                raise Http404("Nessun backup disponibile.")

    target_path = Path(target_file)
    return FileResponse(open(target_path, "rb"), as_attachment=True, filename=target_path.name)

