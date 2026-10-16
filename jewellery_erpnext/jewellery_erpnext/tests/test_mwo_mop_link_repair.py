# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""The work order <-> operation link audit and its repair patch, against a REAL database.

WHAT IS PINNED
--------------
``mop_lineage_audit.audit_mwo_mop_consistency`` reports one seeded row per class, and
``patches/repair_mwo_mop_link_labels`` -- which plans from the very same detectors -- repairs exactly
the repairable ones: a dry run writes nothing yet lists every write the apply then makes, an apply
fixes each class, a second apply changes nothing, Process Loss and manual Stock Entries (and the
manual entry's ledger rows) stay as they are, a repeat flag is cleared but never set, a failing or
drifted case is isolated -- except a repeat-flag case, which undoes the whole run, and an error that
ended the transaction, which is re-raised -- and the patch refuses to run inside the EOD lock or a
reconciliation window.

THE WORLD
---------
A receive row was labelled with work order A while its casting operation belongs to work order B
(they could be siblings; here they also have different orders so the order re-derivation shows).
Around it: the casting Issue that put the operation on a tree, the injection Stock Entry and MOP
Log rows that copied the wrong label, a Department IR row with the same mistake, B's pointer left
on the finished operation while its successor is open, split children of A that copied A's
pointer, a draft pointing at an operation that no longer exists, a properly cancelled work order
pointing at B's operation, a receive whose repeat flag only the mislabelled row justified, a Process
Loss entry spanning both work orders, a manual Stock Entry with a stale header operation and a
ledger row of its own, a cancelled reservation, a Subcontracting Log, a MOP Log row older than its
(re-issued) operation and one whose operation no longer exists. Held back from the relabels: a
receive row and an injection entry whose operation is a month newer than the document, a receive
row and its injection entry whose old label still keys an active reservation, and a correctly
labelled casting receive row without a tree.

WHY db_insert
-------------
The controllers now refuse every one of these states, so they can only be written straight to the
table -- which is also what keeps this module independent of ``create_test_data`` masters: every
name carries a fresh ``TMML-<token>`` prefix and nothing else on the site is read. The repair
itself runs through its real code paths (``run``, the shared detectors, ``get_repeat_work_orders``).

TRANSACTIONS
------------
Each test runs inside a savepoint that ``tearDown`` rolls back, and ``frappe.db.commit`` is stubbed:
``IntegrationTestCase`` only rolls back once per class, and the repair is site-wide, so a test must
never leave a repaired (or seeded) world behind for the next one. The site may hold unrelated
mismatches from other modules' data; assertions therefore look only at this world's rows.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from jewellery_erpnext import mop_lineage_audit as audit
from jewellery_erpnext.patches import repair_mwo_mop_link_labels as repair

EOD_LOCK = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock.is_eod_sync_locked"
RESERVATION_TYPES = ["Material Transfer (WORK ORDER)", "Material Receive (WORK ORDER)"]
SAVEPOINT = "tmml_test_case"


class World:
	"""One isolated fixture world; every row it writes is named ``<prefix>-<KIND>-<NN>``."""

	def __init__(self):
		self.prefix = f"TMML-{frappe.generate_hash(length=6).lower()}"
		self._counters = {}

	def name(self, kind):
		self._counters[kind] = self._counters.get(kind, 0) + 1
		return f"{self.prefix}-{kind}-{self._counters[kind]:02d}"

	@staticmethod
	def insert(doctype, values):
		doc = frappe.get_doc({"doctype": doctype, **values})
		doc.db_insert()
		return doc.name

	def work_order(self, pmo, docstatus=1, status="Not Started", split_from=None):
		return self.insert(
			"Manufacturing Work Order",
			{
				"name": self.name("MWO"),
				"docstatus": docstatus,
				"status": status,
				"manufacturing_order": pmo,
				"split_from": split_from,
				"qty": 1,
			},
		)

	def operation(self, mwo, status, previous=None):
		return self.insert(
			"Manufacturing Operation",
			{
				"name": self.name("MOP"),
				"manufacturing_work_order": mwo,
				"status": status,
				"previous_mop": previous,
				"qty": 1,
			},
		)

	@staticmethod
	def point(mwo, mop):
		frappe.db.set_value(
			"Manufacturing Work Order",
			mwo,
			"manufacturing_operation",
			mop,
			update_modified=False,
		)

	@staticmethod
	def created(creation):
		"""Timestamps for a row written ``creation`` (None: now, as db_insert stamps it)."""
		if not creation:
			return {}
		return {
			"creation": creation,
			"modified": creation,
			"owner": "Administrator",
			"modified_by": "Administrator",
		}

	def employee_ir(
		self,
		typ,
		operation,
		rows,
		is_repeat_receive=0,
		worker_performance=None,
		creation=None,
	):
		"""A submitted Employee IR; ``rows`` are ``(mop, mwo, tree)``. Returns (name, [row names])."""
		name = self.insert(
			"Employee IR",
			{
				"name": self.name("EIR"),
				"docstatus": 1,
				"type": typ,
				"operation": operation,
				"subcontracting": "No",
				"is_repeat_receive": is_repeat_receive,
				"worker_performance": worker_performance,
				**self.created(creation),
			},
		)
		row_names = []
		for idx, (mop, mwo, tree) in enumerate(rows, 1):
			row_names.append(
				self.insert(
					"Employee IR Operation",
					{
						"name": f"{name}-R{idx}",
						"parent": name,
						"parenttype": "Employee IR",
						"parentfield": "employee_ir_operations",
						"idx": idx,
						"docstatus": 1,
						"manufacturing_operation": mop,
						"manufacturing_work_order": mwo,
						"tree_number": tree,
					},
				)
			)
		return name, row_names

	def stock_entry(
		self,
		stock_entry_type,
		header,
		rows,
		auto_created=0,
		purpose="Material Transfer",
		creation=None,
	):
		"""A submitted Stock Entry; ``rows`` are ``(mop, row work order)``. Returns (name, [row names])."""
		name = self.insert(
			"Stock Entry",
			{
				"name": self.name("SE"),
				"docstatus": 1,
				"stock_entry_type": stock_entry_type,
				"purpose": purpose,
				"auto_created": auto_created,
				**header,
				**self.created(creation),
			},
		)
		row_names = []
		for idx, (mop, row_mwo) in enumerate(rows, 1):
			row_names.append(
				self.insert(
					"Stock Entry Detail",
					{
						"name": f"{name}-R{idx}",
						"parent": name,
						"parenttype": "Stock Entry",
						"parentfield": "items",
						"idx": idx,
						"docstatus": 1,
						"item_code": f"M-{self.prefix}-GOLD",
						"qty": 1,
						"manufacturing_operation": mop,
						"custom_manufacturing_work_order": row_mwo,
					},
				)
			)
		return name, row_names

	def mop_log(self, mop, stamp, voucher_type, voucher_no, creation=None):
		values = {
			"name": self.name("LOG"),
			"manufacturing_operation": mop,
			"manufacturing_work_order": stamp,
			"voucher_type": voucher_type,
			"voucher_no": voucher_no,
			"item_code": f"M-{self.prefix}-GOLD",
			"qty_change": 1,
			"qty_after_transaction": 1,
			"qty_after_transaction_item_based": 1,
			"qty_after_transaction_batch_based": 1,
			"is_cancelled": 0,
			"is_synced": 1,
		}
		values.update(self.created(creation))
		return self.insert("MOP Log", values)


def build_world():
	"""Seed one row per audit class (see the module docstring); returns the world with its names."""
	w = World()
	w.casting = World.insert(
		"Department Operation",
		{
			"name": f"{w.prefix} Casting",
			"operation": f"{w.prefix} Casting",
			"tree_no_reqd": 1,
		},
	)
	w.tree = f"{w.prefix}-TREE"
	w.pmo_a, w.pmo_b = f"{w.prefix}-PMO-A", f"{w.prefix}-PMO-B"

	# A: the work order the receive row was wrongly labelled with. Its own pointer is consistent.
	w.mwo_a = w.work_order(w.pmo_a)
	w.a1 = w.operation(w.mwo_a, "Finished")
	w.a2 = w.operation(w.mwo_a, "Not Started", previous=w.a1)
	World.point(w.mwo_a, w.a2)
	# B: the real owner of the received operation b1. The receive minted b2 but moved A's pointer,
	# so B still points at the finished b1.
	w.mwo_b = w.work_order(w.pmo_b)
	w.b0 = w.operation(w.mwo_b, "Finished")
	w.b1 = w.operation(w.mwo_b, "Finished", previous=w.b0)
	w.b2 = w.operation(w.mwo_b, "WIP", previous=w.b1)
	World.point(w.mwo_b, w.b1)
	# C: a SUBMITTED work order pointing at B's operation -- reported, never rewritten.
	w.mwo_c = w.work_order(w.pmo_a)
	World.point(w.mwo_c, w.b2)
	# Split children of A that copied A's pointer: one still a draft, one discarded.
	w.split_draft = w.work_order(
		w.pmo_a, docstatus=0, status="Not Started", split_from=w.mwo_a
	)
	w.split_discarded = w.work_order(
		w.pmo_a, docstatus=2, status="Not Started", split_from=w.mwo_a
	)
	World.point(w.split_draft, w.a2)
	World.point(w.split_discarded, w.a2)
	# A draft pointing at an operation that no longer exists: nothing owns the pointer either.
	w.gone_mop = f"{w.prefix}-MOP-GONE"
	w.dangling_draft = w.work_order(w.pmo_a, docstatus=0, status="Not Started")
	World.point(w.dangling_draft, w.gone_mop)
	# A PROPERLY cancelled work order (on_cancel ran) still pointing at B's operation: history.
	w.cancelled_foreign = w.work_order(w.pmo_a, docstatus=2, status="Cancelled")
	World.point(w.cancelled_foreign, w.b2)

	# The casting Issue put b1 on the tree; the receive row then named A next to b1 and lost it.
	w.issue_eir, _rows = w.employee_ir("Issue", w.casting, [(w.b1, w.mwo_b, w.tree)])
	w.receive_eir, (w.receive_row,) = w.employee_ir(
		"Receive", w.casting, [(w.b1, w.mwo_a, None)]
	)
	# E's repeat flag rests only on the mislabelled row; F becomes a repeat once the row says B.
	w.repeat_eir, _rows = w.employee_ir(
		"Receive",
		w.casting,
		[(w.a1, w.mwo_a, w.tree)],
		is_repeat_receive=1,
		worker_performance="NO",
	)
	w.fresh_eir, _rows = w.employee_ir("Receive", w.casting, [(w.b0, w.mwo_b, w.tree)])

	w.dir = World.insert(
		"Department IR", {"name": w.name("DIR"), "docstatus": 1, "type": "Receive"}
	)
	w.dir_row = World.insert(
		"Department IR Operation",
		{
			"name": f"{w.dir}-R1",
			"parent": w.dir,
			"parenttype": "Department IR",
			"parentfield": "department_ir_operation",
			"idx": 1,
			"docstatus": 1,
			"manufacturing_operation": w.b0,
			"manufacturing_work_order": w.mwo_a,
			"parent_manufacturing_order": w.pmo_a,
		},
	)

	w.injection_se, (w.injection_row,) = w.stock_entry(
		"Material Transfer (WORK ORDER)",
		{
			"manufacturing_work_order": w.mwo_a,
			"manufacturing_operation": w.b1,
			"manufacturing_order": w.pmo_a,
			"employee_ir": w.receive_eir,
			"custom_eir_operation_row": w.receive_row,
		},
		[(w.b1, w.mwo_a)],
		auto_created=1,
	)
	# Manual entry, header operation of another work order: row 1 falls back to the header label
	# (row rule), row 2 is labelled right but differs from the reserving header (header rule).
	w.manual_se, w.manual_rows = w.stock_entry(
		"Material Transfer (WORK ORDER)",
		{
			"manufacturing_work_order": w.mwo_a,
			"manufacturing_operation": w.b0,
			"manufacturing_order": w.pmo_a,
		},
		[(w.b0, None), (w.b0, w.mwo_b)],
	)
	# Process Loss: one entry for both work orders, header named after the first row.
	w.loss_se, w.loss_rows = w.stock_entry(
		"Process Loss",
		{
			"manufacturing_work_order": w.mwo_a,
			"manufacturing_operation": w.a1,
			"manufacturing_order": w.pmo_a,
		},
		[(w.a1, w.mwo_a), (w.b0, w.mwo_b)],
		auto_created=1,
		purpose="Repack",
	)

	w.sre = World.insert(
		"Stock Reservation Entry",
		{
			"name": w.name("SRE"),
			"docstatus": 2,
			"status": "Cancelled",
			"voucher_type": "Sales Order",
			"voucher_no": f"{w.prefix}-SO",
			"item_code": f"M-{w.prefix}-GOLD",
			"manufacturing_work_order": w.mwo_a,
			"manufacturing_operation": w.b1,
			"reserved_qty": 1,
		},
	)
	w.subcontracting_log = World.insert(
		"Subcontracting Log",
		{
			"name": w.name("SCL"),
			"reference_doctype": "Stock Entry",
			"reference_docname": w.injection_se,
			"manufacturing_work_order_name": w.mwo_a,
			"manufacturing_operation_name": w.b1,
		},
	)

	w.log_se = w.mop_log(w.b1, w.mwo_a, "Stock Entry", w.injection_se)
	w.log_carry = w.mop_log(w.b2, w.mwo_a, "Manufacturing Operation", w.b1)
	w.log_ok = w.mop_log(w.b1, w.mwo_b, "Employee IR", w.receive_eir)
	# A re-issued short name: the operation is new, the row a month older and stamped with a work
	# order that no longer exists.
	w.mwo_e = w.work_order(w.pmo_b)
	w.e1 = w.operation(w.mwo_e, "WIP")
	w.log_reuse = w.mop_log(
		w.e1,
		f"{w.prefix}-MWO-GONE",
		"Manufacturing Work Order",
		"gone",
		creation=add_to_date(now_datetime(), days=-30),
	)
	w.log_orphan = w.mop_log(f"{w.prefix}-MOP-GONE", w.mwo_a, "Stock Entry", "gone")
	# The manual entry's own ledger row, on B's operation under the entry's header work order A: it
	# keeps that stamp until a person decides which half the entry really moved.
	w.log_manual = w.mop_log(w.b0, w.mwo_a, "Stock Entry", w.manual_se)

	# The same re-issued operation e1 named by a receive row and by an injection entry written a month
	# before it existed: whatever they named, it was not today's e1.
	month_ago = add_to_date(now_datetime(), days=-30)
	w.reuse_eir, (w.reuse_row,) = w.employee_ir(
		"Receive", None, [(w.e1, f"{w.prefix}-MWO-GONE", None)], creation=month_ago
	)
	w.reuse_se, (w.reuse_se_row,) = w.stock_entry(
		"Material Transfer (WORK ORDER)",
		{
			"manufacturing_work_order": w.mwo_a,
			"manufacturing_operation": w.e1,
			"manufacturing_order": w.pmo_a,
			"employee_ir": w.reuse_eir,
			"custom_eir_operation_row": w.reuse_row,
		},
		[(w.e1, None)],
		auto_created=1,
		creation=month_ago,
	)

	# F's operation received under A's label, and the reservation made from it still ACTIVE under
	# that label: the receive's (and the entry's) cancel finds it by the label, so neither moves.
	w.mwo_f = w.work_order(w.pmo_b)
	w.f1 = w.operation(w.mwo_f, "WIP")
	w.reserved_eir, (w.reserved_row,) = w.employee_ir(
		"Receive", None, [(w.f1, w.mwo_a, None)]
	)
	w.reserved_se, (w.reserved_se_row,) = w.stock_entry(
		"Material Transfer (WORK ORDER)",
		{
			"manufacturing_work_order": w.mwo_a,
			"manufacturing_operation": w.f1,
			"manufacturing_order": w.pmo_a,
			"employee_ir": w.reserved_eir,
			"custom_eir_operation_row": w.reserved_row,
		},
		[(w.f1, w.mwo_a)],
		auto_created=1,
	)
	w.active_sre = World.insert(
		"Stock Reservation Entry",
		{
			"name": w.name("SRE"),
			"docstatus": 1,
			"status": "Reserved",
			"voucher_type": "Sales Order",
			"voucher_no": f"{w.prefix}-SO",
			"item_code": f"M-{w.prefix}-GOLD",
			"manufacturing_work_order": w.mwo_a,
			"manufacturing_operation": w.f1,
			"reserved_qty": 1,
		},
	)

	# A casting receive row WITHOUT a tree whose label is right: not this defect. Its receive charged
	# no tree either, and pinning one now would only change what a cancel of it reverses.
	w.mwo_g = w.work_order(w.pmo_b)
	w.g1 = w.operation(w.mwo_g, "Finished")
	w.issue_g, _rows = w.employee_ir("Issue", w.casting, [(w.g1, w.mwo_g, w.tree)])
	w.consistent_receive_eir, (w.consistent_row,) = w.employee_ir(
		"Receive", w.casting, [(w.g1, w.mwo_g, None)]
	)
	return w


# Every field the repair may write, per document of the world (plus the ones it must not).
def _tracked(w):
	mwo_fields = ["manufacturing_operation", "status"]
	eir_fields = ["is_repeat_receive", "worker_performance", "tree_number"]
	row_fields = ["manufacturing_work_order", "tree_number", "is_finding_mwo"]
	se_fields = [
		"manufacturing_work_order",
		"manufacturing_order",
		"manufacturing_operation",
		"custom_tree_number",
	]
	sed_fields = ["manufacturing_operation", "custom_manufacturing_work_order"]
	tracked = [
		("Manufacturing Work Order", n, mwo_fields)
		for n in (
			w.mwo_a,
			w.mwo_b,
			w.mwo_c,
			w.split_draft,
			w.split_discarded,
			w.dangling_draft,
			w.cancelled_foreign,
			w.mwo_e,
			w.mwo_f,
			w.mwo_g,
		)
	]
	tracked += [
		("Employee IR", n, eir_fields)
		for n in (
			w.issue_eir,
			w.receive_eir,
			w.repeat_eir,
			w.fresh_eir,
			w.reuse_eir,
			w.reserved_eir,
			w.issue_g,
			w.consistent_receive_eir,
		)
	]
	tracked += [
		("Employee IR Operation", r.name, row_fields)
		for r in frappe.get_all(
			"Employee IR Operation",
			filters={"parent": ["like", f"{w.prefix}%"]},
			fields=["name"],
		)
	]
	tracked += [
		(
			"Department IR Operation",
			w.dir_row,
			["manufacturing_work_order", "parent_manufacturing_order"],
		)
	]
	tracked += [
		("Stock Entry", n, se_fields)
		for n in (w.injection_se, w.manual_se, w.loss_se, w.reuse_se, w.reserved_se)
	]
	tracked += [
		("Stock Entry Detail", n, sed_fields)
		for n in [
			w.injection_row,
			*w.manual_rows,
			*w.loss_rows,
			w.reuse_se_row,
			w.reserved_se_row,
		]
	]
	tracked += [
		("MOP Log", n, ["manufacturing_work_order", "is_cancelled"])
		for n in (
			w.log_se,
			w.log_carry,
			w.log_ok,
			w.log_reuse,
			w.log_orphan,
			w.log_manual,
		)
	]
	tracked += [
		("Stock Reservation Entry", n, ["manufacturing_work_order", "docstatus"])
		for n in (w.sre, w.active_sre)
	]
	tracked += [
		("Subcontracting Log", w.subcontracting_log, ["manufacturing_work_order_name"])
	]
	return tracked


def snapshot(w, only=None):
	"""``{(doctype, name): {field: value}}`` for the world, plus how many Comments it carries."""
	state = {
		(doctype, name): frappe.db.get_value(doctype, name, fields, as_dict=True)
		for doctype, name, fields in _tracked(w)
		if not only or (doctype, name) in only
	}
	state["comments"] = frappe.db.count(
		"Comment", {"reference_name": ["like", f"{w.prefix}%"]}
	)
	return state


def mine(rows, w, *keys):
	"""Rows of ``rows`` that belong to this world (any of ``keys`` carries the prefix)."""
	return [
		r for r in rows if any(str(r.get(k) or "").startswith(w.prefix) for k in keys)
	]


def writes_of(report, w):
	"""``{(doctype, name, field): (old, new)}`` the report plans / made for this world's documents."""
	out = {}
	for case in report["cases"]:
		if not case["name"].startswith(w.prefix):
			continue
		for write in case["writes"]:
			out[(write["doctype"], write["name"], write["field"])] = (
				write["old"],
				write["new"],
			)
	return out


def expected_writes(w):
	return {
		("Employee IR Operation", w.receive_row, "manufacturing_work_order"): (
			w.mwo_a,
			w.mwo_b,
		),
		("Employee IR Operation", w.receive_row, "tree_number"): (None, w.tree),
		("Employee IR", w.receive_eir, "tree_number"): (None, w.tree),
		("Department IR Operation", w.dir_row, "manufacturing_work_order"): (
			w.mwo_a,
			w.mwo_b,
		),
		("Department IR Operation", w.dir_row, "parent_manufacturing_order"): (
			w.pmo_a,
			w.pmo_b,
		),
		("Stock Entry", w.injection_se, "manufacturing_work_order"): (w.mwo_a, w.mwo_b),
		("Stock Entry", w.injection_se, "manufacturing_order"): (w.pmo_a, w.pmo_b),
		("Stock Entry", w.injection_se, "custom_tree_number"): (None, w.tree),
		("Stock Entry Detail", w.injection_row, "custom_manufacturing_work_order"): (
			w.mwo_a,
			w.mwo_b,
		),
		("MOP Log", w.log_se, "manufacturing_work_order"): (w.mwo_a, w.mwo_b),
		("MOP Log", w.log_carry, "manufacturing_work_order"): (w.mwo_a, w.mwo_b),
		("Manufacturing Work Order", w.split_draft, "manufacturing_operation"): (
			w.a2,
			None,
		),
		("Manufacturing Work Order", w.split_draft, "status"): ("Not Started", "Draft"),
		("Manufacturing Work Order", w.split_discarded, "manufacturing_operation"): (
			w.a2,
			None,
		),
		("Manufacturing Work Order", w.split_discarded, "status"): (
			"Not Started",
			"Cancelled",
		),
		("Manufacturing Work Order", w.dangling_draft, "manufacturing_operation"): (
			w.gone_mop,
			None,
		),
		("Manufacturing Work Order", w.dangling_draft, "status"): (
			"Not Started",
			"Draft",
		),
		("Manufacturing Work Order", w.mwo_b, "manufacturing_operation"): (w.b1, w.b2),
		("Employee IR", w.repeat_eir, "is_repeat_receive"): (1, 0),
		("Employee IR", w.repeat_eir, "worker_performance"): ("NO", None),
	}


def _blank(value):
	return value if value not in ("", None) else None


class TestMWOMOPLinkRepair(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint(SAVEPOINT)
		self.addCleanup(self._rollback)
		patchers = (
			# The patch refuses inside these windows; a lock leaked by another module must not decide
			# this one's outcome (the refusal tests switch it on explicitly).
			patch(EOD_LOCK, return_value=False),
			patch.object(repair, "open_reconciliation_windows", return_value=[]),
			# The site's MOP Settings decide the reservation types; pin the production list.
			patch.object(
				audit,
				"mwo_mop_reservation_stock_entry_types",
				return_value=RESERVATION_TYPES,
			),
			# Real SQL, no commit: see the module docstring.
			patch.object(frappe.db, "commit", lambda *args, **kwargs: None),
		)
		for patcher in patchers:
			patcher.start()
			self.addCleanup(patcher.stop)

	def _rollback(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.db.value_cache.clear()

	def _apply(self):
		with patch("builtins.print"):
			return repair.run(dry_run=False)

	def _dry_run(self):
		with patch("builtins.print"):
			return repair.run(dry_run=True)

	# ------------------------------------------------------------------------------------------
	# audit
	# ------------------------------------------------------------------------------------------

	def test_audit_reports_one_row_per_seeded_class(self):
		baseline = audit.mwo_mop_findings()
		w = build_world()
		out = audit.audit_mwo_mop_consistency(limit=0)

		ir_rows = mine(out["ir_rows"]["rows"], w, "parent")
		self.assertEqual(
			{
				(
					r["parenttype"],
					r["row_name"],
					r["row_mwo"],
					r["owner"],
					int(r["is_name_reuse"]),
				)
				for r in ir_rows
			},
			{
				("Employee IR", w.receive_row, w.mwo_a, w.mwo_b, 0),
				("Department IR", w.dir_row, w.mwo_a, w.mwo_b, 0),
				("Employee IR", w.reuse_row, f"{w.prefix}-MWO-GONE", w.mwo_e, 1),
				("Employee IR", w.reserved_row, w.mwo_a, w.mwo_f, 0),
			},
		)
		self.assertGreaterEqual(out["ir_rows"]["name_reuse"], 1)
		self.assertEqual(
			{
				(r["name"], int(r["is_name_reuse"]))
				for r in mine(out["se_headers"]["rows"], w, "name")
			},
			{
				(w.injection_se, 0),
				(w.manual_se, 0),
				(w.reuse_se, 1),
				(w.reserved_se, 0),
			},
		)
		self.assertEqual(
			{
				(r["row_name"], r["rule"])
				for r in mine(out["se_rows"]["rows"], w, "stock_entry")
			},
			{
				(w.injection_row, "row"),
				(w.manual_rows[0], "row"),
				(w.manual_rows[1], "reservation_header"),
				(w.reuse_se_row, "row"),
				(w.reserved_se_row, "row"),
			},
			"Process Loss rows must never be judged against their header",
		)
		self.assertEqual(
			{r["name"] for r in mine(out["sre"]["rows"], w, "name")},
			{w.sre, w.active_sre},
		)
		self.assertEqual(
			{r["name"] for r in mine(out["mop_log_stamps"]["rows"], w, "name")},
			{w.log_se, w.log_carry, w.log_manual},
		)
		self.assertEqual(
			[r["name"] for r in mine(out["mop_log_name_reuse"]["rows"], w, "name")],
			[w.log_reuse],
		)
		self.assertEqual(
			out["mop_log_orphans"]["count"], baseline["mop_log"]["orphans"]["rows"] + 1
		)
		self.assertEqual(
			{
				(r["mwo"], r["docstatus"])
				for r in mine(out["pointer_foreign"]["rows"], w, "mwo")
			},
			{
				(w.mwo_c, 1),
				(w.split_draft, 0),
				(w.split_discarded, 2),
				(w.cancelled_foreign, 2),
			},
		)
		stale = mine(out["pointer_stale"]["rows"], w, "mwo")
		self.assertEqual(
			[(r["mwo"], r["pointer"], r["successor"]) for r in stale],
			[(w.mwo_b, w.b1, w.b2)],
		)
		self.assertEqual(
			{r["mwo"] for r in mine(out["pointer_on_non_submitted"]["rows"], w, "mwo")},
			{w.split_draft, w.split_discarded, w.dangling_draft, w.cancelled_foreign},
		)
		no_tree = mine(out["casting_receive_without_tree"]["rows"], w, "parent")
		self.assertEqual(
			[(r["row_name"], r["issue_tree"]) for r in no_tree],
			[(w.receive_row, w.tree)],
			"only a row labelled with another work order lost its tree to the defect",
		)
		self.assertEqual(
			[r["name"] for r in mine(out["subcontracting_log"]["rows"], w, "name")],
			[w.subcontracting_log],
		)
		self.assertEqual(
			(
				out["process_loss_multi_mwo"]["count"],
				out["process_loss_multi_mwo"]["stock_entries"],
			),
			(
				baseline["process_loss_multi_mwo"]["rows"] + 1,
				baseline["process_loss_multi_mwo"]["stock_entries"] + 1,
			),
		)
		for key, bucket in out.items():
			if bucket["rows"]:
				self.assertEqual(bucket["count"], len(bucket["rows"]), key)

	def test_audit_caps_rows_but_not_counts(self):
		build_world()
		out = audit.audit_mwo_mop_consistency(limit=1)
		self.assertLessEqual(len(out["se_rows"]["rows"]), 1)
		self.assertGreaterEqual(out["se_rows"]["count"], 3)

	def test_audit_and_preview_are_system_manager_only(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			audit.audit_mwo_mop_consistency()
		with self.assertRaises(frappe.PermissionError):
			repair.preview()

	# ------------------------------------------------------------------------------------------
	# repair
	# ------------------------------------------------------------------------------------------

	def test_dry_run_lists_every_write_and_writes_nothing(self):
		w = build_world()
		before = snapshot(w)

		report = self._dry_run()

		self.assertTrue(report["dry_run"])
		self.assertEqual(writes_of(report, w), expected_writes(w))
		self.assertEqual(
			snapshot(w), before, "a dry run must leave every row and Comment as it was"
		)
		report_only = report["report_only"]
		self.assertIn(w.manual_se, report_only["manual_stock_entries"])
		self.assertIn(w.sre, report_only["sre_cancelled"])
		self.assertIn(w.active_sre, report_only["sre_active"])
		self.assertIn(w.subcontracting_log, report_only["subcontracting_log"])
		self.assertIn(w.log_reuse, report_only["mop_log_name_reuse"])
		self.assertIn(w.mwo_c, report_only["pointer_foreign_submitted"])
		self.assertIn(w.cancelled_foreign, report_only["pointer_on_cancelled"])
		self.assertIn(w.log_manual, report_only["mop_log_unrepaired_stock_entry"])
		self.assertIn(w.reuse_row, report_only["ir_rows_name_reuse"])
		self.assertIn(w.reserved_row, report_only["ir_rows_active_reservation"])
		skipped = {
			row["name"]: row["reason"] for row in report_only["stock_entries_skipped"]
		}
		self.assertIn("newer than the entry", skipped.get(w.reuse_se, ""))
		self.assertIn("active reservation", skipped.get(w.reserved_se, ""))
		# The filled casting row is reported with what its tree never recorded.
		self.assertEqual(
			[
				(row["name"], row["employee_ir"], row["tree"])
				for row in report_only["casting_tree_filled_never_charged"]
				if row["name"].startswith(w.prefix)
			],
			[(w.receive_row, w.receive_eir, w.tree)],
		)

	def test_apply_repairs_each_class_and_a_second_run_is_a_no_op(self):
		w = build_world()
		planned = writes_of(self._dry_run(), w)
		untouched = {
			("Stock Entry", w.manual_se),
			*(("Stock Entry Detail", n) for n in w.manual_rows),
			("Stock Entry", w.loss_se),
			*(("Stock Entry Detail", n) for n in w.loss_rows),
			("Stock Reservation Entry", w.sre),
			("Stock Reservation Entry", w.active_sre),
			("Subcontracting Log", w.subcontracting_log),
			("MOP Log", w.log_ok),
			("MOP Log", w.log_reuse),
			("MOP Log", w.log_orphan),
			("MOP Log", w.log_manual),
			("Manufacturing Work Order", w.mwo_a),
			("Manufacturing Work Order", w.mwo_c),
			("Manufacturing Work Order", w.cancelled_foreign),
			("Employee IR", w.fresh_eir),
			("Employee IR", w.issue_eir),
			("Employee IR", w.consistent_receive_eir),
			("Employee IR Operation", w.consistent_row),
			("Employee IR Operation", w.reuse_row),
			("Employee IR Operation", w.reserved_row),
			("Stock Entry", w.reuse_se),
			("Stock Entry Detail", w.reuse_se_row),
			("Stock Entry", w.reserved_se),
			("Stock Entry Detail", w.reserved_se_row),
		}
		untouched_before = snapshot(w, only=untouched)

		report = self._apply()

		self.assertEqual(
			writes_of(report, w),
			planned,
			"the apply must make exactly the writes the dry run listed",
		)
		for (doctype, name, field), (_old, new) in expected_writes(w).items():
			self.assertEqual(
				_blank(frappe.db.get_value(doctype, name, field)),
				new,
				f"{doctype} {name}.{field}",
			)
		after = snapshot(w, only=untouched)
		# The world's Comment count moves (repaired documents get one); the untouched rows must not.
		after.pop("comments")
		untouched_before.pop("comments")
		self.assertEqual(
			after,
			untouched_before,
			"manual / Process Loss / cancelled / report-only rows stay as they are",
		)
		self.assertFalse(
			[
				c
				for c in report["cases"]
				if c["name"].startswith(w.prefix) and c["outcome"] != "applied"
			]
		)

		commented = set(
			frappe.get_all(
				"Comment",
				filters={
					"comment_type": "Info",
					"reference_name": ["like", f"{w.prefix}%"],
					"content": ["like", f"%{repair.REPAIR_TAG}%"],
				},
				pluck="reference_name",
			)
		)
		self.assertEqual(
			commented,
			{
				w.receive_eir,
				w.dir,
				w.injection_se,
				w.b1,
				w.b2,
				w.split_draft,
				w.split_discarded,
				w.dangling_draft,
				w.mwo_b,
				w.repeat_eir,
			},
		)

		# What is left is exactly what the patch holds back for a person.
		findings = audit.mwo_mop_findings()
		self.assertEqual(
			{r.row_name for r in mine(findings["ir_rows"], w, "parent")},
			{w.reuse_row, w.reserved_row},
		)
		self.assertEqual(
			{r.name for r in mine(findings["mop_log"]["stamps"], w, "name")},
			{w.log_manual},
		)
		self.assertFalse(mine(findings["pointer_stale"], w, "mwo"))
		self.assertEqual(
			{
				r.mwo
				for r in mine(findings["pointer_on_non_submitted"]["rows"], w, "mwo")
			},
			{w.cancelled_foreign},
		)
		self.assertFalse(mine(findings["casting_receive_without_tree"], w, "parent"))
		self.assertEqual(
			{r.name for r in mine(findings["se_headers"], w, "name")},
			{w.manual_se, w.reuse_se, w.reserved_se},
		)

		second = self._apply()
		self.assertEqual(
			second["applied"], 0, "a second run must find nothing left to repair"
		)
		self.assertFalse(writes_of(second, w))

	def test_repeat_flag_is_cleared_but_never_set(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			get_repeat_work_orders,
		)

		w = build_world()
		self._apply()

		self.assertEqual(
			frappe.db.get_value("Employee IR", w.repeat_eir, "is_repeat_receive"), 0
		)
		self.assertIsNone(
			_blank(
				frappe.db.get_value("Employee IR", w.repeat_eir, "worker_performance")
			)
		)
		# After the relabel the fresh receive and the relabelled receive share work order B at the
		# casting operation, so the Employee IR's own rule WOULD call each a repeat now...
		rows = [{"manufacturing_operation": w.b0, "manufacturing_work_order": w.mwo_b}]
		self.assertEqual(
			get_repeat_work_orders(rows, w.casting, w.fresh_eir), [w.mwo_b]
		)
		# ...but the patch only ever clears a flag.
		self.assertEqual(
			frappe.db.get_value("Employee IR", w.fresh_eir, "is_repeat_receive"), 0
		)
		self.assertEqual(
			frappe.db.get_value("Employee IR", w.receive_eir, "is_repeat_receive"), 0
		)

	def test_refuses_while_the_eod_lock_is_held(self):
		w = build_world()
		before = snapshot(w)
		with patch(EOD_LOCK, return_value=True):
			for call in (repair.execute, lambda: repair.run(dry_run=True)):
				with self.assertRaises(frappe.ValidationError) as caught:
					call()
				self.assertIn("outside the eod window", str(caught.exception).lower())
		self.assertEqual(snapshot(w), before)

	def test_refuses_while_a_reconciliation_window_is_open(self):
		w = build_world()
		before = snapshot(w)
		with patch.object(
			repair, "open_reconciliation_windows", return_value=["Casting - X"]
		):
			with self.assertRaises(frappe.ValidationError) as caught:
				repair.execute()
		self.assertIn("Casting - X", str(caught.exception))
		self.assertEqual(snapshot(w), before)

	def test_a_failing_case_rolls_back_alone_and_is_logged(self):
		w = build_world()
		real_add_comment = repair.add_comment

		def add_comment(case):
			if case.name == w.split_draft:
				raise RuntimeError("comment refused")
			return real_add_comment(case)

		with patch.object(repair, "add_comment", side_effect=add_comment), patch.object(
			repair.frappe, "log_error"
		) as log_error:
			report = self._apply()

		# Its writes ran before the Comment failed; the savepoint undid them.
		self.assertEqual(
			frappe.db.get_value(
				"Manufacturing Work Order", w.split_draft, "manufacturing_operation"
			),
			w.a2,
		)
		self.assertEqual(
			frappe.db.get_value("Manufacturing Work Order", w.split_draft, "status"),
			"Not Started",
		)
		self.assertIn(w.split_draft, [f["name"] for f in report["failed"]])
		# Other data on a shared site may fail too; this world's failure must be among the logs.
		self.assertIn(
			w.split_draft,
			[call.kwargs.get("reference_name") for call in log_error.call_args_list],
		)
		# Every other case still applied.
		self.assertIsNone(
			_blank(
				frappe.db.get_value(
					"Manufacturing Work Order",
					w.split_discarded,
					"manufacturing_operation",
				)
			)
		)
		self.assertEqual(
			frappe.db.get_value(
				"Manufacturing Work Order", w.mwo_b, "manufacturing_operation"
			),
			w.b2,
		)

	def test_a_value_that_changed_after_detection_is_skipped_not_forced(self):
		w = build_world()
		real_build_plan = repair.build_plan

		def build_plan_then_edit(findings=None):
			plan = real_build_plan(findings)
			# Someone edits the draft between detection and write.
			frappe.db.set_value(
				"Manufacturing Work Order",
				w.split_draft,
				"status",
				"Stopped",
				update_modified=False,
			)
			return plan

		with patch.object(repair, "build_plan", side_effect=build_plan_then_edit):
			report = self._apply()

		self.assertIn(w.split_draft, [d["name"] for d in report["drifted"]])
		self.assertEqual(
			frappe.db.get_value(
				"Manufacturing Work Order", w.split_draft, "manufacturing_operation"
			),
			w.a2,
		)
		self.assertEqual(
			frappe.db.get_value("Manufacturing Work Order", w.split_draft, "status"),
			"Stopped",
		)
		self.assertIsNone(
			_blank(
				frappe.db.get_value(
					"Manufacturing Work Order",
					w.split_discarded,
					"manufacturing_operation",
				)
			)
		)

	def test_a_failing_repeat_flag_case_undoes_the_whole_run(self):
		"""A repeat-flag verdict exists only in the run whose relabels produced it -- a second run finds
		the rows already relabelled and would never plan it -- so a class-5 case that fails takes the
		whole run back with it and the run raises: the next migrate starts from scratch."""
		w = build_world()
		before = snapshot(w)
		real_add_comment = repair.add_comment

		def add_comment(case):
			if case.name == w.repeat_eir:
				raise RuntimeError("comment refused")
			return real_add_comment(case)

		with patch.object(repair, "add_comment", side_effect=add_comment), patch.object(
			repair.frappe, "log_error"
		) as log_error:
			with self.assertRaises(frappe.ValidationError) as caught:
				self._apply()

		self.assertIn(w.repeat_eir, str(caught.exception))
		self.assertEqual(
			snapshot(w),
			before,
			"every class of the run is rolled back, Comments included",
		)
		self.assertIn(
			w.repeat_eir,
			[call.kwargs.get("reference_name") for call in log_error.call_args_list],
		)

	def test_an_error_that_ended_the_transaction_is_re_raised(self):
		"""A deadlock or a lost connection rolls the whole transaction back, savepoint included:
		``ROLLBACK TO SAVEPOINT`` then fails, and the case's ORIGINAL error must surface -- not the
		savepoint error, and not a quiet "failed" while every earlier case is silently gone."""
		w = build_world()
		case = repair._case("pointer_stale", "Manufacturing Work Order", w.mwo_b)
		real_rollback = frappe.db.rollback

		def rollback(*args, save_point=None, **kwargs):
			if save_point and save_point.startswith("mwo_mop_link_"):
				raise Exception(f"SAVEPOINT {save_point} does not exist")
			return real_rollback(*args, save_point=save_point, **kwargs)

		with (
			patch.object(
				repair, "add_comment", side_effect=RuntimeError("Deadlock found")
			),
			patch.object(frappe.db, "rollback", side_effect=rollback),
			patch.object(repair.frappe, "log_error") as log_error,
		):
			with self.assertRaises(RuntimeError) as caught:
				repair.apply_case(case)

		self.assertIn("Deadlock found", str(caught.exception))
		log_error.assert_not_called()
