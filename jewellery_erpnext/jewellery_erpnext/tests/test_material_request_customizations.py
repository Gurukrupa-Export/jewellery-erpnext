# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the Material Request customizations: Gemstone validation, MOP SE.

The department-transfer route has its own file,
``test_material_request_department_transfer``.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.material_request import (
	material_request as mr_custom,
)
from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
	before_validate as mr_before_validate,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import material_request as mr_mod

_MR_EVENTS = "jewellery_erpnext.jewellery_erpnext.doc_events.material_request"
_MR_CUSTOM = "jewellery_erpnext.jewellery_erpnext.customization.material_request.material_request"


class MockMR:
	def __init__(
		self,
		workflow_state="Material Reserved",
		mr_type="Manufacture",
		manufacturer="Manu-A",
	):
		self.workflow_state = workflow_state
		self.material_request_type = mr_type
		self.custom_manufacturer = manufacturer
		self.items = []


class TestValidateGemstoneAlternativeItems(IntegrationTestCase):
	def test_ignores_non_reserved_state(self):
		mr = MockMR(workflow_state="Draft")
		mr_mod.validate_gemstone_alternative_items(mr)  # Should return without error

	def test_ignores_non_manufacture_type(self):
		mr = MockMR(mr_type="Material Transfer")
		mr_mod.validate_gemstone_alternative_items(mr)  # Should return without error

	@patch(f"{_MR_EVENTS}._get_default_gemstone_item", return_value="GEM-DUMMY")
	def test_throws_when_alternative_item_missing(self, mock_get_default):
		mr = MockMR()
		mr.items = [
			SimpleNamespace(item_code="GEM-DUMMY", custom_alternative_item=None)
		]
		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_mod.validate_gemstone_alternative_items(mr)
		self.assertIn(
			"Please select Alternative Item for dummy gemstone item", str(ctx.exception)
		)

	@patch(f"{_MR_EVENTS}._get_default_gemstone_item", return_value="GEM-DUMMY")
	def test_throws_when_alternative_item_same_as_default(self, mock_get_default):
		mr = MockMR()
		mr.items = [
			SimpleNamespace(item_code="GEM-DUMMY", custom_alternative_item="GEM-DUMMY")
		]
		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_mod.validate_gemstone_alternative_items(mr)
		self.assertIn(
			"Alternative Item cannot be dummy gemstone item", str(ctx.exception)
		)

	@patch(f"{_MR_EVENTS}._get_default_gemstone_item", return_value="GEM-DUMMY")
	def test_passes_when_alternative_item_valid(self, mock_get_default):
		mr = MockMR()
		mr.items = [
			SimpleNamespace(item_code="GEM-DUMMY", custom_alternative_item="REAL-GEM-1")
		]
		mr_mod.validate_gemstone_alternative_items(mr)  # Should not throw


class TestMakeMopStockEntry(IntegrationTestCase):
	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value")
	@patch(f"{_MR_CUSTOM}.frappe.get_cached_value", return_value=("MWO-1", "MO-1"))
	@patch(f"{_MR_CUSTOM}.mri_warehouse_map", return_value={"MRI-1": "WH-FROM"})
	@patch(f"{_MR_CUSTOM}.frappe.copy_doc")
	def test_creates_mop_stock_entry(
		self, mock_copy, mock_mri_map, mock_cached, mock_get_value, mock_get_doc
	):
		se = MagicMock()
		se.items = [MagicMock(material_request_item="MRI-1")]
		mock_copy.return_value = se

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Manufacturing Operation":
				return {
					"department": "Dept A",
					"status": "Pending",
					"employee": None,
					"department_ir_status": None,
				}
			if doctype == "Warehouse":
				return "WH-TARGET"
			return None

		mock_get_value.side_effect = _gv

		mr_dict = {"custom_reserve_se": "SE-OLD", "name": "MR-1"}

		# Mocking the dictionary .get / .db_set on `self` for make_mop_stock_entry
		mr_obj = MagicMock()
		mr_obj.get.side_effect = lambda k: mr_dict.get(k)

		mr_custom.make_mop_stock_entry(mr_obj, mop="MOP-1")

		se.save.assert_called_once()
		se.submit.assert_called_once()
		self.assertEqual(se.stock_entry_type, "Material Transfer (WORK ORDER)")
		self.assertEqual(se.manufacturing_operation, "MOP-1")
		self.assertEqual(se.to_department, "Dept A")
		mr_obj.db_set.assert_called_once_with("custom_mop_se", se.name)

	@patch(f"{_MR_CUSTOM}.frappe.log_error")
	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value")
	def test_throws_when_in_transit(self, mock_get_value, mock_get_doc, mock_log_error):
		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Manufacturing Operation":
				return {"department": "Dept A", "department_ir_status": "In-Transit"}
			return None

		mock_get_value.side_effect = _gv

		mr_obj = MagicMock()
		# Answers per key, not one value for every key: custom_mop_se must come back empty
		# or the idempotency guard returns before the in-transit check is reached.
		mr_obj.get.side_effect = lambda k: {"custom_reserve_se": "SE-OLD"}.get(k)

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_custom.make_mop_stock_entry(mr_obj, mop="MOP-1")
		self.assertIn("in-transit status", str(ctx.exception))

	@patch(f"{_MR_CUSTOM}.frappe.copy_doc")
	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	def test_returns_none_when_mop_se_already_stamped(self, mock_get_doc, mock_copy):
		"""before_update_after_submit re-fires on every Update in this state."""
		mr_obj = MagicMock()
		mr_obj.get.side_effect = lambda k: {
			"custom_mop_se": "SE-MOP-1",
			"custom_reserve_se": "SE-OLD",
		}.get(k)

		self.assertIsNone(mr_custom.make_mop_stock_entry(mr_obj, mop="MOP-1"))
		mock_copy.assert_not_called()
		mr_obj.db_set.assert_not_called()


