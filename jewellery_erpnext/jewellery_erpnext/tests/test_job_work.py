# Copyright (c) 2026, Nirali and contributors
# See license.txt
"""Gurukrupa -> KGGK job work (``jewellery_erpnext.job_work`` and the hooks that call it).

Every test mocks the database calls it depends on, so nothing is written and no fixture
data is needed: each one pins one rule of the flow.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.sub_utils import snc
from jewellery_erpnext.jewellery_erpnext import job_work
from jewellery_erpnext.jewellery_erpnext.doc_events import (
	purchase_invoice,
	purchase_order,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan import (
	manufacturing_plan,
)


class _Doc(SimpleNamespace):
	"""SimpleNamespace with Frappe-style ``get`` / ``set`` / ``append``."""

	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)

	def append(self, table, row):
		row = _Doc(**row)
		getattr(self, table).append(row)
		return row


def _values(table):
	"""``frappe.db.get_value`` stand-in answering ``(doctype, name, fieldname)`` lookups.

	A dict filter is looked up by its sorted items, so a test can answer
	``get_value("Company", {"supplier_code": "S"}, "name")`` as well.
	"""

	def get_value(doctype, filters=None, fieldname="name", *args, **kwargs):
		key = (
			filters if not isinstance(filters, dict) else tuple(sorted(filters.items()))
		)
		if isinstance(fieldname, list | tuple):
			fieldname = tuple(fieldname)
		return table.get((doctype, key, fieldname))

	return get_value


class TestIsJobWorkOrder(IntegrationTestCase):
	def _check(self, table):
		with patch.object(frappe.db, "get_value", side_effect=_values(table)):
			return job_work.is_job_work_order("MWO-1")

	def test_traces_mwo_to_employee_ir_purchase_order(self):
		table = {
			("Manufacturing Work Order", "MWO-1", "manufacturing_order"): "PMO-K",
			("Parent Manufacturing Order", "PMO-K", "sales_order_item"): "SOI-1",
			("Sales Order Item", "SOI-1", "po_no"): "PO-1",
			("Purchase Order", "PO-1", "employee_ir"): "EIR-1",
		}
		self.assertTrue(self._check(table))

	def test_purchase_order_without_employee_ir_is_not_job_work(self):
		table = {
			("Manufacturing Work Order", "MWO-1", "manufacturing_order"): "PMO-K",
			("Parent Manufacturing Order", "PMO-K", "sales_order_item"): "SOI-1",
			("Sales Order Item", "SOI-1", "po_no"): "PO-1",
		}
		self.assertFalse(self._check(table))

	def test_mwo_without_pmo_is_not_job_work(self):
		self.assertFalse(self._check({}))


class TestCompleteJobWorkMwo(IntegrationTestCase):
	def _run(self, doc, statuses, job_work_mwos, cancel=False):
		with (
			patch.object(
				job_work, "is_job_work_order", side_effect=lambda m: m in job_work_mwos
			),
			patch.object(
				frappe.db,
				"get_value",
				side_effect=lambda dt, name, field: statuses.get(name),
			),
			patch.object(frappe.db, "set_value") as set_value,
		):
			job_work.complete_job_work_mwo(doc, cancel=cancel)
		return set_value

	def _issue(self, *mwos, stock_entry_type="Customer Goods Issue"):
		return _Doc(
			stock_entry_type=stock_entry_type,
			items=[_Doc(custom_manufacturing_work_order=m) for m in mwos],
		)

	def test_submit_completes_only_job_work_mwos(self):
		set_value = self._run(self._issue("MWO-J", "MWO-X"), {}, {"MWO-J"})
		set_value.assert_called_once_with(
			"Manufacturing Work Order", "MWO-J", "status", "Completed"
		)

	def test_other_stock_entry_types_never_touch_status(self):
		set_value = self._run(
			self._issue("MWO-J", stock_entry_type="Material Issue"), {}, {"MWO-J"}
		)
		set_value.assert_not_called()

	def test_cancel_reopens_a_completed_mwo(self):
		set_value = self._run(
			self._issue("MWO-J"), {"MWO-J": "Completed"}, {"MWO-J"}, cancel=True
		)
		set_value.assert_called_once_with(
			"Manufacturing Work Order", "MWO-J", "status", "In Process"
		)

	def test_cancel_leaves_a_moved_on_mwo_alone(self):
		set_value = self._run(
			self._issue("MWO-J"), {"MWO-J": "Closed"}, {"MWO-J"}, cancel=True
		)
		set_value.assert_not_called()


class TestReceiveNeedsPurchaseInvoice(IntegrationTestCase):
	def _receive(self, subcontractor="KGGK-SUP"):
		return _Doc(
			type="Receive",
			subcontracting="Yes",
			subcontractor=subcontractor,
			department="Sub Contracting - GEPL",
			employee_ir_operations=[_Doc(manufacturing_work_order="MWO-1")],
		)

	def _run(self, doc, own_company=True, issue="EIR-ISSUE", po="PO-1", invoiced=True):
		def sql(query, *args, **kwargs):
			if "tabEmployee IR" in query:
				return [(issue,)] if issue else []
			return [(1,)] if invoiced else []

		with (
			patch.object(frappe.db, "exists", return_value=own_company),
			patch.object(frappe.db, "sql", side_effect=sql),
			patch.object(frappe.db, "get_value", return_value=po),
		):
			job_work.validate_job_work_purchase_invoice(doc)

	def test_outside_subcontractor_is_not_checked(self):
		self._run(self._receive(), own_company=False, invoiced=False)

	def test_issue_is_never_checked(self):
		doc = self._receive()
		doc.type = "Issue"
		self._run(doc, invoiced=False)

	def test_kggk_receive_without_invoice_is_blocked(self):
		with self.assertRaises(frappe.ValidationError):
			self._run(self._receive(), invoiced=False)

	def test_kggk_receive_with_invoice_passes(self):
		self._run(self._receive(), invoiced=True)


class TestAdditionalCosts(IntegrationTestCase):
	def _manufacture(self, rows):
		return _Doc(
			docstatus=0,
			stock_entry_type="Manufacture",
			manufacturing_order="PMO-G",
			company="Gurukrupa Export Private Limited",
			additional_costs=rows,
		)

	def test_rebuilds_job_work_rows_and_keeps_manual_rows(self):
		doc = self._manufacture(
			[
				_Doc(description="Job Work: stale", amount=1),
				_Doc(description="Freight", amount=7),
			]
		)
		invoice_row = frappe._dict(
			purchase_invoice="PI-1",
			po_detail="POI-1",
			amount=1500.0,
			expense_account="Subcontracting Expense - GEPL",
			cost_center="Main - GEPL",
			manufacturing_operation="MOP-1",
		)
		with (
			patch.object(frappe.db, "sql", return_value=[invoice_row]),
			patch.object(frappe.db, "get_value", return_value="MWO-1"),
			patch.object(job_work, "_kggk_sales_invoice", return_value="SI-K-1"),
		):
			job_work.add_job_work_additional_costs(doc)

		self.assertEqual([r.description for r in doc.additional_costs][0], "Freight")
		added = doc.additional_costs[1]
		self.assertEqual(added.amount, 1500.0)
		self.assertEqual(added.expense_account, "Subcontracting Expense - GEPL")
		self.assertEqual(added.custom_purchase_invoice, "PI-1")
		self.assertEqual(added.custom_sales_invoice, "SI-K-1")
		self.assertEqual(added.custom_parent_manufacturing_order, "PMO-G")
		self.assertEqual(added.custom_manufacturing_work_order, "MWO-1")
		self.assertEqual(len(doc.additional_costs), 2)

	def test_other_entries_are_untouched(self):
		doc = self._manufacture([_Doc(description="Job Work: keep", amount=1)])
		doc.stock_entry_type = "Material Transfer"
		with patch.object(frappe.db, "sql") as sql:
			job_work.add_job_work_additional_costs(doc)
		sql.assert_not_called()
		self.assertEqual(len(doc.additional_costs), 1)


class TestPurchaseInvoiceRate(IntegrationTestCase):
	def _row(self, **fields):
		row = _Doc(
			purchase_order="PO-1",
			po_detail="POI-1",
			qty=1,
			rate=500.0,
			amount=500.0,
			**fields,
		)
		row.precision = lambda field: 2
		return row

	def test_job_work_row_takes_kggk_invoice_amount(self):
		row = self._row()
		with patch.object(frappe.db, "sql", return_value=[(1234.5,)]):
			purchase_invoice.update_rate_from_sales_invoice(_Doc(items=[row]), {"PO-1"})
		self.assertEqual((row.rate, row.amount), (1234.5, 1234.5))

	def test_other_purchase_orders_keep_their_rate(self):
		row = self._row()
		with patch.object(frappe.db, "sql") as sql:
			purchase_invoice.update_rate_from_sales_invoice(
				_Doc(items=[row]), {"PO-OTHER"}
			)
		sql.assert_not_called()
		self.assertEqual(row.rate, 500.0)

	def test_submitted_invoice_is_never_repriced(self):
		doc = _Doc(docstatus=1, is_return=0, items=[self._row()])
		with patch.object(purchase_invoice, "_get_job_work_purchase_orders") as job_pos:
			purchase_invoice.before_validate(doc)
		job_pos.assert_not_called()


class TestJobWorkQuotation(IntegrationTestCase):
	def _po(self, *mops):
		return _Doc(
			name="PO-1",
			supplier="KGGK-SUP",
			items=[
				_Doc(
					idx=i + 1,
					name=f"POI-{i + 1}",
					custom_pmo="PMO-G",
					manufacturing_operation=m,
					rate=10,
				)
				for i, m in enumerate(mops)
			],
		)

	def _table(self):
		pmo_fields = (
			"item_code",
			"qty",
			"diamond_quality",
			"master_bom",
		)
		return {
			(
				"Company",
				(("supplier_code", "KGGK-SUP"),),
				"name",
			): "KG GK Jewellers Private Limited",
			("Manufacturing Operation", "MOP-1", "manufacturing_work_order"): "MWO-1",
			("Manufacturing Operation", "MOP-2", "manufacturing_work_order"): "MWO-1",
			("Manufacturing Work Order", "MWO-1", "qty"): 1,
			("Parent Manufacturing Order", "PMO-G", pmo_fields): frappe._dict(
				item_code="RI0001-001", qty=3, master_bom="BOM-1"
			),
		}

	def test_quantity_comes_from_the_mwo(self):
		target = _Doc(items=[])
		with patch.object(frappe.db, "get_value", side_effect=_values(self._table())):
			purchase_order.append_job_work_quotation_items(self._po("MOP-1"), target)
		self.assertEqual(target.company, "KG GK Jewellers Private Limited")
		self.assertEqual(target.items[0].qty, 1)
		self.assertEqual(target.items[0].copy_bom, "BOM-1")
		self.assertEqual(target.items[0].custom_po_details, "POI-1")

	def test_same_mwo_twice_on_one_po_is_refused(self):
		with (
			patch.object(frappe.db, "get_value", side_effect=_values(self._table())),
			self.assertRaises(frappe.ValidationError),
		):
			purchase_order.append_job_work_quotation_items(
				self._po("MOP-1", "MOP-2"), _Doc(items=[])
			)

	def test_supplier_not_mapped_to_a_company_is_refused(self):
		with (
			patch.object(frappe.db, "get_value", return_value=None),
			self.assertRaises(frappe.ValidationError),
		):
			purchase_order.append_job_work_quotation_items(
				self._po("MOP-1"), _Doc(items=[])
			)


class TestIncomeRate(IntegrationTestCase):
	def _rate(self, price):
		table = {
			("Purchase Order", "PO-1", "supplier"): "KGGK-SUP",
			("Item", "RI0001-001", "item_subcategory"): "Ring",
		}
		with (
			patch.object(frappe.db, "get_value", side_effect=_values(table)),
			patch.object(frappe.db, "sql", return_value=[(price,)] if price else []),
		):
			return manufacturing_plan.get_job_work_rate(
				_Doc(po_no="PO-1", item_code="RI0001-001")
			)

	def test_rate_comes_from_supplier_price_list(self):
		self.assertEqual(self._rate(850), 850)

	def test_missing_price_stops_the_plan(self):
		with self.assertRaises(frappe.ValidationError):
			self._rate(None)


class TestSalesInvoicePerPmo(IntegrationTestCase):
	def _invoice(self, *so_details):
		return _Doc(
			is_return=0,
			items=[
				_Doc(idx=i + 1, so_detail=d, item_code="Subcontracting Income")
				for i, d in enumerate(so_details)
			],
		)

	def _run(self, doc, job_work_rows):
		with patch.object(job_work, "_job_work_so_items", return_value=job_work_rows):
			job_work.set_job_work_sales_invoice_links(doc)

	def test_rows_are_stamped_with_pmo_and_gurukrupa_mwo(self):
		doc = self._invoice("SOI-1")
		self._run(doc, {"SOI-1": frappe._dict(pmo="PMO-K1", job_work_mwo="MWO-G1")})
		self.assertEqual(doc.items[0].custom_parent_manufacturing_order, "PMO-K1")
		self.assertEqual(doc.items[0].custom_job_work_mwo, "MWO-G1")

	def test_two_pmos_on_one_invoice_are_refused(self):
		rows = {
			"SOI-1": frappe._dict(pmo="PMO-K1", job_work_mwo="MWO-G1"),
			"SOI-2": frappe._dict(pmo="PMO-K2", job_work_mwo="MWO-G2"),
		}
		with self.assertRaises(frappe.ValidationError):
			self._run(self._invoice("SOI-1", "SOI-2"), rows)

	def test_other_rows_cannot_ride_on_a_job_work_invoice(self):
		with self.assertRaises(frappe.ValidationError):
			self._run(
				self._invoice("SOI-1", "SOI-OTHER"),
				{"SOI-1": frappe._dict(pmo="PMO-K1", job_work_mwo="MWO-G1")},
			)

	def test_invoice_without_job_work_rows_is_untouched(self):
		doc = self._invoice("SOI-OTHER")
		self._run(doc, {})
		self.assertIsNone(doc.items[0].get("custom_parent_manufacturing_order"))


class TestSncAllMaterials(IntegrationTestCase):
	def _settleable(self, item_code, template, include_all_items):
		with patch.object(snc, "_item_template", return_value=template):
			return snc._is_settleable_item(item_code, include_all_items)

	def test_stones_settle_only_with_all_materials(self):
		self.assertFalse(self._settleable("D-AD-1", "D", False))
		self.assertTrue(self._settleable("D-AD-1", "D", True))
		self.assertTrue(self._settleable("G-AM-1", "G", True))

	def test_gold_rules_are_unchanged(self):
		self.assertTrue(self._settleable("M-G-22KT", "M", False))
		self.assertTrue(self._settleable("F-G-CLASP", "F", False))
		self.assertFalse(self._settleable("M-S-925", "M", True))


class TestJobWorkGst(IntegrationTestCase):
	def test_template_from_india_compliance_is_applied(self):
		po = MagicMock()
		po.as_dict.return_value = {}
		po.doctype, po.company = "Purchase Order", "Gurukrupa Export Private Limited"
		details = frappe._dict(
			taxes_and_charges="Input GST In-state - GEPL",
			place_of_supply="24-Gujarat",
			taxes=[{"account_head": "Input Tax CGST - GEPL", "rate": 9}],
		)
		with patch(
			"india_compliance.gst_india.overrides.transaction.get_gst_details",
			return_value=details,
		):
			job_work.apply_job_work_gst(po)
		self.assertEqual(po.taxes_and_charges, "Input GST In-state - GEPL")
		po.append.assert_called_once_with("taxes", details.taxes[0])
		po.save.assert_called_once()

	def test_no_template_leaves_po_untaxed(self):
		po = MagicMock()
		po.as_dict.return_value = {}
		with patch(
			"india_compliance.gst_india.overrides.transaction.get_gst_details",
			return_value=frappe._dict(),
		):
			job_work.apply_job_work_gst(po)
		po.save.assert_not_called()


class TestJobWorkGstDisabledTemplate(IntegrationTestCase):
	def test_disabled_pick_is_swapped_for_enabled_template_of_same_category(self):
		po = MagicMock()
		po.as_dict.return_value = {}
		po.doctype, po.company = "Purchase Order", "Gurukrupa Export Private Limited"
		details = frappe._dict(
			taxes_and_charges="Input GST Out-state (T1) - GEPL", taxes=[{"rate": 99}]
		)

		def get_value(doctype, filters, field="name", *args, **kwargs):
			if filters == "Input GST Out-state (T1) - GEPL":
				return {"disabled": 1, "tax_category": "Out-State"}[field]
			if isinstance(filters, dict) and filters.get("tax_category") == "Out-State":
				return "Input GST Out-state - GEPL"
			return None

		with (
			patch(
				"india_compliance.gst_india.overrides.transaction.get_gst_details",
				return_value=details,
			),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch(
				"erpnext.controllers.accounts_controller.get_taxes_and_charges",
				return_value=[{"rate": 3}],
			),
			patch.object(frappe.db, "savepoint"),
		):
			job_work.apply_job_work_gst(po)
		self.assertEqual(po.taxes_and_charges, "Input GST Out-state - GEPL")
		po.append.assert_called_once_with("taxes", {"rate": 3})

	def test_failed_save_leaves_po_untaxed_and_does_not_raise(self):
		po = MagicMock()
		po.as_dict.return_value = {}
		po.save.side_effect = frappe.ValidationError("boom")
		details = frappe._dict(taxes_and_charges="Input GST In-state - GEPL", taxes=[])
		with (
			patch(
				"india_compliance.gst_india.overrides.transaction.get_gst_details",
				return_value=details,
			),
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(frappe, "msgprint") as msgprint,
		):
			job_work.apply_job_work_gst(po)
		rollback.assert_called_once_with(save_point="job_work_gst")
		po.reload.assert_called_once()
		msgprint.assert_called_once()
