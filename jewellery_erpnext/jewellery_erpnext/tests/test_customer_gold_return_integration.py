# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Receipt-linked raw return of customer gold -- real documents on a disposable site.

Integration evidence for the receipt-linked return (``customer_gold_return``): the desk path
(Customer Goods Received -> Create -> Issue, ``make_stock_in_entry``), the API path
(``make_customer_gold_return``), the entitlement and physical-stock guards, cancel / amend,
descendant batches (D08), the Settle conversion lane, the read-only return preview and a
best-effort concurrency check.

NOT FOR CI. It needs ``customer_gold_disposable_site: 1`` (the base ``setUpClass`` skips
otherwise) and it submits real Stock Entries, Delivery Notes and Journal Entries.

Rollback is CLASS-scoped, so every assertion is on a delta or on rows scoped to a document the
test itself created. ``TestConcurrentReturns`` is the only class that commits; it cleans up by
cancelling what it created and restoring the Subcontracting Settings it changed.

ORACLES. Every money figure is hand-computed from the fixture's own rate: the base suite's
``RAW_RATE`` is Rs.71,648.30 per 10 g, so a receipt row is booked at Rs.7,164.83 per gram, which
each test first verifies on the receipt row's own ``basic_rate`` before relying on it. Nothing
here calls ``get_booked_rate`` to obtain an expected value.
"""

import threading
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.tests import (
	test_customer_gold_integration as base,
)

COMPANY = base.COMPANY
CUSTOMER = base.CUSTOMER
RETURN_SE_TYPE = base.RETURN_SE_TYPE
REPACK_SE_TYPE = base.REPACK_SE_TYPE
SETTINGS_DOCTYPE = base.SETTINGS_DOCTYPE

LEDGER = "Customer Gold Ledger Entry"
ALLOCATION = "Customer Gold Allocation"
LANE = f"Customer Goods|{CUSTOMER}"

#: Rs.71,648.30 per 10 g -> Rs.7,164.83 per gram. Stated as a literal, then checked against
#: every receipt row the tests book, so a fixture drift fails loudly instead of silently
#: re-basing every expected amount.
BOOKED = Decimal("71648.30") / Decimal("10")
assert BOOKED == Decimal("7164.83")

#: The Settle helper's own Stock Entry Type (``cg_settle.REPACK_SE_TYPE``). Absent on a fresh
#: disposable site; created inside the class transaction.
SETTLE_REPACK_TYPE = "Repack-Metal Conversion"

#: Tables whose row counts prove a refused or read-only call wrote nothing.
COUNTED = (
	"Stock Entry",
	LEDGER,
	ALLOCATION,
	"Journal Entry",
	"GL Entry",
	"Material Request",
)


def money(qty, rate=BOOKED):
	"""Exact rupee value of ``qty`` grams at ``rate``, rounded half-up to the paisa."""
	return float(
		(Decimal(str(qty)) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
	)


def exact(qty, rate=BOOKED):
	"""Unrounded value, for comparisons that must tolerate one rounding step."""
	return float(Decimal(str(qty)) * rate)


class _ReturnCase(base._CustomerGoldIntegrationCase):
	"""Nominal policy + the configured return type, exactly as ``TestRawGoldReturn`` sets them,
	plus the helpers the return scenarios share. No test methods of its own."""

	# Borrowed helpers. Assigned through attribute access so no base Test* class name is ever
	# bound in this module's namespace (unittest would collect and re-run its tests).
	_stocked_batch = base.TestFulfilmentLedger._stocked_batch
	_sales_order = base.TestFulfilmentLedger._sales_order
	_delivery = base.TestFulfilmentLedger._delivery
	_transfer = base.TestCustodyTransfer._transfer

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.customer_gold_valuation_policy = "Nominal"
		settings.customer_gold_return_stock_entry_type = RETURN_SE_TYPE
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype=SETTINGS_DOCTYPE)

		# Same second warehouse TestCustodyTransfer uses, for the physical-shortfall cases.
		cls.wip_warehouse = cls._ensure_warehouse("CG Test Floor")
		frappe.db.set_value(
			"Warehouse",
			cls.wip_warehouse,
			"department",
			cls._ensure_department("CG Test Dept"),
		)

	# ------------------------------------------------------------------ fixtures
	def _booked_receipt(self, qty):
		"""Submit a receipt of ``qty`` g and verify the booked rate on the row itself."""
		se = self._receipt(qty=qty)
		self._submit(se)
		row = se.items[0]
		self.assertAlmostEqual(
			flt(row.basic_rate),
			float(BOOKED),
			places=2,
			msg="fixture drift: the receipt row is not booked at Rs.7,164.83/g",
		)
		self.assertAlmostEqual(flt(row.basic_amount), money(qty), places=2)
		return se

	def _receipt_event(self, receipt):
		events = frappe.get_all(
			LEDGER,
			filters={
				"reference_docname": receipt.name,
				"cg_source_row": receipt.items[0].name,
				"cg_event_kind": "Receipt",
			},
			fields=["name", "cg_gross_qty_delta", "cg_carrying_value_delta"],
		)
		self.assertEqual(len(events), 1, f"receipt events for {receipt.name}: {events}")
		self.assertAlmostEqual(
			flt(events[0].cg_carrying_value_delta),
			money(receipt.items[0].qty),
			places=2,
		)
		return events[0]

	def _receipt_ledger_row(self, receipt):
		"""The Receipt ledger row as ``receipt_remaining`` expects it (dict-like)."""
		from jewellery_erpnext.customer_subcontracting.customer_gold_allocations import (
			effective_receipt_events,
		)

		found = effective_receipt_events(
			{
				"reference_docname": receipt.name,
				"cg_source_row": receipt.items[0].name,
			}
		)
		self.assertEqual(len(found), 1)
		return found[0]

	def _remaining(self, receipt):
		from jewellery_erpnext.customer_subcontracting.customer_gold_return import (
			receipt_remaining,
		)

		event = self._receipt_ledger_row(receipt)
		return flt(receipt_remaining(COMPANY, CUSTOMER, [event]).get(event.name))

	def _desk_issue(self, receipt_name, qty=None, receipt_row=None):
		"""Exactly what the desk's Create > Issue does: ``make_stock_in_entry`` on the receipt,
		with a preview-chosen quantity riding in ``frappe.flags.args``."""
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			make_stock_in_entry,
		)

		previous = frappe.flags.args
		frappe.flags.args = (
			frappe._dict(qty=qty, receipt_row=receipt_row)
			if (qty or receipt_row)
			else None
		)
		try:
			return make_stock_in_entry(receipt_name)
		finally:
			frappe.flags.args = previous

	def _submit_desk_issue(self, doc, receipt):
		"""Insert and submit a Create > Issue document. A refusal is reported with the batches
		the row's Serial and Batch Bundle actually picked, because that is what decides it."""
		doc.insert()
		try:
			doc.submit()
		except frappe.ValidationError as exc:
			picked = [
				frappe.get_all(
					"Serial and Batch Entry",
					filters={"parent": row.serial_and_batch_bundle},
					fields=["batch_no", "qty"],
				)
				for row in doc.items
				if row.get("serial_and_batch_bundle")
			]
			self.fail(
				f"the desk Issue for receipt batch {receipt.items[0].batch_no} could not be "
				f"submitted: {exc}. use_serial_batch_fields="
				f"{[row.use_serial_batch_fields for row in doc.items]}, bundle batches={picked}"
			)
		return doc

	def _linked_return(self, receipt, qty):
		"""A submitted return of ``qty`` g from the receipt's own batch, linked to its row.

		Built by hand (``use_serial_batch_fields = 1``) rather than through Create > Issue, so
		scenarios that only NEED a prior return do not depend on the desk mapping -- whose batch
		selection is exercised, and currently fails, in the RET-01 / RET-02 cases.
		"""
		se = self._hand_issue(receipt, receipt.items[0].batch_no, qty)
		se.insert()
		se.submit()
		return se

	def _hand_issue(self, receipt, batch_no, qty, item_code=None, warehouse=None):
		"""A return Stock Entry typed by hand against one receipt row."""
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = RETURN_SE_TYPE
		se.purpose = "Material Issue"
		se.company = COMPANY
		se.posting_date = self.posting_date
		se.set_posting_time = 1
		se._customer = CUSTOMER
		se.append(
			"items",
			{
				"item_code": item_code or self.item,
				"qty": qty,
				"s_warehouse": warehouse or self.warehouse,
				"batch_no": batch_no,
				"use_serial_batch_fields": 1,
				"uom": "Gram",
				"stock_uom": "Gram",
				"conversion_factor": 1,
				"inventory_type": "Customer Goods",
				"customer": CUSTOMER,
				"against_stock_entry": receipt.name,
				"ste_detail": receipt.items[0].name,
			},
		)
		return se

	def _lane_repack(
		self, source_batch, consumed, target_item, produced, target_batch=None
	):
		"""A lane-tagged conversion / repack inside the custody warehouse, the shape Metal
		Conversions and Settle produce. Returns the submitted entry."""
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = REPACK_SE_TYPE
		se.purpose = "Repack"
		se.company = COMPANY
		se.posting_date = self.posting_date
		se.set_posting_time = 1
		se._customer = CUSTOMER
		common = {
			"uom": "Gram",
			"stock_uom": "Gram",
			"conversion_factor": 1,
			"inventory_type": "Customer Goods",
			"customer": CUSTOMER,
			"custom_conversion_lane": LANE,
			"expense_account": self.difference_account,
		}
		se.append(
			"items",
			{
				**common,
				"item_code": self.item,
				"qty": consumed,
				"s_warehouse": self.warehouse,
				"batch_no": source_batch,
				"use_serial_batch_fields": 1,
			},
		)
		produced_row = {
			**common,
			"item_code": target_item,
			"qty": produced,
			"t_warehouse": self.warehouse,
		}
		if target_batch:
			produced_row["batch_no"] = target_batch
			produced_row["use_serial_batch_fields"] = 1
		se.append("items", produced_row)
		se.flags.ignore_mandatory = True
		se.save()
		se.submit()
		return se

	# ------------------------------------------------------------------ readers
	def _counts(self):
		return {doctype: frappe.db.count(doctype) for doctype in COUNTED}

	def _ledger(self, voucher, kind=None):
		filters = {"reference_docname": voucher}
		if kind:
			filters["cg_event_kind"] = kind
		return frappe.get_all(
			LEDGER,
			filters=filters,
			fields=[
				"name",
				"cg_event_kind",
				"cg_source_event",
				"cg_reversal_of",
				"batch_no",
				"cg_gross_qty_delta",
				"cg_carrying_value_delta",
			],
			order_by="creation",
		)

	def _allocations(self, voucher=None, receipt_event=None):
		filters = {}
		if voucher:
			filters["reference_docname"] = voucher
		if receipt_event:
			filters["receipt_event"] = receipt_event
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
				"reversal_of",
			],
			order_by="creation",
		)

	def _gl_net(self, voucher, account, include_cancelled=False):
		"""debit - credit on ``account`` for ``voucher``."""
		filters = {"voucher_no": voucher, "account": account}
		if not include_cancelled:
			filters["is_cancelled"] = 0
		rows = frappe.get_all(GL_ENTRY, filters=filters, fields=["debit", "credit"])
		return sum(flt(r.debit) - flt(r.credit) for r in rows)

	def _assert_return_posted(self, se, receipt, qty):
		"""The full accounting shape of one receipt-linked return of ``qty`` g."""
		receipt_event = self._receipt_event(receipt)
		value = money(qty)

		events = self._ledger(se.name, "Return")
		self.assertEqual(len(events), 1, f"return events: {events}")
		self.assertEqual(events[0].cg_source_event, receipt_event.name)
		self.assertAlmostEqual(flt(events[0].cg_gross_qty_delta), -qty, places=3)
		self.assertAlmostEqual(flt(events[0].cg_carrying_value_delta), -value, places=2)

		self.assertAlmostEqual(
			self._gl_net(se.name, self.liability_account),
			value,
			places=2,
			msg=f"liability not debited by {value}; GL {self._gles(se.name)}",
		)
		gl = self._gles(se.name)
		self.assertAlmostEqual(
			sum(flt(r.debit) for r in gl), sum(flt(r.credit) for r in gl), places=2
		)
		self.assertAlmostEqual(sum(flt(r.credit) for r in gl), value, places=2)

		allocations = self._allocations(voucher=se.name)
		self.assertEqual(len(allocations), 1, f"allocations: {allocations}")
		allocation = allocations[0]
		self.assertEqual(allocation.disposition, "Raw Return")
		self.assertEqual(allocation.basis, "Receipt Link")
		self.assertEqual(allocation.receipt_event, receipt_event.name)
		self.assertEqual(allocation.receipt_voucher, receipt.name)
		self.assertEqual(allocation.receipt_row, receipt.items[0].name)
		self.assertEqual(allocation.cg_event, events[0].name)
		self.assertAlmostEqual(flt(allocation.gross_qty), qty, places=6)
		self.assertAlmostEqual(flt(allocation.amount), value, places=2)
		return events[0], allocation


