# Copyright (c) 2026, Nirali and contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch
from contextlib import ExitStack, contextmanager
import frappe

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.sub_utils import (
	repack,
	cg_settle,
)
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


_C = "jewellery_erpnext.customer_subcontracting.sub_utils.cg_settle"
_LOCK = "jewellery_erpnext.jewellery_erpnext.lock_order"

TARGET = "Central RM - GEPL"
OTHER = "Waxing RM - GEPL"
CUSTOMER = "CUST-A"
ITEM = "M-G-22KT-91.6-Y"
ITEM2 = "M-G-24KT-99.9-Y"


class _Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


MR_BATCH = "MR-BATCH-01"


def _row(
	idx=1, item=ITEM, qty=10.0, pure=9.16, customer=None, from_wh=None, batch=MR_BATCH
):
	return _Doc(
		idx=idx,
		item_code=item,
		qty=qty,
		custom_pure_qty=pure,
		customer=customer,
		from_warehouse=from_wh,
		batch_no=batch,
	)


def _mr(items=None, **overrides):
	values = {
		"name": "MAT-MR-1",
		"company": "GEPL",
		"docstatus": 0,
		"material_request_type": "Material Transfer",
		"set_from_warehouse": TARGET,
		"customer": CUSTOMER,
		"_customer": CUSTOMER,
		"branch": "BR",
		"items": items if items is not None else [_row()],
		"check_permission": MagicMock(),
	}
	values.update(overrides)
	return _Doc(**values)


def _batch(warehouse, item=ITEM, batch="BATCH-1", qty=10.0):
	return {
		"batch_no": batch,
		"item_code": item,
		"warehouse": warehouse,
		"qty": qty,
		"available_qty": qty,
	}


def _ladder(same=None, regular=None, different=None, record=None):
	"""side_effect for find_owner_rm_warehouse, dispatching by call shape.

	Honours the ``warehouses`` kwarg like the real finder: a scripted batch is only
	returned when its warehouse is in the searched list, so a target-only search and an
	others-only search resolve differently (needed to tell convert-in-place from Case 3).
	"""

	def _pick(batch, whs):
		if not batch:
			return None
		if whs is None or batch["warehouse"] in whs:
			return batch
		return None

	def _impl(owner_customer, item_code, qty, **kwargs):
		if record is not None:
			record.append({"owner": owner_customer, "qty": qty, **kwargs})
		whs = kwargs.get("warehouses")
		if kwargs.get("search_different_purity"):
			return _pick(different, whs)
		if owner_customer is None:
			return _pick(regular, whs)
		return _pick(same, whs)

	return _impl


@contextmanager
def _patch_execute():
	"""Patch the lock-order helpers so _execute does no real locking/series work."""
	with ExitStack() as stack:
		stack.enter_context(patch(f"{_LOCK}.preallocate_series_for_docs"))
		stack.enter_context(patch(f"{_LOCK}.lock_bins"))
		stack.enter_context(patch(f"{_LOCK}.series_stubs", return_value=()))
		yield


class TestSettleValidation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_cancelled_mr_is_rejected(self):
		mr = _mr(docstatus=2)
		with patch(f"{_C}.frappe.get_doc", return_value=mr):  # noqa: SIM117
			with self.assertRaises(frappe.ValidationError):
				cg_settle.settle_material_request("MAT-MR-1")

	def test_submitted_mr_is_settleable(self):
		# Settle now runs on submitted MRs too (not just drafts); only a cancelled
		# document is rejected.
		cg_settle._validate_settleable(_mr(docstatus=1))

	def test_non_material_transfer_is_rejected(self):
		mr = _mr(material_request_type="Purchase")
		with patch(f"{_C}.frappe.get_doc", return_value=mr):  # noqa: SIM117
			with self.assertRaises(frappe.ValidationError):
				cg_settle.settle_material_request("MAT-MR-1")

	def test_missing_customer_is_rejected(self):
		mr = _mr(customer=None, _customer=None)
		with patch(f"{_C}.frappe.get_doc", return_value=mr), patch(  # noqa: SIM117
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=0.0):
			with self.assertRaises(frappe.ValidationError):
				cg_settle.settle_material_request("MAT-MR-1")

	def test_empty_items_is_rejected(self):
		mr = _mr(items=[])
		with patch(f"{_C}.frappe.get_doc", return_value=mr):  # noqa: SIM117
			with self.assertRaises(frappe.ValidationError):
				cg_settle.settle_material_request("MAT-MR-1")

	def test_missing_target_warehouse_is_rejected(self):
		# No from_warehouse on the row and no set_from_warehouse on the MR.
		mr = _mr([_row(from_wh=None)], set_from_warehouse=None)
		with patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[OTHER]
		), self.assertRaises(frappe.ValidationError):
			cg_settle.settle_material_request("MAT-MR-1")

	def tearDown(self):
		return super().tearDown()


