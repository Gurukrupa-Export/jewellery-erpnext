# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""The as-built (finished goods) BOM's standard items are what the piece consumed.

SNC 48ara8a7ti failed with "BOM BOM-RI00650-006-008 must be submitted". create_finished_goods_bom
copied the design BOM, whose only standard item is the finished item itself (the order form's
placeholder). ERPNext filled that row's bom_no from Item.default_bom, a draft as-built BOM, and
validate_bom_no refused it; with a submitted default the same row raises BOM recursion instead.

validate_bom_no skips its "must be submitted" check under frappe.in_test, so these tests pin the
mechanism ERPNext actually runs (set_bom_material_details and validate_materials), not that check.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import bom as bom_events
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	_consumed_bom_items,
	_detail_tables_rebuild_items,
)

FG_ITEM = "RI00650-006"
GOLD = "M-G-22KT-91.75-Y"
DIAMOND = "D-NT-RO-6B-+7-7.5"
# Stands in for BOM-RI00650-006-008, RI00650-006's draft default. A name that exists nowhere, so
# the control fails the same way on every site: under frappe.in_test ERPNext skips the "must be
# submitted" check, and on kg-gk the real -008 would pass the remaining checks.
DRAFT_DEFAULT = "BOM-_TEST-FG-DRAFT-DEFAULT"

# SNC 48ara8a7ti: two batches of the customer's 22KT and one company diamond lot.
SNC_48ARA8A7TI = [
	{"item_code": DIAMOND, "qty": 2.36, "uom": "Carat"},
	{"item_code": GOLD, "qty": 2.021, "uom": "Gram"},
	{"item_code": GOLD, "qty": 6.929, "uom": "Gram"},
]


class TestConsumedBomItems(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_each_material_once_summed_across_batches(self):
		rows = {r["item_code"]: r for r in _consumed_bom_items(SNC_48ARA8A7TI, FG_ITEM)}

		self.assertEqual(set(rows), {GOLD, DIAMOND})
		self.assertAlmostEqual(rows[GOLD]["qty"], 8.95, places=9)
		self.assertAlmostEqual(rows[DIAMOND]["qty"], 2.36, places=9)
		self.assertEqual(rows[GOLD]["uom"], "Gram")
		self.assertEqual(rows[DIAMOND]["uom"], "Carat")

	def test_the_finished_item_is_never_its_own_raw_material(self):
		data = [*SNC_48ARA8A7TI, {"item_code": FG_ITEM, "qty": 1, "uom": "Nos"}]

		self.assertNotIn(
			FG_ITEM, [r["item_code"] for r in _consumed_bom_items(data, FG_ITEM)]
		)

	def test_rows_never_link_a_sub_assembly_bom(self):
		for row in _consumed_bom_items(SNC_48ARA8A7TI, FG_ITEM):
			self.assertEqual(row["do_not_explode"], 1)
			self.assertNotIn("bom_no", row)

	def test_zero_and_blank_rows_are_dropped(self):
		data = [
			{"item_code": GOLD, "qty": 0, "uom": "Gram"},
			{"item_code": None, "qty": 1, "uom": "Gram"},
			{"item_code": DIAMOND, "qty": -1, "uom": "Carat"},
		]

		self.assertEqual(_consumed_bom_items(data, FG_ITEM), [])


def _item_det(item_code):
	"""What Item gives ERPNext for every row: RI00650-006's draft default, as on the site."""
	return frappe._dict(
		default_bom=DRAFT_DEFAULT,
		include_item_in_manufacturing=1,
		item_name=item_code,
		description=item_code,
		image="",
		stock_uom="Gram",
	)


class TestErpnextLeavesConsumedRowsUnlinked(IntegrationTestCase):
	"""ERPNext's own BOM code, run on the rows create_finished_goods_bom now builds."""

	@classmethod
	def setUpClass(cls):
		pass

	def _bom(self, rows):
		bom = frappe.new_doc("BOM")
		bom.item = FG_ITEM
		for row in rows:
			bom.append("items", row)
		bom.get_item_det = _item_det
		bom.get_rm_rate = lambda *args, **kwargs: 0
		return bom

	def test_a_draft_item_default_never_reaches_a_consumed_row(self):
		bom = self._bom(_consumed_bom_items(SNC_48ARA8A7TI, FG_ITEM))
		bom.set_bom_material_details()

		self.assertEqual([row.bom_no for row in bom.items], ["", ""])
		bom.validate_materials()

	def test_the_copied_design_row_is_what_picked_up_the_draft_default(self):
		"""The mechanism behind the failure: a plain row takes Item.default_bom."""
		bom = self._bom([{"item_code": FG_ITEM, "qty": 1, "uom": "Nos"}])
		bom.set_bom_material_details()

		self.assertEqual(bom.items[0].bom_no, DRAFT_DEFAULT)
		with self.assertRaises(frappe.ValidationError):
			bom.validate_materials()


class TestSubmitRebuildListsEachMaterialOnce(IntegrationTestCase):
	"""doc_events/bom.py appends the detail variants on submit; the as-built rows make way."""

	@classmethod
	def setUpClass(cls):
		pass

	def _as_built(self):
		bom = frappe.new_doc("BOM")
		bom.item = FG_ITEM
		bom.bom_type = "Finish Goods"
		for row in _consumed_bom_items(SNC_48ARA8A7TI, FG_ITEM):
			bom.append("items", row)
		bom.append("metal_detail", {"item_variant": GOLD, "quantity": 8.95})
		bom.append("diamond_detail", {"item_variant": DIAMOND, "quantity": 2.36})
		return bom

	def _rebuild(self, bom):
		uoms = {GOLD: "Gram", DIAMOND: "Carat"}
		with (
			patch.object(bom_events.frappe, "get_all", return_value=[]),
			patch.object(
				bom_events.frappe.db,
				"get_value",
				side_effect=lambda doctype, name, field=None, *a, **kw: uoms.get(name)
				if field == "stock_uom"
				else None,
			),
		):
			bom_events._set_bom_items_by_child_tables(bom, None)

	def test_each_material_appears_once_after_submit(self):
		bom = self._as_built()
		self.assertTrue(_detail_tables_rebuild_items(bom))
		bom.items = []
		self._rebuild(bom)

		self.assertEqual(
			sorted((row.item_code, row.qty) for row in bom.items),
			[(DIAMOND, 2.36), (GOLD, 8.95)],
		)

	def test_without_making_way_every_material_is_listed_twice(self):
		"""Why create_finished_goods_bom empties the table before submit."""
		bom = self._as_built()
		self._rebuild(bom)

		self.assertEqual(len(bom.items), 4)

	def test_nothing_to_rebuild_from_keeps_the_as_built_rows(self):
		bom = frappe.new_doc("BOM")
		bom.append("items", {"item_code": GOLD, "qty": 1})

		self.assertFalse(_detail_tables_rebuild_items(bom))