class TestMakeDepartmentMopStockEntry(IntegrationTestCase):
	def test_returns_none_if_no_reserve_se(self):
		mr_obj = MagicMock()
		mr_obj.get.return_value = None
		self.assertIsNone(mr_custom.make_department_mop_stock_entry(mr_obj))

	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value")
	def test_throws_if_in_transit(self, mock_get_value, mock_get_doc):
		mr_obj = MagicMock()
		# Answers per key, not one value for every key: custom_mop_se must come back empty
		# or the idempotency guard returns before the in-transit check is reached.
		mr_obj.get.side_effect = lambda k: {"custom_reserve_se": "SE-RESERVE"}.get(k)
		mock_get_value.return_value = {"department_ir_status": "In-Transit"}

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_custom.make_department_mop_stock_entry(mr_obj, mop="MOP-1")
		self.assertIn("in-transit status", str(ctx.exception))

	@patch(f"{_MR_CUSTOM}.frappe.copy_doc")
	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	def test_returns_none_when_mop_se_already_stamped(self, mock_get_doc, mock_copy):
		mr_obj = MagicMock()
		mr_obj.get.side_effect = lambda k: {
			"custom_mop_se": "SE-MOP-1",
			"custom_reserve_se": "SE-RESERVE",
		}.get(k)

		self.assertIsNone(
			mr_custom.make_department_mop_stock_entry(mr_obj, mop="MOP-1")
		)
		mock_copy.assert_not_called()
		mr_obj.db_set.assert_not_called()

	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.copy_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.sql")
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value")
	@patch(f"{_MR_CUSTOM}.frappe.get_cached_value", return_value=("MWO-1", "MO-1"))
	def test_sources_from_the_destination_warehouse_after_a_department_transfer(
		self, mock_cached, mock_get_value, mock_sql, mock_copy, mock_get_doc
	):
		"""The last-Stock-Entry query is not consulted once the move is on the record."""
		se = MagicMock()
		se.items = [MagicMock(material_request_item="MRI-1")]
		mock_copy.return_value = se

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Manufacturing Operation":
				return {
					"department": "Dept A",
					"status": "Not Started",
					"employee": None,
					"department_ir_status": None,
				}
			return "WH-DEPT-MFG"

		mock_get_value.side_effect = _gv

		mr_obj = MagicMock()
		mr_obj.name = "MR-1"
		mr_obj.get.side_effect = lambda k: {
			"custom_reserve_se": "SE-RESERVE",
			"custom_department": "DEPT-A",
			"custom_department_transfer_se": "SE-DEPT-1",
			"custom_destination_warehouse": "WH-DEST",
		}.get(k)

		mr_custom.make_department_mop_stock_entry(mr_obj, mop="MOP-1")

		self.assertEqual(se.items[0].s_warehouse, "WH-DEST")
		self.assertEqual(se.items[0].t_warehouse, "WH-DEPT-MFG")
		mock_sql.assert_not_called()

	@patch(f"{_MR_CUSTOM}.frappe.get_doc")
	@patch(f"{_MR_CUSTOM}.frappe.copy_doc")
	@patch(f"{_MR_CUSTOM}.frappe.db.sql", return_value=[])
	@patch(f"{_MR_CUSTOM}.frappe.db.get_value")
	@patch(f"{_MR_CUSTOM}.frappe.get_cached_value", return_value=("MWO-1", "MO-1"))
	def test_uses_fallback_swarehouse_and_employee_warehouse(
		self, mock_cached, mock_get_value, mock_sql, mock_copy, mock_get_doc
	):
		se = MagicMock()
		se.items = [MagicMock(material_request_item="MRI-1")]
		mock_copy.return_value = se

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Manufacturing Operation":
				return {
					"department": "Dept A",
					"status": "WIP",
					"employee": "EMP-1",
					"department_ir_status": None,
				}
			if doctype == "Warehouse" and "employee" in name:
				return "WH-EMP"
			if doctype == "Warehouse":
				return "WH-DEPT-MFG"
			return None

		mock_get_value.side_effect = _gv

		mr_obj = MagicMock()
		mr_obj.name = "MR-1"
		mr_obj.items = [MagicMock(warehouse="WH-FALLBACK")]
		mr_obj.get.side_effect = (
			lambda k: "SE-RESERVE"
			if k == "custom_reserve_se"
			else "DEPT-A"
			if k == "custom_department"
			else None
		)

		mr_custom.make_department_mop_stock_entry(mr_obj, mop="MOP-1")
		self.assertEqual(se.items[0].s_warehouse, "WH-FALLBACK")
		self.assertEqual(se.items[0].t_warehouse, "WH-EMP")
		self.assertEqual(se.to_department, "DEPT-A")