class TestSettleCase1Available(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_enough_in_target_is_a_noop(self):
		mr = _mr([_row(qty=10.0)])
		with patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=10.0), patch(
			f"{_C}.find_owner_rm_warehouse"
		) as finder, patch(f"{_C}._transfer") as tr, patch(f"{_C}._convert") as cv:
			res = cg_settle.settle_material_request("MAT-MR-1")

		self.assertEqual(res["settled"], [])
		finder.assert_not_called()
		tr.assert_not_called()
		cv.assert_not_called()

	def test_partial_availability_only_sources_the_shortfall(self):
		mr = _mr([_row(qty=10.0)])
		calls = []
		with _patch_execute(), patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=4.0), patch(
			f"{_C}._gather_convertible", return_value=None
		), patch(
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(
				same=_batch(OTHER, qty=6.0, batch=MR_BATCH),
				regular=_batch(TARGET, batch="REG"),
				record=calls,
			),
		), patch(f"{_C}._transfer") as tr:
			cg_settle.settle_material_request("MAT-MR-1")

		# shortfall is 10 - 4 = 6, not the full 10. Pick the same-purity find (not the
		# in-target convertible search, which is asked in pure weight).
		same_call = next(
			c
			for c in calls
			if c["owner"] == CUSTOMER and not c.get("search_different_purity")
		)
		self.assertEqual(same_call["qty"], 6.0)
		self.assertEqual(tr.call_args_list[0][0][2], 6.0)  # transferred qty

	def tearDown(self):
		return super().tearDown()


