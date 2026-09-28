# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Customer Gold Traceability report -- every view, its columns, its arithmetic and its money gate.

Two kinds of test live here:

* **Unit** (plain ``unittest.TestCase``, intended for CI). ``trace`` is patched to return a real
  :class:`AttributionReplay` run over synthetic movements, so the report's arithmetic is checked
  against the pure engine without a single database read. Warehouse stage, roles, company
  permission and every ``frappe.get_all`` the money path makes are stubbed; the stubbed reader
  raises on any read it was not told to expect, so a hidden DB access fails loudly.
* **Integration** (``TestTraceabilityReportWritesNothingIntegration``) -- RPT-10. Subclasses the
  Customer Gold integration base and therefore SKIPS unless the site opts in with
  ``customer_gold_disposable_site``. It opens every view against real documents and asserts the
  report wrote nothing.

Every expected number below is worked by hand in the test (literals or ``Decimal`` arithmetic);
none is obtained by calling the code under test.
"""

import unittest
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting import customer_gold_allocations as cga
from jewellery_erpnext.customer_subcontracting import customer_gold_trace as cgt
from jewellery_erpnext.customer_subcontracting.report.customer_gold_traceability import (
	customer_gold_traceability as report,
)
from jewellery_erpnext.jewellery_erpnext.tests import (
	test_customer_gold_integration as base,
)

COMPANY = "CG Unit Co"
CUSTOMER = "CG-UNIT-A"
LANE = f"Customer Goods|{CUSTOMER}"
LEDGER = "Customer Gold Ledger Entry"
ALLOCATION = "Customer Gold Allocation"

RM_WH, TRANSIT_WH, WIP_WH, FG_WH, LOSS_WH = (
	"RM - CGU",
	"Transit - CGU",
	"WIP - CGU",
	"FG - CGU",
	"Loss - CGU",
)
#: Stage by warehouse, as ``warehouse_stage`` would read it from warehouse_type. The loss
#: warehouse deliberately maps to nothing: its batch reads "Loss / Scrap" only because the
#: replay produced it in a Process Loss voucher.
STAGES = {
	RM_WH: "RM",
	TRANSIT_WH: "Transit",
	WIP_WH: "WIP",
	FG_WH: "FG",
	LOSS_WH: None,
}

MONEY_FIELDS = {
	"booked_rate",
	"nominal_amount",
	"revaluation",
	"released_fg",
	"released_raw",
	"pending_amount",
	"settlement_vouchers",
	"financial_status",
	"basis",
}
FG_MONEY_FIELDS = {
	"basic_rate",
	"basic_amount",
	"additional_cost",
	"valuation_amount",
	"ledger_value",
	"running_rate",
}

REPORT = report.__name__
TRACE = cgt.__name__
ALLOCATIONS = cga.__name__


# ------------------------------------------------------------------------------------------
# Synthetic fixtures
# ------------------------------------------------------------------------------------------


def _receipt(
	name,
	voucher,
	item_code,
	batch_no,
	gross,
	purity,
	posting_date,
	carrying=None,
	stock_uom="Gram",
	row="r1",
):
	"""A receipt event shaped like ``load_receipts`` returns it."""
	return frappe._dict(
		name=name,
		company=COMPANY,
		customer=CUSTOMER,
		reference_doctype="Stock Entry",
		reference_docname=voucher,
		cg_source_row=row,
		item_code=item_code,
		batch_no=batch_no,
		stock_uom=stock_uom,
		cg_gross_qty_delta=gross,
		cg_carrying_value_delta=carrying,
		cg_currency="INR" if carrying is not None else None,
		purity=purity,
		unit="fine" if purity else "qty",
		posting_date=posting_date,
		key=f"{voucher}|{row}",
	)


def _mv(
	voucher_no,
	detail_no,
	posting,
	batch_no,
	warehouse,
	item_code,
	qty,
	purity=None,
	kind=cgt.KIND_MOVEMENT,
	voucher_type="Stock Entry",
	**extra,
):
	row = {
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"detail_no": detail_no,
		"posting": frappe.utils.get_datetime(posting),
		"batch_no": batch_no,
		"warehouse": warehouse,
		"item_code": item_code,
		"qty": qty,
		"purity": purity,
		"kind": kind,
		"disposition": cgt.DISPOSITION_OTHER,
	}
	row.update(extra)
	return row


def _replay(receipts, movements):
	return cgt.AttributionReplay({r.key: {"unit": r.unit} for r in receipts}).run(
		movements
	)


def _settlement_fixture():
	"""Three receipts, one of them stones in carats, traced through every stage.

	R1  100 g of 24KT (purity 100)   -> 100 fine g
	    transfer 40 g RM -> WIP; manufacture 20 g (WIP) into FG1; FG1 delivered;
	    10 g returned raw from RM; Process Loss 5 g (WIP) into loss batch LB1;
	    Process Loss 2 g (WIP) with no output.
	    Held: RM 100-40-10 = 50, WIP 40-20-5-2 = 13, Loss/Scrap 5. Delivered 20, returned 10,
	    loss 2. Owed 100-20-10 = 70. Balance 100-(50+13+5)-(20+10+2) = 0.
	R2  50 g of 22KT (purity 91.6)   -> 45.8 fine g
	    transfer 5 g RM -> Transit (4.58 fine); manufacture 25 g (RM) into FG2 (22.9 fine),
	    FG2 still in FG. Held RM 20 g = 18.32 + Transit 4.58 = 22.9, FG 22.9. Owed 45.8.
	R3  2.5 ct of stone (no purity)  -> measured in carats, never moved. Owed 2.5 ct.
	"""
	r1 = _receipt(
		"CGLE-R1", "SE-R1", "G24", "B1", 100, 100.0, "2026-09-01", carrying=716483.00
	)
	r2 = _receipt(
		"CGLE-R2", "SE-R2", "G22", "B2", 50, 91.6, "2026-09-01", carrying=327749.84
	)
	r3 = _receipt(
		"CGLE-R3",
		"SE-R3",
		"STONE",
		"B3",
		2.5,
		None,
		"2026-09-02",
		stock_uom="Carat",
	)
	receipts = [r1, r2, r3]
	production = {"kind": cgt.KIND_PRODUCTION, "lane": LANE}
	movements = [
		_mv(
			"SE-R1", "r1", "2026-09-01 10:00", "B1", RM_WH, "G24", 100, 100.0,
			kind=cgt.KIND_RECEIPT, receipt_key=r1.key,
		),
		_mv(
			"SE-R2", "r1", "2026-09-01 11:00", "B2", RM_WH, "G22", 50, 91.6,
			kind=cgt.KIND_RECEIPT, receipt_key=r2.key,
		),
		_mv(
			"SE-R3", "r1", "2026-09-02 10:00", "B3", RM_WH, "STONE", 2.5, None,
			kind=cgt.KIND_RECEIPT, receipt_key=r3.key,
		),
		_mv("SE-T1", "t1", "2026-09-05 10:00", "B1", RM_WH, "G24", -40, 100.0),
		_mv("SE-T1", "t1", "2026-09-05 10:00", "B1", WIP_WH, "G24", 40, 100.0),
		_mv("SE-T2", "t2", "2026-09-06 10:00", "B2", RM_WH, "G22", -5, 91.6),
		_mv("SE-T2", "t2", "2026-09-06 10:00", "B2", TRANSIT_WH, "G22", 5, 91.6),
		_mv("SE-M1", "m1a", "2026-09-10 10:00", "B1", WIP_WH, "G24", -20, 100.0, **production),
		_mv("SE-M1", "m1b", "2026-09-10 10:00", "FG1", FG_WH, "RING", 1, None, **production),
		_mv("SE-M2", "m2a", "2026-09-11 10:00", "B2", RM_WH, "G22", -25, 91.6, **production),
		_mv("SE-M2", "m2b", "2026-09-11 10:00", "FG2", FG_WH, "RING", 1, None, **production),
		_mv(
			"DN-1", "dn1", "2026-09-12 10:00", "FG1", FG_WH, "RING", -1, None,
			voucher_type="Delivery Note", disposition=cgt.DISPOSITION_DELIVERED,
		),
		_mv(
			"SE-RET1", "ret1", "2026-09-15 10:00", "B1", RM_WH, "G24", -10, 100.0,
			disposition=cgt.DISPOSITION_RETURNED,
		),
		_mv(
			"SE-L1", "l1a", "2026-09-18 10:00", "B1", WIP_WH, "G24", -5, 100.0,
			is_loss=True, **production,
		),
		_mv(
			"SE-L1", "l1b", "2026-09-18 10:00", "LB1", LOSS_WH, "G24", 5, 100.0,
			is_loss=True, **production,
		),
		_mv(
			"SE-L2", "l2a", "2026-09-19 10:00", "B1", WIP_WH, "G24", -2, 100.0,
			is_loss=True, **production,
		),
	]  # fmt: skip
	return receipts, _replay(receipts, movements)


class FakeReads:
	"""Answers exactly the reads the money path makes, and raises on anything else."""

	def __init__(
		self,
		allocations=(),
		allocated=(),
		ledger=(),
		reversed_=(),
		postings=None,
		cancelled_jes=(),
	):
		self.allocations = [frappe._dict(a) for a in allocations]
		self.allocated = list(allocated)
		self.ledger = [frappe._dict(e) for e in ledger]
		self.reversed_ = set(reversed_)
		self.postings = postings or {}
		self.cancelled_jes = set(cancelled_jes)

	def get_all(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
		filters = filters or {}
		if doctype == ALLOCATION:
			if pluck == "cg_event":
				return list(self.allocated)
			wanted = set(filters["receipt_event"][1])
			return [a for a in self.allocations if a.receipt_event in wanted]
		if doctype == "Journal Entry":
			assert filters.get("docstatus") == 2, filters
			return [n for n in filters["name"][1] if n in self.cancelled_jes]
		if doctype == LEDGER:
			kind = filters["cg_event_kind"]
			if kind == "Reversal":
				return [n for n in filters["cg_reversal_of"][1] if n in self.reversed_]
			assert filters.get("company") == COMPANY, filters
			kinds = set(kind[1]) if isinstance(kind, list) else {kind}
			return [e for e in self.ledger if e.cg_event_kind in kinds]
		if any(dt == doctype for dt, _name in self.postings):
			return [
				frappe._dict(
					name=name,
					posting_date=self.postings[(doctype, name)][0],
					posting_time=self.postings[(doctype, name)][1],
				)
				for name in filters["name"][1]
				if (doctype, name) in self.postings
			]
		raise AssertionError(f"unexpected read of {doctype} with {filters}")


def _no_reads(*args, **kwargs):
	raise AssertionError(f"unexpected frappe.get_all{args} -- the money path was read")


class _ReportUnitCase(unittest.TestCase):
	"""Stubs the environment every view needs: warehouse stage and company permission."""

	def setUp(self):
		for target in (
			patch(f"{TRACE}.warehouse_stage", side_effect=STAGES.get),
			patch("frappe.has_permission", return_value=True),
		):
			target.start()
			self.addCleanup(target.stop)

	def _execute(self, filters, roles, receipts, replay, reads=None):
		with (
			patch(f"{REPORT}.trace", return_value=(receipts, replay, set())) as traced,
			patch("frappe.get_roles", return_value=roles),
			patch(
				"frappe.get_all", side_effect=(reads.get_all if reads else _no_reads)
			),
			patch(f"{ALLOCATIONS}.is_allocation_schema_ready", return_value=True),
		):
			result = report.execute(dict({"company": COMPANY}, **filters))
		self.traced = traced
		return result


# ------------------------------------------------------------------------------------------
# Filters, roles, columns
# ------------------------------------------------------------------------------------------


class TestFilterValidation(unittest.TestCase):
	"""RU-01..RU-05 (report-unit checks, not matrix IDs) -- the filter guard runs before anything is read."""

	def _validate(self, filters, permitted=True):
		with patch("frappe.has_permission", return_value=permitted) as perm:
			report._validate_filters(frappe._dict(filters))
		return perm

	def test_company_is_required(self):
		"""RU-01."""
		with self.assertRaisesRegex(frappe.ValidationError, "Company is required"):
			self._validate({})

	def test_a_company_the_user_cannot_read_is_a_permission_error(self):
		"""RU-02."""
		with self.assertRaises(frappe.PermissionError):
			self._validate({"company": COMPANY}, permitted=False)

	def test_permission_is_checked_on_the_company_itself(self):
		"""RU-02."""
		perm = self._validate({"company": COMPANY})
		perm.assert_called_once_with("Company", doc=COMPANY)

	def test_an_unknown_view_is_refused(self):
		"""RU-03."""
		with self.assertRaisesRegex(frappe.ValidationError, "Unknown view"):
			self._validate({"company": COMPANY, "view": "Everything"})

	def test_from_date_after_to_date_is_refused(self):
		"""RU-04."""
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be after"):
			self._validate(
				{"company": COMPANY, "from_date": "2026-09-20", "to_date": "2026-09-10"}
			)
		# the same day on both ends is a valid one-day window
		self._validate(
			{"company": COMPANY, "from_date": "2026-09-10", "to_date": "2026-09-10"}
		)

	def test_fg_view_needs_an_entry_or_a_serial_number_creator(self):
		"""RU-05."""
		with self.assertRaisesRegex(frappe.ValidationError, "FG Valuation needs"):
			self._validate({"company": COMPANY, "view": report.VIEW_FG})
		self._validate(
			{"company": COMPANY, "view": report.VIEW_FG, "stock_entry": "SE-MFG-1"}
		)
		self._validate(
			{
				"company": COMPANY,
				"view": report.VIEW_FG,
				"serial_number_creator": "SNC-1",
			}
		)


class TestMoneyGate(_ReportUnitCase):
	"""RPT-11 -- money is withheld server-side, from columns AND from row data."""

	def test_only_accounts_roles_see_money(self):
		"""RPT-11."""
		cases = {
			("Accounts User",): True,
			("Accounts Manager",): True,
			("System Manager",): True,
			("Stock User", "Stock Manager", "Sales User"): False,
			(): False,
		}
		for roles, expected in cases.items():
			with self.subTest(roles=roles), patch(
				"frappe.get_roles", return_value=list(roles)
			):
				self.assertIs(report.can_see_money(), expected)

	def test_without_an_accounts_role_no_money_column_and_no_money_key(self):
		"""RPT-11 -- a Stock Manager exporting the report must not get money in the row data."""
		receipts, replay = _settlement_fixture()
		columns, rows, _msg, _chart, summary = self._execute(
			{}, ["Stock User", "Stock Manager"], receipts, replay
		)

		fieldnames = {c["fieldname"] for c in columns}
		self.assertFalse(fieldnames & MONEY_FIELDS)
		self.assertFalse([c for c in columns if c["fieldtype"] == "Currency"])
		self.assertEqual(len(rows), 3)
		for row in rows:
			self.assertFalse(set(row) & MONEY_FIELDS, row)
		self.assertNotIn("Pending liability", [s["label"] for s in summary])

	def test_with_accounts_user_the_money_columns_and_keys_appear(self):
		"""RPT-11."""
		receipts, replay = _settlement_fixture()
		# The rate-outlier check reads the purchase / feed reference; it has its own tests.
		with patch.object(report, "_rate_outliers", return_value={}):
			columns, rows, _msg, _chart, summary = self._execute(
				{}, ["Accounts User"], receipts, replay, reads=FakeReads()
			)

		fieldnames = [c["fieldname"] for c in columns]
		self.assertTrue(MONEY_FIELDS <= set(fieldnames))
		self.assertEqual(fieldnames[-1], "flags")
		for row in rows:
			self.assertTrue(MONEY_FIELDS <= set(row), row)
		by_receipt = {r["receipt"]: r for r in rows}
		# nothing released yet: pending is the whole nominal value
		self.assertAlmostEqual(
			by_receipt["SE-R1"]["pending_amount"], 716483.00, places=2
		)
		self.assertEqual(by_receipt["SE-R1"]["financial_status"], "Open")
		# the stone receipt carried no currency: not valued, never a zero
		self.assertIsNone(by_receipt["SE-R3"]["pending_amount"])
		self.assertEqual(by_receipt["SE-R3"]["financial_status"], "Not valued")
		pending = {s["label"]: s["value"] for s in summary}["Pending liability"]
		self.assertAlmostEqual(pending, 716483.00 + 327749.84, places=2)


class TestColumns(unittest.TestCase):
	"""RU-06 -- the column set of every view."""

	SETTLEMENT = [
		"receipt",
		"receipt_row",
		"posting_date",
		"customer",
		"item_code",
		"receipt_batch",
		"measure",
		"received",
		"delivered",
		"returned",
		"owed_back",
		"held_rm",
		"held_wip",
		"held_fg",
		"held_scrap",
		"loss",
		"unexplained",
		"material_status",
	]
	MONEY = [
		"booked_rate",
		"nominal_amount",
		"revaluation",
		"released_fg",
		"released_raw",
		"pending_amount",
		"settlement_vouchers",
		"financial_status",
		"basis",
	]

	@staticmethod
	def _names(columns):
		return [c["fieldname"] for c in columns]

	def test_settlement_columns_with_and_without_money(self):
		"""RU-06, RPT-11."""
		self.assertEqual(
			self._names(report._settlement_columns(False)), [*self.SETTLEMENT, "flags"]
		)
		self.assertEqual(
			self._names(report._settlement_columns(True)),
			[*self.SETTLEMENT, *self.MONEY, "flags"],
		)

	def test_position_columns(self):
		"""RU-06."""
		self.assertEqual(
			self._names(report._position_columns()),
			[
				"receipt",
				"receipt_row",
				"batch_no",
				"serial_no",
				"item_code",
				"generation",
				"produced_by",
				"warehouse",
				"stage",
				"holding_qty",
				"free_qty",
				"measure",
				"receipt_share",
				"receipt_equivalent",
			],
		)

	def test_movement_columns(self):
		"""RU-06."""
		self.assertEqual(
			self._names(report._movement_columns()),
			[
				"posting",
				"receipt",
				"receipt_row",
				"action",
				"voucher_type",
				"voucher_no",
				"batch_no",
				"serial_no",
				"item_code",
				"from_warehouse",
				"to_warehouse",
				"voucher_qty",
				"measure",
				"receipt_share",
			],
		)
		voucher = next(
			c for c in report._movement_columns() if c["fieldname"] == "voucher_no"
		)
		self.assertEqual(
			(voucher["fieldtype"], voucher["options"]), ("Dynamic Link", "voucher_type")
		)


# ------------------------------------------------------------------------------------------
# Receipt Settlement
# ------------------------------------------------------------------------------------------


class TestReceiptSettlementNumbers(_ReportUnitCase):
	"""RPT-07, RU-08, RU-09 -- received, disposed, owed and held-by-stage per receipt row."""

	def setUp(self):
		super().setUp()
		receipts, replay = _settlement_fixture()
		self.columns, rows, _msg, _chart, self.summary = self._execute(
			{}, ["Stock User"], receipts, replay
		)
		self.rows = {r["receipt"]: r for r in rows}

	def _assert_row(self, row, expected):
		for field, value in expected.items():
			with self.subTest(receipt=row["receipt"], field=field):
				if isinstance(value, float):
					self.assertAlmostEqual(row[field], value, places=3)
				else:
					self.assertEqual(row[field], value)

	def test_a_receipt_disposed_every_way_at_once(self):
		"""RPT-07 -- R1: delivered 20, returned 10, loss 2; held RM 50, WIP 13, scrap 5."""
		self._assert_row(
			self.rows["SE-R1"],
			{
				"receipt_row": "r1",
				"receipt_batch": "B1",
				"measure": "Fine g",
				"received": 100.0,
				"delivered": 20.0,
				"returned": 10.0,
				"owed_back": 70.0,
				"held_rm": 50.0,
				"held_wip": 13.0,
				"held_fg": 0.0,
				"held_scrap": 5.0,
				"loss": 2.0,
				"unexplained": 0.0,
				"material_status": "Partly disposed",
				"flags": "",
			},
		)

	def test_a_part_purity_receipt_is_measured_in_fine_grams(self):
		"""RPT-07 -- R2: 50 g at 91.6 = 45.8 fine; RM 18.32 + Transit 4.58 = 22.9, FG 22.9."""
		self._assert_row(
			self.rows["SE-R2"],
			{
				"measure": "Fine g",
				"received": 45.8,
				"delivered": 0.0,
				"returned": 0.0,
				"owed_back": 45.8,
				"held_rm": 22.9,
				"held_wip": 0.0,
				"held_fg": 22.9,
				"held_scrap": 0.0,
				"loss": 0.0,
				"material_status": "Open",
				"flags": "",
			},
		)

	def test_a_stone_receipt_stays_in_its_own_unit(self):
		"""RPT-07 -- R3: 2.5 ct, measured in carats, never converted to fine grams."""
		self._assert_row(
			self.rows["SE-R3"],
			{
				"measure": "Carat",
				"received": 2.5,
				"owed_back": 2.5,
				"held_rm": 2.5,
				"material_status": "Open",
			},
		)

	def test_the_summary_adds_only_fine_gram_rows(self):
		"""RU-08 -- fine totals are 100 + 45.8 = 145.8 and 70 + 45.8 = 115.8; the 2.5 ct row
		is counted as a row but never summed into grams (148.3 / 118.3 would be the defect)."""
		summary = {s["label"]: s["value"] for s in self.summary}
		self.assertEqual(summary["Receipt rows"], 3)
		self.assertAlmostEqual(summary["Received (fine g)"], 145.8, places=3)
		self.assertAlmostEqual(summary["Still owed (fine g)"], 115.8, places=3)

	def test_the_filters_narrow_the_receipts(self):
		"""RU-09 -- item, receipt row and to_date filters drop receipts, not replay figures."""
		receipts, replay = _settlement_fixture()
		cases = {
			"item": ({"item_code": "G22"}, {"SE-R2"}),
			"to_date": ({"to_date": "2026-09-01"}, {"SE-R1", "SE-R2"}),
			"batch": ({"batch": "B3"}, {"SE-R3"}),
			"receipt": ({"receipt": "SE-R1"}, {"SE-R1"}),
		}
		for label, (filters, expected) in cases.items():
			with self.subTest(label):
				_c, rows, *_rest = self._execute(
					filters, ["Stock User"], receipts, replay
				)
				self.assertEqual({r["receipt"] for r in rows}, expected)
		# the replay is always asked for the customer's whole scope, cut at the day's end
		self._execute({"to_date": "2026-09-01"}, ["Stock User"], receipts, replay)
		_args, kwargs = self.traced.call_args
		self.assertEqual(
			kwargs["to_datetime"],
			frappe.utils.get_datetime("2026-09-01 23:59:59.999999"),
		)


class TestSettlementSummary(unittest.TestCase):
	"""RPT-07 / RU-08 on the summary alone."""

	def test_a_carat_row_is_never_summed_into_fine_grams(self):
		"""RPT-07, RU-08."""
		rows = [
			{"measure": "Fine g", "received": 10.0, "owed_back": 4.0, "pending_amount": 100.0},
			{"measure": "Fine g", "received": 5.5, "owed_back": 5.5, "pending_amount": None},
			{"measure": "Carat", "received": 1000.0, "owed_back": 900.0, "pending_amount": 50.0},
		]  # fmt: skip
		summary = {
			s["label"]: s["value"] for s in report._settlement_summary(rows, True)
		}
		self.assertEqual(summary["Receipt rows"], 3)
		self.assertAlmostEqual(summary["Received (fine g)"], 15.5, places=3)
		self.assertAlmostEqual(summary["Still owed (fine g)"], 9.5, places=3)
		# money is additive across units; an unvalued row contributes nothing
		self.assertAlmostEqual(summary["Pending liability"], 150.0, places=2)

		without_money = report._settlement_summary(rows, False)
		self.assertNotIn("Pending liability", [s["label"] for s in without_money])


class TestStatuses(unittest.TestCase):
	"""RU-12 material status, RU-13 financial status."""

	def test_material_status(self):
		"""RU-12."""
		cases = [
			# received, delivered, returned, held -> status
			((0.0, 0.0, 0.0, 0.0), "Nothing received"),
			((10.0, 6.0, 4.0, 0.0), "Closed"),
			((10.0, 6.0, 3.9996, 0.0), "Closed"),  # within the 0.0005 tolerance
			((10.0, 3.0, 0.0, 0.0), "Owed, no metal held"),
			((10.0, 3.0, 1.0, 6.0), "Partly disposed"),
			((10.0, 0.0, 0.0, 10.0), "Open"),
		]
		for args, expected in cases:
			with self.subTest(args=args):
				self.assertEqual(report._material_status(*args), expected)

	def test_financial_status(self):
		"""RU-13 -- including ACC-16: material closed but the liability still pending."""
		allocated = "Allocated"
		cases = [
			(dict(nominal=None, pending=None, basis=allocated), 5.0, "Not valued"),
			# A stone typed at 0: valued, at zero -- nothing was booked, so nothing is "settled".
			(
				dict(nominal=0.0, pending=0.0, revaluation=0.0, basis=allocated),
				10.0,
				"Nothing booked",
			),
			(dict(nominal=1000.0, pending=0.004, basis=allocated), 5.0, "Settled"),
			(
				dict(nominal=1000.0, pending=250.0, released_fg=750.0, basis=allocated),
				0.0,
				"Material closed, settlement pending",
			),
			(
				dict(
					nominal=1000.0, pending=600.0, released_raw=400.0, basis=allocated
				),
				3.0,
				"Partly settled",
			),
			(dict(nominal=1000.0, pending=1000.0, basis=allocated), 5.0, "Open"),
			(
				dict(nominal=1000.0, pending=600.0, released_fg=400.0, basis="Derived"),
				3.0,
				"Partly settled (derived)",
			),
			(
				dict(nominal=1000.0, pending=0.0, basis="Derived"),
				0.0,
				"Settled (derived)",
			),
		]
		for money, owed, expected in cases:
			with self.subTest(expected=expected):
				self.assertEqual(
					report._financial_status(frappe._dict(money), owed), expected
				)


class TestRateOutliers(unittest.TestCase):
	"""RU-14 -- a receipt booked at ten times the per-gram rate is flagged against an INDEPENDENT
	reference (``customer_gold_rate.reference_rate``: latest purchase, else an earlier feed day),
	never against the customer's other receipts -- on kg-gk the 10x receipts were the majority."""

	@staticmethod
	def _rated(name, gross, purity, value, currency="INR"):
		receipt = _receipt(
			name, f"SE-{name}", "G", f"B-{name}", gross, purity, "2026-09-01"
		)
		receipt.cg_carrying_value_delta = value
		receipt.cg_currency = currency
		return receipt

	def _outliers(self, receipts, reference):
		rate = "jewellery_erpnext.customer_subcontracting.customer_gold_rate"
		settings = (
			"jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings."
			"subcontracting_settings.get_customer_gold_settings"
		)
		side = reference if callable(reference) else (lambda *a, **k: reference)
		with patch(f"{rate}.reference_rate", side_effect=side), patch(
			settings, return_value=frappe._dict()
		):
			return report._rate_outliers(receipts)

	def test_the_ten_times_receipts_are_flagged_even_when_they_are_the_majority(self):
		"""RU-14 -- reference 7,164.83/g (band 0.5x..2x). Per gram: A 7,164.83 (1.0x), B
		130,072/20 = 6,503.60 (0.91x), C..E 71,648.30 (10.0x). C, D, E flagged although they are
		three of five; a stone and an unvalued row are ignored."""
		receipts = [
			self._rated("A", 10, 100.0, 71648.30),
			self._rated("B", 20, 91.6, 130072.00),
			self._rated("C", 10, 100.0, 716483.00),
			self._rated("D", 1, 100.0, 71648.30),
			self._rated("E", 2, 100.0, 143296.60),
			self._rated("STONE", 2.5, None, 3750.00),
			self._rated("UNVALUED", 10, 100.0, 1.0, currency=None),
		]
		reference = frappe._dict(rate=7164.83, source="PR-1 (2026-09-01)")
		flagged = self._outliers(receipts, reference)
		self.assertEqual(set(flagged), {"SE-C|r1", "SE-D|r1", "SE-E|r1"})
		self.assertIn("10.0x", flagged["SE-C|r1"])
		self.assertIn("PR-1", flagged["SE-C|r1"])

	def test_no_reference_flags_nothing(self):
		"""RU-14 -- with no purchase and no earlier feed day there is nothing to compare with."""
		receipts = [self._rated("C", 10, 100.0, 716483.00)]
		self.assertEqual(self._outliers(receipts, None), {})

	def test_a_failing_reference_lookup_flags_nothing_and_does_not_break_the_report(
		self,
	):
		"""RU-14."""

		def boom(*args, **kwargs):
			raise frappe.ValidationError("feed unavailable")

		receipts = [self._rated("C", 10, 100.0, 716483.00)]
		self.assertEqual(self._outliers(receipts, boom), {})

	def test_the_flag_reaches_the_row_only_for_users_who_see_money(self):
		"""RU-14, RPT-11 -- the flag quotes rates, so it is money."""
		receipts = [
			self._rated("A", 10, 100.0, 71648.30),
			self._rated("C", 10, 100.0, 716483.00),
		]
		replay = cgt.AttributionReplay({r.key: {"unit": r.unit} for r in receipts})
		flagged = {"SE-C|r1": "booked 71648.3/g is 10.0x the reference"}
		with patch(f"{TRACE}.warehouse_stage", side_effect=STAGES.get), patch.object(
			report, "_rate_outliers", return_value=flagged
		), patch.object(report, "_money_by_receipt", return_value={}):
			with_money = report._settlement_rows(receipts, replay, frappe._dict(), True)
			without = report._settlement_rows(receipts, replay, frappe._dict(), False)
		self.assertIn(
			"10.0x the reference",
			{r["receipt"]: r["flags"] for r in with_money}["SE-C"],
		)
		self.assertEqual({r["receipt"]: r["flags"] for r in without}["SE-C"], "")


