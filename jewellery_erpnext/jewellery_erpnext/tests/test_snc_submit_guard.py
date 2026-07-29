# Copyright (c) 2026, Nirali and contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.sub_utils import snc


class _Doc(SimpleNamespace):
	"""SimpleNamespace that also supports Frappe-style ``.get()`` access."""

	def get(self, key, default=None):
		return getattr(self, key, default)


def _fg_mwo(**fields):
	defaults = {
		"doctype": "Manufacturing Work Order",
		"name": "MWO-FG-1",
		"for_fg": 1,
		"manufacturing_order": "PMO-0001",
	}
	defaults.update(fields)
	return _Doc(**defaults)


def _sibling(name, snc_requirement="Need", snc_done=0):
	return _Doc(name=name, snc_requirement=snc_requirement, snc_done=snc_done)


def _transfer(**fields):
	defaults = {
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Transfer (WORK ORDER)",
		"manufacturing_work_order": "MWO-WORK-1",
		"custom_request_id": None,
	}
	defaults.update(fields)
	return _Doc(**defaults)


class TestSncSubmitGuard(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	# ---- validate_snc_before_submit -------------------------------------

	def test_non_fg_mwo_is_noop(self):
		with patch.object(snc.frappe, "get_all") as get_all, patch.object(
			snc.frappe, "throw"
		) as throw:
			snc.validate_snc_before_submit(_fg_mwo(for_fg=0))
		get_all.assert_not_called()
		throw.assert_not_called()

	def test_missing_pmo_is_noop(self):
		with patch.object(snc.frappe, "get_all") as get_all, patch.object(
			snc.frappe, "throw"
		) as throw:
			snc.validate_snc_before_submit(_fg_mwo(manufacturing_order=None))
		get_all.assert_not_called()
		throw.assert_not_called()

	def test_blocks_when_sibling_needs_snc_and_not_done(self):
		siblings = [_sibling("MWO-WORK-1", "Need", 0)]
		with patch.object(snc.frappe, "get_all", return_value=siblings), patch.object(
			snc.frappe, "throw", side_effect=RuntimeError
		) as throw:
			with self.assertRaises(RuntimeError):
				snc.validate_snc_before_submit(_fg_mwo())
		self.assertIn("MWO-WORK-1", throw.call_args[0][0])

	def test_allows_when_sibling_settled(self):
		siblings = [_sibling("MWO-WORK-1", "Need", 1)]
		with patch.object(snc.frappe, "get_all", return_value=siblings), patch.object(
			snc.frappe, "throw"
		) as throw:
			snc.validate_snc_before_submit(_fg_mwo())
		throw.assert_not_called()

	def test_allows_when_sibling_not_need(self):
		siblings = [_sibling("MWO-WORK-1", "Not Need", 0)]
		with patch.object(snc.frappe, "get_all", return_value=siblings), patch.object(
			snc.frappe, "throw"
		) as throw:
			snc.validate_snc_before_submit(_fg_mwo())
		throw.assert_not_called()

	def test_fallback_blocks_when_requirement_blank_and_button_visible(self):
		siblings = [_sibling("MWO-WORK-1", None, 0)]
		with patch.object(snc.frappe, "get_all", return_value=siblings), patch.object(
			snc, "validate_button_visibility", return_value=True
		) as vbv, patch.object(snc.frappe, "throw", side_effect=RuntimeError) as throw:
			with self.assertRaises(RuntimeError):
				snc.validate_snc_before_submit(_fg_mwo())
		vbv.assert_called_once_with("MWO-WORK-1")
		self.assertIn("MWO-WORK-1", throw.call_args[0][0])

	def test_fallback_allows_when_requirement_blank_and_button_hidden(self):
		siblings = [_sibling("MWO-WORK-1", "", 0)]
		with patch.object(snc.frappe, "get_all", return_value=siblings), patch.object(
			snc, "validate_button_visibility", return_value=False
		), patch.object(snc.frappe, "throw") as throw:
			snc.validate_snc_before_submit(_fg_mwo())
		throw.assert_not_called()

	def test_button_hidden_when_snc_already_done(self):
		mwo = _Doc(
			name="MWO-WORK-1",
			docstatus=1,
			manufacturing_order="PMO-0001",
			snc_done=1,
			customer="Customer A",
		)
		# snc_done short-circuits before the (heavier) live held-gold computation.
		with patch.object(snc, "_get_mwo", return_value=mwo), patch.object(
			snc, "_mwo_needs_settlement"
		) as needs:
			self.assertFalse(snc.validate_button_visibility("MWO-WORK-1"))
		needs.assert_not_called()

	def test_needs_settlement_detects_later_transfer_borrow(self):
		# Regression for the multi-transfer bug: the first transfer is the order
		# customer's own gold (Not Need), a later transfer borrows another customer's
		# gold. Detection reads the LIVE held position, so the borrow is caught no
		# matter which transfer brought it.
		mwo = _Doc(
			name="MWO-WORK-1",
			docstatus=1,
			manufacturing_order="PMO-0001",
			manufacturing_operation="MOP-1",
			customer="MHCU0012",
		)
		received = [
			_receive_row("M-G-18KT-75.4-P", 2.0, "Waxing WO", "MHCU0012", "B-OWN"),
			_receive_row("M-G-18KT-75.4-P", 1.0, "Setting WO", "KACU0043", "B-BORROW"),
		]
		with patch.object(snc, "_get_mwo", return_value=mwo), patch.object(
			snc, "_is_customer_gold", return_value=1
		), patch.object(snc, "_get_receivable_gold_rows", return_value=received):
			self.assertTrue(snc._mwo_needs_settlement(mwo))

	def test_needs_settlement_false_when_only_own_gold(self):
		mwo = _Doc(
			name="MWO-WORK-1",
			docstatus=1,
			manufacturing_order="PMO-0001",
			manufacturing_operation="MOP-1",
			customer="MHCU0012",
		)
		received = [
			_receive_row("M-G-18KT-75.4-P", 2.0, "Waxing WO", "MHCU0012", "B-OWN"),
		]
		with patch.object(snc, "_get_mwo", return_value=mwo), patch.object(
			snc, "_is_customer_gold", return_value=1
		), patch.object(snc, "_get_receivable_gold_rows", return_value=received):
			self.assertFalse(snc._mwo_needs_settlement(mwo))

	def test_mixed_lists_only_unsettled(self):
		siblings = [
			_sibling("MWO-A", "Need", 0),  # pending -> listed
			_sibling("MWO-B", "Need", 1),  # done -> not listed
			_sibling("MWO-C", "Not Need", 0),  # not needed -> not listed
		]
		with patch.object(snc.frappe, "get_all", return_value=siblings), patch.object(
			snc.frappe, "throw", side_effect=RuntimeError
		) as throw:
			with self.assertRaises(RuntimeError):
				snc.validate_snc_before_submit(_fg_mwo())
		msg = throw.call_args[0][0]
		self.assertIn("MWO-A", msg)
		self.assertNotIn("MWO-B", msg)
		self.assertNotIn("MWO-C", msg)

	# ---- stamp_snc_requirement ------------------------------------------

	def test_stamp_sets_need_and_reopens_done(self):
		# Fresh borrowed gold -> Need, and any prior settlement is re-opened.
		with patch.object(
			snc, "_mwo_needs_settlement", return_value=True
		), patch.object(snc.frappe.db, "set_value") as set_value:
			snc.stamp_snc_requirement(_transfer())
		set_value.assert_called_once_with(
			"Manufacturing Work Order",
			"MWO-WORK-1",
			{"snc_requirement": "Need", "snc_done": 0},
		)

	def test_stamp_sets_not_need_without_touching_done(self):
		# No borrowed gold held -> Not Need; a completed settlement stays done.
		with patch.object(
			snc, "_mwo_needs_settlement", return_value=False
		), patch.object(snc.frappe.db, "set_value") as set_value:
			snc.stamp_snc_requirement(_transfer())
		set_value.assert_called_once_with(
			"Manufacturing Work Order",
			"MWO-WORK-1",
			{"snc_requirement": "Not Need"},
		)

	def test_stamp_skips_non_transfer(self):
		with patch.object(snc.frappe.db, "set_value") as set_value:
			snc.stamp_snc_requirement(_transfer(stock_entry_type="Material Issue"))
		set_value.assert_not_called()

	def test_stamp_skips_snc_settlement_transfer(self):
		with patch.object(snc.frappe.db, "set_value") as set_value:
			snc.stamp_snc_requirement(_transfer(custom_request_id="SNC-abcdef1234"))
		set_value.assert_not_called()

	def test_stamp_skips_when_no_work_order(self):
		with patch.object(snc.frappe.db, "set_value") as set_value:
			snc.stamp_snc_requirement(_transfer(manufacturing_work_order=None))
		set_value.assert_not_called()


def _receive_row(
	item_code,
	qty,
	s_warehouse,
	batch_customer,
	batch_no,
	voucher_type="Customer Subcontracting",
):
	"""A normalized receivable-gold row as _get_receivable_gold_rows would return."""
	return {
		"item_code": item_code,
		"batch_no": batch_no,
		"qty": qty,
		"custom_pure_qty": round(qty * 0.754, 3),
		"batch_customer": batch_customer,
		"customer": batch_customer,
		"batch_voucher_type": voucher_type,
		"inventory_type": "Customer Goods" if batch_customer else "Regular Stock",
		"s_warehouse": s_warehouse,
		"pcs": 1,
		"stock_reservation_entry": "SRE-1",
		"stock_reservation_entry_detail": None,
	}


class TestRowNeedsSettlement(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _mwo(self, customer="MHCU0012"):
		return _Doc(name="MWO-WORK-1", customer=customer)

	def test_regular_order_customer_repair_row_skipped(self):
		# Case 3: regular order borrowing a customer's "Customer Repair" gold must NOT
		# be settled (regression: it needed settlement before the voucher-type gate).
		row = _receive_row(
			"M-G-18KT-75.4-P",
			1.0,
			"Setting WO",
			"KACU0043",
			"B-KACU",
			voucher_type="Customer Repair",
		)
		self.assertFalse(
			snc._row_needs_settlement(self._mwo(), row, pmo_is_customer_gold=0)
		)

	def test_regular_order_customer_subcontracting_row_settled(self):
		# Control for case 2: same borrow under "Customer Subcontracting" still settles.
		row = _receive_row(
			"M-G-18KT-75.4-P",
			1.0,
			"Setting WO",
			"KACU0043",
			"B-KACU",
			voucher_type="Customer Subcontracting",
		)
		self.assertTrue(
			snc._row_needs_settlement(self._mwo(), row, pmo_is_customer_gold=0)
		)

	def test_subcon_different_customer_repair_row_skipped(self):
		# Case 6 guard: even a DIFFERENT customer's gold is skipped when its voucher
		# type is "Customer Repair" -- the global rule wins over order-type logic.
		row = _receive_row(
			"M-G-18KT-75.4-P",
			1.0,
			"Setting WO",
			"KACU0043",
			"B-KACU",
			voucher_type="Customer Repair",
		)
		self.assertFalse(
			snc._row_needs_settlement(self._mwo(), row, pmo_is_customer_gold=1)
		)


class TestSncCreateSettlement(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run_create_snc(self, mwo, received, is_customer_gold, owner_batch):
		original_transfer = _Doc(company="GEPL", branch="BR", to_warehouse="Waxing WO")
		with patch.object(snc, "_get_mwo", return_value=mwo), patch.object(
			snc, "validate_button_visibility", return_value=True
		), patch.object(
			snc, "_is_customer_gold", return_value=is_customer_gold
		), patch.object(
			snc, "_get_original_material_transfer", return_value=original_transfer
		), patch.object(
			snc, "_get_receivable_gold_rows", return_value=received
		), patch.object(
			snc, "find_owner_batch", return_value=owner_batch
		), patch.object(
			snc, "trigger_make_receive", return_value={"docname": "MR-1"}
		) as make_receive, patch.object(
			snc, "create_material_transfer_work_order", return_value="MT-1"
		) as make_transfer, patch.object(snc.frappe.db, "set_value") as set_value:
			snc.create_snc(mwo)
		return make_receive, make_transfer, set_value

	def test_transfer_mirrors_received_rows(self):
		# Subcontracting order: the replacement transfer must mirror the Make Receive
		# row-for-row (1 g + 2.35 g), NOT copy the stale single 2.36 g original row.
		mwo = _Doc(
			name="MWO-WORK-1",
			manufacturing_order="PMO-0001",
			manufacturing_operation="MOP-1",
			customer="MHCU0012",
			company="GEPL",
		)
		received = [
			_receive_row(
				"M-G-18KT-75.4-P", 1.0, "Diamond Setting WO", "KACU0043", "B-KACU"
			),
			_receive_row("M-G-18KT-75.4-P", 2.35, "Waxing WO", None, "B-REG"),
		]
		make_receive, make_transfer, set_value = self._run_create_snc(
			mwo, received, 1, {"batch_no": "MHCU-BATCH", "warehouse": "Model Making RM"}
		)

		make_transfer.assert_called_once()
		rows = make_transfer.call_args[0][2]  # (mwo, original_transfer, rows, owner)
		self.assertEqual([r["qty"] for r in rows], [1.0, 2.35])
		# each row returns owner gold to the warehouse it was received from
		self.assertEqual(
			[r["t_warehouse"] for r in rows], ["Diamond Setting WO", "Waxing WO"]
		)
		self.assertTrue(all(r["batch_no"] == "MHCU-BATCH" for r in rows))
		self.assertTrue(all(r["s_warehouse"] == "Model Making RM" for r in rows))
		self.assertEqual(make_transfer.call_args[0][3], "MHCU0012")  # owner customer
		# Receive triggered once, for the full set of settle rows.
		make_receive.assert_called_once()
		self.assertEqual(make_receive.call_args.kwargs["receive_items"], received)
		set_value.assert_called_once_with(
			"Manufacturing Work Order", "MWO-WORK-1", "snc_done", 1
		)

	def test_regular_order_settles_only_customer_gold(self):
		# Regular order (is_customer_gold=0): only borrowed CUSTOMER gold is settled;
		# regular/company gold stays put.
		mwo = _Doc(
			name="MWO-WORK-2",
			manufacturing_order="PMO-0002",
			manufacturing_operation="MOP-2",
			customer="MHCU0012",
			company="GEPL",
		)
		received = [
			_receive_row("M-G-18KT-75.4-P", 1.0, "Setting WO", "KACU0043", "B-KACU"),
			_receive_row("M-G-18KT-75.4-P", 2.0, "Waxing WO", None, "B-REG"),
		]
		make_receive, make_transfer, _ = self._run_create_snc(
			mwo, received, 0, {"batch_no": "REG-BATCH", "warehouse": "Regular RM"}
		)

		rows = make_transfer.call_args[0][2]
		self.assertEqual([r["qty"] for r in rows], [1.0])  # only the customer-gold row
		self.assertEqual(len(make_receive.call_args.kwargs["receive_items"]), 1)

	def test_customer_repair_row_not_settled(self):
		# Mixed borrow on a regular order: a "Customer Subcontracting" customer batch
		# settles; a "Customer Repair" customer batch is left untouched.
		mwo = _Doc(
			name="MWO-WORK-3",
			manufacturing_order="PMO-0003",
			manufacturing_operation="MOP-3",
			customer="MHCU0012",
			company="GEPL",
		)
		received = [
			_receive_row(
				"M-G-18KT-75.4-P",
				1.0,
				"Setting WO",
				"KACU0043",
				"B-KACU",
				voucher_type="Customer Subcontracting",
			),
			_receive_row(
				"M-G-18KT-75.4-P",
				3.0,
				"Repair WO",
				"TNCU0007",
				"B-REPAIR",
				voucher_type="Customer Repair",
			),
		]
		make_receive, make_transfer, _ = self._run_create_snc(
			mwo, received, 0, {"batch_no": "REG-BATCH", "warehouse": "Regular RM"}
		)

		rows = make_transfer.call_args[0][2]
		# Only the subcontracting row is settled; the repair row is skipped.
		self.assertEqual([r["qty"] for r in rows], [1.0])
		self.assertEqual([r["item_code"] for r in rows], ["M-G-18KT-75.4-P"])
		# Receive is driven by the settle rows, so the repair row is excluded there too
		# (same as regular gold in test_regular_order_settles_only_customer_gold).
		self.assertEqual(len(make_receive.call_args.kwargs["receive_items"]), 1)


class TestSncOwnerBatchAllocation(IntegrationTestCase):
	"""Per-run allocation map for the owner-batch finders.

	The finders take no database hold, so a multi-row settlement can be handed the same
	batch twice and over-draw it at submit ("need X have Y"). Two rules make one run
	self-consistent, and both are load-bearing:

	* a hit is only *reserved* when its consumer submits LATER (reserving a source that
	  is consumed immediately would double-count against the ledger reduction);
	* stock the run *creates* is never returned by a finder, so the conversion output
	  has to be claimed by the caller or a later row re-discovers it.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _find(self, allocated, required, on_hand=3.0, reserve=True):
		with patch.object(
			snc.frappe, "get_all", return_value=["BATCH-1"]
		), patch.object(
			snc, "_get_raw_material_warehouses", return_value=["Central RM"]
		), patch.object(
			snc,
			"_consumable_batch_qty_map",
			return_value={("BATCH-1", "Central RM"): on_hand},
		), patch.object(snc, "get_batch_qty", return_value=on_hand):
			return snc._find_available_owner_batch(
				"CUST-1",
				"M-G-18KT-75.4-P",
				required,
				company="GEPL",
				allocated=allocated,
				reserve=reserve,
			)

	def test_same_batch_is_not_handed_out_twice(self):
		allocated = {}
		first = self._find(allocated, 2.0, on_hand=3.0)
		self.assertEqual(first["batch_no"], "BATCH-1")
		# Only 1.0 left; a second row needing 2.0 must NOT be given the same batch.
		self.assertIsNone(self._find(allocated, 2.0, on_hand=3.0))

	def test_partial_reuse_accumulates(self):
		allocated = {}
		self._find(allocated, 1.0, on_hand=3.0)
		self._find(allocated, 1.5, on_hand=3.0)
		self.assertEqual(allocated[("BATCH-1", "Central RM")], 2.5)

	def test_remaining_balance_is_reported_not_gross(self):
		allocated = {}
		self._find(allocated, 1.0, on_hand=3.0)
		second = self._find(allocated, 1.0, on_hand=3.0)
		self.assertEqual(second["available_qty"], 2.0)

	def test_reserve_false_reads_the_map_without_claiming(self):
		"""Conversion sources submit immediately; reserving would double-count."""
		allocated = {}
		self._find(allocated, 1.0, on_hand=3.0, reserve=False)
		self.assertEqual(allocated, {})

	def test_reserve_false_still_respects_existing_claims(self):
		allocated = {("BATCH-1", "Central RM"): 2.5}
		self.assertIsNone(self._find(allocated, 1.0, on_hand=3.0, reserve=False))

	def test_conversion_output_is_claimed_so_a_later_row_cannot_reuse_it(self):
		"""The claim that actually fixes the original crash."""
		mwo = _Doc(
			name="MWO-WORK-1",
			manufacturing_order="PMO-0001",
			manufacturing_operation="MOP-1",
			customer="MHCU0012",
			company="GEPL",
		)
		received = [
			_receive_row("M-G-18KT-75.4-P", 1.0, "Waxing WO", "KACU0043", "B-1"),
			_receive_row("M-G-18KT-75.4-P", 0.98, "Waxing WO", "KACU0043", "B-2"),
		]
		seen = {}

		def _capture(*args, **kwargs):
			seen["allocated"] = kwargs.get("allocated")
			return {
				"batch_no": "SRC-22KT",
				"item_code": "M-G-22KT-91.6-Y",
				"warehouse": "Model Making RM",
				"qty": 1.2,
			}

		with patch.object(snc, "_get_mwo", return_value=mwo), patch.object(
			snc, "validate_button_visibility", return_value=True
		), patch.object(snc, "_is_customer_gold", return_value=1), patch.object(
			snc,
			"_get_original_material_transfer",
			return_value=_Doc(company="GEPL", branch="BR"),
		), patch.object(
			snc, "_get_receivable_gold_rows", return_value=received
		), patch.object(snc, "find_owner_batch", return_value=None), patch.object(
			snc, "find_owner_rm_warehouse", side_effect=_capture
		), patch.object(
			snc,
			"create_repack_metal_conversion",
			return_value={
				"stock_entry": "SE-CONV",
				"target_batch": "OUT-18KT",
				"warehouse": "Model Making RM",
			},
		), patch.object(
			snc, "trigger_make_receive", return_value={"docname": "MR-1"}
		), patch.object(
			snc, "create_material_transfer_work_order", return_value="MT-1"
		), patch.object(snc.frappe.db, "set_value"):
			snc.create_snc(mwo)

		allocated = seen["allocated"]
		self.assertIsNotNone(allocated)
		# Both rows' conversion outputs land in the same (batch, warehouse) here, so the
		# claim must ACCUMULATE — otherwise row 2 sees row 1's output as free stock.
		self.assertEqual(allocated[("OUT-18KT", "Model Making RM")], 1.98)

	# ---- warehouse / parameter forwarding -------------------------------

	def test_explicit_warehouses_bypass_the_default_lookup(self):
		# When the caller supplies warehouses, the finder must NOT fall back to
		# _get_raw_material_warehouses(company).
		with patch.object(
			snc.frappe, "get_all", return_value=["BATCH-1"]
		), patch.object(
			snc, "_get_raw_material_warehouses"
		) as default_wh, patch.object(
			snc,
			"_consumable_batch_qty_map",
			return_value={("BATCH-1", "Given RM"): 5.0},
		), patch.object(snc, "get_batch_qty", return_value=5.0):
			hit = snc._find_available_owner_batch(
				"CUST-1",
				"M-G-18KT-75.4-P",
				2.0,
				company="GEPL",
				warehouses=["Given RM"],
			)
		self.assertEqual(hit["warehouse"], "Given RM")
		default_wh.assert_not_called()

	def test_find_owner_rm_warehouse_same_purity_forwards_params(self):
		sentinel = {"batch_no": "B", "warehouse": "W"}
		alloc = {}
		with patch.object(
			snc, "_find_available_owner_batch", return_value=sentinel
		) as find:
			result = snc.find_owner_rm_warehouse(
				"CUST-1",
				"ITEM",
				3.0,
				company="GEPL",
				warehouses=["W1"],
				allocated=alloc,
				reserve=False,
			)
		self.assertIs(result, sentinel)
		args, kwargs = find.call_args
		self.assertEqual(args[:4], ("CUST-1", "ITEM", 3.0, "GEPL"))
		self.assertEqual(kwargs["warehouses"], ["W1"])
		self.assertIs(kwargs["allocated"], alloc)
		self.assertEqual(kwargs["reserve"], False)

	def test_find_owner_rm_warehouse_different_purity_forwards_params(self):
		# The convertible-purity branch must forward warehouses/allocated/reserve to
		# the finder for the CANDIDATE item, not the requested one.
		hit = {"batch_no": "B24", "warehouse": "W", "item_code": "CAND-24KT"}
		alloc = {}
		with patch.object(snc, "_get_purity_label", return_value="18KT"), patch.object(
			snc, "PURITY_PRIORITY", ["24KT"]
		), patch.object(
			snc, "_get_gold_items_for_purity", return_value=["CAND-24KT"]
		), patch.object(snc, "_get_item_purity", return_value=99.9), patch.object(
			snc, "_find_available_owner_batch", return_value=hit
		) as find:
			result = snc.find_owner_rm_warehouse(
				"CUST-1",
				"ITEM",
				1.0,
				search_different_purity=True,
				company="GEPL",
				warehouses=["W1"],
				allocated=alloc,
				reserve=False,
			)
		self.assertIs(result, hit)
		args, kwargs = find.call_args
		self.assertEqual(args[0], "CUST-1")
		self.assertEqual(args[1], "CAND-24KT")  # candidate item, not the original
		self.assertEqual(args[3], "GEPL")
		self.assertEqual(kwargs["warehouses"], ["W1"])
		self.assertIs(kwargs["allocated"], alloc)
		self.assertEqual(kwargs["reserve"], False)

	def test_find_owner_batch_forwards_all_params(self):
		sentinel = object()
		alloc = {}
		with patch.object(
			snc, "find_owner_rm_warehouse", return_value=sentinel
		) as find:
			result = snc.find_owner_batch(
				"CUST-1",
				"ITEM",
				2.0,
				company="GEPL",
				warehouses=["W1"],
				allocated=alloc,
				reserve=False,
			)
		self.assertIs(result, sentinel)
		args, kwargs = find.call_args
		self.assertEqual(args, ("CUST-1", "ITEM", 2.0))
		self.assertEqual(kwargs["company"], "GEPL")
		self.assertEqual(kwargs["warehouses"], ["W1"])
		self.assertIs(kwargs["allocated"], alloc)
		self.assertEqual(kwargs["reserve"], False)

	# ---- create_snc call sites ------------------------------------------

	def _snc_mwo(self):
		return _Doc(
			name="MWO-WORK-1",
			manufacturing_order="PMO-0001",
			manufacturing_operation="MOP-1",
			customer="MHCU0012",
			company="GEPL",
		)

	def test_create_snc_passes_the_allocated_map_to_find_owner_batch(self):
		# Found-batch path: find_owner_batch must receive the run's allocation map so
		# the reserve is held against later rows.
		mwo = self._snc_mwo()
		received = [
			_receive_row("M-G-18KT-75.4-P", 1.0, "Waxing WO", "KACU0043", "B-1")
		]
		with patch.object(snc, "_get_mwo", return_value=mwo), patch.object(
			snc, "validate_button_visibility", return_value=True
		), patch.object(snc, "_is_customer_gold", return_value=1), patch.object(
			snc,
			"_get_original_material_transfer",
			return_value=_Doc(company="GEPL", branch="BR"),
		), patch.object(
			snc, "_get_receivable_gold_rows", return_value=received
		), patch.object(
			snc,
			"find_owner_batch",
			return_value={"batch_no": "OWN", "warehouse": "Model Making RM"},
		) as fob, patch.object(
			snc, "trigger_make_receive", return_value={"docname": "MR-1"}
		), patch.object(
			snc, "create_material_transfer_work_order", return_value="MT-1"
		), patch.object(snc.frappe.db, "set_value"):
			snc.create_snc(mwo)
		fob.assert_called_once()
		self.assertIn("allocated", fob.call_args.kwargs)
		self.assertIsInstance(fob.call_args.kwargs["allocated"], dict)

	def test_create_snc_reads_the_conversion_source_without_reserving(self):
		# Conversion path: the source is consumed immediately, so it must be read with
		# reserve=False (still passing the shared allocation map).
		mwo = self._snc_mwo()
		received = [
			_receive_row("M-G-18KT-75.4-P", 1.0, "Waxing WO", "KACU0043", "B-1")
		]
		seen = {}

		def _capture(*args, **kwargs):
			seen["reserve"] = kwargs.get("reserve")
			seen["allocated"] = kwargs.get("allocated")
			return {
				"batch_no": "SRC-22KT",
				"item_code": "M-G-22KT-91.6-Y",
				"warehouse": "Model Making RM",
				"qty": 1.2,
			}

		with patch.object(snc, "_get_mwo", return_value=mwo), patch.object(
			snc, "validate_button_visibility", return_value=True
		), patch.object(snc, "_is_customer_gold", return_value=1), patch.object(
			snc,
			"_get_original_material_transfer",
			return_value=_Doc(company="GEPL", branch="BR"),
		), patch.object(
			snc, "_get_receivable_gold_rows", return_value=received
		), patch.object(snc, "find_owner_batch", return_value=None), patch.object(
			snc, "find_owner_rm_warehouse", side_effect=_capture
		), patch.object(
			snc,
			"create_repack_metal_conversion",
			return_value={
				"stock_entry": "SE-CONV",
				"target_batch": "OUT-18KT",
				"warehouse": "Model Making RM",
			},
		), patch.object(
			snc, "trigger_make_receive", return_value={"docname": "MR-1"}
		), patch.object(
			snc, "create_material_transfer_work_order", return_value="MT-1"
		), patch.object(snc.frappe.db, "set_value"):
			snc.create_snc(mwo)
		self.assertEqual(seen["reserve"], False)
		self.assertIsInstance(seen["allocated"], dict)

	def tearDown(self):
		return super().tearDown()
