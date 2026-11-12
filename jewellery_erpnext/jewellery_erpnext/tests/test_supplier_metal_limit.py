# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Supplier-wise pure metal weight limit (``Supplier.custom_metal_weight_limit``).

Pure-logic: a fake world of suppliers / warehouses / Bins / unsynced MOP Logs behind keyed stubs.
Every read the guard makes is answered here; an unexpected SQL statement fails the test, and every
other ``frappe.db.get_value`` / ``frappe.get_all`` call is forwarded verbatim. Nothing is written.

The spec's own example is the base holding of supplier SUP-A: 60 g @ 90 + 40 g @ 80 + 20 g @ 70 in
three linked warehouses = 120 g metal, 100 g pure, limit 100 -- the supplier is exactly at its limit.
"""

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import flt

from jewellery_erpnext import hooks
from jewellery_erpnext.jewellery_erpnext.doc_events import stock_entry as se_events
from jewellery_erpnext.jewellery_erpnext.doc_events import supplier_metal_limit as sml

MOP_LOG = "jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log"

SUPPLIERS = {
	"SUP-A": {"custom_metal_weight_limit": 100, "supplier_name": "Aarya Jewels"},
	"SUP-B": {"custom_metal_weight_limit": 0, "supplier_name": "No Limit Jewels"},
	"SUP-C": {"custom_metal_weight_limit": 50, "supplier_name": "Sadguru Hallmarking"},
}

WAREHOUSES = {
	"A-MSL": "SUP-A",
	"A-WIP": "SUP-A",
	"A-HM": "SUP-A",
	"B-WIP": "SUP-B",
	"C-WIP": "SUP-C",
	"DEPT": None,
	"STORE": None,
}

# item -> (variant_of, purity_percentage, Metal Purity attribute value)
ITEMS = {
	"M-90": ("M", 90, "90"),
	"F-80": ("F", 80, "80"),
	"ML-70": ("ML", 70, "70"),
	"M-9175": ("M", 91.75, "91.75"),
	"M-24KT": ("M", 100, "99.9"),  # purity_percentage maps 99.9 -> 100 (24KT is pure)
	"M-18KT": ("M", 0, "75.15"),  # purity_percentage stored as 0 on kggk-prod
	"FL-9175": ("FL", 91.75, "91.75"),
	"D-STONE": ("D", None, None),
	"MU00062-009": ("MU00062", None, None),  # design / FG template, starts with "M"
	"REF-CF-001": (None, None, None),  # refining scrap, no purity
	"ALLOY": ("M", None, None),  # metal family but no Metal Purity -> does not count
}

BASE_BINS = {
	("M-90", "A-MSL"): 60,
	("F-80", "A-WIP"): 40,
	("ML-70", "A-HM"): 20,
	("D-STONE", "A-WIP"): 5,
	("MU00062-009", "A-WIP"): 1,
	("M-9175", "C-WIP"): 50,  # 45.875 g pure
	("M-90", "B-WIP"): 500,
	("M-90", "STORE"): 10000,
}


def _bin_name(item_code, warehouse):
	return f"BIN::{item_code}::{warehouse}"


class _Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


def _row(item_code, qty, s_warehouse=None, t_warehouse=None):
	return _Doc(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		conversion_factor=1,
		s_warehouse=s_warehouse,
		t_warehouse=t_warehouse,
	)


def _se(*rows, name="SE-TEST"):
	return _Doc(name=name, items=list(rows), flags=frappe._dict())


def _system_settings(key=None, *args, **kwargs):
	return {"rounding_method": "Banker's Rounding", "time_zone": "Asia/Kolkata"}.get(
		key
	)


class _World(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Warm the translation lookup before anything is stubbed (its first read goes through
		# frappe.get_all under suppress(); a stub there would poison it for later modules).
		frappe._("Supplier Metal Weight Limit Exceeded")

	def setUp(self):
		self.suppliers = {k: dict(v) for k, v in SUPPLIERS.items()}
		self.bins = dict(BASE_BINS)
		self.stale_bins = None  # what a plain (snapshot) read returns, when it differs
		self.logs = []  # unsynced, non-cancelled MOP Logs
		self.mop_balances = {}  # manufacturing_operation -> [balance rows]
		self.sql_calls = []
		self.supplier_locks = []
		self.supplier_busy = False
		# a Bin another transaction is writing right now (the NOWAIT share read fails)
		self.bins_busy = False
		self.busy_bins = set()  # Bin names whose primary-key share read fails right now
		self.seq_created = []  # counter rows the guard INSERT IGNOREd
		self.seq_insert_busy = (
			False  # that insert meets a lock (zero lock wait -> timeout)
		)
		# issue counters (tabSeries key -> current): as this transaction's snapshot sees them, and
		# as committed now (the locking read); None = same as the snapshot
		self.seq_snapshot = {}
		self.seq_latest = None
		self.seq_bumps = []
		self.field_exists = True
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		real_get_value = frappe.db.get_value
		real_get_all = frappe.get_all

		def get_value(*args, **kwargs):
			if args and args[0] == "Item Variant Attribute":
				filters = args[1]
				if filters.get("attribute") == "Metal Purity":
					return (ITEMS.get(filters["parent"]) or (None, None, None))[2]
			return real_get_value(*args, **kwargs)

		def get_all(*args, **kwargs):
			if args and args[0] == "Warehouse":
				wanted = set(kwargs["filters"]["subcontractor"][1])
				return [
					frappe._dict(name=wh, subcontractor=sup)
					for wh, sup in WAREHOUSES.items()
					if sup in wanted
				]
			return real_get_all(*args, **kwargs)

		for stub in (
			patch.object(sml, "bulk_map", side_effect=self._bulk_map),
			patch.object(sml, "get_purity_percentage", side_effect=self._purity),
			patch.object(sml, "prefetch_purity_percentages"),
			patch.object(
				sml, "_limit_field_exists", side_effect=lambda: self.field_exists
			),
			patch.object(sml, "_bin_warehouse_index", return_value="warehouse"),
			patch("frappe.db.sql", side_effect=self._sql),
			patch("frappe.db.get_value", side_effect=get_value),
			patch.object(frappe, "get_all", side_effect=get_all),
			patch.object(frappe, "get_system_settings", side_effect=_system_settings),
			patch(
				f"{MOP_LOG}.get_current_mop_balance_rows", side_effect=self._balances
			),
			patch(
				f"{MOP_LOG}.drop_pre_refining_rows", side_effect=lambda rows, mwo: rows
			),
		):
			self.stack.enter_context(stub)
		self.stack.enter_context(
			patch.dict(frappe.local.flags, {"in_eod_mop_sync": False})
		)
		self.assertEqual(
			flt(1.0, 3), 1.0, "flt(x, 3) is zeroing -- the harness cannot fail"
		)

	# ------------------------------------------------------------------ stubs
	def _bulk_map(self, doctype, names, fields):
		names = [n for n in names if n]
		if doctype == "Warehouse":
			return {
				n: {"subcontractor": WAREHOUSES.get(n)}
				for n in names
				if n in WAREHOUSES
			}
		if doctype == "Item":
			return {n: {"variant_of": ITEMS[n][0]} for n in names if n in ITEMS}
		if doctype == "Supplier":
			return {n: dict(self.suppliers[n]) for n in names if n in self.suppliers}
		raise AssertionError(f"unexpected bulk_map({doctype})")

	@staticmethod
	def _purity(item_code):
		return (ITEMS.get(item_code) or (None, None, None))[1]

	def _balances(self, mop, include_fields=None):
		return [frappe._dict(r) for r in self.mop_balances.get(mop, [])]

	def _sql(self, query, values=None, as_dict=False, **kwargs):
		q = " ".join(query.split())
		self.sql_calls.append(q)
		values = values or {}
		if "FROM `tabSupplier`" in q:
			if self.supplier_busy:
				raise frappe.QueryTimeoutError("Lock wait timeout exceeded")
			self.supplier_locks.append((tuple(values["suppliers"]), "NOWAIT" in q))
			return [(s,) for s in values["suppliers"]]
		if "FROM `tabSeries`" in q or "INTO `tabSeries`" in q:
			key = values[0]
			if "INSERT IGNORE" in q:
				if self.seq_insert_busy:
					self.assertIn("SET STATEMENT innodb_lock_wait_timeout=0 FOR", q)
					raise frappe.QueryTimeoutError("Lock wait timeout exceeded")
				self.seq_created.append(key)
				return ()
			if "INSERT INTO" in q:
				self.assertIn("ON DUPLICATE KEY UPDATE", q)
				self.seq_bumps.append(key)
				return ()
			seen = self.seq_snapshot
			if "FOR UPDATE" in q:
				seen = (
					self.seq_latest
					if self.seq_latest is not None
					else self.seq_snapshot
				)
				if key not in seen:
					# never locked while missing: the INSERT IGNORE must have created it
					self.assertIn(key, self.seq_created)
					return ((0,),)
			return ((seen[key],),) if key in seen else ()
		if q.startswith("SELECT name FROM `tabBin` FORCE INDEX (`warehouse`)"):
			self.assertIn("LOCK IN SHARE MODE NOWAIT", q)
			if self.bins_busy:
				raise frappe.QueryTimeoutError("Lock wait timeout exceeded")
			return [
				(_bin_name(item, wh),)
				for (item, wh) in sorted(self.bins)
				if wh in values["warehouses"]
			]
		if q.startswith(
			"SELECT b.name, i.variant_of FROM `tabBin` b LEFT JOIN `tabItem`"
		):
			self.assertNotIn("LOCK", q)
			bins = self.stale_bins if self.stale_bins is not None else self.bins
			return [
				(_bin_name(item, wh), ITEMS[item][0])
				for (item, wh) in sorted(bins)
				if _bin_name(item, wh) in values["names"]
			]
		if "WHERE name IN %(names)s LOCK IN SHARE MODE NOWAIT" in q:
			if self.busy_bins & set(values["names"]):
				raise frappe.QueryTimeoutError("Lock wait timeout exceeded")
			self.share_locked = sorted(values["names"])
			return [
				frappe._dict(item_code=item, warehouse=wh, actual_qty=qty)
				for (item, wh), qty in sorted(self.bins.items())
				if _bin_name(item, wh) in values["names"]
			]
		if q.startswith("SELECT b.item_code, b.warehouse FROM `tabBin`"):
			return [
				(item, wh)
				for (item, wh) in sorted(self.bins)
				if wh in values["warehouses"] and ITEMS[item][0] in values["families"]
			]
		if "WHERE name IN %(names)s" in q:
			self.assertIn("FOR UPDATE", q)
			return [
				frappe._dict(item_code=item, warehouse=wh, actual_qty=qty)
				for (item, wh), qty in sorted(self.bins.items())
				if _bin_name(item, wh) in values["names"] and wh in values["warehouses"]
			]
		if "b.actual_qty != 0" in q:
			bins = self.stale_bins if self.stale_bins is not None else self.bins
			return [
				frappe._dict(item_code=item, warehouse=wh, actual_qty=qty)
				for (item, wh), qty in sorted(bins.items())
				if wh in values["warehouses"]
				and ITEMS[item][0] in values["families"]
				and qty
			]
		if q.startswith("SELECT DISTINCT manufacturing_work_order FROM `tabMOP Log`"):
			whs = set(values["warehouses"])
			return [
				(mwo,)
				for mwo in sorted(
					{
						log.manufacturing_work_order
						for log in self.logs
						if log.to_warehouse in whs or log.from_warehouse in whs
					}
				)
			]
		if "manufacturing_work_order IN %(mwos)s" in q:
			return [
				frappe._dict(log)
				for log in sorted(self.logs, key=lambda log: (log.creation, log.name))
				if log.manufacturing_work_order in values["mwos"]
			]
		raise AssertionError(f"unexpected SQL: {q}")

	# ------------------------------------------------------------------ helpers
	def _log(self, mwo, mop, item_code, qty, from_wh, to_wh, creation, batch_no="B1"):
		self.logs.append(
			frappe._dict(
				name=f"LOG-{len(self.logs):04d}",
				manufacturing_work_order=mwo,
				manufacturing_operation=mop,
				item_code=item_code,
				batch_no=batch_no,
				from_warehouse=from_wh,
				to_warehouse=to_wh,
				qty_after_transaction_batch_based=qty,
				creation=creation,
			)
		)

	def _submit(self, doc):
		"""The Stock Entry before_submit chain's three relevant steps, prelock_bins in between."""
		sml.lock_supplier_limits(doc)
		pairs = [
			(r.item_code, r.get(attr))
			for r in doc.items
			for attr in ("s_warehouse", "t_warehouse")
		] + list(doc.flags.get(sml.BIN_PAIRS_FLAG) or [])
		if doc.flags.get(sml.BIN_PAIRS_FLAG):
			doc.flags[sml.LOCKED_BINS_FLAG] = [
				_bin_name(i, w)
				for i, w in sorted({p for p in pairs if all(p)})
				if (i, w) in self.bins
			]
		sml.validate_supplier_metal_limit(doc)

	def _refused(self, fn, *args):
		with self.assertRaises(sml.SupplierMetalLimitError) as raised:
			fn(*args)
		return str(raised.exception)