class TestMakeInTransitStockEntry(IntegrationTestCase):
	@patch(f"{_MR_EVENTS}.frappe.db.get_value")
	def test_throws_if_missing_transit_warehouse(self, mock_get_value):
		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Warehouse" and name == "WH-TO":
				return ("Dept A", "Regular", None)
			if doctype == "Material Request":
				return ("Dept From", "WH-SET")
			return None

		mock_get_value.side_effect = _gv

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_mod.make_in_transit_stock_entry("MR-1", "WH-TO", "TT-1")
		self.assertIn("Transit warehouse is not mentioned", str(ctx.exception))

	@patch(f"{_MR_EVENTS}.make_stock_entry")
	@patch(f"{_MR_EVENTS}.frappe.db.get_value")
	def test_throws_if_transfer_type_has_no_se_type(self, mock_get_value, mock_mse):
		se = MagicMock()
		mock_mse.return_value = se

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Warehouse" and name == "WH-TO":
				return ("Dept A", "Regular", "WH-TRANSIT")
			if doctype == "Material Request":
				return ("Dept From", "WH-SET")
			if doctype == "Warehouse" and name == "Dept From":
				return "Regular"
			if doctype == "Transfer Type":
				return None
			return None

		mock_get_value.side_effect = _gv

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_mod.make_in_transit_stock_entry("MR-1", "WH-TO", "TT-1")
		self.assertIn("Please specify a Stock Entry Type", str(ctx.exception))

	@patch(f"{_MR_EVENTS}.make_stock_entry")
	@patch(f"{_MR_EVENTS}.frappe.db.get_value")
	def test_handles_customer_goods(self, mock_get_value, mock_mse):
		se = MagicMock()
		se.items = [MagicMock(customer="CUST-1")]
		mock_mse.return_value = se

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Warehouse" and name == "WH-TO":
				return ("Dept A", "Regular", "WH-TRANSIT")
			if doctype == "Material Request":
				return ("Dept From", "WH-SET")
			if doctype == "Warehouse" and name == "Dept From":
				return "Regular"
			if doctype == "Transfer Type":
				return "Regular Transfer"
			return None

		mock_get_value.side_effect = _gv

		res = mr_mod.make_in_transit_stock_entry("MR-1", "WH-TO", "TT-1")
		self.assertEqual(res.stock_entry_type, "Customer Goods Transfer")

	@patch(f"{_MR_EVENTS}.make_stock_entry")
	@patch(f"{_MR_EVENTS}.frappe.db.get_value")
	def test_handles_consumables(self, mock_get_value, mock_mse):
		se = MagicMock()
		se.items = [MagicMock(customer=None)]
		mock_mse.return_value = se

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Warehouse" and name == "WH-TO":
				return ("Dept A", "Consumables", "WH-TRANSIT")
			if doctype == "Material Request":
				return ("Dept From", "WH-SET")
			if doctype == "Warehouse" and name == "Dept From":
				return "Consumables"
			if doctype == "Transfer Type":
				return "Regular Transfer"
			return None

		mock_get_value.side_effect = _gv

		res = mr_mod.make_in_transit_stock_entry("MR-1", "WH-TO", "TT-1")
		self.assertEqual(res.stock_entry_type, "Consumables Issue to  Department")
		self.assertEqual(res.to_warehouse, "WH-SET")


