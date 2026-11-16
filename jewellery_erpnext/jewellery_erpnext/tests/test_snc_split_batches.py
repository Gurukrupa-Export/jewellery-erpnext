# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Create SNC: the owner's metal may come from several batches of ONE warehouse.

GJCU0009 held 22.057 g of 24KT in Central RM as 12.0 g + 10.0 g + 0.057 g; settling 15 g of
22KT needs 13.777 g of 24KT, which no single batch held, so Create SNC threw "No available
stock" although the warehouse had plenty. Every DB read is stubbed: these tests need no data.
"""

from contextlib import ExitStack, nullcontext
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.sub_utils import snc

OWNER = "CUST-SPLIT"
GOLD_24 = "M-G-24KT-99.9-Y"
GOLD_22 = "M-G-22KT-91.75-Y"
RM = "Central RM - T"
RM_2 = "Tagging RM - T"
# 15 g of 22KT carries 13.763 g fine; 13.763 / 0.999 = 13.777 g of 24KT.
NEED_24 = 13.777


class _FakeStockEntry:
	"""The slice of a Stock Entry document create_repack_metal_conversion touches."""

	name = "MAT-STE-SPLIT"

	def __init__(self):
		self.items = []
		self.header = {}

	def update(self, values):
		self.header.update(values)

	def append(self, table, values):
		row = frappe._dict(values)
		self.items.append(row)
		return row

	def insert(self, **kwargs):
		pass


def _settings(key=None, *args, **kwargs):
	return {"rounding_method": "Banker's Rounding", "time_zone": "Asia/Kolkata"}.get(
		key
	)


class TestSNCSplitBatches(IntegrationTestCase):
	def setUp(self):
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		# flt(x, 3) reads the rounding method; pin it so a cold CI process cannot zero it.
		self.stack.enter_context(
			patch.object(frappe, "get_system_settings", side_effect=_settings)
		)

	def _stock(self, batches, consumable, reserved=None):
		"""Stub the owner's batches, their consumable balance and get_batch_qty.

		``consumable`` maps ``(batch, warehouse)`` to qty; ``reserved`` maps the same key to
		the qty a Stock Reservation Entry holds (get_batch_qty nets it out).
		"""
		reserved = reserved or {}
		self.stack.enter_context(
			patch.object(snc, "_owner_batch_names", return_value=list(batches))
		)
		self.stack.enter_context(
			patch.object(
				snc,
				"_consumable_batch_qty_map",
				side_effect=lambda batch_nos, item_code, warehouses: {
					key: qty
					for key, qty in consumable.items()
					if key[0] in batch_nos and key[1] in warehouses
				},
			)
		)
		self.stack.enter_context(
			patch.object(
				snc,
				"get_batch_qty",
				side_effect=lambda batch_no, warehouse, item_code: consumable.get(
					(batch_no, warehouse), 0
				)
				- reserved.get((batch_no, warehouse), 0),
			)
		)

	def _central_rm_split(self):
		self._stock(
			["B04", "B05", "B61"],
			{("B04", RM): 12.0, ("B05", RM): 10.0, ("B61", RM): 0.057},
		)

	def test_split_covers_the_need_oldest_batch_first(self):
		self._central_rm_split()
		allocated = {}
		found = snc._find_owner_batches_in_one_warehouse(
			OWNER, GOLD_24, NEED_24, warehouses=[RM], allocated=allocated
		)
		self.assertEqual(
			found["sources"],
			[{"batch_no": "B04", "qty": 12.0}, {"batch_no": "B05", "qty": 1.777}],
		)
		self.assertEqual((found["warehouse"], found["batch_no"]), (RM, "B04"))
		self.assertEqual(found["qty"], NEED_24)
		self.assertEqual(allocated, {("B04", RM): 12.0, ("B05", RM): 1.777})

	def test_split_never_mixes_warehouses(self):
		self._stock(["B1", "B2"], {("B1", RM): 8.0, ("B2", RM_2): 8.0})
		self.assertIsNone(
			snc._find_owner_batches_in_one_warehouse(
				OWNER, GOLD_24, NEED_24, warehouses=[RM, RM_2]
			)
		)

	def test_split_takes_the_warehouse_that_holds_enough(self):
		self._stock(
			["B1", "B2", "B3"],
			{("B1", RM): 8.0, ("B2", RM_2): 8.0, ("B3", RM_2): 8.0},
		)
		found = snc._find_owner_batches_in_one_warehouse(
			OWNER, GOLD_24, NEED_24, warehouses=[RM, RM_2]
		)
		self.assertEqual(found["warehouse"], RM_2)
		self.assertEqual(
			found["sources"],
			[{"batch_no": "B2", "qty": 8.0}, {"batch_no": "B3", "qty": 5.777}],
		)

	def test_split_nets_what_earlier_rows_claimed(self):
		self._central_rm_split()
		allocated = {("B04", RM): 5.0}
		found = snc._find_owner_batches_in_one_warehouse(
			OWNER, GOLD_24, NEED_24, warehouses=[RM], allocated=allocated
		)
		self.assertEqual(
			found["sources"],
			[{"batch_no": "B04", "qty": 7.0}, {"batch_no": "B05", "qty": 6.777}],
		)
		self.assertEqual(allocated, {("B04", RM): 12.0, ("B05", RM): 6.777})

	def test_split_does_not_record_claims_without_reserve(self):
		self._central_rm_split()
		allocated = {}
		snc._find_owner_batches_in_one_warehouse(
			OWNER, GOLD_24, NEED_24, warehouses=[RM], allocated=allocated, reserve=False
		)
		self.assertEqual(allocated, {})

	def test_split_respects_stock_reservations(self):
		self._stock(
			["B04", "B05", "B61"],
			{("B04", RM): 12.0, ("B05", RM): 10.0, ("B61", RM): 0.057},
			reserved={("B04", RM): 10.0},
		)
		# Free: 2.0 + 10.0 + 0.057 = 12.057 < 13.777.
		self.assertIsNone(
			snc._find_owner_batches_in_one_warehouse(
				OWNER, GOLD_24, NEED_24, warehouses=[RM]
			)
		)

	def _different_purity(self, allow_split):
		self.stack.enter_context(
			patch.object(
				snc,
				"_get_gold_items_for_purity",
				side_effect=lambda purity, item: [GOLD_24] if purity == "24KT" else [],
			)
		)
		self.stack.enter_context(
			patch.object(
				snc,
				"_get_item_purity",
				side_effect=lambda item: {GOLD_24: 99.9, GOLD_22: 91.75}[item],
			)
		)
		return snc.find_owner_rm_warehouse(
			OWNER,
			GOLD_22,
			13.763,
			search_different_purity=True,
			warehouses=[RM],
			allocated={},
			allow_split=allow_split,
		)

	def test_different_purity_split_only_when_the_caller_opts_in(self):
		self._central_rm_split()
		# Settle (cg_settle) keeps the one-batch contract: it reads batch_no for the full qty.
		self.assertIsNone(self._different_purity(allow_split=False))

	def test_different_purity_split_for_create_snc(self):
		self._central_rm_split()
		found = self._different_purity(allow_split=True)
		self.assertEqual(found["item_code"], GOLD_24)
		self.assertEqual(found["qty"], NEED_24)
		self.assertEqual(
			[part["qty"] for part in found["sources"]],
			[12.0, 1.777],
		)

	def test_single_batch_is_still_preferred_over_a_split(self):
		self._stock(
			["B04", "B05", "BIG"],
			{("B04", RM): 12.0, ("B05", RM): 10.0, ("BIG", RM): 20.0},
		)
		found = self._different_purity(allow_split=True)
		self.assertEqual(found["batch_no"], "BIG")
		self.assertNotIn("sources", found)

	def test_split_pure_qty_adds_up_exactly(self):
		parts = snc._split_pure_qty(
			[{"batch_no": "B1", "qty": 5.39}, {"batch_no": "B2", "qty": 9.61}], 13.763
		)
		self.assertEqual([p[:2] for p in parts], [("B1", 5.39), ("B2", 9.61)])
		self.assertAlmostEqual(sum(p[2] for p in parts), 13.763, places=6)

	def test_conversion_consumes_one_row_per_source_batch(self):
		entry = _FakeStockEntry()
		real_get_value = frappe.db.get_value

		def get_value(*args, **kwargs):
			if args and args[0] == "Stock Entry Detail":
				return "TARGET-22"
			return real_get_value(*args, **kwargs)

		self.stack.enter_context(patch.object(frappe, "new_doc", return_value=entry))
		self.stack.enter_context(
			patch.object(frappe.db, "get_value", side_effect=get_value)
		)
		self.stack.enter_context(
			patch.object(snc, "_get_item_purity", return_value=91.75)
		)
		self.stack.enter_context(patch.object(snc, "_submit_consuming_stock_entry"))
		self.stack.enter_context(
			patch.object(snc, "snc_settlement_conversion", return_value=nullcontext())
		)
		mwo = frappe._dict(
			name="MWO-1",
			manufacturing_order="PMO-1",
			manufacturing_operation="MOP-1",
		)
		original = frappe._dict(company="Co", branch=None)

		result = snc.create_repack_metal_conversion(
			mwo=mwo,
			original_transfer=original,
			source_batch={
				"item_code": GOLD_24,
				"batch_no": "B04",
				"warehouse": RM,
				"qty": NEED_24,
				"sources": [
					{"batch_no": "B04", "qty": 12.0},
					{"batch_no": "B05", "qty": 1.777},
				],
			},
			required_item_code=GOLD_22,
			required_pure_qty=13.763,
			required_qty=15.0,
			owner_customer=OWNER,
		)

		consumed = [
			(row.batch_no, row.qty) for row in entry.items if row.get("s_warehouse")
		]
		produced = [row for row in entry.items if row.get("t_warehouse")]
		self.assertEqual(consumed, [("B04", 12.0), ("B05", 1.777)])
		self.assertEqual(len(produced), 1)
		self.assertEqual((produced[0].item_code, produced[0].qty), (GOLD_22, 15.0))
		self.assertEqual(result["target_batch"], "TARGET-22")
