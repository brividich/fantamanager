"""Supervisor → Voto algoritmico: i parametri dell'algoritmo per tutta la
piattaforma, con l'anteprima di cosa cambia prima di salvare.

Solo superuser. La pagina manda la bozza completa all'anteprima a ogni modifica
e mostra lo scostamento rispetto ai valori salvati sullo stesso campione.
"""
import json

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import AlgoSample, AlgoSettingsVersion
from ..providers.apifootball import ApiFootballError
from ..providers.apifootball import is_configured as apifootball_configured
from ..services import voto_algo
from ..voto_algoritmico import ALGO_DEFAULTS, ROLES, STAT_KEYS, effective_algo_rules
from .supervisor import supervisor_required

MAX_BODY = 200_000


def _json_body(request):
    if len(request.body or b"") > MAX_BODY:
        return None
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _plain(rules):
    """Regole complete, senza chiavi interne, pronte per la pagina."""
    return {k: v for k, v in rules.items() if not k.startswith("_")}


def _page_url(sample_key=None):
    url = reverse("supervisor_algo")
    return f"{url}?campione={sample_key}" if sample_key else url


@supervisor_required
def supervisor_algo(request):
    saved = _plain(voto_algo.platform_rules())
    defaults = _plain(effective_algo_rules({}))
    sample_key = request.GET.get("campione") or voto_algo.SIM_KEY
    samples = voto_algo.samples_list()
    if sample_key not in {s["key"] for s in samples}:
        sample_key = voto_algo.SIM_KEY

    groups = []
    for title, items in voto_algo.FIELD_GROUPS:
        fields = []
        for key, label, hint, step, lo, hi in items:
            fields.append({
                "key": key, "label": label, "hint": hint,
                "is_bool": step is bool,
                "step": None if step is bool else step, "min": lo, "max": hi,
                "value": saved[key], "default": defaults[key],
                "changed": not voto_algo._same(saved[key], defaults[key]),
            })
        groups.append({"title": title, "fields": fields})

    stat_cols = [{"key": k, "label": voto_algo.STAT_LABELS[k]} for k in STAT_KEYS]
    tables = []
    for table, title, hint in (
        ("perf_weights", "Pesi del rendimento",
         "quanto vale ogni unità sopra (o sotto) la media del ruolo, ogni 90'"),
        ("perf_baseline", "Media del ruolo",
         "produzione per 90' di un giocatore «da 6»: il rendimento conta lo scarto da qui"),
    ):
        rows = []
        for role in ROLES:
            cells = []
            for stat in STAT_KEYS:
                used = stat in ALGO_DEFAULTS["perf_weights"].get(role, {}) or stat in ALGO_DEFAULTS["perf_malus"]
                cells.append({
                    "stat": stat, "used": used,
                    "value": saved[table].get(role, {}).get(stat, 0),
                    "default": defaults[table].get(role, {}).get(stat, 0),
                })
            rows.append({"role": role, "label": voto_algo.ROLE_LABELS[role], "cells": cells})
        tables.append({"key": table, "title": title, "hint": hint, "rows": rows})
    malus = [{"stat": s, "label": voto_algo.STAT_LABELS[s], "value": saved["perf_malus"].get(s, 0),
              "default": defaults["perf_malus"].get(s, 0)} for s in ALGO_DEFAULTS["perf_malus"]]

    versions = list(AlgoSettingsVersion.objects.select_related("created_by").order_by("-created_at", "-id")[:15])
    for v in versions:
        v.n_changes = len(voto_algo._flatten(v.rules or {}))

    return render(request, "auctions/supervisor_algo.html", {
        "groups": groups,
        "tables": tables,
        "stat_cols": stat_cols,
        "malus": malus,
        "samples": samples,
        "sample_key": sample_key,
        "versions": versions,
        "active_version": versions[0] if versions else None,
        "saved_overrides": len(voto_algo._flatten(voto_algo.diff_from_defaults(saved))),
        "algo_saved": saved,
        "algo_defaults": defaults,
        "api_ready": apifootball_configured(),
        "console_active": "algo",
    })


@supervisor_required
@require_POST
def supervisor_algo_preview(request):
    data = _json_body(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "Richiesta non valida."}, status=400)
    draft, errors = voto_algo.clean_rules(data.get("rules"))
    if errors:
        return JsonResponse({"ok": False, "errors": errors}, status=400)
    name, rows = voto_algo.sample_rows(data.get("sample"))
    result = voto_algo.preview(rows, voto_algo.platform_rules(), draft)
    result.update({"ok": True, "sample_name": name})
    return JsonResponse(result)


