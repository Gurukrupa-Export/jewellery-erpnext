# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the blanket per-material loss booking gate.

A Department Operation carries four Check fields — "Don't Allow Loss Metal",
"Don't Allow Loss Diamond", "Don't Allow Loss Finding" and "Don't Allow Loss
Gemstone". A ticked box means no process loss may be booked against any item
whose ``Item.variant_of`` is that template.

The two loss paths behave differently, and that asymmetry is the point of most of
these tests:

  * **Automatic** — blocked rows drop out of the ``book_metal_loss`` pool BEFORE
    ``total_qty`` is summed, so the survivors absorb the blocked row's share and
    the booked total still equals ``gross_wt - received_gross_wt``. That is what
    keeps ``validate_loss_tables_required``'s 0.0005 g sum-match passing
    unchanged. This path NEVER throws -- saving must always succeed.
  * **Manual** — refused, and only from ``on_submit``.

The contract is fail-open: no box ticked means an operation behaves exactly as
before. That is what protects every existing site.

Matching is on EXACT ``variant_of``, so the loss and broken variants this app
mints ("ML", "FL", "DL", "GL", ...) are deliberately not caught.

DB-free per the suite convention — ``setUpClass`` is neutralised and every
``frappe`` lookup is patched. ``frappe.db.get_value`` is never blanket-patched
(it would hijack DocType meta loading); the gate's own resolvers are patched
instead.
"""

from unittest.mock import patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.material_loss_gate import (
	get_blocked_loss_variants,
	is_variant_loss_blocked,
	validate_loss_rows_against_material_gate,
	validate_material_gate_left_nothing_to_book,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
	EmployeeIR,
)

GATE = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.material_loss_gate"
EIR = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir"

METAL = "M-G-22KT-91.9-Y"
CHAIN = "F-G-22KT-91.9-Y-CHA-KC-2.50 MM"
DIAMOND = "D-RND-VVS-EF-1.00 MM"
METAL_LOSS = "ML-G-22KT-91.9-Y"

VARIANTS = {
	METAL: "M",
	CHAIN: "F",
	DIAMOND: "D",
	METAL_LOSS: "ML",
}


def _mop_row(item_code, batch_no, qty, pcs=0):
	return frappe._dict(
		{"item_code": item_code, "batch_no": batch_no, "qty": qty, "pcs": pcs}
	)


def _loss_row(item_code, idx=1):
	return frappe._dict({"item_code": item_code, "idx": idx})


class _DocStub:
	"""Stand-in for the pieces of Employee IR the gate touches."""

	def __init__(self, operation="Casting", manual_rows=None, auto_rows=None):
		self.operation = operation
		self.type = "Receive"
		self.flags = frappe._dict()
		self.employee_loss_details = auto_rows or []
		self.manually_book_loss_details = manual_rows or []


class TestIsVariantLossBlocked(IntegrationTestCase):
	"""The pure predicate — no DB, no patching needed."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_blocked_when_variant_flagged(self):
		self.assertTrue(is_variant_loss_blocked(METAL, {"M"}, VARIANTS))

	def test_not_blocked_when_variant_not_flagged(self):
		self.assertFalse(is_variant_loss_blocked(METAL, {"D"}, VARIANTS))

	def test_not_blocked_when_nothing_ticked(self):
		# Fail-open: the guarantee that shipping this changes nothing.
		self.assertFalse(is_variant_loss_blocked(METAL, set(), VARIANTS))

	def test_each_flag_matches_only_its_own_template(self):
		for item, variant in ((METAL, "M"), (DIAMOND, "D"), (CHAIN, "F")):
			self.assertTrue(is_variant_loss_blocked(item, {variant}, VARIANTS))
			others = {"M", "D", "F", "G"} - {variant}
			self.assertFalse(is_variant_loss_blocked(item, others, VARIANTS))

	def test_loss_variant_not_caught_by_metal_flag(self):
		# Exact variant_of equality: "ML" is not "M". Documented, deliberate.
		self.assertFalse(is_variant_loss_blocked(METAL_LOSS, {"M"}, VARIANTS))

	def test_not_blocked_when_variant_unknown(self):
		# A non-variant item (blank/absent variant_of) books loss as before.
		self.assertFalse(is_variant_loss_blocked("METAL LOSS", {"M"}, {}))
		self.assertFalse(is_variant_loss_blocked(METAL, {"M"}, {METAL: None}))

	def test_handles_missing_item_code(self):
		self.assertFalse(is_variant_loss_blocked(None, {"M"}, VARIANTS))
		self.assertFalse(is_variant_loss_blocked("", {"M"}, VARIANTS))


