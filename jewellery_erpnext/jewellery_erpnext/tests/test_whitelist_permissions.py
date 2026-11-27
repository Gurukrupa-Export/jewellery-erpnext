# Copyright (c) 2026, Nirali and contributors
# See license.txt
#
# Permission hardening of whitelisted endpoints (Lens audit follow-up). DB-free where possible:
# permission calls are mocked so each test pins "refuses before doing anything" without needing
# users and roles. One test per behaviour, grouped by tier.

import importlib
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext import utils as app_utils
from jewellery_erpnext.customer_subcontracting.sub_utils import snc
from jewellery_erpnext.jewellery_erpnext.doc_events import quotation, warehouse_tracking
from jewellery_erpnext.jewellery_erpnext.doctype.child_stock_reconcilation.child_stock_reconcilation import (
	ChildStockReconcilation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.combine_job_card import (
	combine_job_card,
)
from jewellery_erpnext.jewellery_erpnext.doctype.jewellery_settings.jewellery_settings import (
	JewellerySettings,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order import (
	parent_manufacturing_order as pmo_mod,
)
from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
	RefiningEntry,
)

# Endpoints with no browser or API caller: the @frappe.whitelist decorator was removed.
UNWHITELISTED = [
	(
		"jewellery_erpnext.customer_subcontracting.sub_utils.repack",
		"create_pending_repack",
	),
	(
		"jewellery_erpnext.gurukrupa_exports.doctype.serial_no_and_design_code_order.serial_no_and_design_code_order",
		"make_quotation",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.job_card",
		"create_stock_entry_material_receipt",
	),
	("jewellery_erpnext.jewellery_erpnext.doc_events.quotation", "xl_preview"),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.sales_invoice",
		"xl_preview_sales_invoice",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.sales_order",
		"xl_preview_sales_order",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry",
		"set_filter_for_main_slip",
	),
	("jewellery_erpnext.jewellery_erpnext.doc_events.work_order", "get_work_orders"),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir",
		"DepartmentIR.get_summary_data",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir",
		"DepartmentIR.get_operations",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion",
		"get_filtered_batches",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip",
		"create_stock_entries",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip",
		"get_any_item_from_attribute",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation",
		"ManufacturingOperation.create_fg",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation",
		"ManufacturingOperation.get_stock_entry",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation",
		"ManufacturingOperation.get_stock_summary",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation",
		"get_mop_log_balance",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order",
		"ManufacturingWorkOrder.create_repair_un_pack_stock_entry",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions",
		"MetalConversions.set_attribute_value",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.tracking_bom.tracking_bom",
		"update_tracking_bom_detail",
	),
	("jewellery_erpnext.query", "set_metal_purity"),
	("jewellery_erpnext.query", "get_production_item"),
	("jewellery_erpnext.query", "set_warehouses"),
	("jewellery_erpnext.query", "get_wo_operations"),
	("jewellery_erpnext.query", "get_parcel_place"),
	("jewellery_erpnext.query", "set_wo_items_grade"),
	("jewellery_erpnext.query", "get_item_code"),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.material_request",
		"create_stock_entry",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.customer_approval.customer_approval",
		"serial_no_filter",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir",
		"calculation_time_log",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir",
		"EmployeeIR.create_subcontracting_order",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir",
		"EmployeeIR.validate_process_loss",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.fg_bom_field_configuration.fg_bom_field_configuration",
		"get_active_fields_for_subcategory",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan",
		"create_cad_mwo",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan",
		"ManufacturingPlan.get_items_for_production",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions",
		"MetalConversions.get_alloy_bailance",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.operation_card_transfer.operation_card_transfer",
		"get_warehouse_from_operation_card",
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order",
		"make_manufacturing_order",
	),
	(
		"jewellery_erpnext.refining.doctype.refining_entry.refining_entry",
		"RefiningEntry.preview_external_refining_po",
	),
	("jewellery_erpnext.utils", "get_item_from_attribute_full"),
]

