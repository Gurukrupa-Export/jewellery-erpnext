"""PMO Refinery Work Order: creation rules, material transfer rows, and its exclusion from the
regular flows (tagging roll-up, Serial Number submit, Employee IR, Department IR, Stock Entry,
Process Transfer, Main Slip).

DB-free: every lookup the code makes is patched, so these run without fixtures.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.doc_events import (
	refinery_mwo,
)

MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.doc_events.refinery_mwo"
MWO_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order"
CENTRAL = "Central - T"


def _meta_with_flag():
	meta = MagicMock()
	meta.has_field.return_value = True
	return meta


def _ir(doctype, **fields):
	doc = frappe._dict(doctype=doctype, docstatus=0, **fields)
	doc.get = lambda key, default=None: doc[key] if key in doc else default
	return doc


def _row(idx, mwo, mop=None):
	row = frappe._dict(idx=idx, manufacturing_work_order=mwo, manufacturing_operation=mop)
	return row


class TestRefineryMwoGuards(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		return

	def test_filter_excludes_refinery_work_orders(self):
		with patch.object(frappe, "get_meta", return_value=_meta_with_flag()):
			filters = refinery_mwo.exclude_refinery_filter({"manufacturing_order": "PMO-1"})
		self.assertEqual(filters, {"manufacturing_order": "PMO-1", "is_refinery_mwo": 0})

	def test_filter_is_unchanged_before_migrate(self):
		meta = MagicMock()
		meta.has_field.return_value = False
		with patch.object(frappe, "get_meta", return_value=meta):
			filters = refinery_mwo.exclude_refinery_filter({"manufacturing_order": "PMO-1"})
		self.assertEqual(filters, {"manufacturing_order": "PMO-1"})

	def _ir_check(self, func, doc, table, refinery, refinery_dept=CENTRAL):
		with (
			patch(f"{MODULE}.refinery_mwos", return_value=refinery),
			patch(f"{MODULE}.get_refinery_department", return_value=refinery_dept),
		):
			func(doc, table)

	def test_employee_ir_refuses_refinery_work_order(self):
		doc = _ir(
			"Employee IR",
			type="Issue",
			employee_ir_operations=[_row(1, "MWO-REG"), _row(2, "MWO-REF")],
		)
		with self.assertRaisesRegex(frappe.ValidationError, "MWO-REF"):
			self._ir_check(
				refinery_mwo.block_refinery_in_employee_ir,
				doc,
				"employee_ir_operations",
				{"MWO-REF": (CENTRAL, "MFR")},
			)

	def test_employee_ir_receive_is_refused_too(self):
		doc = _ir("Employee IR", type="Receive", employee_ir_operations=[_row(1, "MWO-REF")])
		with self.assertRaises(frappe.ValidationError):
			self._ir_check(
				refinery_mwo.block_refinery_in_employee_ir,
				doc,
				"employee_ir_operations",
				{"MWO-REF": (CENTRAL, "MFR")},
			)

	def test_employee_ir_without_refinery_work_order_passes(self):
		doc = _ir("Employee IR", type="Issue", employee_ir_operations=[_row(1, "MWO-REG")])
		self._ir_check(
			refinery_mwo.block_refinery_in_employee_ir, doc, "employee_ir_operations", {}
		)

	def test_department_ir_inside_refinery_department_passes(self):
		doc = _ir(
			"Department IR",
			type="Issue",
			current_department=CENTRAL,
			next_department=CENTRAL,
			department_ir_operation=[_row(1, "MWO-REF")],
		)
		self._ir_check(
			refinery_mwo.validate_refinery_department_ir,
			doc,
			"department_ir_operation",
			{"MWO-REF": (CENTRAL, "MFR")},
		)

	def test_department_ir_to_another_department_is_refused(self):
		doc = _ir(
			"Department IR",
			type="Issue",
			current_department=CENTRAL,
			next_department="Tagging - T",
			department_ir_operation=[_row(1, "MWO-REF")],
		)
		with self.assertRaisesRegex(frappe.ValidationError, "within the refinery department"):
			self._ir_check(
				refinery_mwo.validate_refinery_department_ir,
				doc,
				"department_ir_operation",
				{"MWO-REF": (CENTRAL, "MFR")},
			)

	def test_department_ir_without_refinery_department_set_is_refused(self):
		doc = _ir(
			"Department IR",
			type="Receive",
			current_department=CENTRAL,
			department_ir_operation=[_row(1, "MWO-REF")],
		)
		with self.assertRaises(frappe.ValidationError):
			self._ir_check(
				refinery_mwo.validate_refinery_department_ir,
				doc,
				"department_ir_operation",
				{"MWO-REF": (CENTRAL, "MFR")},
				refinery_dept=None,
			)

	def test_refuse_refinery_mwo_for_split_and_finding_transfer(self):
		with patch(f"{MODULE}.is_refinery_mwo", return_value=True):
			with self.assertRaisesRegex(frappe.ValidationError, "Split Work Order"):
				refinery_mwo.refuse_refinery_mwo("MWO-REF", "Split Work Order")
		with patch(f"{MODULE}.is_refinery_mwo", return_value=False):
			refinery_mwo.refuse_refinery_mwo("MWO-REG", "Split Work Order")


class TestRefineryMwoCreateAndTransfer(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		return

	def test_button_needs_refinery_department_and_a_work_order_in_it(self):
		pmo = frappe._dict(name="PMO-1", docstatus=1, manufacturer="MFR")
		with patch(f"{MODULE}.get_refinery_department", return_value=None):
			self.assertFalse(refinery_mwo.can_create_refinery_work_order(pmo))
		with (
			patch(f"{MODULE}.get_refinery_department", return_value=CENTRAL),
			patch.object(frappe.db, "exists", return_value=None),
		):
			self.assertFalse(refinery_mwo.can_create_refinery_work_order(pmo))
		with (
			patch(f"{MODULE}.get_refinery_department", return_value=CENTRAL),
			patch.object(frappe.db, "exists", return_value="MWO-1") as exists,
		):
			self.assertTrue(refinery_mwo.can_create_refinery_work_order(pmo))
		self.assertEqual(exists.call_args[0][1]["department"], CENTRAL)
		self.assertEqual(exists.call_args[0][1]["is_refinery_mwo"], 0)
		pmo.docstatus = 0
		self.assertFalse(refinery_mwo.can_create_refinery_work_order(pmo))

	def test_first_operation_starts_in_refinery_department_without_operation(self):
		doc = frappe._dict(is_refinery_mwo=1, department=CENTRAL)
		doc.get = lambda key, default=None: doc[key] if key in doc else default
		mop = frappe._dict(department="MPM - T", operation="Op-1", status="Finished")
		self.assertTrue(refinery_mwo.set_refinery_operation_department(doc, mop))
		self.assertEqual((mop.department, mop.operation, mop.status), (CENTRAL, None, "Not Started"))

		regular = frappe._dict(is_refinery_mwo=0)
		regular.get = lambda key, default=None: regular[key] if key in regular else default
		self.assertFalse(refinery_mwo.set_refinery_operation_department(regular, mop))

	def test_source_balance_keeps_positive_rows_and_their_warehouse(self):
		def log(item, batch, qty, to_wh="Central WO - T", from_wh=None, pcs=0):
			return frappe._dict(
				item_code=item,
				batch_no=batch,
				qty_after_transaction_batch_based=qty,
				pcs_after_transaction_batch_based=pcs,
				to_warehouse=to_wh,
				from_warehouse=from_wh,
			)

		latest = [
			log("M-G", "B1", 4.32),
			log("D-NT", "B2", 0.0),
			log("D-NT", "B3", 1.07, pcs=4),
			# An outward row keeps only its source warehouse.
			log("F-CH", "B4", 0.5, to_wh=None, from_wh="Central WO - T"),
		]
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows",
			return_value=latest,
		) as current:
			balance = refinery_mwo.source_balance_rows("MOP-1")
		self.assertEqual(current.call_args[0][0], "MOP-1")
		self.assertEqual(
			[(r.item_code, r.batch_no, r.qty, r.warehouse) for r in balance],
			[
				("M-G", "B1", 4.32, "Central WO - T"),
				("D-NT", "B3", 1.07, "Central WO - T"),
				("F-CH", "B4", 0.5, "Central WO - T"),
			],
		)
		self.assertEqual(balance[1].pcs, 4)

	def test_transfer_rows_land_on_the_refinery_operation(self):
		balance = [frappe._dict(item_code="M-G", batch_no="B1", qty=4.3201, pcs=0, warehouse="Central WO - T")]
		rows = refinery_mwo.build_transfer_rows(balance, "MOP-SRC", "MOP-REF", "MWO-REF", "Refinery WH - T")
		self.assertEqual(rows[0]["qty"], 4.32)
		self.assertEqual(rows[0]["s_warehouse"], "Central WO - T")
		self.assertEqual(rows[0]["t_warehouse"], "Refinery WH - T")
		self.assertEqual(rows[0]["manufacturing_operation"], "MOP-REF")
		self.assertEqual(rows[0]["custom_manufacturing_work_order"], "MWO-REF")

	def _mwo(self, **fields):
		base = dict(
			name="MWO-SRC",
			manufacturing_order="PMO-1",
			docstatus=1,
			is_refinery_mwo=0,
			for_fg=0,
			department=CENTRAL,
			manufacturing_operation="MOP-SRC",
		)
		base.update(fields)
		doc = frappe._dict(base)
		doc.get = lambda key, default=None: doc[key] if key in doc else default
		return doc

	def test_transfer_source_must_be_same_pmo_and_in_refinery_department(self):
		refinery = self._mwo(name="MWO-REF", is_refinery_mwo=1, manufacturing_operation="MOP-REF")
		with patch.object(frappe.db, "get_value", return_value="Not Started"):
			refinery_mwo.validate_transfer_source(refinery, self._mwo())
			with self.assertRaisesRegex(frappe.ValidationError, "PMO-2"):
				refinery_mwo.validate_transfer_source(refinery, self._mwo(manufacturing_order="PMO-2"))
			with self.assertRaisesRegex(frappe.ValidationError, "refinery department"):
				refinery_mwo.validate_transfer_source(refinery, self._mwo(department="Casting - T"))
			with self.assertRaises(frappe.ValidationError):
				refinery_mwo.validate_transfer_source(refinery, self._mwo(for_fg=1))
		with patch.object(frappe.db, "get_value", return_value="In-Transit"):
			with self.assertRaisesRegex(frappe.ValidationError, "In-Transit"):
				refinery_mwo.validate_transfer_source(refinery, self._mwo())

	def test_outward_log_takes_material_off_the_source_operation_only(self):
		item = MagicMock()
		item.as_dict.return_value = {
			"name": "ROW-1",
			"item_code": "M-G",
			"qty": 4.32,
			"batch_no": "B1",
			"s_warehouse": "Central WO - T",
			"t_warehouse": "Refinery WH - T",
			"manufacturing_operation": "MOP-REF",
		}
		se = frappe._dict(
			doctype="Stock Entry",
			name="STE-1",
			stock_entry_type="Material Transfer to Department",
			items=[item],
		)
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.create_mop_log_for_stock_transfer_to_mo"
		) as create_log:
			refinery_mwo._post_source_outward_logs(se, self._mwo())
		voucher, row = create_log.call_args[0][:2]
		self.assertEqual(voucher.manufacturing_work_order, "MWO-SRC")
		self.assertEqual(row.manufacturing_operation, "MOP-SRC")
		self.assertIsNone(row.t_warehouse)
		self.assertTrue(create_log.call_args[1]["outward"])


class TestRefineryMwoLeftOutOfRegularFlow(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		return

	def test_serial_number_submit_ignores_open_refinery_work_order(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
			ManufacturingWorkOrder,
		)

		fg = frappe._dict(name="MWO-FG", manufacturing_order="PMO-1", manufacturer="MFR")
		with (
			patch.object(frappe, "get_meta", return_value=_meta_with_flag()),
			patch.object(frappe.db, "get_value", return_value="Tagging - T"),
			patch.object(frappe, "get_all", return_value=[]) as get_all,
		):
			ManufacturingWorkOrder.validate_other_work_orders(fg)
		self.assertEqual(get_all.call_args[0][1]["is_refinery_mwo"], 0)

	def test_fg_weight_roll_up_skips_refinery_work_orders(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
			ManufacturingWorkOrder,
		)

		fg = MagicMock(name="MWO-FG", manufacturing_order="PMO-1")
		fg.name = "MWO-FG"
		stop = RuntimeError("stop after the sibling query")
		with (
			patch.object(frappe, "get_meta", return_value=_meta_with_flag()),
			patch.object(frappe.db, "get_all", side_effect=stop) as get_all,
		):
			with self.assertRaises(RuntimeError):
				ManufacturingWorkOrder.sync_mwo_weights(fg)
		self.assertEqual(get_all.call_args[0][1]["is_refinery_mwo"], 0)

	def test_product_tolerance_bucket_skips_refinery_rows(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.product_tolerance import (
			bucket_rows_by_pmo,
		)

		def mwo(name, **flags):
			d = frappe._dict(
				name=name,
				manufacturing_order="PMO-1",
				metal_type="Gold",
				is_finding_mwo=0,
				for_fg=0,
				is_refinery_mwo=0,
				qty=1,
			)
			d.update(flags)
			return d

		rows = [
			frappe._dict(manufacturing_work_order="MWO-REG", gross_wt=5.0, net_wt=4.0, finding_wt=0, diamond_wt=0, gemstone_wt=0, manufacturing_operation="MOP-1"),
			frappe._dict(manufacturing_work_order="MWO-REF", gross_wt=9.0, net_wt=9.0, finding_wt=0, diamond_wt=0, gemstone_wt=0, manufacturing_operation="MOP-2"),
		]
		mwo_map = {"MWO-REG": mwo("MWO-REG"), "MWO-REF": mwo("MWO-REF", is_refinery_mwo=1)}
		buckets = bucket_rows_by_pmo(rows, mwo_map)
		self.assertEqual(list(buckets), ["PMO-1"])
		self.assertEqual(buckets["PMO-1"].mops, ["MOP-1"])


def _stock_entry(**fields):
	doc = _ir("Stock Entry", items=[], **fields)
	doc.meta = MagicMock()
	doc.meta.get_label.side_effect = lambda f: f.replace("_", " ").title()
	return doc


class TestRefineryMwoOtherFlows(IntegrationTestCase):
	"""The remaining transfer paths: Stock Entry, Process Transfer, Main Slip."""

	@classmethod
	def setUpClass(cls):
		return

	def _se_check(self, doc, refinery, refinery_dept=CENTRAL):
		with (
			patch.object(frappe, "get_meta", return_value=_meta_with_flag()),
			patch(f"{MODULE}.refinery_mwos", return_value=refinery),
			patch(f"{MODULE}.get_refinery_department", return_value=refinery_dept),
			patch(f"{MODULE}._row_mwos", return_value=[]),
		):
			refinery_mwo.validate_refinery_stock_entry(doc)

	def test_stock_entry_to_employee_is_refused(self):
		doc = _stock_entry(manufacturing_work_order="MWO-REF", to_employee="EMP-1")
		with self.assertRaisesRegex(frappe.ValidationError, "To Employee"):
			self._se_check(doc, {"MWO-REF": (CENTRAL, "MFR")})

	def test_stock_entry_to_main_slip_or_subcontractor_is_refused(self):
		for field in ("to_main_slip", "subcontractor"):
			doc = _stock_entry(manufacturing_work_order="MWO-REF", **{field: "X"})
			with self.assertRaises(frappe.ValidationError):
				self._se_check(doc, {"MWO-REF": (CENTRAL, "MFR")})

	def test_stock_entry_to_another_department_is_refused(self):
		doc = _stock_entry(
			manufacturing_work_order="MWO-REF", department=CENTRAL, to_department="Tagging - T"
		)
		with self.assertRaisesRegex(frappe.ValidationError, "within the refinery department"):
			self._se_check(doc, {"MWO-REF": (CENTRAL, "MFR")})

	def test_stock_entry_inside_refinery_department_passes(self):
		doc = _stock_entry(
			manufacturing_work_order="MWO-REF", department=CENTRAL, to_department=CENTRAL
		)
		self._se_check(doc, {"MWO-REF": (CENTRAL, "MFR")})

	def test_stock_entry_without_departments_passes(self):
		# The refinery transfer itself and refining entries carry no header department.
		self._se_check(_stock_entry(manufacturing_work_order="MWO-REF"), {"MWO-REF": (CENTRAL, "MFR")})

	def test_regular_stock_entry_is_untouched(self):
		doc = _stock_entry(manufacturing_work_order="MWO-REG", to_employee="EMP-1")
		self._se_check(doc, {})

	def test_stock_entry_check_is_inert_before_migrate(self):
		meta = MagicMock()
		meta.has_field.return_value = False
		doc = _stock_entry(manufacturing_work_order="MWO-REF", to_employee="EMP-1")
		with (
			patch.object(frappe, "get_meta", return_value=meta),
			patch(f"{MODULE}.refinery_mwos") as lookup,
		):
			refinery_mwo.validate_refinery_stock_entry(doc)
		lookup.assert_not_called()

	def _pt_check(self, department, refinery_dept=CENTRAL):
		doc = _ir("Process Transfer", department=department, operations=[_row(1, "MWO-REF")])
		with (
			patch(f"{MODULE}.refinery_mwos", return_value={"MWO-REF": (CENTRAL, "MFR")}),
			patch(f"{MODULE}.get_refinery_department", return_value=refinery_dept),
		):
			refinery_mwo.validate_refinery_process_transfer(doc, "operations")

	def test_process_transfer_inside_refinery_department_passes(self):
		self._pt_check(CENTRAL)

	def test_process_transfer_in_another_department_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Process Transfer"):
			self._pt_check("Casting - T")
		with self.assertRaises(frappe.ValidationError):
			self._pt_check(CENTRAL, refinery_dept=None)

	def test_main_slip_row_of_refinery_work_order_is_refused(self):
		doc = _ir(
			"Main Slip", main_slip_operation=[_row(1, "MWO-REG"), _row(2, "MWO-REF")]
		)
		with (
			patch(f"{MODULE}.refinery_mwos", return_value={"MWO-REF": (CENTRAL, "MFR")}),
			self.assertRaisesRegex(frappe.ValidationError, "Row 2"),
		):
			refinery_mwo.refuse_refinery_rows(doc, "main_slip_operation", "Main Slip")