class TestGetBlockedLossVariants(IntegrationTestCase):
	"""The Department Operation reader."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_no_operation_short_circuits_without_a_query(self):
		with patch(f"{GATE}.frappe.db.get_value") as get_value:
			self.assertEqual(get_blocked_loss_variants(None), set())
			get_value.assert_not_called()

	def test_missing_operation_row_returns_empty_set(self):
		with patch(f"{GATE}.frappe.db.get_value", return_value=None):
			self.assertEqual(get_blocked_loss_variants("Casting"), set())

	def test_nothing_ticked_returns_empty_set(self):
		flags = frappe._dict(
			{
				"dont_allow_loss_metal": 0,
				"dont_allow_loss_diamond": 0,
				"dont_allow_loss_finding": 0,
				"dont_allow_loss_gemstone": 0,
			}
		)
		with patch(f"{GATE}.frappe.db.get_value", return_value=flags):
			self.assertEqual(get_blocked_loss_variants("Casting"), set())

	def test_ticked_flags_map_to_their_templates(self):
		flags = frappe._dict(
			{
				"dont_allow_loss_metal": 1,
				"dont_allow_loss_diamond": 0,
				"dont_allow_loss_finding": 1,
				"dont_allow_loss_gemstone": 0,
			}
		)
		with patch(f"{GATE}.frappe.db.get_value", return_value=flags):
			self.assertEqual(get_blocked_loss_variants("Casting"), {"M", "F"})

	def test_all_four_ticked(self):
		flags = frappe._dict(
			{
				"dont_allow_loss_metal": 1,
				"dont_allow_loss_diamond": 1,
				"dont_allow_loss_finding": 1,
				"dont_allow_loss_gemstone": 1,
			}
		)
		with patch(f"{GATE}.frappe.db.get_value", return_value=flags):
			self.assertEqual(get_blocked_loss_variants("Casting"), {"M", "D", "F", "G"})


class TestBookMetalLossMaterialGate(IntegrationTestCase):
	"""End-to-end through book_metal_loss: exclusion + redistribution."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, mop_log_rows, gwt, r_gwt, blocked=None, manual_rows=None):
		doc = _DocStub(manual_rows=manual_rows)
		patches = [
			patch(f"{EIR}.frappe.db.get_all", return_value=mop_log_rows),
			# The finding-category gate stays out of the way in these cases.
			patch(f"{EIR}.get_loss_booking_map", return_value={}),
			patch(f"{EIR}.get_finding_category_map", return_value={}),
			patch(
				f"{EIR}.get_blocked_loss_variants",
				return_value=blocked if blocked is not None else set(),
			),
			patch(f"{EIR}.get_variant_of_map", return_value=VARIANTS),
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

	def test_nothing_ticked_is_unchanged_behaviour(self):
		"""The guarantee that shipping this changes nothing on any site."""
		rows = [_mop_row(METAL, "B-M", 80.0), _mop_row(CHAIN, "B-F", 20.0)]
		result = self._run(rows, gwt=100.0, r_gwt=98.0)

		by_item = {e["item_code"]: e for e in result}
		self.assertEqual(flt(by_item[METAL]["proportionally_loss"], 3), 1.600)
		self.assertEqual(flt(by_item[CHAIN]["proportionally_loss"], 3), 0.400)

	def test_blocked_metal_excluded_and_finding_absorbs_full_loss(self):
		"""Metal 80g + Chain 20g, received 98g of 100g, Metal blocked.

		The chain takes the whole 2.000 g — not the 0.400 g it would take if the
		metal participated — so the booked total still matches the baseline.
		"""
		rows = [_mop_row(METAL, "B-M", 80.0), _mop_row(CHAIN, "B-F", 20.0)]
		result = self._run(rows, gwt=100.0, r_gwt=98.0, blocked={"M"})

		by_item = {e["item_code"]: e for e in result}
		self.assertNotIn(METAL, by_item, "blocked metal must not appear in the pool")
		self.assertEqual(flt(by_item[CHAIN]["proportionally_loss"], 3), 2.000)
		self.assertEqual(
			flt(sum(e["proportionally_loss"] for e in result), 3),
			2.000,
			"booked total must still equal gross_wt - received_gross_wt",
		)

	def test_blocked_finding_excluded_and_metal_absorbs_full_loss(self):
		rows = [_mop_row(METAL, "B-M", 80.0), _mop_row(CHAIN, "B-F", 20.0)]
		result = self._run(rows, gwt=100.0, r_gwt=98.0, blocked={"F"})

		by_item = {e["item_code"]: e for e in result}
		self.assertNotIn(CHAIN, by_item)
		self.assertEqual(flt(by_item[METAL]["proportionally_loss"], 3), 2.000)

	def test_diamond_flag_does_not_disturb_the_auto_pool(self):
		"""The pool is already M/F-only, so a D flag changes nothing here."""
		rows = [_mop_row(METAL, "B-M", 80.0), _mop_row(CHAIN, "B-F", 20.0)]
		result = self._run(rows, gwt=100.0, r_gwt=98.0, blocked={"D"})

		by_item = {e["item_code"]: e for e in result}
		self.assertEqual(flt(by_item[METAL]["proportionally_loss"], 3), 1.600)
		self.assertEqual(flt(by_item[CHAIN]["proportionally_loss"], 3), 0.400)

	def test_loss_variant_still_books_under_metal_flag(self):
		"""``ML-`` passes the item-code prefix filter and is not variant "M"."""
		rows = [_mop_row(METAL, "B-M", 50.0), _mop_row(METAL_LOSS, "B-ML", 50.0)]
		result = self._run(rows, gwt=100.0, r_gwt=99.0, blocked={"M"})

		by_item = {e["item_code"]: e for e in result}
		self.assertNotIn(METAL, by_item)
		self.assertEqual(flt(by_item[METAL_LOSS]["proportionally_loss"], 3), 1.000)

	def test_all_eligible_rows_blocked_books_nothing_without_throwing(self):
		"""An emptied pool is NOT an error on the save path.

		On a metal-only operation with Don't Allow Loss Metal ticked this is the
		normal case, not an edge case -- throwing here made the document
		unsaveable. The submit-time validate_material_gate_left_nothing_to_book is
		what explains the empty table.
		"""
		rows = [_mop_row(METAL, "B-M", 20.0)]
		result = self._run(rows, gwt=20.0, r_gwt=19.0, blocked={"M"})
		self.assertEqual(result, [])

	def test_no_throw_when_manual_booking_already_covers_the_shortfall(self):
		"""The whole 1.000 g is already booked by hand against an allowed item."""
		rows = [_mop_row(METAL, "B-M", 20.0)]
		manual = [
			frappe._dict(
				{
					"item_code": DIAMOND,
					"manufacturing_work_order": "MWO-1",
					"stock_uom": "Carat",
					"proportionally_loss": 5.0,
				}
			)
		]
		result = self._run(
			rows, gwt=20.0, r_gwt=19.0, blocked={"M"}, manual_rows=manual
		)
		self.assertEqual(result, [], "no auto rows, but no throw either")

	def test_no_throw_when_receive_gained_weight(self):
		"""A gain books no loss, so there is nothing to attribute."""
		rows = [_mop_row(METAL, "B-M", 20.0)]
		result = self._run(rows, gwt=20.0, r_gwt=20.0, blocked={"M"})
		self.assertEqual(result, [])


class TestValidateLossRowsAgainstMaterialGate(IntegrationTestCase):
	"""The submit-only validator. Manual table only -- never the automatic one."""

	@classmethod
	def setUpClass(cls):
		pass

	def _validate(self, doc, blocked):
		patches = [
			patch(f"{GATE}.get_blocked_loss_variants", return_value=blocked),
			patch(f"{GATE}.get_variant_of_map", return_value=VARIANTS),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		validate_loss_rows_against_material_gate(doc)

	def test_issue_is_ignored(self):
		doc = _DocStub(manual_rows=[_loss_row(METAL)])
		doc.type = "Issue"
		self._validate(doc, {"M"})

	def test_nothing_ticked_passes(self):
		doc = _DocStub(manual_rows=[_loss_row(METAL)])
		self._validate(doc, set())

	def test_allowed_rows_pass(self):
		doc = _DocStub(auto_rows=[_loss_row(METAL)], manual_rows=[_loss_row(DIAMOND)])
		self._validate(doc, {"F", "G"})

	def test_blocked_manual_row_throws_with_delete_wording(self):
		doc = _DocStub(manual_rows=[_loss_row(DIAMOND, idx=2)])
		with self.assertRaises(ValidationError) as ctx:
			self._validate(doc, {"D"})

		message = str(ctx.exception)
		self.assertIn("Manually Book Loss Details row #2", message)
		self.assertIn("Don't Allow Loss Diamond", message)
		self.assertIn(DIAMOND, message)
		self.assertIn("Delete this row", message)

	def test_blocked_auto_row_is_ignored(self):
		"""The automatic table is machine-written and never refused.

		book_metal_loss already excludes blocked items while building it, so the
		only way a blocked row gets here is a draft saved before the flag was
		ticked. Re-saving rebuilds it; throwing would punish the operator for an
		admin's change. This is the regression guard for that decision.
		"""
		doc = _DocStub(auto_rows=[_loss_row(METAL, idx=1)])
		self._validate(doc, {"M"})

	def test_blocked_auto_row_ignored_even_alongside_an_allowed_manual_row(self):
		doc = _DocStub(
			auto_rows=[_loss_row(METAL, idx=1)], manual_rows=[_loss_row(DIAMOND)]
		)
		self._validate(doc, {"M"})

	def test_unknown_variant_row_passes(self):
		doc = _DocStub(manual_rows=[_loss_row("METAL LOSS")])
		self._validate(doc, {"M", "D", "F", "G"})


class TestMaterialGateLeftNothingToBook(IntegrationTestCase):
	"""The submit-time explanation for an automatic table the flags emptied."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, doc, blocked, balance_items):
		patches = [
			patch(f"{GATE}.get_blocked_loss_variants", return_value=blocked),
			patch(f"{GATE}.get_variant_of_map", return_value=VARIANTS),
			patch(
				f"{GATE}.frappe.get_all",
				return_value=[{"item_code": i} for i in balance_items],
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		validate_material_gate_left_nothing_to_book(doc)

	@staticmethod
	def _doc(gross=20.0, received=19.0, auto=None, manual=None):
		doc = _DocStub(auto_rows=auto, manual_rows=manual)
		doc.employee_ir_operations = [
			frappe._dict(
				{
					"gross_wt": gross,
					"received_gross_wt": received,
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-1",
				}
			)
		]
		return doc

	def test_throws_when_every_eligible_item_is_blocked(self):
		with self.assertRaises(ValidationError) as ctx:
			self._run(self._doc(), {"M"}, [METAL])

		message = str(ctx.exception)
		self.assertIn("Don't Allow Loss Metal", message)
		self.assertIn("MWO-1", message)
		self.assertIn("Manually Book Loss Details", message)

	def test_silent_when_an_eligible_item_survived_the_gate(self):
		"""Then the empty table has some other cause -- do not blame the flags."""
		self._run(self._doc(), {"M"}, [METAL, CHAIN])

	def test_silent_when_loss_was_booked_automatically(self):
		self._run(self._doc(auto=[_loss_row(CHAIN)]), {"M"}, [METAL])

	def test_silent_when_loss_was_booked_manually(self):
		self._run(self._doc(manual=[_loss_row(DIAMOND)]), {"M"}, [METAL])

	def test_silent_when_there_is_no_shortfall(self):
		self._run(self._doc(gross=20.0, received=20.0), {"M"}, [METAL])

	def test_silent_when_nothing_is_ticked(self):
		self._run(self._doc(), set(), [METAL])

	def test_silent_for_issue(self):
		doc = self._doc()
		doc.type = "Issue"
		self._run(doc, {"M"}, [METAL])

	def test_silent_when_balance_has_no_eligible_items(self):
		"""D/G never enter the automatic pool, so they cannot have been gated out."""
		self._run(self._doc(), {"D"}, [DIAMOND])
