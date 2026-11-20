"""A repair sent in on a customer's Purchase Order carries two BOMs down to the work order.

The PO's Copy BOM is what the repair is manufactured on (PMO / MWO ``master_bom``); its Serial
Id BOM is the serial's own BOM, which Unpack Serial No books (``serial_id_bom``). Both arrive
under the ordering site's BOM names and must be translated to this site's BOMs.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import purchase_order as po_events
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan import (
	manufacturing_plan as mp_module,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	ManufacturingWorkOrder,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	ParentManufacturingOrder,
)

PO_MODULE = "jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order"


class TestResolveSiteBom(UnitTestCase):
	def test_the_sync_stamp_wins_over_a_same_named_bom(self):
		with patch(f"{PO_MODULE}.frappe.db.has_column", return_value=True), patch(
			f"{PO_MODULE}.frappe.db.sql", return_value=(("BOM-EA1-004",),)
		) as sql, patch(f"{PO_MODULE}.frappe.db.exists", return_value=True):
			self.assertEqual(po_events.resolve_site_bom("BOM-EA1-001"), "BOM-EA1-004")
		self.assertEqual(sql.call_args.args[1], "BOM-EA1-001")

	def test_an_unstamped_name_that_exists_here_is_kept(self):
		with patch(f"{PO_MODULE}.frappe.db.has_column", return_value=True), patch(
			f"{PO_MODULE}.frappe.db.sql", return_value=()
		), patch(f"{PO_MODULE}.frappe.db.exists", return_value=True):
			self.assertEqual(po_events.resolve_site_bom("BOM-LOCAL-1"), "BOM-LOCAL-1")

	def test_a_site_without_the_stamp_columns_falls_back_to_the_name(self):
		with patch(f"{PO_MODULE}.frappe.db.has_column", return_value=False), patch(
			f"{PO_MODULE}.frappe.db.sql"
		) as sql, patch(f"{PO_MODULE}.frappe.db.exists", return_value=True):
			self.assertEqual(po_events.resolve_site_bom("BOM-LOCAL-1"), "BOM-LOCAL-1")
		sql.assert_not_called()

	def test_an_unknown_bom_resolves_to_none(self):
		with patch(f"{PO_MODULE}.frappe.db.has_column", return_value=True), patch(
			f"{PO_MODULE}.frappe.db.sql", return_value=()
		), patch(f"{PO_MODULE}.frappe.db.exists", return_value=False):
			self.assertIsNone(po_events.resolve_site_bom("BOM-NOT-HERE"))
		self.assertIsNone(po_events.resolve_site_bom(None))


class TestValidateCopyBomSerialIdBom(UnitTestCase):
	def _po(self, *rows):
		return SimpleNamespace(name="PO-1", items=[frappe._dict(r) for r in rows])

	def test_both_boms_come_back_translated(self):
		site = {"BOM-GK-COPY": "BOM-KG-COPY", "BOM-GK-SERIAL": "BOM-KG-SERIAL"}
		po = self._po(
			dict(
				idx=1,
				name="POI-1",
				custom_copy_bom="BOM-GK-COPY",
				custom_serial_id_bom="BOM-GK-SERIAL",
			),
			dict(idx=2, name="POI-2", custom_copy_bom="BOM-GK-COPY"),
		)
		with patch(f"{PO_MODULE}.resolve_site_bom", side_effect=site.get):
			resolved = po_events.validate_copy_bom(po)
		self.assertEqual(resolved["POI-1"], ("BOM-KG-COPY", "BOM-KG-SERIAL"))
		self.assertEqual(resolved["POI-2"], ("BOM-KG-COPY", None))

	def test_an_unsynced_serial_id_bom_is_refused(self):
		po = self._po(
			dict(
				idx=1,
				name="POI-1",
				custom_copy_bom="BOM-GK-COPY",
				custom_serial_id_bom="BOM-GK-SERIAL",
			)
		)
		with patch(
			f"{PO_MODULE}.resolve_site_bom",
			side_effect={"BOM-GK-COPY": "BOM-KG-COPY"}.get,
		), self.assertRaises(frappe.ValidationError) as ctx:
			po_events.validate_copy_bom(po)
		self.assertIn("BOM-GK-SERIAL", str(ctx.exception))
		self.assertIn("Row #1", str(ctx.exception))


class TestRepairMasterBom(UnitTestCase):
	def _row(self):
		return frappe._dict(
			bom="BOM-COPY", manufacturing_bom="BOM-COPY", serial_id_bom="BOM-SERIAL"
		)

	def test_a_purchase_order_repair_is_manufactured_on_its_copy_bom(self):
		master = mp_module._resolve_repair_master_bom(
			self._row(), {"custom_po_details": "POI-1"}
		)
		self.assertEqual(master, "BOM-COPY")

	def test_other_repairs_keep_the_serial_bom(self):
		master = mp_module._resolve_repair_master_bom(self._row(), {})
		self.assertEqual(master, "BOM-SERIAL")


class TestCarriedSerialIdBomIsKept(UnitTestCase):
	PMO_MODULE = (
		"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order."
		"parent_manufacturing_order"
	)

	def _pmo(self, **values):
		pmo = ParentManufacturingOrder.__new__(ParentManufacturingOrder)
		pmo.__dict__.update(
			dict(custom_tracking_bom=None, serial_no="SR-1", sales_order_item="SOI-1", **values)
		)
		pmo.db_set = lambda *args, **kwargs: setattr(pmo, "_db_set", args)
		return pmo

	def test_purchase_order_serial_id_bom_survives_after_insert(self):
		pmo = self._pmo(serial_id_bom="BOM-KG-SERIAL")
		with patch(f"{self.PMO_MODULE}.frappe.db.get_value", return_value="POI-1"), patch(
			f"{self.PMO_MODULE}.get_serial_bom", return_value="BOM-OTHER"
		):
			pmo.after_insert()
		self.assertFalse(hasattr(pmo, "_db_set"))

	def test_other_repairs_still_take_the_serials_bom(self):
		pmo = self._pmo(serial_id_bom="BOM-DESIGN")
		with patch(f"{self.PMO_MODULE}.frappe.db.get_value", return_value=None), patch(
			f"{self.PMO_MODULE}.get_serial_bom", return_value="BOM-OTHER"
		):
			pmo.after_insert()
		self.assertEqual(pmo._db_set, ("serial_id_bom", "BOM-OTHER"))


class TestUnpackRepairOrderLink(UnitTestCase):
	MWO_MODULE = (
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order."
		"manufacturing_work_order"
	)

	def _mwo(self):
		mwo = ManufacturingWorkOrder.__new__(ManufacturingWorkOrder)
		mwo.__dict__["manufacturing_order"] = "PMO-1"
		return mwo

	def test_link_detection(self):
		cases = [
			(("Repair Order", "RO-1"), True),
			(("Repair Order", None), False),
			((None, None), False),
			(None, False),
		]
		for value, expected in cases:
			with patch(f"{self.MWO_MODULE}.frappe.db.get_value", return_value=value):
				self.assertIs(self._mwo()._has_repair_order_link(), expected, value)