# Module-level endpoints whose first statement is a frappe.has_permission(..., throw=True) check,
# with arguments that reach it.
CHECKED_FUNCTIONS = [
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.job_card",
		"create_new_job_card",
		{
			"company": "C",
			"posting_date": "2026-01-01",
			"for_quantity": 1,
			"work_order": "WO-1",
			"bom_no": "B",
			"operation": "O",
			"workstation": "W",
			"employee": "E",
		},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.job_card",
		"create_stock_entry",
		{
			"company": "C",
			"work_order": "WO-1",
			"from_warehouse": "A",
			"to_warehouse": "B",
			"from_job_card": "JC-1",
			"item_code": "I",
			"gross_wt": 1,
			"purity": 1,
			"net_wt": 1,
		},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.job_card",
		"create_internal_transfer",
		{},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.job_card",
		"stock_entry_detail",
		{"item": "JC-1"},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.quotation",
		"update_status",
		{"quotation_id": "QTN-1"},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator",
		"get_operation_details",
		{"mwo": "MWO-1", "pmo": "PMO-1", "docname": "MOP-1"},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.qc.qc",
		"receive_gross_wt_from_qc",
		{
			"doc_name": "QC-1",
			"mwo": "MWO-1",
			"mnf_opt": "MOP-1",
			"eir": "EIR-1",
			"g_wt": 1,
			"r_gwt": 1,
		},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan",
		"get_details_to_append",
		{"source_names": "[]"},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order",
		"make_quotation",
		{"source_name": "PO-1"},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation",
		"create_scrap_wo_stock_entry",
		{"se_data": json.dumps({"manufacturing_operation": "MOP-1"})},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry",
		"validation_of_serial_item",
		{"issue_doc": "SE-1"},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.customer_approval.customer_approval",
		"get_stock_entry_data",
		{"stock_entry_reference": "SE-1"},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.customer_approval.customer_approval",
		"quantity_calculation",
		{"stock_entry_reference": "SE-1"},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order",
		"get_linked_stock_entries",
		{"pmo_name": "PMO-1"},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order",
		"get_stock_summary",
		{"pmo_name": "PMO-1"},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.sales_order_utils",
		"download_cost_sheet",
		{"sales_order": "SO-1"},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification",
		"create_product_certification_receive",
		{"source_name": "PC-1"},
	),
	(
		"jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_tracking",
		"refresh_msl_tracking",
		{"warehouse": "WH-1"},
	),
]

REFINING_ACTIONS = [
	("receive_from_supplier", {"recovery_weight": 1}),
	("fetch_dust_materials", {}),
	("receive_materials", {}),
	("generate_recovery_table", {}),
	("start_refining", {}),
	("distribute_recovered_gold", {}),
	("verify_recovery", {}),
	("complete_refining", {}),
	("transfer_recovered_materials", {}),
	("create_central_transfer", {}),
]


def _denied(*args, **kwargs):
	raise frappe.PermissionError


class _Row:
	"""A child row stub (MagicMock reserves ``parent`` for itself)."""

	def __init__(self, parent, parenttype):
		self.parent = parent
		self.parenttype = parenttype
		self.flags = frappe._dict()
		self.update = MagicMock()
		self.save = MagicMock()


def _denying_doc(**attrs):
	doc = MagicMock(**attrs)
	doc.check_permission.side_effect = frappe.PermissionError
	return doc