# ------------------------------------------------------------------------------------------
# Money by receipt
# ------------------------------------------------------------------------------------------


def _alloc(name, disposition, amount, doctype, docname, voucher=None, reversal_of=None):
	return {
		"name": name,
		"receipt_event": "CGLE-R1",
		"disposition": disposition,
		"reversal_of": reversal_of,
		"reference_doctype": doctype,
		"reference_docname": docname,
		"settlement_voucher": voucher,
		"amount": amount,
		"recorded_at": None,
	}


class TestMoneyFromAllocations(unittest.TestCase):
	"""RU-15, RU-16, RU-17 -- released amounts read from Customer Gold Allocation.

	R1: 100 g at 7,164.83/g -> nominal 716,483.00.
	  A1 FG Delivery    DN-1      JV-1  +143,296.60  (20 g)
	  A2 Delivery Return DN-RET-1 JV-2   -35,824.15  (5 g back)
	  A3 Raw Return     SE-RET-1         +71,648.30  (10 g)
	  A4 Raw Return     SE-RET-2         +14,329.66  (2 g; SE-RET-2 later cancelled)
	  A5 Reversal of A4 SE-RET-2         -14,329.66
	  Revaluation SR-1 on B1 (R1 holds all of it) +5,000.00; SR-2 (reversed) +777.00 ignored.

	released_fg  = 143,296.60 - 35,824.15            = 107,472.45
	released_raw = 71,648.30 + 14,329.66 - 14,329.66 =  71,648.30
	  (a Reversal counted as FG instead of mapped back to Raw Return would read 93,142.79 /
	  85,977.96)
	pending      = 716,483.00 + 5,000.00 - 107,472.45 - 71,648.30 = 542,362.25
	JV-1 is cancelled but A1 was never reversed -> flagged.
	"""

	def setUp(self):
		self.receipt = _receipt(
			"CGLE-R1",
			"SE-R1",
			"G24",
			"B1",
			100,
			100.0,
			"2026-09-01",
			carrying=716483.00,
		)
		self.replay = _replay(
			[self.receipt],
			[
				_mv(
					"SE-R1", "r1", "2026-09-01 10:00", "B1", RM_WH, "G24", 100, 100.0,
					kind=cgt.KIND_RECEIPT, receipt_key=self.receipt.key,
				),
				_mv(
					"SR-1", "sr1", "2026-09-16 10:00", "B1", RM_WH, "G24", -100, 100.0,
					kind=cgt.KIND_RECONCILIATION, voucher_type="Stock Reconciliation",
				),
				_mv(
					"SR-1", "sr1", "2026-09-16 10:00", "B1", RM_WH, "G24", 100, 100.0,
					kind=cgt.KIND_RECONCILIATION, voucher_type="Stock Reconciliation",
				),
			],
		)  # fmt: skip
		self.reads = FakeReads(
			allocations=[
				_alloc("A1", "FG Delivery", 143296.60, "Delivery Note", "DN-1", "JV-1"),
				_alloc("A2", "Delivery Return", -35824.15, "Delivery Note", "DN-RET-1", "JV-2"),
				_alloc("A3", "Raw Return", 71648.30, "Stock Entry", "SE-RET-1"),
				_alloc("A4", "Raw Return", 14329.66, "Stock Entry", "SE-RET-2"),
				_alloc(
					"A5", "Reversal", -14329.66, "Stock Entry", "SE-RET-2", reversal_of="A4"
				),
			],
			allocated=["DN-1-EVENT", "DN-RET-1-EVENT", "SE-RET-1-EVENT", "SE-RET-2-EVENT"],
			ledger=[
				{
					"name": "REV-1",
					"cg_event_kind": "Revaluation",
					"reference_doctype": "Stock Reconciliation",
					"reference_docname": "SR-1",
					"batch_no": "B1",
					"cg_carrying_value_delta": 5000.00,
				},
				{
					"name": "REV-2",
					"cg_event_kind": "Revaluation",
					"reference_doctype": "Stock Reconciliation",
					"reference_docname": "SR-1",
					"batch_no": "B1",
					"cg_carrying_value_delta": 777.00,
				},
			],
			reversed_=["REV-2"],
			postings={
				("Delivery Note", "DN-1"): ("2026-09-12", "10:00:00"),
				("Delivery Note", "DN-RET-1"): ("2026-09-20", "10:00:00"),
				("Stock Entry", "SE-RET-1"): ("2026-09-15", "10:00:00"),
				("Stock Entry", "SE-RET-2"): ("2026-09-13", "10:00:00"),
				("Stock Reconciliation", "SR-1"): ("2026-09-16", "10:00:00"),
			},
			cancelled_jes=["JV-1"],
		)  # fmt: skip

	def _money(self, filters=None):
		with (
			patch("frappe.get_all", side_effect=self.reads.get_all),
			patch(f"{ALLOCATIONS}.is_allocation_schema_ready", return_value=True),
		):
			return report._money_by_receipt(
				[self.receipt], self.replay, frappe._dict(filters or {})
			)["CGLE-R1"]

	def test_released_split_by_disposition_with_reversal_mapped_back(self):
		"""RU-15 -- FG Delivery + Delivery Return -> FG; Raw Return + its Reversal -> raw."""
		m = self._money()
		self.assertAlmostEqual(m.booked_rate, 7164.83, places=2)
		self.assertAlmostEqual(m.nominal, 716483.00, places=2)
		self.assertAlmostEqual(m.released_fg, 107472.45, places=2)
		self.assertAlmostEqual(m.released_raw, 71648.30, places=2)
		self.assertEqual(m.basis, "Allocated")
		self.assertEqual(m.vouchers, {"JV-1", "JV-2", "SE-RET-1", "SE-RET-2"})

	def test_pending_is_nominal_plus_revaluation_minus_released(self):
		"""RU-16 -- 716,483.00 + 5,000.00 - 107,472.45 - 71,648.30 = 542,362.25."""
		m = self._money()
		self.assertAlmostEqual(m.revaluation, 5000.00, places=2)
		expected = (
			Decimal("716483.00")
			+ Decimal("5000.00")
			- Decimal("107472.45")
			- Decimal("71648.30")
		)
		self.assertEqual(expected, Decimal("542362.25"))
		self.assertAlmostEqual(m.pending, float(expected), places=2)

	def test_a_cancelled_settlement_voucher_with_a_standing_allocation_is_flagged(self):
		"""RU-17."""
		m = self._money()
		self.assertEqual(
			m.flags, ["settlement JV-1 is cancelled but its allocation stands"]
		)

	def test_a_reversed_allocation_hides_its_cancelled_voucher(self):
		"""RU-17 -- once A1 is reversed, the cancelled JV-1 is consistent, not a finding."""
		self.reads.allocations.append(
			frappe._dict(
				_alloc(
					"A6",
					"Reversal",
					-143296.60,
					"Delivery Note",
					"DN-1",
					reversal_of="A1",
				)
			)
		)
		m = self._money()
		self.assertEqual(m.flags, [])
		# 107,472.45 - 143,296.60: the reversal lands on FG, the disposition it undoes
		self.assertAlmostEqual(m.released_fg, -35824.15, places=2)
		self.assertAlmostEqual(m.released_raw, 71648.30, places=2)

	def test_the_cutoff_drops_later_allocations_and_revaluations(self):
		"""RU-16 -- to 2026-09-13: only DN-1 (09-12) and SE-RET-2 (09-13, net 0) count;
		DN-RET-1 (09-20), SE-RET-1 (09-15) and SR-1 (09-16) are after the cutoff.
		pending = 716,483.00 - 143,296.60 = 573,186.40."""
		m = self._money({"to_date": "2026-09-13"})
		self.assertAlmostEqual(m.released_fg, 143296.60, places=2)
		self.assertAlmostEqual(m.released_raw, 0.0, places=2)
		self.assertAlmostEqual(m.revaluation, 0.0, places=2)
		self.assertAlmostEqual(m.pending, 573186.40, places=2)


