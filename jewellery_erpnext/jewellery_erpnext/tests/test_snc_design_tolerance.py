# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""F6 -- a finished piece that differs from its design beyond tolerance needs an approver.

KLHGX62F1119 shipped two diamonds and a finding short of its design, with no record: 0.396 ct
of Natural diamond against a 0.452-0.520 ct band, 5.440 g of metal against a 3.787-4.357 g Net
band. The Department IR tolerance check exists but is armed per department (none on KGJPL) and
skips diamond bands scoped to a diamond type. The SNC now checks the finished piece against the
same PMO bands, scoped bands included, when its department has product tolerance switched on.

Pure-logic: the bands, item facts and department flag are stubbed; nothing is written. The band
figures below are PMO-KGJPL-EA02652-001-0002's own.
"""

import unittest
from unittest.mock import patch

import frappe

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events import (
	product_tolerance as pt,
)

PMO = "PMO-KGJPL-EA02652-001-0002"
DEPARTMENT = "Tagging - KGJPL"
GOLD_22 = "M-G-22KT-91.75-Y"
FINDING = "F-SCREW-22KT"
NATURAL = "D-NT-RO-6B-+6.5-7"
LAB = "D-LG-RO-6B-+6.5-7"
RUBY = "G-RUBY-OV"
EMERALD = "G-EMERALD-OV"

FACTS = {
	GOLD_22: {"variant_of": "M", "scope": {}},
	FINDING: {"variant_of": "F", "scope": {}},
	NATURAL: {"variant_of": "D", "scope": {"diamond_type": "Natural"}},
	LAB: {"variant_of": "D", "scope": {"diamond_type": "Lab Grown"}},
	RUBY: {
		"variant_of": "G",
		"scope": {"gemstone_type": "Ruby", "gemstone_shape": "Oval"},
	},
	EMERALD: {
		"variant_of": "G",
		"scope": {"gemstone_type": "Emerald", "gemstone_shape": "Oval"},
	},
}

BANDS = {
	"metal": [
		frappe._dict(
			metal_type="Gold",
			weight_type="Net Weight",
			from_tolerance_wt=3.787,
			to_tolerance_wt=4.357,
		)
	],
	"diamond": [
		frappe._dict(
			weight_type="Weight wise",
			diamond_type="Natural",
			from_tolerance_wt=0.452,
			to_tolerance_wt=0.520,
		)
	],
}


def _row(item, qty, piece=1):
	return frappe._dict(row_material=item, qty=qty, id=piece)


def _snc(*rows, reason=None):
	return frappe._dict(
		doctype="Serial Number Creator",
		parent_manufacturing_order=PMO,
		department=DEPARTMENT,
		manufacturing_work_order="MWO-1",
		manufacturing_operation="MOP-1",
		fg_details=list(rows),
		custom_tolerance_override_reason=reason,
	)


#: The piece as designed: 4.072 g metal, 0.486 ct Natural diamond.
DESIGNED = (_row(GOLD_22, 4.072), _row(NATURAL, 0.486))
#: KLHGX62F1119 as built.
AS_BUILT = (_row(GOLD_22, 5.44), _row(NATURAL, 0.396))


class _ToleranceCase(unittest.TestCase):
	armed = 1
	bands = BANDS
	roles = ()

	def setUp(self):
		def get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Parent Manufacturing Order" and fieldname == "metal_type":
				return "Gold"
			return None

		self.bands_read = patch.object(
			pt, "get_tolerance_bands", side_effect=lambda pmos: {PMO: self.bands}
		)
		patches = [
			self.bands_read,
			patch.object(pt, "_item_facts", side_effect=lambda codes: FACTS),
			patch(f"{pt.__name__}.frappe.db.get_value", side_effect=get_value),
			patch(
				f"{pt.__name__}.frappe.get_cached_value",
				side_effect=lambda *a, **k: self.armed,
			),
			patch(
				f"{pt.__name__}.frappe.get_roles",
				side_effect=lambda *a, **k: list(self.roles),
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)


class TestTheFinishedPieceIsChecked(_ToleranceCase):
	def test_klhgx62f1119_as_built_fails_on_metal_and_diamond(self):
		failures = pt.get_snc_tolerance_failures(_snc(*AS_BUILT))
		self.assertEqual(len(failures), 2, failures)
		self.assertTrue(any("Metal" in f and "5.44" in f for f in failures), failures)
		self.assertTrue(
			any("Diamond" in f and "0.396" in f for f in failures), failures
		)

	def test_the_piece_as_designed_passes(self):
		self.assertEqual(pt.get_snc_tolerance_failures(_snc(*DESIGNED)), [])

	def test_a_scoped_band_is_checked_against_its_own_subtotal(self):
		"""0.47 ct Natural + 0.30 ct Lab Grown: the Natural band sees 0.47, not the 0.77 total.
		The Department IR check skips a scoped band altogether."""
		failures = pt.get_snc_tolerance_failures(
			_snc(_row(GOLD_22, 4.072), _row(NATURAL, 0.47), _row(LAB, 0.30))
		)
		self.assertEqual(failures, [])

	def test_a_piece_missing_a_designed_stone_entirely_fails(self):
		"""Zero is exempt on a Department IR row (refined metal); on a finished piece it is the gap."""
		failures = pt.get_snc_tolerance_failures(_snc(_row(GOLD_22, 4.072)))
		self.assertEqual(len(failures), 1, failures)
		self.assertIn("Diamond", failures[0])

	def test_a_missing_finding_shows_in_the_net_weight(self):
		"""A Net band covers metal plus findings, so a 1.570 g finding left out takes it below."""
		built = pt.get_snc_tolerance_failures(
			_snc(_row(GOLD_22, 2.502), _row(NATURAL, 0.486))
		)
		with_finding = pt.get_snc_tolerance_failures(
			_snc(_row(GOLD_22, 2.502), _row(FINDING, 1.570), _row(NATURAL, 0.486))
		)
		self.assertEqual(len(built), 1, built)
		self.assertEqual(with_finding, [])

	def test_each_piece_is_checked_on_its_own(self):
		failures = pt.get_snc_tolerance_failures(
			_snc(
				_row(GOLD_22, 4.072, piece=1),
				_row(NATURAL, 0.486, piece=1),
				_row(GOLD_22, 4.072, piece=2),
				_row(NATURAL, 0.396, piece=2),
			)
		)
		self.assertEqual(len(failures), 1, failures)
		self.assertIn("piece 2", failures[0])


class TestEachScopeIsItsOwnRequirement(_ToleranceCase):
	"""Two diamond types banded side by side: one in range must not excuse the other."""

	bands = {
		"diamond": [
			frappe._dict(
				weight_type="Weight wise",
				diamond_type="Natural",
				from_tolerance_wt=0.452,
				to_tolerance_wt=0.520,
			),
			frappe._dict(
				weight_type="Weight wise",
				diamond_type="Lab Grown",
				from_tolerance_wt=0.28,
				to_tolerance_wt=0.32,
			),
		]
	}

	def test_a_short_natural_is_caught_even_with_lab_stones_in_range(self):
		failures = pt.get_snc_tolerance_failures(
			_snc(_row(GOLD_22, 4.072), _row(NATURAL, 0.396), _row(LAB, 0.30))
		)
		self.assertEqual(len(failures), 1, failures)
		self.assertIn("0.396", failures[0])

	def test_both_scopes_in_range_pass(self):
		self.assertEqual(
			pt.get_snc_tolerance_failures(
				_snc(_row(GOLD_22, 4.072), _row(NATURAL, 0.486), _row(LAB, 0.30))
			),
			[],
		)


class TestGemstoneScopes(_ToleranceCase):
	bands = {
		"gemstone": [
			frappe._dict(
				weight_type="Gemstone Type Range",
				gemstone_type="Ruby",
				from_tolerance_wt=0.9,
				to_tolerance_wt=1.1,
			)
		]
	}

	def test_a_type_band_sees_only_that_type(self):
		self.assertEqual(
			pt.get_snc_tolerance_failures(
				_snc(_row(GOLD_22, 4.0), _row(RUBY, 1.0), _row(EMERALD, 0.5))
			),
			[],
		)

	def test_a_type_band_catches_that_type_short(self):
		failures = pt.get_snc_tolerance_failures(
			_snc(_row(GOLD_22, 4.0), _row(RUBY, 0.5))
		)
		self.assertEqual(len(failures), 1, failures)


class TestTheGateAtSubmit(_ToleranceCase):
	def test_outside_tolerance_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			pt.validate_snc_design_tolerance(_snc(*AS_BUILT))

	def test_a_reason_without_the_role_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			pt.validate_snc_design_tolerance(_snc(*AS_BUILT, reason="client accepted"))

	def test_the_role_without_a_reason_is_refused(self):
		self.roles = (pt.TOLERANCE_APPROVER_ROLE,)
		with self.assertRaises(frappe.ValidationError):
			pt.validate_snc_design_tolerance(_snc(*AS_BUILT))

	def test_an_approver_with_a_reason_may_submit_and_is_recorded(self):
		self.roles = (pt.TOLERANCE_APPROVER_ROLE,)
		snc = _snc(*AS_BUILT, reason="client accepted the lighter setting")
		pt.validate_snc_design_tolerance(snc)
		self.assertEqual(snc.custom_tolerance_override_by, frappe.session.user)

	def test_a_piece_within_tolerance_needs_no_approver(self):
		snc = _snc(*DESIGNED)
		pt.validate_snc_design_tolerance(snc)
		self.assertIsNone(snc.custom_tolerance_override_by)


class TestArmedPerDepartment(_ToleranceCase):
	armed = 0

	def test_a_department_without_product_tolerance_is_not_checked(self):
		"""No KGJPL department has it on today, so shipping behaviour changes only when one does."""
		pt.validate_snc_design_tolerance(_snc(*AS_BUILT))
		self.bands_read.target.get_tolerance_bands.assert_not_called()
