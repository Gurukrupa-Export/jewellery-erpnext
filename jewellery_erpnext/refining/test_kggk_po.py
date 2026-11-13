# Copyright (c) 2026, Nirali and contributors
# See license.txt
"""The KGGK <-> refiner (GK) Purchase Order round trip.

Both halves run on one test site: the SERVER half (what KGGK runs) against a real submitted
Purchase Order, the CLIENT half (what the refiner runs) with the HTTP calls to KGGK mocked.
"""

from unittest.mock import patch

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, today

from jewellery_erpnext.customer_subcontracting.customer_goods_eligibility import (
	can_be_customer_goods,
)
from jewellery_erpnext.refining import kggk_po
from jewellery_erpnext.refining.constants import (
	REFINING_TYPE_SERIAL,
	REFINING_TYPE_UNUSED,
	SOURCE_TYPE_BOM_COMPONENT,
	SOURCE_TYPE_SERIAL,
	SOURCE_TYPE_UNUSED,
)

COMPANY = "Test_Company"
SUPPLIER = "Test_Supplier"
CUSTOMER_NAME = "_Test KGGK Refining Customer"
SERVICE_ITEM = "REF-RMS-001"
METAL_ITEM = "ML-G-18KT-75.4-P"
KGGK_PO = "KGGK-PO-TEST-00001"
MOD = "jewellery_erpnext.refining.kggk_po"


def _ensure_po_fields():
	from jewellery_erpnext.patches.add_gk_refining_receipt_fields import execute

	execute()


def _no_commit(test):
	"""apply/reverse commit before releasing their lock; keep the test transaction intact."""
	patcher = patch.object(frappe.db, "commit")
	patcher.start()
	test.addCleanup(patcher.stop)


def _ordered_qty(item_code, warehouse):
	return flt(
		frappe.db.get_value(
			"Bin", {"item_code": item_code, "warehouse": warehouse}, "ordered_qty"
		)
	)