class TestPureWeight(_World):
	def test_spec_example_is_100g_pure(self):
		held = sml._bin_holdings({wh: "SUP-A" for wh in ("A-MSL", "A-WIP", "A-HM")})
		self.assertEqual(held, {"SUP-A": 100.0})

	def test_purity_falls_back_to_the_attribute_value_when_percentage_is_zero(self):
		self.assertEqual(sml.purity_of("M-18KT"), 75.15)

	def test_24kt_counts_as_pure(self):
		self.assertEqual(sml.purity_of("M-24KT"), 100)

	def test_items_without_a_purity_do_not_count(self):
		self.assertIsNone(sml.purity_of("REF-CF-001"))
		self.assertEqual(
			sml._metal_items(["ALLOY", "D-STONE", "MU00062-009", "REF-CF-001"]), {}
		)

	def test_findings_and_loss_families_count(self):
		self.assertEqual(
			sml._metal_items(["F-80", "ML-70", "FL-9175"]),
			{"F-80": 80, "ML-70": 70, "FL-9175": 91.75},
		)


class TestStockEntryGuard(_World):
	def test_transfer_over_the_limit_is_refused_with_the_numbers(self):
		message = self._refused(self._submit, _se(_row("M-90", 10, "STORE", "A-MSL")))
		self.assertIn("Aarya Jewels", message)
		for number in ("100.0", "9.0", "109.0"):
			self.assertIn(number, message)

	def test_receipt_and_repack_rows_without_a_source_are_counted(self):
		self._refused(self._submit, _se(_row("M-9175", 1, None, "A-WIP")))

	def test_move_between_the_same_suppliers_warehouses_passes_even_at_the_limit(self):
		self._submit(
			_se(_row("M-90", 30, "A-MSL", "A-WIP"), _row("F-80", 10, "A-WIP", "A-HM"))
		)
		self.assertEqual(self.supplier_locks, [])

	def test_internal_move_with_a_purer_item_in_counts_the_net(self):
		# 10 g @ 70 out (-7) and 10 g @ 90 in (+9) within SUP-A: net +2 -> 102 > 100.
		message = self._refused(
			self._submit,
			_se(_row("ML-70", 10, "A-HM", "STORE"), _row("M-90", 10, "STORE", "A-WIP")),
		)
		self.assertIn("102.0", message)

	def test_return_out_of_an_over_limit_supplier_passes(self):
		self.bins[("M-90", "A-MSL")] = 200  # 190 g pure, well over
		self._submit(_se(_row("M-90", 50, "A-MSL", "DEPT")))
		self.assertEqual(self.supplier_locks, [])

	def test_exactly_reaching_the_limit_passes(self):
		# SUP-C holds 45.875; 4.125 g @ 100 brings it to 50.000.
		self._submit(_se(_row("M-24KT", 4.125, "STORE", "C-WIP")))
		self.assertEqual(self.supplier_locks, [(("SUP-C",), False)])

	def test_one_gram_past_the_limit_is_refused(self):
		self._refused(self._submit, _se(_row("M-24KT", 5.125, "STORE", "C-WIP")))

	def test_supplier_without_a_limit_is_not_checked(self):
		self._submit(_se(_row("M-90", 5000, "STORE", "B-WIP")))
		self.assertEqual(self.supplier_locks, [])
		self.assertEqual(self.sql_calls, [])

	def test_a_site_not_yet_migrated_treats_every_supplier_as_unlimited(self):
		self.field_exists = False
		self._submit(_se(_row("M-90", 5000, "STORE", "A-MSL")))
		self.assertEqual(self.sql_calls, [])

	def test_only_the_breaching_supplier_is_named(self):
		message = self._refused(
			self._submit,
			_se(
				_row("M-9175", 10, "STORE", "A-MSL"),
				_row("M-24KT", 1, "STORE", "C-WIP"),
			),
		)
		self.assertIn("Aarya Jewels", message)
		self.assertNotIn("Sadguru", message)
		self.assertEqual(self.supplier_locks, [(("SUP-A", "SUP-C"), False)])

	def test_non_metal_rows_are_ignored(self):
		self._submit(
			_se(
				_row("D-STONE", 100, "STORE", "A-WIP"),
				_row("MU00062-009", 5, "STORE", "A-WIP"),
				_row("REF-CF-001", 1000, "STORE", "A-WIP"),
			)
		)
		self.assertEqual(self.sql_calls, [])

	def test_eod_sync_entries_are_waived(self):
		with patch.dict(frappe.local.flags, {"in_eod_mop_sync": True}):
			self._submit(_se(_row("M-9175", 100, "DEPT", "A-WIP")))
		self.assertEqual(self.sql_calls, [])

	def test_the_check_reads_the_locked_bins_not_the_snapshot(self):
		# The snapshot still shows SUP-C nearly empty; the locked (current) read sees 45.875.
		self.stale_bins = {}
		self._refused(self._submit, _se(_row("M-9175", 5, "STORE", "C-WIP")))
		self.assertTrue(any("WHERE name IN %(names)s" in q for q in self.sql_calls))

	def test_lock_step_hands_prelock_the_suppliers_metal_bins(self):
		doc = _se(_row("M-9175", 1, "STORE", "A-MSL"))
		sml.lock_supplier_limits(doc)
		self.assertEqual(
			sorted(doc.flags[sml.BIN_PAIRS_FLAG]),
			[("F-80", "A-WIP"), ("M-90", "A-MSL"), ("ML-70", "A-HM")],
		)

	def test_an_unsynced_employee_ir_issue_counts_against_a_stock_entry(self):
		# PR #1655 review: an Issue sent 4 g pure to SUP-C today (MOP Logs, Bins unchanged). A Stock
		# Entry that fits only if that is ignored (45.875 + 0.5 = 46.375) is refused at 50.375.
		self._log("MWO-9", "MOP-9", "M-24KT", 4, "DEPT", "C-WIP", "2026-11-10 10:00")
		message = self._refused(
			self._submit, _se(_row("M-24KT", 0.5, "STORE", "C-WIP"))
		)
		self.assertIn("49.875", message)
		self.assertIn("50.375", message)
		self.logs = []
		self._submit(_se(_row("M-24KT", 0.5, "STORE", "C-WIP")))

	def test_an_unsynced_receive_back_frees_room_for_a_stock_entry(self):
		# 45.875 + 5.125 = 51.0 is refused on the Bins alone; 10 g received back today -> 41.0.
		self._log("MWO-8", "MOP-8", "M-24KT", 10, "C-WIP", "DEPT", "2026-11-10 09:00")
		self._submit(_se(_row("M-24KT", 5.125, "STORE", "C-WIP")))

	def test_an_issue_committed_after_the_snapshot_asks_the_stock_entry_to_retry(self):
		key = sml._issue_seq_key("SUP-C")
		self.seq_snapshot = {key: 3}
		self.seq_latest = {key: 4}
		doc = _se(_row("M-24KT", 0.1, "STORE", "C-WIP"))
		with self.assertRaises(frappe.ValidationError) as raised:
			self._submit(doc)
		self.assertNotIsInstance(raised.exception, sml.SupplierMetalLimitError)
		self.assertIn("retry", str(raised.exception))
		self.assertIsNone(doc.flags.get(sml.BIN_PAIRS_FLAG))

	def test_a_first_issue_after_the_snapshot_is_seen_too(self):
		# The counter row did not exist yet in the snapshot.
		self.seq_latest = {sml._issue_seq_key("SUP-C"): 1}
		with self.assertRaises(frappe.ValidationError) as raised:
			self._submit(_se(_row("M-24KT", 0.1, "STORE", "C-WIP")))
		self.assertIn("retry", str(raised.exception))

	def test_counter_row_is_created_before_it_is_locked(self):
		# PR #1655 re-review: FOR UPDATE on a missing key gap-locks tabSeries. Order per key:
		# plain read -> INSERT IGNORE -> FOR UPDATE.
		self._submit(_se(_row("M-24KT", 1, "STORE", "C-WIP")))
		series = [q for q in self.sql_calls if "tabSeries" in q]
		self.assertEqual(len(series), 3)
		self.assertNotIn("FOR UPDATE", series[0])
		self.assertIn("INSERT IGNORE", series[1])
		self.assertNotIn(
			"SET STATEMENT", series[1]
		)  # Stock Entry path may wait, as on Supplier
		self.assertIn("FOR UPDATE", series[2])
		self.assertEqual(self.seq_created, [sml._issue_seq_key("SUP-C")])

	def test_a_fresh_counter_row_is_not_a_moved_counter(self):
		# No row in the snapshot, none committed since: the row this entry creates reads 0.
		self._submit(_se(_row("M-24KT", 1, "STORE", "C-WIP")))
		self.assertEqual(self.seq_bumps, [])

	def test_a_stock_entry_never_bumps_the_issue_counter(self):
		self.seq_snapshot = {sml._issue_seq_key("SUP-C"): 7}
		self._submit(_se(_row("M-24KT", 1, "STORE", "C-WIP")))
		self.assertEqual(self.seq_bumps, [])


