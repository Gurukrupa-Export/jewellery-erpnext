# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Metal Conversions Chain Subcontracting (doc_events/chain_subcontracting.py).

Pure-logic suite: every master read and every Stock Entry / Purchase Order is stubbed, so it
runs on any site and says nothing about the stock ledger -- the desk walk-through in the PR
covers that. Stubs are keyed on what they answer (never a blanket ``frappe.db.get_value``),
and ``flt`` is replaced by a deterministic shim, because frappe's own returns 0.0 when the
rounding setting cannot be read.

Lock order is asserted by recording the calls into ``lock_order``; whether two real requests
deadlock is not something a stub can show.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import frappe
from frappe import _
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions import (
	metal_conversions as mc,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events import (
	chain_subcontracting as cs,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events import (
	lanes as lanes_mod,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events import (
	melting_loss,
)
from jewellery_erpnext.patches import add_metal_conversion_chain_sc as chain_sc_patch
from jewellery_erpnext.patches import add_metal_conversion_workflow as workflow_patch

_REAL_GET_ALL = frappe.get_all

_LOCK_ORDER = "jewellery_erpnext.jewellery_erpnext.lock_order"
_SCRAP_WAREHOUSE = "jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion.get_scrap_warehouse"

EMPLOYEE_DEPT = {"EMP-1": "Chain - GK", "EMP-2": "Polish - GK", "EMP-X": None}
DEPT_RM = {"Chain - GK": "Chain RM - GK", "Polish - GK": "Polish RM - GK"}
SUPPLIER_RM = {"SUP-1": "SUP-1 MSL WH - GK"}


def _det_flt(value, precision=None, rounding_method=None):
	try:
		num = float(value or 0)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


def _row(batch, qty):
	return frappe._dict(batch=batch, qty=qty)


class _Doc(frappe._dict):
	"""A Metal Conversions stand-in: ``_dict`` field access plus the Document calls used."""

	def get_doc_before_save(self):
		return self.get("_before")

	def is_new(self):
		return bool(self.get("_new"))

	def set(self, key, value):
		self[key] = value


def _chain_doc(**fields):
	doc = _Doc(
		name="MCON-CH-1",
		company="KG GK Jewellers Private Limited",
		branch="Main",
		employee="EMP-1",
		department="Chain - GK",
		manufacturer="Shubh",
		date="2026-10-12",
		source_warehouse="Chain RM - GK",
		target_warehouse="Chain RM - GK",
		is_chain_manufacturing="Yes",
		is_chain_subcontracting=1,
		to_employee="EMP-2",
		chain_supplier="SUP-1",
		source_item="M-G-22KT-91.6-Y",
		source_qty=10.0,
		target_item=None,
		target_qty=0,
		multiple_metal_converter=0,
		is_melting_loss=0,
		source_batch_details=[_row("B-1", 6.0), _row("B-2", 4.0)],
		workflow_state="Draft",
	)
	doc.update(fields)
	doc.meta = SimpleNamespace(get_label=lambda f: f.replace("_", " ").title())
	doc.db_set = MagicMock()
	return doc


def _get_value(doctype, name=None, fieldname=None, *args, **kwargs):
	"""Keyed answers for the master reads the chain module makes."""
	if doctype == "Employee" and fieldname == "department":
		return EMPLOYEE_DEPT.get(name)
	if doctype == "Department" and fieldname == "manufacturer":
		return "Shubh"
	raise AssertionError(f"unexpected get_value({doctype!r}, {name!r}, {fieldname!r})")


class _ChainCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# The first _() reads Translation through get_all; warm it before any stub.
		_("warm")

	def setUp(self):
		self._patches = [
			patch.object(cs, "flt", _det_flt),
			patch.object(lanes_mod, "flt", _det_flt),
			patch.object(cs, "_precision", return_value=3),
			patch.object(cs.frappe.db, "get_value", side_effect=_get_value),
			patch.object(
				cs, "get_department_rm_warehouse", side_effect=lambda d: DEPT_RM.get(d)
			),
			patch.object(
				cs,
				"get_supplier_rm_warehouse",
				side_effect=lambda s, company=None: SUPPLIER_RM.get(s),
			),
			patch.object(cs, "get_department_chain_flags", return_value=(True, True)),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in reversed(self._patches):
			p.stop()


class TestValidateChain(_ChainCase):
	def test_non_chain_doc_is_normalised_and_cleared(self):
		doc = _chain_doc(
			is_chain_manufacturing=None, is_chain_subcontracting=1, to_department="X"
		)
		cs.validate_chain(doc)
		self.assertEqual(doc.is_chain_manufacturing, "No")
		self.assertEqual(doc.is_chain_subcontracting, 0)
		for field in cs.CHAIN_FIELDS:
			self.assertIsNone(doc.get(field), field)
		self.assertEqual(doc.dept_is_chain_manufacturing, 1)

	def test_chain_resolves_every_warehouse_from_masters(self):
		doc = _chain_doc(source_warehouse="Hand Picked WH", target_qty=9.5)
		cs.validate_chain(doc)
		self.assertEqual(doc.source_warehouse, "Chain RM - GK")
		self.assertEqual(doc.target_warehouse, "Chain RM - GK")
		self.assertEqual(doc.chain_supplier_warehouse, "SUP-1 MSL WH - GK")
		self.assertEqual(doc.to_department, "Polish - GK")
		self.assertEqual(doc.to_warehouse, "Polish RM - GK")
		self.assertEqual(doc.chain_process_loss_qty, 0.5)

	def test_department_follows_the_employee(self):
		doc = _chain_doc(department="Somewhere Else")
		cs.validate_chain(doc)
		self.assertEqual(doc.department, "Chain - GK")
		self.assertEqual(doc.manufacturer, "Shubh")

	def test_alloy_is_cleared(self):
		doc = _chain_doc(
			source_alloy="ALLOY", source_alloy_qty="1.2", target_alloy_qty=3
		)
		cs.validate_chain(doc)
		self.assertIsNone(doc.source_alloy)
		self.assertIsNone(doc.source_alloy_qty)
		self.assertEqual(doc.target_alloy_qty, 0)

	def test_ineligible_departments_are_refused(self):
		for flags in ((False, False), (True, False)):
			with patch.object(cs, "get_department_chain_flags", return_value=flags):
				with self.assertRaises(ValidationError, msg=str(flags)):
					cs.validate_chain(_chain_doc())

	def test_chain_manufacturing_without_subcontracting_is_refused(self):
		with self.assertRaises(ValidationError):
			cs.validate_chain(_chain_doc(is_chain_subcontracting=0))

	def test_other_modes_are_refused(self):
		for fields in ({"multiple_metal_converter": 1}, {"is_melting_loss": 1}):
			with self.assertRaises(ValidationError):
				cs.validate_chain(_chain_doc(**fields))

	def test_missing_warehouses_are_named(self):
		with self.assertRaises(ValidationError):
			cs.validate_chain(_chain_doc(chain_supplier="SUP-NO-WH"))
		with self.assertRaises(ValidationError):
			cs.validate_chain(_chain_doc(to_employee="EMP-X"))


class TestFrozenAfterIssue(_ChainCase):
	def _issued(self, **changes):
		before = _chain_doc(chain_issue_stock_entry="SE-ISSUE-1")
		doc = _chain_doc(chain_issue_stock_entry="SE-ISSUE-1", _before=before)
		doc.update(changes)
		return doc

	def test_unchanged_issued_doc_saves(self):
		cs.validate_chain(self._issued(target_item="CH-22KT", target_qty=9.5))

	def test_stock_fields_cannot_change(self):
		for changes in (
			{"source_qty": 11.0},
			{"chain_supplier": "SUP-2"},
			{"source_item": "M-OTHER"},
			{"source_batch_details": [_row("B-9", 10.0)]},
			{"chain_issue_stock_entry": None},
		):
			with self.assertRaises(ValidationError, msg=str(changes)):
				cs.validate_chain(self._issued(**changes))

	def test_issued_doc_skips_eligibility(self):
		# A department unticked after Issue must not strand the metal at the supplier.
		with patch.object(
			cs, "get_department_chain_flags", return_value=(False, False)
		):
			cs.validate_chain(self._issued())


class TestAfterValidate(_ChainCase):
	def test_issue_runs_once_in_the_issue_state(self):
		with patch.object(cs, "run_issue") as run_issue:
			cs.after_validate(_chain_doc(workflow_state="Issue"))
			cs.after_validate(
				_chain_doc(workflow_state="Issue", chain_issue_stock_entry="SE-1")
			)
			cs.after_validate(_chain_doc(workflow_state="Draft"))
		self.assertEqual(run_issue.call_count, 1)

	def test_batch_shows_the_first_allocation(self):
		doc = _chain_doc()
		with patch.object(cs, "run_issue"):
			cs.after_validate(doc)
		self.assertEqual(doc.batch, "B-1")

	def test_non_chain_is_untouched(self):
		doc = _chain_doc(
			is_chain_subcontracting=0, workflow_state="Issue", batch="KEEP"
		)
		with patch.object(cs, "run_issue") as run_issue:
			cs.after_validate(doc)
		run_issue.assert_not_called()
		self.assertEqual(doc.batch, "KEEP")

	def test_back_to_draft_with_an_issue_runs_cancel_issue(self):
		doc = _chain_doc(workflow_state="Draft", chain_issue_stock_entry="SE-ISSUE-1")
		with (
			patch.object(cs, "run_cancel_issue") as run_cancel_issue,
			patch.object(cs, "run_issue") as run_issue,
		):
			cs.after_validate(doc)
		run_cancel_issue.assert_called_once_with(doc)
		run_issue.assert_not_called()


class TestValidateIssue(_ChainCase):
	def setUp(self):
		super().setUp()
		self.batch_item = {"B-1": "M-G-22KT-91.6-Y", "B-2": "M-G-22KT-91.6-Y"}
		self.available = {"B-1": 6.0, "B-2": 4.0}
		self.po_uom = ("Gram", 0)

		def _get_all(doctype, *args, **kwargs):
			if doctype == "Batch":
				return [[name, item] for name, item in self.batch_item.items()]
			return _REAL_GET_ALL(doctype, *args, **kwargs)

		for p in (
			patch.object(cs.frappe, "get_all", side_effect=_get_all),
			patch.object(cs, "_po_item_uom", side_effect=lambda: self.po_uom),
			patch.object(
				cs,
				"get_batch_qty",
				side_effect=lambda batch_no, warehouse: self.available[batch_no],
			),
		):
			p.start()
			self._patches.append(p)

	def _ready(self, **fields):
		doc = _chain_doc(**fields)
		cs.validate_chain(doc)
		return doc

	def test_a_complete_doc_passes(self):
		cs.validate_issue(self._ready())

	def test_new_doc_is_refused(self):
		with self.assertRaises(ValidationError):
			cs.validate_issue(self._ready(_new=True))

	def test_batch_of_another_item_is_refused(self):
		self.batch_item["B-2"] = "M-OTHER"
		with self.assertRaises(ValidationError):
			cs.validate_issue(self._ready())

	def test_short_batch_is_refused(self):
		self.available["B-1"] = 5.0
		with self.assertRaises(ValidationError):
			cs.validate_issue(self._ready())

	def test_allocation_must_cover_source_qty(self):
		with self.assertRaises(ValidationError):
			cs.validate_issue(self._ready(source_qty=12.0))

	def test_missing_po_item_is_refused(self):
		self.po_uom = (None, 0)
		with self.assertRaisesRegex(ValidationError, "does not exist"):
			cs.validate_issue(self._ready())

	def _fractional(self):
		# 10.25 g, fully allocated, so only the UOM rule can refuse it.
		self.available["B-1"] = 6.25
		return self._ready(
			source_qty=10.25,
			source_batch_details=[_row("B-1", 6.25), _row("B-2", 4.0)],
		)

	def test_whole_number_uom_refuses_a_fractional_qty(self):
		self.po_uom = ("Nos", 1)
		with self.assertRaisesRegex(ValidationError, "whole-number"):
			cs.validate_issue(self._fractional())

	def test_gram_takes_a_fractional_qty(self):
		cs.validate_issue(self._fractional())

	def test_whole_number_uom_takes_a_whole_qty(self):
		self.po_uom = ("Nos", 1)
		cs.validate_issue(self._ready())

	def test_missing_inputs_are_reported(self):
		doc = self._ready()
		doc.chain_supplier = None
		doc.chain_supplier_warehouse = None
		doc.to_employee = None
		with self.assertRaises(ValidationError):
			cs.validate_issue(doc)


class TestValidateReceive(_ChainCase):
	def setUp(self):
		super().setUp()
		self.supplier_stock = {"B-1": 6.0, "B-2": 4.0}
		self.issue_status = 1
		# No purity on either item unless a test sets one: the fine-metal check skips.
		self.purity = {}

		def _receive_get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Stock Entry" and fieldname == "docstatus":
				return self.issue_status
			return _get_value(doctype, name, fieldname, *args, **kwargs)

		for p in (
			patch.object(cs.frappe.db, "get_value", side_effect=_receive_get_value),
			patch.object(
				cs,
				"get_batch_qty",
				side_effect=lambda batch_no, warehouse: self.supplier_stock[batch_no],
			),
			patch.object(
				cs, "_metal_purity", side_effect=lambda item: self.purity.get(item)
			),
		):
			p.start()
			self._patches.append(p)

	def _received(self, **fields):
		# An issued document keeps the supplier warehouse Issue resolved; it is frozen.
		doc = _chain_doc(
			workflow_state="Receive",
			chain_issue_stock_entry="SE-ISSUE-1",
			chain_supplier_warehouse="SUP-1 MSL WH - GK",
			target_item="CH-22KT",
			target_qty=9.5,
		)
		cs.validate_chain(doc)
		doc.update(fields)
		return doc

	def test_a_complete_receive_passes(self):
		cs.validate_receive(self._received())

	def test_only_the_receive_action_submits(self):
		# Draft -> .submit() lands on "Submitted" (first docstatus-1 state): refused.
		for state in ("Draft", "Issue", "Submitted"):
			with self.assertRaises(ValidationError, msg=state):
				cs.validate_receive(self._received(workflow_state=state))

	def test_issue_must_be_submitted(self):
		self.issue_status = 2
		with self.assertRaises(ValidationError):
			cs.validate_receive(self._received())

	def test_target_rules(self):
		for fields in (
			{"target_item": None},
			{"target_qty": 0},
			{"target_qty": 10.5},
		):
			with self.assertRaises(ValidationError, msg=str(fields)):
				cs.validate_receive(self._received(**fields))

	def test_full_target_is_allowed(self):
		cs.validate_receive(self._received(target_qty=10.0))

	def test_metal_must_still_be_at_the_supplier(self):
		self.supplier_stock["B-2"] = 1.0
		with self.assertRaises(ValidationError):
			cs.validate_receive(self._received())

	def test_same_purity_passes(self):
		self.purity = {"M-G-22KT-91.6-Y": 91.6, "CH-22KT": 91.6}
		cs.validate_receive(self._received())

	def test_target_cannot_hold_more_fine_metal_than_was_issued(self):
		# 10 g at 75% is 7.5 g fine; 9.5 g at 91.6% would be 8.702 g.
		self.purity = {"M-G-22KT-91.6-Y": 75.0, "CH-22KT": 91.6}
		with self.assertRaisesRegex(ValidationError, "fine metal"):
			cs.validate_receive(self._received())

	def test_missing_purity_skips_the_fine_metal_check(self):
		self.purity = {"M-G-22KT-91.6-Y": 75.0}
		cs.validate_receive(self._received())


class TestRunReceive(_ChainCase):
	def _run(self, target_qty):
		doc = _chain_doc(
			workflow_state="Receive",
			chain_issue_stock_entry="SE-ISSUE-1",
			chain_supplier_warehouse="SUP-1 MSL WH - GK",
			to_warehouse="Polish RM - GK",
			target_item="CH-22KT",
			target_qty=target_qty,
		)
		with (
			# Lock order has its own suite (TestReceiveLockOrder).
			patch.object(cs, "_lock_receive"),
			patch.object(
				cs, "make_repack_stock_entry", return_value="SE-REPACK-1"
			) as repack,
			patch.object(
				melting_loss, "make_process_loss_entry", return_value="SE-LOSS-1"
			) as loss,
			patch.object(
				cs, "make_final_transfer", return_value="SE-TRANSFER-1"
			) as transfer,
		):
			cs.run_receive(doc)
		return doc, repack, loss, transfer

	def test_repack_takes_target_qty_and_loss_the_rest(self):
		doc, repack, loss, transfer = self._run(9.5)
		repack_rows = repack.call_args.args[1]
		self.assertEqual(sum(r["qty"] for r in repack_rows), 9.5)
		self.assertEqual(repack.call_args.args[2], 9.5)
		loss_kwargs = loss.call_args.kwargs
		self.assertEqual(loss_kwargs["loss_qty"], 0.5)
		self.assertEqual(sum(r["qty"] for r in loss_kwargs["rows"]), 0.5)
		self.assertEqual(loss_kwargs["source_wh"], "SUP-1 MSL WH - GK")
		# FIFO: the repack drains B-1 first, the loss comes from the end of B-2.
		self.assertEqual(loss_kwargs["rows"], [{"batch": "B-2", "qty": 0.5}])
		transfer.assert_called_once_with(doc, "SE-REPACK-1", 9.5)
		doc.db_set.assert_has_calls(
			[
				call(
					{
						"stock_entry": "SE-REPACK-1",
						"chain_repack_stock_entry": "SE-REPACK-1",
					}
				),
				call("chain_loss_stock_entry", "SE-LOSS-1"),
				call("chain_transfer_stock_entry", "SE-TRANSFER-1"),
			]
		)

	def test_zero_loss_skips_the_process_loss_entry(self):
		doc, repack, loss, transfer = self._run(10.0)
		loss.assert_not_called()
		transfer.assert_called_once()


class TestReceiveLockOrder(_ChainCase):
	"""Receive pins every counter, then every Bin, before its first Stock Entry (F3)."""

	def _run(self, target_qty):
		events = []
		doc = _chain_doc(
			workflow_state="Receive",
			chain_issue_stock_entry="SE-ISSUE-1",
			chain_supplier_warehouse="SUP-1 MSL WH - GK",
			to_warehouse="Polish RM - GK",
			target_item="CH-22KT",
			target_qty=target_qty,
		)
		with (
			patch(
				f"{_LOCK_ORDER}.series_stubs",
				side_effect=lambda company, *types: list(types),
			),
			patch(
				f"{_LOCK_ORDER}.preallocate_series_for_docs",
				side_effect=lambda *stubs: events.append(("counters", set(stubs))),
			),
			patch(
				f"{_LOCK_ORDER}.lock_bins",
				side_effect=lambda pairs: events.append(("bins", set(pairs))),
			),
			patch.object(
				melting_loss, "_resolve_loss_item", return_value="ML-22KT"
			) as resolve_loss_item,
			patch(_SCRAP_WAREHOUSE, return_value="Chain Scrap - GK"),
			patch.object(
				cs,
				"make_repack_stock_entry",
				side_effect=lambda *a: events.append("repack") or "SE-REPACK-1",
			),
			patch.object(
				melting_loss,
				"make_process_loss_entry",
				side_effect=lambda *a, **k: events.append("loss") or "SE-LOSS-1",
			),
			patch.object(
				cs,
				"make_final_transfer",
				side_effect=lambda *a: events.append("transfer") or "SE-TRANSFER-1",
			),
		):
			cs.run_receive(doc)
		return events, resolve_loss_item

	def test_counters_then_bins_then_the_entries(self):
		events, _resolve = self._run(9.5)
		self.assertEqual(
			events[0],
			(
				"counters",
				{
					mc.CONVERSION_SE_TYPE,
					cs.CHAIN_SC_SE_TYPE,
					melting_loss.MELTING_LOSS_SE_TYPE,
				},
			),
		)
		self.assertEqual(
			events[1],
			(
				"bins",
				{
					("M-G-22KT-91.6-Y", "SUP-1 MSL WH - GK"),
					("CH-22KT", "SUP-1 MSL WH - GK"),
					("CH-22KT", "Polish RM - GK"),
					("ML-22KT", "Chain Scrap - GK"),
				},
			),
		)
		self.assertEqual(events[2:], ["repack", "loss", "transfer"])

	def test_zero_loss_pins_no_loss_counter_or_bin(self):
		events, resolve_loss_item = self._run(10.0)
		self.assertEqual(
			events[0], ("counters", {mc.CONVERSION_SE_TYPE, cs.CHAIN_SC_SE_TYPE})
		)
		self.assertEqual(
			events[1],
			(
				"bins",
				{
					("M-G-22KT-91.6-Y", "SUP-1 MSL WH - GK"),
					("CH-22KT", "SUP-1 MSL WH - GK"),
					("CH-22KT", "Polish RM - GK"),
				},
			),
		)
		self.assertEqual(events[2:], ["repack", "transfer"])
		resolve_loss_item.assert_not_called()


class _FakeDoc(SimpleNamespace):
	def __init__(self, payload=None, **kwargs):
		super().__init__(**kwargs)
		self.payload = payload or {}
		self.items = []
		self.flags = frappe._dict()
		self.saved = self.submitted = False
		self.name = kwargs.get("name", "SE-NEW-1")

	def append(self, table, row):
		self.items.append(row)

	def save(self):
		self.saved = True

	def submit(self):
		self.submitted = True


class _BuilderCase(_ChainCase):
	def setUp(self):
		super().setUp()
		self.captured = []

		def _get_doc(payload, *args, **kwargs):
			doc = _FakeDoc(payload)
			self.captured.append(doc)
			return doc

		for p in (
			patch.object(cs.frappe, "get_doc", side_effect=_get_doc),
			patch.object(cs.frappe, "new_doc", side_effect=lambda dt: _FakeDoc()),
			patch("jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"),
			patch(
				"jewellery_erpnext.jewellery_erpnext.lock_order.preallocate_series_for_docs"
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.lock_order.stock_lock_key",
				side_effect=lambda i, w, b=None: (i or "", w or "", b or ""),
			),
		):
			p.start()
			self._patches.append(p)

	def _doc(self, **fields):
		doc = _chain_doc(
			chain_supplier_warehouse="SUP-1 MSL WH - GK",
			to_warehouse="Polish RM - GK",
			target_item="CH-22KT",
			target_qty=9.5,
		)
		doc.update(fields)
		return doc


class TestStockEntryBuilders(_BuilderCase):
	def test_issue_moves_each_batch_department_to_supplier(self):
		cs.make_issue_stock_entry(self._doc())
		se = self.captured[-1]
		self.assertEqual(se.payload["stock_entry_type"], cs.CHAIN_SC_SE_TYPE)
		self.assertEqual(se.payload["purpose"], "Material Transfer")
		self.assertEqual(se.payload["custom_metal_conversion_reference"], "MCON-CH-1")
		self.assertEqual(se.payload["auto_created"], 1)
		self.assertEqual(
			[(r["batch_no"], r["qty"]) for r in se.items], [("B-1", 6.0), ("B-2", 4.0)]
		)
		for row in se.items:
			self.assertEqual(row["s_warehouse"], "Chain RM - GK")
			self.assertEqual(row["t_warehouse"], "SUP-1 MSL WH - GK")
			self.assertEqual(row["inventory_type"], "Regular Stock")
		self.assertTrue(se.saved and se.submitted)

	def test_repack_stays_inside_the_supplier_warehouse(self):
		with patch.object(
			cs,
			"get_batch_lane_map",
			return_value={
				"B-1": ("Regular Stock", None),
				"B-2": ("Regular Stock", None),
			},
		), patch.object(mc, "flt", _det_flt):
			cs.make_repack_stock_entry(
				self._doc(),
				[{"batch": "B-1", "qty": 6.0}, {"batch": "B-2", "qty": 3.5}],
				9.5,
			)
		se = self.captured[-1]
		self.assertEqual(se.payload["stock_entry_type"], mc.CONVERSION_SE_TYPE)
		consume = [r for r in se.items if r.get("s_warehouse")]
		produce = [r for r in se.items if r.get("t_warehouse")]
		self.assertEqual(sum(r["qty"] for r in consume), 9.5)
		self.assertEqual({r["s_warehouse"] for r in consume}, {"SUP-1 MSL WH - GK"})
		self.assertEqual(len(produce), 1)
		self.assertEqual(produce[0]["item_code"], "CH-22KT")
		self.assertEqual(produce[0]["qty"], 9.5)
		self.assertEqual(produce[0]["t_warehouse"], "SUP-1 MSL WH - GK")

	def test_final_transfer_carries_the_minted_batch(self):
		with patch.object(
			cs, "_produced_batches", return_value=[{"batch": "TB-1", "qty": 9.5}]
		):
			cs.make_final_transfer(self._doc(), "SE-REPACK-1", 9.5)
		se = self.captured[-1]
		self.assertEqual(se.payload["stock_entry_type"], cs.CHAIN_SC_SE_TYPE)
		self.assertEqual(len(se.items), 1)
		row = se.items[0]
		self.assertEqual(row["batch_no"], "TB-1")
		self.assertEqual(row["item_code"], "CH-22KT")
		self.assertEqual(row["s_warehouse"], "SUP-1 MSL WH - GK")
		self.assertEqual(row["t_warehouse"], "Polish RM - GK")

	def test_final_transfer_refuses_a_short_repack(self):
		with patch.object(
			cs, "_produced_batches", return_value=[{"batch": "TB-1", "qty": 9.0}]
		):
			with self.assertRaises(ValidationError):
				cs.make_final_transfer(self._doc(), "SE-REPACK-1", 9.5)


class TestSubcontractingPO(_BuilderCase):
	def test_draft_po_for_the_service_item(self):
		po = _FakeDoc(name="PO-1")
		with (
			patch.object(cs.frappe, "new_doc", return_value=po),
			patch.object(
				cs,
				"_linked_address",
				side_effect=lambda dt, name: {
					"Company": "ADDR-CO",
					"Supplier": "ADDR-SUP",
				}[dt],
			),
			patch.object(cs, "nowdate", return_value="2026-10-12"),
			patch.object(cs, "_po_item_uom", return_value=("Gram", 0)),
		):
			name = cs.make_subcontracting_po(self._doc(date="2026-09-01"))
		self.assertEqual(name, "PO-1")
		self.assertEqual(po.supplier, "SUP-1")
		self.assertEqual(po.purchase_type, cs.CHAIN_SC_PURCHASE_TYPE)
		self.assertEqual(po.custom_metal_conversion, "MCON-CH-1")
		self.assertEqual(po.billing_address, "ADDR-CO")
		self.assertEqual(po.supplier_address, "ADDR-SUP")
		# A back-dated conversion must not produce a Required By date before today.
		self.assertEqual(po.schedule_date, "2026-10-12")
		self.assertEqual(
			po.items,
			[
				{
					"item_code": cs.CHAIN_SC_PO_ITEM,
					"qty": 10.0,
					# The item's own stock UOM, so no purchase-UOM conversion applies.
					"uom": "Gram",
					"conversion_factor": 1,
					"schedule_date": "2026-10-12",
				}
			],
		)
		self.assertTrue(po.flags.ignore_permissions)
		self.assertTrue(po.saved)
		self.assertFalse(po.submitted)


class TestProcessLossEntry(IntegrationTestCase):
	"""The extracted builder takes split_allocations dicts as well as table rows."""

	@classmethod
	def setUpClass(cls):
		_("warm")

	def test_returns_the_existing_entry(self):
		doc = _chain_doc()
		with patch.object(melting_loss.frappe.db, "exists", return_value="SE-LOSS-9"):
			self.assertEqual(
				melting_loss.make_process_loss_entry(
					doc, rows=[], loss_qty=0.5, source_wh="X"
				),
				"SE-LOSS-9",
			)

	def test_dict_rows_from_the_supplier_warehouse(self):
		doc = _chain_doc()
		captured = {}

		def _get_doc(payload, *args, **kwargs):
			captured["se"] = _FakeDoc(payload, name="SE-LOSS-1")
			return captured["se"]

		with (
			patch.object(melting_loss.frappe.db, "exists", return_value=None),
			patch.object(melting_loss, "_loss_precision", return_value=3),
			patch.object(melting_loss, "flt", _det_flt),
			patch.object(melting_loss.frappe, "get_doc", side_effect=_get_doc),
			patch.object(
				melting_loss.frappe, "new_doc", side_effect=lambda dt: _FakeDoc()
			),
			patch.object(melting_loss, "_resolve_loss_item", return_value="ML-22KT"),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion.get_scrap_warehouse",
				return_value="Chain Scrap - GK",
			),
			patch("jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"),
			patch(
				"jewellery_erpnext.jewellery_erpnext.lock_order.preallocate_series_for_docs"
			),
		):
			name = melting_loss.make_process_loss_entry(
				doc,
				rows=[{"batch": "B-2", "qty": 0.5}],
				loss_qty=0.5,
				source_wh="SUP-1 MSL WH - GK",
			)
		self.assertEqual(name, "SE-LOSS-1")
		consume, produce = captured["se"].items
		self.assertEqual(consume["batch_no"], "B-2")
		self.assertEqual(consume["s_warehouse"], "SUP-1 MSL WH - GK")
		self.assertEqual(produce["t_warehouse"], "Chain Scrap - GK")
		self.assertEqual(produce["qty"], 0.5)
		doc.db_set.assert_not_called()


#: The rows of the four entries a received chain conversion owns.
_ENTRY_ROWS = [
	frappe._dict(item_code="CH-22KT", s_warehouse="SUP", t_warehouse="TO"),
	frappe._dict(item_code="M-G-22KT-91.6-Y", s_warehouse="SUP", t_warehouse=None),
	frappe._dict(item_code="ML-22KT", s_warehouse=None, t_warehouse="SCRAP"),
	frappe._dict(item_code="CH-22KT", s_warehouse=None, t_warehouse="SUP"),
	frappe._dict(item_code="M-G-22KT-91.6-Y", s_warehouse="DEPT", t_warehouse="SUP"),
]


class TestCancelChain(_ChainCase):
	def _cancel(
		self, po_status, entries=("SE-TRANSFER-1", "SE-REPACK-1", "SE-ISSUE-1")
	):
		order = []
		doc = _chain_doc(
			chain_issue_stock_entry="SE-ISSUE-1",
			chain_transfer_stock_entry="SE-TRANSFER-1",
			chain_purchase_order="PO-1",
		)

		def _get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Purchase Order":
				return po_status
			raise AssertionError(doctype)

		def _get_all(doctype, *args, **kwargs):
			if doctype == "Stock Entry":
				return list(entries)
			if doctype == "Stock Entry Detail":
				return _ENTRY_ROWS
			return _REAL_GET_ALL(doctype, *args, **kwargs)

		with (
			patch.object(cs.frappe.db, "get_value", side_effect=_get_value),
			patch.object(cs.frappe, "get_all", side_effect=_get_all),
			patch(
				f"{_LOCK_ORDER}.lock_bins",
				side_effect=lambda pairs: order.append(("lock", set(pairs))),
			),
			patch.object(
				cs, "_cancel_stock_entry", side_effect=lambda name: order.append(name)
			),
			patch.object(
				melting_loss,
				"cancel_melting_loss_stock_entries",
				side_effect=lambda d: order.append("loss"),
			),
			patch.object(
				mc,
				"cancel_conversion_stock_entries",
				side_effect=lambda d: order.append("repack"),
			),
			patch.object(cs.frappe, "delete_doc") as delete_doc,
		):
			cs.cancel_chain(doc)
		return order, delete_doc, doc

	def test_reverse_order_and_draft_po_deleted(self):
		order, delete_doc, doc = self._cancel(po_status=0)
		self.assertEqual(order[1:], ["SE-TRANSFER-1", "loss", "repack", "SE-ISSUE-1"])
		delete_doc.assert_called_once_with(
			"Purchase Order", "PO-1", ignore_permissions=True
		)
		doc.db_set.assert_called_once_with("chain_purchase_order", None)

	def test_every_bin_is_locked_before_the_first_cancel(self):
		order, _delete_doc, _doc = self._cancel(po_status=0)
		self.assertEqual(
			order[0],
			(
				"lock",
				{
					("CH-22KT", "SUP"),
					("CH-22KT", "TO"),
					("M-G-22KT-91.6-Y", "SUP"),
					("M-G-22KT-91.6-Y", "DEPT"),
					("ML-22KT", "SCRAP"),
				},
			),
		)

	def test_nothing_to_lock_without_entries(self):
		order, _delete_doc, _doc = self._cancel(po_status=0, entries=())
		self.assertNotIn(
			"lock", [event[0] for event in order if isinstance(event, tuple)]
		)

	def test_submitted_po_stops_everything(self):
		with self.assertRaises(ValidationError):
			self._cancel(po_status=1)


class TestCancelIssue(_ChainCase):
	"""Issue -> Draft (Cancel Issue) undoes an Issue that has not been received (F2)."""

	def _cancel_issue(self, po_status, po="PO-1"):
		# Kept on the test so a refusal can still be checked for what ran before it.
		self.cancelled = cancelled = []
		doc = _chain_doc(
			workflow_state="Draft",
			chain_issue_stock_entry="SE-ISSUE-1",
			chain_purchase_order=po,
			target_item="CH-22KT",
			target_qty=9.5,
			chain_process_loss_qty=0.5,
		)

		def _get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Purchase Order" and fieldname == "docstatus":
				return po_status
			raise AssertionError(f"unexpected get_value({doctype!r}, {name!r})")

		with (
			patch.object(cs.frappe.db, "get_value", side_effect=_get_value),
			patch.object(
				cs,
				"_cancel_stock_entry",
				side_effect=lambda name: cancelled.append(name),
			),
			patch.object(cs.frappe, "delete_doc") as delete_doc,
		):
			cs.run_cancel_issue(doc)
		return doc, cancelled, delete_doc

	def test_cancels_the_issue_and_deletes_the_draft_po(self):
		doc, cancelled, delete_doc = self._cancel_issue(po_status=0)
		self.assertEqual(cancelled, ["SE-ISSUE-1"])
		delete_doc.assert_called_once_with(
			"Purchase Order", "PO-1", force=True, ignore_permissions=True
		)
		self.assertIsNone(doc.chain_issue_stock_entry)
		self.assertIsNone(doc.chain_purchase_order)
		# The receipt typed in the Issue state will not happen now.
		self.assertIsNone(doc.target_item)
		self.assertEqual(doc.target_qty, 0)
		self.assertEqual(doc.chain_process_loss_qty, 0)
		# The allocation stays: the metal is back in the department warehouse.
		self.assertEqual(
			[row.batch for row in doc.source_batch_details], ["B-1", "B-2"]
		)

	def test_a_submitted_po_stops_it_before_anything_is_cancelled(self):
		with self.assertRaisesRegex(ValidationError, "PO-1"):
			self._cancel_issue(po_status=1)
		self.assertEqual(self.cancelled, [])

	def test_a_cancelled_po_is_left_alone(self):
		doc, cancelled, delete_doc = self._cancel_issue(po_status=2)
		self.assertEqual(cancelled, ["SE-ISSUE-1"])
		delete_doc.assert_not_called()
		self.assertIsNone(doc.chain_purchase_order)

	def test_no_po_reads_no_po(self):
		doc, cancelled, delete_doc = self._cancel_issue(po_status=None, po=None)
		self.assertEqual(cancelled, ["SE-ISSUE-1"])
		delete_doc.assert_not_called()

	def test_an_entry_cancelled_by_hand_is_not_cancelled_again(self):
		def _get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Stock Entry" and fieldname == "docstatus":
				return 2
			raise AssertionError(doctype)

		with (
			patch.object(cs.frappe.db, "get_value", side_effect=_get_value),
			patch.object(cs.frappe, "get_doc") as get_doc,
		):
			cs._cancel_stock_entry("SE-ISSUE-1")
		get_doc.assert_not_called()


class TestGuardTrash(_ChainCase):
	"""Delete is refused only while the Issue keeps metal at the supplier (F2)."""

	def _guard(self, issue_status, **fields):
		def _get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Stock Entry" and fieldname == "docstatus":
				return issue_status
			raise AssertionError(doctype)

		with patch.object(cs.frappe.db, "get_value", side_effect=_get_value):
			cs.guard_trash(_chain_doc(**fields))

	def test_a_live_issue_cannot_be_deleted(self):
		with self.assertRaisesRegex(ValidationError, "Cancel Issue"):
			self._guard(1, chain_issue_stock_entry="SE-ISSUE-1")

	def test_a_cancelled_conversion_can_be_deleted(self):
		self._guard(None, docstatus=2, chain_issue_stock_entry="SE-ISSUE-1")

	def test_an_issue_cancelled_by_hand_can_be_deleted(self):
		self._guard(2, chain_issue_stock_entry="SE-ISSUE-1")

	def test_a_never_issued_conversion_can_be_deleted(self):
		self._guard(None)


class TestControllerRouting(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		_("warm")

	def test_chain_submit_goes_to_receive_only(self):
		doc = _chain_doc(workflow_state="Receive")
		with (
			patch.object(mc, "validate_receive") as validate_receive,
			patch.object(mc, "run_receive") as run_receive,
			patch.object(mc, "make_metal_stock_entry") as conversion,
		):
			mc.MetalConversions.before_submit(doc)
			mc.MetalConversions.on_submit(doc)
		validate_receive.assert_called_once_with(doc)
		run_receive.assert_called_once_with(doc)
		conversion.assert_not_called()

	def test_non_chain_submit_is_unchanged(self):
		doc = _chain_doc(is_chain_manufacturing="No", is_chain_subcontracting=0)
		with patch.object(mc, "validate_receive") as validate_receive:
			mc.MetalConversions.before_submit(doc)
		validate_receive.assert_not_called()

	def test_chain_cancel_goes_through_cancel_chain(self):
		doc = _chain_doc()
		with (
			patch.object(mc, "cancel_chain") as cancel_chain,
			patch.object(mc, "cancel_conversion_stock_entries") as conversion,
		):
			mc.MetalConversions.on_cancel(doc)
		cancel_chain.assert_called_once_with(doc)
		conversion.assert_not_called()


class TestWorkflowPatch(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_submitted_is_the_first_submitted_state(self):
		"""Frappe back-fills and programmatic submits take the first docstatus-1 state."""
		first_submitted = next(
			s for s, status in workflow_patch.STATES if status == "1"
		)
		self.assertEqual(first_submitted, workflow_patch.SUBMITTED)
		self.assertEqual(workflow_patch.STATES[0], (workflow_patch.DRAFT, "0"))

	def test_draft_offers_issue_or_submit_never_both(self):
		from_draft = {
			action: condition
			for state, action, _next, condition, _allowed in workflow_patch.TRANSITIONS
			if state == workflow_patch.DRAFT
		}
		self.assertEqual(
			from_draft[workflow_patch.ISSUE_ACTION], workflow_patch.IS_CHAIN
		)
		self.assertEqual(
			from_draft[workflow_patch.SUBMIT_ACTION], workflow_patch.NOT_CHAIN
		)
		for flag, expected in ((1, "Issue"), (0, "Submit"), (None, "Submit")):
			doc = frappe._dict(is_chain_subcontracting=flag)
			offered = [
				action
				for action, condition in from_draft.items()
				if frappe.safe_eval(condition, None, {"doc": doc})
			]
			self.assertEqual(offered, [expected], flag)

	def test_issue_is_a_save_and_receive_a_submit(self):
		status = dict(workflow_patch.STATES)
		self.assertEqual(status[workflow_patch.ISSUE], "0")
		self.assertEqual(status[workflow_patch.RECEIVE], "1")

	def test_cancel_issue_is_system_manager_only(self):
		self.assertIn(
			(
				workflow_patch.ISSUE,
				workflow_patch.CANCEL_ISSUE_ACTION,
				workflow_patch.DRAFT,
				None,
				workflow_patch.SYSTEM_MANAGER,
			),
			workflow_patch.TRANSITIONS,
		)
		others = [
			row[4] for row in workflow_patch.TRANSITIONS if row[1] != "Cancel Issue"
		]
		self.assertEqual(set(others), {workflow_patch.ALL})

	def _workflow_before_cancel_issue(self):
		"""The workflow as the first version of the patch created it."""
		workflow = _FakeWorkflow(
			states=[state for state, _doc_status in workflow_patch.STATES],
			transitions=[
				(row[0], row[1])
				for row in workflow_patch.TRANSITIONS
				if row[1] != workflow_patch.CANCEL_ISSUE_ACTION
			],
		)
		return workflow

	def test_an_existing_workflow_gets_only_cancel_issue(self):
		workflow = self._workflow_before_cancel_issue()
		before = list(workflow.transitions)
		with patch.object(workflow_patch.frappe, "logger"):
			workflow_patch._add_missing(workflow)
		self.assertEqual(workflow.transitions[: len(before)], before)
		added = workflow.transitions[len(before) :]
		self.assertEqual(len(added), 1)
		self.assertEqual(
			(added[0].state, added[0].action, added[0].next_state, added[0].allowed),
			("Issue", "Cancel Issue", "Draft", "System Manager"),
		)
		workflow.save.assert_called_once_with(ignore_permissions=True)

	def test_a_complete_workflow_is_not_saved(self):
		workflow = self._workflow_before_cancel_issue()
		with patch.object(workflow_patch.frappe, "logger"):
			workflow_patch._add_missing(workflow)
			workflow_patch._add_missing(workflow)
		workflow.save.assert_called_once()

	def test_execute_amends_an_existing_workflow_and_creates_a_missing_one(self):
		workflow = self._workflow_before_cancel_issue()
		for exists, add_calls, create_calls in ((True, 1, 0), (False, 0, 1)):
			with (
				patch.object(workflow_patch, "_ensure_masters"),
				patch.object(workflow_patch.frappe.db, "exists", return_value=exists),
				patch.object(workflow_patch.frappe.db, "get_value", return_value=None),
				patch.object(workflow_patch.frappe, "get_doc", return_value=workflow),
				patch.object(workflow_patch, "_add_missing") as add_missing,
				patch.object(workflow_patch, "_create_workflow") as create_workflow,
			):
				workflow_patch.execute()
			self.assertEqual(add_missing.call_count, add_calls, exists)
			self.assertEqual(create_workflow.call_count, create_calls, exists)


class _FakeWorkflow(SimpleNamespace):
	def __init__(self, states, transitions):
		super().__init__(
			name="Metal Conversions",
			states=[frappe._dict(state=state) for state in states],
			transitions=[
				frappe._dict(state=state, action=action)
				for state, action in transitions
			],
			save=MagicMock(),
		)

	def append(self, table, row):
		getattr(self, table).append(frappe._dict(row))


class TestEnsurePoItem(IntegrationTestCase):
	"""The PO service item is created in Gram, not the template's "Nos" (F1)."""

	@classmethod
	def setUpClass(cls):
		pass

	def _ensure(self, existing=False, template=True, gram=True):
		template_doc = SimpleNamespace(name=chain_sc_patch.TEMPLATE_ITEM)
		copy = SimpleNamespace(
			stock_uom="Nos",
			purchase_uom="Nos",
			sales_uom="Nos",
			uoms=[frappe._dict(uom="Nos", conversion_factor=1)],
			insert=MagicMock(),
		)
		answers = {
			("Item", chain_sc_patch.CHAIN_SC_PO_ITEM): existing,
			("Item", chain_sc_patch.TEMPLATE_ITEM): template,
			("UOM", chain_sc_patch.PO_ITEM_UOM): gram,
		}

		def _exists(doctype, name=None, *args, **kwargs):
			return answers[(doctype, name)]

		with (
			patch.object(chain_sc_patch.frappe.db, "exists", side_effect=_exists),
			patch.object(chain_sc_patch.frappe, "get_doc", return_value=template_doc),
			patch.object(
				chain_sc_patch.frappe, "copy_doc", return_value=copy
			) as copy_doc,
			patch.object(chain_sc_patch.frappe, "logger"),
		):
			chain_sc_patch._ensure_po_item()
		return copy, copy_doc

	def test_created_in_gram(self):
		item, _copy_doc = self._ensure()
		self.assertEqual(item.item_code, chain_sc_patch.CHAIN_SC_PO_ITEM)
		self.assertEqual(item.stock_uom, "Gram")
		self.assertIsNone(item.purchase_uom)
		self.assertIsNone(item.sales_uom)
		# ERPNext adds the Gram x 1 row itself on save.
		self.assertEqual(item.uoms, [])
		item.insert.assert_called_once_with(ignore_permissions=True)

	def test_keeps_the_template_uom_when_gram_is_missing(self):
		item, _copy_doc = self._ensure(gram=False)
		self.assertEqual(item.stock_uom, "Nos")
		self.assertEqual(len(item.uoms), 1)
		item.insert.assert_called_once()

	def test_an_existing_item_is_left_alone(self):
		_item, copy_doc = self._ensure(existing=True)
		copy_doc.assert_not_called()

	def test_no_template_no_item(self):
		_item, copy_doc = self._ensure(template=False)
		copy_doc.assert_not_called()
