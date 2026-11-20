# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the diamond / gemstone loss booked from a Manufacturing Operation (dg_loss_booking).

DB-free per the suite convention: ``setUpClass`` is neutralised and every lookup the code
under test makes is answered by a stub keyed on its doctype, passing anything else through
untouched (``frappe.db.get_value`` / ``frappe.get_all`` also load DocType meta and
translations). The pieces covered are the ones the booking's correctness rests on: the row
rules (requirement 5), the destination warehouse, the Process Loss row shape, the negative MOP
Log rows, the reservation pass, and the Stock Entry cancel guard and restore that replace a
booking document.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	loss_stock_entry,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation import (
	dg_loss_booking as mlb,
)

MOD = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.dg_loss_booking"
LSE = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry"
MOP_LOG = "jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log"
OWNERSHIP = "jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership"

DIAMOND = "D-RND-VVS-EF-1.00 MM"
GEMSTONE = "G-RUBY-RND-2.00 MM"
EMP_A = "KGJPL - 00680"
EMP_B = "KGJPL - 00675"


def _bal(item_code, batch_no, qty, pcs, variant_of="D"):
	return frappe._dict(
		item_code=item_code,
		batch_no=batch_no,
		variant_of=variant_of,
		stock_uom="Carat",
		available_qty=qty,
		available_pcs=pcs,
	)


BALANCE = {
	(DIAMOND, "B-D1"): _bal(DIAMOND, "B-D1", 0.300, 10),
	(GEMSTONE, "B-G1"): _bal(GEMSTONE, "B-G1", 1.250, 4, variant_of="G"),
	(DIAMOND, "B-D0"): _bal(DIAMOND, "B-D0", 0.050, 0),
}
LOSS_TYPES = {"Burn": "Scrap", "CVD Rejection": "Raw Material", "Unmapped": None}
ALLOWED = {EMP_A, EMP_B}


def _row(
	idx=1,
	item_code=DIAMOND,
	batch_no="B-D1",
	loss_type="Burn",
	qty=0.010,
	pcs=1,
	employee=EMP_A,
):
	return frappe._dict(
		idx=idx,
		item_code=item_code,
		batch_no=batch_no,
		loss_type=loss_type,
		loss_qty=qty,
		loss_pcs=pcs,
		employee=employee,
		stock_uom="Carat",
		variant_of="D" if item_code == DIAMOND else "G",
		target_warehouse="Scrap - DS",
	)


def _keyed(real, owned):
	"""A side_effect answering ``owned[doctype]`` and forwarding every other call verbatim."""

	def fake(*args, **kwargs):
		doctype = args[0] if args else kwargs.get("doctype")
		if doctype in owned:
			answer = owned[doctype]
			return answer(*args, **kwargs) if callable(answer) else answer
		return real(*args, **kwargs)

	return fake