class TestLegacyDerivedReleases(unittest.TestCase):
	"""RU-18 -- a legacy event (no allocation rows) split across receipts by the replay.

	R2: 10 g of 24KT (purity 100) booked 71,648.30  -> 7,164.83 per g = per fine g
	R3: 10 g of 22KT (purity 91.6) booked 65,000.00 -> 6,500 per g = 6,500/0.916 per fine g
	SE-M9 consumes 4 g of R2's batch (4 fine) and 5 g of R3's (4.58 fine) into one piece;
	DN-9 delivers it. The legacy Delivery event on DN-9 released 60,000.00.

	weight R2 = 4 x 7,164.83            = 28,659.32
	weight R3 = 4.58 x 6,500 / 0.916    = 32,500.00
	R2 part   = 60,000 x 28,659.32 / 61,159.32 = 28,116.06
	R3 part   = 60,000 x 32,500.00 / 61,159.32 = 31,883.94

	A legacy Return on SE-RET9 (2 g of R2's batch) released 14,329.66 -> all to R2, raw.
	LEG-3 (reversed) and LEG-4 (has allocations) are on DN-9 too and must be ignored.
	"""

	def setUp(self):
		r2 = _receipt(
			"CGLE-R2", "SE-R2", "G24", "B2", 10, 100.0, "2026-09-01", carrying=71648.30
		)
		r3 = _receipt(
			"CGLE-R3", "SE-R3", "G22", "B3", 10, 91.6, "2026-09-01", carrying=65000.00
		)
		self.receipts = [r2, r3]
		production = {"kind": cgt.KIND_PRODUCTION, "lane": LANE}
		self.replay = _replay(
			self.receipts,
			[
				_mv(
					"SE-R2", "r1", "2026-09-01 10:00", "B2", RM_WH, "G24", 10, 100.0,
					kind=cgt.KIND_RECEIPT, receipt_key=r2.key,
				),
				_mv(
					"SE-R3", "r1", "2026-09-01 11:00", "B3", RM_WH, "G22", 10, 91.6,
					kind=cgt.KIND_RECEIPT, receipt_key=r3.key,
				),
				_mv("SE-M9", "m9a", "2026-09-05", "B2", RM_WH, "G24", -4, 100.0, **production),
				_mv("SE-M9", "m9b", "2026-09-05", "B3", RM_WH, "G22", -5, 91.6, **production),
				_mv("SE-M9", "m9c", "2026-09-05", "FG9", FG_WH, "RING", 1, None, **production),
				_mv(
					"DN-9", "dn9-r1", "2026-09-06", "FG9", FG_WH, "RING", -1, None,
					voucher_type="Delivery Note", disposition=cgt.DISPOSITION_DELIVERED,
				),
				_mv(
					"SE-RET9", "ret9-r1", "2026-09-07", "B2", RM_WH, "G24", -2, 100.0,
					disposition=cgt.DISPOSITION_RETURNED,
				),
			],
		)  # fmt: skip

		def event(
			name, kind, docname, row, value, voucher=None, doctype="Delivery Note"
		):
			return {
				"name": name,
				"cg_event_kind": kind,
				"reference_doctype": doctype,
				"reference_docname": docname,
				"cg_source_row": row,
				"cg_carrying_value_delta": value,
				"cg_settlement_voucher": voucher,
			}

		self.reads = FakeReads(
			allocated=["LEG-4"],
			ledger=[
				event("LEG-1", "Delivery", "DN-9", "dn9-r1", -60000.00, "JV-9"),
				event("LEG-2", "Return", "SE-RET9", "ret9-r1", -14329.66, doctype="Stock Entry"),
				event("LEG-3", "Delivery", "DN-9", "dn9-r1", -99999.00, "JV-8"),
				event("LEG-4", "Delivery", "DN-9", "dn9-r1", -88888.00, "JV-7"),
			],
			reversed_=["LEG-3"],
			postings={
				("Delivery Note", "DN-9"): ("2026-09-06", "10:00:00"),
				("Stock Entry", "SE-RET9"): ("2026-09-07", "10:00:00"),
			},
		)  # fmt: skip

	def test_a_legacy_event_is_split_by_trail_share_times_booked_rate(self):
		"""RU-18."""
		with (
			patch("frappe.get_all", side_effect=self.reads.get_all),
			patch(f"{ALLOCATIONS}.is_allocation_schema_ready", return_value=True),
		):
			money = report._money_by_receipt(self.receipts, self.replay, frappe._dict())

		w2 = Decimal("4") * Decimal("7164.83")
		w3 = Decimal("4.58") * Decimal("6500") / Decimal("0.916")
		self.assertEqual(w3, Decimal("32500"))
		r2_part = (Decimal("60000") * w2 / (w2 + w3)).quantize(Decimal("0.01"))
		r3_part = (Decimal("60000") * w3 / (w2 + w3)).quantize(Decimal("0.01"))
		self.assertEqual((r2_part, r3_part), (Decimal("28116.06"), Decimal("31883.94")))

		r2, r3 = money["CGLE-R2"], money["CGLE-R3"]
		self.assertAlmostEqual(r2.released_fg, float(r2_part), places=2)
		self.assertAlmostEqual(r3.released_fg, float(r3_part), places=2)
		self.assertAlmostEqual(r2.released_raw, 14329.66, places=2)
		self.assertAlmostEqual(r3.released_raw, 0.0, places=2)
		self.assertEqual((r2.basis, r3.basis), ("Derived", "Derived"))
		self.assertEqual(r2.vouchers, {"JV-9", "SE-RET9"})
		self.assertEqual(r3.vouchers, {"JV-9"})
		# pending = nominal - released
		self.assertAlmostEqual(
			r2.pending,
			float(Decimal("71648.30") - r2_part - Decimal("14329.66")),
			places=2,
		)
		self.assertAlmostEqual(
			r3.pending, float(Decimal("65000.00") - r3_part), places=2
		)
		self.assertEqual(
			report._financial_status(r3, 10 * 0.916 - 4.58), "Partly settled (derived)"
		)