class _DBFree(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@classmethod
	def tearDownClass(cls):
		pass


class TestUnwhitelisted(_DBFree):
	def test_removed_endpoints_are_not_whitelisted(self):
		for module, name in UNWHITELISTED:
			obj = importlib.import_module(module)
			for part in name.split("."):
				obj = getattr(obj, part)
			with self.subTest(endpoint=f"{module}.{name}"):
				self.assertNotIn(obj, frappe.whitelisted)

	def test_warehouse_button_uses_checked_wrapper(self):
		self.assertIn(warehouse_tracking.refresh_msl_tracking, frappe.whitelisted)
		self.assertNotIn(
			warehouse_tracking.recalculate_msl_tracking, frappe.whitelisted
		)


class TestCheckedFunctionsRefuse(_DBFree):
	def test_each_endpoint_refuses_without_permission(self):
		for module, name, kwargs in CHECKED_FUNCTIONS:
			fn = getattr(importlib.import_module(module), name)
			with self.subTest(endpoint=name):
				with patch.object(
					frappe, "has_permission", side_effect=_denied
				) as has_perm:
					with self.assertRaises(frappe.PermissionError):
						fn(**kwargs)
				self.assertTrue(has_perm.called)
				self.assertTrue(has_perm.call_args.kwargs.get("throw"))

	def test_make_sales_order_batch_checks_each_order(self):
		from jewellery_erpnext.jewellery_erpnext.doc_events import sales_order

		with (
			patch.object(frappe, "new_doc", return_value=MagicMock()),
			patch.object(frappe, "has_permission", side_effect=_denied) as has_perm,
		):
			with self.assertRaises(frappe.PermissionError):
				sales_order.make_sales_order_batch(json.dumps(["SO-1"]))
		self.assertEqual(has_perm.call_args.args, ("Sales Order", "read", "SO-1"))


class TestDocumentChecks(_DBFree):
	def test_refining_actions_need_write(self):
		for method, kwargs in REFINING_ACTIONS:
			doc = _denying_doc()
			with self.subTest(method=method):
				with self.assertRaises(frappe.PermissionError):
					getattr(RefiningEntry, method)(doc, **kwargs)
				doc.check_permission.assert_called_once_with("write")
				doc.db_set.assert_not_called()

	def test_jewellery_settings_variant_update_needs_write(self):
		doc = _denying_doc()
		with self.assertRaises(frappe.PermissionError):
			JewellerySettings.update_uom_conversion_in_variant(doc)
		doc.check_permission.assert_called_once_with("write")

	def test_create_snc_needs_read_on_the_work_order(self):
		with patch.object(snc, "_get_mwo", return_value=_denying_doc(name="MWO-1")):
			with self.assertRaises(frappe.PermissionError):
				snc.create_snc("MWO-1")

	def test_generate_bom_needs_read_and_does_not_enqueue(self):
		with (
			patch.object(frappe, "get_doc", return_value=_denying_doc()),
			patch.object(frappe, "enqueue") as enqueue,
		):
			with self.assertRaises(frappe.PermissionError):
				quotation.generate_bom("QTN-1")
		enqueue.assert_not_called()

	def test_hold_comment_needs_read_on_the_document(self):
		doc = _denying_doc()
		with patch.object(frappe, "get_doc", return_value=doc):
			with self.assertRaises(frappe.PermissionError):
				pmo_mod.add_hold_comment("Sales Invoice", "SINV-1", "reason")
		doc.add_comment.assert_not_called()

	def test_customer_approval_items_need_read(self):
		with patch.object(frappe, "get_doc", return_value=_denying_doc(docstatus=1)):
			with self.assertRaises(frappe.PermissionError):
				app_utils.get_sales_order_items("CA-1")

	def test_child_reconciliation_needs_read_on_source(self):
		doc = MagicMock(
			previous_child_stock_reconciliation=1, stock_reconcillation="SR-1"
		)
		with patch.object(frappe, "get_doc", return_value=_denying_doc()):
			with self.assertRaises(frappe.PermissionError):
				ChildStockReconcilation.fetch_stock_reconciliation_item(doc)
		doc.append.assert_not_called()


class TestAllowlists(_DBFree):
	def test_db_get_value_allows_only_department_ir_departments(self):
		with patch.object(
			frappe.db, "get_value", return_value={"current_department": "D"}
		) as gv:
			result = app_utils.db_get_value(
				"Department IR",
				"DIR-1",
				json.dumps(["current_department", "next_department"]),
			)
		self.assertEqual(result, {"current_department": "D"})
		gv.assert_called_once()

	def test_db_get_value_refuses_anything_else(self):
		for doctype, fields in (
			("Salary Slip", ["net_pay"]),
			("Department IR", ["owner"]),
			("Department IR", ["current_department", "modified_by"]),
			("Department IR", []),
		):
			with self.subTest(doctype=doctype, fields=fields):
				with patch.object(
					frappe.db, "get_value", wraps=frappe.db.get_value
				) as gv:
					with self.assertRaises(frappe.PermissionError):
						app_utils.db_get_value(doctype, "X", json.dumps(fields))
				# frappe.throw may read settings; the requested record must never be read.
				self.assertNotIn(
					doctype, [c.args[0] for c in gv.call_args_list if c.args]
				)

	def test_type_of_party_allows_only_sales_and_purchase_types(self):
		with patch.object(frappe.db, "get_value", return_value="Outright"):
			self.assertEqual(
				app_utils.get_type_of_party(
					"Sales Type Multiselect", "SO-1", "sales_type"
				),
				"Outright",
			)
			self.assertEqual(
				app_utils.get_type_of_party(
					"Purchase Type Multiselect", "PO-1", "purchase_type"
				),
				"Outright",
			)
		with patch.object(frappe.db, "get_value", wraps=frappe.db.get_value) as gv:
			with self.assertRaises(frappe.PermissionError):
				app_utils.get_type_of_party("Sales Invoice Item", "SINV-1", "rate")
		self.assertNotIn(
			"Sales Invoice Item", [c.args[0] for c in gv.call_args_list if c.args]
		)


class TestRowOwnership(_DBFree):
	def test_bom_row_of_another_parent_is_refused(self):
		parent = MagicMock(doctype="BOM")
		parent.name = "BOM-1"
		foreign = _Row("BOM-2", "BOM")
		with patch.object(frappe, "get_doc", return_value=foreign):
			with self.assertRaises(frappe.PermissionError):
				quotation.update_table(
					parent,
					"BOM Metal Detail",
					"metal_detail",
					{"docname": "row-x", "rate": 1},
				)
		foreign.save.assert_not_called()

	def test_own_bom_row_is_updated_without_reparenting(self):
		parent = MagicMock(doctype="BOM")
		parent.name = "BOM-1"
		own = _Row("BOM-1", "BOM")
		payload = {
			"docname": "row-1",
			"rate": 5,
			"parent": "BOM-2",
			"parenttype": "Item",
			"parentfield": "x",
		}
		with patch.object(frappe, "get_doc", return_value=own):
			quotation.update_table(parent, "BOM Metal Detail", "metal_detail", payload)
		own.update.assert_called_once_with({"rate": 5})
		own.save.assert_called_once()

	def test_combine_job_card_row_needs_write_on_its_parent(self):
		with (
			patch.object(frappe.db, "get_value", return_value="CJC-1"),
			patch.object(frappe, "has_permission", side_effect=_denied) as has_perm,
		):
			with self.assertRaises(frappe.PermissionError):
				combine_job_card._check_combine_job_card_write("row-1")
		self.assertEqual(
			has_perm.call_args.args, ("Combine Job Card", "write", "CJC-1")
		)

	def test_combine_job_card_unknown_row_is_refused(self):
		with patch.object(frappe.db, "get_value", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				combine_job_card._check_combine_job_card_write("row-x")

	def test_combine_create_stock_entry_checks_before_creating(self):
		with (
			patch.object(frappe, "get_doc", return_value=MagicMock(items=[1])),
			patch.object(
				combine_job_card, "_check_combine_job_card_write", side_effect=_denied
			),
			patch.object(combine_job_card, "make_stock_entry") as make_se,
		):
			with self.assertRaises(frappe.PermissionError):
				combine_job_card.create_stock_entry(json.dumps([["JC-1", "row-1"]]))
		make_se.assert_not_called()


class TestStockSummary(_DBFree):
	def test_pmo_without_work_orders_shows_nothing(self):
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe, "get_all", return_value=[]),
			patch.object(frappe, "render_template", return_value="html") as render,
		):
			pmo_mod.get_stock_summary("PMO-1")
		self.assertEqual(render.call_args.args[1], {"data": [], "total_qty": 0})