@supervisor_required
@require_POST
def supervisor_algo_calibrate(request):
    data = _json_body(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "Richiesta non valida."}, status=400)
    draft, errors = voto_algo.clean_rules(data.get("rules"))
    if errors:
        return JsonResponse({"ok": False, "errors": errors}, status=400)
    try:
        target_mean = float(data.get("mean", 6.0))
        target_sd = float(data.get("sd", 0.6))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Media e deviazione devono essere numeri."}, status=400)
    if not (5.0 <= target_mean <= 7.0 and 0.2 <= target_sd <= 1.5):
        return JsonResponse({"ok": False, "error": "Media fra 5 e 7, deviazione fra 0,2 e 1,5."}, status=400)
    _name, rows = voto_algo.sample_rows(data.get("sample"))
    try:
        cal = voto_algo.calibrate_on(rows, draft, target_mean, target_sd)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    for key, (lo, hi) in (("calib_scale", (0.2, 3)), ("calib_shift", (-2, 2))):
        cal[key] = max(lo, min(hi, cal[key]))
    return JsonResponse({"ok": True, **cal})


@supervisor_required
@require_POST
def supervisor_algo_save(request):
    sample_key = request.POST.get("campione")
    try:
        raw = json.loads(request.POST.get("rules") or "{}")
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        messages.error(request, "Parametri non leggibili: nessuna modifica salvata.")
        return redirect(_page_url(sample_key))
    full, errors = voto_algo.clean_rules(raw)
    if errors:
        messages.error(request, "Non salvato: " + "; ".join(errors.values()))
        return redirect(_page_url(sample_key))
    overrides = voto_algo.diff_from_defaults(full)
    current = voto_algo.platform_overrides()
    if overrides == current:
        messages.info(request, "Nessuna differenza rispetto ai valori in uso: niente da salvare.")
        return redirect(_page_url(sample_key))
    note = (request.POST.get("note") or "").strip()[:200]
    AlgoSettingsVersion.objects.create(rules=overrides, note=note, created_by=request.user)
    messages.success(request, "Parametri salvati: da ora i voti algoritmici si calcolano con questi.")
    return redirect(_page_url(sample_key))


@supervisor_required
@require_POST
def supervisor_algo_restore(request, version_id):
    sample_key = request.POST.get("campione")
    old = AlgoSettingsVersion.objects.filter(pk=version_id).first()
    if old is None:
        messages.error(request, "Versione non trovata.")
    else:
        AlgoSettingsVersion.objects.create(rules=old.rules, created_by=request.user,
                                           note=f"Ripristino della versione {old.pk}")
        messages.success(request, f"Ripristinata la versione {old.pk}.")
    return redirect(_page_url(sample_key))


@supervisor_required
@require_POST
def supervisor_algo_defaults(request):
    sample_key = request.POST.get("campione")
    if not voto_algo.platform_overrides():
        messages.info(request, "I valori in uso sono già quelli di partenza.")
    else:
        AlgoSettingsVersion.objects.create(rules={}, created_by=request.user, note="Ritorno ai valori di partenza")
        messages.success(request, "Tornati ai valori di partenza (la versione precedente resta nello storico).")
    return redirect(_page_url(sample_key))


@supervisor_required
@require_POST
def supervisor_algo_sample_import(request):
    try:
        round_number = int(request.POST.get("giornata") or 0)
    except ValueError:
        round_number = 0
    if not 1 <= round_number <= 38:
        messages.error(request, "Giornata fra 1 e 38.")
        return redirect(_page_url())
    if not apifootball_configured():
        messages.error(request, "API-Football non configurata: imposta APIFOOTBALL_KEY sul server e riavvia.")
        return redirect(_page_url())
    try:
        sample = voto_algo.import_apifootball_round(round_number, request.user)
    except (ApiFootballError, ValueError) as exc:
        messages.error(request, f"Campione non importato: {exc}")
        return redirect(_page_url())
    messages.success(request, f"Campione «{sample.name}» importato: {len(sample.rows)} prestazioni.")
    return redirect(_page_url(str(sample.pk)))


@supervisor_required
@require_POST
def supervisor_algo_sample_delete(request, sample_id):
    deleted, _ = AlgoSample.objects.filter(pk=sample_id).delete()
    if deleted:
        messages.success(request, "Campione eliminato.")
    return redirect(_page_url())