# ------------------------------------------------------------------------------------------
# FG Valuation
# ------------------------------------------------------------------------------------------


def _se_row(name, idx, item_code, qty, rate, amount, **fields):
	row = frappe._dict(
		name=name,
		idx=idx,
		item_code=item_code,
		batch_no=f"BATCH-{name}",
		transfer_qty=qty,
		stock_uom="Nos",
		s_warehouse=None,
		t_warehouse=None,
		inventory_type=None,
		customer=None,
		basic_rate=rate,
		basic_amount=flt(qty) * flt(rate),
		additional_cost=0.0,
		amount=amount,
		valuation_rate=(flt(amount) / flt(qty)) if qty else 0.0,
	)
	row.update(fields)
	return row


def _manufacture(fg_rows, additional=7.11, company=COMPANY):
	"""F1 / VAL-01: A 2 x 1000 (customer gold) + B 3 x 500 (company diamond) + 7.11."""
	consumed = [
		_se_row(
			"sa", 1, "GOLD-22KT", 2, 1000.0, 2000.0,
			s_warehouse=WIP_WH, inventory_type="Customer Goods", customer=CUSTOMER,
		),
		_se_row(
			"sb", 2, "DIAMOND", 3, 500.0, 1500.0,
			s_warehouse=WIP_WH, inventory_type="Regular Stock",
		),
	]  # fmt: skip
	return _FakeStockEntry(
		name="SE-MFG-1",
		company=company,
		items=consumed + fg_rows,
		additional_costs=[frappe._dict(amount=additional)] if additional else [],
	)