GL_ENTRY = "GL Entry"


class TestDeskReturnPath(_ReturnCase):
	"""RET-01..05, API parity, RET-16, RET-18/19, LIF-12: the desk Create > Issue path and the
	guards it now goes through."""

	def test_desk_issue_returns_the_remaining_two_grams_at_the_booked_rate(self):
		"""RET-01 / F2. Receive 20 g, deliver 18 g, then Create > Issue from the receipt.

		The Issue must offer exactly the 2 g still owed, link its row to the receipt row, and
		post a Return event, a Dr Liability of 2 x 7,164.83 = 14,329.66 and one Raw Return
		allocation.
		"""
		receipt = self._booked_receipt(20)
		row = receipt.items[0]
		dn = self._delivery(row.batch_no, qty=18)
		dn.save()
		dn.submit()

		doc = self._desk_issue(receipt.name)
		self.assertEqual(doc.stock_entry_type, RETURN_SE_TYPE)
		self.assertEqual(doc.custom_cg_issue_against, receipt.name)
		self.assertEqual(len(doc.items), 1, [i.as_dict() for i in doc.items])
		self.assertAlmostEqual(flt(doc.items[0].qty), 2.0, places=3)
		self.assertEqual(doc.items[0].ste_detail, row.name)
		self.assertEqual(doc.items[0].against_stock_entry, receipt.name)

		self._submit_desk_issue(doc, receipt)
		doc.reload()

		self.assertEqual(doc.items[0].against_stock_entry, receipt.name)
		self.assertEqual(doc.items[0].ste_detail, row.name)
		self.assertEqual(doc.custom_cg_issue_against, receipt.name)
		self.assertEqual(doc.items[0].expense_account, self.liability_account)
		self._assert_return_posted(doc, receipt, 2.0)
		self.assertAlmostEqual(self._remaining(receipt), 0.0, places=3)

	def test_two_partial_desk_returns_accumulate(self):
		"""RET-02 (desk). 0.5 g then 1.5 g through Create > Issue with the preview-chosen
		quantity: each Issue offers exactly the chosen quantity and the draws accumulate."""
		receipt = self._booked_receipt(10)

		def desk(qty):
			doc = self._desk_issue(receipt.name, qty=qty)
			self.assertEqual(len(doc.items), 1)
			self.assertAlmostEqual(flt(doc.items[0].qty), qty, places=3)
			self.assertEqual(doc.items[0].ste_detail, receipt.items[0].name)
			return self._submit_desk_issue(doc, receipt)

		self._assert_two_partials_accumulate(receipt, desk)

	def test_two_partial_linked_returns_accumulate(self):
		"""RET-02 (hand-built, receipt-linked rows). Same arithmetic without the desk mapping, so
		the accumulation rule is evidenced independently of the RET-01 batch-selection defect."""
		receipt = self._booked_receipt(10)
		self._assert_two_partials_accumulate(
			receipt, lambda qty: self._linked_return(receipt, qty)
		)

	def _assert_two_partials_accumulate(self, receipt, return_qty):
		"""0.5 g then 1.5 g against a fresh 10 g receipt: 2 g and Rs.14,329.66 drawn.

		0.5 x 7,164.83 = 3,582.415 and 1.5 x 7,164.83 = 10,747.245 each sit on a half paisa, so
		each allocation is allowed one rounding step; their sum must be the exact 14,329.66.
		"""
		receipt_event = self._receipt_event(receipt)

		first = return_qty(0.5)
		self.assertAlmostEqual(self._remaining(receipt), 9.5, places=3)
		second = return_qty(1.5)

		allocations = self._allocations(receipt_event=receipt_event.name)
		self.assertEqual(
			[a.disposition for a in allocations], ["Raw Return", "Raw Return"]
		)
		self.assertEqual(
			{a.cg_event for a in allocations},
			{
				self._ledger(first.name, "Return")[0].name,
				self._ledger(second.name, "Return")[0].name,
			},
		)
		for allocation, qty in zip(allocations, (0.5, 1.5), strict=True):
			self.assertAlmostEqual(flt(allocation.gross_qty), qty, places=6)
			self.assertAlmostEqual(flt(allocation.amount), exact(qty), delta=0.0051)
		self.assertAlmostEqual(
			sum(flt(a.gross_qty) for a in allocations), 2.0, places=6
		)
		self.assertAlmostEqual(
			sum(flt(a.amount) for a in allocations), 14329.66, delta=0.0101
		)
		# The liability moved by what the allocations say, document by document.
		for se in (first, second):
			allocation = self._allocations(voucher=se.name)[0]
			event = self._ledger(se.name, "Return")[0]
			self.assertAlmostEqual(
				self._gl_net(se.name, self.liability_account),
				flt(allocation.amount),
				places=2,
				msg=(
					f"{se.name} qty {se.items[0].qty}: GL liability debit "
					f"{self._gl_net(se.name, self.liability_account)}, allocation "
					f"{allocation.amount}, ledger event {event.cg_carrying_value_delta}, "
					f"row basic_amount {frappe.db.get_value('Stock Entry Detail', se.items[0].name, 'basic_amount')}"
				),
			)
		self.assertAlmostEqual(self._remaining(receipt), 8.0, places=3)

	def test_an_exhausted_receipt_row_is_not_offered_and_cannot_be_returned_again(self):
		"""RET-03. Return all 10 g; Create > Issue then maps no row, and a hand-built Issue is
		refused and writes nothing.

		With the batch physically empty the physical guard answers first ("free"). To reach the
		entitlement guard the batch is then refilled with 3 g of the SAME customer's metal from
		a second receipt (a lane-tagged repack into the receipt batch -- what Settle does); the
		receipt row still owes nothing, so a 1 g return against it must say "left to return".
		"""
		receipt = self._booked_receipt(10)
		batch = receipt.items[0].batch_no
		full = self._linked_return(receipt, 10)
		self.assertAlmostEqual(flt(full.items[0].qty), 10.0, places=3)
		self.assertAlmostEqual(self._remaining(receipt), 0.0, places=3)

		offered = self._desk_issue(receipt.name)
		self.assertFalse(
			[i for i in offered.items if i.ste_detail == receipt.items[0].name],
			"an exhausted receipt row was offered again",
		)

		before = self._counts()
		with self.assertThrowsContaining("is free in"):
			self._hand_issue(receipt, batch, 1).insert()
		self.assertEqual(self._counts(), before)

		other = self._booked_receipt(10)
		self._lane_repack(other.items[0].batch_no, 3, self.item, 3, target_batch=batch)
		from erpnext.stock.doctype.batch.batch import get_batch_qty

		self.assertAlmostEqual(
			flt(get_batch_qty(batch_no=batch, warehouse=self.warehouse)), 3.0, places=3
		)

		before = self._counts()
		with self.assertThrowsContaining("left to return"):
			self._hand_issue(receipt, batch, 1).insert()
		self.assertEqual(self._counts(), before)

	def test_more_than_the_entitlement_is_refused_even_when_stock_is_there(self):
		"""RET-04. Receipt A 10 g, 8 g returned (2 g owed). Its batch is refilled with 5 g of
		receipt B's metal, so 7 g sits there physically. A 3 g return against A must be refused
		("left to return") with nothing written; 2 g is still allowed."""
		receipt = self._booked_receipt(10)
		batch = receipt.items[0].batch_no
		self._linked_return(receipt, 8)
		self.assertAlmostEqual(self._remaining(receipt), 2.0, places=3)

		other = self._booked_receipt(10)
		self._lane_repack(other.items[0].batch_no, 5, self.item, 5, target_batch=batch)
		from erpnext.stock.doctype.batch.batch import get_batch_qty

		self.assertAlmostEqual(
			flt(get_batch_qty(batch_no=batch, warehouse=self.warehouse)), 7.0, places=3
		)
		self.assertAlmostEqual(self._remaining(receipt), 2.0, places=3)

		before = self._counts()
		with self.assertThrowsContaining("left to return"):
			self._hand_issue(receipt, batch, 3).insert()
		self.assertEqual(self._counts(), before)

		allowed = self._hand_issue(receipt, batch, 2)
		allowed.insert()
		allowed.submit()
		self._assert_return_posted(allowed, receipt, 2.0)

	def test_a_return_against_one_receipt_does_not_eat_anothers_entitlement(self):
		"""RET-04 (follow-on). Receipt B handed in 10 g and nothing was ever returned or
		delivered against it. After receipt A's last 2 g are returned out of the shared batch
		(A: 10 received, 10 returned), B must still be owed its full 10 g -- 5 g of it in B's own
		batch and 5 g in A's refilled batch, all physically present."""
		receipt = self._booked_receipt(10)
		batch = receipt.items[0].batch_no
		self._linked_return(receipt, 8)
		other = self._booked_receipt(10)
		self._lane_repack(other.items[0].batch_no, 5, self.item, 5, target_batch=batch)
		self.assertAlmostEqual(self._remaining(other), 10.0, places=3)

		last = self._hand_issue(receipt, batch, 2)
		last.insert()
		last.submit()

		self.assertAlmostEqual(self._remaining(receipt), 0.0, places=3)
		self.assertAlmostEqual(
			self._remaining(other),
			10.0,
			places=3,
			msg="a return linked to receipt A reduced receipt B's entitlement",
		)

	def test_metal_moved_out_of_custody_is_refused_as_not_free(self):
		"""RET-05. 10 g owed, but 6 g was transferred to the floor: Create > Issue still offers
		the 10 g entitlement, and submitting it is refused by the physical guard ("free"), with
		nothing written."""
		receipt = self._booked_receipt(10)
		self._transfer(receipt.items[0].batch_no, 6, self.wip_warehouse)
		self.assertAlmostEqual(self._remaining(receipt), 10.0, places=3)

		doc = self._desk_issue(receipt.name)
		self.assertAlmostEqual(flt(doc.items[0].qty), 10.0, places=3)
		before = self._counts()
		with self.assertThrowsContaining("is free in"):
			doc.insert()
		self.assertEqual(self._counts(), before)

	def test_the_api_writes_exactly_what_the_desk_path_writes(self):
		"""API parity. ``make_customer_gold_return`` for 2 g: one Return event and one Raw Return
		allocation (the hook is the only writer -- no duplicate), same numbers as RET-01."""
		from jewellery_erpnext.customer_subcontracting.customer_gold_return import (
			make_customer_gold_return,
		)

		receipt = self._booked_receipt(10)
		name = make_customer_gold_return(
			company=COMPANY,
			customer=CUSTOMER,
			batch_no=receipt.items[0].batch_no,
			qty=2,
			warehouse=self.warehouse,
			item_code=self.item,
		)
		se = frappe.get_doc("Stock Entry", name)
		self.assertEqual(len(self._ledger(name)), 1, self._ledger(name))
		self._assert_return_posted(se, receipt, 2.0)
		self.assertEqual(se.items[0].against_stock_entry, receipt.name)
		self.assertEqual(se.items[0].ste_detail, receipt.items[0].name)
		self.assertAlmostEqual(self._remaining(receipt), 8.0, places=3)

	def test_replaying_record_return_writes_nothing(self):
		"""RET-16. Calling the on_submit hook again on a submitted return is absorbed."""
		from jewellery_erpnext.customer_subcontracting import customer_gold_return

		receipt = self._booked_receipt(10)
		se = self._linked_return(receipt, 2)
		before = self._counts()
		customer_gold_return.record_return(frappe.get_doc("Stock Entry", se.name))
		self.assertEqual(self._counts(), before)
		self._assert_return_posted(se, receipt, 2.0)

	def test_cancel_then_amend_a_partial_return(self):
		"""RET-18 / RET-19. Return 3 g, cancel it: one Reversal event (+21,494.49) and one
		Reversal allocation (-3 g, -21,494.49), entitlement back to 10 g, liability GL netted to
		zero. Then amend to 4 g: the receipt's allocations net to 4 g / 28,659.32."""
		receipt = self._booked_receipt(10)
		receipt_event = self._receipt_event(receipt)
		se = self._linked_return(receipt, 3)
		event, allocation = self._assert_return_posted(se, receipt, 3.0)
		self.assertAlmostEqual(self._remaining(receipt), 7.0, places=3)

		se.reload()
		se.cancel()

		reversals = self._ledger(se.name, "Reversal")
		self.assertEqual(len(reversals), 1, reversals)
		self.assertEqual(reversals[0].cg_reversal_of, event.name)
		self.assertAlmostEqual(flt(reversals[0].cg_gross_qty_delta), 3.0, places=3)
		self.assertAlmostEqual(
			flt(reversals[0].cg_carrying_value_delta), money(3), places=2
		)

		rows = self._allocations(voucher=se.name)
		reversal_rows = [r for r in rows if r.disposition == "Reversal"]
		self.assertEqual(len(rows), 2, rows)
		self.assertEqual(len(reversal_rows), 1)
		self.assertEqual(reversal_rows[0].reversal_of, allocation.name)
		self.assertAlmostEqual(flt(reversal_rows[0].gross_qty), -3.0, places=6)
		self.assertAlmostEqual(flt(reversal_rows[0].amount), -money(3), places=2)
		self.assertAlmostEqual(self._remaining(receipt), 10.0, places=3)

		self.assertFalse(self._gles(se.name), "live GL rows survived the cancel")
		self.assertAlmostEqual(
			self._gl_net(se.name, self.liability_account, include_cancelled=True),
			0.0,
			places=2,
		)

		# What the desk's Amend does: a fresh draft copy of the cancelled entry.
		amended = frappe.copy_doc(se)
		amended.amended_from = se.name
		amended.docstatus = 0
		for child in amended.get_all_children():
			child.docstatus = 0
		# The copy does not carry the batch pick (a draft copy starts with no bundle), so the
		# operator picks the receipt batch again, exactly as on the desk form.
		amended.items[0].batch_no = receipt.items[0].batch_no
		amended.items[0].use_serial_batch_fields = 1
		amended.items[0].serial_and_batch_bundle = None
		amended.items[0].qty = 4
		amended.insert()
		amended.submit()
		self._assert_return_posted(amended, receipt, 4.0)

		net = self._allocations(receipt_event=receipt_event.name)
		self.assertAlmostEqual(sum(flt(r.gross_qty) for r in net), 4.0, places=6)
		self.assertAlmostEqual(sum(flt(r.amount) for r in net), money(4), places=2)
		self.assertAlmostEqual(self._remaining(receipt), 6.0, places=3)

	def test_cancelling_a_receipt_with_a_submitted_return_is_blocked(self):
		"""LIF-12. Receipt 10 g, 2 g returned against it. Cancelling the receipt would leave a
		Return event and a Raw Return allocation pointing at a reversed receipt and the batch at
		-2 g, so it must be refused, leaving the receipt submitted and unreversed."""
		receipt = self._booked_receipt(10)
		ret = self._linked_return(receipt, 2)
		before = self._counts()

		doc = frappe.get_doc("Stock Entry", receipt.name)
		# A savepoint stands in for the request-level rollback a refused desk cancel gets.
		frappe.db.savepoint("lif12")
		try:
			doc.cancel()
		except frappe.ValidationError as exc:
			frappe.db.rollback(save_point="lif12")
			message = str(exc)
		else:
			evidence = {
				"receipt_events": self._ledger(receipt.name),
				"return_events": self._ledger(ret.name),
				"return_allocations": self._allocations(voucher=ret.name),
				"batch_qty": frappe.db.sql(
					"""SELECT SUM(actual_qty) FROM `tabStock Ledger Entry`
					WHERE batch_no = %s AND is_cancelled = 0""",
					receipt.items[0].batch_no,
				),
			}
			self.fail(
				f"the receipt cancelled with a submitted return against it: {evidence}"
			)

		# Refused by the customer-gold dependency guard, which names the return -- not merely by
		# erpnext's negative-batch guard, which a refilled or descendant batch would slip past.
		self.assertIn("cannot be cancelled while customer gold received on it", message)
		self.assertIn(ret.name, message)
		self.assertEqual(
			frappe.db.get_value("Stock Entry", receipt.name, "docstatus"), 1
		)
		self.assertFalse(self._ledger(receipt.name, "Reversal"))
		self.assertEqual(self._counts(), before)