class TestPrelockBinsIntegration(_World):
	def test_supplier_bins_join_the_single_sorted_bin_batch(self):
		doc = _se(_row("M-9175", 1, "STORE", "A-MSL"))
		doc.flags[sml.BIN_PAIRS_FLAG] = [("M-90", "A-MSL"), ("F-80", "A-WIP")]
		with patch.object(
			se_events.frappe, "conf", frappe._dict(serialize_stock_submit_by_item=False)
		), patch.object(se_events, "preallocate_series_for_docs"), patch.object(
			se_events, "lock_bins", return_value=["BIN-1"]
		) as lock_bins, patch.object(se_events, "lock_bins_for_rows") as lock_rows:
			se_events.prelock_bins(doc, method=None)
		lock_rows.assert_not_called()
		lock_bins.assert_called_once()
		self.assertEqual(
			sorted(lock_bins.call_args.args[0]),
			sorted(
				[
					("M-9175", "STORE"),
					("M-9175", "A-MSL"),
					("M-90", "A-MSL"),
					("F-80", "A-WIP"),
				]
			),
		)
		self.assertEqual(doc.flags[sml.LOCKED_BINS_FLAG], ["BIN-1"])

	def test_without_a_limited_supplier_prelock_is_unchanged(self):
		doc = _se(_row("M-9175", 1, "STORE", "DEPT"))
		with patch.object(
			se_events.frappe, "conf", frappe._dict(serialize_stock_submit_by_item=False)
		), patch.object(se_events, "preallocate_series_for_docs"), patch.object(
			se_events, "lock_bins"
		) as lock_bins, patch.object(se_events, "lock_bins_for_rows") as lock_rows:
			se_events.prelock_bins(doc, method=None)
		lock_bins.assert_not_called()
		lock_rows.assert_called_once_with(doc.items, "s_warehouse", "t_warehouse")

	def test_hook_order_is_supplier_lock_then_prelock_then_check(self):
		chain = hooks.doc_events["Stock Entry"]["before_submit"]
		guard = "jewellery_erpnext.jewellery_erpnext.doc_events.supplier_metal_limit"
		prelock = (
			"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.prelock_bins"
		)
		self.assertLess(
			chain.index(f"{guard}.lock_supplier_limits"), chain.index(prelock)
		)
		self.assertGreater(
			chain.index(f"{guard}.validate_supplier_metal_limit"), chain.index(prelock)
		)


