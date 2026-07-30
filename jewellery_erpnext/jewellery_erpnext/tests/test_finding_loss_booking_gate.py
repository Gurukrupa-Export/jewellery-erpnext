# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the per-operation finding-category loss booking gate.

A Department Operation carries a ``finding_loss_booking`` table of
``(finding_category, loss_booking)`` rows. A finding whose category is listed
with the flag off must not have process loss booked against it; its share of the
shortfall is absorbed by the remaining rows, so the booked total still equals
``gross_wt - received_gross_wt`` and the balance validators keep passing.

The contract is fail-open: an unlisted category, or an operation with no table,
behaves exactly as before. That is what protects every existing site.

DB-free per the suite convention — ``setUpClass`` is neutralised and every
``frappe`` lookup is patched.
"""

from unittest.mock import patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	finding_loss_gate,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.finding_loss_gate import (
	is_loss_booking_blocked,
	validate_loss_rows_against_gate,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
	EmployeeIR,
)

GATE = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.finding_loss_gate"
EIR = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir"

METAL = "M-G-22KT-91.9-Y"
CHAIN = "F-G-22KT-91.9-Y-CHA-KC-2.50 MM"
CLASP = "F-G-22KT-91.9-Y-CLA-LC-3.00 MM"


def _row(item_code, batch_no, qty, pcs=0):
	return frappe._dict(
		{"item_code": item_code, "batch_no": batch_no, "qty": qty, "pcs": pcs}
	)


class _DocStub:
	"""Stand-in for the pieces of Employee IR that book_metal_loss touches."""

	def __init__(self, operation="Casting", manual_rows=None):
		self.operation = operation
		self.manually_book_loss_details = manual_rows or []


class TestIsLossBookingBlocked(IntegrationTestCase):
	"""The pure predicate — no DB, no patching needed."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_blocked_when_category_listed_and_flag_off(self):
		self.assertTrue(
			is_loss_booking_blocked(CHAIN, {"Chains": 0}, {CHAIN: "Chains"})
		)

	def test_not_blocked_when_flag_on(self):
		self.assertFalse(
			is_loss_booking_blocked(CHAIN, {"Chains": 1}, {CHAIN: "Chains"})
		)

	def test_not_blocked_when_category_unlisted(self):
		# Fail-open: the operation gates Clasps, this item is a Chain.
		self.assertFalse(
			is_loss_booking_blocked(CHAIN, {"Clasps": 0}, {CHAIN: "Chains"})
		)

	def test_not_blocked_when_no_table_configured(self):
		self.assertFalse(is_loss_booking_blocked(CHAIN, {}, {CHAIN: "Chains"}))

	def test_metal_never_blocked(self):
		# The gate is finding-only; a metal item is never excluded even if some
		# category were somehow mapped to it.
		self.assertFalse(
			is_loss_booking_blocked(METAL, {"Chains": 0}, {METAL: "Chains"})
		)

	def test_not_blocked_when_item_has_no_category(self):
		self.assertFalse(is_loss_booking_blocked(CHAIN, {"Chains": 0}, {}))

	def test_handles_missing_item_code(self):
		self.assertFalse(is_loss_booking_blocked(None, {"Chains": 0}, {}))
		self.assertFalse(is_loss_booking_blocked("", {"Chains": 0}, {}))