class TestGetPmoData(IntegrationTestCase):
	@patch(f"{_MR_CUSTOM}.frappe.qb.from_")
	@patch(f"{_MR_CUSTOM}.get_mapped_doc")
	def test_maps_variants(self, mock_mapped_doc, mock_from):
		mock_chain = MagicMock()
		mock_from.return_value = mock_chain
		mock_chain.join.return_value = mock_chain
		mock_chain.on.return_value = mock_chain
		mock_chain.select.return_value = mock_chain
		mock_chain.where.return_value = mock_chain

		mock_chain.run.return_value = [
			frappe._dict(
				{
					"item_code": "ITM-1",
					"qty": 1,
					"uom": "Nos",
					"rate": 100,
					"inventory_type": "Regular",
					"customer": None,
					"conversion_factor": 1,
					"t_warehouse": "T",
					"s_warehouse": "S",
					"batch_no": "B1",
				}
			)
		]

		def side_effect(*args, **kwargs):
			set_missing_values = args[4]
			target = MagicMock()
			target.custom_item_type = "Gemstone"
			set_missing_values(MagicMock(), target)
			return target

		mock_mapped_doc.side_effect = side_effect

		res = mr_custom.get_pmo_data("PMO-1", None)
		res.append.assert_called_once_with(
			"items",
			{
				"warehouse": "T",
				"from_warehouse": "S",
				"item_code": "ITM-1",
				"qty": 1,
				"uom": "Nos",
				"conversion_factor": 1,
				"rate": 100,
				"inventory_type": "Regular",
				"customer": None,
				"batch_no": "B1",
			},
		)


class TestGetItemDetails(IntegrationTestCase):
	@patch(f"{_MR_EVENTS}.nowdate", return_value="2026-01-01")
	@patch(f"{_MR_EVENTS}.frappe.qb.from_")
	def test_throws_if_inactive_or_missing(self, mock_from, mock_nowdate):
		mock_chain = MagicMock()
		mock_from.return_value = mock_chain
		mock_chain.left_join.return_value = mock_chain
		mock_chain.on.return_value = mock_chain
		mock_chain.select.return_value = mock_chain
		mock_chain.where.return_value = mock_chain
		mock_chain.run.return_value = []

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_mod.get_item_details({"item_code": "INV-1"})
		self.assertIn("inactive or its end-of-life", str(ctx.exception))

	@patch(f"{_MR_EVENTS}.nowdate", return_value="2026-01-01")
	@patch(f"{_MR_EVENTS}.frappe.qb.from_")
	def test_returns_correct_details(self, mock_from, mock_nowdate):
		mock_chain = MagicMock()
		mock_from.return_value = mock_chain
		mock_chain.left_join.return_value = mock_chain
		mock_chain.on.return_value = mock_chain
		mock_chain.select.return_value = mock_chain
		mock_chain.where.return_value = mock_chain
		mock_chain.run.return_value = [
			frappe._dict(
				{
					"stock_uom": "Nos",
					"description": "Desc",
					"image": "img.png",
					"item_name": "Item Name",
					"has_serial_no": 1,
					"has_batch_no": 0,
					"sample_quantity": 2,
					"expense_account": "Exp",
				}
			)
		]

		res = mr_mod.get_item_details({"item_code": "ITM-1", "qty": 10})
		self.assertEqual(res.uom, "Nos")
		self.assertEqual(res.qty, 10)
		self.assertEqual(res.has_serial_no, 1)


_MR_BV = "jewellery_erpnext.jewellery_erpnext.customization.material_request.utils.before_validate"


class TestUpdatePureQty(IntegrationTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils.prefetch_purity_percentages"
	)
	@patch("frappe.db.get_value")
	def test_throws_if_pure_gold_item_missing(self, mock_get_value, mock_prefetch):
		mock_get_value.return_value = None
		mr = MagicMock()
		mr.custom_transfer_type = "Transfer to Reserve"
		mr.custom_manufacturer = "Manu-1"
		mr.items = [
			MagicMock(
				custom_variant_of="M", custom_alternative_item="ITEM-ALLOY", qty=10.0
			)
		]

		from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
			before_validate as mr_before_validate,
		)

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_before_validate.update_pure_qty(mr)
		self.assertIn("Select Manufacturer in session defaults", str(ctx.exception))