class TestReturnDescendants(_ReturnCase):
	"""RET-11 / D08 and RET-08: returning a batch produced FROM the receipt's metal."""

	def test_a_converted_purity_is_refused_as_different_purity(self):
		"""RET-11 / D08. 6 g of the 99.9% receipt converted to 7.95 g of 75.4% (6 x 99.9 / 75.4
		= 7.9496, held at 2 dp). Returning 1 g of that 75.4% batch against the 99.9% receipt is
		refused ("Different Purity") and writes nothing."""
		receipt = self._booked_receipt(10)
		conversion = self._lane_repack(
			receipt.items[0].batch_no, 6, self.operating_item, 7.95
		)
		converted = conversion.items[1].batch_no
		self.assertTrue(converted)
		self.assertNotEqual(converted, receipt.items[0].batch_no)

		before = self._counts()
		frappe.clear_messages()
		with self.assertThrowsContaining("different item or purity"):
			self._hand_issue(
				receipt, converted, 1, item_code=self.operating_item
			).insert()
		self.assertEqual(self._counts(), before)
		# The dialog title carries the decision's name.
		titles = [
			(frappe.parse_json(m) if isinstance(m, str) else m).get("title")
			for m in frappe.local.message_log
		]
		self.assertIn("Customer Gold Return: Different Purity", titles)

	def test_a_same_item_descendant_returns_at_the_booked_rate(self):
		"""RET-08. A lane-tagged repack moves 4 g of the receipt batch into a new batch of the
		same item (value carried: 4 x 7,164.83 = 28,659.32, i.e. 7,164.83/g -- inside the 0.05%
		D08 tolerance). Returning 2 g of that descendant against the receipt is allowed and
		releases 2 x 7,164.83 = 14,329.66 from the liability."""
		receipt = self._booked_receipt(10)
		repack = self._lane_repack(receipt.items[0].batch_no, 4, self.item, 4)
		descendant = repack.items[1].batch_no
		self.assertNotEqual(descendant, receipt.items[0].batch_no)

		produced = frappe.db.get_value(
			"Stock Ledger Entry",
			{
				"voucher_no": repack.name,
				"voucher_detail_no": repack.items[1].name,
				"is_cancelled": 0,
			},
			["actual_qty", "stock_value_difference"],
			as_dict=True,
		)
		self.assertAlmostEqual(flt(produced.actual_qty), 4.0, places=3)
		self.assertAlmostEqual(flt(produced.stock_value_difference), money(4), places=2)

		se = self._hand_issue(receipt, descendant, 2)
		se.insert()
		se.submit()
		event, _ = self._assert_return_posted(se, receipt, 2.0)
		self.assertEqual(event.batch_no, descendant)
		self.assertAlmostEqual(self._remaining(receipt), 8.0, places=3)


