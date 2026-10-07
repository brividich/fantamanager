# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for FantaManager (Windows .exe + macOS .app).

Build from the project root:
    pyinstaller --clean --noconfirm packaging/fantamanager.spec

Produces a one-folder bundle in dist/FantaManager/ (Windows) or a
dist/FantaManager.app bundle (macOS). The platform build scripts then wrap
that into a Setup.exe (Inno Setup) or a .dmg.
"""
import os
import sys

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = os.path.dirname(SPECPATH)  # packaging/ lives one level under the project root

# --- Data files: templates + static + Django/app package data ---------------
datas = []
datas += collect_data_files("auctions")          # auctions/templates/**
# ...e gli stessi template di nuovo, per nome: collect_data_files puo' rinunciare
# in silenzio e lasciare un'app che muore su TemplateDoesNotExist. PyInstaller
# deduplica, quindi elencarli due volte non costa nulla.
_templates = os.path.join(ROOT, "auctions", "templates")
if os.path.isdir(_templates):
    datas += [(_templates, os.path.join("auctions", "templates"))]
# auctions/data/ (vuota nel repository: le statistiche di terzi non si
# ridistribuiscono). Se una build privata ci mette dei file, li imbarca.
_data = os.path.join(ROOT, "auctions", "data")
if os.path.isdir(_data):
    datas += [(_data, os.path.join("auctions", "data"))]
# L'icona serve due volte: come icona dell'exe (sotto) e come immagine da
# mostrare vicino all'orologio, che va letta a runtime e quindi imbarcata.
_ico = os.path.join(ROOT, "packaging", "icon.ico")
if os.path.exists(_ico):
    datas += [(_ico, "packaging")]
datas += collect_data_files("django")            # admin templates, locale, etc.
datas += collect_data_files("autobahn")          # nvx/_utf8validator.c (websocket)
_static = os.path.join(ROOT, "static")
if os.path.isdir(_static):
    datas += [(_static, "static")]

# --- Hidden imports: apps referenced by string, ORM migrations, ASGI stack --
hiddenimports = []
for pkg in ("auctions", "liveauction", "channels", "daphne", "twisted", "django"):
    hiddenimports += collect_submodules(pkg)
hiddenimports += collect_submodules("pystray")   # icona vicino all'orologio
hiddenimports += [
    "liveauction.settings",
    "liveauction.asgi",
    "liveauction.wsgi",
    "auctions.apps",
    "auctions.consumers",
    "auctions.routing",
    "psycopg",          # harmless if absent; lets POSTGRES_DB work if ever set
]

a = Analysis(
    [os.path.join(ROOT, "run_app.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "PyInstaller"],
    noarchive=False,
)

pyz = PYZ(a.pure)

_icon_ico = os.path.join(ROOT, "packaging", "icon.ico")
_icon_icns = os.path.join(ROOT, "packaging", "icon.icns")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FantaManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Niente finestra nera all'avvio: e' brutta da mostrare a un cliente e sa di
    # cosa andata storta. Quello che serviva vederci sta nell'app, e per fermare
    # l'asta c'e' l'icona vicino all'orologio (vedi run_app._start_tray).
    console=False,
    icon=_icon_ico if os.path.exists(_icon_ico) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="FantaManager",
)

# macOS application bundle (ignored on Windows/Linux).
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="FantaManager.app",
        icon=_icon_icns if os.path.exists(_icon_icns) else None,
        bundle_identifier="com.fantamanager.liveauction",
        info_plist={
            "CFBundleName": "FantaManager",
            "CFBundleDisplayName": "FantaManager Live Auction",
            "LSBackgroundOnly": False,
            "NSHighResolutionCapable": True,
        },
    )
