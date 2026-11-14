from datetime import date, datetime
from decimal import Decimal as D

from spe.providers.esselunga import parse_receipt, to_purchase

# Synthetic receipt in the layout pypdf extracts from Esselunga's PDFs (structure copied, content invented).
RECEIPT = """\
           114210112613510390601000
            * ESSELUNGA S.p.A. *
                             IVA    EURO
PASTA DI SEMOLA 500G          *c    1,29
BANANE                        *a    1,74
  1,160 kg x 1,50 EUR/kg
YOGURT BIANCO X4              *c    2,49
  SCONTO FIDATY 20%                 0,50-S
DETERSIVO PIATTI              *d    2,98
P)MB PANE INTEGRALE           *a    1,40
  PUNTI FRAGOLA 8
SHOPPER                       *d    0,10

*a = IVA 04%
*c = IVA 10%
*d = IVA 22%


TOTALE EURO                         9,50 *
di cui IVA                          1,02

PAGAMENTO CARTA DI CREDITO          1,50
PAGAMENTO BUONO PASTO ELETTRON      8,00

              03-11-2026 18:42
     PAGAMENTO CARTA DI CREDITO
EURO      1,50
"""


def test_receipt_lines_reconcile_to_the_total_and_payments():
    r = parse_receipt(RECEIPT)
    assert r["total"] == D("9.50") and r["at"].date() == date(2026, 11, 3)
    assert sum(a for _, a, _ in r["lines"]) == r["total"]  # discounts are negative lines
    assert ("SCONTO FIDATY 20%", D("-0.50"), True) in r["lines"]
    p, issue = to_purchase("123", "Centro", r)
    assert issue is None and p.problems() == []
    eligible = {ln.description: ln.voucher_eligible for ln in p.lines}
    assert eligible["DETERSIVO PIATTI"] is False and eligible["PASTA DI SEMOLA 500G"] is True  # 22% VAT vs 10%
    assert [(x.method, x.amount) for x in p.payments] == [("card", D("1.50")), ("edenred", D("8.00"))]


def test_unknown_payment_wording_is_held():
    _, issue = to_purchase("1", "", parse_receipt(RECEIPT.replace("CARTA DI CREDITO          1,50", "SATISPAY                  1,50")))
    assert issue == "unknown payment 'SATISPAY' on the receipt"


def test_receipt_pdfs_are_archived_once_including_a_backfill(db, tmp_path, monkeypatch):
    from conftest import conn

    from spe import vault
    from spe.db import Evidence
    from spe.providers import Context, esselunga
    from spe.settings import Settings

    class FakeClient:
        downloads: list[int] = []

        def __init__(self, cookies):
            pass

        def open(self):
            return "card"

        def receipts(self, card, since):
            listed = [{"id": 1, "dataOperazione": "03/01/2024", "importo": "9.90", "isPDF": True, "isPdfAvailable": True},
                      {"id": 2, "dataOperazione": "02/11/2026", "importo": "4.50", "isPDF": True, "isPdfAvailable": True},
                      {"id": 3, "dataOperazione": "01/02/2026", "importo": "1.00", "isPDF": True, "isPdfAvailable": True}]
            return [m for m in listed if datetime.strptime(m["dataOperazione"], "%d/%m/%Y").date() >= since]

        def receipt_pdf(self, card, receipt_id):
            self.downloads.append(receipt_id)
            return None if receipt_id == 3 else b"%PDF-1.7 receipt " + str(receipt_id).encode()  # 3: the site serves 0 bytes

        def cookies(self):
            return []

    monkeypatch.setattr(esselunga, "EsselungaClient", FakeClient)
    monkeypatch.setattr(esselunga.time, "sleep", lambda s: None)
    settings = Settings("http://sure.test", "s", receipts_dir=str(tmp_path))
    c = conn(db, "esselunga")
    c.session, c.cursor = vault.seal("s", {"cookies": []}), {"last_success": "2026-11-10"}
    db.add(Evidence(user_id=1, connection_id=c.id, provider="esselunga", key="2", kind="purchase", payload={}))  # parsed before archiving existed
    db.commit()

    assert esselunga.Esselunga().sync(Context(db, None, settings, c)).endswith("2 PDFs archived")
    assert (tmp_path / f"user-1/esselunga-{c.id}/2024/2024-01-03_1.pdf").read_bytes() == b"%PDF-1.7 receipt 1"
    assert (tmp_path / f"user-1/esselunga-{c.id}/2026/2026-11-02_2.pdf").exists()
    assert db.query(Evidence).count() == 1  # the 2024 receipt is only archived, not matched against Sure

    FakeClient.downloads.clear()
    assert esselunga.Esselunga().sync(Context(db, None, settings, c)).endswith("0 PDFs archived")
    assert FakeClient.downloads == []  # the backfill is done; the empty one is outside the sync window