class TestBookMetalLossFindingGate(IntegrationTestCase):
	"""End-to-end through book_metal_loss: exclusion + redistribution."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, mop_log_rows, gwt, r_gwt, booking_map=None, category_map=None):
		doc = _DocStub()
		patches = [
			patch(f"{EIR}.frappe.db.get_all", return_value=mop_log_rows),
			patch(
				f"{EIR}.get_loss_booking_map",
				return_value=booking_map if booking_map is not None else {},
			),
			patch(
				f"{EIR}.get_finding_category_map",
				return_value=category_map if category_map is not None else {},
			),
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

	def test_blocked_finding_excluded_and_metal_absorbs_full_loss(self):
		"""Metal 80g + Chain 20g, received 98g of 100g.

		With Chains gated off the chain books nothing and the metal takes the
		whole 2.000 g — not the 1.600 g it would take if the chain participated.
		"""
		rows = [_row(METAL, "B-M", 80.0), _row(CHAIN, "B-F", 20.0)]
		result = self._run(
			rows,
			gwt=100.0,
			r_gwt=98.0,
			booking_map={"Chains": 0},
			category_map={CHAIN: "Chains"},
		)

		by_item = {entry["item_code"]: entry for entry in result}
		self.assertNotIn(CHAIN, by_item, "gated finding must not appear in the pool")
		self.assertEqual(flt(by_item[METAL]["proportionally_loss"], 3), 2.000)
		self.assertEqual(
			flt(sum(e["proportionally_loss"] for e in result), 3),
			2.000,
			"booked total must still equal gross_wt - received_gross_wt",
		)

	def test_finding_participates_when_flag_on(self):
		"""Same weights, Chains ticked on => the pre-change proportional split."""
		rows = [_row(METAL, "B-M", 80.0), _row(CHAIN, "B-F", 20.0)]
		result = self._run(
			rows,
			gwt=100.0,
			r_gwt=98.0,
			booking_map={"Chains": 1},
			category_map={CHAIN: "Chains"},
		)

		by_item = {entry["item_code"]: entry for entry in result}
		self.assertEqual(flt(by_item[METAL]["proportionally_loss"], 3), 1.600)
		self.assertEqual(flt(by_item[CHAIN]["proportionally_loss"], 3), 0.400)
		self.assertEqual(flt(sum(e["proportionally_loss"] for e in result), 3), 2.000)

	def test_unlisted_category_books_loss(self):
		"""Fail-open: the operation gates Clasps only, so the Chain still books."""
		rows = [_row(METAL, "B-M", 80.0), _row(CHAIN, "B-F", 20.0)]
		result = self._run(
			rows,
			gwt=100.0,
			r_gwt=98.0,
			booking_map={"Clasps": 0},
			category_map={CHAIN: "Chains"},
		)

		by_item = {entry["item_code"]: entry for entry in result}
		self.assertEqual(flt(by_item[METAL]["proportionally_loss"], 3), 1.600)
		self.assertEqual(flt(by_item[CHAIN]["proportionally_loss"], 3), 0.400)

	def test_empty_table_is_unchanged_behaviour(self):
		"""No table configured => byte-identical to the pre-change split.

		This is the guarantee that shipping the feature changes nothing on any
		existing Department Operation.
		"""
		rows = [_row(METAL, "B-M", 80.0), _row(CHAIN, "B-F", 20.0)]
		result = self._run(rows, gwt=100.0, r_gwt=98.0, booking_map={})

		by_item = {entry["item_code"]: entry for entry in result}
		self.assertEqual(flt(by_item[METAL]["proportionally_loss"], 3), 1.600)
		self.assertEqual(flt(by_item[CHAIN]["proportionally_loss"], 3), 0.400)

	def test_only_the_blocked_category_is_excluded(self):
		"""Two finding categories, one gated: the other still shares the loss."""
		rows = [
			_row(METAL, "B-M", 50.0),
			_row(CHAIN, "B-F1", 30.0),
			_row(CLASP, "B-F2", 20.0),
		]
		result = self._run(
			rows,
			gwt=100.0,
			r_gwt=99.0,
			booking_map={"Chains": 0, "Clasps": 1},
			category_map={CHAIN: "Chains", CLASP: "Clasps"},
		)

		by_item = {entry["item_code"]: entry for entry in result}
		self.assertNotIn(CHAIN, by_item)
		# Pool is now 50 + 20 = 70; loss 1.0 splits 50/70 and 20/70.
		self.assertEqual(flt(by_item[METAL]["proportionally_loss"], 3), 0.714)
		self.assertEqual(flt(by_item[CLASP]["proportionally_loss"], 3), 0.286)
		self.assertEqual(flt(sum(e["proportionally_loss"] for e in result), 3), 1.000)

	def test_all_eligible_rows_blocked_throws(self):
		"""Nothing left to book against => a clear throw, not a silent empty table."""
		rows = [_row(CHAIN, "B-F", 20.0)]
		with self.assertRaises(ValidationError) as ctx:
			self._run(
				rows,
				gwt=20.0,
				r_gwt=19.0,
				booking_map={"Chains": 0},
				category_map={CHAIN: "Chains"},
			)
		self.assertIn("Chains", str(ctx.exception))

	def test_gain_on_receive_does_not_throw_when_all_blocked(self):
		"""r_gwt > gwt is not a shortfall, so there is nothing to attribute."""
		rows = [_row(CHAIN, "B-F", 20.0)]
		result = self._run(
			rows,
			gwt=20.0,
			r_gwt=21.0,
			booking_map={"Chains": 0},
			category_map={CHAIN: "Chains"},
		)
		self.assertEqual(result, [])

	def test_dg_rows_still_excluded_alongside_the_gate(self):
		"""The pre-existing M/F filter is preserved, not replaced."""
		rows = [
			_row(METAL, "B-M", 80.0),
			_row(CHAIN, "B-F", 20.0),
			_row("D-1-2-3", "B-D", 5.0, pcs=10),
			_row("G-1-2-3", "B-G", 5.0, pcs=10),
			_row("O-1-2-3", "B-O", 5.0),
		]
		result = self._run(
			rows,
			gwt=100.0,
			r_gwt=98.0,
			booking_map={"Chains": 0},
			category_map={CHAIN: "Chains"},
		)

		self.assertEqual([e["item_code"] for e in result], [METAL])
		self.assertEqual(flt(result[0]["proportionally_loss"], 3), 2.000)


class TestValidateLossRowsAgainstGate(IntegrationTestCase):
	"""The submit/validate guard covering both loss tables."""

	@classmethod
	def setUpClass(cls):
		pass

	def _doc(self, employee_rows=None, manual_rows=None, doc_type="Receive"):
		return frappe._dict(
			{
				"name": "EMP-IR-0001",
				"type": doc_type,
				"operation": "Casting",
				"employee_loss_details": employee_rows or [],
				"manually_book_loss_details": manual_rows or [],
			}
		)

	def _patch(self, booking_map, category_map):
		for target, value in (
			("get_loss_booking_map", booking_map),
			("get_finding_category_map", category_map),
		):
			p = patch(f"{GATE}.{target}", return_value=value)
			p.start()
			self.addCleanup(p.stop)

	def test_manual_row_on_blocked_category_throws(self):
		self._patch({"Chains": 0}, {CHAIN: "Chains"})
		doc = self._doc(manual_rows=[frappe._dict({"idx": 1, "item_code": CHAIN})])
		with self.assertRaises(ValidationError) as ctx:
			validate_loss_rows_against_gate(doc)
		message = str(ctx.exception)
		self.assertIn("Chains", message)
		self.assertIn("Manually Book Loss Details", message)

	def test_stale_auto_row_on_blocked_category_throws(self):
		"""Draft saved before the flag was flipped must fail at submit."""
		self._patch({"Chains": 0}, {CHAIN: "Chains"})
		doc = self._doc(employee_rows=[frappe._dict({"idx": 1, "item_code": CHAIN})])
		with self.assertRaises(ValidationError) as ctx:
			validate_loss_rows_against_gate(doc)
		self.assertIn("Employee Loss Details", str(ctx.exception))

	def test_allowed_category_passes(self):
		self._patch({"Chains": 1}, {CHAIN: "Chains"})
		doc = self._doc(manual_rows=[frappe._dict({"idx": 1, "item_code": CHAIN})])
		validate_loss_rows_against_gate(doc)

	def test_metal_row_passes(self):
		self._patch({"Chains": 0}, {METAL: None})
		doc = self._doc(manual_rows=[frappe._dict({"idx": 1, "item_code": METAL})])
		validate_loss_rows_against_gate(doc)

	def test_issue_type_is_skipped(self):
		# The gate is Receive-only; an Issue has no loss tables to police.
		self._patch({"Chains": 0}, {CHAIN: "Chains"})
		doc = self._doc(
			manual_rows=[frappe._dict({"idx": 1, "item_code": CHAIN})],
			doc_type="Issue",
		)
		validate_loss_rows_against_gate(doc)

	def test_no_table_configured_short_circuits(self):
		"""An operation with no gate must not even resolve item categories."""
		p = patch(f"{GATE}.get_loss_booking_map", return_value={})
		p.start()
		self.addCleanup(p.stop)
		category = patch(f"{GATE}.get_finding_category_map")
		mock_category = category.start()
		self.addCleanup(category.stop)

		doc = self._doc(manual_rows=[frappe._dict({"idx": 1, "item_code": CHAIN})])
		validate_loss_rows_against_gate(doc)
		mock_category.assert_not_called()


class TestGateMapBuilders(IntegrationTestCase):
	"""The two prefetch maps."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_loss_booking_map_shape(self):
		rows = [
			frappe._dict({"finding_category": "Chains", "loss_booking": 0}),
			frappe._dict({"finding_category": "Clasps", "loss_booking": 1}),
			frappe._dict({"finding_category": None, "loss_booking": 0}),
		]
		with patch(f"{GATE}.frappe.get_all", return_value=rows) as mock_get_all:
			result = finding_loss_gate.get_loss_booking_map("Casting")
		self.assertEqual(result, {"Chains": 0, "Clasps": 1})
		mock_get_all.assert_called_once()

	def test_loss_booking_map_without_operation_does_not_query(self):
		with patch(f"{GATE}.frappe.get_all") as mock_get_all:
			self.assertEqual(finding_loss_gate.get_loss_booking_map(None), {})
		mock_get_all.assert_not_called()

	def test_category_map_queries_findings_only(self):
		rows = [frappe._dict({"parent": CHAIN, "attribute_value": "Chains"})]
		with patch(f"{GATE}.frappe.get_all", return_value=rows) as mock_get_all:
			result = finding_loss_gate.get_finding_category_map(
				[METAL, CHAIN, "D-1-2-3", None]
			)
		self.assertEqual(result, {CHAIN: "Chains"})
		# Only the F-prefixed code is sent to the database.
		self.assertEqual(
			mock_get_all.call_args.kwargs["filters"]["parent"], ["in", [CHAIN]]
		)

	def test_category_map_without_findings_does_not_query(self):
		with patch(f"{GATE}.frappe.get_all") as mock_get_all:
			self.assertEqual(
				finding_loss_gate.get_finding_category_map([METAL, "D-1-2-3"]), {}
			)
		mock_get_all.assert_not_called()