class TestKggkPoServer(IntegrationTestCase):
	"""What KGGK runs when the refiner calls it."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_po_fields()
		cls.warehouse = frappe.db.get_value(
			"Warehouse", {"company": COMPANY, "is_group": 0}, "name"
		)

	def setUp(self):
		_no_commit(self)

	def _po(self, submit=True, qty=10, rate=5):
		po = frappe.new_doc("Purchase Order")
		po.supplier = SUPPLIER
		po.company = COMPANY
		po.transaction_date = today()
		po.schedule_date = add_days(today(), 1)
		po.purchase_type = None
		po.append(
			"items",
			{
				"item_code": SERVICE_ITEM,
				"qty": qty,
				"uom": "Gram",
				"rate": rate,
				"warehouse": self.warehouse,
				"schedule_date": add_days(today(), 1),
			},
		)
		po.insert(ignore_permissions=True)
		if submit:
			po.submit()
		# A real one points at the KGGK Refining Entry that raised it; the endpoints only
		# need the link to be set.
		po.db_set("refining_entry", "RFN-TEST-LINK", update_modified=False)
		return frappe.get_doc("Purchase Order", po.name)

	def test_apply_marks_every_line_received(self):
		po = self._po()
		before = _ordered_qty(SERVICE_ITEM, self.warehouse)

		result = kggk_po.apply_gk_receipt(po.name, "RFN-GK-1", "gk.test")

		po.reload()
		self.assertEqual(result["action"], "applied")
		self.assertEqual(flt(po.per_received), 100)
		self.assertEqual(flt(po.items[0].received_qty), 10)
		self.assertEqual(po.get(kggk_po.GK_ENTRY_FIELD), "RFN-GK-1")
		self.assertTrue(po.get(kggk_po.GK_RECEIVED_ON_FIELD))
		self.assertIn(po.status, ("To Bill", "Completed"))
		# Nothing is pending any more, so the Bin no longer counts it as ordered.
		self.assertEqual(
			flt(_ordered_qty(SERVICE_ITEM, self.warehouse), 3), flt(before - 10, 3)
		)

	def test_apply_is_idempotent_and_refuses_another_entry(self):
		po = self._po()
		kggk_po.apply_gk_receipt(po.name, "RFN-GK-1")

		again = kggk_po.apply_gk_receipt(po.name, "RFN-GK-1")
		self.assertEqual(again["action"], "already_applied")

		with self.assertRaises(kggk_po.GkReceiptConflict):
			kggk_po.apply_gk_receipt(po.name, "RFN-GK-2")

	def test_reverse_restores_the_order(self):
		po = self._po()
		before = _ordered_qty(SERVICE_ITEM, self.warehouse)
		kggk_po.apply_gk_receipt(po.name, "RFN-GK-1")

		with self.assertRaises(kggk_po.GkReceiptConflict):
			kggk_po.reverse_gk_receipt(po.name, "RFN-GK-2")

		result = kggk_po.reverse_gk_receipt(po.name, "RFN-GK-1")
		po.reload()
		self.assertEqual(result["action"], "reversed")
		self.assertEqual(flt(po.per_received), 0)
		self.assertEqual(flt(po.items[0].received_qty), 0)
		self.assertFalse(po.get(kggk_po.GK_ENTRY_FIELD))
		self.assertEqual(po.status, "To Receive and Bill")
		self.assertEqual(
			flt(_ordered_qty(SERVICE_ITEM, self.warehouse), 3), flt(before, 3)
		)
		# Nothing left to reverse.
		self.assertEqual(
			kggk_po.reverse_gk_receipt(po.name, "RFN-GK-1")["action"], "not_applied"
		)

	def test_draft_po_cannot_be_received(self):
		po = self._po(submit=False)
		with self.assertRaises(frappe.ValidationError):
			kggk_po.apply_gk_receipt(po.name, "RFN-GK-1")

	def test_list_drops_a_received_po(self):
		po = self._po()
		names = [r.name for r in kggk_po.list_refining_pos(SUPPLIER)]
		self.assertIn(po.name, names)

		kggk_po.apply_gk_receipt(po.name, "RFN-GK-1")
		names = [r.name for r in kggk_po.list_refining_pos(SUPPLIER)]
		self.assertNotIn(po.name, names)

	def test_sent_material_rows(self):
		def entry(refining_type, rows):
			return frappe._dict(
				refining_type=refining_type,
				material_items=[frappe._dict(row) for row in rows],
			)

		rows = [
			{"item_code": "A", "qty": 2, "uom": "Gram", "source_type": SOURCE_TYPE_UNUSED},
			{"item_code": "B", "qty": 1, "uom": "Nos", "source_type": SOURCE_TYPE_SERIAL},
			{"item_code": "C", "qty": 1.5, "uom": "Gram", "source_type": SOURCE_TYPE_BOM_COMPONENT},
			{"item_code": "D", "qty": 0, "uom": "Gram", "source_type": SOURCE_TYPE_UNUSED},
		]
		# Loose material: the BOM breakdown is display-only, the zero row is dropped.
		self.assertEqual(
			[r["item_code"] for r in kggk_po.sent_material_rows(entry(REFINING_TYPE_UNUSED, rows))],
			["A"],
		)
		# Serial: the piece travels as its BOM breakdown, never as the piece count.
		self.assertEqual(
			[r["item_code"] for r in kggk_po.sent_material_rows(entry(REFINING_TYPE_SERIAL, rows))],
			["A", "C"],
		)


def _po_payload(materials=None, refining_type="Work Order Refining"):
	return {
		"purchase_order": KGGK_PO,
		"supplier": "GJSU0569",
		"supplier_name": "Gurukrupa Export Private Limited - ST",
		"company": "KG GK Jewellers Private Limited",
		"refining_entry": "RFN-MWO-26-09999",
		"refining_type": refining_type,
		"gk_refining_entry": None,
		"materials": materials
		if materials is not None
		else [
			{
				"item_code": METAL_ITEM,
				"qty": 2.5,
				"uom": "Gram",
				"purity": None,
				"source_type": "MWO",
				"is_consumable": 0,
			}
		],
	}


def _ensure_customer():
	name = frappe.db.get_value("Customer", {"customer_name": CUSTOMER_NAME}, "name")
	if name:
		return name
	return (
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": CUSTOMER_NAME,
				"customer_type": "Company",
				"customer_group": frappe.db.get_value(
					"Customer Group", {"is_group": 0}, "name"
				),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


class TestKggkPoRefiner(IntegrationTestCase):
	"""What the refiner (GK) runs: a Refining Entry filled from, and receiving, KGGK's PO."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.customer = _ensure_customer()

	def setUp(self):
		self.fetch = patch(f"{MOD}.fetch_refining_po", return_value=_po_payload()).start()
		self.push = patch(f"{MOD}.push_receipt", return_value={"action": "applied"}).start()
		self.reverse = patch(
			f"{MOD}.push_reversal", return_value={"action": "reversed"}
		).start()
		self.addCleanup(patch.stopall)
		self._clear_po_entries()

	def _clear_po_entries(self):
		# Earlier tests leave entries holding the same PO; free it for this one.
		frappe.db.set_value(
			"Refining Entry",
			{"source_purchase_order": KGGK_PO},
			"source_purchase_order",
			None,
		)

	def _entry(self):
		re = frappe.new_doc("Refining Entry")
		re.company = COMPANY
		re.refining_type = REFINING_TYPE_UNUSED
		re.department = "Waxing - T"
		re.refining_department = "Refinery - T"
		re.manufacturer = "Shubh"
		re.customer = self.customer
		return re

	def _filled(self):
		re = self._entry()
		re.set_from_kggk_purchase_order(KGGK_PO)
		re.save()
		return re

	def test_fill_from_po(self):
		re = self._entry()
		info = re.set_from_kggk_purchase_order(KGGK_PO)

		self.fetch.assert_called_once_with(KGGK_PO)
		# A Work Order consignment arrives as loose material.
		self.assertEqual(re.refining_type, REFINING_TYPE_UNUSED)
		self.assertEqual(re.source_purchase_order, KGGK_PO)
		self.assertEqual(re.source_refining_entry, "RFN-MWO-26-09999")
		self.assertEqual(re.source_po_company, "KG GK Jewellers Private Limited")
		self.assertEqual(len(re.material_items), 1)
		self.assertEqual(re.material_items[0].item_code, METAL_ITEM)
		self.assertEqual(flt(re.material_items[0].qty, 3), 2.5)
		self.assertEqual(re.material_items[0].source_type, SOURCE_TYPE_UNUSED)
		self.assertEqual(info["rows"], 1)
		# A Customer picked by the user is kept.
		self.assertEqual(re.customer, self.customer)

	def test_missing_item_is_reported(self):
		self.fetch.return_value = _po_payload(
			materials=[
				{"item_code": "_No Such Item Here", "qty": 1, "uom": "Gram", "is_consumable": 0}
			]
		)
		with self.assertRaises(frappe.ValidationError):
			self._entry().set_from_kggk_purchase_order(KGGK_PO)

	def test_customer_is_mandatory(self):
		re = self._entry()
		re.customer = None
		re.set_from_kggk_purchase_order(KGGK_PO)
		re.customer = None
		with self.assertRaises(frappe.ValidationError):
			re.save()

	def test_po_cannot_be_used_twice(self):
		self._filled()
		with self.assertRaises(frappe.ValidationError):
			self._filled()

	def test_submit_receives_the_material_and_tells_kggk(self):
		re = self._filled()
		apply_workflow(re, "Submit")
		re.reload()

		self.push.assert_called_once()
		self.assertEqual(self.push.call_args[0][0].name, re.name)
		self.assertTrue(re.po_receipt_se)
		# No transfer out of a source warehouse: the material was never here.
		self.assertFalse(re.material_transfer_se)

		se = frappe.get_doc("Stock Entry", re.po_receipt_se)
		self.assertEqual(se.purpose, "Material Receipt")
		self.assertEqual(se.custom_refining_entry, re.name)
		self.assertEqual(se.items[0].t_warehouse, re.refining_warehouse)
		self.assertEqual(flt(se.items[0].qty, 3), 2.5)

		row = re.material_items[0]
		self.assertEqual(row.warehouse, re.refining_warehouse)
		if frappe.db.get_value("Item", METAL_ITEM, "has_batch_no"):
			self.assertEqual(row.batch_no, se.items[0].batch_no)
			owner = frappe.db.get_value(
				"Batch",
				row.batch_no,
				["custom_inventory_type", "custom_customer"],
				as_dict=True,
			)
			if can_be_customer_goods(METAL_ITEM):
				self.assertEqual(owner.custom_inventory_type, "Customer Goods")
				self.assertEqual(owner.custom_customer, self.customer)
				self.assertEqual(se.items[0].inventory_type, "Customer Goods")

	def test_kggk_refusal_fails_the_submit(self):
		self.push.side_effect = frappe.ValidationError("KGGK refused")
		re = self._filled()
		with self.assertRaises(frappe.ValidationError):
			apply_workflow(re, "Submit")

	def test_cancel_reverses_receipt_and_kggk(self):
		re = self._filled()
		apply_workflow(re, "Submit")
		re.reload()
		se_name = re.po_receipt_se

		re.cancel()

		self.assertEqual(frappe.db.get_value("Stock Entry", se_name, "docstatus"), 2)
		self.reverse.assert_called_once()
		self.assertEqual(self.reverse.call_args[0][0].name, re.name)

	def test_processing_copy_refines_the_received_batches(self):
		re = self._filled()
		apply_workflow(re, "Submit")
		re.reload()
		received_batch = re.material_items[0].batch_no

		copy = frappe.get_doc("Refining Entry", re.receive_materials())
		self.assertEqual(copy.source_purchase_order, KGGK_PO)
		self.assertEqual(copy.material_items[0].batch_no, received_batch)
		# The copy shares the PO without counting as a second use of it.
		copy.save()
		# Receiving happened once, on the parent.
		self.push.assert_called_once()