class TestSettleCase2SamePurity(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_matching_batch_transfers_directly_without_relabel(self):
		# Found batch already equals the MR batch -> no relabel, just transfer it.
		mr = _mr([_row(qty=10.0)])
		with _patch_execute(), patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=0.0), patch(
			f"{_C}._gather_convertible", return_value=None
		), patch(
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(
				same=_batch(OTHER, batch=MR_BATCH),
				regular=_batch(TARGET, batch="REG-B"),
			),
		), patch(f"{_C}._transfer") as tr, patch(f"{_C}._convert") as cv:
			res = cg_settle.settle_material_request("MAT-MR-1")

		self.assertEqual(res["settled"], [1])
		cv.assert_not_called()
		self.assertEqual(tr.call_count, 2)

		# 1) customer gold in: OTHER -> TARGET, customer set, in the MR batch
		_, _item1, qty1, batch1, from1, to1, cust1 = tr.call_args_list[0][0]
		self.assertEqual(
			(from1, to1, cust1, batch1), (OTHER, TARGET, CUSTOMER, MR_BATCH)
		)
		# 2) regular gold back: TARGET -> OTHER, no customer
		_, _item2, qty2, batch2, from2, to2, cust2 = tr.call_args_list[1][0]
		self.assertEqual((from2, to2, cust2, batch2), (TARGET, OTHER, None, "REG-B"))
		self.assertEqual(qty1, qty2)  # equal-and-opposite, lender ends whole

	def test_different_batch_is_relabelled_into_the_mr_batch(self):
		# Found batch differs -> relabel it (same-item repack) into the MR batch in the
		# source warehouse, then transfer the MR batch in.
		mr = _mr([_row(qty=10.0)])
		with _patch_execute(), patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=0.0), patch(
			f"{_C}._gather_convertible", return_value=None
		), patch(
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(
				same=_batch(OTHER, batch="CUST-B"),
				regular=_batch(TARGET, batch="REG-B"),
			),
		), patch(f"{_C}._convert", return_value=MR_BATCH) as cv, patch(
			f"{_C}._transfer"
		) as tr:
			res = cg_settle.settle_material_request("MAT-MR-1")

		self.assertEqual(res["settled"], [1])
		# One relabel repack: same item in & out, found batch -> MR batch, in OTHER.
		cv.assert_called_once()
		ckw = cv.call_args.kwargs
		self.assertEqual(
			(
				ckw["source_item"],
				ckw["target_item"],
				ckw["source_batch"],
				ckw["target_batch"],
				ckw["warehouse"],
			),
			(ITEM, ITEM, "CUST-B", MR_BATCH, OTHER),
		)
		# Customer transfer now carries the MR batch, not the found batch.
		_, _, _, batch1, from1, to1, cust1 = tr.call_args_list[0][0]
		self.assertEqual(
			(from1, to1, cust1, batch1), (OTHER, TARGET, CUSTOMER, MR_BATCH)
		)

	def test_missing_regular_gold_fails_loudly(self):
		mr = _mr([_row(qty=10.0)])
		with _patch_execute(), patch(f"{_C}.frappe.get_doc", return_value=mr), patch(  # noqa: SIM117
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=0.0), patch(
			f"{_C}._gather_convertible", return_value=None
		), patch(
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(same=_batch(OTHER), regular=None),
		):
			with self.assertRaises(frappe.ValidationError):
				cg_settle.settle_material_request("MAT-MR-1")

	def test_nothing_anywhere_fails_loudly(self):
		mr = _mr([_row(qty=10.0)])
		with patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=0.0), patch(
			f"{_C}._gather_convertible", return_value=None
		), patch(
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(same=None, regular=None, different=None),
		), self.assertRaises(frappe.ValidationError):
			cg_settle.settle_material_request("MAT-MR-1")

	def tearDown(self):
		return super().tearDown()


