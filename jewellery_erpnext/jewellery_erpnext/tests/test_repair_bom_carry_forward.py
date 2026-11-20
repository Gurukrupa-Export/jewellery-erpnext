"""A repair sent in on a customer's Purchase Order carries two BOMs down to the work order.

The PO's Copy BOM is what the repair is manufactured on (PMO / MWO ``master_bom``); its Serial
Id BOM is the serial's own BOM, which Unpack Serial No books (``serial_id_bom``). The ordering site's
BOM names are translated to this site's by the ``get_purchase_order_items`` Server Script.
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
from jewellery_erpnext.utils import is_purchase_order_repair

PO_MODULE = "jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order"


class TestValidateCopyBomSerialIdBom(UnitTestCase):
	def _po(self, *rows):
		return SimpleNamespace(name="PO-1", items=[frappe._dict(r) for r in rows])

	def test_names_are_checked_as_they_are(self):
		po = self._po(
			dict(idx=1, custom_copy_bom="BOM-COPY", custom_serial_id_bom="BOM-SERIAL"),
			dict(idx=2, custom_copy_bom="BOM-COPY"),
		)
		with patch(f"{PO_MODULE}.frappe.db.exists", return_value=True) as exists:
			po_events.validate_copy_bom(po)
		self.assertEqual(
			sorted(c.args for c in exists.call_args_list),
			[("BOM", "BOM-COPY"), ("BOM", "BOM-COPY"), ("BOM", "BOM-SERIAL")],
		)

	def test_an_unknown_serial_id_bom_is_refused_with_its_row(self):
		po = self._po(
			dict(idx=1, custom_copy_bom="BOM-COPY", custom_serial_id_bom="BOM-NOT-HERE")
		)
		with patch(
			f"{PO_MODULE}.frappe.db.exists",
			side_effect=lambda dt, name: name == "BOM-COPY",
		), self.assertRaises(frappe.ValidationError) as ctx:
			po_events.validate_copy_bom(po)
		self.assertIn("BOM-NOT-HERE", str(ctx.exception))
		self.assertIn("Row #1", str(ctx.exception))


class TestIsPurchaseOrderRepair(UnitTestCase):
	def test_reads_custom_po_details_of_the_sales_order_item(self):
		with patch(
			"jewellery_erpnext.utils.frappe.db.get_value", return_value="POI-1"
		) as gv:
			self.assertTrue(is_purchase_order_repair("SOI-1"))
		gv.assert_called_once_with("Sales Order Item", "SOI-1", "custom_po_details")
		with patch("jewellery_erpnext.utils.frappe.db.get_value", return_value=None):
			self.assertFalse(is_purchase_order_repair("SOI-1"))
		self.assertFalse(is_purchase_order_repair(None))


class TestRepairMasterBom(UnitTestCase):
	def _row(self):
		return frappe._dict(
			bom="BOM-COPY", manufacturing_bom="BOM-COPY", serial_id_bom="BOM-SERIAL"
		)

	def test_a_purchase_order_repair_is_manufactured_on_its_copy_bom(self):
		with patch.object(mp_module, "get_repair_order_design_bom", return_value=None):
			master = mp_module._resolve_repair_master_bom(
				self._row(), {"custom_po_details": "POI-1"}
			)
		self.assertEqual(master, "BOM-COPY")

	def test_other_header_repairs_keep_the_serial_bom(self):
		with patch.object(mp_module, "get_repair_order_design_bom", return_value=None):
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
			dict(
				custom_tracking_bom=None,
				serial_no="SR-1",
				sales_order_item="SOI-1",
				**values,
			)
		)
		pmo.db_set = lambda *args, **kwargs: setattr(pmo, "_db_set", args)
		return pmo

	def test_purchase_order_serial_id_bom_survives_after_insert(self):
		pmo = self._pmo(serial_id_bom="BOM-KG-SERIAL")
		with patch(
			f"{self.PMO_MODULE}.is_purchase_order_repair", return_value=True
		), patch(f"{self.PMO_MODULE}.get_serial_bom", return_value="BOM-OTHER"):
			pmo.after_insert()
		self.assertFalse(hasattr(pmo, "_db_set"))

	def test_other_repairs_still_take_the_serials_bom(self):
		pmo = self._pmo(serial_id_bom="BOM-DESIGN")
		with patch(
			f"{self.PMO_MODULE}.is_purchase_order_repair", return_value=False
		), patch(f"{self.PMO_MODULE}.get_serial_bom", return_value="BOM-OTHER"):
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
