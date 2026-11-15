from datetime import date
from decimal import Decimal

from spe.providers.edenred_it import normalize
from spe.vouchers import Kind


def row(when, code, value="8", n=1, shop="100001", term="000000001", desc="Utilizzo BUONI/VOUCHER presso ESSELUNGA - MILANO - CENTRO"):
    return {"dataOperazione": when, "codOperazione": code, "valBuono": value, "numBuoni": str(n), "descOperazione": desc,
            "elencoDettagli": [{"eserCd": shop, "termCd": term}]}


def test_one_checkout_paid_with_three_vouchers_is_one_movement():
    rows = [row("10/11/2026 13:52:04", "04") for _ in range(3)] + [
        row("10/11/2026 13:52:04", "04", term="000290154"),  # another till, same second: another checkout
        row("02/11/2026 00:17:15", "03", n=20, shop=None, term=None, desc="Ordine di BUONI/VOUCHER su CLOUD"),
        row("11/11/2026 12:22:00", "02", n=2, shop=None, term=None, desc="Ricarica BUONI/VOUCHER sulla TESSERA"),
        row("11/11/2026 12:30:00", "99", desc="something new"),
    ]
    got = sorted(((m.kind, m.amount, m.booked_on) for m in normalize(rows)), key=str)
    assert got == sorted([
        (Kind.REDEMPTION, Decimal(24), date(2026, 11, 10)),
        (Kind.REDEMPTION, Decimal(8), date(2026, 11, 10)),
        (Kind.ISSUANCE, Decimal(160), date(2026, 11, 2)),
        (Kind.TRANSFER, Decimal(16), date(2026, 11, 11)),
        (Kind.OTHER, Decimal(8), date(2026, 11, 11)),  # unknown codes are held, never guessed
    ], key=str)
    assert len({m.source_id for m in normalize(rows)}) == 5
    assert normalize(rows)[0].description == "ESSELUNGA - MILANO - CENTRO" and normalize(rows)[0].merchant == "Esselunga"
