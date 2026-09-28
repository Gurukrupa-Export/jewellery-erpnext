# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Customer Gold Allocation on real documents: which receipt each settlement released.

Integration only, NOT in CI. It runs on the disposable ``cg-integration.test`` site through the
base suite's guard (``customer_gold_disposable_site`` in site_config), under Nominal.

Every expected amount below is computed by hand in this file from the receipt rows' own
``basic_rate`` x the grams drawn, with ``Decimal`` arithmetic rounded half-up to the paisa --
never by calling the allocation writer or the report under test.

Rollback is CLASS-scoped, so every assertion is scoped to the documents the test itself created,
or is a before/after delta.

Scenario IDs: ACC-01, ACC-02, ACC-04, ACC-06, ACC-12, ACC-13, ACC-14, REG-01, REG-07, VAL-01,
REQ-09, RPT-10.
"""

from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	SETTINGS_DOCTYPE,
)
from jewellery_erpnext.jewellery_erpnext.tests import (
	test_customer_gold_integration as base,
)

ALLOCATION = "Customer Gold Allocation"
LEDGER = "Customer Gold Ledger Entry"
REPORT_MODULE = "jewellery_erpnext.customer_subcontracting.report.customer_gold_traceability.customer_gold_traceability"

#: Three raw quotes per 10 g, one per receipt, so each receipt books its own per-gram rate:
#: 7,164.83 / 7,400.00 / 6,925.00. All inside the rate check's 0.5x-2x band.
RAW_RATES = (Decimal("71648.30"), Decimal("74000.00"), Decimal("69250.00"))
#: Grams of R1, R2 and R3 that go into the one finished piece.
DRAWS = (Decimal("4"), Decimal("3"), Decimal("1"))
RECEIVED = Decimal("10")
PRODUCTION_COST = Decimal("250.00")
PAISA = Decimal("0.01")
#: The report measures a gold receipt in FINE grams ("Measured In: Fine g"). The fixture item is
#: the 99.9% variant, so a gram received or drawn is 0.999 fine g on that report.
FINE_PER_GRAM = Decimal("0.999")


def D(value):
	return Decimal(str(value))


def money(value):
	return D(value).quantize(PAISA, rounding=ROUND_HALF_UP)


def per_gram(raw_per_10g):
	return (raw_per_10g / Decimal("10")).quantize(PAISA, rounding=ROUND_HALF_UP)


class TestCustomerGoldAllocationIntegration(base._CustomerGoldIntegrationCase):
	"""Receipt-wise settlement: Customer Gold Allocation rows, their JEs and the report."""

	PIECE = "CG-TEST-PIECE-NOS"
	PIECE_HSN = "71131910"
	ALLOY = "CG-TEST-ALLOY"
	COMPANY_RECEIPT_TYPE = "CG Test Company Receipt"

	# -- helpers borrowed from the base suite (functions, never the Test* classes) ---------------
	_ensure_piece = base.TestNosPieceSettlesItsCustomerGold.__dict__["_ensure_piece"]
	_deliver_piece = base.TestNosPieceSettlesItsCustomerGold._deliver_piece
	_sales_order = base.TestFulfilmentLedger._sales_order
	_delivery = base.TestFulfilmentLedger._delivery
	_events = base.TestFulfilmentLedger._events
	_settlement_entries = base.TestManufacturedPieceSettles._settlement_entries
	_company_stock = base.TestKLHGX62F1119Pattern._company_stock
	_gl_balance = base.TestKLHGX62F1119Pattern._gl_balance
	_return_of = base.TestFulfilmentReturns._return_of
	_return = base.TestRawGoldReturn._return

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Nominal + the configured raw-return type, exactly as TestRawGoldReturn sets them.
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.customer_gold_valuation_policy = "Nominal"
		settings.customer_gold_return_stock_entry_type = base.RETURN_SE_TYPE
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype=SETTINGS_DOCTYPE)
		cls._ensure_piece()
		# The company-stock masters TestKLHGX62F1119Pattern builds: a plain Material Receipt
		# type and a batch-tracked company alloy that is NOT flagged as customer goods.
		if not frappe.db.exists("Stock Entry Type", cls.COMPANY_RECEIPT_TYPE):
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = cls.COMPANY_RECEIPT_TYPE
			doc.purpose = "Material Receipt"
			doc.insert(ignore_permissions=True)
		if not frappe.db.exists("Item", cls.ALLOY):
			item = frappe.new_doc("Item")
			item.item_code = cls.ALLOY
			item.item_name = "CG Test Alloy"
			item.item_group = cls.item_group
			item.stock_uom = "Gram"
			item.is_stock_item = 1
			item.has_batch_no = 1
			item.create_new_batch = 1
			item.gst_hsn_code = "74031900"
			item.insert(ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")

	# -- fixtures ----------------------------------------------------------------------------
	def _receive_at(self, raw_rate, qty=RECEIVED):
		"""A customer-gold receipt booked while today's quote is ``raw_rate`` per 10 g.

		Backdating a receipt is refused at submit, so the rate is varied by rewriting today's
		Gold Rates row around the receipt and restoring the suite's own quote afterwards.
		"""
		self._ensure_gold_rate(self.posting_date, float(raw_rate))
		try:
			se = self._receipt(qty=float(qty))
			self._submit(se)
		finally:
			self._ensure_gold_rate(self.posting_date, base.RAW_RATE)

		row = frappe.db.get_value(
			"Stock Entry Detail",
			se.items[0].name,
			["name", "batch_no", "qty", "basic_rate", "basic_amount"],
			as_dict=True,
		)
		event = frappe.db.get_value(
			LEDGER,
			{"reference_docname": se.name, "cg_event_kind": "Receipt"},
			["name", "cg_carrying_value_delta"],
			as_dict=True,
		)
		self.assertTrue(event, f"receipt {se.name} wrote no Receipt event")
		rate = money(row.basic_rate)
		# Fixture guard: the receipt really booked the quote it was given, per gram.
		self.assertEqual(rate, per_gram(raw_rate), f"{se.name} booked {row.basic_rate}")
		return frappe._dict(
			se=se.name,
			row=row.name,
			batch=row.batch_no,
			rate=rate,
			qty=D(row.qty),
			nominal=money(D(row.qty) * rate),
			event=event.name,
		)

	def _three_receipts(self):
		receipts = [self._receive_at(raw) for raw in RAW_RATES]
		self.assertEqual(
			len({r.rate for r in receipts}),
			3,
			"the three receipts must book three different rates",
		)
		return receipts

	def _make_piece_from(self, draws, production_cost=Decimal("0")):
		"""One Nos piece from several customer batches -- a real Manufacture entry."""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			_finished_goods_ownership,
		)

		consumed = [
			{
				"item_code": self.item,
				"qty": float(qty),
				"s_warehouse": self.warehouse,
				"batch_no": batch,
				"use_serial_batch_fields": 1,
				"inventory_type": "Customer Goods",
				"customer": base.CUSTOMER,
				"allow_zero_valuation_rate": 1,
				"expense_account": self.difference_account,
			}
			for batch, qty in draws
		]
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = base.MANUFACTURE_SE_TYPE
		se.purpose = "Manufacture"
		se.company = base.COMPANY
		se.manufacturer = base.MANUFACTURER
		se.posting_date = self.posting_date
		se.set_posting_time = 1
		for row in consumed:
			se.append("items", row)
		fg_inventory_type, fg_customer = _finished_goods_ownership(consumed)
		se.append(
			"items",
			{
				"item_code": self.PIECE,
				"qty": 1,
				"t_warehouse": self.warehouse,
				"uom": "Nos",
				"stock_uom": "Nos",
				"conversion_factor": 1,
				"is_finished_item": 1,
				"inventory_type": fg_inventory_type,
				"customer": fg_customer,
				"allow_zero_valuation_rate": 1,
				"expense_account": self.difference_account,
			},
		)
		if production_cost:
			se.append(
				"additional_costs",
				{
					"expense_account": self._ensure_account(
						"CG Test Manufacturing Cost", "Expense"
					),
					"description": "Production cost",
					"amount": float(production_cost),
				},
			)
		se.flags.ignore_mandatory = True
		se.save()
		se.submit()
		return se

	def _piece_chain(self, production_cost=Decimal("0")):
		"""R1/R2/R3 at three rates -> one piece of 4 + 3 + 1 g -> its finished batch."""
		receipts = self._three_receipts()
		se = self._make_piece_from(
			[(r.batch, qty) for r, qty in zip(receipts, DRAWS, strict=True)],
			production_cost=production_cost,
		)
		fg = [r for r in se.items if r.get("is_finished_item")][0]
		self.assertTrue(fg.batch_no, "the Manufacture minted no finished batch")
		self.assertEqual(
			frappe.db.get_value(
				"Batch", fg.batch_no, ["custom_inventory_type", "custom_customer"]
			),
			("Customer Goods", base.CUSTOMER),
			"the finished piece is not owned by the customer whose gold made it",
		)
		return frappe._dict(receipts=receipts, manufacture=se, fg_batch=fg.batch_no)

	def _deliver_item(self, item_code, batch_no, qty, uom):
		"""Sales Order + Delivery Note for any item -- the base helpers hardcode theirs."""
		if not frappe.db.exists("Sales Type", base.SALES_TYPE):
			frappe.get_doc(
				{"doctype": "Sales Type", "type": base.SALES_TYPE, "tax_rate": 0}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer Payment Terms", {"customer": base.CUSTOMER}):
			frappe.get_doc(
				{"doctype": "Customer Payment Terms", "customer": base.CUSTOMER}
			).insert(ignore_permissions=True)
		line = {
			"item_code": item_code,
			"qty": qty,
			"rate": 0,
			"warehouse": self.warehouse,
			"uom": uom,
			"stock_uom": uom,
			"conversion_factor": 1,
		}
		so = frappe.new_doc("Sales Order")
		so.company = base.COMPANY
		so.customer = base.CUSTOMER
		so.sales_type = base.SALES_TYPE
		so.transaction_date = self.posting_date
		so.delivery_date = self.posting_date
		so.append("items", {**line, "delivery_date": self.posting_date})
		so.flags.ignore_mandatory = True
		so.save()
		so.submit()

		dn = frappe.new_doc("Delivery Note")
		dn.company = base.COMPANY
		dn.customer = base.CUSTOMER
		dn.posting_date = self.posting_date
		dn.set_posting_time = 1
		dn.append(
			"items",
			{
				**line,
				"batch_no": batch_no,
				"use_serial_batch_fields": 1,
				"against_sales_order": so.name,
				"so_detail": so.items[0].name,
			},
		)
		dn.flags.ignore_mandatory = True
		dn.save()
		dn.submit()
		return dn

	def _deliver_raw(self, batch, qty):
		dn = self._delivery(batch, qty=float(qty))
		dn.save()
		dn.submit()
		return dn

	# -- readers -----------------------------------------------------------------------------
	def _allocations(self, voucher, disposition=None):
		filters = {"reference_docname": voucher}
		if disposition:
			filters["disposition"] = disposition
		return frappe.get_all(
			ALLOCATION,
			filters=filters,
			fields=[
				"name",
				"disposition",
				"basis",
				"cg_event",
				"receipt_event",
				"receipt_voucher",
				"receipt_row",
				"source_batch",
				"gross_qty",
				"amount",
				"settlement_voucher",
				"reversal_of",
			],
			order_by="creation",
		)

	def _by_receipt(self, allocations):
		result = {}
		for a in allocations:
			self.assertNotIn(
				a.receipt_event,
				result,
				f"two allocations for one receipt: {allocations}",
			)
			result[a.receipt_event] = a
		return result

	def _net_for_receipt(self, receipt_event):
		rows = frappe.get_all(
			ALLOCATION,
			filters={"receipt_event": receipt_event},
			fields=["gross_qty", "amount"],
		)
		return (
			sum((D(flt(r.gross_qty, 6)) for r in rows), Decimal("0")),
			sum((money(r.amount) for r in rows), Decimal("0")),
		)

	def _liability_debit(self, je_name):
		je = frappe.get_doc("Journal Entry", je_name)
		return sum(
			(
				money(r.debit_in_account_currency) - money(r.credit_in_account_currency)
				for r in je.accounts
				if r.account == self.liability_account
			),
			Decimal("0"),
		)

	def _report(self, filters):
		frappe.set_user("Administrator")
		execute = frappe.get_attr(f"{REPORT_MODULE}.execute")
		return execute({"company": base.COMPANY, **filters})

	def _settlement_row(self, receipt):
		result = self._report(
			{
				"customer": base.CUSTOMER,
				"receipt": receipt.se,
				"view": "Receipt Settlement",
			}
		)
		rows = [r for r in result[1] if r.get("receipt") == receipt.se]
		self.assertEqual(len(rows), 1, f"report rows for {receipt.se}: {rows}")
		return frappe._dict(rows[0])

	def _counts(self):
		return {
			doctype: frappe.db.count(doctype)
			for doctype in (
				"Stock Entry",
				LEDGER,
				ALLOCATION,
				"Journal Entry",
				"GL Entry",
			)
		}

	# -- ACC-02 ------------------------------------------------------------------------------
	def test_acc02_one_piece_from_three_receipts_settles_each_at_its_own_rate(self):
		"""ACC-02 (F4-like): R1/R2/R3 booked at three rates, 4 + 3 + 1 g in one piece.

		One settlement JE; one FG Delivery allocation per receipt with gross 4/3/1 and amount
		= grams x that receipt's own rate; the allocations sum to the JE's liability debit to
		the paisa and carry the JE; the report shows the same released amounts per receipt and
		pending = nominal - released.
		"""
		chain = self._piece_chain()
		dn = self._deliver_piece(chain.fg_batch)

		entries = self._settlement_entries(dn.name)
		self.assertEqual(len(entries), 1, f"expected one settlement JE, got {entries}")
		je = entries[0]

		allocations = self._allocations(dn.name)
		self.assertEqual(len(allocations), 3, allocations)
		by_receipt = self._by_receipt(allocations)

		expected_total = Decimal("0")
		for receipt, drawn in zip(chain.receipts, DRAWS, strict=True):
			expected = money(drawn * receipt.rate)
			expected_total += expected
			a = by_receipt.get(receipt.event)
			self.assertIsNotNone(a, f"no allocation names receipt {receipt.se}")
			self.assertEqual(a.disposition, "FG Delivery")
			self.assertEqual(a.basis, "Component Share")
			self.assertEqual(a.receipt_voucher, receipt.se)
			self.assertEqual(a.receipt_row, receipt.row)
			self.assertEqual(a.source_batch, receipt.batch)
			self.assertAlmostEqual(flt(a.gross_qty), float(drawn), places=6)
			self.assertEqual(
				money(a.amount),
				expected,
				f"{receipt.se}: {drawn} g x {receipt.rate} should release {expected}",
			)
			self.assertEqual(a.settlement_voucher, je)

		# 4 x 7,164.83 + 3 x 7,400.00 + 1 x 6,925.00
		self.assertEqual(expected_total, Decimal("57784.32"))
		self.assertEqual(
			sum((money(a.amount) for a in allocations), Decimal("0")), expected_total
		)
		self.assertEqual(
			self._liability_debit(je),
			expected_total,
			"the JE's liability debit is not the sum of its receipt allocations",
		)

		for receipt, drawn in zip(chain.receipts, DRAWS, strict=True):
			row = self._settlement_row(receipt)
			released = money(drawn * receipt.rate)
			self.assertEqual(money(row.nominal_amount), receipt.nominal)
			self.assertEqual(money(row.released_fg), released)
			self.assertEqual(money(row.released_raw), Decimal("0.00"))
			self.assertEqual(money(row.pending_amount), receipt.nominal - released)
			self.assertIn(je, row.settlement_vouchers or "")

	# -- ACC-04 ------------------------------------------------------------------------------
	def test_acc04_two_deliveries_of_one_raw_receipt_never_exceed_it(self):
		"""ACC-04: 10 g received raw, delivered 4 g then 6 g on two Delivery Notes.

		Each DN writes one Direct Batch allocation against the receipt, 4 g / 6 g at the booked
		rate; together they release exactly the receipt, never more. The ledger events' own
		values are checked too, since an over-release there would be a separate defect.
		"""
		receipt = self._receive_at(RAW_RATES[0])
		first = self._deliver_raw(receipt.batch, 4)
		second = self._deliver_raw(receipt.batch, 6)

		for dn, drawn in ((first, Decimal("4")), (second, Decimal("6"))):
			expected = money(drawn * receipt.rate)
			(a,) = self._allocations(dn.name)
			self.assertEqual(a.disposition, "FG Delivery")
			self.assertEqual(a.basis, "Direct Batch")
			self.assertEqual(a.receipt_event, receipt.event)
			self.assertAlmostEqual(flt(a.gross_qty), float(drawn), places=6)
			self.assertEqual(money(a.amount), expected)

			(event,) = self._events(dn.name, "Delivery")
			self.assertEqual(
				money(event.cg_carrying_value_delta),
				-expected,
				f"the ledger event of {dn.name} does not carry {drawn} g x {receipt.rate}",
			)
			(je,) = self._settlement_entries(dn.name)
			self.assertEqual(self._liability_debit(je), expected)

		gross, amount = self._net_for_receipt(receipt.event)
		self.assertEqual(gross, RECEIVED)
		self.assertEqual(amount, receipt.nominal)
		self.assertLessEqual(amount, receipt.nominal)

		row = self._settlement_row(receipt)
		self.assertEqual(money(row.released_fg), receipt.nominal)
		self.assertEqual(money(row.pending_amount), Decimal("0.00"))
		self.assertEqual(row.measure, "Fine g")
		self.assertAlmostEqual(
			flt(row.received), float(RECEIVED * FINE_PER_GRAM), places=3
		)
		self.assertAlmostEqual(
			flt(row.delivered), float(RECEIVED * FINE_PER_GRAM), places=3
		)
		self.assertAlmostEqual(flt(row.owed_back), 0.0, places=3)

	# -- ACC-06 ------------------------------------------------------------------------------
	def test_acc06_invoice_from_the_delivery_writes_no_event_allocation_or_je(self):
		"""ACC-06: a Sales Invoice made from the DN moves no metal and settles nothing."""
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		receipt = self._receive_at(RAW_RATES[0])
		dn = self._deliver_raw(receipt.batch, 10)
		before = {
			doctype: frappe.db.count(doctype)
			for doctype in (LEDGER, ALLOCATION, "Journal Entry")
		}

		si = make_sales_invoice(dn.name)
		si.flags.ignore_mandatory = True
		si.insert(ignore_permissions=True)
		si.submit()

		after = {doctype: frappe.db.count(doctype) for doctype in before}
		self.assertEqual(after, before)
		self.assertEqual(self._allocations(si.name), [])
		self.assertEqual(self._events(si.name), [])

	# -- ACC-13 ------------------------------------------------------------------------------
	def test_acc13_cancelling_the_delivery_reverses_every_allocation(self):
		"""ACC-13: cancel the piece's DN -> JE cancelled, one negated Reversal per allocation,
		every receipt nets to zero, and the report's pending is back to the nominal."""
		chain = self._piece_chain()
		dn = self._deliver_piece(chain.fg_batch)
		(je,) = self._settlement_entries(dn.name)
		originals = self._allocations(dn.name, "FG Delivery")
		self.assertEqual(len(originals), 3)

		dn.reload()
		dn.cancel()

		self.assertEqual(frappe.db.get_value("Journal Entry", je, "docstatus"), 2)
		reversals = self._allocations(dn.name, "Reversal")
		self.assertEqual(len(reversals), len(originals), reversals)
		by_original = {r.reversal_of: r for r in reversals}
		self.assertEqual(set(by_original), {o.name for o in originals})
		for original in originals:
			reversal = by_original[original.name]
			self.assertEqual(reversal.receipt_event, original.receipt_event)
			self.assertAlmostEqual(
				flt(reversal.gross_qty), -flt(original.gross_qty), places=6
			)
			self.assertEqual(money(reversal.amount), -money(original.amount))

		for receipt in chain.receipts:
			gross, amount = self._net_for_receipt(receipt.event)
			self.assertEqual(gross, Decimal("0"), receipt.se)
			self.assertEqual(amount, Decimal("0.00"), receipt.se)
			row = self._settlement_row(receipt)
			self.assertEqual(money(row.released_fg), Decimal("0.00"))
			self.assertEqual(money(row.pending_amount), receipt.nominal)

	# -- ACC-14 ------------------------------------------------------------------------------
	def test_acc14_sales_return_mirrors_the_original_receipts(self):
		"""ACC-14: a sales return of the piece restores the SAME receipt events with negated
		gross and amount (proportions identical), its JE reverses the liability, net 0."""
		chain = self._piece_chain()
		liability_before = D(self._gl_balance(self.liability_account))
		dn = self._deliver_piece(chain.fg_batch)
		originals = self._by_receipt(self._allocations(dn.name, "FG Delivery"))
		released = sum((money(a.amount) for a in originals.values()), Decimal("0"))

		ret = self._return_of(dn, 1)

		returned = self._by_receipt(self._allocations(ret.name, "Delivery Return"))
		self.assertEqual(set(returned), set(originals))
		self.assertEqual(set(returned), {r.event for r in chain.receipts})
		for receipt_event, original in originals.items():
			restored = returned[receipt_event]
			self.assertAlmostEqual(
				flt(restored.gross_qty), -flt(original.gross_qty), places=6
			)
			self.assertEqual(money(restored.amount), -money(original.amount))

		(ret_je,) = self._settlement_entries(ret.name)
		self.assertEqual(self._liability_debit(ret_je), -released)
		self.assertEqual(
			D(self._gl_balance(self.liability_account)),
			liability_before,
			"delivery + return did not leave the liability where it was",
		)
		for receipt in chain.receipts:
			gross, amount = self._net_for_receipt(receipt.event)
			self.assertEqual(gross, Decimal("0"), receipt.se)
			self.assertEqual(amount, Decimal("0.00"), receipt.se)

	# -- ACC-01 ------------------------------------------------------------------------------
	def test_acc01_partly_delivered_partly_returned_raw_closes_once(self):
		"""ACC-01: one receipt, 4 g delivered raw by DN and 6 g handed back raw -> the
		receipt is closed once: nothing owed back, both released columns filled, nothing
		pending."""
		receipt = self._receive_at(RAW_RATES[1])
		dn = self._deliver_raw(receipt.batch, 4)
		returned = self._return(receipt.batch, 6)

		(raw,) = self._allocations(returned, "Raw Return")
		self.assertEqual(raw.basis, "Receipt Link")
		self.assertEqual(raw.receipt_event, receipt.event)
		self.assertAlmostEqual(flt(raw.gross_qty), 6.0, places=6)
		self.assertEqual(money(raw.amount), money(Decimal("6") * receipt.rate))
		(fg,) = self._allocations(dn.name, "FG Delivery")
		self.assertEqual(money(fg.amount), money(Decimal("4") * receipt.rate))

		row = self._settlement_row(receipt)
		self.assertEqual(row.measure, "Fine g")
		self.assertAlmostEqual(flt(row.owed_back), 0.0, places=3)
		self.assertAlmostEqual(
			flt(row.delivered), float(Decimal("4") * FINE_PER_GRAM), places=3
		)
		self.assertAlmostEqual(
			flt(row.returned), float(Decimal("6") * FINE_PER_GRAM), places=3
		)
		self.assertEqual(money(row.released_fg), money(Decimal("4") * receipt.rate))
		self.assertEqual(money(row.released_raw), money(Decimal("6") * receipt.rate))
		self.assertLessEqual(abs(flt(row.pending_amount)), 0.01)

	# -- ACC-12 ------------------------------------------------------------------------------
	def test_acc12_replaying_record_fulfilment_writes_nothing_new(self):
		"""ACC-12: record_fulfilment on the already-submitted DN again -> no duplicate events,
		allocations or JE."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		chain = self._piece_chain()
		dn = self._deliver_piece(chain.fg_batch)
		events = sorted(e.name for e in self._events(dn.name))
		allocations = sorted(a.name for a in self._allocations(dn.name))
		entries = self._settlement_entries(dn.name)
		jes = frappe.db.count("Journal Entry")
		self.assertEqual(len(allocations), 3)

		cgf.record_fulfilment(frappe.get_doc("Delivery Note", dn.name))

		self.assertEqual(sorted(e.name for e in self._events(dn.name)), events)
		self.assertEqual(
			sorted(a.name for a in self._allocations(dn.name)), allocations
		)
		self.assertEqual(self._settlement_entries(dn.name), entries)
		self.assertEqual(frappe.db.count("Journal Entry"), jes)

	# -- REG-07 / REG-01 ---------------------------------------------------------------------
	def test_reg07_company_stock_delivery_writes_nothing(self):
		"""REG-07: a DN of the company's own batch -> no ledger event, allocation or JE."""
		batch = self._company_stock(self.ALLOY, 10, 20.0, "Gram")
		self.assertFalse(frappe.db.get_value("Batch", batch, "custom_customer"))
		jes = frappe.db.count("Journal Entry")
		allocations = frappe.db.count(ALLOCATION)

		dn = self._deliver_item(self.ALLOY, batch, 5, "Gram")

		self.assertEqual(self._events(dn.name), [])
		self.assertEqual(self._allocations(dn.name), [])
		self.assertEqual(frappe.db.count(ALLOCATION), allocations)
		self.assertEqual(frappe.db.count("Journal Entry"), jes)

	def test_reg01_company_only_manufacture_allocates_nothing(self):
		"""REG-01: a Manufacture of company alloy only -> no custody event, no allocation."""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			_finished_goods_ownership,
		)

		batch = self._company_stock(self.ALLOY, 10, 20.0, "Gram")
		allocations = frappe.db.count(ALLOCATION)
		consumed = [
			{
				"item_code": self.ALLOY,
				"qty": 5,
				"s_warehouse": self.warehouse,
				"batch_no": batch,
				"use_serial_batch_fields": 1,
				"inventory_type": "Regular Stock",
				"expense_account": self.difference_account,
			}
		]
		inventory_type, customer = _finished_goods_ownership(consumed)
		self.assertEqual((inventory_type, customer), ("Regular Stock", None))
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = base.MANUFACTURE_SE_TYPE
		se.purpose = "Manufacture"
		se.company = base.COMPANY
		se.manufacturer = base.MANUFACTURER
		se.posting_date = self.posting_date
		se.set_posting_time = 1
		se.append("items", consumed[0])
		se.append(
			"items",
			{
				"item_code": self.PIECE,
				"qty": 1,
				"t_warehouse": self.warehouse,
				"uom": "Nos",
				"stock_uom": "Nos",
				"conversion_factor": 1,
				"is_finished_item": 1,
				"inventory_type": inventory_type,
				"expense_account": self.difference_account,
			},
		)
		se.flags.ignore_mandatory = True
		se.save()
		se.submit()

		self.assertEqual(self._events(se.name), [])
		self.assertEqual(self._allocations(se.name), [])
		self.assertEqual(frappe.db.count(ALLOCATION), allocations)

	# -- VAL-01 ------------------------------------------------------------------------------
	def test_val01_fg_valuation_bridge_balances_for_the_manufacture(self):
		"""VAL-01: the FG Valuation view of the real Manufacture entry -- Difference is 0 and
		Posted FG value = consumed (the rows' basic_amount) + the additional cost."""
		chain = self._piece_chain(production_cost=PRODUCTION_COST)
		name = chain.manufacture.name

		consumed_rows = frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": name, "s_warehouse": ["is", "set"]},
			fields=["batch_no", "qty", "basic_amount"],
		)
		self.assertEqual(len(consumed_rows), 3)
		consumed = sum((money(r.basic_amount) for r in consumed_rows), Decimal("0"))
		# The consumed rows are valued at each receipt's own booked rate (batch-wise).
		self.assertEqual(
			consumed,
			sum(
				(money(q * r.rate) for r, q in zip(chain.receipts, DRAWS, strict=True)),
				Decimal("0"),
			),
		)

		_columns, rows = self._report(
			{"view": "FG Valuation", "stock_entry": name, "customer": base.CUSTOMER}
		)
		bridge = {r["note"]: r for r in rows if r.get("side") == "Bridge"}
		difference = bridge.get("Difference")
		posted = bridge.get("Posted FG value")
		self.assertIsNotNone(difference, f"no Difference row in {list(bridge)}")
		self.assertIsNotNone(posted, f"no Posted FG value row in {list(bridge)}")
		self.assertLessEqual(abs(flt(difference["valuation_amount"])), 0.01)
		self.assertLessEqual(
			abs(D(flt(posted["valuation_amount"], 2)) - (consumed + PRODUCTION_COST)),
			PAISA,
		)
		self.assertEqual(
			money(bridge["Additional costs"]["valuation_amount"]), PRODUCTION_COST
		)

	# -- REQ-09 ------------------------------------------------------------------------------
	def test_req09_manufactured_piece_batch_carries_no_batch_rate(self):
		"""REQ-09 probe: F8 intends a manufactured piece's Batch Rate (custom_metal_rate) to be
		0 -- its row rate is the whole piece, not metal. Asserts that intent on this site."""
		chain = self._piece_chain()
		fg_rate = flt(frappe.db.get_value("Batch", chain.fg_batch, "custom_metal_rate"))
		fg_row_rate = flt(
			frappe.db.get_value(
				"Stock Entry Detail",
				{"parent": chain.manufacture.name, "is_finished_item": 1},
				"valuation_rate",
			)
		)
		self.assertEqual(
			fg_rate,
			0.0,
			f"finished batch {chain.fg_batch} carries Batch Rate {fg_rate} "
			f"(the FG row's valuation_rate is {fg_row_rate}); F8 intends 0",
		)

	# -- RPT-10 ------------------------------------------------------------------------------
	def test_rpt10_every_report_view_is_read_only(self):
		"""RPT-10: all four views run against real documents and write nothing."""
		chain = self._piece_chain()
		dn = self._deliver_piece(chain.fg_batch)
		self.assertTrue(self._allocations(dn.name))
		receipt = chain.receipts[0]

		before = self._counts()
		settlement = self._report(
			{
				"customer": base.CUSTOMER,
				"receipt": receipt.se,
				"view": "Receipt Settlement",
			}
		)
		position = self._report(
			{
				"customer": base.CUSTOMER,
				"receipt": receipt.se,
				"view": "Material Position",
			}
		)
		movements = self._report(
			{"customer": base.CUSTOMER, "receipt": receipt.se, "view": "Movements"}
		)
		fg = self._report(
			{"view": "FG Valuation", "stock_entry": chain.manufacture.name}
		)
		self.assertEqual(self._counts(), before)

		self.assertTrue(settlement[1], "Receipt Settlement returned no rows")
		self.assertIsInstance(position[1], list)
		self.assertTrue(movements[1], "Movements returned no rows for a traced receipt")
		self.assertTrue(fg[1], "FG Valuation returned no rows")
