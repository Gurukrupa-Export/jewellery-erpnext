# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Manufacturer purity resolution through PMO and Manufacturing Plan lifecycles."""

from unittest.mock import Mock, patch

import frappe
from frappe.model.document import Document
from frappe.tests import UnitTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan import (
	manufacturing_plan as mp_mod,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order import (
	parent_manufacturing_order as pmo_mod,
)


class _PMO(frappe._dict):
	"""Run the real hooks and Frappe hook ordering without writing manufacturing records."""

	metal_details = pmo_mod.ParentManufacturingOrder.metal_details
	validate = pmo_mod.ParentManufacturingOrder.validate
	before_save = pmo_mod.ParentManufacturingOrder.before_save
	before_submit = pmo_mod.ParentManufacturingOrder.before_submit
	on_submit = pmo_mod.ParentManufacturingOrder.on_submit
	run_before_save_methods = Document.run_before_save_methods

	def is_new(self):
		return bool(self.get("__islocal"))

	def run_method(self, method):
		if method != "before_validate":
			getattr(self, method)()

	def reset_seen(self):
		pass

	def set_title_field(self):
		pass

	def _validate_diamond_grade_policy(self):
		pass

	def _set_diamond_grade(self):
		pass

	def db_set(self, values):
		self.update(values)


def _pmo(**values):
	doc = _PMO(
		manufacturer="Shubh",
		metal_type="Gold",
		metal_touch="22KT",
		metal_purity=None,
		custom_tracking_bom=None,
		item_code="MU01125-006",
		diamond_grade="6B",
		flags=frappe._dict(),
		docstatus=0,
		_action="save",
		__islocal=True,
	)
	doc.update(values)
	return doc


def _criterion(**values):
	row = {
		"parent": "Shubh",
		"parenttype": "Manufacturer",
		"parentfield": "metal_criteria",
		"metal_type": "Gold",
		"metal_touch": "22KT",
		"metal_purity": "91.9",
	}
	row.update(values)
	return row


class TestPMOMetalPurity(UnitTestCase):
	def setUp(self):
		super().setUp()
		# Distractors precede the match, so a lookup missing any key returns the wrong row.
		self.criteria = [
			_criterion(parent="Another Manufacturer", metal_purity="91.6"),
			_criterion(parenttype="Customer", metal_purity="92.5"),
			_criterion(parentfield="other_criteria", metal_purity="92.0"),
			_criterion(metal_type="Silver", metal_purity="95.0"),
			_criterion(metal_touch="18KT", metal_purity="75.4"),
			_criterion(),
		]
		self.criteria_queries = []
		self.get_value = self.enterContext(
			patch.object(pmo_mod.frappe.db, "get_value", side_effect=self._get_value)
		)
		self.enterContext(
			patch.object(pmo_mod, "get_current_mould_id", return_value=None)
		)
		self.enterContext(patch.object(pmo_mod, "validate_mfg_date"))
		self.enterContext(patch.object(pmo_mod, "update_parent_details"))
		self.enterContext(patch.object(pmo_mod.frappe.db, "sql"))

	def _get_value(self, doctype, filters, fieldname=None, **kwargs):
		if doctype == "Manufacturing Setting":
			return frappe._dict()
		self.assertEqual(doctype, "Metal Criteria")
		self.criteria_queries.append(filters.copy())
		for row in self.criteria:
			if all(row.get(key) == value for key, value in filters.items()):
				return row["metal_purity"]
		return None

	def test_initial_save_resolves_exact_manufacturer_criterion_once(self):
		doc = _pmo()
		doc.run_before_save_methods()
		self.assertEqual(doc.metal_purity, "91.9")
		self.assertEqual(len(self.criteria_queries), 1)

	def test_existing_draft_save_replaces_stale_purity_once(self):
		doc = _pmo(__islocal=False, metal_purity="92.5")
		doc.run_before_save_methods()
		self.assertEqual(doc.metal_purity, "91.9")
		self.assertEqual(len(self.criteria_queries), 1)

	def test_changing_each_lookup_input_recalculates_purity(self):
		for field, value, expected in (
			("manufacturer", "Another Manufacturer", "91.6"),
			("metal_type", "Silver", "95.0"),
			("metal_touch", "18KT", "75.4"),
		):
			with self.subTest(field=field):
				doc = _pmo(__islocal=False, metal_purity="91.9")
				doc[field] = value
				doc.run_before_save_methods()
				self.assertEqual(doc.metal_purity, expected)

	def test_customer_criterion_cannot_supply_manufacturer_purity(self):
		self.criteria = [_criterion(parenttype="Customer", metal_purity="92.5")]
		doc = _pmo(metal_purity="91.6")
		doc.metal_details()
		self.assertEqual(doc.metal_purity, "91.6")

	def test_matching_does_not_require_tracking_bom(self):
		for tracking_bom in (None, "TRACKING-BOM"):
			with self.subTest(tracking_bom=tracking_bom):
				doc = _pmo(custom_tracking_bom=tracking_bom)
				doc.metal_details()
				self.assertEqual(doc.metal_purity, "91.9")

	def test_missing_inputs_preserve_purity_without_querying(self):
		for field in ("manufacturer", "metal_type", "metal_touch"):
			for missing in (None, ""):
				with self.subTest(field=field, missing=missing):
					doc = _pmo(metal_purity="92.5", **{field: missing})
					doc.metal_details()
					self.assertEqual(doc.metal_purity, "92.5")
		self.get_value.assert_not_called()

	def test_missing_match_preserves_purity(self):
		doc = _pmo(metal_touch="24KT", metal_purity="99.9")
		doc.metal_details()
		self.assertEqual(doc.metal_purity, "99.9")

	def test_blank_configured_purity_preserves_current_value(self):
		for blank in (None, ""):
			with self.subTest(blank=blank):
				self.criteria = [_criterion(metal_purity=blank)]
				doc = _pmo(metal_purity="92.5")
				doc.metal_details()
				self.assertEqual(doc.metal_purity, "92.5")

	def test_ignore_validations_still_resolves_derived_purity(self):
		for is_new in (True, False):
			with self.subTest(is_new=is_new):
				doc = _pmo(
					__islocal=is_new,
					flags=frappe._dict(ignore_validations=True),
				)
				doc.run_before_save_methods()
				self.assertEqual(doc.metal_purity, "91.9")

	def test_submit_resolves_before_downstream_bom_and_work_order(self):
		doc = _pmo(__islocal=False, _action="submit", docstatus=1, metal_purity="92.5")
		observed = []
		doc.submit_bom = Mock(
			side_effect=lambda: observed.append(("bom", doc.metal_purity))
		)
		doc.create_material_requests = Mock()
		with (
			patch.object(pmo_mod, "set_metal_tolerance_table", return_value=False),
			patch.object(pmo_mod, "set_diamond_tolerance_table", return_value=False),
			patch.object(
				pmo_mod,
				"create_manufacturing_work_order",
				side_effect=lambda pmo: observed.append(("mwo", pmo.metal_purity)),
			),
		):
			doc.run_before_save_methods()
			doc.on_submit()
		self.assertEqual(observed, [("bom", "91.9"), ("mwo", "91.9")])
		self.assertEqual(len(self.criteria_queries), 1)


class TestManufacturingPlanMetalType(UnitTestCase):
	def _create_order(self, *, source, bom_type, cached):
		doc = frappe._dict(select_manufacture_order="Manufacturing")
		row = frappe._dict(
			manufacturing_order_qty=1,
			qty_per_manufacturing_order=1,
			sales_order="SO-1" if source == "sales_order" else None,
			docname="SO-ITEM-1",
			mwo="MWO-1" if source == "mwo" else None,
			manufacturing_bom="BOM-1",
			customer="CUSTOMER-1",
			diamond_quality=None,
		)
		source_details = {
			"metal_type": "Gold",
			"metal_touch": "22KT",
			"metal_colour": "Yellow",
		}
		bom_details = {
			"metal_type_": bom_type,
			"metal_touch": None,
			"metal_colour": None,
		}
		cache = {"customer_data": {"CUSTOMER-1": {"is_internal_customer": False}}}
		if cached:
			map_name, key = (
				("so_data", row.docname)
				if source == "sales_order"
				else ("mwo_data", row.mwo)
			)
			cache[map_name] = {key: source_details}
			cache["bom_data"] = {"BOM-1": bom_details}

		with (
			patch.object(
				mp_mod.frappe, "get_value", return_value=source_details.copy()
			),
			patch.object(mp_mod.frappe.db, "get_value", return_value=bom_details),
			patch.object(mp_mod, "make_manufacturing_order") as make_order,
		):
			self.assertEqual(mp_mod.create_manufacturing_order(doc, row, cache), 1)
		make_order.assert_called_once()
		return make_order.call_args.kwargs["so_det"]

	def test_blank_bom_type_preserves_sales_order_and_mwo_type(self):
		for source in ("sales_order", "mwo"):
			for cached in (True, False):
				for blank in (None, ""):
					with self.subTest(source=source, cached=cached, blank=blank):
						details = self._create_order(
							source=source, bom_type=blank, cached=cached
						)
						self.assertEqual(details["metal_type"], "Gold")
						self.assertEqual(details["metal_touch"], "22KT")

	def test_populated_bom_type_retains_precedence(self):
		for source in ("sales_order", "mwo"):
			for cached in (True, False):
				with self.subTest(source=source, cached=cached):
					details = self._create_order(
						source=source, bom_type="Silver", cached=cached
					)
					self.assertEqual(details["metal_type"], "Silver")