def _totals_row(qty=0.0, pcs=None, variant="D", item_code="ITEM-1"):
	"""One Material Request Item row, as ``update_pure_qty`` reads it.

	``SimpleNamespace`` rather than ``MagicMock``: a MagicMock hands back a truthy mock
	for ``.pcs``, which ``cint`` quietly turns into 0 -- so a broken sum would still pass.
	"""
	return SimpleNamespace(
		custom_variant_of=variant,
		item_code=item_code,
		custom_alternative_item=None,
		qty=qty,
		pcs=pcs,
		custom_pure_qty=None,
	)


def _totals_mr(rows):
	return SimpleNamespace(
		custom_transfer_type="Transfer to Reserve",
		custom_manufacturer="Manu-1",
		items=rows,
		custom_total_quantity=None,
		custom_total_pcs=None,
	)


@patch(_MR_BV + ".prefetch_purity_percentages")
class TestUpdateTotalPcs(IntegrationTestCase):
	"""``custom_total_pcs`` -- the Total Pcs header total.

	Diamond ("D") rows are used throughout except where stated: ``_is_pure_qty_row`` is
	False for them, so the purity branch never runs and these stay pure arithmetic.
	"""

	def test_sums_the_pcs_column(self, _mock_prefetch):
		# The real numbers from KGJPL-MR-MF-26-34263.
		mr = _totals_mr(
			[_totals_row(qty=0.066, pcs="1"), _totals_row(qty=0.640, pcs="32")]
		)

		mr_before_validate.update_pure_qty(mr)

		self.assertEqual(mr.custom_total_pcs, 33)
		self.assertAlmostEqual(mr.custom_total_quantity, 0.706, places=3)

	def test_blank_and_missing_pcs_count_as_zero(self, _mock_prefetch):
		# pcs is nullable on 9,069 of the live rows, and a Data field so it can also be "".
		mr = _totals_mr(
			[
				_totals_row(qty=1.0, pcs="5"),
				_totals_row(qty=2.0, pcs=None),
				_totals_row(qty=3.0, pcs=""),
			]
		)

		mr_before_validate.update_pure_qty(mr)

		self.assertEqual(mr.custom_total_pcs, 5)

	def test_no_items_totals_zero(self, _mock_prefetch):
		mr = _totals_mr([])

		mr_before_validate.update_pure_qty(mr)

		self.assertEqual(mr.custom_total_pcs, 0)

	def test_recomputing_does_not_double(self, _mock_prefetch):
		mr = _totals_mr([_totals_row(qty=1.0, pcs="7")])

		mr_before_validate.update_pure_qty(mr)
		mr_before_validate.update_pure_qty(mr)

		self.assertEqual(mr.custom_total_pcs, 7)

	@patch(_MR_BV + ".get_purity_percentage")
	@patch("frappe.db.get_value")
	def test_counts_a_row_that_total_quantity_skips(
		self, mock_get_value, mock_purity, _mock_prefetch
	):
		"""A purity-less metal row still counts toward Total Pcs.

		``update_pure_qty`` has a ``continue`` for a metal/findings row whose item carries
		no purity percentage, which drops that row out of ``custom_total_quantity``. Total
		Pcs is accumulated before that branch on purpose, so it stays a faithful count of
		the grid. This pins the divergence so neither side drifts unnoticed.
		"""

		# Scoped to this call only, and keyed on the argument, rather than a blanket
		# return_value: a blanket patch of frappe.db.get_value also hijacks meta loading.
		def _get_value(doctype, *args, **kwargs):
			self.assertEqual(doctype, "Manufacturing Setting")
			return "PURE-GOLD"

		mock_get_value.side_effect = _get_value
		# Truthy for the pure item, falsy for the row's own item -> hits the ``continue``.
		mock_purity.side_effect = lambda item: 100.0 if item == "PURE-GOLD" else None

		mr = _totals_mr(
			[_totals_row(qty=5.0, pcs="4", variant="M", item_code="ITEM-ALLOY")]
		)

		mr_before_validate.update_pure_qty(mr)

		self.assertEqual(mr.custom_total_pcs, 4)
		self.assertEqual(mr.custom_total_quantity, 0)


