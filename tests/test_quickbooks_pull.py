"""QuickBooks pull: mapping, idempotency, incremental cursor, paging, report."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.models import Expense, Order
from app.services import api_sync, quickbooks
from app.services import settings as settings_service

from .conftest import utc


def _line(account: str, description: str = "") -> list[dict]:
    return [{"Description": description,
             "AccountBasedExpenseLineDetail": {"AccountRef": {"name": account}}}]


def _seed_books(intuit) -> None:
    intuit.add("Purchase", "11", TotalAmt=120.5, CurrencyRef={"value": "USD"},
               EntityRef={"name": "Figma"}, PrivateNote="Licencias de diseño",
               Line=_line("Software"))
    intuit.add("Bill", "21", TotalAmt=900, Balance=900, DocNumber="B-1",
               VendorRef={"name": "AWS"}, Line=_line("Infraestructura", "Cloud agosto"))
    intuit.add("Invoice", "31", TotalAmt=500, Balance=0, CustomerRef={"name": "Tienda Norte"})
    intuit.add("Invoice", "32", TotalAmt=800, Balance=300, CustomerRef={"name": "Café Sur"})
    intuit.add("SalesReceipt", "41", TotalAmt=75, CustomerRef={"name": "Mostrador"})


def _expenses(db) -> dict[str, Expense]:
    db.expire_all()
    return {e.extra["external_key"]: e for e in db.query(Expense).all()}


def _orders(db) -> dict[str, Order]:
    db.expire_all()
    return {o.id: o for o in db.query(Order).all()}


# ---------------------------------------------------------------- mapping

def test_first_pull_maps_each_entity_to_our_records(connected, intuit):
    _seed_books(intuit)
    report = quickbooks.pull(connected)

    expenses = _expenses(connected)
    purchase = expenses["qbo:expenses:purchase:11"]
    assert (purchase.description, purchase.category, purchase.vendor) == (
        "Licencias de diseño", "Software", "Figma")
    assert (purchase.amount, purchase.currency, purchase.date) == (120.5, "USD", "2026-03-10")

    bill = expenses["qbo:expenses:bill:21"]
    assert (bill.description, bill.category, bill.vendor) == ("Cloud agosto", "Infraestructura", "AWS")
    assert bill.extra["source_status"] == "Open"          # unpaid bill, Intuit's own word
    assert bill.extra["doc_number"] == "B-1"
    # No CurrencyRef: the company's home currency, not a guess.
    assert bill.currency == "USD"

    orders = _orders(connected)
    assert set(orders) == {"QB-INV-31", "QB-INV-32", "QB-SR-41"}
    assert orders["QB-INV-31"].status == "approved"      # balance 0 -> revenue
    assert orders["QB-INV-32"].status == "pending"       # balance left -> waits
    assert orders["QB-INV-32"].extra["source_status"] == "Open"
    assert orders["QB-SR-41"].status == "approved"       # a receipt is paid by definition
    assert all(o.gateway == "QuickBooks" for o in orders.values())
    assert orders["QB-INV-31"].extra["_raw"]["CustomerRef"] == {"name": "Tienda Norte"}

    entities = report["entities"]
    assert set(entities) == {"expenses:purchase", "expenses:bill", "orders:invoice",
                             "orders:salesreceipt"}
    assert entities["orders:invoice"]["imported"] == 2
    assert all(e["verification"]["exact"] for e in entities.values())


def test_first_pull_starts_at_the_configured_date(connected, intuit):
    settings_service.update(connected, {"quickbooks.since": "2026-03-01"})
    intuit.add("Purchase", "1", TotalAmt=10, TxnDate="2026-02-28")
    intuit.add("Purchase", "2", TotalAmt=20, TxnDate="2026-03-01")

    quickbooks.pull(connected)

    assert set(_expenses(connected)) == {"qbo:expenses:purchase:2"}
    assert "TxnDate >= '2026-03-01'" in intuit.queries[0]


def test_default_start_is_january_first_of_this_year(connected, intuit):
    quickbooks.pull(connected)
    assert f"TxnDate >= '{datetime.now().year}-01-01'" in intuit.queries[0]


def test_a_description_falls_back_to_the_document_and_vendor(connected, intuit):
    intuit.add("Bill", "7", TotalAmt=50, Balance=0, DocNumber="F-77", VendorRef={"name": "Truora"})
    quickbooks.pull(connected)
    bill = _expenses(connected)["qbo:expenses:bill:7"]
    assert bill.description == "Factura de proveedor F-77 · Truora"
    assert bill.category == "Sin categoría"
    assert bill.extra["source_status"] == "Paid"


def test_records_without_a_usable_amount_are_skipped_and_explained(connected, intuit):
    intuit.add("Purchase", "1", TotalAmt=0)
    intuit.add("Purchase", "2")
    intuit.add("Purchase", "3", TotalAmt=15)

    report = quickbooks.pull(connected)

    result = report["entities"]["expenses:purchase"]
    assert (result["received"], result["imported"], result["skipped"]) == (3, 1, 2)
    assert any("monto usable" in problem for problem in result["problems"])


def test_a_currency_without_a_rate_is_kept_and_declared(connected, intuit):
    intuit.add("Purchase", "1", TotalAmt=100, CurrencyRef={"value": "EUR"})
    report = quickbooks.pull(connected)
    assert _expenses(connected)["qbo:expenses:purchase:1"].currency == "EUR"
    assert any("Sin tasa USD para EUR" in p for p in report["entities"]["expenses:purchase"]["problems"])


def test_long_ids_still_fit_the_order_id_column():
    order_id = quickbooks._order_id("Invoice", "9" * 40)
    assert len(order_id) <= 20 and order_id.startswith("QB-")
    assert order_id == quickbooks._order_id("Invoice", "9" * 40)     # stable across pulls


# ------------------------------------------------------------ what to pull

def test_switches_choose_what_comes_in(connected, intuit):
    _seed_books(intuit)
    settings_service.update(connected, {"quickbooks.sync_sales": False})

    report = quickbooks.pull(connected)

    assert set(report["entities"]) == {"expenses:purchase", "expenses:bill"}
    assert _orders(connected) == {}


def test_nothing_switched_on_is_an_error_not_an_empty_success(connected, intuit):
    settings_service.update(connected, {"quickbooks.sync_sales": False,
                                        "quickbooks.sync_expenses": False})
    with pytest.raises(quickbooks.QuickBooksError, match="No hay nada que traer"):
        quickbooks.pull(connected)


# ----------------------------------------------------- idempotency, cursor

def test_a_second_pull_updates_instead_of_duplicating(connected, intuit):
    _seed_books(intuit)
    quickbooks.pull(connected)

    # The invoice gets paid in QuickBooks after the first pull.
    invoice = next(r for r in intuit.records["Invoice"] if r["Id"] == "32")
    invoice.update(Balance=0, MetaData={"LastUpdatedTime": utc(+1)})
    report = quickbooks.pull(connected)

    orders = _orders(connected)
    assert len(orders) == 3
    assert orders["QB-INV-32"].status == "approved"
    assert orders["QB-INV-32"].extra["source_status"] == "Paid"
    assert report["entities"]["orders:invoice"]["updated"] == 1
    assert len(_expenses(connected)) == 2


def test_later_pulls_ask_only_for_what_changed(connected, intuit):
    quickbooks.pull(connected)
    conn = quickbooks.connection(connected)
    assert conn.cursor is not None
    assert conn.last_sync_at is not None

    intuit.queries.clear()
    quickbooks.pull(connected)
    assert all("MetaData.LastUpdatedTime >= " in sql for sql in intuit.queries)
    assert all(conn.cursor in sql for sql in intuit.queries)


def test_an_old_transaction_edited_today_comes_through(connected, intuit):
    quickbooks.pull(connected)
    # Dated years ago, but Intuit touched it after our last pull.
    intuit.add("Purchase", "99", TotalAmt=42, TxnDate="2019-05-05", updated=utc(+1))
    quickbooks.pull(connected)
    assert "qbo:expenses:purchase:99" in _expenses(connected)


def test_the_cursor_does_not_move_when_a_pull_fails(connected, intuit):
    quickbooks.pull(connected)
    cursor = quickbooks.connection(connected).cursor

    intuit.api_status = 500
    with pytest.raises(quickbooks.QuickBooksError):
        quickbooks.pull(connected)

    connected.expire_all()
    assert quickbooks.connection(connected).cursor == cursor


def test_pages_are_walked_until_a_short_one(connected, intuit, monkeypatch):
    monkeypatch.setattr(quickbooks, "PAGE_SIZE", 2)
    for index in range(5):
        intuit.add("Purchase", str(index), TotalAmt=10 + index)

    report = quickbooks.pull(connected)

    purchase_queries = [q for q in intuit.queries if "FROM Purchase" in q]
    assert [q.split("STARTPOSITION ")[1].split()[0] for q in purchase_queries] == ["1", "3", "5"]
    assert report["entities"]["expenses:purchase"]["pages"] == 3
    assert len(_expenses(connected)) == 5


# ------------------------------------------------------------------ report

def test_the_report_is_kept_for_the_settings_card(connected, intuit):
    _seed_books(intuit)
    quickbooks.pull(connected)
    stored = quickbooks.status(connected)["last_pull"]
    assert stored["source"].startswith("QuickBooks · ")
    assert stored["entities"]["expenses:purchase"]["imported"] == 1


def test_wiping_imported_records_restarts_the_history(connected, intuit):
    _seed_books(intuit)
    quickbooks.pull(connected)
    connected.add(Expense(description="Manual", category="Operación", amount=5,
                          currency="USD", date="2026-03-01", extra={}))
    connected.commit()

    removed = api_sync.wipe(connected, "imported")

    assert (removed["orders"], removed["expenses"]) == (3, 2)
    assert [e.description for e in connected.query(Expense).all()] == ["Manual"]
    connected.expire_all()
    conn = quickbooks.connection(connected)
    assert conn.cursor is None and conn.status == "connected"
    assert quickbooks.status(connected)["last_pull"] is None


# ---------------------------------------------------------------- known bugs

@pytest.mark.xfail(strict=True, reason=(
    "BUG: a voided invoice comes back with TotalAmt 0, is skipped as 'no usable "
    "amount', and the previously imported order keeps counting as revenue."))
def test_a_voided_invoice_stops_counting_as_revenue(connected, intuit):
    intuit.add("Invoice", "31", TotalAmt=500, Balance=0, CustomerRef={"name": "Tienda Norte"})
    quickbooks.pull(connected)
    assert _orders(connected)["QB-INV-31"].status == "approved"

    invoice = intuit.records["Invoice"][0]
    invoice.update(TotalAmt=0, Balance=0, PrivateNote="Voided",
                   MetaData={"LastUpdatedTime": utc(+1)})
    quickbooks.pull(connected)

    order = _orders(connected).get("QB-INV-31")
    assert order is None or order.status != "approved" or order.amount == 0


@pytest.mark.xfail(strict=True, reason=(
    "BUG: when MAX_RECORDS truncates the first pull the cursor still advances "
    "to now, so the records past the cap are never fetched."))
def test_a_truncated_first_pull_catches_up_on_the_next_one(connected, intuit, monkeypatch):
    monkeypatch.setattr(quickbooks, "PAGE_SIZE", 2)
    monkeypatch.setattr(quickbooks, "MAX_RECORDS", 3)
    for index in range(5):
        intuit.add("Purchase", str(index), TotalAmt=10 + index, updated=utc(-60 + index))

    first = quickbooks.pull(connected)
    assert any("hay más" in p for p in first["entities"]["expenses:purchase"]["problems"])

    quickbooks.pull(connected)
    assert len(_expenses(connected)) == 5


@pytest.mark.xfail(strict=True, reason=(
    "BUG: a Purchase with Credit=true is a refund (money back) but is stored "
    "as a positive expense."))
def test_a_credit_card_refund_is_not_booked_as_an_expense(connected, intuit):
    intuit.add("Purchase", "5", TotalAmt=30, Credit=True, PaymentType="CreditCard")
    quickbooks.pull(connected)
    expense = _expenses(connected).get("qbo:expenses:purchase:5")
    assert expense is None or expense.amount < 0