class _FakeStockEntry:
	"""The attributes ``_fg_valuation`` reads from a Stock Entry, and nothing else. A
	``frappe._dict`` cannot stand in: its ``items`` is the dict method."""

	def __init__(self, **fields):
		self.__dict__.update(fields)
		self.permission_checks = []

	def get(self, fieldname, default=None):
		return self.__dict__.get(fieldname, default)

	def check_permission(self, ptype):
		self.permission_checks.append(ptype)


def _fg(name, idx, amount, qty=1):
	return _se_row(
		name,
		idx,
		"FG-RING",
		qty,
		3500.0 / 2 if name != "fg" else 3500.0,
		amount,
		t_warehouse=FG_WH,
		inventory_type="Customer Goods",
		customer=CUSTOMER,
		additional_cost=7.11 if name == "fg" else 3.555,
	)


class TestFGValuation(unittest.TestCase):
	"""VAL-01, VAL-02, VAL-03, VAL-09 -- the bridge from consumed amounts to the posted FG."""

	def _run(self, entry, show_money=True, sle=()):
		sle_rows = [frappe._dict(r) for r in sle]

		def get_all(doctype, filters=None, fields=None, **kwargs):
			self.assertEqual(doctype, "Stock Ledger Entry")
			self.assertEqual(filters, {"voucher_no": entry.name, "is_cancelled": 0})
			return sle_rows

		def get_doc(doctype, name):
			self.assertEqual((doctype, name), ("Stock Entry", entry.name))
			return entry

		with (
			patch("frappe.get_all", side_effect=get_all),
			patch("frappe.get_doc", side_effect=get_doc),
		):
			return report._fg_valuation(
				frappe._dict(company=COMPANY, stock_entry=entry.name), show_money
			)

	@staticmethod
	def _bridge(rows):
		return {r["note"]: r["valuation_amount"] for r in rows if r["side"] == "Bridge"}

	def test_val01_consumed_plus_additional_equals_the_posted_fg(self):
		"""VAL-01 / F1 -- 2000 + 1500 + 7.11 = 3507.11 expected; FG posted 3507.11; diff 0."""
		entry = _manufacture([_fg("fg", 3, 3507.11)])
		_columns, rows = self._run(entry)
		self.assertEqual(entry.permission_checks, ["read"])
		bridge = self._bridge(rows)
		self.assertAlmostEqual(bridge["Consumed materials, total"], 3500.00, places=2)
		self.assertAlmostEqual(bridge["Additional costs"], 7.11, places=2)
		self.assertAlmostEqual(
			bridge["Expected FG value (consumed + additional)"], 3507.11, places=2
		)
		self.assertAlmostEqual(bridge["Posted FG value"], 3507.11, places=2)
		self.assertAlmostEqual(bridge["Difference"], 0.0, places=2)

		fg_row = next(r for r in rows if r["side"] == "Produced")
		self.assertAlmostEqual(fg_row["valuation_amount"], 3507.11, places=2)

	def test_consumed_is_split_by_owner(self):
		"""VAL-01 -- customer gold 2000 and company stock 1500, each with its own bridge row."""
		_columns, rows = self._run(_manufacture([_fg("fg", 3, 3507.11)]))
		bridge = self._bridge(rows)
		self.assertAlmostEqual(
			bridge[f"Consumed: Customer Goods: {CUSTOMER}"], 2000.00, places=2
		)
		self.assertAlmostEqual(bridge["Consumed: Regular Stock"], 1500.00, places=2)
		owners = {r["idx"]: r["owner"] for r in rows if r["side"] == "Consumed"}
		self.assertEqual(owners, {1: f"Customer Goods: {CUSTOMER}", 2: "Regular Stock"})

	def test_val02_two_units_at_half_a_paisa_still_bridge_to_the_total(self):
		"""VAL-02 -- two FG units of 1753.555 each: the bridge sums the unrounded amounts to
		3507.11 (adding the two rows rounded to paise could read 3507.12), and the difference
		stays 0."""
		_columns, rows = self._run(
			_manufacture([_fg("fg1", 3, 1753.555), _fg("fg2", 4, 1753.555)])
		)
		bridge = self._bridge(rows)
		self.assertAlmostEqual(bridge["Posted FG value"], 3507.11, places=2)
		self.assertAlmostEqual(bridge["Difference"], 0.0, places=2)
		self.assertEqual(len([r for r in rows if r["side"] == "Produced"]), 2)

	def test_val03_summing_unit_rates_is_not_the_consumed_total(self):
		"""VAL-03 -- 1000 + 500 = 1500 per-unit rates; the consumed total is 2000 + 1500 = 3500.
		The report shows both, and the bridge carries the amount, not the rate sum."""
		_columns, rows = self._run(_manufacture([_fg("fg", 3, 3507.11)]))
		consumed_rates = sum(r["basic_rate"] for r in rows if r["side"] == "Consumed")
		self.assertAlmostEqual(consumed_rates, 1500.0, places=2)
		self.assertAlmostEqual(
			self._bridge(rows)["Consumed materials, total"], 3500.0, places=2
		)
		self.assertNotAlmostEqual(consumed_rates, 3500.0, places=2)

	def test_val09_running_rate_note_only_where_the_ledger_rate_differs(self):
		"""VAL-09 -- the FG's SLE carries the warehouse average (2800 over 5 units), not the
		row's 3507.11: the row says so. Gold's SLE rate equals its row rate: no note."""
		sle = [
			{
				"voucher_detail_no": "sa",
				"stock_value_difference": -2000.0,
				"valuation_rate": 1000.0,
				"qty_after_transaction": 8.0,
			},
			{
				"voucher_detail_no": "fg",
				"stock_value_difference": 3507.11,
				"valuation_rate": 2800.0,
				"qty_after_transaction": 5.0,
			},
		]
		_columns, rows = self._run(_manufacture([_fg("fg", 3, 3507.11)]), sle=sle)
		by_idx = {r["idx"]: r for r in rows if r["side"] != "Bridge"}
		self.assertIn("Running rate is the warehouse average", by_idx[3]["note"])
		self.assertIn("5.0 units held", by_idx[3]["note"])
		self.assertAlmostEqual(by_idx[3]["running_rate"], 2800.0, places=4)
		self.assertAlmostEqual(by_idx[3]["ledger_value"], 3507.11, places=2)
		self.assertEqual(by_idx[1]["note"], "")
		# no SLE row at all (diamond) -> no note, running rate 0
		self.assertEqual(by_idx[2]["note"], "")

	def test_without_money_no_money_columns_keys_or_bridge(self):
		"""VAL-01, RPT-11 -- quantities only."""
		columns, rows = self._run(
			_manufacture([_fg("fg", 3, 3507.11)]), show_money=False
		)
		self.assertFalse({c["fieldname"] for c in columns} & FG_MONEY_FIELDS)
		self.assertEqual(len(rows), 3)
		for row in rows:
			self.assertFalse(set(row) & FG_MONEY_FIELDS, row)
			self.assertNotEqual(row["side"], "Bridge")

	def test_an_entry_of_another_company_is_refused(self):
		"""RU-05."""
		with self.assertRaisesRegex(
			frappe.ValidationError, "belongs to another company"
		):
			self._run(_manufacture([_fg("fg", 3, 3507.11)], company="Other Co"))

	def test_execute_routes_the_fg_view_without_a_trace(self):
		"""RU-05 -- the FG view never replays the ledger."""
		entry = _manufacture([_fg("fg", 3, 3507.11)])
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.get_roles", return_value=["Accounts User"]),
			patch(f"{REPORT}.trace", side_effect=AssertionError("traced")),
			patch("frappe.get_all", return_value=[]),
			patch("frappe.get_doc", return_value=entry),
		):
			columns, rows = report.execute(
				{"company": COMPANY, "view": report.VIEW_FG, "stock_entry": entry.name}
			)
		self.assertIn("valuation_amount", {c["fieldname"] for c in columns})
		self.assertAlmostEqual(self._bridge(rows)["Difference"], 0.0, places=2)