class TestTotalPcsFieldLayout(IntegrationTestCase):
	"""The rendered layout, not just the arithmetic.

	Worth its own test because the layout is provisioned by two different routes -- the
	patch on a migrated site, ``install.after_sync`` on a fresh one -- and an earlier
	revision of this change was correct on a migrated site while landing Total Pcs in the
	Terms tab on a fresh install. Only a meta-level assertion catches that.

	The last test here covers something CI structurally cannot: ``install.sh`` moves
	``gke_customization/fixtures`` aside before installing any app, so the fixture re-import
	that resets this layout on every production migrate never happens on a test site. It is
	simulated by hand instead.
	"""

	EXPECTED = [
		"items",
		"custom_total_section",
		"custom_total_quantity",
		"custom_column_break_total_pcs",
		"custom_total_pcs",
		"custom_order_details",
	]

	def test_total_pcs_renders_beside_total_quantity(self):
		meta = frappe.get_meta("Material Request")
		order = [df.fieldname for df in meta.fields]
		start = order.index("items")

		self.assertEqual(order[start : start + len(self.EXPECTED)], self.EXPECTED)

	def test_items_grid_keeps_its_own_section(self):
		# A Column Break sharing items_section would halve the grid: Frappe splits a
		# section's width evenly across its columns, with no exception for Table fields.
		meta = frappe.get_meta("Material Request")
		order = [df.fieldname for df in meta.fields]
		between = order[
			order.index("items_section") + 1 : order.index("custom_total_section")
		]

		self.assertEqual(between, ["items"])

	def test_total_pcs_is_a_read_only_int(self):
		df = frappe.get_meta("Material Request").get_field("custom_total_pcs")

		self.assertIsNotNone(df)
		self.assertEqual(df.fieldtype, "Int")
		self.assertTrue(df.read_only)
		# allow_on_submit stays off: update_pure_qty only runs up to submit.
		self.assertFalse(df.allow_on_submit)

	def test_layout_recovers_after_a_fixture_reset(self):
		"""The production condition CI cannot reach, performed by hand.

		On a real site ``sync_fixtures()`` re-imports gke_customization's Custom Field rows
		on every migrate, putting ``custom_total_quantity`` back on ``items`` and
		``custom_order_details`` back on ``custom_total_quantity``. ``after_migrate`` runs
		afterwards and is what repairs it. CI never sees this because ``install.sh`` disables
		those fixtures, so without this test the repair path is only ever exercised on
		production -- the worst possible place to discover it does not work.

		Everything written here is rolled back by the harness at class teardown; the explicit
		re-apply below also leaves the site correct for any test that runs after it, since
		Property Setter changes escape the meta cache.
		"""
		from jewellery_erpnext.patches.add_material_request_total_pcs_field import (
			after_migrate,
		)

		def order():
			frappe.clear_cache(doctype="Material Request")
			fields = [df.fieldname for df in frappe.get_meta("Material Request").fields]
			start = fields.index("items")
			return fields[start : start + len(self.EXPECTED)]

		self.addCleanup(after_migrate)

		# What the gke fixture restores, plus the Property Setters a Customize Form "Reset
		# Layout" would remove -- together, the worst state the hook has to recover from.
		frappe.db.delete(
			"Property Setter",
			{"doc_type": "Material Request", "property": "field_order"},
		)
		frappe.db.delete(
			"Property Setter",
			{"doc_type": "Material Request", "property": "insert_after"},
		)
		frappe.db.set_value(
			"Custom Field",
			"Material Request-custom_total_quantity",
			"insert_after",
			"items",
		)
		frappe.db.set_value(
			"Custom Field",
			"Material Request-custom_order_details",
			"insert_after",
			"custom_total_quantity",
		)

		self.assertNotEqual(
			order(), self.EXPECTED, msg="the reset did not break the layout"
		)

		before = frappe.db.count(
			"Error Log", {"method": ["like", "%Total Pcs layout%"]}
		)
		after_migrate()

		self.assertEqual(order(), self.EXPECTED)
		# after_migrate swallows exceptions, so a silent failure would otherwise look like a
		# pass on a site that happened to already be correct.
		self.assertEqual(
			frappe.db.count("Error Log", {"method": ["like", "%Total Pcs layout%"]}),
			before,
			msg="after_migrate logged an error instead of applying the layout",
		)


