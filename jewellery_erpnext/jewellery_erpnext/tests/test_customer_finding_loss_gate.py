# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the customer-supplied-finding loss booking gate.

A finding the customer supplied comes back at its issued weight, so no process loss
may be booked against it. The gate refuses a loss row when all three hold: the item's
``Item.variant_of`` is ``"F"``, its Batch is ``custom_inventory_type = "Customer
Goods"``, and that Batch was minted by a ``Customer Goods Received`` Stock Entry or by
a Purchase Receipt.

The two loss paths behave differently, and that asymmetry is the point of most of these
tests -- it is the same split ``material_loss_gate`` already makes:

  * **Automatic** -- blocked batches drop out of the ``book_metal_loss`` pool BEFORE
    ``total_qty`` is summed, so the survivors absorb the blocked row's share and the
    booked total still equals ``gross_wt - received_gross_wt``. That is what keeps
    ``validate_loss_tables_required``'s 0.0005 g sum-match passing unchanged. This path
    NEVER throws -- saving must always succeed.
  * **Manual** -- refused, and only from ``on_submit``.

The contract is fail-open: a batch that is not customer-supplied finding stock, or whose
origin cannot be resolved at all, books loss exactly as before.

Origin resolution has two paths and both are pinned here. ERPNext NULLs
``reference_doctype``/``reference_name`` when the minting voucher is cancelled but leaves
``custom_voucher_detail_no`` alone (940 live batches are already in that state), so the
detail row is primary and ``reference_name`` the fallback.

DB-free per the suite convention -- ``setUpClass`` is neutralised and every ``frappe``
lookup is patched. ``frappe.db.get_value`` is never blanket-patched (it would hijack
DocType meta loading) and ``frappe.get_all`` is never patched (the first ``_()`` reads
Translation through it); the gate reads through ``frappe.db.get_all`` in its own module
namespace precisely so this suite can fake it safely.
"""

from unittest.mock import patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.customer_finding_loss_gate import (
	CUSTOMER_GOODS,
	DEFAULT_RECEIPT_SE_TYPE,
	get_blocked_finding_batches,
	get_customer_goods_receipt_se_types,
	is_customer_goods_finding_blocked,
	validate_customer_goods_finding_loss_rows,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
	EmployeeIR,
)

GATE = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.customer_finding_loss_gate"
EIR = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir"

METAL = "M-G-22KT-91.9-Y"
CHAIN = "F-G-22KT-91.9-Y-CHA-KC-2.50 MM"
CLASP = "F-G-18KT-75.0-Y-CLA-LB-1.00 MM"
FINDING_LOSS = "FL-G-22KT-91.9-Y"

VARIANTS = {
	METAL: "M",
	CHAIN: "F",
	CLASP: "F",
	FINDING_LOSS: "FL",
}

CG_BATCH = "B-CG-FINDING"
REG_BATCH = "B-REGULAR"
RECEIPT_SE = "MAT-STE-2026-00042"
RECEIPT_PR = "MAT-PRE-2026-00007"


def _mop_row(item_code, batch_no, qty, pcs=0):
	return frappe._dict(
		{"item_code": item_code, "batch_no": batch_no, "qty": qty, "pcs": pcs}
	)


def _loss_row(item_code, batch_no, idx=1):
	return frappe._dict({"item_code": item_code, "batch_no": batch_no, "idx": idx})


class _DocStub:
	"""Stand-in for the pieces of Employee IR the gate touches."""

	def __init__(self, manual_rows=None, auto_rows=None, doc_type="Receive"):
		self.operation = "Casting"
		self.type = doc_type
		self.flags = frappe._dict()
		self.employee_loss_details = auto_rows or []
		self.manually_book_loss_details = manual_rows or []


def _fake_db_get_all(batches=None, se_details=None, pr_items=None, receipt_ses=None):
	"""A ``frappe.db.get_all`` that answers only the four reads the gate makes.

	Keyed on the doctype rather than call order, so a change in query order inside the
	resolver does not silently start feeding it the wrong rows.
	"""
	batches = batches or []
	se_details = se_details or {}
	pr_items = pr_items or {}
	receipt_ses = set(receipt_ses or [])

	def run(doctype, filters=None, fields=None, pluck=None, **kwargs):
		filters = filters or {}
		if doctype == "Batch":
			wanted = set(filters.get("name", [None, []])[1])
			assert filters.get("custom_inventory_type") == CUSTOMER_GOODS
			return [dict(b) for b in batches if b["name"] in wanted]
		if doctype in ("Stock Entry Detail", "Purchase Receipt Item"):
			source = se_details if doctype == "Stock Entry Detail" else pr_items
			wanted = set(filters.get("name", [None, []])[1])
			return [{"name": n, "parent": p} for n, p in source.items() if n in wanted]
		if doctype == "Stock Entry":
			wanted = set(filters.get("name", [None, []])[1])
			allowed = set(filters.get("stock_entry_type", [None, []])[1])
			assert DEFAULT_RECEIPT_SE_TYPE in allowed
			return sorted(n for n in wanted if n in receipt_ses)
		raise AssertionError(f"unexpected read of {doctype}")

	return run


class TestIsCustomerGoodsFindingBlocked(IntegrationTestCase):
	"""The pure predicate — no DB, no patching needed."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_blocked_when_batch_is_in_the_map(self):
		self.assertTrue(
			is_customer_goods_finding_blocked(CG_BATCH, {CG_BATCH: RECEIPT_SE})
		)

	def test_not_blocked_when_batch_absent(self):
		self.assertFalse(
			is_customer_goods_finding_blocked(REG_BATCH, {CG_BATCH: RECEIPT_SE})
		)

	def test_not_blocked_when_nothing_is_blocked(self):
		# Fail-open: the guarantee that shipping this changes nothing.
		self.assertFalse(is_customer_goods_finding_blocked(CG_BATCH, {}))

	def test_handles_missing_batch_no(self):
		# MOP Log.batch_no is a Data field with no referential integrity, so a blank
		# batch is routine rather than exceptional.
		self.assertFalse(
			is_customer_goods_finding_blocked(None, {CG_BATCH: RECEIPT_SE})
		)
		self.assertFalse(is_customer_goods_finding_blocked("", {CG_BATCH: RECEIPT_SE}))