class TestSettleConversionLane(_ReturnCase):
	"""Settle's ``_convert_multi`` now stamps the ownership lane, so it writes Conversion events.

	And the customer's value must travel with the metal: the produced customer row carries the
	consumed customer value. A produced row at zero with the consumed value landing on Stock
	Adjustment would expense the customer's gold (``_append_item`` forces
	``allow_zero_valuation_rate = 1`` on every row).
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Stock Entry Type", SETTLE_REPACK_TYPE):
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = SETTLE_REPACK_TYPE
			doc.purpose = "Repack"
			doc.insert(ignore_permissions=True)

	def _settle_convert(self, batch):
		"""4 g of 99.9% -> 5.30 g of 75.4% (4 x 99.9 / 75.4 = 5.29973, held at 3 dp -> 5.3)."""
		from jewellery_erpnext.customer_subcontracting.sub_utils.cg_settle import (
			_convert_multi,
		)

		target = _convert_multi(
			frappe._dict(company=COMPANY, branch=None),
			source_item=self.item,
			source_rows=[{"batch_no": batch, "qty": 4}],
			target_item=self.operating_item,
			target_qty=5.3,
			warehouse=self.warehouse,
			customer=CUSTOMER,
		)
		se_name = frappe.db.get_value(
			"Stock Entry Detail",
			{"batch_no": target, "t_warehouse": self.warehouse, "docstatus": 1},
			"parent",
		)
		return target, frappe.get_doc("Stock Entry", se_name)

	def test_every_row_carries_the_customer_lane_and_conversion_events_are_written(
		self,
	):
		"""Settle lane. Every row stamped "Customer Goods|<customer>"; Conversion Out and
		Conversion In both written for the Repack."""
		receipt = self._booked_receipt(10)
		_, se = self._settle_convert(receipt.items[0].batch_no)

		self.assertEqual(se.stock_entry_type, SETTLE_REPACK_TYPE)
		self.assertEqual(len(se.items), 2)
		self.assertEqual({row.custom_conversion_lane for row in se.items}, {LANE})

		kinds = [e.cg_event_kind for e in self._ledger(se.name)]
		self.assertIn("Conversion Out", kinds)
		self.assertIn("Conversion In", kinds)

	def test_the_customer_value_travels_with_the_converted_metal(self):
		"""Settle lane valuation. 4 g consumed at 7,164.83 = 28,659.32 leaves the source batch;
		the produced 5.3 g must carry that same 28,659.32, and nothing may post to Stock
		Adjustment (which would expense the customer's gold and leave the liability standing)."""
		receipt = self._booked_receipt(10)
		_, se = self._settle_convert(receipt.items[0].batch_no)

		sles = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": se.name, "is_cancelled": 0},
			fields=["item_code", "actual_qty", "stock_value_difference"],
		)
		consumed = [s for s in sles if flt(s.actual_qty) < 0]
		produced = [s for s in sles if flt(s.actual_qty) > 0]
		self.assertAlmostEqual(
			sum(flt(s.stock_value_difference) for s in consumed),
			-money(4),
			places=2,
			msg=f"SLEs {sles}",
		)
		gl = self._gles(se.name)
		self.assertAlmostEqual(
			sum(flt(s.stock_value_difference) for s in produced),
			money(4),
			places=2,
			msg=(
				"the Settle conversion produced the customer's metal at a different value "
				f"than it consumed; SLEs {sles}; GL {gl}"
			),
		)
		self.assertAlmostEqual(
			self._gl_net(se.name, self.difference_account),
			0.0,
			places=2,
			msg=f"customer value posted to Stock Adjustment; GL {gl}",
		)


