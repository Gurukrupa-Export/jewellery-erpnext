# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the Customer Goods Return "Settle" sourcing
(``customer_subcontracting.sub_utils.cg_settle``).

DB-free per the suite convention: ``setUpClass`` is neutralised, docs are plain
``SimpleNamespace``, and the finders / Stock Entry builders / lock helpers are mocked so
the ORCHESTRATION (which entries, in which direction, with which ownership) is verified
without touching the database.
"""

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.sub_utils import cg_settle

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


def _row(idx=1, item=ITEM, qty=10.0, pure=9.16, customer=None, from_wh=None):
	return _Doc(
		idx=idx,
		item_code=item,
		qty=qty,
		custom_pure_qty=pure,
		customer=customer,
		from_warehouse=from_wh,
	)


def _mr(items=None, **overrides):
	values = dict(
		name="MAT-MR-1",
		company="GEPL",
		docstatus=0,
		material_request_type="Material Transfer",
		set_from_warehouse=TARGET,
		customer=CUSTOMER,
		_customer=CUSTOMER,
		branch="BR",
		items=items if items is not None else [_row()],
		check_permission=MagicMock(),
	)
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
	"""side_effect for find_owner_rm_warehouse, dispatching by call shape."""

	def _impl(owner_customer, item_code, qty, **kwargs):
		if record is not None:
			record.append({"owner": owner_customer, "qty": qty, **kwargs})
		if kwargs.get("search_different_purity"):
			return different
		if owner_customer is None:
			return regular
		return same

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

	def test_submitted_mr_is_rejected(self):
		mr = _mr(docstatus=1)
		with patch(f"{_C}.frappe.get_doc", return_value=mr):
			with self.assertRaises(frappe.ValidationError):
				cg_settle.settle_material_request("MAT-MR-1")

	def test_non_material_transfer_is_rejected(self):
		mr = _mr(material_request_type="Purchase")
		with patch(f"{_C}.frappe.get_doc", return_value=mr):
			with self.assertRaises(frappe.ValidationError):
				cg_settle.settle_material_request("MAT-MR-1")

	def test_missing_customer_is_rejected(self):
		mr = _mr(customer=None, _customer=None)
		with patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=0.0):
			with self.assertRaises(frappe.ValidationError):
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
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(
				same=_batch(OTHER, qty=6.0),
				regular=_batch(TARGET, batch="REG"),
				record=calls,
			),
		), patch(f"{_C}._transfer") as tr:
			cg_settle.settle_material_request("MAT-MR-1")

		# shortfall is 10 - 4 = 6, not the full 10
		same_call = [c for c in calls if c["owner"] == CUSTOMER][0]
		self.assertEqual(same_call["qty"], 6.0)
		self.assertEqual(tr.call_args_list[0][0][2], 6.0)  # transferred qty

	def tearDown(self):
		return super().tearDown()


class TestSettleCase2SamePurity(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_two_transfers_in_the_right_direction_and_ownership(self):
		mr = _mr([_row(qty=10.0)])
		with _patch_execute(), patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=0.0), patch(
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(
				same=_batch(OTHER, batch="CUST-B"),
				regular=_batch(TARGET, batch="REG-B"),
			),
		), patch(f"{_C}._transfer") as tr, patch(f"{_C}._convert") as cv:
			res = cg_settle.settle_material_request("MAT-MR-1")

		self.assertEqual(res["settled"], [1])
		cv.assert_not_called()
		self.assertEqual(tr.call_count, 2)

		# 1) customer gold in: OTHER -> TARGET, customer set
		_, item1, qty1, batch1, from1, to1, cust1 = tr.call_args_list[0][0]
		self.assertEqual(
			(from1, to1, cust1, batch1), (OTHER, TARGET, CUSTOMER, "CUST-B")
		)
		# 2) regular gold back: TARGET -> OTHER, no customer
		_, item2, qty2, batch2, from2, to2, cust2 = tr.call_args_list[1][0]
		self.assertEqual((from2, to2, cust2, batch2), (TARGET, OTHER, None, "REG-B"))
		self.assertEqual(qty1, qty2)  # equal-and-opposite, lender ends whole

	def test_missing_regular_gold_fails_loudly(self):
		mr = _mr([_row(qty=10.0)])
		with _patch_execute(), patch(f"{_C}.frappe.get_doc", return_value=mr), patch(
			f"{_C}._get_raw_material_warehouses", return_value=[TARGET, OTHER]
		), patch(f"{_C}._owner_qty_in_warehouse", return_value=0.0), patch(
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
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(same=None, regular=None, different=None),
		):
			with self.assertRaises(frappe.ValidationError):
				cg_settle.settle_material_request("MAT-MR-1")

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
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(
				same=None,
				regular=_batch(TARGET, item=ITEM, batch="REG-ITEM"),
				different=_batch(OTHER, item=ITEM2, batch="CUST-24", qty=9.17),
			),
		), patch(f"{_C}._convert", side_effect=["B-ITEM", "B-ITEM2"]) as cv, patch(
			f"{_C}._transfer"
		) as tr:
			res = cg_settle.settle_material_request("MAT-MR-1")

		self.assertEqual(res["settled"], [1])
		self.assertEqual(cv.call_count, 2)
		self.assertEqual(tr.call_count, 2)

		# 1) customer conversion in the lending warehouse: item2 -> item
		c1 = cv.call_args_list[0].kwargs
		self.assertEqual(
			(c1["source_item"], c1["target_item"], c1["warehouse"], c1["customer"]),
			(ITEM2, ITEM, OTHER, CUSTOMER),
		)
		# 2) regular conversion in the target: item -> item2, company gold
		c2 = cv.call_args_list[1].kwargs
		self.assertEqual(
			(c2["source_item"], c2["target_item"], c2["warehouse"], c2["customer"]),
			(ITEM, ITEM2, TARGET, None),
		)
		# 3) customer transfer of the produced item: OTHER -> TARGET
		_, i3, _, b3, f3, t3, cust3 = tr.call_args_list[0][0]
		self.assertEqual(
			(i3, b3, f3, t3, cust3), (ITEM, "B-ITEM", OTHER, TARGET, CUSTOMER)
		)
		# 4) regular transfer of the produced item2 back: TARGET -> OTHER
		_, i4, _, b4, f4, t4, cust4 = tr.call_args_list[1][0]
		self.assertEqual(
			(i4, b4, f4, t4, cust4), (ITEM2, "B-ITEM2", TARGET, OTHER, None)
		)

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
			f"{_C}.find_owner_rm_warehouse",
			side_effect=_ladder(
				same=_batch(OTHER), regular=_batch(TARGET, batch="REG"), record=calls
			),
		), patch(f"{_C}._transfer"):
			cg_settle.settle_material_request("MAT-MR-1")

		maps = [c["allocated"] for c in calls]
		self.assertGreaterEqual(len(maps), 4)  # 2 rows x (same + regular)
		for m in maps[1:]:
			self.assertIs(m, maps[0])

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

	def tearDown(self):
		return super().tearDown()