class _NoDB(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# Build the translation cache before anything is patched (frappe._ reads Translation).
		frappe._("Row {0}")

	def assertThrows(self, fn, *needles):
		with self.assertRaises(ValidationError) as ctx:
			fn()
		message = str(ctx.exception)
		for needle in needles:
			self.assertIn(needle, message)
		return message


class TestValidateLossRows(_NoDB):
	"""Requirement 5: qty, pcs, employee, item and loss type rules, all reported together."""

	def _validate(self, rows, blocked=()):
		mlb.validate_loss_rows(rows, BALANCE, ALLOWED, set(blocked), LOSS_TYPES)

	def test_valid_rows_pass(self):
		self._validate(
			[
				_row(1),
				_row(2, GEMSTONE, "B-G1", "CVD Rejection", 1.25, 4, EMP_B),
				_row(3, DIAMOND, "B-D0", qty=0.05, pcs=0),
			]
		)

	def test_qty_up_to_available_passes(self):
		self._validate([_row(qty=0.300, pcs=10)])

	def test_qty_over_available_refused(self):
		self.assertThrows(
			lambda: self._validate([_row(qty=0.301)]), "exceeds the available qty"
		)

	def test_zero_qty_refused(self):
		self.assertThrows(
			lambda: self._validate([_row(qty=0)]), "Loss Qty must be greater than 0"
		)

	def test_pcs_over_available_refused(self):
		self.assertThrows(
			lambda: self._validate([_row(pcs=11)]), "exceeds the available PCS"
		)

	def test_pcs_required_when_batch_has_pcs(self):
		self.assertThrows(lambda: self._validate([_row(pcs=0)]), "Loss PCS is required")

	def test_pcs_must_be_zero_when_batch_has_none(self):
		self.assertThrows(
			lambda: self._validate([_row(batch_no="B-D0", qty=0.01, pcs=1)]),
			"Loss PCS must be 0",
		)

	def test_fractional_pcs_refused(self):
		self.assertThrows(lambda: self._validate([_row(pcs=1.5)]), "whole number")

	def test_loss_type_required(self):
		self.assertThrows(
			lambda: self._validate([_row(loss_type=None)]), "Loss Type is required"
		)

	def test_unknown_loss_type_refused(self):
		self.assertThrows(
			lambda: self._validate([_row(loss_type="Nope")]), "does not exist"
		)

	def test_loss_type_without_warehouse_type_refused(self):
		self.assertThrows(
			lambda: self._validate([_row(loss_type="Unmapped")]),
			"no Destination Warehouse Type",
		)

	def test_employee_not_on_work_order_refused(self):
		self.assertThrows(
			lambda: self._validate([_row(employee="KGJPL - 99999")]), "has not worked"
		)

	def test_employee_required(self):
		self.assertThrows(
			lambda: self._validate([_row(employee=None)]), "Employee is required"
		)

	def test_item_not_in_operation_balance_refused(self):
		self.assertThrows(
			lambda: self._validate([_row(batch_no="B-ELSEWHERE")]),
			"not a diamond / gemstone available",
		)

	def test_same_batch_twice_refused(self):
		self.assertThrows(
			lambda: self._validate([_row(1), _row(2, loss_type="CVD Rejection")]),
			"already on row 1",
		)

	def test_blocked_variant_refused(self):
		self.assertThrows(
			lambda: self._validate([_row()], blocked={"D"}), "does not allow loss"
		)

	def test_no_rows_refused(self):
		self.assertThrows(lambda: self._validate([]), "at least one")

	def test_every_problem_reported_at_once(self):
		message = self.assertThrows(
			lambda: self._validate(
				[_row(1, qty=1.0), _row(2, GEMSTONE, "B-G1", pcs=9)]
			),
			"Row 1",
			"Row 2",
		)
		self.assertIn("exceeds the available qty", message)
		self.assertIn("exceeds the available PCS", message)


class TestDepartmentWarehouse(_NoDB):
	def _resolve(self, names):
		real = frappe.get_all
		with patch(
			f"{MOD}.frappe.get_all", side_effect=_keyed(real, {"Warehouse": names})
		):
			return mlb.get_department_warehouse("Diamond Setting - KGJPL", "Scrap")

	def test_single_warehouse_resolves(self):
		self.assertEqual(self._resolve(["Scrap - DS"]), ("Scrap - DS", None))

	def test_missing_warehouse_reported(self):
		warehouse, error = self._resolve([])
		self.assertIsNone(warehouse)
		self.assertIn("has no Scrap warehouse", error)

	def test_several_warehouses_reported(self):
		warehouse, error = self._resolve(["Scrap A", "Scrap B"])
		self.assertIsNone(warehouse)
		self.assertIn("more than one Scrap warehouse", error)

	def test_filters_exclude_groups_and_employee_warehouses(self):
		real = frappe.get_all
		seen = {}

		def warehouses(*args, **kwargs):
			seen.update(kwargs.get("filters") or {})
			return ["Raw - DS"]

		with patch(
			f"{MOD}.frappe.get_all", side_effect=_keyed(real, {"Warehouse": warehouses})
		):
			mlb.get_department_warehouse("Diamond Setting - KGJPL", "Raw Material")
		self.assertEqual(seen["warehouse_type"], "Raw Material")
		self.assertEqual(seen["department"], "Diamond Setting - KGJPL")
		self.assertEqual(seen["is_group"], 0)
		self.assertEqual(seen["employee"], ["is", "not set"])


class TestOperationBlockReason(_NoDB):
	def _reason(self, enabled=True, status="WIP", current="MOP-1"):
		mop = frappe._dict(
			name="MOP-1",
			department="Diamond Setting - KGJPL",
			status=status,
			manufacturing_work_order="MWO-1",
		)
		real = frappe.db.get_value
		with (
			patch(f"{MOD}.is_department_enabled", return_value=enabled),
			patch(
				f"{MOD}.frappe.db.get_value",
				side_effect=_keyed(real, {"Manufacturing Work Order": current}),
			),
		):
			return mlb.get_operation_block_reason(mop)

	def test_open_current_operation_can_book(self):
		self.assertIsNone(self._reason())

	def test_department_not_enabled(self):
		self.assertIn("not enabled", self._reason(enabled=False))

	def test_finished_operation(self):
		for status in ("Finished", "Revert", "Hold"):
			self.assertIn(status, self._reason(status=status))

	def test_not_the_current_operation(self):
		self.assertIn("not the current operation", self._reason(current="MOP-2"))


class TestGroupByEmployee(_NoDB):
	def test_one_group_per_employee_in_first_seen_order(self):
		entries = [
			frappe._dict(row=_row(1, employee=EMP_B)),
			frappe._dict(row=_row(2, employee=EMP_A)),
			frappe._dict(row=_row(3, employee=EMP_B)),
		]
		groups = mlb.group_by_employee(entries)
		self.assertEqual([g[0] for g in groups], [EMP_B, EMP_A])
		self.assertEqual([e.row.idx for e in groups[0][1]], [1, 3])


class _FakeEntry:
	"""Stock Entry stand-in: records header fields and appended rows."""

	def __init__(self):
		self.items = []
		self.flags = frappe._dict()

	def append(self, table, row):
		self.items.append(frappe._dict(row))

	def set(self, field, value):
		setattr(self, field, value)

	def get(self, field, default=None):
		return getattr(self, field, default)


class TestBuildLossStockEntry(_NoDB):
	def _build(self, rows, customer=None):
		mop = frappe._dict(
			name="MOP-1",
			company="KGJPL",
			manufacturing_work_order="MWO-1",
			manufacturing_order="PMO-1",
			department="Diamond Setting - KGJPL",
		)
		entries = [
			frappe._dict(
				row=row,
				qty=flt(row.loss_qty, 3),
				s_warehouse="Emp WH",
				loss_item=f"L-{row.idx}",
			)
			for row in rows
		]
		real = frappe.new_doc
		with (
			patch(
				f"{MOD}.frappe.new_doc",
				side_effect=_keyed(real, {"Stock Entry": lambda *a, **k: _FakeEntry()}),
			),
			patch(
				f"{OWNERSHIP}.resolve_batch_ownership",
				return_value=("Regular Stock", customer),
			),
		):
			return mlb.build_loss_stock_entry(mop, EMP_A, entries)

	def test_header(self):
		se = self._build([_row()])
		self.assertEqual(se.stock_entry_type, "Process Loss")
		self.assertEqual(se.purpose, "Repack")
		self.assertEqual(se.auto_created, 1)
		self.assertEqual(se.employee, EMP_A)
		self.assertEqual(se.manufacturing_operation, "MOP-1")
		self.assertEqual(se.manufacturing_work_order, "MWO-1")
		self.assertEqual(se.department, "Diamond Setting - KGJPL")
		self.assertEqual(se.company, "KGJPL")
		# No link field: the entry itself is the record of the booking.
		self.assertFalse(hasattr(se, "custom_mop_loss_booking"))
		self.assertIn("MOP-1", se.remarks)
		self.assertIn("Burn", se.remarks)

	def test_consume_then_produce_per_row(self):
		rows = [
			_row(1, qty=0.01, pcs=1),
			_row(2, GEMSTONE, "B-G1", "CVD Rejection", 0.5, 2),
		]
		rows[1].target_warehouse = "Raw - DS"
		se = self._build(rows)
		self.assertEqual(len(se.items), 4)
		consume, produce = se.items[0], se.items[1]
		self.assertEqual(
			(consume.item_code, consume.s_warehouse, consume.t_warehouse),
			(DIAMOND, "Emp WH", None),
		)
		self.assertEqual(consume.batch_no, "B-D1")
		self.assertEqual(
			(produce.item_code, produce.s_warehouse, produce.t_warehouse),
			("L-1", None, "Scrap - DS"),
		)
		self.assertEqual(produce.is_finished_item, 1)
		self.assertEqual(produce.set_basic_rate_manually, 1)
		self.assertNotIn("batch_no", produce)
		# The CVD row lands in the Raw Material warehouse its Loss Type names.
		self.assertEqual(se.items[3].t_warehouse, "Raw - DS")

	def test_qty_and_pcs_on_both_rows(self):
		se = self._build([_row(qty=0.025, pcs=3)])
		for item in se.items:
			self.assertEqual(item.qty, 0.025)
			self.assertEqual(item.transfer_qty, 0.025)
			self.assertEqual(item.pcs, "3")
			self.assertEqual(item.employee, EMP_A)
			self.assertEqual(item.manufacturing_operation, "MOP-1")
			self.assertEqual(item.custom_manufacturing_work_order, "MWO-1")

	def test_customer_ownership_carried(self):
		real = frappe.db.get_value
		with patch(
			f"{MOD}.frappe.db.get_value",
			side_effect=_keyed(real, {"Batch": "Customer Diamond"}),
		):
			se = self._build([_row()], customer="CUST-1")
		for item in se.items:
			self.assertEqual(item.customer, "CUST-1")
		self.assertEqual(se.customer_voucher_type, "Customer Diamond")


class TestWriteMopLogs(_NoDB):
	def test_one_outward_row_per_consumed_stone(self):
		consume = frappe._dict(
			s_warehouse="Emp WH", t_warehouse=None, manufacturing_operation="MOP-1"
		)
		produce = frappe._dict(
			s_warehouse=None, t_warehouse="Scrap - DS", manufacturing_operation="MOP-1"
		)
		# A namespace, not a frappe._dict: ``items`` would resolve to dict.items.
		se = SimpleNamespace(items=[consume, produce, consume])
		with patch(f"{MOP_LOG}.create_mop_log_for_stock_transfer_to_mo") as create:
			mlb.write_mop_logs(se, 7)
		self.assertEqual(
			create.call_args_list,
			[call(se, consume, is_synced=True, outward=True, flow_index=7)] * 2,
		)


class TestRecheckBalance(_NoDB):
	def test_shrunk_balance_refused(self):
		pending = [frappe._dict(row=_row(qty=0.2), qty=0.2)]
		with patch(
			f"{MOD}.get_dg_balance",
			return_value={(DIAMOND, "B-D1"): _bal(DIAMOND, "B-D1", 0.1, 5)},
		):
			self.assertRaises(ValidationError, mlb.recheck_balance, "MOP-1", pending)

	def test_unchanged_balance_passes(self):
		pending = [frappe._dict(row=_row(qty=0.2), qty=0.2)]
		with patch(f"{MOD}.get_dg_balance", return_value=BALANCE):
			mlb.recheck_balance("MOP-1", pending)


def _sre(name, warehouse, remaining):
	return frappe._dict(
		name=name,
		warehouse=warehouse,
		reserved_qty=remaining,
		delivered_qty=0,
		transferred_qty=0,
		consumed_qty=0,
	)


class TestReservations(_NoDB):
	MOP = frappe._dict(name="MOP-1", manufacturing_work_order="MWO-1", company="KGJPL")

	def _find(self, rows, sres):
		with (
			patch(
				f"{LSE}._find_sre",
				side_effect=lambda owner, ref, *a: (sres[ref.idx], []),
			),
			patch(f"{LSE}._validate_sre_qty"),
		):
			return mlb.find_reservations(self.MOP, rows)

	def _reduce(self, found, replacement="R"):
		reduce = MagicMock(
			side_effect=lambda owner, ref, sre, qty, table: (
				f"{sre.name}-{replacement}" if replacement else None
			)
		)
		db = MagicMock()
		with (
			patch(f"{LSE}._reduce_sre", reduce),
			patch(f"{MOD}.frappe.db.set_value", db.set_value),
		):
			mlb.reduce_reservations(found)
		return reduce, db.set_value

	def test_find_uses_the_operation_as_stand_in_owner(self):
		owners = []
		with (
			patch(
				f"{LSE}._find_sre",
				side_effect=lambda owner, ref, *a: (
					owners.append((owner, ref)) or (_sre("SRE-1", "Emp WH", 0.3), [])
				),
			),
			patch(f"{LSE}._validate_sre_qty"),
		):
			found = mlb.find_reservations(self.MOP, [_row(1)])
		owner, ref = owners[0]
		self.assertEqual(
			(owner.doctype, owner.name, owner.company),
			("Manufacturing Operation", "MOP-1", "KGJPL"),
		)
		# The row's operation goes to the helper for its op-matched preference.
		self.assertEqual(ref.manufacturing_operation, "MOP-1")
		self.assertEqual(found[0].s_warehouse, "Emp WH")

	def test_reduces_with_each_rows_stock_entry_as_owner(self):
		found = self._find(
			[_row(1, qty=0.01), _row(2, GEMSTONE, "B-G1", qty=0.5)],
			{1: _sre("SRE-1", "Emp WH", 0.3), 2: _sre("SRE-2", "Emp WH", 1.25)},
		)
		for entry in found:
			entry.se = frappe._dict(name=f"STE-{entry.row.idx}")
		reduce, set_value = self._reduce(found)
		self.assertEqual(reduce.call_count, 2)
		owners = {c.args[2].name: c.args[0].name for c in reduce.call_args_list}
		self.assertEqual(owners, {"SRE-1": "STE-1", "SRE-2": "STE-2"})
		# A replacement exists, so nothing is stamped by hand.
		set_value.assert_not_called()

	def test_used_up_reservation_stamped_with_the_entry(self):
		found = self._find([_row(1, qty=0.3)], {1: _sre("SRE-1", "Emp WH", 0.3)})
		found[0].se = frappe._dict(name="STE-1")
		_reduce, set_value = self._reduce(found, replacement=None)
		set_value.assert_called_once_with(
			"Stock Reservation Entry",
			"SRE-1",
			"employee_ir",
			"STE-1",
			update_modified=False,
		)

	def test_spent_reservation_left_alone(self):
		found = self._find([_row(1)], {1: _sre("SRE-1", "Emp WH", 0)})
		found[0].se = frappe._dict(name="STE-1")
		reduce, set_value = self._reduce(found)
		reduce.assert_not_called()
		set_value.assert_not_called()
		self.assertTrue(found[0].spent)
		self.assertEqual(found[0].s_warehouse, "Emp WH")

	def test_two_rows_on_one_reservation_refused(self):
		shared = _sre("SRE-1", "Emp WH", 0.5)
		self.assertThrows(
			lambda: self._find(
				[_row(1), _row(2, batch_no="B-D0", pcs=0)], {1: shared, 2: shared}
			),
			"SRE-1",
		)

	def test_reductions_run_in_lock_order(self):
		found = self._find(
			[_row(1, GEMSTONE, "B-G1"), _row(2, DIAMOND, "B-D1")],
			{1: _sre("SRE-G", "Emp WH", 1.25), 2: _sre("SRE-D", "Emp WH", 0.3)},
		)
		for entry in found:
			entry.se = frappe._dict(name="STE-1")
		reduce, _set_value = self._reduce(found)
		self.assertEqual(
			[c.args[2].name for c in reduce.call_args_list], ["SRE-D", "SRE-G"]
		)


class TestBookOrder(_NoDB):
	"""Entries are named before the reservations are stamped, and submitted after."""

	def test_insert_reduce_submit_then_mop_logs(self):
		events = []
		mop = frappe._dict(
			name="MOP-1",
			company="KGJPL",
			manufacturer="Labh",
			manufacturing_work_order="MWO-1",
		)
		rows = [_row(1, employee=EMP_A), _row(2, GEMSTONE, "B-G1", employee=EMP_B)]
		pending = [
			frappe._dict(row=rows[0], qty=0.01, spent=False),
			frappe._dict(row=rows[1], qty=0.01, spent=False),
		]

		def build(_mop, employee, group):
			se = MagicMock()
			se.name = f"STE-{employee[-3:]}"
			se.insert.side_effect = lambda: events.append(("insert", se.name))
			se.submit.side_effect = lambda: events.append(("submit", se.name))
			return se

		def reduce(found):
			events.append(("reduce", tuple(e.se.name for e in found)))

		real = frappe.db.get_value
		with (
			patch(f"{MOD}.validate_booking"),
			patch(f"{MOD}.find_reservations", return_value=pending),
			patch(f"{MOD}.resolve_loss_item", return_value="L-1"),
			patch(f"{MOD}.build_loss_stock_entry", side_effect=build),
			patch(f"{MOD}.reduce_reservations", side_effect=reduce),
			patch(
				f"{MOD}.recheck_balance",
				side_effect=lambda *a: events.append(("recheck",)),
			),
			patch(
				f"{MOD}.frappe.db.get_value",
				side_effect=_keyed(real, {"Manufacturing Operation": "MOP-1"}),
			),
			patch(f"{MOP_LOG}.get_last_mop_index", return_value=4),
			patch(
				f"{MOD}.write_mop_logs",
				side_effect=lambda se, idx: events.append(("log", se.name, idx)),
			),
		):
			entries = mlb.book(mop, rows)

		self.assertEqual(len(entries), 2)
		self.assertEqual(
			events,
			[
				("insert", "STE-680"),
				("insert", "STE-675"),
				("reduce", ("STE-680", "STE-675")),
				("submit", "STE-680"),
				("submit", "STE-675"),
				("recheck",),
				("log", "STE-680", 4),
				("log", "STE-675", 4),
			],
		)
		for entry in entries:
			self.assertTrue(entry.flags.skip_mwo_reservation)


def _entry(**extra):
	base = dict(
		name="STE-1",
		stock_entry_type="Process Loss",
		employee_ir=None,
		manufacturing_operation="MOP-1",
		flags=frappe._dict(),
	)
	base.update(extra)
	return frappe._dict(base)


class TestIsLossEntry(_NoDB):
	def _check(self, doc, logged=True):
		db = MagicMock()
		db.sql.return_value = [("LOG-1",)] if logged else []
		with patch(f"{MOD}.frappe.db", db):
			return mlb.is_loss_entry(doc), db.sql

	def test_dialog_entry_recognised(self):
		result, sql = self._check(_entry())
		self.assertTrue(result)
		self.assertIn("qty_change < 0", sql.call_args.args[0])

	def test_employee_ir_loss_entry_ignored(self):
		result, sql = self._check(_entry(employee_ir="EIR-1"))
		self.assertFalse(result)
		sql.assert_not_called()

	def test_other_entry_types_ignored(self):
		result, sql = self._check(
			_entry(stock_entry_type="Material Transfer (WORK ORDER)")
		)
		self.assertFalse(result)
		sql.assert_not_called()

	def test_process_loss_without_mop_logs_ignored(self):
		# Main Slip / warehouse receive losses: Process Loss on an operation, no MOP Log rows.
		result, _sql = self._check(_entry(), logged=False)
		self.assertFalse(result)


def _snap(owner, batch="B-D1", **extra):
	data = {
		"employee_ir": owner,
		"original_reserved_qty": 0.3,
		"original_delivered_qty": 0,
		"batch_no": batch,
	}
	data.update(extra)
	return json.dumps(data)


def _sre_row(name, docstatus, employee_ir, snapshot, item_code=DIAMOND):
	return frappe._dict(
		name=name,
		docstatus=docstatus,
		item_code=item_code,
		employee_ir=employee_ir,
		custom_replaced_sre_snapshot=snapshot,
	)


class TestMarkedReservations(_NoDB):
	def _classify(self, rows):
		db = MagicMock()
		db.sql.return_value = rows
		with patch(f"{MOD}.frappe.db", db):
			return mlb._marked_reservations("STE-1")

	def test_live_replacement(self):
		live, superseded, used_up = self._classify(
			[_sre_row("SRE-2", 1, "STE-1", _snap("STE-1"))]
		)
		self.assertEqual([r.name for r in live], ["SRE-2"])
		self.assertEqual((superseded, used_up), ([], []))

	def test_replacement_reduced_again_is_superseded(self):
		_live, superseded, _used_up = self._classify(
			[
				# Ours, cancelled by a later loss; the later one's replacement is not ours.
				_sre_row("SRE-2", 2, "STE-1", _snap("STE-1")),
				_sre_row(
					"SRE-3", 1, "STE-9", _snap("STE-9", previous_employee_ir="STE-1")
				),
			]
		)
		self.assertEqual([r.name for r in superseded], ["SRE-2"])

	def test_relocated_copy_is_not_superseded(self):
		# A relocation cancels and copies the reservation, markers included.
		live, superseded, _used_up = self._classify(
			[
				_sre_row("SRE-2", 2, "STE-1", _snap("STE-1")),
				_sre_row("SRE-4", 1, "STE-1", _snap("STE-1")),
			]
		)
		self.assertEqual([r.name for r in live], ["SRE-4"])
		self.assertEqual(superseded, [])

	def test_used_up_original(self):
		# Stamped after cancel; its snapshot (if any) is an earlier owner's.
		_live, superseded, used_up = self._classify(
			[_sre_row("SRE-1", 2, "STE-1", _snap("EIR-7"))]
		)
		self.assertEqual([r.name for r in used_up], ["SRE-1"])
		self.assertEqual(superseded, [])

	def test_previous_owner_key_does_not_match_the_like(self):
		# The LIKE arm must not read a later owner's "previous_*" keys as this entry's marker.
		self.assertNotIn(
			'"employee_ir": "STE-1"',
			_snap(
				"STE-9", previous_employee_ir="STE-1", previous_snapshot=_snap("STE-1")
			),
		)


class TestGuardLossEntryCancel(_NoDB):
	def _guard(self, status="WIP", current="MOP-1", superseded=()):
		doc = _entry()
		real = frappe.db.get_value
		with (
			patch(f"{MOD}.is_loss_entry", return_value=True),
			patch(
				f"{MOD}.frappe.db.get_value",
				side_effect=_keyed(
					real,
					{
						"Manufacturing Operation": frappe._dict(
							name="MOP-1",
							status=status,
							manufacturing_work_order="MWO-1",
						),
						"Manufacturing Work Order": current,
					},
				),
			),
			patch(
				f"{MOD}._marked_reservations",
				return_value=([], [frappe._dict(name=n) for n in superseded], []),
			),
		):
			mlb.guard_loss_entry_cancel(doc)
		return doc

	def test_current_operation_cancels_and_flags_the_entry(self):
		doc = self._guard()
		self.assertTrue(doc.flags[mlb.LOSS_ENTRY_FLAG])

	def test_finished_operation_refused(self):
		self.assertThrows(lambda: self._guard(status="Finished"), "MOP-1", "Finished")

	def test_moved_on_operation_refused(self):
		self.assertThrows(lambda: self._guard(current="MOP-2"), "MOP-2")

	def test_superseded_reservation_refused(self):
		self.assertThrows(lambda: self._guard(superseded=["SRE-2"]), "SRE-2")

	def test_other_entries_untouched(self):
		doc = _entry()
		with patch(f"{MOD}.is_loss_entry", return_value=False):
			mlb.guard_loss_entry_cancel(doc)
		self.assertNotIn(mlb.LOSS_ENTRY_FLAG, doc.flags)


class TestRestoreLossEntryReservations(_NoDB):
	def test_used_up_read_before_restore_then_rebuilt(self):
		events = []
		doc = _entry()
		doc.flags[mlb.LOSS_ENTRY_FLAG] = True
		with (
			patch(
				f"{MOD}._marked_reservations",
				side_effect=lambda name: events.append("read")
				or ([], [], [frappe._dict(name="SRE-1")]),
			),
			patch(
				f"{LSE}._restore_reduced_sres",
				side_effect=lambda se: events.append(("restore", se.name)),
			),
			patch(
				f"{MOD}.rebuild_used_up_reservation",
				side_effect=lambda sre, se: events.append(("rebuild", sre, se)),
			),
			patch(
				f"{MOP_LOG}.recalculate_manufacturing_operation_weights",
				side_effect=lambda mop, prefixes: events.append(
					("recalc", mop, prefixes)
				),
			),
		):
			mlb.restore_loss_entry_reservations(doc)
		self.assertEqual(
			events,
			[
				"read",
				("restore", "STE-1"),
				("rebuild", "SRE-1", "STE-1"),
				("recalc", "MOP-1", ("diamond", "gemstone")),
			],
		)

	def test_unflagged_entry_untouched(self):
		with patch(f"{LSE}._restore_reduced_sres") as restore:
			mlb.restore_loss_entry_reservations(_entry())
		restore.assert_not_called()


class TestPreviousMarkers(_NoDB):
	"""A reduction of an already-reduced reservation hands the earlier owner's markers back."""

	def _reduce(self, **markers):
		sre = SimpleNamespace(
			name="SRE-2",
			warehouse="WH",
			reserved_qty=0.3,
			delivered_qty=0.0,
			transferred_qty=0.0,
			consumed_qty=0.0,
			available_qty=0.3,
			voucher_qty=0.3,
			voucher_type="Material Request",
			voucher_no="MR-1",
			voucher_detail_no="MRI-1",
			reservation_based_on="Qty",
			sb_entries=[],
			cancel=MagicMock(),
			**markers,
		)
		clone = SimpleNamespace(
			sb_entries=[],
			flags=SimpleNamespace(ignore_permissions=False),
			insert=MagicMock(),
			submit=MagicMock(),
			name="SRE-3",
		)
		with patch(f"{LSE}.frappe.copy_doc", return_value=clone):
			loss_stock_entry._reduce_sre(
				SimpleNamespace(name="STE-9", doctype="Stock Entry"),
				frappe._dict(idx=1, batch_no="B-D1"),
				sre,
				0.1,
				"Loss Details",
			)
		return json.loads(clone.custom_replaced_sre_snapshot)

	def test_earlier_markers_kept(self):
		snapshot = self._reduce(
			employee_ir="STE-1", custom_replaced_sre_snapshot=_snap("STE-1")
		)
		self.assertEqual(snapshot["employee_ir"], "STE-9")
		self.assertEqual(snapshot["previous_employee_ir"], "STE-1")
		self.assertEqual(snapshot["previous_snapshot"], _snap("STE-1"))

	def test_pristine_reservation_adds_nothing(self):
		snapshot = self._reduce()
		self.assertNotIn("previous_employee_ir", snapshot)
		self.assertNotIn("previous_snapshot", snapshot)

	def test_restore_hands_markers_back(self):
		snapshot = _snap(
			"STE-9", previous_employee_ir="STE-1", previous_snapshot=_snap("STE-1")
		)
		reduced = SimpleNamespace(
			name="SRE-3",
			available_qty=0.2,
			sb_entries=[],
			cancel=MagicMock(),
		)
		restored = SimpleNamespace(
			sb_entries=[],
			flags=SimpleNamespace(ignore_permissions=False),
			insert=MagicMock(),
			submit=MagicMock(),
			employee_ir="STE-9",
			custom_replaced_sre_snapshot=snapshot,
		)
		db = MagicMock()
		db.sql.return_value = [
			frappe._dict(name="SRE-3", custom_replaced_sre_snapshot=snapshot)
		]
		with (
			patch(f"{LSE}.frappe.db", db),
			patch(f"{LSE}.frappe.get_doc", return_value=reduced),
			patch(f"{LSE}.frappe.copy_doc", return_value=restored),
			patch(f"{LSE}._reservation_voucher_qty", return_value=0.3),
		):
			loss_stock_entry._restore_reduced_sres(SimpleNamespace(name="STE-9"))
		self.assertEqual(restored.employee_ir, "STE-1")
		self.assertEqual(restored.custom_replaced_sre_snapshot, _snap("STE-1"))
		self.assertEqual(restored.reserved_qty, 0.3)
		restored.submit.assert_called_once()


class TestBookingPermission(_NoDB):
	"""Stock Entry create alone is not enough: the user must also read the operation."""

	def _perm(self, se_create=True, mop_read=True):
		calls = []

		def has_permission(doctype, ptype="read", doc=None, throw=False, **kwargs):
			calls.append((doctype, ptype, doc))
			allowed = se_create if doctype == "Stock Entry" else mop_read
			if not allowed and throw:
				raise frappe.PermissionError(doctype)
			return allowed

		return patch(f"{MOD}.frappe.has_permission", side_effect=has_permission), calls

	def test_operation_read_checked_on_the_operation_itself(self):
		perm, calls = self._perm()
		with perm:
			self.assertTrue(mlb.has_booking_permission("MOP-1"))
		self.assertIn(("Stock Entry", "create", None), calls)
		self.assertIn(("Manufacturing Operation", "read", "MOP-1"), calls)

	def test_button_hidden_without_operation_read(self):
		perm, _calls = self._perm(mop_read=False)
		with perm, patch(f"{MOD}.get_operation") as get_operation:
			self.assertFalse(mlb.can_book_loss("MOP-1"))
		get_operation.assert_not_called()

	def test_context_refused_without_operation_read(self):
		perm, _calls = self._perm(mop_read=False)
		with perm, patch(f"{MOD}.get_operation") as get_operation:
			self.assertRaises(
				frappe.PermissionError, mlb.get_loss_booking_context, "MOP-1"
			)
		get_operation.assert_not_called()

	def test_booking_refused_without_operation_read(self):
		perm, _calls = self._perm(mop_read=False)
		with perm, patch(f"{MOD}.book") as book:
			self.assertRaises(frappe.PermissionError, mlb.book_loss, "MOP-1", "[]")
		book.assert_not_called()

	def test_booking_refused_without_stock_entry_create(self):
		perm, _calls = self._perm(se_create=False)
		with perm, patch(f"{MOD}.book") as book:
			self.assertRaises(frappe.PermissionError, mlb.book_loss, "MOP-1", "[]")
		book.assert_not_called()


class TestOwnerLabel(_NoDB):
	def test_employee_ir_wording_unchanged_for_stand_ins(self):
		self.assertEqual(
			loss_stock_entry._owner_label(frappe._dict(name="EIR-1")),
			"Employee IR EIR-1",
		)
		self.assertEqual(
			loss_stock_entry._owner_label(
				MagicMock(name="x", doctype=MagicMock())
			).split()[0:2],
			["Employee", "IR"],
		)

	def test_stock_entry_and_operation_named_as_themselves(self):
		self.assertEqual(
			loss_stock_entry._owner_label(
				frappe._dict(doctype="Stock Entry", name="STE-1")
			),
			"Stock Entry STE-1",
		)
		self.assertEqual(
			loss_stock_entry._owner_label(
				frappe._dict(doctype="Manufacturing Operation", name="MOP-1")
			),
			"Manufacturing Operation MOP-1",
		)