class TestReturnPreview(_ReturnCase):
	"""``get_customer_gold_return_preview``: the route it proposes, that it reads only, and that
	it honours document permission (RET-20)."""

	def _preview_row(self, receipt):
		from jewellery_erpnext.customer_subcontracting.customer_gold_return import (
			get_customer_gold_return_preview,
		)

		before = self._counts()
		result = get_customer_gold_return_preview(receipt.name)
		self.assertEqual(self._counts(), before, "the preview wrote something")
		rows = [r for r in result["rows"] if r["receipt_row"] == receipt.items[0].name]
		self.assertEqual(len(rows), 1, result)
		return rows[0]

	def test_free_in_custody_is_direct(self):
		"""Preview: 10 g received, all free in its custody warehouse -> "Direct"."""
		receipt = self._booked_receipt(10)
		row = self._preview_row(receipt)
		self.assertEqual(row["route"], "Direct")
		self.assertAlmostEqual(row["received"], 10.0, places=3)
		self.assertAlmostEqual(row["remaining"], 10.0, places=3)
		self.assertEqual(row["custody_warehouse"], self.warehouse)

	def test_moved_to_another_warehouse_is_a_transfer(self):
		"""Preview: all 10 g moved to the floor warehouse -> "Transfer via Material Request"."""
		receipt = self._booked_receipt(10)
		self._transfer(receipt.items[0].batch_no, 10, self.wip_warehouse)
		row = self._preview_row(receipt)
		self.assertEqual(row["route"], "Transfer via Material Request")
		self.assertAlmostEqual(row["remaining"], 10.0, places=3)

	def test_fully_converted_is_settle_then_issue(self):
		"""Preview: all 10 g of 99.9% converted to 13.25 g of 75.4% (10 x 99.9 / 75.4 =
		13.2493 -> 13.25) -> "Settle, then Issue"."""
		receipt = self._booked_receipt(10)
		self._lane_repack(receipt.items[0].batch_no, 10, self.operating_item, 13.25)
		row = self._preview_row(receipt)
		self.assertEqual(row["route"], "Settle, then Issue")
		self.assertAlmostEqual(row["remaining"], 10.0, places=3)
		self.assertTrue(row["holdings"])
		self.assertFalse(any(h["same_item"] for h in row["holdings"]))

	def test_a_user_without_read_permission_is_refused(self):
		"""RET-20. A user with no roles cannot preview a receipt they cannot read."""
		from jewellery_erpnext.customer_subcontracting.customer_gold_return import (
			get_customer_gold_return_preview,
		)

		receipt = self._booked_receipt(10)
		email = "cg-return-noperm@example.com"
		if not frappe.db.exists("User", email):
			user = frappe.new_doc("User")
			user.email = email
			user.first_name = "CG No Permission"
			user.send_welcome_email = 0
			user.insert(ignore_permissions=True)

		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				get_customer_gold_return_preview(receipt.name)
		finally:
			frappe.set_user("Administrator")