# ------------------------------------------------------------------------------------------
# Material Position and Movements
# ------------------------------------------------------------------------------------------


class TestMaterialPosition(_ReportUnitCase):
	"""RU-19 -- where each receipt's metal is, and its receipt-item equivalent."""

	BATCH_ITEMS = {
		"B1": "G24",
		"B2": "G22",
		"B3": "STONE",
		"FG2": "RING",
		"LB1": "G24",
	}
	FREE = {("B1", RM_WH): 45.0, ("B2", RM_WH): 12.0, ("B3", RM_WH): 9.0}

	def _positions(self, filters):
		receipts, replay = _settlement_fixture()
		with (
			patch(
				"frappe.db.get_value",
				side_effect=lambda dt, name, field: self.BATCH_ITEMS[name],
			),
			patch(
				"erpnext.stock.doctype.batch.batch.get_batch_qty",
				side_effect=lambda batch_no, warehouse: self.FREE.get(
					(batch_no, warehouse), 0.0
				),
			),
		):
			return self._execute(
				dict(view=report.VIEW_POSITION, **filters),
				["Stock User"],
				receipts,
				replay,
			)

	def test_warehouse_filter_and_receipt_equivalent(self):
		"""RU-19 -- in RM: R1 50 fine of 24KT = 50 g; R2 18.32 fine of 22KT = 18.32/0.916
		= 20 g; R3 2.5 ct = 2.5 ct. Free is min(batch qty free, holding): 45, 12, 2.5."""
		columns, rows = self._positions({"warehouse": RM_WH})
		self.assertEqual({r["warehouse"] for r in rows}, {RM_WH})
		got = {
			r["batch_no"]: (
				r["receipt"],
				r["receipt_share"],
				r["receipt_equivalent"],
				r["holding_qty"],
				r["free_qty"],
				r["stage"],
				r["measure"],
			)
			for r in rows
		}
		self.assertEqual(
			got,
			{
				"B1": ("SE-R1", 50.0, 50.0, 50.0, 45.0, "RM", "Fine g"),
				"B2": ("SE-R2", 18.32, 20.0, 20.0, 12.0, "RM", "Fine g"),
				"B3": ("SE-R3", 2.5, 2.5, 2.5, 2.5, "RM", "Carat"),
			},
		)
		b1 = next(r for r in rows if r["batch_no"] == "B1")
		self.assertEqual((b1["generation"], b1["produced_by"]), (0, None))

	def test_every_holding_without_a_filter(self):
		"""RU-19 -- seven holdings carry a share; the delivered FG1 is gone. Produced batches
		name their voucher and generation; the loss batch reads Loss / Scrap."""
		_columns, rows = self._positions({})
		got = {(r["batch_no"], r["warehouse"]): r for r in rows}
		self.assertEqual(
			set(got),
			{
				("B1", RM_WH),
				("B1", WIP_WH),
				("B2", RM_WH),
				("B2", TRANSIT_WH),
				("B3", RM_WH),
				("FG2", FG_WH),
				("LB1", LOSS_WH),
			},
		)
		self.assertEqual(got[("LB1", LOSS_WH)]["stage"], "Loss / Scrap")
		self.assertEqual(got[("B2", TRANSIT_WH)]["stage"], "Transit")
		fg2 = got[("FG2", FG_WH)]
		self.assertEqual((fg2["generation"], fg2["produced_by"]), (1, "SE-M2"))
		self.assertAlmostEqual(fg2["receipt_share"], 22.9, places=3)
		self.assertAlmostEqual(fg2["receipt_equivalent"], 25.0, places=3)


