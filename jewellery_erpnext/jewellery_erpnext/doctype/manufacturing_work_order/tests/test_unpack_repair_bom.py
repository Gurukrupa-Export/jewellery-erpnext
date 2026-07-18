"""
Pure-logic unit tests for the repair-unpack BOM resolver
(ManufacturingWorkOrder._resolve_repair_order_bom).

Run with:
  bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.tests.test_unpack_repair_bom

Note: frappe.db is a LocalProxy, so a default patch() mints async-mock children.
We inject an explicit MagicMock via patch(target, obj).
"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	ManufacturingWorkOrder,
)

MOD = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order"


def _resolve(pmo_value, ro_bom, requested_fields=None):
	"""Call _resolve_repair_order_bom with frappe.db.get_value mocked.

	pmo_value: what get_value returns for the PMO row -- (order_form_type, order_form_id) or None.
	ro_bom   : what get_value returns for the Repair Order's BOM field (the design BOM).
	requested_fields: optional list the caller can inspect to assert WHICH Repair Order field
	    was read (guards against regressing to new_bom).
	"""

	def gv(dt, name, fieldname):
		if dt == "Parent Manufacturing Order":
			return pmo_value
		if dt == "Repair Order":
			if requested_fields is not None:
				requested_fields.append(fieldname)
			return ro_bom
		return None

	mock_db = MagicMock()
	mock_db.get_value.side_effect = gv
	fake_self = SimpleNamespace(manufacturing_order="PMO-1")
	with patch(f"{MOD}.frappe.db", mock_db):
		return ManufacturingWorkOrder._resolve_repair_order_bom(fake_self)


class TestResolveRepairOrderBom(unittest.TestCase):
	def test_returns_design_bom_for_repair_order(self):
		bom, pmo = _resolve(("Repair Order", "RO-1"), "BOM-X")
		self.assertEqual(bom, "BOM-X")
		self.assertEqual(pmo, "PMO-1")

	def test_reads_the_design_bom_field_not_new_bom(self):
		"""new_bom is only ever written when required_design == 'No', so a Manual/CAD
		repair never has one. Requiring it blocked the unpack outright -- the resolver
		must read the design ``bom`` link instead."""
		asked = []
		_resolve(("Repair Order", "RO-1"), "BOM-X", requested_fields=asked)
		self.assertEqual(asked, ["bom"])

	def test_resolves_when_repair_order_has_no_new_bom(self):
		# The Manual-design case: new_bom absent is irrelevant, only `bom` is consulted.
		bom, _ = _resolve(("Repair Order", "ORD/RO/00021-2"), "BOM-EA02216-001-001")
		self.assertEqual(bom, "BOM-EA02216-001-001")

	def test_throws_when_order_form_is_not_repair_order(self):
		# order_form_type is "Order", not "Repair Order".
		with self.assertRaises(frappe.ValidationError):
			_resolve(("Order", "ORD-1"), "BOM-X")

	def test_throws_when_no_order_form_id(self):
		with self.assertRaises(frappe.ValidationError):
			_resolve(("Repair Order", None), "BOM-X")

	def test_throws_when_pmo_missing(self):
		# get_value returns None (PMO row not found) -> (None, None) -> not a Repair Order.
		with self.assertRaises(frappe.ValidationError):
			_resolve(None, "BOM-X")

	def test_throws_when_repair_order_has_no_bom(self):
		with self.assertRaises(frappe.ValidationError):
			_resolve(("Repair Order", "RO-1"), None)


def _check_dept(sn_dept, mwo_dept):
	"""Run _assert_serial_in_department with Warehouse.department mocked to sn_dept."""
	mock_db = MagicMock()
	mock_db.get_value.return_value = sn_dept
	fake_self = SimpleNamespace(department=mwo_dept, serial_no="SN1")
	with patch(f"{MOD}.frappe.db", mock_db):
		ManufacturingWorkOrder._assert_serial_in_department(fake_self, "WH-1")


class TestAssertSerialInDepartment(unittest.TestCase):
	def test_matching_department_ok(self):
		_check_dept("Dept A", "Dept A")  # no throw

	def test_mismatched_department_throws(self):
		with self.assertRaises(frappe.ValidationError):
			_check_dept("Dept A", "Dept B")

	def test_empty_string_and_none_are_equal(self):
		# "" (warehouse) and None (MWO) normalize together -> allowed.
		_check_dept("", None)
		_check_dept(None, None)

	def test_serial_wh_without_department_but_mwo_has_throws(self):
		with self.assertRaises(frappe.ValidationError):
			_check_dept(None, "Dept A")


def _check_bookable(item_tracking):
	"""Run _assert_components_bookable with Item (has_serial_no, serial_no_series) mocked.

	item_tracking: dict item_code -> (has_serial_no, serial_no_series).
	"""

	def gv(dt, name, fieldname):
		return item_tracking.get(name)

	mock_db = MagicMock()
	mock_db.get_value.side_effect = gv
	fake_self = SimpleNamespace()
	with patch(f"{MOD}.frappe.db", mock_db):
		ManufacturingWorkOrder._assert_components_bookable(
			fake_self, list(item_tracking.keys()), "BOM-X"
		)


class TestAssertComponentsBookable(unittest.TestCase):
	def test_batch_and_seriesed_serial_items_ok(self):
		# batch item (0, None) and serial item WITH a series -> allowed.
		_check_bookable({"BATCH-ITEM": (0, None), "SER-ITEM": (1, "SER-.###")})

	def test_serial_item_without_series_throws(self):
		with self.assertRaises(frappe.ValidationError):
			_check_bookable({"M-METAL": (0, None), "STONE-DV": (1, None)})

	def test_empty_bom_ok(self):
		_check_bookable({})


# ---------------------------------------------------------------------------
# _resolve_full_repair_components -- the repair unpack must book the FULL item
# (all metal + every diamond group + findings), resolved from the design BOM's
# detail tables, not the reduced repair new_bom.
# ---------------------------------------------------------------------------


class _Row(dict):
	"""Minimal stand-in for a BOM detail child row: dict-backed .get() plus
	attribute set (so the resolver can inject `diamond_grade` / read
	`item_variant`)."""

	__getattr__ = dict.get

	def __setattr__(self, key, value):
		self[key] = value


class _FakeBOM:
	def __init__(self, tables, gross=60.653, qty=1.0, bom_type="Template"):
		self._tables = tables
		self.gross_weight = gross
		self.quantity = qty
		self.bom_type = bom_type

	def get(self, key, default=None):
		return self._tables.get(key, [] if default is None else default)


def _resolve_full(
	design_tables,
	*,
	design_bom="BOM-DESIGN",
	diamond_grade="6B",
	set_variant_fn=None,
):
	"""Call _resolve_full_repair_components with frappe.db / frappe.get_doc /
	set_item_variant mocked. set_item_variant is faked to resolve each row's
	item_variant to its ``expect`` value (simulating variant resolution).

	The design BOM is passed in by _resolve_repair_order_bom, which owns the Repair
	Order lookup and its validation."""

	def gv(dt, name, fieldname=None):
		if dt == "Parent Manufacturing Order":
			return diamond_grade
		return None

	def default_set_variant(bom):
		for tbl in (
			"metal_detail",
			"diamond_detail",
			"finding_detail",
			"gemstone_detail",
		):
			for row in bom.get(tbl) or []:
				row.item_variant = row.get("expect")

	fn = set_variant_fn or default_set_variant
	fake_bom = _FakeBOM(design_tables)
	mock_db = MagicMock()
	mock_db.get_value.side_effect = gv
	fake_self = SimpleNamespace()
	with patch(f"{MOD}.frappe.db", mock_db), patch(
		f"{MOD}.frappe.get_doc", return_value=fake_bom
	), patch("jewellery_erpnext.jewellery_erpnext.doc_events.bom.set_item_variant", fn):
		return ManufacturingWorkOrder._resolve_full_repair_components(
			fake_self, "PMO-1", design_bom
		)


def _necklace_tables():
	return {
		"metal_detail": [_Row(item="M", quantity=53.228, expect="M-G-22KT-91.9-Y")],
		"diamond_detail": [
			_Row(
				item="D",
				diamond_sieve_size="+6-6.5",
				quantity=3.456,
				expect="D-NT-RO-6B-+6-6.5",
			),
			_Row(
				item="D",
				diamond_sieve_size="+7-7.5",
				quantity=0.48,
				expect="D-NT-RO-6B-+7-7.5",
			),
			_Row(
				item="D",
				diamond_sieve_size="+7.5-8",
				quantity=0.86,
				expect="D-NT-RO-6B-+7.5-8",
			),
			_Row(
				item="D",
				diamond_sieve_size="+8-8.5",
				quantity=2.73,
				expect="D-NT-RO-6B-+8-8.5",
			),
		],
		"finding_detail": [
			_Row(
				item="F",
				finding_type="9 Highway Chain",
				quantity=5.92,
				expect="F-G-22KT-91.9-Y-CHA-9HC-12.00 INCH",
			)
		],
	}


class TestResolveFullRepairComponents(unittest.TestCase):
	def test_returns_full_component_set(self):
		comps, gross, qty = _resolve_full(_necklace_tables())
		self.assertEqual(gross, 60.653)
		self.assertEqual(qty, 1.0)
		self.assertEqual(len(comps), 6)
		by = {c["item_code"]: c["qty"] for c in comps}
		self.assertEqual(by["M-G-22KT-91.9-Y"], 53.228)
		self.assertEqual(by["D-NT-RO-6B-+8-8.5"], 2.73)
		self.assertEqual(by["F-G-22KT-91.9-Y-CHA-9HC-12.00 INCH"], 5.92)
		# All 4 diamond groups present -- the bug booked only 1.
		self.assertEqual(sum(1 for c in comps if c["item_code"].startswith("D-")), 4)

	def test_injects_diamond_grade_from_pmo(self):
		seen = {}

		def capture(bom):
			for row in bom.get("diamond_detail"):
				seen[row.get("diamond_sieve_size")] = row.get("diamond_grade")
				row.item_variant = row.get("expect")

		_resolve_full(_necklace_tables(), diamond_grade="6B", set_variant_fn=capture)
		self.assertTrue(seen and all(g == "6B" for g in seen.values()))

	def test_preserves_pre_set_row_grade(self):
		tables = _necklace_tables()
		tables["diamond_detail"][0]["diamond_grade"] = "6AB"  # already graded
		seen = {}

		def capture(bom):
			for row in bom.get("diamond_detail"):
				seen[row.get("diamond_sieve_size")] = row.get("diamond_grade")
				row.item_variant = row.get("expect")

		_resolve_full(tables, diamond_grade="6B", set_variant_fn=capture)
		self.assertEqual(seen["+6-6.5"], "6AB")  # not overwritten
		self.assertEqual(seen["+7-7.5"], "6B")  # injected

	def test_aggregates_duplicate_variants(self):
		tables = {
			"diamond_detail": [
				_Row(
					item="D",
					diamond_sieve_size="+8-8.5",
					quantity=1.0,
					expect="D-NT-RO-6B-+8-8.5",
				),
				_Row(
					item="D",
					diamond_sieve_size="+8-8.5",
					quantity=1.73,
					expect="D-NT-RO-6B-+8-8.5",
				),
			]
		}
		comps, *_ = _resolve_full(tables)
		self.assertEqual(len(comps), 1)
		self.assertEqual(comps[0]["item_code"], "D-NT-RO-6B-+8-8.5")
		self.assertAlmostEqual(comps[0]["qty"], 2.73)

	def test_skips_zero_qty_and_unresolved_rows(self):
		tables = {
			"metal_detail": [
				_Row(item="M", quantity=53.228, expect="M-G-22KT-91.9-Y"),
				_Row(item="M", quantity=0.0, expect="M-ZERO"),  # zero qty -> skip
				_Row(item="M", quantity=1.0, expect=None),  # unresolved -> skip
			]
		}
		comps, *_ = _resolve_full(tables)
		self.assertEqual([c["item_code"] for c in comps], ["M-G-22KT-91.9-Y"])

	def test_reads_the_bom_it_is_handed(self):
		"""The design BOM is resolved once by _resolve_repair_order_bom and passed in;
		this resolver must not re-look-it-up (the old code fell back to a
		`design_id_bom` attribute that has no column on Manufacturing Work Order,
		raising AttributeError instead of a friendly throw)."""
		fake_bom = _FakeBOM(_necklace_tables())
		mock_db = MagicMock()
		mock_db.get_value.return_value = "6B"
		with patch(f"{MOD}.frappe.db", mock_db), patch(
			f"{MOD}.frappe.get_doc", return_value=fake_bom
		) as mock_get_doc, patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.bom.set_item_variant",
			lambda bom: None,
		):
			ManufacturingWorkOrder._resolve_full_repair_components(
				SimpleNamespace(), "PMO-1", "BOM-HANDED-IN"
			)
		mock_get_doc.assert_called_once_with("BOM", "BOM-HANDED-IN")


if __name__ == "__main__":
	unittest.main()