class TestConcurrentReturns(_ReturnCase):
	"""F7 / RET-17. Two connections each return 1.5 g against the same 2 g receipt row at the
	same moment: at most one may succeed.

	This class COMMITS (other connections cannot see uncommitted rows), so it cleans up after
	itself: every document it submitted is cancelled, drafts are deleted, and the two
	Subcontracting Settings fields it changed are put back.
	"""

	@classmethod
	def setUpClass(cls):
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		cls._saved_settings = {
			"customer_gold_valuation_policy": settings.get(
				"customer_gold_valuation_policy"
			),
			"customer_gold_return_stock_entry_type": settings.get(
				"customer_gold_return_stock_entry_type"
			),
		}
		super().setUpClass()

	def _worker(self, site, sites_path, draft, barrier, results):
		try:
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			frappe.set_user("Administrator")
			frappe.flags.in_test = True
			doc = frappe.get_doc("Stock Entry", draft)
			barrier.wait(timeout=120)
			doc.submit()
			frappe.db.commit()
			results[draft] = ("ok", None)
		except Exception as exc:
			try:
				frappe.db.rollback()
			except Exception:
				pass
			results[draft] = ("error", f"{type(exc).__name__}: {exc}")
		finally:
			frappe.destroy()

	def test_two_concurrent_returns_cannot_both_draw_the_last_grams(self):
		"""F7 / RET-17. Receipt 2 g; two 1.5 g returns submitted concurrently from separate
		connections. Exactly one succeeds; the other fails on the entitlement ("left to return")
		or on a lock timeout, and the receipt's allocations total 1.5 g."""
		site = frappe.local.site
		sites_path = frappe.local.sites_path
		receipt = self._booked_receipt(2)
		drafts = []
		results = {}
		try:
			for _ in range(2):
				doc = self._hand_issue(receipt, receipt.items[0].batch_no, 1.5)
				doc.insert()
				drafts.append(doc.name)
			frappe.db.commit()

			barrier = threading.Barrier(2)
			threads = [
				threading.Thread(
					target=self._worker,
					args=(site, sites_path, name, barrier, results),
				)
				for name in drafts
			]
			for thread in threads:
				thread.start()
			for thread in threads:
				thread.join(timeout=300)
			frappe.db.rollback()

			self.assertEqual(set(results), set(drafts), results)
			succeeded = [n for n, (state, _) in results.items() if state == "ok"]
			failed = {n: msg for n, (state, msg) in results.items() if state != "ok"}
			self.assertEqual(len(succeeded), 1, f"results: {results}")
			for message in failed.values():
				self.assertTrue(
					"left to return" in message
					or "Lock wait timeout" in message
					or "Deadlock" in message,
					f"the losing return failed for another reason: {message}",
				)
			event = self._receipt_event(receipt)
			self.assertAlmostEqual(
				sum(
					flt(a.gross_qty)
					for a in self._allocations(receipt_event=event.name)
				),
				1.5,
				places=6,
			)
		finally:
			frappe.db.rollback()
			for name in drafts:
				status = frappe.db.get_value("Stock Entry", name, "docstatus")
				if status == 1:
					frappe.get_doc("Stock Entry", name).cancel()
				elif status == 0:
					frappe.delete_doc("Stock Entry", name, force=True)
			if frappe.db.get_value("Stock Entry", receipt.name, "docstatus") == 1:
				frappe.get_doc("Stock Entry", receipt.name).cancel()
			settings = frappe.get_doc(SETTINGS_DOCTYPE)
			for field, value in self._saved_settings.items():
				settings.set(field, value)
			settings.save(ignore_permissions=True)
			frappe.db.commit()
			frappe.clear_cache(doctype=SETTINGS_DOCTYPE)