class TestMovements(_ReportUnitCase):
	"""RU-20 -- the trail respects from_date, warehouse and batch."""

	def _movements(self, filters):
		receipts, replay = _settlement_fixture()
		_columns, rows = self._execute(
			dict(view=report.VIEW_MOVEMENTS, **filters),
			["Stock User"],
			receipts,
			replay,
		)
		return [
			(r["voucher_no"], r["action"], r["batch_no"], round(r["receipt_share"], 3))
			for r in rows
		]

	def test_from_date_drops_earlier_steps(self):
		"""RU-20 -- from 2026-09-10: SE-M1, SE-M2, DN-1, SE-RET1, SE-L1, SE-L2 only."""
		self.assertEqual(
			self._movements({"from_date": "2026-09-10"}),
			[
				("SE-M1", "Consumed", "B1", 20.0),
				("SE-M1", "Produced", "FG1", 20.0),
				("SE-M2", "Consumed", "B2", 22.9),
				("SE-M2", "Produced", "FG2", 22.9),
				("DN-1", "Delivered", "FG1", 20.0),
				("SE-RET1", "Returned", "B1", 10.0),
				("SE-L1", "Consumed", "B1", 5.0),
				("SE-L1", "Produced", "LB1", 5.0),
				("SE-L2", "Consumed", "B1", 2.0),
			],
		)

	def test_warehouse_matches_either_end_of_a_step(self):
		"""RU-20 -- WIP: into it by SE-T1, out of it by SE-M1, SE-L1 and SE-L2."""
		self.assertEqual(
			self._movements({"warehouse": WIP_WH}),
			[
				("SE-T1", "Transfer", "B1", 40.0),
				("SE-M1", "Consumed", "B1", 20.0),
				("SE-L1", "Consumed", "B1", 5.0),
				("SE-L2", "Consumed", "B1", 2.0),
			],
		)

	def test_batch_filter(self):
		"""RU-20 -- B2: its receipt, the transit transfer (5 g = 4.58 fine), the consumption."""
		self.assertEqual(
			self._movements({"batch": "B2"}),
			[
				("SE-R2", "Receipt", "B2", 45.8),
				("SE-T2", "Transfer", "B2", 4.58),
				("SE-M2", "Consumed", "B2", 22.9),
			],
		)

	def test_the_trail_keeps_warehouses_and_voucher_quantities(self):
		"""RU-20."""
		receipts, replay = _settlement_fixture()
		_columns, rows = self._execute(
			{"view": report.VIEW_MOVEMENTS, "batch": "B2"},
			["Stock User"],
			receipts,
			replay,
		)
		transfer = next(r for r in rows if r["voucher_no"] == "SE-T2")
		self.assertEqual(
			(
				transfer["from_warehouse"],
				transfer["to_warehouse"],
				transfer["voucher_qty"],
				transfer["measure"],
			),
			(RM_WH, TRANSIT_WH, 5.0, "Fine g"),
		)