class TestValidateWarehouse(IntegrationTestCase):
	def test_throws_if_set_warehouse_same(self):
		mr = MagicMock(
			material_request_type="Material Transfer",
			set_from_warehouse="WH-1",
			set_warehouse="WH-1",
		)
		from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
			before_validate as mr_before_validate,
		)

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_before_validate.validate_warehouse(mr)
		self.assertIn("cannot be the same", str(ctx.exception))

	def test_throws_if_row_warehouse_same(self):
		mr = MagicMock(
			material_request_type="Material Transfer",
			set_from_warehouse="WH-1",
			set_warehouse="WH-2",
		)
		mr.items = [MagicMock(from_warehouse="WH-3", warehouse="WH-3")]
		from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
			before_validate as mr_before_validate,
		)

		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_before_validate.validate_warehouse(mr)
		self.assertIn("cannot be the same", str(ctx.exception))

	def test_ignores_non_material_transfer(self):
		mr = MagicMock(
			material_request_type="Manufacture",
			set_from_warehouse="WH-1",
			set_warehouse="WH-1",
		)
		from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
			before_validate as mr_before_validate,
		)

		mr_before_validate.validate_warehouse(mr)  # Should not throw


class TestMakeStockInEntry(IntegrationTestCase):
	@patch(f"{_MR_EVENTS}.get_mapped_doc")
	def test_mapping_configuration(self, mock_get_mapped_doc):
		def mock_get_mapped(*args, **kwargs):
			set_missing_values = args[4]
			target = MagicMock()
			source = MagicMock(_customer="CUST-1")
			set_missing_values(source, target)

			self.assertEqual(target.material_request_type, "Material Transfer")
			self.assertEqual(target.customer, "CUST-1")
			self.assertIsNone(target.custom_reserve_se)

			update_item = args[2]["Stock Entry Detail"]["postprocess"]
			target_row = frappe._dict()
			source_row = frappe._dict(
				parent="MR-1", name="MRI-1", t_warehouse="WH-T", qty=10
			)
			update_item(source_row, target_row, source)

			self.assertEqual(target_row.material_request, "MR-1")
			self.assertEqual(target_row.from_warehouse, "WH-T")
			self.assertEqual(target_row.warehouse, "")

			return target

		mock_get_mapped_doc.side_effect = mock_get_mapped
		mr_mod.make_stock_in_entry("MR-1")


class TestMakeStockEntry(IntegrationTestCase):
	@patch(f"{_MR_EVENTS}.get_mapped_doc")
	@patch(f"{_MR_EVENTS}.frappe.get_value")
	def test_mapping_configuration(self, mock_get_value, mock_get_mapped_doc):
		mock_get_value.return_value = frappe._dict(bom_no="BOM-1", for_quantity=100)

		def mock_get_mapped(*args, **kwargs):
			set_missing_values = args[4]
			update_item = args[2]["Material Request Item"]["postprocess"]

			source = MagicMock(
				material_request_type="Material Transfer",
				inventory_type="Customer Goods",
				job_card="JC-1",
				name="MR-1",
			)
			# frappe._dict, not MagicMock: MagicMock(name=...) names the mock itself
			# rather than setting a `name` attribute, and `name` is the key the
			# batch/serial map is built on.
			source.items = [
				frappe._dict(
					name="MRI-1",
					item_code="ITM-1",
					idx=1,
					batch_no="BATCH-1",
					serial_no="SR-1",
				)
			]

			target = MagicMock()
			# idx deliberately does NOT match the source row's: the mapper's `condition`
			# can drop source rows, which renumbers target idx. Rows are paired by
			# material_request_item (the source row name), so this must still resolve.
			target_row = frappe._dict(
				item_code="ITM-1",
				idx=7,
				conversion_factor=1,
				material_request_item="MRI-1",
			)
			target.items = [target_row]

			set_missing_values(source, target)

			self.assertEqual(target.purpose, "Material Transfer for Manufacture")
			self.assertEqual(target.stock_entry_type, "Customer Goods Transfer")
			self.assertEqual(target_row.batch_no, "BATCH-1")
			self.assertEqual(target_row.serial_no, "SR-1")
			# Stock Entry Detail hides serial_no/batch_no unless this flag is set.
			self.assertEqual(target_row.use_serial_batch_fields, 1)
			self.assertEqual(target.bom_no, "BOM-1")

			source_row = frappe._dict(
				stock_qty=10,
				ordered_qty=5,
				conversion_factor=1,
				warehouse="WH-S",
				from_warehouse="WH-FROM",
			)
			update_item(source_row, target_row, source)

			self.assertEqual(target_row.qty, 5)
			self.assertEqual(target_row.t_warehouse, "WH-S")
			self.assertNotIn("allow_zero_valuation_rate", target_row)

			return target

		mock_get_mapped_doc.side_effect = mock_get_mapped
		mr_mod.make_stock_entry("MR-1")


_GEPL = "Gurukrupa Export Private Limited"