class TestGetCustomerGoodsReceiptSeTypes(IntegrationTestCase):
	"""The Stock Entry Type allow-list."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_always_includes_the_seeded_literal(self):
		with patch(f"{GATE}.frappe.db.has_column", return_value=True):
			self.assertIn(
				DEFAULT_RECEIPT_SE_TYPE, get_customer_goods_receipt_se_types()
			)

	def test_literal_is_past_tense(self):
		# "Customer Goods Receive" is an ACCOUNT name on this site, not a Stock Entry
		# Type. Conflating the two would make the gate match nothing.
		self.assertEqual(DEFAULT_RECEIPT_SE_TYPE, "Customer Goods Received")


class TestGetBlockedFindingBatches(IntegrationTestCase):
	"""Origin resolution: both paths, both voucher types, and the fail-open cases."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(
		self,
		rows,
		batches,
		se_details=None,
		pr_items=None,
		receipt_ses=None,
		has_detail=True,
	):
		patches = [
			patch(f"{GATE}.get_variant_of_map", return_value=VARIANTS),
			patch(f"{GATE}.frappe.db.has_column", return_value=has_detail),
			patch(
				f"{GATE}.get_customer_goods_receipt_se_types",
				return_value={DEFAULT_RECEIPT_SE_TYPE},
			),
			patch(
				f"{GATE}.frappe.db.get_all",
				side_effect=_fake_db_get_all(
					batches, se_details, pr_items, receipt_ses
				),
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		return get_blocked_finding_batches(rows)

	def test_blocked_via_detail_row_when_reference_is_null(self):
		"""The 940-batch case: cancel stripped reference_name, detail row survived."""
		blocked = self._run(
			[_mop_row(CHAIN, CG_BATCH, 20.0)],
			batches=[
				{
					"name": CG_BATCH,
					"reference_doctype": None,
					"reference_name": None,
					"custom_voucher_detail_no": "SED-1",
				}
			],
			se_details={"SED-1": RECEIPT_SE},
			receipt_ses=[RECEIPT_SE],
		)
		self.assertEqual(blocked, {CG_BATCH: RECEIPT_SE})

	def test_blocked_via_reference_name_when_detail_row_is_gone(self):
		blocked = self._run(
			[_mop_row(CHAIN, CG_BATCH, 20.0)],
			batches=[
				{
					"name": CG_BATCH,
					"reference_doctype": "Stock Entry",
					"reference_name": RECEIPT_SE,
					"custom_voucher_detail_no": None,
				}
			],
			receipt_ses=[RECEIPT_SE],
		)
		self.assertEqual(blocked, {CG_BATCH: RECEIPT_SE})

	def test_blocked_via_purchase_receipt_without_any_type_filter(self):
		"""Any Purchase Receipt origin qualifies — there is no PR "type" to check."""
		blocked = self._run(
			[_mop_row(CHAIN, CG_BATCH, 20.0)],
			batches=[
				{
					"name": CG_BATCH,
					"reference_doctype": "Purchase Receipt",
					"reference_name": RECEIPT_PR,
					"custom_voucher_detail_no": "PRI-1",
				}
			],
			pr_items={"PRI-1": RECEIPT_PR},
		)
		self.assertEqual(blocked, {CG_BATCH: RECEIPT_PR})

	def test_not_blocked_on_a_transfer_or_issue_stock_entry(self):
		"""Customer Goods Transfer / Issue are not receipts, so they do not qualify."""
		blocked = self._run(
			[_mop_row(CHAIN, CG_BATCH, 20.0)],
			batches=[
				{
					"name": CG_BATCH,
					"reference_doctype": "Stock Entry",
					"reference_name": "MAT-STE-2026-09999",
					"custom_voucher_detail_no": "SED-9",
				}
			],
			se_details={"SED-9": "MAT-STE-2026-09999"},
			receipt_ses=[],  # that SE is not of a receipt type
		)
		self.assertEqual(blocked, {})

	def test_metal_never_blocked_even_on_an_identical_batch(self):
		"""The gate is finding-only; the metal on the same customer batch is untouched."""
		blocked = self._run(
			[_mop_row(METAL, CG_BATCH, 80.0)],
			batches=[
				{
					"name": CG_BATCH,
					"reference_doctype": "Stock Entry",
					"reference_name": RECEIPT_SE,
					"custom_voucher_detail_no": "SED-1",
				}
			],
			se_details={"SED-1": RECEIPT_SE},
			receipt_ses=[RECEIPT_SE],
		)
		self.assertEqual(blocked, {})

	def test_finding_loss_variant_not_caught(self):
		# Exact variant_of equality: "FL" is not "F". Documented, deliberate — an FL
		# item is minted loss, never customer-received.
		blocked = self._run(
			[_mop_row(FINDING_LOSS, CG_BATCH, 5.0)],
			batches=[
				{
					"name": CG_BATCH,
					"reference_doctype": "Stock Entry",
					"reference_name": RECEIPT_SE,
					"custom_voucher_detail_no": "SED-1",
				}
			],
			se_details={"SED-1": RECEIPT_SE},
			receipt_ses=[RECEIPT_SE],
		)
		self.assertEqual(blocked, {})

	def test_regular_stock_batch_not_blocked(self):
		"""The inventory-type filter is pushed into the Batch query, so it returns none."""
		blocked = self._run([_mop_row(CHAIN, REG_BATCH, 20.0)], batches=[])
		self.assertEqual(blocked, {})

	def test_fails_open_when_origin_is_unresolvable(self):
		"""Both provenance paths gone — not blocked, so the document stays submittable."""
		blocked = self._run(
			[_mop_row(CHAIN, CG_BATCH, 20.0)],
			batches=[
				{
					"name": CG_BATCH,
					"reference_doctype": None,
					"reference_name": None,
					"custom_voucher_detail_no": None,
				}
			],
		)
		self.assertEqual(blocked, {})

	def test_missing_detail_column_falls_back_to_reference_name(self):
		"""install.py does not guarantee custom_voucher_detail_no; an unguarded select
		would raise MariaDB 1054 from inside the loss engine."""
		blocked = self._run(
			[_mop_row(CHAIN, CG_BATCH, 20.0)],
			batches=[
				{
					"name": CG_BATCH,
					"reference_doctype": "Stock Entry",
					"reference_name": RECEIPT_SE,
				}
			],
			receipt_ses=[RECEIPT_SE],
			has_detail=False,
		)
		self.assertEqual(blocked, {CG_BATCH: RECEIPT_SE})

	def test_empty_input_costs_no_query_at_all(self):
		with patch(f"{GATE}.frappe.db.get_all") as get_all:
			self.assertEqual(get_blocked_finding_batches([]), {})
			self.assertEqual(get_blocked_finding_batches(None), {})
			get_all.assert_not_called()

	def test_metal_only_operation_costs_no_query(self):
		"""The zero-extra-queries promise the sibling gates make, on the common case.

		The item-code prefix narrows before anything is read, so an operation carrying
		no findings at all never touches Item or Batch.
		"""
		with patch(f"{GATE}.frappe.db.get_all") as get_all, patch(
			f"{GATE}.get_variant_of_map"
		) as variant_map:
			self.assertEqual(
				get_blocked_finding_batches(
					[_mop_row(METAL, CG_BATCH, 80.0), _mop_row(METAL, REG_BATCH, 20.0)]
				),
				{},
			)
			get_all.assert_not_called()
			variant_map.assert_not_called()

	def test_rows_without_a_batch_cost_no_query(self):
		with patch(f"{GATE}.frappe.db.get_all") as get_all:
			self.assertEqual(
				get_blocked_finding_batches([_mop_row(CHAIN, None, 1.0)]), {}
			)
			get_all.assert_not_called()

	def test_mixed_batches_block_only_the_customer_supplied_one(self):
		blocked = self._run(
			[_mop_row(CHAIN, CG_BATCH, 20.0), _mop_row(CLASP, REG_BATCH, 10.0)],
			batches=[
				{
					"name": CG_BATCH,
					"reference_doctype": "Stock Entry",
					"reference_name": RECEIPT_SE,
					"custom_voucher_detail_no": "SED-1",
				}
			],
			se_details={"SED-1": RECEIPT_SE},
			receipt_ses=[RECEIPT_SE],
		)
		self.assertEqual(blocked, {CG_BATCH: RECEIPT_SE})


class TestBookMetalLossCustomerFindingGate(IntegrationTestCase):
	"""End-to-end through book_metal_loss: exclusion + redistribution, never a throw."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, mop_log_rows, gwt, r_gwt, blocked=None, manual_rows=None):
		doc = _DocStub(manual_rows=manual_rows)
		patches = [
			patch(f"{EIR}.frappe.db.get_all", return_value=mop_log_rows),
			# The other two gates stay out of the way in these cases.
			patch(f"{EIR}.get_loss_booking_map", return_value={}),
			patch(f"{EIR}.get_finding_category_map", return_value={}),
			patch(f"{EIR}.get_blocked_loss_variants", return_value=set()),
			patch(f"{EIR}.get_variant_of_map", return_value=VARIANTS),
			patch(
				f"{EIR}.get_blocked_finding_batches",
				return_value=blocked if blocked is not None else {},
			),
			patch(f"{EIR}.batch_priority_map", return_value={}),
			patch(f"{EIR}.get_batch_sre_headroom", return_value={}),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

		return EmployeeIR.book_metal_loss(
			doc,
			mwo="MWO-1",
			opt="MOP-1",
			gwt=gwt,
			r_gwt=r_gwt,
			allowed_loss_percentage=None,
		)

	def test_no_customer_finding_is_unchanged_behaviour(self):
		"""The guarantee that shipping this changes nothing on any site."""
		rows = [_mop_row(METAL, REG_BATCH, 80.0), _mop_row(CHAIN, "B-F", 20.0)]
		result = self._run(rows, gwt=100.0, r_gwt=98.0)

		by_item = {e["item_code"]: e for e in result}
		self.assertEqual(flt(by_item[METAL]["proportionally_loss"], 3), 1.600)
		self.assertEqual(flt(by_item[CHAIN]["proportionally_loss"], 3), 0.400)

	def test_customer_finding_excluded_and_metal_absorbs_full_loss(self):
		"""Metal 80 g + customer Chain 20 g, received 98 g of 100 g.

		The metal takes the whole 2.000 g — not the 1.600 g it would take if the
		customer's chain participated — so the booked total still matches the
		baseline and validate_loss_tables_required needs no change.
		"""
		rows = [_mop_row(METAL, REG_BATCH, 80.0), _mop_row(CHAIN, CG_BATCH, 20.0)]
		result = self._run(rows, gwt=100.0, r_gwt=98.0, blocked={CG_BATCH: RECEIPT_SE})

		by_batch = {e["batch_no"]: e for e in result}
		self.assertNotIn(
			CG_BATCH, by_batch, "customer finding must not appear in the pool"
		)
		self.assertEqual(flt(by_batch[REG_BATCH]["proportionally_loss"], 3), 2.000)
		self.assertEqual(flt(sum(e["proportionally_loss"] for e in result), 3), 2.000)

	def test_same_item_blocked_on_one_batch_only(self):
		"""Blocking is per BATCH, not per item: the company-owned chain still books."""
		rows = [_mop_row(CHAIN, CG_BATCH, 50.0), _mop_row(CHAIN, REG_BATCH, 50.0)]
		result = self._run(rows, gwt=100.0, r_gwt=98.0, blocked={CG_BATCH: RECEIPT_SE})

		by_batch = {e["batch_no"]: e for e in result}
		self.assertNotIn(CG_BATCH, by_batch)
		self.assertEqual(flt(by_batch[REG_BATCH]["proportionally_loss"], 3), 2.000)

	def test_never_throws_even_when_the_pool_is_emptied(self):
		"""Saving must always succeed — the submit-time explainer names the cause."""
		rows = [_mop_row(CHAIN, CG_BATCH, 20.0)]
		result = self._run(rows, gwt=100.0, r_gwt=98.0, blocked={CG_BATCH: RECEIPT_SE})
		self.assertEqual(result, [])


class TestValidateCustomerGoodsFindingLossRows(IntegrationTestCase):
	"""The manual table refusal — submit-only, operator-entered rows only."""

	@classmethod
	def setUpClass(cls):
		pass

	def _patch_gate(self, blocked):
		p = patch(f"{GATE}.get_blocked_finding_batches", return_value=blocked)
		p.start()
		self.addCleanup(p.stop)

	def test_throws_naming_item_batch_and_origin(self):
		self._patch_gate({CG_BATCH: RECEIPT_SE})
		doc = _DocStub(manual_rows=[_loss_row(CHAIN, CG_BATCH, idx=2)])

		with self.assertRaises(ValidationError) as caught:
			validate_customer_goods_finding_loss_rows(doc)

		message = str(caught.exception)
		self.assertIn(CHAIN, message)
		self.assertIn(CG_BATCH, message)
		self.assertIn(RECEIPT_SE, message)

	def test_allows_a_row_on_an_unblocked_batch(self):
		self._patch_gate({CG_BATCH: RECEIPT_SE})
		doc = _DocStub(manual_rows=[_loss_row(CHAIN, REG_BATCH)])
		validate_customer_goods_finding_loss_rows(doc)

	def test_automatic_table_is_not_inspected(self):
		"""book_metal_loss already excluded these; refusing here would punish the
		operator for a state they had no part in."""
		self._patch_gate({CG_BATCH: RECEIPT_SE})
		doc = _DocStub(auto_rows=[_loss_row(CHAIN, CG_BATCH)])
		validate_customer_goods_finding_loss_rows(doc)

	def test_issue_is_not_checked(self):
		self._patch_gate({CG_BATCH: RECEIPT_SE})
		doc = _DocStub(manual_rows=[_loss_row(CHAIN, CG_BATCH)], doc_type="Issue")
		validate_customer_goods_finding_loss_rows(doc)

	def test_empty_manual_table_costs_no_query(self):
		with patch(f"{GATE}.get_blocked_finding_batches") as resolver:
			validate_customer_goods_finding_loss_rows(_DocStub())
			resolver.assert_not_called()


class TestCustomerFindingGateIsWiredSubmitOnly(IntegrationTestCase):
	"""The gate must not run on save — an emptied pool is the normal case here."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_validate_does_not_call_the_manual_gate(self):
		import inspect

		source = inspect.getsource(EmployeeIR.validate)
		self.assertNotIn("validate_customer_goods_finding_loss_rows", source)

	def test_on_submit_calls_the_manual_gate(self):
		import inspect

		source = inspect.getsource(EmployeeIR.on_submit)
		self.assertIn("validate_customer_goods_finding_loss_rows", source)
