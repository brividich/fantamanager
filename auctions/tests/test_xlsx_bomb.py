"""Un .xlsx «bomba»: pochi KB che dichiarano gigabyte di XML.

Un .xlsx è uno zip: openpyxl lo scompatta tutto (in modalità completa anche
in memoria) e il processo unico del server si ferma. Prima di aprirlo,
``uploads.check_xlsx_bytes`` legge le dimensioni dalla directory dello zip,
senza scompattare, e rifiuta con un messaggio che dice cosa fare.
"""
import io
import struct
import zipfile
import zlib
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .. import uploads
from ..models import League
from ..providers import importers
from ..providers.team_sheet_import import SheetError, parse_team_sheet_xlsx
from ..services import voti


def _declared_bomb(declared=4 * 1024 ** 3):
    """A tiny zip whose directory declares ``declared`` bytes for its sheet."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/worksheets/sheet1.xml", "<x/>")
    data = bytearray(buf.getvalue())
    # Riscrive la dimensione non compressa (local header e central directory).
    name = b"xl/worksheets/sheet1.xml"
    small = declared & 0xFFFFFFFF if declared < 2 ** 32 else 0xFFFFFFFE
    for sig, size_off in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        start = 0
        while (i := data.find(sig, start)) >= 0:
            name_off = 30 if sig == b"PK\x03\x04" else 46
            n = struct.unpack_from("<H", data, i + (26 if sig == b"PK\x03\x04" else 28))[0]
            if bytes(data[i + name_off:i + name_off + n]) == name:
                struct.pack_into("<I", data, i + size_off, small)
            start = i + 4
    return bytes(data)


def _ratio_bomb():
    """A real zip: 50 MB of zeros squeezed into a few KB (ratio ~1000)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", b"\0" * (50 * 1024 * 1024))
    return buf.getvalue()


class CheckXlsxTests(SimpleTestCase):
    def test_a_declared_giant_is_refused(self):
        data = _declared_bomb()
        self.assertLess(len(data), 2048)
        with self.assertRaisesMessage(uploads.UploadRejected, "troppo grande"):
            uploads.check_xlsx_bytes(data)

    def test_a_squeezed_giant_is_refused(self):
        with self.assertRaises(uploads.UploadRejected):
            uploads.check_xlsx_bytes(_ratio_bomb())

    def test_a_normal_workbook_passes(self):
        import openpyxl
        wb = openpyxl.Workbook()
        for i in range(500):
            wb.active.append([f"Giocatore {i}", "C", "Inter", i])
        buf = io.BytesIO()
        wb.save(buf)
        self.assertEqual(uploads.check_xlsx_bytes(buf.getvalue()), buf.getvalue())

    def test_not_a_zip_is_left_to_the_parser(self):
        self.assertEqual(uploads.check_xlsx_bytes(b"Nome;Ruolo\n"), b"Nome;Ruolo\n")

    def test_every_xlsx_reader_checks_first(self):
        bomb = _declared_bomb()
        with self.assertRaises(uploads.UploadRejected):
            importers.parse_listone_file(io.BytesIO(bomb), "listone.xlsx")
        with self.assertRaises(uploads.UploadRejected):
            list(importers._tabular_rows(io.BytesIO(bomb), "rose.xlsx"))
        with self.assertRaises(uploads.UploadRejected):
            voti.parse_voti_file(bomb, "voti.xlsx")
        with self.assertRaisesMessage(SheetError, "troppo grande"):
            parse_team_sheet_xlsx(bomb, "scheda.xlsx")


class XlsxUploadViewTests(TestCase):
    def test_the_listone_upload_says_what_to_do(self):
        owner = User.objects.create_user("presidente", password="pw-presidente")
        league = League.objects.create(name="Lega", owner=owner, budget=Decimal("500"))
        self.client.force_login(owner)
        resp = self.client.post(reverse("admin_import_players"), {
            "league_id": league.id,
            "csv_file": SimpleUploadedFile("listone.xlsx", _declared_bomb()),
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("CSV", resp.json()["error"])