def _reservation_mr(**kwargs):
	"""Minimal Manufacture MR. SimpleNamespace, not MagicMock: the rule branches on
	falsiness of set_warehouse/custom_manufacturer, which a MagicMock would make truthy."""
	doc = {
		"material_request_type": "Manufacture",
		"set_warehouse": None,
		"custom_manufacturer": "Shubh",
		"company": _GEPL,
		"items": [],
	}
	doc.update(kwargs)
	return SimpleNamespace(**doc)


def _reservation_row(variant, warehouse=None):
	return SimpleNamespace(custom_variant_of=variant, warehouse=warehouse)


@patch("frappe.get_cached_value", return_value=_GEPL)
@patch(
	f"{_MR_BV}.get_variant_warehouse_map",
	return_value={
		"M": "Waxing RSV - GEPL",
		"D": "Diamond Setting RSV - GEPL",
		"G": "Diamond Setting RSV - GEPL",
		"F": "Central RSV - GEPL",
	},
)
class TestSetReservationWarehouse(IntegrationTestCase):
	def test_fills_header_and_empty_rows(self, mock_map, mock_cached):
		mr = _reservation_mr(items=[_reservation_row("M")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertEqual(mr.set_warehouse, "Waxing RSV - GEPL")
		self.assertEqual(mr.items[0].warehouse, "Waxing RSV - GEPL")

	def test_distinct_variants_sharing_one_warehouse_still_fill(
		self, mock_map, mock_cached
	):
		# D and G both map to Diamond Setting RSV, so the header can still express it.
		mr = _reservation_mr(items=[_reservation_row("D"), _reservation_row("G")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertEqual(mr.set_warehouse, "Diamond Setting RSV - GEPL")

	def test_leaves_rows_that_already_match(self, mock_map, mock_cached):
		mr = _reservation_mr(items=[_reservation_row("M", "Waxing RSV - GEPL")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertEqual(mr.set_warehouse, "Waxing RSV - GEPL")

	def test_ignores_non_manufacture_type(self, mock_map, mock_cached):
		mr = _reservation_mr(
			material_request_type="Material Transfer", items=[_reservation_row("M")]
		)
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)

	def test_never_overwrites_an_existing_warehouse(self, mock_map, mock_cached):
		mr = _reservation_mr(
			set_warehouse="RM Procurement - GEPL", items=[_reservation_row("M")]
		)
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertEqual(mr.set_warehouse, "RM Procurement - GEPL")

	def test_ignores_rows_routed_elsewhere(self, mock_map, mock_cached):
		mr = _reservation_mr(items=[_reservation_row("M", "RM Procurement - GEPL")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)
		self.assertEqual(mr.items[0].warehouse, "RM Procurement - GEPL")

	def test_ignores_unmapped_variant(self, mock_map, mock_cached):
		# The dummy gemstone item carries variant_of = NULL.
		mr = _reservation_mr(items=[_reservation_row(None)])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)

	def test_ignores_partially_mappable_request(self, mock_map, mock_cached):
		mr = _reservation_mr(items=[_reservation_row("M"), _reservation_row(None)])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)
		self.assertIsNone(mr.items[0].warehouse)

	def test_ignores_variants_resolving_to_different_warehouses(
		self, mock_map, mock_cached
	):
		mr = _reservation_mr(items=[_reservation_row("M"), _reservation_row("D")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)

	def test_ignores_empty_items(self, mock_map, mock_cached):
		mr = _reservation_mr(items=[])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)

	def test_ignores_warehouse_of_another_company(self, mock_map, mock_cached):
		mock_cached.return_value = "KG GK Jewellers Private Limited"
		mr = _reservation_mr(items=[_reservation_row("M")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)

	@patch("frappe.defaults.get_user_default", return_value=None)
	def test_ignores_missing_manufacturer(self, mock_default, mock_map, mock_cached):
		mock_map.return_value = {}
		mr = _reservation_mr(custom_manufacturer=None, items=[_reservation_row("M")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertIsNone(mr.set_warehouse)

	@patch("frappe.defaults.get_user_default", return_value="Shubh")
	def test_falls_back_to_session_default_manufacturer(
		self, mock_default, mock_map, mock_cached
	):
		mr = _reservation_mr(custom_manufacturer=None, items=[_reservation_row("M")])
		mr_before_validate.set_reservation_warehouse(mr)

		self.assertEqual(mr.set_warehouse, "Waxing RSV - GEPL")
		mock_map.assert_called_once_with("Shubh")