class TestEmployeeIRIssue(_World):
	def _eir(self, *mops, type="Issue", subcontracting="Yes", subcontractor="SUP-C"):
		return _Doc(
			name="EIR-TEST",
			type=type,
			subcontracting=subcontracting,
			subcontractor=subcontractor,
			employee_ir_operations=[
				_Doc(manufacturing_operation=mop, manufacturing_work_order=f"MWO-{mop}")
				for mop in mops
			],
		)

	def _balance(self, mop, item_code, qty):
		self.mop_balances.setdefault(mop, []).append(
			{
				"item_code": item_code,
				"batch_no": "B1",
				"qty_after_transaction_batch_based": qty,
			}
		)

	def test_issue_over_the_limit_is_refused(self):
		self._balance("MOP-1", "M-9175", 5)  # +4.5875 -> 50.4625
		self._balance("MOP-1", "D-STONE", 3)  # not metal
		message = self._refused(
			sml.validate_employee_ir_supplier_limit, self._eir("MOP-1")
		)
		self.assertIn("Sadguru Hallmarking", message)
		self.assertIn("45.875", message)
		self.assertEqual(self.supplier_locks, [(("SUP-C",), True)])

	def test_issue_within_the_limit_passes(self):
		self._balance("MOP-1", "M-24KT", 4)  # 49.875
		sml.validate_employee_ir_supplier_limit(self._eir("MOP-1"))

	def test_an_earlier_unsynced_issue_today_counts(self):
		self._log("MWO-9", "MOP-9", "M-24KT", 4, "DEPT", "C-WIP", "2026-11-10 10:00")
		self._balance("MOP-1", "M-24KT", 0.5)  # 45.875 + 4 pending + 0.5 = 50.375
		self._refused(sml.validate_employee_ir_supplier_limit, self._eir("MOP-1"))

	def test_an_unsynced_receive_back_today_frees_room(self):
		self._log("MWO-8", "MOP-8", "M-24KT", 10, "C-WIP", "DEPT", "2026-11-10 09:00")
		self._balance("MOP-1", "M-9175", 5)  # 45.875 - 10 + 4.5875 = 40.4625
		sml.validate_employee_ir_supplier_limit(self._eir("MOP-1"))

	def test_pending_follows_the_last_operation_of_the_work_order(self):
		# Issued to the supplier, then received back and moved on, all unsynced: nothing pending.
		self._log("MWO-7", "MOP-7a", "M-24KT", 4, "DEPT", "C-WIP", "2026-11-10 08:00")
		self._log("MWO-7", "MOP-7b", "M-24KT", 4, "C-WIP", "DEPT", "2026-11-10 09:00")
		self.assertEqual(sml.pending_sync_pure(["C-WIP"]), 0.0)

	def test_the_operations_being_issued_are_not_counted_twice(self):
		self._log(
			"MWO-MOP-1", "MOP-1", "M-24KT", 4, "DEPT", "C-WIP", "2026-11-10 10:00"
		)
		self.assertEqual(
			sml.pending_sync_pure(["C-WIP"], exclude_operations=["MOP-1"]), 0.0
		)
		self.assertEqual(sml.pending_sync_pure(["C-WIP"]), 4.0)

	def test_busy_supplier_asks_to_retry(self):
		self.supplier_busy = True
		self._balance("MOP-1", "M-24KT", 1)
		with self.assertRaises(frappe.ValidationError) as raised:
			sml.validate_employee_ir_supplier_limit(self._eir("MOP-1"))
		self.assertNotIsInstance(raised.exception, sml.SupplierMetalLimitError)
		self.assertIn("retry", str(raised.exception))

	def test_preflight_refuses_without_locking(self):
		self._balance("MOP-1", "M-9175", 5)
		self._refused(sml.employee_ir_preflight, self._eir("MOP-1"))
		self.assertEqual(self.supplier_locks, [])
		self.assertFalse(any("tabSeries" in q for q in self.sql_calls))
		self.assertFalse(any("LOCK IN SHARE MODE" in q for q in self.sql_calls))

	def test_an_issue_committed_after_the_snapshot_asks_to_retry(self):
		key = sml._issue_seq_key("SUP-C")
		self.seq_snapshot = {key: 3}
		self.seq_latest = {key: 4}
		self._balance("MOP-1", "M-24KT", 0.1)
		with self.assertRaises(frappe.ValidationError) as raised:
			sml.validate_employee_ir_supplier_limit(self._eir("MOP-1"))
		self.assertNotIsInstance(raised.exception, sml.SupplierMetalLimitError)
		self.assertIn("retry", str(raised.exception))
		self.assertEqual(self.seq_bumps, [])

	def test_the_issue_reads_the_latest_bins_not_the_snapshot(self):
		# The snapshot still shows SUP-C empty; the latest committed Bins hold 45.875.
		self.stale_bins = {}
		self._balance("MOP-1", "M-9175", 5)  # 45.875 + 4.5875 = 50.4625
		message = self._refused(
			sml.validate_employee_ir_supplier_limit, self._eir("MOP-1")
		)
		self.assertIn("45.875", message)
		self.assertTrue(
			any(
				"WHERE name IN %(names)s LOCK IN SHARE MODE NOWAIT" in q
				for q in self.sql_calls
			)
		)

	def test_a_bin_being_written_asks_to_retry(self):
		self.bins_busy = True
		self._balance("MOP-1", "M-24KT", 1)
		with self.assertRaises(frappe.ValidationError) as raised:
			sml.validate_employee_ir_supplier_limit(self._eir("MOP-1"))
		self.assertNotIsInstance(raised.exception, sml.SupplierMetalLimitError)
		self.assertIn("retry", str(raised.exception))

	def test_the_issue_creates_its_counter_row_without_waiting_before_locking_it(self):
		self._balance("MOP-1", "M-24KT", 1)
		sml.validate_employee_ir_supplier_limit(self._eir("MOP-1"))
		series = [q for q in self.sql_calls if "tabSeries" in q]
		self.assertNotIn("FOR UPDATE", series[0])
		self.assertTrue(
			series[1].startswith(
				"SET STATEMENT innodb_lock_wait_timeout=0 FOR INSERT IGNORE"
			)
		)
		self.assertIn("FOR UPDATE NOWAIT", series[2])
		self.assertIn(
			"ON DUPLICATE KEY UPDATE", series[3]
		)  # the bump, row already held

	def test_issue_counter_insert_never_waits(self):
		self.seq_insert_busy = True
		self._balance("MOP-1", "M-24KT", 1)
		with self.assertRaises(frappe.ValidationError) as raised:
			sml.validate_employee_ir_supplier_limit(self._eir("MOP-1"))
		self.assertNotIsInstance(raised.exception, sml.SupplierMetalLimitError)
		self.assertIn("retry", str(raised.exception))
		self.assertEqual(self.seq_bumps, [])

	def test_only_metal_bins_are_share_locked(self):
		self.bins[("D-STONE", "C-WIP")] = 20
		self._balance("MOP-1", "M-24KT", 1)
		sml.validate_employee_ir_supplier_limit(self._eir("MOP-1"))
		self.assertEqual(self.share_locked, [_bin_name("M-9175", "C-WIP")])

	def test_a_non_metal_bin_being_written_does_not_make_the_issue_busy(self):
		self.bins[("D-STONE", "C-WIP")] = 20
		self.busy_bins = {_bin_name("D-STONE", "C-WIP")}
		self._balance("MOP-1", "M-24KT", 1)
		sml.validate_employee_ir_supplier_limit(self._eir("MOP-1"))

	def test_a_metal_bin_being_written_asks_to_retry(self):
		self.busy_bins = {_bin_name("M-9175", "C-WIP")}
		self._balance("MOP-1", "M-24KT", 1)
		with self.assertRaises(frappe.ValidationError) as raised:
			sml.validate_employee_ir_supplier_limit(self._eir("MOP-1"))
		self.assertNotIsInstance(raised.exception, sml.SupplierMetalLimitError)
		self.assertIn("retry", str(raised.exception))

	def test_only_a_passing_issue_bumps_the_counter(self):
		key = sml._issue_seq_key("SUP-C")
		self._balance("MOP-1", "M-24KT", 4)  # 49.875: passes
		sml.validate_employee_ir_supplier_limit(self._eir("MOP-1"))
		self.assertEqual(self.seq_bumps, [key])
		self._balance("MOP-2", "M-9175", 5)  # 50.4625: refused
		self._refused(sml.validate_employee_ir_supplier_limit, self._eir("MOP-2"))
		self.assertEqual(self.seq_bumps, [key])

	def test_counter_keys_fit_the_series_name_column(self):
		key = sml._issue_seq_key("A" * 140)
		self.assertLessEqual(len(key), 100)
		self.assertNotEqual(key, sml._issue_seq_key("A" * 139))

	def test_non_subcontracting_issue_and_receive_are_not_checked(self):
		self._balance("MOP-1", "M-9175", 500)
		sml.validate_employee_ir_supplier_limit(self._eir("MOP-1", subcontracting="No"))
		sml.validate_employee_ir_supplier_limit(self._eir("MOP-1", type="Receive"))
		self.assertEqual(self.sql_calls, [])

	def test_supplier_without_a_limit_is_not_checked(self):
		self._balance("MOP-1", "M-9175", 500)
		sml.validate_employee_ir_supplier_limit(
			self._eir("MOP-1", subcontractor="SUP-B")
		)
		self.assertEqual(self.sql_calls, [])