# ------------------------------------------------------------------------------------------
# Integration -- RPT-10
# ------------------------------------------------------------------------------------------


class TestTraceabilityReportWritesNothingIntegration(base._CustomerGoldIntegrationCase):
	"""INTEGRATION. RPT-10 -- opening every view writes nothing.

	Skips unless the site sets ``customer_gold_disposable_site`` (the base ``setUpClass`` guard):
	it submits a real receipt and a real raw return, then runs the report over them.
	"""

	WATCHED = (
		"Stock Entry",
		"Stock Ledger Entry",
		"Customer Gold Ledger Entry",
		"Customer Gold Allocation",
		"Journal Entry",
		"GL Entry",
	)

	_stocked_batch = base.TestFulfilmentLedger._stocked_batch
	_return = base.TestRawGoldReturn._return

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# as base.TestRawGoldReturn.setUpClass: Nominal valuation and the configured return type
		settings = frappe.get_doc(base.SETTINGS_DOCTYPE)
		settings.customer_gold_valuation_policy = "Nominal"
		settings.customer_gold_return_stock_entry_type = base.RETURN_SE_TYPE
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype=base.SETTINGS_DOCTYPE)

	def _counts(self):
		return {doctype: frappe.db.count(doctype) for doctype in self.WATCHED}

	def test_every_view_reads_without_writing(self):
		"""RPT-10 -- counts of every written doctype are unchanged and nothing commits."""
		batch = self._stocked_batch(qty=10)
		return_name = self._return(batch, 2)
		receipt = frappe.db.get_value(
			"Customer Gold Ledger Entry",
			{"batch_no": batch, "cg_event_kind": "Receipt"},
			"reference_docname",
		)
		self.assertTrue(receipt, "fixture wrote no Receipt event")

		base_filters = {"company": base.COMPANY, "customer": base.CUSTOMER}
		views = [
			report.VIEW_SETTLEMENT,
			report.VIEW_POSITION,
			report.VIEW_MOVEMENTS,
		]
		manufacture = frappe.db.get_value(
			"Stock Entry",
			{"company": base.COMPANY, "purpose": "Manufacture", "docstatus": 1},
			"name",
		)

		before = self._counts()
		results = {}
		with patch(
			"frappe.db.commit", side_effect=AssertionError("the report committed")
		):
			for view in views:
				results[view] = report.execute(dict(base_filters, view=view))
			if manufacture:
				results[report.VIEW_FG] = report.execute(
					dict(base_filters, view=report.VIEW_FG, stock_entry=manufacture)
				)
		self.assertEqual(self._counts(), before)

		# and the settlement it read is the one the fixture made: 2 of 10 g returned raw,
		# at the booked 7,164.83/g -> 14,329.66 released of 71,648.30.
		rows = [
			r for r in results[report.VIEW_SETTLEMENT][1] if r["receipt"] == receipt
		]
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertAlmostEqual(row["returned"] / row["received"], 0.2, places=3)
		self.assertAlmostEqual(row["delivered"], 0.0, places=3)
		self.assertAlmostEqual(row["nominal_amount"], 71648.30, places=2)
		self.assertAlmostEqual(row["released_raw"], 14329.66, places=2)
		self.assertAlmostEqual(row["pending_amount"], 57318.64, places=2)
		self.assertIn(return_name, row["settlement_vouchers"])
		self.assertEqual(row["basis"], "Allocated")

		movements = [
			r for r in results[report.VIEW_MOVEMENTS][1] if r["receipt"] == receipt
		]
		self.assertIn(
			(return_name, "Returned"),
			{(r["voucher_no"], r["action"]) for r in movements},
		)