class TestSettleConvertInPlace(IntegrationTestCase):
	"""Convertible purity already in the TARGET warehouse -> one in-place repack."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_in_target_convertible_is_converted_in_place_and_mirrored(self):
		mr = _mr([_row(qty=10.0, pure=9.16)])
		gathered = {
			"item_code": ITEM2,
			"purity": 99.9,
			"warehouse": TARGET,
			"rows": [
				{"batch_no": "CUST-18-A", "qty": 6.632},
				{"batch_no": "CUST-18-B", "qty": 6.63},
			],
		}
		with _patch_execute(), patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=0.0), patch(
			f"{_C}._gather_convertible", return_value=gathered
		) as gc, patch(
			f"{_C}._reserve_regular",
			return_value=_batch(TARGET, item=ITEM, batch="REG-24"),
		) as rr, patch(f"{_C}.find_owner_rm_warehouse") as finder, patch(
			f"{_C}._convert_multi", return_value=MR_BATCH
		) as cvm, patch(f"{_C}._convert", return_value="REG-18") as cv, patch(
			f"{_C}._transfer"
		) as tr:
			res = cg_settle.settle_material_request("MAT-MR-1")

		self.assertEqual(res["settled"], [1])
		gc.assert_called_once()  # in-target gather tried first
		rr.assert_called_once()  # regular 24KT reserved for the mirror

		# 1) customer multi-source repack: both 18KT batches -> 24KT into the MR batch
		cvm.assert_called_once()
		mkw = cvm.call_args.kwargs
		self.assertEqual(
			(
				mkw["source_item"],
				mkw["target_item"],
				mkw["warehouse"],
				mkw["customer"],
				mkw["target_batch"],
			),
			(ITEM2, ITEM, TARGET, CUSTOMER, MR_BATCH),
		)
		self.assertEqual(
			[r["batch_no"] for r in mkw["source_rows"]], ["CUST-18-A", "CUST-18-B"]
		)
		# 2) regular mirror repack: 24KT (regular) -> 18KT, same qtys reversed, in target
		cv.assert_called_once()
		ckw = cv.call_args.kwargs
		self.assertEqual(
			(ckw["source_item"], ckw["target_item"], ckw["warehouse"], ckw["customer"]),
			(ITEM, ITEM2, TARGET, None),
		)
		self.assertEqual(ckw["source_qty"], 10.0)  # shortfall of 24KT consumed
		self.assertEqual(
			ckw["target_qty"], 13.262
		)  # 6.632 + 6.63 of 18KT produced back
		finder.assert_not_called()
		tr.assert_not_called()

	def test_in_target_is_preferred_over_borrowing_same_purity(self):
		# Both an in-target 18KT and an elsewhere 24KT exist -> convert in place, do not
		# borrow (no transfer, same-purity finder never consulted).
		mr = _mr([_row(qty=10.0)])
		gathered = {
			"item_code": ITEM2,
			"purity": 99.9,
			"warehouse": TARGET,
			"rows": [{"batch_no": "CUST-18-A", "qty": 13.263}],
		}
		with _patch_execute(), patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=0.0), patch(
			f"{_C}._gather_convertible", return_value=gathered
		), patch(
			f"{_C}._reserve_regular",
			return_value=_batch(TARGET, item=ITEM, batch="REG-24"),
		), patch(f"{_C}.find_owner_rm_warehouse") as finder, patch(
			f"{_C}._convert_multi", return_value=MR_BATCH
		) as cvm, patch(f"{_C}._convert", return_value="REG-18"), patch(
			f"{_C}._transfer"
		) as tr:
			cg_settle.settle_material_request("MAT-MR-1")

		cvm.assert_called_once()
		finder.assert_not_called()  # in-target gather short-circuits the ladder
		tr.assert_not_called()

	def tearDown(self):
		return super().tearDown()


class TestSettleCase3Convert(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_two_conversions_then_two_transfers_balanced(self):
		mr = _mr([_row(qty=10.0, pure=9.16)])
		with _patch_execute(), patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=0.0), patch(
			f"{_C}._gather_convertible", return_value=None
		), patch(
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(
				same=None,
				regular=_batch(TARGET, item=ITEM, batch="REG-ITEM"),
				different=_batch(OTHER, item=ITEM2, batch="CUST-24", qty=9.17),
			),
		), patch(f"{_C}._convert", side_effect=[MR_BATCH, "B-ITEM2"]) as cv, patch(
			f"{_C}._transfer"
		) as tr:
			res = cg_settle.settle_material_request("MAT-MR-1")

		self.assertEqual(res["settled"], [1])
		self.assertEqual(cv.call_count, 2)
		self.assertEqual(tr.call_count, 2)

		# 1) customer conversion in the lending warehouse: item2 -> item, produced
		#    directly into the MR's batch.
		c1 = cv.call_args_list[0].kwargs
		self.assertEqual(
			(
				c1["source_item"],
				c1["target_item"],
				c1["warehouse"],
				c1["customer"],
				c1["target_batch"],
			),
			(ITEM2, ITEM, OTHER, CUSTOMER, MR_BATCH),
		)
		# 2) regular conversion in the target: item -> item2, company gold, auto batch
		c2 = cv.call_args_list[1].kwargs
		self.assertEqual(
			(c2["source_item"], c2["target_item"], c2["warehouse"], c2["customer"]),
			(ITEM, ITEM2, TARGET, None),
		)
		self.assertIsNone(c2.get("target_batch"))
		# 3) customer transfer of the produced item (MR batch): OTHER -> TARGET
		_, i3, _, b3, f3, t3, cust3 = tr.call_args_list[0][0]
		self.assertEqual(
			(i3, b3, f3, t3, cust3), (ITEM, MR_BATCH, OTHER, TARGET, CUSTOMER)
		)
		# 4) regular transfer of the produced item2 back: TARGET -> OTHER
		_, i4, _, b4, f4, t4, cust4 = tr.call_args_list[1][0]
		self.assertEqual(
			(i4, b4, f4, t4, cust4), (ITEM2, "B-ITEM2", TARGET, OTHER, None)
		)

	def tearDown(self):
		return super().tearDown()


class TestConvertBatchStamping(IntegrationTestCase):
	"""_convert honours target_batch so create_child_batches won't mint a new one."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run_convert(self, target_batch, produced_query=None):
		appended = []
		fake_se = SimpleNamespace(name="SE-X", insert=MagicMock())
		with patch(f"{_C}._new_se", return_value=fake_se), patch(
			f"{_C}._append_item", side_effect=lambda se, row: appended.append(row)
		), patch(f"{_C}._submit_consuming_stock_entry"), patch(
			f"{_C}.frappe.db.get_value", return_value=produced_query
		) as gv:
			out = cg_settle._convert(
				_mr(),
				source_item=ITEM2,
				source_qty=13.0,
				source_batch="SRC",
				target_item=ITEM,
				target_qty=10.0,
				warehouse=OTHER,
				customer=CUSTOMER,
				target_batch=target_batch,
			)
		return out, appended, gv

	def test_target_batch_is_stamped_and_returned_without_query(self):
		out, appended, gv = self._run_convert(MR_BATCH)
		self.assertEqual(out, MR_BATCH)
		gv.assert_not_called()  # no need to look up the produced batch
		self.assertEqual(
			appended[1]["batch_no"], MR_BATCH
		)  # stamped on the produced row

	def test_no_target_batch_falls_back_to_the_auto_minted_batch(self):
		out, appended, gv = self._run_convert(None, produced_query="AUTO-A-A")
		self.assertEqual(out, "AUTO-A-A")
		gv.assert_called_once()
		self.assertNotIn("batch_no", appended[1])

	def tearDown(self):
		return super().tearDown()


