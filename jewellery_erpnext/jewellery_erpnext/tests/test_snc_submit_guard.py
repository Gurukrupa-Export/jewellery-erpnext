# Copyright (c) 2026, Nirali and contributors
# See license.txt

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting import customer_gold_fulfilment as cgf
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	VALUATION_ZERO,
)
from jewellery_erpnext.customer_subcontracting.sub_utils import snc
from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events import (
	utils as batch_utils,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)

# Only the modules are imported, so none of their Test* classes is collected again here.
from jewellery_erpnext.jewellery_erpnext.tests import (
	test_conversion_lane_downstream as lane_downstream,
)
from jewellery_erpnext.jewellery_erpnext.tests import (
	test_metal_conversion_batch_isolation as mc_isolation,
)


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


def _without_target(items):
	"""Receive items minus the per-row ``t_warehouse`` create_snc adds to each row."""
	return [{k: v for k, v in item.items() if k != "t_warehouse"} for item in items]


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
		with patch.object(
			snc, "_get_snc_source_warehouses", return_value=["Model Making RM"]
		), patch.object(snc, "_get_mwo", return_value=mwo), patch.object(
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
		# Receive triggered once, for the full set of settle rows, each landing in the
		# warehouse its owner batch is drawn from.
		make_receive.assert_called_once()
		receive_items = make_receive.call_args.kwargs["receive_items"]
		self.assertEqual(_without_target(receive_items), received)
		self.assertEqual(
			[r["t_warehouse"] for r in receive_items], ["Model Making RM"] * 2
		)
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
	* create_snc finds EVERY row's owner stock before it submits anything, so all of its
	  hits are reserved, and no finder can re-discover stock the run mints.
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

	def test_every_source_is_found_and_reserved_before_anything_is_submitted(self):
		"""Two rows on the conversion path: both owner sources are found (and reserved in
		the shared map) before the receive or any conversion is submitted, so no finder
		can see a conversion's output or a source another row already took."""
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
		events = []

		def _find(*args, **kwargs):
			events.append(
				("find", kwargs.get("reserve", True), id(kwargs["allocated"]))
			)
			return {
				"batch_no": "SRC-22KT",
				"item_code": "M-G-22KT-91.6-Y",
				"warehouse": "Model Making RM",
				"qty": 1.2,
			}

		def _convert(**kwargs):
			events.append(("convert",))
			return {
				"stock_entry": "SE-CONV",
				"target_batch": "OUT-18KT",
				"warehouse": "Model Making RM",
			}

		def _receive(*args, **kwargs):
			events.append(("receive",))
			return {"docname": "MR-1"}

		with patch.object(snc, "_get_mwo", return_value=mwo), patch.object(
			snc, "validate_button_visibility", return_value=True
		), patch.object(snc, "_is_customer_gold", return_value=1), patch.object(
			snc,
			"_get_original_material_transfer",
			return_value=_Doc(company="GEPL", branch="BR"),
		), patch.object(
			snc, "_get_receivable_gold_rows", return_value=received
		), patch.object(
			snc, "_get_snc_source_warehouses", return_value=["Model Making RM"]
		), patch.object(snc, "find_owner_batch", return_value=None), patch.object(
			snc, "find_owner_rm_warehouse", side_effect=_find
		), patch.object(
			snc, "create_repack_metal_conversion", side_effect=_convert
		), patch.object(
			snc, "trigger_make_receive", side_effect=_receive
		), patch.object(
			snc, "create_material_transfer_work_order", return_value="MT-1"
		), patch.object(snc.frappe.db, "set_value"):
			snc.create_snc(mwo)

		kinds = [e[0] for e in events]
		self.assertEqual(kinds, ["find", "find", "receive"] + ["convert"] * 4)
		finds = [e for e in events if e[0] == "find"]
		self.assertTrue(all(reserve for _, reserve, _ in finds))  # all reserved
		self.assertEqual(len({alloc for _, _, alloc in finds}), 1)  # one shared map

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
		# the reserve is held against later rows, and only the SNC source warehouses.
		mwo = self._snc_mwo()
		received = [
			_receive_row("M-G-18KT-75.4-P", 1.0, "Waxing WO", "KACU0043", "B-1")
		]
		with patch.object(
			snc, "_get_snc_source_warehouses", return_value=["Store RM"]
		), patch.object(snc, "_get_mwo", return_value=mwo), patch.object(
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
		self.assertEqual(fob.call_args.kwargs["warehouses"], ["Store RM"])

	def test_create_snc_reserves_the_conversion_source(self):
		# Conversion path: the source is found in pass 1 and only consumed in pass 2, so
		# it must be reserved in the shared allocation map, like any other hit.
		mwo = self._snc_mwo()
		received = [
			_receive_row("M-G-18KT-75.4-P", 1.0, "Waxing WO", "KACU0043", "B-1")
		]
		seen = {}

		def _capture(*args, **kwargs):
			seen["reserve"] = kwargs.get("reserve", True)
			seen["allocated"] = kwargs.get("allocated")
			seen["warehouses"] = kwargs.get("warehouses")
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
		), patch.object(
			snc, "_get_snc_source_warehouses", return_value=["Store RM"]
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
		self.assertEqual(seen["reserve"], True)
		self.assertIsInstance(seen["allocated"], dict)
		self.assertEqual(seen["warehouses"], ["Store RM"])

	def tearDown(self):
		return super().tearDown()


F_ITEM = "F-G-22KT-91.75-Y-HG-RBH-2.70 MM"
M_ITEM = "M-G-22KT-91.75-Y"


def _template_of(item_code):
	# Stand-in for Item.variant_of: the code's first segment (M, F, D, G).
	return item_code.split("-")[0]


class TestSncFindingSettlement(IntegrationTestCase):
	"""SNC settles borrowed Finding (F) rows as well as Metal (M) rows.

	A finding the owner already holds is transferred as-is; otherwise the owner's metal is
	converted into the finding (the Metal Conversions M -> F pattern) and the borrowed
	finding is mirrored back into that metal for its original owner.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _mwo(self, customer="GJCU0009"):
		return _Doc(
			name="MWO-WORK-1",
			manufacturing_order="PMO-0001",
			manufacturing_operation="MOP-1",
			customer=customer,
			company="GEPL",
		)

	def _run(self, mwo, received, is_customer_gold, **mocks):
		"""Run create_snc with every collaborator mocked; ``mocks`` override defaults."""
		defaults = {
			"find_owner_batch": None,
			"find_owner_metal_for_finding": None,
			"find_owner_rm_warehouse": None,
			"create_repack_metal_conversion": {
				"stock_entry": "SE-CONV",
				"target_batch": "OUT-F",
				"warehouse": "Central RM",
			},
		}
		defaults.update(mocks)
		patches = {}
		with ExitStack() as stack:
			enter = stack.enter_context
			enter(patch.object(snc, "_get_mwo", return_value=mwo))
			enter(patch.object(snc, "validate_button_visibility", return_value=True))
			enter(patch.object(snc, "_is_customer_gold", return_value=is_customer_gold))
			enter(
				patch.object(
					snc,
					"_get_original_material_transfer",
					return_value=_Doc(company="GEPL", branch="BR"),
				)
			)
			enter(patch.object(snc, "_get_receivable_gold_rows", return_value=received))
			enter(patch.object(snc, "_item_template", side_effect=_template_of))
			enter(
				patch.object(
					snc, "_get_snc_source_warehouses", return_value=["Central RM"]
				)
			)
			for name, value in defaults.items():
				kwarg = "side_effect" if callable(value) else "return_value"
				patches[name] = enter(patch.object(snc, name, **{kwarg: value}))
			patches["trigger_make_receive"] = enter(
				patch.object(
					snc, "trigger_make_receive", return_value={"docname": "MR-1"}
				)
			)
			patches["create_material_transfer_work_order"] = enter(
				patch.object(
					snc, "create_material_transfer_work_order", return_value="MT-1"
				)
			)
			enter(patch.object(snc.frappe.db, "set_value"))
			result = snc.create_snc(mwo)
		return result, patches

	# ---- which rows are settleable --------------------------------------

	def test_receivable_rows_keep_gold_metal_and_finding_only(self):
		live_rows = [
			{
				"item_code": code,
				"available_to_receive_qty": qty,
				"s_warehouse": "Waxing WO",
			}
			for code, qty in (
				(M_ITEM, 1.0),
				(F_ITEM, 0.5),
				("D-NT-RD-1", 0.1),
				("G-GR-OV-1", 0.2),
				# Non-gold metals: the finders source gold only, so they stay out.
				("M-S-22KT-92.5-W", 0.3),
				("M-EG-24KT-99.5-Y", 0.3),
				("M-AL", 0.3),
				("M-L-142", 0.3),
			)
		]
		with patch.object(
			snc, "get_make_receive_entry_rows", return_value={"rows": live_rows}
		), patch.object(
			snc, "_get_raw_material_warehouses", return_value=["Central RM"]
		), patch.object(snc, "_item_template", side_effect=_template_of), patch.object(
			snc, "_get_item_purity", return_value=91.75
		):
			rows = snc._get_receivable_gold_rows(self._mwo())
		self.assertEqual([r["item_code"] for r in rows], [M_ITEM, F_ITEM])
		self.assertEqual(rows[1]["custom_pure_qty"], 0.459)  # 0.5 x 91.75%

	# ---- metal candidates for a finding ---------------------------------

	def test_metal_items_for_finding_try_same_touch_and_exact_purity_first(self):
		items = ["M-G-24KT-99.9-Y", "M-G-22KT-91.9-Y", "M-G-18KT-75.15-Y", M_ITEM]
		with patch.object(
			snc.frappe, "get_all", return_value=items
		) as get_all, patch.object(
			snc, "_get_item_purity", side_effect=lambda code: float(code.split("-")[3])
		):
			candidates = snc._get_metal_items_for_finding(F_ITEM)
		self.assertEqual(
			candidates,
			[M_ITEM, "M-G-22KT-91.9-Y", "M-G-24KT-99.9-Y", "M-G-18KT-75.15-Y"],
		)
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["variant_of"], "M")
		self.assertEqual(filters["name"], ["like", "M-G-%-Y"])  # type G, colour Y

	def test_metal_items_for_finding_rejects_malformed_code(self):
		with patch.object(snc.frappe, "get_all") as get_all:
			self.assertEqual(snc._get_metal_items_for_finding("F-G"), [])
		get_all.assert_not_called()

	def test_find_owner_metal_for_finding_sizes_source_by_fine_weight(self):
		# The first candidate has no stock; the second is sized to carry the same fine
		# metal: 0.9175 pure / 99.9% = 0.918 g of 24KT.
		hit = {
			"batch_no": "CUST-24",
			"warehouse": "Central RM",
			"item_code": "M-G-24KT-99.9-Y",
		}
		alloc = {}
		with patch.object(
			snc,
			"_get_metal_items_for_finding",
			return_value=[M_ITEM, "M-G-24KT-99.9-Y"],
		), patch.object(
			snc, "_get_item_purity", side_effect=lambda code: float(code.split("-")[3])
		), patch.object(
			snc, "_find_available_owner_batch", side_effect=[None, hit]
		) as find:
			result = snc.find_owner_metal_for_finding(
				"GJCU0009",
				F_ITEM,
				0.9175,
				company="GEPL",
				allocated=alloc,
				reserve=False,
			)
		self.assertIs(result, hit)
		self.assertEqual(result["qty"], 0.918)
		args, kwargs = find.call_args
		self.assertEqual(args[:4], ("GJCU0009", "M-G-24KT-99.9-Y", 0.918, "GEPL"))
		self.assertIs(kwargs["allocated"], alloc)
		self.assertEqual(kwargs["reserve"], False)

	def test_same_purity_metal_is_taken_one_to_one_by_weight(self):
		# 1.0 g of a 91.75 finding has pure 0.918 after 3-decimal rounding; sizing the
		# 91.75 metal from that would ask for 1.001 g. Same purity must stay exactly 1:1.
		hit = {"batch_no": "CUST-22", "warehouse": "Central RM", "item_code": M_ITEM}
		with patch.object(
			snc, "_get_metal_items_for_finding", return_value=[M_ITEM]
		), patch.object(
			snc, "_get_item_purity", side_effect=lambda code: float(code.split("-")[3])
		), patch.object(snc, "_find_available_owner_batch", return_value=hit) as find:
			result = snc.find_owner_metal_for_finding(
				"GJCU0009", F_ITEM, 0.918, required_qty=1.0, company="GEPL"
			)
		self.assertEqual(find.call_args[0][2], 1.0)
		self.assertEqual(result["qty"], 1.0)

	def test_same_purity_conversion_produces_its_source_weight(self):
		appended = []
		se = _Doc(name="SE-CONV", update=lambda values: None, insert=lambda **kw: None)
		original_get_value = snc.frappe.db.get_value

		def _get_value(doctype, *args, **kwargs):
			if doctype == "Stock Entry Detail":
				return "OUT-F"
			return original_get_value(doctype, *args, **kwargs)

		with patch.object(snc.frappe, "new_doc", return_value=se), patch.object(
			snc, "_append_item", side_effect=lambda doc, values: appended.append(values)
		), patch.object(snc, "_submit_consuming_stock_entry"), patch.object(
			snc, "_get_item_purity", side_effect=lambda code: float(code.split("-")[3])
		), patch.object(snc.frappe.db, "get_value", side_effect=_get_value):
			result = snc.create_repack_metal_conversion(
				mwo=self._mwo(),
				original_transfer=_Doc(company="GEPL", branch="BR"),
				source_batch={
					"item_code": M_ITEM,
					"batch_no": "CUST-M22",
					"qty": 1.0,
					"warehouse": "Central RM",
				},
				required_item_code=F_ITEM,
				required_pure_qty=0.918,
				required_qty=1.0,
				owner_customer="GJCU0009",
			)
		source_row, target_row = appended
		self.assertEqual((source_row["item_code"], source_row["qty"]), (M_ITEM, 1.0))
		self.assertEqual((target_row["item_code"], target_row["qty"]), (F_ITEM, 1.0))
		self.assertEqual(result["target_batch"], "OUT-F")

	# ---- create_snc on finding rows -------------------------------------

	def test_finding_held_by_owner_is_transferred_without_conversion(self):
		# Regular order that used a customer's finding; the company holds the same one.
		received = [_receive_row(F_ITEM, 0.63, "Waxing WO", "GJCU0009", "B-CUST-F")]
		_, patches = self._run(
			self._mwo(),
			received,
			0,
			find_owner_batch={"batch_no": "REG-F", "warehouse": "Central RM"},
		)
		patches["find_owner_metal_for_finding"].assert_not_called()
		patches["create_repack_metal_conversion"].assert_not_called()
		rows = patches["create_material_transfer_work_order"].call_args[0][2]
		self.assertEqual(
			[(r["item_code"], r["batch_no"]) for r in rows], [(F_ITEM, "REG-F")]
		)
		self.assertEqual(rows[0]["t_warehouse"], "Waxing WO")

	def test_finding_not_held_converts_owner_metal_into_it(self):
		# Customer-gold order that used a company finding; the customer holds only metal.
		received = [_receive_row(F_ITEM, 1.0, "Waxing WO", None, "B-REG-F")]
		metal = {
			"batch_no": "CUST-M22",
			"item_code": M_ITEM,
			"warehouse": "Central RM",
			"qty": 1.0,
		}
		result, patches = self._run(
			self._mwo(), received, 1, find_owner_metal_for_finding=metal
		)
		patches["find_owner_rm_warehouse"].assert_not_called()
		finder_kwargs = patches["find_owner_metal_for_finding"].call_args.kwargs
		self.assertEqual(finder_kwargs["required_qty"], 1.0)
		self.assertTrue(finder_kwargs.get("reserve", True))
		self.assertIsInstance(finder_kwargs["allocated"], dict)
		self.assertEqual(finder_kwargs["warehouses"], ["Central RM"])

		owner_conv, usage_conv = (
			c.kwargs for c in patches["create_repack_metal_conversion"].call_args_list
		)
		# Owner: customer's metal -> the finding.
		self.assertEqual(owner_conv["source_batch"]["item_code"], M_ITEM)
		self.assertEqual(owner_conv["required_item_code"], F_ITEM)
		self.assertEqual(owner_conv["owner_customer"], "GJCU0009")
		# Mirror: the borrowed finding -> that metal, back to its owner (Regular Stock).
		self.assertEqual(usage_conv["source_batch"]["item_code"], F_ITEM)
		self.assertEqual(usage_conv["source_batch"]["batch_no"], "B-REG-F")
		self.assertEqual(usage_conv["required_item_code"], M_ITEM)
		self.assertIsNone(usage_conv["owner_customer"])

		rows = patches["create_material_transfer_work_order"].call_args[0][2]
		self.assertEqual(
			[(r["item_code"], r["batch_no"]) for r in rows], [(F_ITEM, "OUT-F")]
		)
		self.assertEqual(result["conversions"], ["SE-CONV", "SE-CONV"])

	def test_metal_and_finding_rows_settle_in_one_receive_and_one_transfer(self):
		received = [
			_receive_row(M_ITEM, 2.0, "Waxing WO", None, "B-REG-M"),
			_receive_row(F_ITEM, 0.5, "Setting WO", None, "B-REG-F"),
		]

		def _owner_batch(owner, item_code, *args, **kwargs):
			return {"batch_no": f"CUST-{item_code[0]}", "warehouse": "Central RM"}

		_, patches = self._run(self._mwo(), received, 1, find_owner_batch=_owner_batch)
		patches["trigger_make_receive"].assert_called_once()
		self.assertEqual(
			_without_target(
				patches["trigger_make_receive"].call_args.kwargs["receive_items"]
			),
			received,
		)
		patches["create_material_transfer_work_order"].assert_called_once()
		rows = patches["create_material_transfer_work_order"].call_args[0][2]
		self.assertEqual(
			[(r["item_code"], r["batch_no"], r["t_warehouse"]) for r in rows],
			[(M_ITEM, "CUST-M", "Waxing WO"), (F_ITEM, "CUST-F", "Setting WO")],
		)


class TestSncHardening(IntegrationTestCase):
	"""Warehouse targeting, replay safety, source warehouses and exact-weight swaps."""

	@classmethod
	def setUpClass(cls):
		pass

	def _mwo(self):
		return _Doc(
			name="MWO-WORK-1",
			manufacturing_order="PMO-0001",
			manufacturing_operation="MOP-1",
			customer="GJCU0009",
			company="GEPL",
		)

	def test_rows_sourced_from_two_warehouses_share_one_receive(self):
		# Metal row: owner holds the same item in Store A. Finding row: owner metal in
		# Store B is converted. The single receive lands each row where its settlement
		# draws from, and the mirror conversion runs in the finding row's own warehouse.
		received = [
			_receive_row(M_ITEM, 2.0, "Waxing WO", None, "B-REG-M"),
			_receive_row(F_ITEM, 1.0, "Setting WO", None, "B-REG-F"),
		]
		metal_source = {
			"batch_no": "CUST-M22",
			"item_code": M_ITEM,
			"warehouse": "Store B",
			"qty": 1.0,
		}
		conversions = []

		def _convert(**kwargs):
			conversions.append(kwargs)
			return {
				"stock_entry": "SE-CONV",
				"target_batch": "OUT-F",
				"warehouse": "Store B",
			}

		with ExitStack() as stack:
			enter = stack.enter_context
			enter(patch.object(snc, "_get_mwo", return_value=self._mwo()))
			enter(patch.object(snc, "validate_button_visibility", return_value=True))
			enter(patch.object(snc, "_is_customer_gold", return_value=1))
			enter(
				patch.object(
					snc,
					"_get_original_material_transfer",
					return_value=_Doc(company="GEPL", branch="BR"),
				)
			)
			enter(patch.object(snc, "_get_receivable_gold_rows", return_value=received))
			enter(patch.object(snc, "_item_template", side_effect=_template_of))
			enter(
				patch.object(
					snc,
					"_get_snc_source_warehouses",
					return_value=["Store A", "Store B"],
				)
			)
			enter(
				patch.object(
					snc,
					"find_owner_batch",
					side_effect=lambda owner, item, *a, **k: (
						{"batch_no": "CUST-M", "warehouse": "Store A"}
						if item == M_ITEM
						else None
					),
				)
			)
			enter(
				patch.object(
					snc, "find_owner_metal_for_finding", return_value=metal_source
				)
			)
			enter(
				patch.object(
					snc, "create_repack_metal_conversion", side_effect=_convert
				)
			)
			receive = enter(
				patch.object(
					snc, "trigger_make_receive", return_value={"docname": "MR-1"}
				)
			)
			transfer = enter(
				patch.object(
					snc, "create_material_transfer_work_order", return_value="MT-1"
				)
			)
			enter(patch.object(snc.frappe.db, "set_value"))
			snc.create_snc(self._mwo())

		receive.assert_called_once()
		targets = [r["t_warehouse"] for r in receive.call_args.kwargs["receive_items"]]
		self.assertEqual(targets, ["Store A", "Store B"])
		# Owner leg and mirror leg both in the finding row's own warehouse.
		self.assertEqual(
			[c["source_batch"]["warehouse"] for c in conversions],
			["Store B", "Store B"],
		)
		rows = transfer.call_args[0][2]
		self.assertEqual(
			[(r["batch_no"], r["s_warehouse"], r["t_warehouse"]) for r in rows],
			[("CUST-M", "Store A", "Waxing WO"), ("OUT-F", "Store B", "Setting WO")],
		)

	def test_replayed_receive_throws_before_any_conversion(self):
		# An earlier receive with the same request id means nothing was received now;
		# converting and transferring on top of it would post stock that never moved.
		mwo = self._mwo()
		received = [_receive_row(F_ITEM, 1.0, "Waxing WO", None, "B-REG-F")]
		with ExitStack() as stack:
			enter = stack.enter_context
			enter(patch.object(snc, "_get_mwo", return_value=mwo))
			enter(patch.object(snc, "validate_button_visibility", return_value=True))
			enter(patch.object(snc, "_is_customer_gold", return_value=1))
			enter(
				patch.object(
					snc,
					"_get_original_material_transfer",
					return_value=_Doc(company="GEPL", branch="BR"),
				)
			)
			enter(patch.object(snc, "_get_receivable_gold_rows", return_value=received))
			enter(patch.object(snc, "_item_template", side_effect=_template_of))
			enter(
				patch.object(
					snc, "_get_snc_source_warehouses", return_value=["Store A"]
				)
			)
			enter(patch.object(snc, "find_owner_batch", return_value=None))
			enter(
				patch.object(
					snc,
					"find_owner_metal_for_finding",
					return_value={
						"batch_no": "CUST-M22",
						"item_code": M_ITEM,
						"warehouse": "Store A",
						"qty": 1.0,
					},
				)
			)
			enter(
				patch.object(
					snc,
					"create_mr_wo_stock_entry",
					return_value={"docname": "MR-OLD", "idempotent": True},
				)
			)
			convert = enter(patch.object(snc, "create_repack_metal_conversion"))
			transfer = enter(patch.object(snc, "create_material_transfer_work_order"))
			set_value = enter(patch.object(snc.frappe.db, "set_value"))
			throw = enter(patch.object(snc.frappe, "throw", side_effect=RuntimeError))
			with self.assertRaises(RuntimeError):
				snc.create_snc(mwo)
		self.assertIn("MR-OLD", throw.call_args[0][0])
		convert.assert_not_called()
		transfer.assert_not_called()
		set_value.assert_not_called()

	def test_request_id_is_keyed_on_the_rows_received(self):
		mwo = self._mwo()
		rows = [
			{
				"stock_reservation_entry": "SRE-1",
				"stock_reservation_entry_detail": "D1",
				"batch_no": "B1",
				"t_warehouse": "Store A",
				"qty": 1.0,
			},
			{
				"stock_reservation_entry": "SRE-2",
				"stock_reservation_entry_detail": "D2",
				"batch_no": "B2",
				"t_warehouse": "Store B",
				"qty": 0.5,
			},
		]
		first = snc._snc_request_id(mwo, "Store A", rows)
		self.assertTrue(first.startswith("SNC-"))
		self.assertEqual(len(first), 14)
		# Same rows in another order: same key (a double click still replays).
		self.assertEqual(
			first, snc._snc_request_id(mwo, "Store A", list(reversed(rows)))
		)
		# A re-opened settlement receives different rows: a new key.
		changed = [dict(rows[0], stock_reservation_entry_detail="D9"), rows[1]]
		self.assertNotEqual(first, snc._snc_request_id(mwo, "Store A", changed))
		self.assertNotEqual(
			first,
			snc._snc_request_id(mwo, "Store A", [dict(rows[0], qty=0.9), rows[1]]),
		)

	def test_source_warehouses_exclude_karigar_and_subcontractor(self):
		with patch.object(snc.frappe, "get_all", return_value=["Store A"]) as get_all:
			self.assertEqual(snc._get_snc_source_warehouses("GEPL"), ["Store A"])
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["warehouse_type"], "Raw Material")
		self.assertEqual(filters["employee"], ["is", "not set"])
		self.assertEqual(filters["subcontractor"], ["is", "not set"])
		self.assertEqual(filters["company"], "GEPL")

	def _convert(
		self,
		source_item,
		source_qty,
		required_item,
		required_pure_qty,
		required_qty,
		on_submit=None,
	):
		appended = []
		se = _Doc(name="SE-CONV", update=lambda values: None, insert=lambda **kw: None)
		original_get_value = snc.frappe.db.get_value

		def _get_value(doctype, *args, **kwargs):
			if doctype == "Stock Entry Detail":
				return "OUT-B"
			return original_get_value(doctype, *args, **kwargs)

		with patch.object(snc.frappe, "new_doc", return_value=se), patch.object(
			snc, "_append_item", side_effect=lambda doc, values: appended.append(values)
		), patch.object(
			snc, "_submit_consuming_stock_entry", side_effect=on_submit
		), patch.object(
			snc, "_get_item_purity", side_effect=lambda code: float(code.split("-")[3])
		), patch.object(snc.frappe.db, "get_value", side_effect=_get_value):
			snc.create_repack_metal_conversion(
				mwo=self._mwo(),
				original_transfer=_Doc(company="GEPL", branch="BR"),
				source_batch={
					"item_code": source_item,
					"batch_no": "SRC",
					"qty": source_qty,
					"warehouse": "Store A",
				},
				required_item_code=required_item,
				required_pure_qty=required_pure_qty,
				required_qty=required_qty,
				owner_customer="GJCU0009",
			)
		return appended

	def _minted_batch(self):
		"""The customer batch the SE-CONV conversion mints, as the guard sees it."""
		return SimpleNamespace(
			reference_doctype="Stock Entry",
			reference_name="SE-CONV",
			custom_customer="C1",
		)

	def test_conversion_is_marked_only_while_it_submits(self):
		# The Customer Goods exemption must hold for exactly SNC's own submit of this
		# entry -- the only time the guard runs on the batch it mints -- and not after.
		snc.frappe.flags.snc_settlement_conversions = None
		self.addCleanup(setattr, snc.frappe.flags, "snc_settlement_conversions", None)
		during = []
		self._convert(
			M_ITEM,
			1.0,
			F_ITEM,
			0.918,
			1.0,
			on_submit=lambda se: during.append(
				batch_utils.is_snc_settlement_conversion(self._minted_batch())
			),
		)
		self.assertEqual(during, [True])
		self.assertFalse(batch_utils.is_snc_settlement_conversion(self._minted_batch()))

	def test_conversion_mark_is_cleared_when_the_submit_fails(self):
		snc.frappe.flags.snc_settlement_conversions = None
		self.addCleanup(setattr, snc.frappe.flags, "snc_settlement_conversions", None)

		def _fail(se):
			raise RuntimeError("submit failed")

		with self.assertRaises(RuntimeError):
			self._convert(M_ITEM, 1.0, F_ITEM, 0.918, 1.0, on_submit=_fail)
		self.assertFalse(batch_utils.is_snc_settlement_conversion(self._minted_batch()))

	def test_different_purity_conversion_produces_exactly_the_required_weight(self):
		# 1.0 g of a 91.75 finding: pure 0.918 -> 0.919 g of 99.9 metal. Re-deriving the
		# target from the 3-dp pure qty gives 1.001 here and 0.999 for other weights; the
		# owner leg must produce exactly what the transfer sends.
		_, target = self._convert("M-G-24KT-99.9-Y", 0.919, F_ITEM, 0.918, 1.0)
		self.assertEqual((target["item_code"], target["qty"]), (F_ITEM, 1.0))
		# Mirror leg: the borrowed finding back into exactly the metal the owner spent.
		_, mirror = self._convert(F_ITEM, 1.0, "M-G-24KT-99.9-Y", 0.918, 0.919)
		self.assertEqual(
			(mirror["item_code"], mirror["qty"]), ("M-G-24KT-99.9-Y", 0.919)
		)

	def test_no_owner_stock_message_names_the_row_and_finding_rule(self):
		with patch.object(snc, "_item_template", side_effect=_template_of):
			finding = snc._no_owner_stock_message(
				"GJCU0009", _receive_row(F_ITEM, 1.25, "Waxing WO", None, "B")
			)
			metal = snc._no_owner_stock_message(
				None, _receive_row(M_ITEM, 2.0, "W", None, "B")
			)
		self.assertIn(F_ITEM, finding)
		self.assertIn("1.25", finding)
		self.assertIn("Customer GJCU0009", finding)
		self.assertIn("same metal type and colour", finding)
		self.assertIn("Regular Stock", metal)
		self.assertNotIn("same metal type and colour", metal)


# ---- D6: the conversion lane on SNC's Repack-Metal Conversion -----------

#: MAT-STE-19254 (kg-gk, 2026-09-28) is SNC's owner leg: 1.183 g of GJCU0009's 22KT receipt
#: batch -04 converted into the finding the operation had borrowed, minted as batch "-04-A".
#: It carried no ``custom_conversion_lane``, so the custody dispatcher wrote no Customer Gold
#: event for either row and logged each one as an unclassified one-sided movement. Its mirror
#: leg, MAT-STE-19255, is Regular Stock and rightly writes nothing.
SNC_OWNER = "GJCU0009"
SNC_SOURCE_BATCH = "GJCU0009-2F09-M-G-22KT-91.75-Y-04"
SNC_QTY = 1.183
#: The lane Settle and Metal Conversions stamp for one owner, written out by hand.
SNC_LANE = "Customer Goods|GJCU0009"
#: Batch -04's child pool for the finding: owner, -04's year-month, the finding, -04's serial.
SNC_CHILD_BASE = f"GJCU0009-2F09-{F_ITEM}-04"
#: The mirror leg's source: the borrowed company finding, named by Batch.autoname.
REGULAR_FINDING_BATCH = "GE2F092-FG22KT9175Y-7H3KQ"
UNCLASSIFIED_MOVEMENT = "Customer Gold: unclassified one-sided movement"
RECEIPT_MODULE = "jewellery_erpnext.customer_subcontracting.customer_gold_receipt"


class _BuiltEntry(_Doc):
	"""The Stock Entry ``create_repack_metal_conversion`` builds, held in memory.

	The header arrives through ``update`` and each row through the real ``_append_item``, so
	the rows are exactly what the builder appends. ``insert`` saves nothing.
	"""

	def __init__(self, **fields):
		super().__init__(items=[], **fields)

	def update(self, values):
		vars(self).update(values)

	def append(self, fieldname, value=None):
		rows = getattr(self, fieldname)
		row = frappe._dict(
			value or {}, name=f"SNC-ROW-{len(rows) + 1}", idx=len(rows) + 1
		)
		rows.append(row)
		return row

	def insert(self, **kwargs):
		return self


def _snapshot(entry):
	"""``entry`` and its rows, copied."""
	header = {key: value for key, value in vars(entry).items() if key != "items"}
	copied = _BuiltEntry(**header)
	copied.items = [frappe._dict(row) for row in entry.items]
	return copied


def _snc_row(**fields):
	"""A row as SNC's ``_append_item`` leaves it, before any lane."""
	row = {"use_serial_batch_fields": 1, "allow_zero_valuation_rate": 1, "pcs": 1}
	row.update(fields)
	return row


class TestSncConversionCarriesTheLane(lane_downstream._BatchTableCase):
	"""D6: SNC's conversion of a customer's metal is tagged with the customer's lane.

	``create_repack_metal_conversion`` books a "Repack-Metal Conversion", like Settle and Metal
	Conversions, and the custody dispatcher writes Conversion Out / In only for a row that carries
	a lane. So the builder stamps ``lane_tag(inventory_type, owner)`` on both rows, exactly as
	``cg_settle._convert_multi`` does. It runs for real here; its save and submit are held in
	memory, and the submit runs the real child-batch allocator against an in-memory ``tabBatch``
	(``_BatchTableCase``), as ``create_child_batches`` does in ``before_submit``.
	"""

	def _build(
		self,
		owner,
		source_batch=SNC_SOURCE_BATCH,
		source_item=M_ITEM,
		target_item=F_ITEM,
		existing=(),
	):
		"""Run the real builder; return the entry, the entry as it was submitted, what the
		allocator minted and the builder's result.

		A produced row the allocator leaves alone (company metal) is named the way the
		Serial-and-Batch path would, so the builder can read its target batch back.
		"""
		entry = _BuiltEntry(doctype="Stock Entry", name="SE-SNC-CONV")
		built = SimpleNamespace(entry=entry, submitted=None, minted=[], result=None)
		real_new_doc = frappe.new_doc
		real_get_value = frappe.db.get_value

		def _new_doc(doctype, *args, **kwargs):
			if doctype != "Stock Entry":
				return real_new_doc(doctype, *args, **kwargs)
			return entry

		def _submit(se):
			built.submitted = _snapshot(se)
			built.minted = self._mint(se, existing)
			for row in se.items:
				if row.get("t_warehouse") and not row.get("batch_no"):
					row.batch_no = f"SABB-{row.name}"

		def _get_value(doctype, *args, **kwargs):
			if doctype != "Stock Entry Detail":
				return real_get_value(doctype, *args, **kwargs)
			filters = args[0]
			return next(
				(
					row.batch_no
					for row in entry.items
					if row.item_code == filters["item_code"]
					and row.get("t_warehouse") == filters["t_warehouse"]
				),
				None,
			)

		with ExitStack() as stack:
			enter = stack.enter_context
			enter(patch.object(snc.frappe, "new_doc", side_effect=_new_doc))
			enter(
				patch.object(snc, "_submit_consuming_stock_entry", side_effect=_submit)
			)
			enter(
				patch.object(
					snc,
					"_get_item_purity",
					side_effect=lambda code: float(code.split("-")[3]),
				)
			)
			enter(patch.object(snc.frappe.db, "get_value", side_effect=_get_value))
			built.result = snc.create_repack_metal_conversion(
				mwo=_Doc(
					name="MWO-WORK-1",
					manufacturing_order="PMO-0001",
					manufacturing_operation="MOP-1",
				),
				original_transfer=_Doc(company="GEPL", branch="BR"),
				source_batch={
					"item_code": source_item,
					"batch_no": source_batch,
					"qty": SNC_QTY,
					"warehouse": "Store A",
				},
				required_item_code=target_item,
				required_pure_qty=1.085,
				required_qty=SNC_QTY,
				owner_customer=owner,
			)
		return built

	def _build_regular(self):
		"""The mirror leg: the borrowed company finding back into company metal."""
		return self._build(
			None,
			source_batch=REGULAR_FINDING_BATCH,
			source_item=F_ITEM,
			target_item=M_ITEM,
		)

	def _dispatch(self, entry, owners):
		"""Run ``record_stock_movement``, the on_submit custody dispatcher, over ``entry``.

		Returns ``(events written, errors logged)``. The settings gates, the purity masters and
		the two writers are replaced; the real ``_batch_owner`` resolves ownership from a Batch
		table answering ``owners`` (``{batch: (inventory type, customer)}``) and handing every
		other read to the real ``get_value``.
		"""
		events, errors = [], []
		real_get_value = frappe.db.get_value

		def _get_value(doctype, *args, **kwargs):
			if doctype != "Batch":
				return real_get_value(doctype, *args, **kwargs)
			if args[0] not in owners:
				return None
			inventory_type, customer = owners[args[0]]
			return frappe._dict(
				custom_inventory_type=inventory_type, custom_customer=customer
			)

		with ExitStack() as stack:
			enter = stack.enter_context
			enter(patch.object(cgf, "is_customer_gold_enabled", return_value=True))
			enter(patch.object(cgf, "is_ledger_schema_ready", return_value=True))
			enter(patch(f"{RECEIPT_MODULE}._receipt_settings", return_value=None))
			enter(patch.object(cgf, "_is_customer_gold_return", return_value=False))
			enter(
				patch.object(
					cgf,
					"get_customer_gold_valuation_policy",
					return_value=VALUATION_ZERO,
				)
			)
			enter(
				patch.object(
					cgf,
					"get_purity_percentage",
					side_effect=lambda code: float(code.split("-")[3]),
				)
			)
			enter(patch.object(cgf, "reference_purity", return_value=100.0))
			enter(patch.object(cgf.frappe.db, "get_value", side_effect=_get_value))
			enter(
				patch.object(
					cgf,
					"_write_event",
					side_effect=lambda **event: events.append(event),
				)
			)
			enter(
				patch.object(
					cgf.frappe,
					"log_error",
					side_effect=lambda **error: errors.append(error),
				)
			)
			cgf.record_stock_movement(entry)
		return events, errors

	def test_a_customer_conversion_stamps_the_owner_lane_on_both_rows(self):
		"""Both rows carry "Customer Goods|<owner>", and nothing else on them changes."""
		built = self._build(SNC_OWNER)
		rows = built.submitted.items

		self.assertEqual(
			[row.get("custom_conversion_lane") for row in rows], [SNC_LANE, SNC_LANE]
		)
		owned = {"inventory_type": "Customer Goods", "customer": SNC_OWNER}
		self.assertEqual(
			[
				{k: v for k, v in row.items() if k != "custom_conversion_lane"}
				for row in rows
			],
			[
				_snc_row(
					name="SNC-ROW-1",
					idx=1,
					item_code=M_ITEM,
					qty=SNC_QTY,
					batch_no=SNC_SOURCE_BATCH,
					s_warehouse="Store A",
					**owned,
				),
				_snc_row(
					name="SNC-ROW-2",
					idx=2,
					item_code=F_ITEM,
					qty=SNC_QTY,
					t_warehouse="Store A",
					**owned,
				),
			],
		)

	def test_a_regular_stock_conversion_is_built_exactly_as_before(self):
		"""Pin. The mirror leg of company metal gets no lane: its rows are unchanged."""
		built = self._build_regular()
		regular = {"inventory_type": "Regular Stock", "customer": None}

		self.assertEqual(built.minted, [])
		self.assertEqual(
			[dict(row) for row in built.submitted.items],
			[
				_snc_row(
					name="SNC-ROW-1",
					idx=1,
					item_code=F_ITEM,
					qty=SNC_QTY,
					batch_no=REGULAR_FINDING_BATCH,
					s_warehouse="Store A",
					**regular,
				),
				_snc_row(
					name="SNC-ROW-2",
					idx=2,
					item_code=M_ITEM,
					qty=SNC_QTY,
					t_warehouse="Store A",
					**regular,
				),
			],
		)

	def test_the_lane_leaves_the_child_batch_name_unchanged(self):
		"""Pin. With the lane or without it, the conversion's output is minted from the source
		batch, for the owner, under the same name: "-04-A" for a fresh pool, "-04-B" after an
		existing child. One source row and one output share one lane, so the per-row path finds
		the same parent the single-ownership path took."""
		for existing in ((), (f"{SNC_CHILD_BASE}-A",)):
			with self.subTest(existing=existing):
				built = self._build(SNC_OWNER, existing=existing)
				minted = {}
				for label, lane in (("no lane", None), ("lane", SNC_LANE)):
					entry = _snapshot(built.submitted)
					for row in entry.items:
						row.pop("custom_conversion_lane", None)
						if lane:
							row.custom_conversion_lane = lane
					minted[label] = [
						(
							batch.batch_id,
							batch.custom_customer,
							batch.custom_inventory_type,
							batch.custom_voucher_detail_no,
						)
						for batch in self._mint(entry, existing)
					]

				expected = f"{SNC_CHILD_BASE}-{'B' if existing else 'A'}"
				self.assertEqual(
					minted["no lane"],
					[(expected, SNC_OWNER, "Customer Goods", "SNC-ROW-2")],
				)
				self.assertEqual(minted["lane"], minted["no lane"])
				self.assertEqual(built.result["target_batch"], expected)

	def test_a_customer_conversion_writes_one_conversion_out_and_one_conversion_in(
		self,
	):
		"""(c) Conversion Out on the owner's source row, Conversion In on the produced row, both
		at 91.75%: fine is 1.183 x 0.9175 = 1.0854025 -> 1.085 g each way, and no carrying value
		moves. Nothing is logged as an unclassified movement."""
		built = self._build(SNC_OWNER)
		owners = {SNC_SOURCE_BATCH: ("Customer Goods", SNC_OWNER)}
		owners.update(
			{
				batch.batch_id: (batch.custom_inventory_type, batch.custom_customer)
				for batch in built.minted
			}
		)

		events, errors = self._dispatch(built.entry, owners)

		self.assertEqual(
			[
				(
					event["cg_event_kind"],
					event["cg_source_row"],
					event["batch_no"],
					event["customer"],
					event["item_code"],
					round(event["cg_gross_qty_delta"], 6),
					round(event["cg_fine_gold_delta"], 6),
					event["cg_carrying_value_delta"],
					event["cg_stage"],
				)
				for event in events
			],
			[
				(
					"Conversion Out",
					"SNC-ROW-1",
					SNC_SOURCE_BATCH,
					SNC_OWNER,
					M_ITEM,
					-1.183,
					-1.085,
					None,
					"RM",
				),
				(
					"Conversion In",
					"SNC-ROW-2",
					f"{SNC_CHILD_BASE}-A",
					SNC_OWNER,
					F_ITEM,
					1.183,
					1.085,
					None,
					"RM",
				),
			],
			f"errors logged instead: {errors}",
		)
		self.assertEqual(errors, [])

	def test_a_regular_stock_conversion_writes_no_custody_event(self):
		"""Pin. Company metal on both rows: no event and no diagnostic."""
		built = self._build_regular()
		owners = {
			REGULAR_FINDING_BATCH: ("Regular Stock", None),
			built.result["target_batch"]: ("Regular Stock", None),
		}

		self.assertEqual(self._dispatch(built.entry, owners), ([], []))


class TestSncConversionLaneIntegration(mc_isolation._MetalConversionCase):
	"""INTEGRATION. D6 through the real builder, on real documents.

	Skips unless the site sets ``customer_gold_disposable_site`` (the base ``setUpClass``
	guard). The Metal Conversion fixture supplies the masters, the Nominal policy and the
	"Repack-Metal Conversion" type, created inside the class transaction. One conversion is
	booked for the class and read from every angle: 3.67 g of the customer's 100% metal into
	4.000 g at 91.75% (3.67 / 0.9175 = 4 exactly, so both hold at float precision 2). The
	child-batch allocator, the ledger posting and the custody dispatcher all run for real. Only
	SNC's purity lookup is pointed at the app's purity master: it parses the Metal Purity value as
	a number, and the fixture's isolated ``CG-TEST-`` purities are names.
	"""

	SOURCE_QTY = 3.67
	TARGET_QTY = 4.0

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.wh = cls._fresh_warehouse()
		cls.batch, _rate = cls._customer_batch(cls.wh, 5.0)
		with patch.object(snc, "_get_item_purity", side_effect=get_purity_percentage):
			cls.result = snc.create_repack_metal_conversion(
				mwo=frappe._dict(
					name=None, manufacturing_order=None, manufacturing_operation=None
				),
				original_transfer=frappe._dict(
					company=mc_isolation.COMPANY, branch=None
				),
				source_batch={
					"item_code": mc_isolation.SOURCE_ITEM,
					"batch_no": cls.batch,
					"qty": cls.SOURCE_QTY,
					"warehouse": cls.wh,
				},
				required_item_code=mc_isolation.TARGET_ITEM,
				required_pure_qty=cls.SOURCE_QTY,
				required_qty=cls.TARGET_QTY,
				owner_customer=mc_isolation.CUSTOMER,
			)
		cls.se = frappe.get_doc("Stock Entry", cls.result["stock_entry"])

	def _events(self):
		return frappe.get_all(
			"Customer Gold Ledger Entry",
			filters={"reference_docname": self.se.name},
			fields=[
				"cg_event_kind",
				"batch_no",
				"customer",
				"cg_gross_qty_delta",
				"cg_fine_gold_delta",
				"cg_carrying_value_delta",
				"cg_stage",
			],
			order_by="cg_event_kind desc",
		)

	def test_both_rows_carry_the_customer_lane(self):
		lane = f"Customer Goods|{mc_isolation.CUSTOMER}"
		self.assertEqual(
			[(row.item_code, row.custom_conversion_lane) for row in self.se.items],
			[(mc_isolation.SOURCE_ITEM, lane), (mc_isolation.TARGET_ITEM, lane)],
		)

	def test_the_child_batch_is_named_from_the_receipt_batch(self):
		"""Pin. Owner, the receipt's year-month, the target item, the receipt's serial, "A"."""
		owner = f"{mc_isolation.CUSTOMER}-"
		self.assertTrue(self.batch.startswith(owner), self.batch)
		year_month, _, rest = self.batch[len(owner) :].partition("-")
		self.assertTrue(rest.startswith(f"{mc_isolation.SOURCE_ITEM}-"), self.batch)
		serial = rest[len(mc_isolation.SOURCE_ITEM) + 1 :]

		child = f"{owner}{year_month}-{mc_isolation.TARGET_ITEM}-{serial}-A"
		self.assertEqual(self.result["target_batch"], child)
		self.assertEqual(
			frappe.db.get_value(
				"Batch", child, ["custom_customer", "custom_inventory_type"]
			),
			(mc_isolation.CUSTOMER, "Customer Goods"),
		)

	def test_the_child_keeps_its_provenance_and_value(self):
		"""Pin. The lane also scopes the child's origin entries and Batch Component
		(``update_parent_batch_id``); on SNC's one-source, one-output entry that scope is the
		whole entry, so the child still records its one source at the full 3.67 g. The lane
		pricer does not read the tag: the child carries exactly the value the source gave up."""
		child = self.result["target_batch"]
		self.assertEqual(self._origin(child), {self.batch: 3.67})
		self.assertEqual(
			[
				(row.source_batch, row.customer, row.inventory_type, flt(row.qty, 3))
				for row in self._components(child)
			],
			[(self.batch, mc_isolation.CUSTOMER, "Customer Goods", 3.67)],
		)
		consumed, produced = (self._sle(self.se, row) for row in self.se.items)
		self.assertAlmostEqual(
			flt(produced.stock_value_difference),
			-flt(consumed.stock_value_difference),
			places=2,
		)
		self.assertAlmostEqual(flt(self.se.value_difference), 0.0, places=2)

	def test_the_ledger_gets_one_conversion_out_and_one_conversion_in(self):
		"""Out: -3.67 g at 100% = -3.67 fine, on the receipt batch. In: +4.000 g at 91.75% =
		3.67 fine, on the child. No carrying value moves; both stay at the RM stage."""
		self.assertEqual(
			[
				(
					event.cg_event_kind,
					event.batch_no,
					event.customer,
					flt(event.cg_gross_qty_delta, 3),
					flt(event.cg_fine_gold_delta, 3),
					flt(event.cg_carrying_value_delta, 2),
					event.cg_stage,
				)
				for event in self._events()
			],
			[
				(
					"Conversion Out",
					self.batch,
					mc_isolation.CUSTOMER,
					-3.67,
					-3.67,
					0.0,
					"RM",
				),
				(
					"Conversion In",
					self.result["target_batch"],
					mc_isolation.CUSTOMER,
					4.0,
					3.67,
					0.0,
					"RM",
				),
			],
		)

	def test_nothing_is_logged_as_an_unclassified_movement(self):
		"""Matched on this entry's row names, which are random. ``tabError Log`` is MyISAM, so a
		log survives the class rollback while the entry does not, and the rolled-back naming
		series can hand the entry's name out again."""
		logged = [
			name
			for row in self.se.items
			for name in frappe.get_all(
				"Error Log",
				filters={
					"method": UNCLASSIFIED_MOVEMENT,
					"error": ["like", f"%row {row.name} %"],
				},
				pluck="name",
			)
		]
		self.assertEqual(logged, [])
