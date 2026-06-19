# Copyright (c) 2026, Nirali and contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.sub_utils import repack


def _item(**fields):
	defaults = {
		"batch_no": "BATCH-IN",
		"item_code": "M-G-24KT",
		"qty": 10,
		"inventory_type": "Customer Goods",
		"t_warehouse": "Central RM - GEPL",
		"s_warehouse": None,
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


def _stock_entry(**fields):
	defaults = {
		"doctype": "Stock Entry",
		"docstatus": 1,
		"stock_entry_type": "Customer Goods Received",
		"_customer": "Customer A",
		"items": [_item()],
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


def _log(**fields):
	defaults = {
		"name": "SCL-001",
		"batch_item": "M-G-22KT",
		"balance_pure_qty": 5,
		"pending_pure_qty": 5,
		"settled_pure_qty": 0,
		"quantity": 5,
		"usage_batch": "USED-BATCH",
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


class TestRepackAutomation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_customer_gold_received_starts_repack_for_matching_pending_logs(self):
		pending_logs = [_log()]
		doc = _stock_entry(
			items=[
				_item(batch_no=None),
				_item(item_code="D-STONE"),
				_item(inventory_type="Company Goods"),
				_item(batch_no="BATCH-IN", item_code="M-G-24KT", qty=7),
			]
		)

		with patch.object(
			repack.frappe, "get_all", return_value=pending_logs
		) as get_all, patch.object(repack, "process_repack_settlement") as process:
			repack.create_customer_gold_repack_automation(doc)

		get_all.assert_called_once_with(
			"Subcontracting Log",
			filters={
				"settlement_required": 1,
				"settlement_status": ["in", ["Pending", "Partially Settled"]],
				"mwo_type": "Subcontracting",
				"usage_type": ["in", ["Different Customer Gold", "Company Gold"]],
				"customer": "Customer A",
			},
			fields=["*"],
			order_by="creation asc",
		)
		process.assert_called_once_with(
			incoming_batches=[
				{
					"batch_no": "BATCH-IN",
					"qty": 7.0,
					"remaining_qty": 7.0,
					"inventory_type": "Customer Goods",
					"item_code": "M-G-24KT",
					"warehouse": "Central RM - GEPL",
				}
			],
			pending_logs=pending_logs,
			incoming_customer="Customer A",
		)

	def test_customer_gold_repack_ignores_non_submitted_or_wrong_warehouse_entries(
		self,
	):
		with patch.object(repack.frappe, "get_all") as get_all, patch.object(
			repack, "process_repack_settlement"
		) as process:
			repack.create_customer_gold_repack_automation(_stock_entry(docstatus=0))
			repack.create_customer_gold_repack_automation(
				_stock_entry(items=[_item(t_warehouse="Other Warehouse - GEPL")])
			)

		get_all.assert_not_called()
		process.assert_not_called()

	def test_company_gold_transfer_starts_repack_for_regular_customer_gold_logs(self):
		pending_logs = [_log(name="SCL-REG")]
		doc = _stock_entry(
			stock_entry_type="Material Transfer (DEPARTMENT)",
			_customer=None,
			items=[
				_item(inventory_type="Customer Goods"),
				_item(
					batch_no="COMP-BATCH",
					item_code="M-G-24KT",
					inventory_type="Company Goods",
					qty=4,
				),
			],
		)

		with patch.object(
			repack.frappe, "get_all", return_value=pending_logs
		) as get_all, patch.object(repack, "process_repack_settlement") as process:
			repack.create_company_gold_repack_automation(doc)

		get_all.assert_called_once_with(
			"Subcontracting Log",
			filters={
				"settlement_required": 1,
				"settlement_status": ["in", ["Pending", "Partially Settled"]],
				"mwo_type": "Regular",
				"usage_type": ["in", ["Different Customer Gold", "Same Customer Gold"]],
			},
			fields=["*"],
			order_by="creation asc",
		)
		process.assert_called_once_with(
			incoming_batches=[
				{
					"batch_no": "COMP-BATCH",
					"qty": 4.0,
					"remaining_qty": 4.0,
					"inventory_type": "Company Goods",
					"item_code": "M-G-24KT",
					"warehouse": "Central RM - GEPL",
				}
			],
			pending_logs=pending_logs,
			incoming_customer=None,
		)

	def test_process_repack_settlement_consumes_multiple_sources_until_log_is_settled(
		self,
	):
		incoming_batches = [
			{
				"batch_no": "SRC-22KT",
				"remaining_qty": 2,
				"item_code": "M-G-22KT",
				"warehouse": "WH-A",
			},
			{
				"batch_no": "SRC-24KT",
				"remaining_qty": 5,
				"item_code": "M-G-24KT",
				"warehouse": "WH-B",
			},
		]

		def purity(item_code):
			return {"M-G-22KT": 91.6, "M-G-24KT": 99.9}[item_code]

		with (
			patch.object(
				repack, "get_target_repack_batch", return_value="TARGET-24KT"
			) as target_batch,
			patch.object(repack, "get_purity", side_effect=purity),
			patch.object(
				repack, "create_gold_repack_entry", side_effect=["SE-1", "SE-2"]
			) as create_entry,
			patch.object(repack, "update_settlement_log") as update_log,
		):
			repack.process_repack_settlement(
				incoming_batches=incoming_batches,
				pending_logs=[_log(balance_pure_qty=5)],
				incoming_customer="Customer A",
			)

		target_batch.assert_called_once_with("USED-BATCH")
		create_entry.assert_has_calls(
			[
				call(
					source_batch="SRC-22KT",
					target_batch="TARGET-24KT",
					qty=1.832,
					source_customer="Customer A",
					reference_log="SCL-001",
					source_warehouse="WH-A",
				),
				call(
					source_batch="SRC-24KT",
					target_batch="TARGET-24KT",
					qty=3.168,
					source_customer="Customer A",
					reference_log="SCL-001",
					source_warehouse="WH-B",
				),
			]
		)
		update_log.assert_has_calls(
			[
				call(
					log_name="SCL-001",
					settled_pure_qty=1.832,
					repack_entry="SE-1",
					settlement_batch="SRC-22KT",
				),
				call(
					log_name="SCL-001",
					settled_pure_qty=3.168,
					repack_entry="SE-2",
					settlement_batch="SRC-24KT",
				),
			]
		)
		self.assertAlmostEqual(incoming_batches[0]["remaining_qty"], 0.168)
		self.assertAlmostEqual(incoming_batches[1]["remaining_qty"], 1.832)

	def test_process_repack_settlement_skips_non_gold_and_missing_purity_logs(self):
		with patch.object(
			repack, "get_target_repack_batch", return_value="TARGET-24KT"
		), patch.object(repack, "get_purity", return_value=None), patch.object(
			repack, "create_gold_repack_entry"
		) as create_entry, patch.object(repack.frappe, "log_error") as log_error:
			repack.process_repack_settlement(
				incoming_batches=[
					{
						"batch_no": "SRC-24KT",
						"remaining_qty": 5,
						"item_code": "M-G-24KT",
						"warehouse": "WH-A",
					}
				],
				pending_logs=[_log(batch_item="D-STONE"), _log(name="SCL-NO-PURITY")],
			)

		create_entry.assert_not_called()
		log_error.assert_called_once_with(
			"Purity Not Found : M-G-22KT", "Repack Automation"
		)

	def test_update_settlement_log_marks_gold_log_partially_settled(self):
		log = _log(pending_pure_qty=10, settled_pure_qty=2)
		log.save = MagicMock()

		with patch.object(repack.frappe, "get_doc", return_value=log):
			repack.update_settlement_log("SCL-001", 3.125, "SE-1", "SRC-24KT")

		self.assertEqual(log.settlement_batch, "SRC-24KT")
		self.assertEqual(log.settled_by_repack, "SE-1")
		self.assertAlmostEqual(log.settled_pure_qty, 5.125)
		self.assertAlmostEqual(log.balance_pure_qty, 4.875)
		self.assertEqual(log.settlement_status, "Partially Settled")
		log.save.assert_called_once_with(ignore_permissions=True)

	def test_update_settlement_log_marks_gold_log_settled(self):
		log = _log(pending_pure_qty=5, settled_pure_qty=3.5)
		log.save = MagicMock()

		with patch.object(repack.frappe, "get_doc", return_value=log):
			repack.update_settlement_log("SCL-001", 1.5, "SE-1", "SRC-24KT")

		self.assertAlmostEqual(log.balance_pure_qty, 0)
		self.assertEqual(log.settlement_status, "Settled")
		log.save.assert_called_once_with(ignore_permissions=True)

	def test_get_target_repack_batch_prefers_linked_24kt_batch_for_gold_usage_batch(
		self,
	):
		def get_value(doctype, name, fieldname):
			self.assertEqual((doctype, fieldname), ("Batch", "item"))
			return {
				"USED-BATCH": "M-G-22KT",
				"LINKED-22KT": "M-G-22KT",
				"LINKED-24KT": "M-G-24KT",
			}[name]

		with patch.object(
			repack.frappe.db, "get_value", side_effect=get_value
		), patch.object(
			repack, "get_linked_batches", return_value=["LINKED-22KT", "LINKED-24KT"]
		):
			self.assertEqual(
				repack.get_target_repack_batch("USED-BATCH"), "LINKED-24KT"
			)

	def test_get_target_repack_batch_returns_original_for_non_gold_usage_batch(self):
		with patch.object(
			repack.frappe.db, "get_value", return_value="D-STONE"
		), patch.object(repack, "get_linked_batches") as linked_batches:
			self.assertEqual(
				repack.get_target_repack_batch("DIAMOND-BATCH"), "DIAMOND-BATCH"
			)

		linked_batches.assert_not_called()
