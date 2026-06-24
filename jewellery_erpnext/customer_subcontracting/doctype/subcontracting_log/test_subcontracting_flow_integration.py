# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""End-to-end (non-mocked) customer-subcontracting flow for a NON-GEPL company.

The existing subcontracting suites (``test_repack`` / ``test_subcontracting_log``) are
fully mocked and even bake ``"Central RM - GEPL"`` into their fixtures, so they never
exercise the real different-company path. This suite drives actual Stock Entries through
the live hook chain using the ``Test_Company`` (abbr ``-T``) masters from
``create_test_data``/``setup_data`` and asserts that:

* customer-gold receipts auto-create a company-agnostic parent batch + Subcontracting Log,
* the repack automation produces a "Subcontracting Repack" entry whose company is derived
  from the source **warehouse** (Test_Company), not ``frappe.defaults.get_user_default``.

It guards commit 9110e14 ("remove hardcoded company name") and the follow-up fix in
``sub_utils.repack.create_gold_repack_entry`` (company resolved from ``source_warehouse``).
Validated against the gk site's real non-GEPL company "KG GK Jewellers Private Limited".
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.create_test_data import create_test_data
from jewellery_erpnext.customer_subcontracting.sub_utils import repack


class TestSubcontractingFlowIntegration(IntegrationTestCase):
	GOLD_24 = "M-G-24KT-99.9-Y"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		create_test_data()
		cls.company = "Test_Company"
		cls.warehouse = "Central RM - T"  # Raw Material warehouse owned by Test_Company
		cls.customer = "Test_Customer_External"
		cls.branch = frappe.db.get_value(
			"Warehouse", cls.warehouse, "custom_branch"
		) or frappe.db.get_value("Branch", {"branch_name": "Test Branch"}, "name")

		# Customer is created by setup_data(); ensure it exists for standalone runs.
		if not frappe.db.exists("Customer", cls.customer):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": cls.customer,
					"customer_group": frappe.db.get_value(
						"Customer Group", {"is_group": 0}, "name"
					),
					"territory": frappe.db.get_value(
						"Territory", {"is_group": 0}, "name"
					),
				}
			).insert(ignore_permissions=True)

		# Guard the premise: the warehouse must belong to a non-GEPL company.
		assert frappe.db.get_value("Warehouse", cls.warehouse, "company") == cls.company

	# ------------------------------------------------------------------ helpers
	def _receive_customer_gold(self, customer, qty):
		"""Submit a "Customer Goods Received" SE for 24KT gold; return (doc, batch_no)."""
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Customer Goods Received"
		se.branch = self.branch
		se.to_warehouse = self.warehouse
		se._customer = customer  # runtime attr the batch_rename / repack hooks read
		se.append(
			"items",
			{
				"item_code": self.GOLD_24,
				"qty": qty,
				"t_warehouse": self.warehouse,
				"inventory_type": "Customer Goods",
				"customer": customer,
				"basic_rate": 5000,
				"use_serial_batch_fields": 1,
			},
		)
		se.insert(ignore_permissions=True)
		se.submit()
		se.reload()
		return se, se.items[0].batch_no

	def _pending_company_gold_settlement(self, customer, usage_batch, pure_qty=5):
		"""Insert the pending settlement a "Company Gold" Material Transfer would create.

		The classification that produces this state is covered by the
		``classify_gold_usage`` unit tests; here we only need the resulting log so the
		repack automation has something to settle.
		"""
		return frappe.get_doc(
			{
				"doctype": "Subcontracting Log",
				"customer": customer,
				"batch": usage_batch,
				"item": self.GOLD_24,
				"batch_item": self.GOLD_24,
				"usage_batch": usage_batch,
				"quantity": pure_qty,
				"pure_qty": pure_qty,
				"ownership": "Company Gold",
				"transaction_type": "Material Tranfer (DEPARTMENT)",
				"mwo_type": "Subcontracting",
				"usage_type": "Company Gold",
				"settlement_required": 1,
				"settlement_status": "Pending",
				"settlement_type": "Company Needs Gold",
				"settlement_customer": customer,
				"settlement_priority": 1,
				"pending_pure_qty": pure_qty,
				"settled_pure_qty": 0,
				"balance_pure_qty": pure_qty,
			}
		).insert(ignore_permissions=True)

	# ------------------------------------------------------------------- tests
	def test_customer_goods_received_creates_parent_batch_and_log(self):
		se, batch_no = self._receive_customer_gold(self.customer, 10)

		self.assertTrue(batch_no, "parent batch was not auto-created")
		# Batch naming is company-agnostic (customer-prefixed), not company-abbr based.
		self.assertTrue(
			batch_no.startswith(self.customer),
			f"batch {batch_no!r} is not named after the customer",
		)
		batch = frappe.get_doc("Batch", batch_no)
		self.assertEqual(batch.custom_customer, self.customer)
		self.assertEqual(batch.custom_inventory_type, "Customer Goods")

		self.assertTrue(
			frappe.db.exists(
				"Subcontracting Log",
				{
					"reference_docname": se.name,
					"transaction_type": "Customer Goods Received",
					"customer": self.customer,
					"batch": batch_no,
					"item": self.GOLD_24,
				},
			),
			"Customer Goods Received subcontracting log was not created",
		)

	def test_repack_automation_sets_company_from_warehouse(self):
		# Seed a usage batch + a pending "Company Needs Gold" settlement for the customer.
		_, usage_batch = self._receive_customer_gold(self.customer, 6)
		pending = self._pending_company_gold_settlement(self.customer, usage_batch)

		# Receiving matching customer gold triggers create_gold_repack on on_submit.
		self._receive_customer_gold(self.customer, 8)

		pending.reload()
		repack_se = pending.settled_by_repack
		self.assertTrue(
			repack_se, "repack automation did not create a Subcontracting Repack entry"
		)
		self.assertEqual(
			frappe.db.get_value("Stock Entry", repack_se, "stock_entry_type"),
			"Subcontracting Repack",
		)
		# The decisive assertion: company comes from the warehouse, not the user default.
		self.assertEqual(
			frappe.db.get_value("Stock Entry", repack_se, "company"),
			frappe.db.get_value("Warehouse", self.warehouse, "company"),
		)
		self.assertIn(pending.settlement_status, ("Partially Settled", "Settled"))

	def test_create_gold_repack_entry_uses_warehouse_company_not_user_default(self):
		_, source_batch = self._receive_customer_gold(self.customer, 5)
		_, target_batch = self._receive_customer_gold(self.customer, 1)

		# Force a WRONG user-default company; the fix must ignore it and use the warehouse.
		with patch.object(
			repack.frappe.defaults,
			"get_user_default",
			return_value="Gurukrupa Export Private Limited",
		):
			se_name = repack.create_gold_repack_entry(
				source_batch=source_batch,
				target_batch=target_batch,
				qty=2,
				source_customer=self.customer,
				reference_log=None,
				source_warehouse=self.warehouse,
			)

		self.assertEqual(
			frappe.db.get_value("Stock Entry", se_name, "company"),
			self.company,
			"repack SE company must be derived from the source warehouse, not user default",
		)