class TestSettleAllocationGuard(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_one_allocation_map_is_shared_across_all_rows(self):
		"""Two rows over one source pool must share a map, or both grab the same batch."""
		mr = _mr([_row(idx=1, qty=5.0), _row(idx=2, qty=5.0)])
		calls = []
		with _patch_execute(), patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=0.0), patch(
			f"{_C}._gather_convertible", return_value=None
		), patch(
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(
				same=_batch(OTHER, batch=MR_BATCH),
				regular=_batch(TARGET, batch="REG"),
				record=calls,
			),
		), patch(f"{_C}._transfer"):
			cg_settle.settle_material_request("MAT-MR-1")

		maps = [c["allocated"] for c in calls]
		self.assertGreaterEqual(len(maps), 4)  # 2 rows x (same + regular)
		for m in maps[1:]:
			self.assertIs(m, maps[0])

	def test_second_row_cannot_reuse_target_stock_claimed_by_first_row(self):
		# consumed_target behaviour, end to end: the target holds 8 of the customer's
		# item; two rows each need 5. Row 1 is satisfied from the target and claims it,
		# so row 2 no longer sees that stock as free and must source its 2-unit shortfall.
		mr = _mr([_row(idx=1, qty=5.0), _row(idx=2, qty=5.0)])
		calls = []
		with _patch_execute(), patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}.frappe.get_all", return_value=["B-TGT"]), patch(
			f"{_C}._consumable_batch_qty_map", return_value={("B-TGT", TARGET): 8.0}
		), patch(f"{_C}._gather_convertible", return_value=None), patch(
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(
				same=_batch(OTHER, batch=MR_BATCH),
				regular=_batch(TARGET, batch="REG"),
				record=calls,
			),
		), patch(f"{_C}._transfer"):
			res = cg_settle.settle_material_request("MAT-MR-1")

		# Only row 2 needed sourcing; row 1 was Available from the target.
		self.assertEqual(res["settled"], [2])
		same_calls = [
			c
			for c in calls
			if c["owner"] == CUSTOMER and not c.get("search_different_purity")
		]
		self.assertEqual(len(same_calls), 1)
		self.assertEqual(same_calls[0]["qty"], 2.0)  # 5 required - 3 remaining

	def tearDown(self):
		return super().tearDown()


