# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Multi-counter cascades take every naming counter they mint up front (C-09a).

A transaction that mints Stock Entries of two or more types has to lock every naming
counter it will use before its first Bin, SRE or MOP lock, in the canonical order
(rule rows, then ``tabSeries``, then Bins). Left to the inserts, the second counter is
taken while the first entry's Bins are held. Today that only bites where one of the types
is rule-named (the MOP "Receive Unused/Loose Material" Repack, Main Slip's loss Repack);
once Stock Entry naming is sharded per type it bites on every such path.

DB-free. ``lock_order.series_stubs`` and ``preallocate_series_for_docs`` are patched to
record into one ordered event log, and so is the first Bin-touching call of each path (the
SRE cancel, the Make Receive, a fake Stock Entry ``insert``, the loss-entry builder). Each
test checks that the log has exactly one pre-lock, that it carries one stub per Stock
Entry type the path mints (with the right company), and that it comes before that call.
"""

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.refining.constants import (
	REFINING_TYPE_SCRAP,
	REFINING_TYPE_WORK_ORDER,
)

_LOCK_ORDER = "jewellery_erpnext.jewellery_erpnext.lock_order"
_MOP = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation"
_MAIN_SLIP = "jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip"
_GEMSTONE = "jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion"

COMPANY = "KG GK Jewellers Private Limited"

MR_WO = "Material Receive (WORK ORDER)"
MT_WO = "Material Transfer (WORK ORDER)"
RMC = "Repack-Metal Conversion"
MTTD = "Material Transfer to Department"
RGC = "Repack-Gemstone Conversion"


class _Doc(SimpleNamespace):
	"""SimpleNamespace with Frappe-style ``.get()``."""

	def get(self, key, default=None):
		return getattr(self, key, default)


class _Log:
	"""One ordered log of pre-locks and the calls that touch Bins."""

	def __init__(self):
		self.events = []

	def _stubs(self, company, *types):
		# Order-preserving dedupe, like the real series_stubs.
		return [("stub", company, t) for t in dict.fromkeys(types)]

	def _prelock(self, *stubs):
		self.events.append(("prelock", tuple(stubs)))

	def record(self, label, result=None):
		def _call(*args, **kwargs):
			self.events.append((label,))
			return result

		return _call

	@contextmanager
	def patched(self):
		with (
			patch(f"{_LOCK_ORDER}.series_stubs", side_effect=self._stubs),
			patch(
				f"{_LOCK_ORDER}.preallocate_series_for_docs", side_effect=self._prelock
			),
		):
			yield self

	def labels(self):
		return [e[0] for e in self.events]

	def prelocks(self):
		return [e[1] for e in self.events if e[0] == "prelock"]

	def stock_entry(self, name="SE-FAKE"):
		"""A recording Stock Entry fake: insert logs the type it is inserted as."""
		se = _Doc(name=name, items=[], stock_entry_type=None)
		se.append = lambda table, row: se.items.append(frappe._dict(row))
		se.update = lambda values: se.__dict__.update(values)
		se.insert = lambda *a, **k: self.events.append(("insert", se.stock_entry_type))
		se.submit = MagicMock()
		return se


class _PrelockCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def assertOnePrelockBefore(self, log, first_lock_label, company, types):
		labels = log.labels()
		self.assertEqual(labels.count("prelock"), 1, log.events)
		self.assertIn(first_lock_label, labels, log.events)
		self.assertLess(
			labels.index("prelock"), labels.index(first_lock_label), log.events
		)
		stubs = log.prelocks()[0]
		self.assertEqual({s[1] for s in stubs}, {company}, stubs)
		self.assertEqual(sorted(s[2] for s in stubs), sorted(types), stubs)

	def assertEveryInsertPrelocked(self, log, exempt=()):
		"""Every Stock Entry inserted is of a type pre-locked earlier in the log."""
		locked = set()
		for event in log.events:
			if event[0] == "prelock":
				locked |= {s[2] for s in event[1]}
			elif event[0] == "insert" and event[1] not in exempt:
				self.assertIn(event[1], locked, log.events)


def _passthrough_get_value(overrides):
	"""Patch target for frappe.db.get_value: answer ``overrides`` keyed by
	(doctype, name, field), send everything else to the real method (flt reads System
	Settings through it)."""
	real = frappe.db.get_value

	def _get_value(doctype, *args, **kwargs):
		key = (doctype, *args[:2])
		if key in overrides:
			return overrides[key]
		return real(doctype, *args, **kwargs)

	return _get_value


# ---------------------------------------------------------------------------
# 1/2. Refining submit: Material Transfer -> Material Receipt (Scrap) [-> PO]
# ---------------------------------------------------------------------------


class TestRefiningSubmitPrelock(_PrelockCase):
	def _entry(self, log, refining_type, qty, allocations, **attrs):
		from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
			RefiningEntry,
		)

		fields = dict(
			name="RFN-TEST",
			company=COMPANY,
			refining_type=refining_type,
			is_external=0,
			parent_refining_entry=None,
			warehouse="Source - T",
			refining_warehouse="Refining RM - T",
			supplier="Refinery Supplier - T",
			supplier_warehouse="Supplier - T",
			manufacturer="Shubh",
			material_items=[
				frappe._dict(item_code="M-TEST", qty=qty, uom="Gram", purity="91.75")
			],
			_serial_movement_rows=lambda: [],
			allocate_fifo_batches=lambda *a, **k: [
				{"batch_no": f"B-{i}", "qty": q} for i, q in enumerate(allocations)
			],
			_stamp_batch_ownership=lambda se: None,
			_cancel_source_mwo_sres=log.record("sre_cancel"),
			_zero_source_mwo_and_mop_weights=log.record("zero_weights"),
			get_dust_opening_batch=lambda item_code: "DUST-B",
			set_dust_receipt_difference_account=lambda se: None,
			create_external_refining_po=log.record("po_insert"),
			db_set=MagicMock(),
		)
		fields.update(attrs)
		entry = _Doc(**fields)
		entry.create_material_transfer_se = lambda **k: (
			RefiningEntry.create_material_transfer_se(entry, **k)
		)
		entry.create_dust_opening_receipt_se = lambda **k: (
			RefiningEntry.create_dust_opening_receipt_se(entry, **k)
		)
		entry.on_submit_external = lambda: RefiningEntry.on_submit_external(entry)
		return entry

	def _submit(self, entry, log):
		from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
			RefiningEntry,
		)

		with ExitStack() as stack:
			stack.enter_context(log.patched())
			stack.enter_context(
				patch("frappe.new_doc", side_effect=lambda doctype: log.stock_entry())
			)
			stack.enter_context(
				patch.object(
					frappe.db,
					"get_value",
					side_effect=_passthrough_get_value(
						{("Item", "M-TEST", "has_batch_no"): 1}
					),
				)
			)
			stack.enter_context(
				patch.object(frappe.db, "MAX_WRITES_PER_TRANSACTION", 200_000)
			)
			RefiningEntry.on_submit(entry)

	def test_work_order_refining_locks_the_counter_before_the_sre_cancels(self):
		# E-W305: the SRE cancels write Bin.reserved_qty before the transfer is inserted.
		log = _Log()
		entry = self._entry(log, REFINING_TYPE_WORK_ORDER, 1.0, [1.0])
		self._submit(entry, log)
		self.assertOnePrelockBefore(log, "sre_cancel", COMPANY, ["Material Transfer"])
		self.assertEqual(
			log.labels(), ["prelock", "sre_cancel", "insert", "zero_weights"]
		)

	def test_scrap_shortfall_locks_the_receipt_counter_with_the_transfer(self):
		log = _Log()
		entry = self._entry(log, REFINING_TYPE_SCRAP, 1.0, [0.6])
		self._submit(entry, log)
		self.assertOnePrelockBefore(
			log, "insert", COMPANY, ["Material Transfer", "Material Receipt"]
		)
		self.assertEqual(
			[e[1] for e in log.events if e[0] == "insert"],
			["Material Transfer", "Material Receipt"],
		)
		self.assertEveryInsertPrelocked(log)

	def test_scrap_without_shortfall_locks_only_the_transfer(self):
		log = _Log()
		entry = self._entry(log, REFINING_TYPE_SCRAP, 1.0, [1.0])
		self._submit(entry, log)
		self.assertOnePrelockBefore(log, "insert", COMPANY, ["Material Transfer"])
		self.assertEveryInsertPrelocked(log)

	def test_nothing_to_move_takes_no_counter(self):
		# No transfer rows and no receipt: nothing is minted, so nothing is held.
		log = _Log()
		entry = self._entry(log, REFINING_TYPE_WORK_ORDER, 1.0, [])
		self._submit(entry, log)
		self.assertEqual(log.labels(), ["sre_cancel", "zero_weights"])

	def test_external_scrap_keeps_the_purchase_order_terminal(self):
		# The PO rule is taken last on purpose (followed only by commit); pre-locking it
		# would pin the purchase team's counter for the whole transfer.
		log = _Log()
		entry = self._entry(log, REFINING_TYPE_SCRAP, 1.0, [0.6], is_external=1)
		self._submit(entry, log)
		self.assertOnePrelockBefore(
			log, "insert", COMPANY, ["Material Transfer", "Material Receipt"]
		)
		self.assertEqual(log.labels()[-1], "po_insert")
		self.assertEveryInsertPrelocked(log)


# ---------------------------------------------------------------------------
# 3. complete_refining: Manufacture -> (refining loss) Material Transfer
# ---------------------------------------------------------------------------


class TestCompleteRefiningPrelock(_PrelockCase):
	def _entry(self, refining_loss):
		from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
			RefiningEntry,
		)

		entry = _Doc(
			name="RFN-TEST",
			company=COMPANY,
			refining_loss=refining_loss,
			refining_warehouse="Refining RM - T",
			material_transfer_se=None,
			material_items=[],
			refined_gold=[],
			recovered_diamond=[],
			recovered_gemstone=[],
			_stamp_batch_ownership=lambda se: None,
			_get_pure_gold_24kt_item=lambda: None,
			_recovered_output_customer=lambda se, item: None,
			get_dust_item=lambda: None,
			db_set=MagicMock(),
		)
		# The negative-batch retry recurses through self.
		entry.create_repack_se = lambda: RefiningEntry.create_repack_se(entry)
		return entry

	def _repack(self, entry, log, se_factory=None):
		from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
			RefiningEntry,
		)

		with (
			log.patched(),
			patch(
				"frappe.new_doc",
				side_effect=se_factory or (lambda doctype: log.stock_entry()),
			),
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "rollback"),
		):
			RefiningEntry.create_repack_se(entry)

	def test_refining_loss_locks_the_scrap_transfer_counter_before_the_manufacture(
		self,
	):
		log = _Log()
		self._repack(self._entry(0.25), log)
		self.assertOnePrelockBefore(
			log, "insert", COMPANY, ["Manufacture", "Material Transfer"]
		)

	def test_no_refining_loss_locks_only_the_manufacture(self):
		log = _Log()
		self._repack(self._entry(0), log)
		self.assertOnePrelockBefore(log, "insert", COMPANY, ["Manufacture"])

	def test_negative_batch_retry_relocks_before_its_insert(self):
		# The savepoint retry re-enters the same rows: harmless, and still before insert.
		log = _Log()
		attempts = []

		def _factory(doctype):
			se = log.stock_entry()
			if not attempts:
				se.submit = MagicMock(
					side_effect=frappe.ValidationError("Negative Batch quantity")
				)
			attempts.append(se)
			return se

		self._repack(self._entry(0.25), log, se_factory=_factory)
		self.assertEqual(len(attempts), 2)
		self.assertEqual(log.labels(), ["prelock", "insert", "prelock", "insert"])
		self.assertEveryInsertPrelocked(log)


class TestRefiningStubsFollowTheRefiningSeries(_PrelockCase):
	"""Integration point with C-42 (fix/refining-stock-entry-own-counter-kggk-prod).

	C-42 can name every refining Stock Entry from its own series. The refining stubs must
	then carry that series too, or the pre-lock pins MAT-STE- for the whole transfer again,
	which is the hold C-42 removes. Skipped until C-42 is on the branch; after that it
	fails until ``_refining_series_stubs`` applies the refining series to its stubs.
	"""

	def test_stubs_take_the_refining_series_when_it_is_configured(self):
		try:
			from jewellery_erpnext.refining import stock_entry_naming
		except ImportError:
			self.skipTest("C-42 (refining.stock_entry_naming) is not on this branch")
		from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
			_refining_series_stubs,
		)

		with patch.dict(frappe.conf, {stock_entry_naming.CONFIG_KEY: "RFN-STE-"}):
			stubs = _refining_series_stubs(
				COMPANY, "Material Transfer", "Material Receipt"
			)
		self.assertEqual([s.naming_series for s in stubs], ["RFN-STE-", "RFN-STE-"])


# ---------------------------------------------------------------------------
# 4. create_snc: Make Receive -> Repack-Metal Conversion x2N -> Material Transfer (WO)
# ---------------------------------------------------------------------------


def _settle_row(qty, batch_no):
	return {
		"item_code": "M-G-18KT-75.4-P",
		"qty": qty,
		"custom_pure_qty": qty * 0.754,
		"batch_no": batch_no,
		"s_warehouse": "Waxing WO",
		"batch_customer": "CUST-1",
	}


class TestCreateSncPrelock(_PrelockCase):
	def _run(self, rows, owner_batch, source_batch=None):
		from jewellery_erpnext.customer_subcontracting.sub_utils import snc

		log = _Log()
		mwo = _Doc(
			name="MWO-1",
			company=COMPANY,
			customer="CUST-OWNER",
			manufacturing_order="PMO-1",
			manufacturing_operation="MOP-1",
		)
		with (
			log.patched(),
			patch.object(snc, "_get_mwo", return_value=mwo),
			patch.object(snc, "validate_button_visibility", return_value=True),
			patch.object(snc, "_is_customer_gold", return_value=1),
			patch.object(
				snc,
				"_get_original_material_transfer",
				return_value=_Doc(company=COMPANY, branch="BR"),
			),
			patch.object(snc, "_get_receivable_gold_rows", return_value=rows),
			patch.object(snc, "_row_needs_settlement", return_value=True),
			patch.object(snc, "find_owner_batch", return_value=owner_batch),
			patch.object(snc, "find_owner_rm_warehouse", return_value=source_batch),
			patch.object(
				snc,
				"trigger_make_receive",
				side_effect=log.record("make_receive", {"docname": "MR-1"}),
			),
			patch.object(
				snc,
				"create_repack_metal_conversion",
				side_effect=log.record(
					"conversion",
					{
						"stock_entry": "RMC-1",
						"target_batch": "OUT-1",
						"warehouse": "RM",
					},
				),
			),
			patch.object(
				snc,
				"create_material_transfer_work_order",
				side_effect=log.record("transfer", "MT-1"),
			),
			patch.object(snc.frappe.db, "set_value"),
		):
			snc.create_snc(mwo)
		return log

	def test_owner_batch_path_locks_all_three_counters_before_the_make_receive(self):
		log = self._run(
			[_settle_row(1.0, "B-1"), _settle_row(2.0, "B-2")],
			owner_batch={"batch_no": "OWN", "warehouse": "Model Making RM"},
		)
		self.assertOnePrelockBefore(log, "make_receive", COMPANY, [MR_WO, RMC, MT_WO])
		self.assertEqual(log.labels(), ["prelock", "make_receive", "transfer"])

	def test_conversion_path_locks_once_before_the_make_receive(self):
		log = self._run(
			[_settle_row(1.0, "B-1"), _settle_row(2.0, "B-2")],
			owner_batch=None,
			source_batch={
				"batch_no": "SRC",
				"item_code": "M-G-22KT-91.6-Y",
				"warehouse": "RM",
				"qty": 1.2,
			},
		)
		self.assertOnePrelockBefore(log, "make_receive", COMPANY, [MR_WO, RMC, MT_WO])
		self.assertEqual(
			log.labels(),
			[
				"prelock",
				"make_receive",
				"conversion",
				"conversion",
				"conversion",
				"conversion",
				"transfer",
			],
		)


# ---------------------------------------------------------------------------
# 5. MOP "Receive Unused/Loose Material": Material Receive (WO) -> Repack (rule row)
# ---------------------------------------------------------------------------


class TestReceiveUnusedLoosePrelock(_PrelockCase):
	def _run(self, se_data, existing=None, request_id=None):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation import (
			manufacturing_operation as mop_module,
		)

		log = _Log()
		with (
			log.patched(),
			patch(f"{_MOP}._existing_receive_se", return_value=existing),
			patch(
				f"{_MOP}._preflight_unused_loose_targets",
				side_effect=log.record("preflight"),
			),
			patch(
				f"{_MOP}.create_mr_wo_stock_entry",
				side_effect=log.record("make_receive", {"docname": "SE-RECV"}),
			),
			patch(
				f"{_MOP}._convert_received_scrap_to_scrap_batch",
				side_effect=log.record("repack"),
			),
			patch.object(
				frappe.db,
				"get_value",
				side_effect=_passthrough_get_value(
					{("Manufacturing Operation", "MOP-1", "company"): COMPANY}
				),
			),
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "release_savepoint"),
		):
			mop_module.create_scrap_wo_stock_entry(se_data, request_id=request_id)
		return log

	def test_repack_rule_is_locked_before_the_receive(self):
		# E-D04: the Repack rule row used to come after MAT-STE- and the receive's Bins.
		log = self._run({"manufacturing_operation": "MOP-1", "receive_items": [{}]})
		self.assertOnePrelockBefore(log, "make_receive", COMPANY, [MR_WO, "Repack"])
		self.assertEqual(
			log.labels(), ["preflight", "prelock", "make_receive", "repack"]
		)

	def test_replay_still_locks_for_the_repack_it_may_mint(self):
		log = self._run(
			{"manufacturing_operation": "MOP-1", "receive_items": [{}]},
			existing="SE-RECV",
			request_id="R-1",
		)
		self.assertOnePrelockBefore(log, "make_receive", COMPANY, [MR_WO, "Repack"])
		self.assertNotIn("preflight", log.labels())

	def test_no_operation_takes_no_counter(self):
		log = self._run({"receive_items": [{}]})
		self.assertNotIn("prelock", log.labels())


# ---------------------------------------------------------------------------
# 6. Main Slip on_submit: per loss row Repack -> Material Transfer to Department
# ---------------------------------------------------------------------------


class TestMainSlipPrelock(_PrelockCase):
	def _submit(self, loss_rows):
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			MainSlip,
		)

		log = _Log()
		slip = _Doc(company=COMPANY, main_slip_operation=[], loss_details=loss_rows)
		with (
			log.patched(),
			patch(
				f"{_MAIN_SLIP}.create_loss_stock_entries",
				side_effect=log.record("loss_entries"),
			),
			patch.object(frappe.db, "MAX_WRITES_PER_TRANSACTION", 200_000),
		):
			MainSlip.on_submit(slip)
		return log

	def test_loss_rows_lock_both_counters_before_the_first_entry(self):
		row = _Doc(item_code="M-1", variant_of="M", received_qty=1.0, msl_qty=1.5)
		log = self._submit([row, row])
		self.assertOnePrelockBefore(log, "loss_entries", COMPANY, ["Repack", MTTD])
		self.assertEqual(log.labels(), ["prelock", "loss_entries", "loss_entries"])

	def test_no_loss_rows_take_no_counter(self):
		self.assertEqual(self._submit([]).labels(), [])


# ---------------------------------------------------------------------------
# 7. Gemstone Conversion on_submit: Repack-Gemstone Conversion -> (Resize) Transfer
# ---------------------------------------------------------------------------


class TestGemstoneConversionPrelock(_PrelockCase):
	def _submit(self, conversion_type):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import (
			GemstoneConversion,
		)

		log = _Log()
		doc = _Doc(company=COMPANY, conversion_type=conversion_type, g_source_qty=None)
		with (
			log.patched(),
			patch(
				f"{_GEMSTONE}.make_gemstone_stock_entry",
				side_effect=log.record("conversion_repack"),
			),
			patch(
				f"{_GEMSTONE}.make_target_item_transfer_stock_entry",
				side_effect=log.record("resize_transfer"),
			),
		):
			GemstoneConversion.on_submit(doc)
		return log

	def test_resize_locks_both_counters_before_the_repack(self):
		log = self._submit("Resize")
		self.assertOnePrelockBefore(
			log, "conversion_repack", COMPANY, [RGC, "Material Transfer"]
		)
		self.assertEqual(
			log.labels(), ["prelock", "conversion_repack", "resize_transfer"]
		)

	def test_conversion_locks_only_its_repack(self):
		log = self._submit("Conversion")
		self.assertOnePrelockBefore(log, "conversion_repack", COMPANY, [RGC])


# ---------------------------------------------------------------------------
# The lock-sequence probe (tests/_lock_sequence_probe.py) runs only on the throwaway site;
# its classifier and analysis are checked here, DB-free, on the statements frappe emits.
# ---------------------------------------------------------------------------

_GETSERIES = "SELECT `current` FROM `tabSeries` WHERE `name`=%(param1)s FOR UPDATE"
_PRELOCK_SERIES = "SELECT `current` FROM `tabSeries` WHERE name = %s FOR UPDATE"
_SERIES_BUMP = "UPDATE `tabSeries` SET `current` = `current` + 1 WHERE `name`=%s"
_SERIES_NEW = "INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 1)"
_RULE_LOCK = (
	"SELECT `counter` FROM `tabDocument Naming Rule` WHERE `name`=%(param1)s "
	"ORDER BY `creation` DESC LIMIT 1 FOR UPDATE"
)
_RULE_BUMP = (
	"UPDATE `tabDocument Naming Rule` SET `counter`=5,`modified`=%(param1)s,"
	"`modified_by`=%(param2)s WHERE `name`=%(param3)s"
)
_BIN_LOCK = (
	"\n\t\t\tSELECT name FROM `tabBin`\n\t\t\tWHERE item_code = %s AND warehouse = %s"
	"\n\t\t\tFOR UPDATE\n\t\t\t"
)
_BIN_UPDATE = (
	"UPDATE `tabBin` SET `reserved_stock`=1,`modified`=%(param1)s,"
	"`modified_by`=%(param2)s WHERE `name`=%(param3)s"
)
_BIN_READ = "SELECT `actual_qty` FROM `tabBin` WHERE `item_code`=%(param1)s"
_SRE_UPDATE = (
	"UPDATE `tabStock Reservation Entry` SET `status`=%(param1)s,`modified`=%(param2)s,"
	"`modified_by`=%(param3)s WHERE `name`=%(param4)s"
)
_MOP_LOCK = (
	"SELECT `status` FROM `tabManufacturing Operation` WHERE `name`=%(param1)s "
	"ORDER BY `creation` DESC LIMIT 1 FOR UPDATE"
)
_NOW = {"param1": "2026-11-17 18:09:36", "param2": "Administrator"}


class TestLockSequenceProbeClassifier(_PrelockCase):
	def test_classifies_counter_bin_and_late_statements(self):
		from jewellery_erpnext.jewellery_erpnext.tests._lock_sequence_probe import (
			classify,
		)

		cases = [
			(_GETSERIES, {"param1": "MAT-STE-"}, ("counter", "lock", "MAT-STE-")),
			(_PRELOCK_SERIES, ("MAT-STE-",), ("counter", "lock", "MAT-STE-")),
			(_SERIES_BUMP, ("MAT-STE-",), ("counter", "update", "MAT-STE-")),
			(_SERIES_NEW, ("NEW-",), ("counter", "insert", "NEW-")),
			(_RULE_LOCK, {"param1": "a0h37l6l3v"}, ("counter", "lock", "a0h37l6l3v")),
			(
				_RULE_BUMP,
				{**_NOW, "param3": "a0h37l6l3v"},
				("counter", "update", "a0h37l6l3v"),
			),
			(_BIN_LOCK, ("ITEM", "WH"), ("bin", "lock", ("ITEM", "WH"))),
			(_BIN_UPDATE, {**_NOW, "param3": "BIN-1"}, ("bin", "update", "BIN-1")),
			(
				_SRE_UPDATE,
				{
					"param1": "Cancelled",
					"param2": "x",
					"param3": "y",
					"param4": "SRE-1",
				},
				("sre", "update", "SRE-1"),
			),
			(_MOP_LOCK, {"param1": "MOP-1"}, ("mop", "lock", "MOP-1")),
		]
		for sql, values, (kind, op, key) in cases:
			with self.subTest(sql=sql):
				found = classify(sql, values)
				self.assertIsNotNone(found)
				self.assertEqual((found[0], found[2], found[3]), (kind, op, key))

		self.assertIsNone(classify(_BIN_READ, {"param1": "ITEM"}))

	def _events(self, *spec):
		from jewellery_erpnext.jewellery_erpnext.tests._lock_sequence_probe import (
			LockEvent,
		)

		return [
			LockEvent(seq, kind, "t", "lock", key, "")
			for seq, (kind, key) in enumerate(spec)
		]

	def test_counter_first_taken_after_a_bin_is_late(self):
		from jewellery_erpnext.jewellery_erpnext.tests._lock_sequence_probe import (
			analyse,
		)

		# Base-code shape of the MOP loose-material path (E-D04).
		late, _warnings = analyse(
			self._events(
				("counter", "MAT-STE-"),
				("bin", ("I", "W")),
				("sre", "SRE-1"),
				("counter", "a0h37l6l3v"),
			)
		)
		self.assertEqual(list(late), ["a0h37l6l3v"])

	def test_re_entry_after_bins_is_not_late(self):
		from jewellery_erpnext.jewellery_erpnext.tests._lock_sequence_probe import (
			analyse,
		)

		late, warnings = analyse(
			self._events(
				("counter", "a0h37l6l3v"),
				("counter", "MAT-STE-"),
				("bin", ("I", "W")),
				("counter", "MAT-STE-"),
				("counter", "a0h37l6l3v"),
			)
		)
		self.assertEqual((late, warnings), ({}, {}))

	def test_terminal_counter_and_late_class_warning(self):
		from jewellery_erpnext.jewellery_erpnext.tests._lock_sequence_probe import (
			analyse,
		)

		late, warnings = analyse(
			self._events(
				("mop", "MOP-1"),
				("counter", "MAT-STE-"),
				("bin", ("I", "W")),
				("counter", "6juicfcrjn"),
			),
			terminal={"6juicfcrjn"},
		)
		self.assertEqual(late, {})
		self.assertEqual(list(warnings), ["MAT-STE-"])

	def test_probe_records_then_forwards_and_restores_sql(self):
		from jewellery_erpnext.jewellery_erpnext.tests._lock_sequence_probe import (
			LockSequenceProbe,
		)

		inner = MagicMock(return_value=[[7]])
		with patch.object(frappe.local.db, "sql", inner):
			with LockSequenceProbe() as probe:
				result = frappe.db.sql(_GETSERIES, {"param1": "MAT-STE-"})
				frappe.db.sql(_BIN_READ, {"param1": "ITEM"})
			self.assertIs(frappe.local.db.sql, inner)
		self.assertEqual(result, [[7]])
		self.assertEqual(inner.call_count, 2)
		self.assertEqual(
			[(e.kind, e.key) for e in probe.events], [("counter", "MAT-STE-")]
		)

	def test_probe_refuses_any_other_site(self):
		from jewellery_erpnext.jewellery_erpnext.tests import _lock_sequence_probe

		with patch.object(frappe.local, "site", "kggk-prod.localhost"):
			with self.assertRaises(frappe.ValidationError):
				_lock_sequence_probe.run_probe("receive_unused_loose")
			with self.assertRaises(frappe.ValidationError):
				_lock_sequence_probe.run_all()
