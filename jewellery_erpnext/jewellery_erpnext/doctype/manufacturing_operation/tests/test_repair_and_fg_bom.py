"""
Pure-logic unit tests for the repair serial suffix and Dynamic FG BOM Field Mapping.

Run with:
  bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.tests.test_repair_and_fg_bom

Note: frappe.db / frappe.qb are LocalProxy objects, so a default patch() mints
async-mock children. We always inject an explicit MagicMock via patch(target, obj).
"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	_apply_fg_bom_dynamic_fields,
	_cast_fg_bom_value,
	_next_repair_serial,
)

MOD = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation"


def _db_mock(sql_rows=None, exists=False):
	mock_db = MagicMock()
	mock_db.sql.return_value = sql_rows if sql_rows is not None else []
	if callable(exists):
		mock_db.exists.side_effect = exists
	else:
		mock_db.exists.return_value = exists
	return mock_db


def _qb_chain_mock(run_rows):
	"""A frappe.qb mock whose builder chain returns run_rows from .run()."""
	mock_qb = MagicMock()
	chain = MagicMock()
	for method in ("join", "on", "select", "where", "orderby", "limit"):
		getattr(chain, method).return_value = chain
	chain.run.return_value = run_rows
	mock_qb.from_.return_value = chain
	return mock_qb


class TestNextRepairSerial(unittest.TestCase):
	def test_first_repair_appends_a(self):
		with patch(f"{MOD}.frappe.db", _db_mock([])):
			self.assertEqual(_next_repair_serial("SN0001"), "SN0001/A")

	def test_second_repair_appends_b(self):
		with patch(f"{MOD}.frappe.db", _db_mock([frappe._dict(name="SN0001/A")])):
			self.assertEqual(_next_repair_serial("SN0001"), "SN0001/B")

	def test_picks_next_after_max_existing_letter(self):
		rows = [
			frappe._dict(name="SN0001/A"),
			frappe._dict(name="SN0001/C"),
			frappe._dict(name="SN0001/B"),
		]
		with patch(f"{MOD}.frappe.db", _db_mock(rows)):
			self.assertEqual(_next_repair_serial("SN0001"), "SN0001/D")

	def test_bases_off_original_when_given_suffixed(self):
		# A piece returning as SN0001/A still bases off SN0001 -> next is /B.
		with patch(f"{MOD}.frappe.db", _db_mock([frappe._dict(name="SN0001/A")])):
			self.assertEqual(_next_repair_serial("SN0001/A"), "SN0001/B")

	def test_ignores_non_single_letter_suffixes(self):
		rows = [frappe._dict(name="SN0001/A"), frappe._dict(name="SN0001/AB")]
		with patch(f"{MOD}.frappe.db", _db_mock(rows)):
			self.assertEqual(_next_repair_serial("SN0001"), "SN0001/B")

	def test_exhaustion_at_z_raises(self):
		with patch(f"{MOD}.frappe.db", _db_mock([frappe._dict(name="SN0001/Z")])):
			with self.assertRaises(frappe.ValidationError):
				_next_repair_serial("SN0001")

	def test_skips_taken_names(self):
		# /A computed as next but that Serial No name already exists -> bump to /B.
		taken = {"SN0001/A": True, "SN0001/B": False}
		mock_db = _db_mock([], exists=lambda dt, name: taken.get(name, False))
		with patch(f"{MOD}.frappe.db", mock_db):
			self.assertEqual(_next_repair_serial("SN0001"), "SN0001/B")


class TestCastFgBomValue(unittest.TestCase):
	def test_int(self):
		self.assertEqual(_cast_fg_bom_value("5", "Int"), 5)

	def test_check(self):
		self.assertEqual(_cast_fg_bom_value("1", "Check"), 1)

	def test_float(self):
		self.assertEqual(_cast_fg_bom_value("2.5", "Float"), 2.5)

	def test_currency(self):
		self.assertEqual(_cast_fg_bom_value("2.5", "Currency"), 2.5)

	def test_data_passthrough(self):
		self.assertEqual(_cast_fg_bom_value("High Polish", "Data"), "High Polish")

	def test_blank_and_none_return_none(self):
		self.assertIsNone(_cast_fg_bom_value("", "Data"))
		self.assertIsNone(_cast_fg_bom_value(None, "Int"))


def _bom_mock(item_subcategory=None, item=None):
	"""A new_bom stand-in: .get() returns configured field values, .set() is asserted."""
	new_bom = MagicMock()
	values = {"item_subcategory": item_subcategory, "item": item}
	new_bom.get.side_effect = lambda k: values.get(k)
	return new_bom


class TestApplyFgBomDynamicFields(unittest.TestCase):
	def test_sets_only_mapped_existing_nonempty_deduped(self):
		# The join now returns the field rows directly (PMO + subcategory match).
		mock_qb = _qb_chain_mock(
			[
				frappe._dict(
					field_name="length",
					fg_bom_field="length",
					field_type="Int",
					value="5",
				),
				frappe._dict(
					field_name="g",
					fg_bom_field="ghost_field",
					field_type="Data",
					value="x",
				),
				frappe._dict(
					field_name="pu",
					fg_bom_field="product_usage",
					field_type="Data",
					value="",
				),
				frappe._dict(
					field_name="length",
					fg_bom_field="length",
					field_type="Int",
					value="99",
				),
			]
		)
		meta = MagicMock()
		meta.has_field.side_effect = lambda f: f in ("length", "product_usage")
		new_bom = _bom_mock(item_subcategory="Tennis Bracelet")
		doc = SimpleNamespace(parent_manufacturing_order="PMO-1")

		with patch(f"{MOD}.frappe.qb", mock_qb), patch(
			f"{MOD}.frappe.get_meta", return_value=meta
		), patch(f"{MOD}.frappe.get_all", return_value=[]):
			_apply_fg_bom_dynamic_fields(new_bom, doc)

		# length -> set (first wins over the dup 99); ghost_field -> not on BOM;
		# product_usage -> empty value.
		new_bom.set.assert_called_once_with("length", 5)

	def test_falls_back_to_live_config_when_snapshot_unmapped(self):
		# Value captured BEFORE the config was mapped -> child fg_bom_field is None.
		# The live config now maps field_name "product_size" -> BOM "product_size".
		mock_qb = _qb_chain_mock(
			[
				frappe._dict(
					field_name="product_size",
					fg_bom_field=None,
					field_type="Data",
					value="6",
				)
			]
		)
		meta = MagicMock()
		meta.has_field.side_effect = lambda f: f == "product_size"
		new_bom = _bom_mock(item_subcategory="Casual Pendant")
		doc = SimpleNamespace(parent_manufacturing_order="PMO-1")
		config = [frappe._dict(field_name="product_size", fg_bom_field="product_size")]

		with patch(f"{MOD}.frappe.qb", mock_qb), patch(
			f"{MOD}.frappe.get_meta", return_value=meta
		), patch(f"{MOD}.frappe.get_all", return_value=config):
			_apply_fg_bom_dynamic_fields(new_bom, doc)

		new_bom.set.assert_called_once_with("product_size", "6")

	def test_noop_without_pmo_or_subcategory(self):
		mock_qb = MagicMock()
		new_bom = _bom_mock(item_subcategory=None, item=None)
		doc = SimpleNamespace(parent_manufacturing_order=None)
		with patch(f"{MOD}.frappe.qb", mock_qb):
			_apply_fg_bom_dynamic_fields(new_bom, doc)
		new_bom.set.assert_not_called()
		mock_qb.from_.assert_not_called()

	def test_noop_when_no_matching_rows(self):
		mock_qb = _qb_chain_mock([])
		new_bom = _bom_mock(item_subcategory="Tennis Bracelet")
		doc = SimpleNamespace(parent_manufacturing_order="PMO-1")
		with patch(f"{MOD}.frappe.qb", mock_qb), patch(
			f"{MOD}.frappe.get_all", return_value=[]
		):
			_apply_fg_bom_dynamic_fields(new_bom, doc)
		new_bom.set.assert_not_called()


if __name__ == "__main__":
	unittest.main()