class TestOwnerQtyInWarehouse(IntegrationTestCase):
	"""The pre-existing-availability calc that drives the shortfall."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_nets_out_earlier_row_claims(self):
		consumed = {}
		with patch(f"{_C}.frappe.get_all", return_value=["B1"]), patch(
			f"{_C}._consumable_batch_qty_map", return_value={("B1", TARGET): 8.0}
		):
			first = cg_settle._owner_qty_in_warehouse(CUSTOMER, ITEM, TARGET, consumed)
			cg_settle._claim_target(consumed, CUSTOMER, ITEM, TARGET, 5.0)
			second = cg_settle._owner_qty_in_warehouse(CUSTOMER, ITEM, TARGET, consumed)

		self.assertEqual(first, 8.0)
		self.assertEqual(second, 3.0)

	def test_no_batches_is_zero(self):
		with patch(f"{_C}.frappe.get_all", return_value=[]):
			self.assertEqual(
				cg_settle._owner_qty_in_warehouse(CUSTOMER, ITEM, TARGET, {}), 0.0
			)

	def test_over_claim_floors_at_zero(self):
		# Earlier rows earmarked more than is on hand -> never report a negative.
		consumed = {(CUSTOMER, ITEM, TARGET): 20.0}
		with patch(f"{_C}.frappe.get_all", return_value=["B1"]), patch(
			f"{_C}._consumable_batch_qty_map", return_value={("B1", TARGET): 8.0}
		):
			self.assertEqual(
				cg_settle._owner_qty_in_warehouse(CUSTOMER, ITEM, TARGET, consumed), 0.0
			)

	def tearDown(self):
		return super().tearDown()


class TestSettleHelpers(IntegrationTestCase):
	"""Small pure helpers used by the planner and locker."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_pure_weight_scales_by_shortfall_proportion(self):
		# 9.16 pure over a 10 required, sourcing only the 6 shortfall -> 5.496.
		row = _row(qty=10.0, pure=9.16)
		self.assertEqual(cg_settle._pure_weight(row, ITEM, 6.0, 10.0), 5.496)

	def test_pure_weight_falls_back_to_item_purity(self):
		# No custom_pure_qty on the row -> derive from the item's purity %.
		row = _row(qty=10.0, pure=0)
		with patch(f"{_C}._get_item_purity", return_value=75.0):
			self.assertEqual(cg_settle._pure_weight(row, ITEM, 6.0, 10.0), 4.5)

	def test_claim_target_ignores_non_positive_qty(self):
		consumed = {}
		cg_settle._claim_target(consumed, CUSTOMER, ITEM, TARGET, 0)
		cg_settle._claim_target(consumed, CUSTOMER, ITEM, TARGET, -5.0)
		self.assertEqual(consumed, {})
		cg_settle._claim_target(consumed, CUSTOMER, ITEM, TARGET, 3.0)
		self.assertEqual(consumed[(CUSTOMER, ITEM, TARGET)], 3.0)

	def test_bin_pairs_same_purity_locks_item_in_both_warehouses(self):
		actionable = [
			{
				"level": cg_settle.LEVEL_SAME_PURITY,
				"item": ITEM,
				"target": TARGET,
				"src": {"warehouse": OTHER},
			}
		]
		self.assertEqual(
			cg_settle._bin_pairs(actionable), [(ITEM, TARGET), (ITEM, OTHER)]
		)

	def test_bin_pairs_convert_also_locks_the_source_item(self):
		actionable = [
			{
				"level": cg_settle.LEVEL_CONVERT,
				"item": ITEM,
				"target": TARGET,
				"src": {"warehouse": OTHER, "item_code": ITEM2},
			}
		]
		self.assertEqual(
			cg_settle._bin_pairs(actionable),
			[(ITEM, TARGET), (ITEM, OTHER), (ITEM2, OTHER), (ITEM2, TARGET)],
		)

	def tearDown(self):
		return super().tearDown()


