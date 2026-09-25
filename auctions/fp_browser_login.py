"""Standalone Playwright login helper for Fantapazz.

Run as a separate process (NOT a thread) because Playwright's subprocess
launch is not supported inside secondary threads on Windows.

Usage:
    python fp_browser_login.py <token> <server_origin> [league_id] [media_root]

Opens a real Chromium window, waits for the user to log in (Facebook/Google/
email all work), grabs every cookie (including httpOnly SESS). If a league_id
is given, it navigates to the 'rose-lega' page and downloads the full roster
.xls export, saving it to <media_root>/fp_rose_<token>.xls. The cookie is POSTed
back to the FantaManager cookie-sync endpoint keyed by <token>.
"""
import json
import os
import re
import sys
import time

FP_BASE = "https://www.fantapazz.com"


def _score_export_href(href):
    """Rank an export link: higher = more likely the WHOLE-league roster export.

    Fantapazz pages can carry several export links — one for the full league
    ("rose-lega"/"esporta") and, on some layouts, a per-team "la mia rosa" one.
    Grabbing the first match downloaded a single-team file, so we prefer the
    league-wide export and push anything that smells per-team to the bottom.
    """
    h = href.lower()
    score = 0
    if "rose-lega" in h or "rose_lega" in h or "roselega" in h:
        score += 6
    if "esporta" in h or "export" in h or "excel" in h or "xls" in h:
        score += 3
    if "lega" in h:
        score += 1
    # Per-team / own-roster exports → least preferred.
    if "rosa-squadra" in h or "mia-rosa" in h or "miarosa" in h or "squadra" in h:
        score -= 5
    return score


def _download_rose(page, ctx, league_id, dest_path):
    """Navigate to the rose-lega page and download the full roster .xls.

    Writes a sidecar ``<dest>.log`` listing every export candidate found and
    what was attempted, so a "only my team" import can be diagnosed afterwards.
    """
    log_path = dest_path + ".log"
    log_lines = [f"rose-lega page: {FP_BASE}/fantacalcio/rose-lega/{league_id}/0"]

    def _flush_log():
        try:
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(log_lines))
        except Exception:
            pass

    page.goto(f"{FP_BASE}/fantacalcio/rose-lega/{league_id}/0",
              wait_until="networkidle", timeout=30_000)
    time.sleep(2)

    # Strategy 1: collect ALL export-looking hrefs, then try them best-first.
    html = page.content()
    hrefs = []
    seen = set()
    for m in re.finditer(
        r'href=["\']([^"\']*(?:xls|export|esporta|excel|scarica)[^"\']*)["\']',
        html, re.IGNORECASE,
    ):
        href = m.group(1)
        if href in seen:
            continue
        seen.add(href)
        if href.startswith("/"):
            full = FP_BASE + href
        elif href.startswith("http"):
            full = href
        else:
            full = f"{FP_BASE}/{href}"
        hrefs.append(full)

    hrefs.sort(key=_score_export_href, reverse=True)
    log_lines.append(f"candidate export links ({len(hrefs)}), best-first:")
    log_lines += [f"  [{_score_export_href(h)}] {h}" for h in hrefs]
    _flush_log()

    for full in hrefs:
        try:
            resp = ctx.request.get(full, timeout=30_000)
            if resp.ok:
                body = resp.body()
                if body and len(body) > 2000:
                    with open(dest_path, "wb") as fh:
                        fh.write(body)
                    log_lines.append(f"DOWNLOADED via href: {full} ({len(body)} bytes)")
                    _flush_log()
                    return True
                else:
                    log_lines.append(f"skip (too small {len(body) if body else 0}b): {full}")
            else:
                log_lines.append(f"skip (HTTP {resp.status}): {full}")
        except Exception as e:
            log_lines.append(f"error: {full} → {e}")
    _flush_log()

    # Strategy 2: click a download control and capture the download event.
    selectors = [
        'a:has-text("Esporta")', 'a:has-text("Scarica")', 'button:has-text("Esporta")',
        'button:has-text("Scarica")', 'a[href*="esporta"]', 'a[href*="export"]',
        'a[href*="xls"]', '[title*="excel" i]', '[title*="xls" i]', '.export', '.download',
        'i.fa-download', 'i.fa-file-excel',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            with page.expect_download(timeout=15_000) as dl_info:
                el.click()
            dl_info.value.save_as(dest_path)
            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 2000:
                log_lines.append(f"DOWNLOADED via click: {sel} ({os.path.getsize(dest_path)} bytes)")
                _flush_log()
                return True
        except Exception:
            continue
    log_lines.append("FAILED: no export produced a file")
    _flush_log()
    return False


def _scrape_team_ids(page):
    """Extract all rosa-squadra team IDs from the rendered league page DOM."""
    try:
        html = page.content()
    except Exception:
        return []
    ids = []
    seen = set()
    # Links / attributes referencing the rosa-squadra modal.
    for m in re.finditer(r"rosa-squadra[/=](\d+)", html):
        tid = m.group(1)
        if tid not in seen:
            seen.add(tid); ids.append(tid)
    # Stemma image filenames: squadra_<id>_<ts>.png
    for m in re.finditer(r"squadra_(\d+)_", html):
        tid = m.group(1)
        if tid not in seen:
            seen.add(tid); ids.append(tid)
    return ids


def main():
    if len(sys.argv) < 3:
        print("usage: fp_browser_login.py <token> <server_origin> [league_id]")
        return 1

    token         = sys.argv[1]
    server_origin = sys.argv[2].rstrip("/")
    league_id     = sys.argv[3] if len(sys.argv) > 3 else ""
    media_root    = sys.argv[4] if len(sys.argv) > 4 else os.path.dirname(__file__)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright non installato.")
        return 2

    import urllib.request

    cookie_str  = ""
    rose_ok     = False
    dest_path   = os.path.join(media_root, f"fp_rose_{token}.xls")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--start-maximized"],
            downloads_path=media_root,
        )
        ctx  = browser.new_context(viewport=None, accept_downloads=True)
        page = ctx.new_page()
        page.goto(f"{FP_BASE}/user/login")

        # Wait until the user reaches an authenticated state (max 5 minutes).
        deadline = time.time() + 300
        authed   = False
        while time.time() < deadline:
            try:
                names = {c["name"] for c in ctx.cookies(FP_BASE)}
                if any(n.startswith("SESS") for n in names) and "DRUPAL_UID" in names:
                    authed = True
                    break
            except Exception:
                pass
            time.sleep(2)

        if authed:
            cookies    = ctx.cookies(FP_BASE)
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

            # Download the full roster .xls export.
            if league_id:
                try:
                    os.makedirs(media_root, exist_ok=True)
                    rose_ok = _download_rose(page, ctx, league_id, dest_path)
                except Exception as e:
                    print("Download rose fallito:", e)

        browser.close()

    if not cookie_str:
        print("Login non completato entro il tempo limite.")
        return 3

    # POST the cookie back to the server.
    payload = json.dumps({"cookie": cookie_str, "rose_ready": rose_ok}).encode("utf-8")
    req = urllib.request.Request(
        f"{server_origin}/admin-auction/fantapazz/cookie-sync/",
        data=payload,
        headers={"Content-Type": "application/json", "X-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print("Server response:", resp.read().decode("utf-8", "replace"))
    except Exception as e:
        print("Errore invio cookie:", e)
        return 4

    print(f"Login completato. Cookie inviato. Rose scaricate: {rose_ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
