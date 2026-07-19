# Copyright (c) 2026, Nirali and contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.sub_utils import repack
from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events import (
	utils as batch_utils,
)


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
					qty=2.0,
					source_customer="Customer A",
					reference_log="SCL-001",
					source_warehouse="WH-A",
				),
				call(
					source_batch="SRC-24KT",
					target_batch="TARGET-24KT",
					qty=3.171,
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
		self.assertAlmostEqual(incoming_batches[0]["remaining_qty"], 0.0)
		self.assertAlmostEqual(incoming_batches[1]["remaining_qty"], 1.829, places=3)

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

	# def test_mwo_repack_plan_uses_24kt_only_when_all_logs_have_enough_stock(self):
	# 	logs = [
	# 		_log(
	# 			name="SCL-18",
	# 			batch_item="M-G-22KT",
	# 			usage_batch="USED-18",
	# 			balance_pure_qty=2,
	# 		),
	# 		_log(
	# 			name="SCL-22",
	# 			batch_item="M-G-22KT",
	# 			usage_batch="USED-22",
	# 			balance_pure_qty=3,
	# 		),
	# 	]
	# 	sources = [
	# 		{
	# 			"batch_no": "CUSTOMER-24",
	# 			"warehouse": "WH-1",
	# 			"qty": 5,
	# 			"item_code": "M-G-24KT",
	# 			"purity": 99.9,
	# 		}
	# 	]

	# 	with patch.object(
	# 		repack,
	# 		"get_target_repack_batch",
	# 		side_effect={"USED-18": "TARGET-24-A", "USED-22": "TARGET-24-B"}.get,
	# 	), patch.object(repack.frappe.db, "get_value", return_value="M-G-24KT"):
	# 		plan = repack.build_mwo_repack_plan(logs, sources)

	# 	self.assertEqual(len(plan), 2)

	# 	self.assertEqual(plan[0]["source_batch"], "CUSTOMER-24")
	# 	self.assertEqual(plan[0]["target_batch"], "TARGET-24-A")
	# 	self.assertEqual(plan[0]["qty"], 2.0)
	# 	self.assertEqual(plan[0]["settled_pure_qty"], 2.0)

	# 	self.assertEqual(plan[1]["source_batch"], "CUSTOMER-24")
	# 	self.assertEqual(plan[1]["target_batch"], "TARGET-24-B")
	# 	self.assertEqual(plan[1]["qty"], 3.0)
	# 	self.assertEqual(plan[1]["settled_pure_qty"], 3.0)

	# def test_mwo_repack_plan_falls_back_for_every_log_when_one_lacks_24kt(self):
	# 	logs = [
	# 		_log(
	# 			name="SCL-18",
	# 			batch_item="M-G-18KT",
	# 			usage_batch="USED-18",
	# 			quantity=4,
	# 			pending_pure_qty=3,
	# 			balance_pure_qty=3,
	# 		),
	# 		_log(
	# 			name="SCL-22",
	# 			batch_item="M-G-22KT",
	# 			usage_batch="USED-22",
	# 			quantity=5,
	# 			pending_pure_qty=4,
	# 			balance_pure_qty=4,
	# 		),
	# 	]
	# 	sources = [
	# 		{
	# 			"batch_no": "CUSTOMER-24",
	# 			"warehouse": "WH-24",
	# 			"qty": 6,
	# 			"item_code": "M-G-24KT",
	# 			"purity": 99.9,
	# 		},
	# 		{
	# 			"batch_no": "CUSTOMER-18",
	# 			"warehouse": "WH-18",
	# 			"qty": 4,
	# 			"item_code": "M-G-18KT",
	# 			"purity": 75,
	# 		},
	# 		{
	# 			"batch_no": "CUSTOMER-22",
	# 			"warehouse": "WH-22",
	# 			"qty": 5,
	# 			"item_code": "M-G-22KT",
	# 			"purity": 91.6,
	# 		},
	# 	]

	# 	with patch.object(
	# 		repack,
	# 		"get_target_repack_batch",
	# 		side_effect={"USED-18": "TARGET-18", "USED-22": "TARGET-22"}.get,
	# 	), patch.object(
	# 		repack.frappe.db,
	# 		"get_value",
	# 		side_effect=lambda doctype, name, fieldname: {
	# 			"TARGET-18": "M-G-18KT",
	# 			"TARGET-22": "M-G-22KT",
	# 		}.get(name, "M-G-24KT"),
	# 	):
	# 		plan = repack.build_mwo_repack_plan(logs, sources)

	# 	self.assertEqual(len(plan), 2)
	# 	self.assertEqual(
	# 		plan,
	# 		[
	# 			{
	# 				"log_name": "SCL-18",
	# 				"source_batch": "CUSTOMER-18",
	# 				"source_warehouse": "WH-18",
	# 				"target_batch": "USED-18",
	# 				"qty": 4.0,
	# 				"settled_pure_qty": 3.0,
	# 			},
	# 			{
	# 				"log_name": "SCL-22",
	# 				"source_batch": "CUSTOMER-22",
	# 				"source_warehouse": "WH-22",
	# 				"target_batch": "USED-22",
	# 				"qty": 5.0,
	# 				"settled_pure_qty": 4.0,
	# 			},
	# 		],
	# 	)

	def test_same_item_repack_uses_remaining_work_order_qty_for_partial_log(self):
		log = _log(
			batch_item="M-G-22KT",
			quantity=10,
			pending_pure_qty=8,
			balance_pure_qty=2,
		)

		self.assertEqual(repack.get_remaining_log_qty(log), 2.5)

	def test_customer_gold_repack_allows_mtwo_item_without_item_master_flag(self):
		batch = SimpleNamespace(
			reference_doctype="Stock Entry",
			reference_name="MAT-STE-REPACK",
			custom_customer="Customer A",
			item="M-G-18KT-75.4-Y",
		)

		with patch.object(
			batch_utils.frappe.db,
			"get_value",
			return_value="Subcontracting Repack",
		):
			self.assertTrue(batch_utils.is_subcontracting_gold_repack(batch))

	def test_customer_gold_exception_does_not_apply_to_normal_stock_entries(self):
		batch = SimpleNamespace(
			reference_doctype="Stock Entry",
			reference_name="MAT-STE-TRANSFER",
			custom_customer="Customer A",
			item="M-G-18KT-75.4-Y",
		)

		with patch.object(
			batch_utils.frappe.db,
			"get_value",
			return_value="Customer Goods Transfer",
		):
			self.assertFalse(batch_utils.is_subcontracting_gold_repack(batch))

	def test_repair_unpack_allows_customer_goods_at_mint_via_voucher_type(self):
		# The unpack mints each component's Batch BEFORE the Stock Entry exists, so at the
		# only moment the guard fires reference_doctype is still None and neither
		# reference-based exemption can fire. The "Customer Repair" voucher type stamped just
		# before batch.save() is what identifies the unpack -- no DB read needed.
		batch = SimpleNamespace(
			reference_doctype=None,
			reference_name=None,
			custom_customer="Customer A",
			custom_customer_voucher_type="Customer Repair",
			item="D-NT-RO-6B-+8-8.5",
		)
		self.assertTrue(batch_utils.is_repair_unpack(batch))

	def test_repair_unpack_allows_customer_goods_via_se_reference(self):
		# Second leg: after the SE links the batch, recognise it by the Repair Unpack SE.
		batch = SimpleNamespace(
			reference_doctype="Stock Entry",
			reference_name="GE-SE-RU-26-00003",
			custom_customer="Customer A",
			custom_customer_voucher_type=None,
			item="D-NT-RO-6B-+8-8.5",
		)
		with patch.object(
			batch_utils.frappe.db, "get_value", return_value="Repair Unpack"
		):
			self.assertTrue(batch_utils.is_repair_unpack(batch))

	def test_repair_unpack_requires_a_customer(self):
		# A Customer Goods batch with no customer is malformed (row_ownership rule 3) and
		# must not be silently exempted.
		batch = SimpleNamespace(
			reference_doctype=None,
			reference_name=None,
			custom_customer=None,
			custom_customer_voucher_type="Customer Repair",
			item="D-NT-RO-6B-+8-8.5",
		)
		self.assertFalse(batch_utils.is_repair_unpack(batch))

	def test_repair_unpack_not_applied_to_other_se_types(self):
		batch = SimpleNamespace(
			reference_doctype="Stock Entry",
			reference_name="MAT-STE-TRANSFER",
			custom_customer="Customer A",
			custom_customer_voucher_type=None,
			item="D-NT-RO-6B-+8-8.5",
		)
		with patch.object(
			batch_utils.frappe.db, "get_value", return_value="Customer Goods Transfer"
		):
			self.assertFalse(batch_utils.is_repair_unpack(batch))

	def test_guard_message_names_the_offending_item(self):
		# The anonymous "This item is not allowed as Customer Goods" caused a misdiagnosis;
		# the message must now carry the item so the offending component is obvious.
		batch = SimpleNamespace(
			item="D-NT-RO-6B-+8-8.5",
			custom_inventory_type="Customer Goods",
			custom_customer="Customer A",
			custom_customer_voucher_type=None,
			custom_voucher_detail_no=None,
			reference_doctype=None,
			reference_name=None,
			name="B-NEW-01",
			get=lambda key, default=None: default,
		)
		db = MagicMock()
		db.get_all.return_value = []
		db.get_value.return_value = 0
		with patch.object(batch_utils.frappe, "db", db):
			with self.assertRaises(Exception) as cm:
				batch_utils.update_inventory_dimentions(batch)
		self.assertIn("D-NT-RO-6B-+8-8.5", str(cm.exception))

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