class TestStockEntryBuilders(IntegrationTestCase):
	"""The Stock Entry construction/insert/submit helpers, mocked out of the
	orchestration tests."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_new_se_stamps_customer_goods_and_branch_fallback(self):
		se = MagicMock()
		with patch(f"{_C}.frappe.new_doc", return_value=se), patch(
			f"{_C}.frappe.db.get_value", return_value="BR-FALLBACK"
		) as gv:
			result = cg_settle._new_se(
				_mr(branch=None),
				cg_settle.TRANSFER_SE_TYPE,
				"Material Transfer",
				OTHER,
				TARGET,
				CUSTOMER,
			)
		self.assertIs(result, se)
		vals = se.update.call_args[0][0]
		self.assertEqual(vals["stock_entry_type"], cg_settle.TRANSFER_SE_TYPE)
		self.assertEqual(vals["inventory_type"], cg_settle.CUSTOMER_GOODS)
		self.assertEqual(vals["_customer"], CUSTOMER)
		self.assertEqual(vals["auto_created"], 1)
		self.assertEqual(vals["add_to_transit"], 0)
		self.assertEqual(vals["branch"], "BR-FALLBACK")
		gv.assert_called_once_with("Warehouse", OTHER, "custom_branch")

	def test_new_se_is_regular_stock_without_a_customer(self):
		se = MagicMock()
		with patch(f"{_C}.frappe.new_doc", return_value=se):
			cg_settle._new_se(
				_mr(),
				cg_settle.TRANSFER_SE_TYPE,
				"Material Transfer",
				OTHER,
				TARGET,
				None,
			)
		vals = se.update.call_args[0][0]
		self.assertEqual(vals["inventory_type"], cg_settle.REGULAR_STOCK)
		self.assertIsNone(vals["_customer"])

	def test_transfer_appends_row_inserts_and_submits(self):
		se = MagicMock()
		se.name = "SE-T1"
		with patch(f"{_C}._new_se", return_value=se) as new_se, patch(
			f"{_C}._append_item"
		) as append, patch(f"{_C}._submit_consuming_stock_entry") as submit:
			name = cg_settle._transfer(_mr(), ITEM, 6.0, "B-1", OTHER, TARGET, CUSTOMER)
		self.assertEqual(name, "SE-T1")
		self.assertEqual(new_se.call_args[0][1], cg_settle.TRANSFER_SE_TYPE)
		self.assertEqual(new_se.call_args[0][2], "Material Transfer")
		row = append.call_args[0][1]
		self.assertEqual(row["item_code"], ITEM)
		self.assertEqual(row["qty"], 6.0)
		self.assertEqual(row["batch_no"], "B-1")
		self.assertEqual((row["s_warehouse"], row["t_warehouse"]), (OTHER, TARGET))
		self.assertEqual(row["inventory_type"], cg_settle.CUSTOMER_GOODS)
		se.insert.assert_called_once_with(ignore_permissions=True)
		submit.assert_called_once_with(se)

	def test_convert_builds_two_row_repack_and_returns_produced_batch(self):
		se = MagicMock()
		se.name = "SE-C1"
		with patch(f"{_C}._new_se", return_value=se) as new_se, patch(
			f"{_C}._append_item"
		) as append, patch(f"{_C}._submit_consuming_stock_entry") as submit, patch(
			f"{_C}.frappe.db.get_value", return_value="OUT-BATCH"
		):
			produced = cg_settle._convert(
				_mr(), ITEM2, 9.17, "SRC-B", ITEM, 10.0, OTHER, CUSTOMER
			)
		self.assertEqual(produced, "OUT-BATCH")
		self.assertEqual(new_se.call_args[0][1], cg_settle.REPACK_SE_TYPE)
		self.assertEqual(new_se.call_args[0][2], "Repack")
		self.assertEqual(append.call_count, 2)
		src_row = append.call_args_list[0][0][1]
		tgt_row = append.call_args_list[1][0][1]
		self.assertEqual((src_row["item_code"], src_row["s_warehouse"]), (ITEM2, OTHER))
		self.assertEqual((tgt_row["item_code"], tgt_row["t_warehouse"]), (ITEM, OTHER))
		se.insert.assert_called_once_with(ignore_permissions=True)
		submit.assert_called_once_with(se)

	def test_convert_raises_when_no_target_batch_is_produced(self):
		se = MagicMock()
		se.name = "SE-C2"
		with patch(f"{_C}._new_se", return_value=se), patch(
			f"{_C}._append_item"
		), patch(f"{_C}._submit_consuming_stock_entry"), patch(
			f"{_C}.frappe.db.get_value", return_value=None
		):
			with self.assertRaises(frappe.ValidationError):
				cg_settle._convert(
					_mr(), ITEM2, 9.17, "SRC-B", ITEM, 10.0, OTHER, CUSTOMER
				)

	def tearDown(self):
		return super().tearDown()
