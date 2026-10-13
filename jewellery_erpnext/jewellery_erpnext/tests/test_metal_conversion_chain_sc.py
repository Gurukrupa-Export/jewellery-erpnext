# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Metal Conversions Chain Subcontracting (doc_events/chain_subcontracting.py).

Pure-logic suite: every master read and every Stock Entry / Purchase Order is stubbed, so it
runs on any site and says nothing about the stock ledger -- the desk walk-through in the PR
covers that. Stubs are keyed on what they answer (never a blanket ``frappe.db.get_value``),
and ``flt`` is replaced by a deterministic shim, because frappe's own returns 0.0 when the
rounding setting cannot be read.
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
from jewellery_erpnext.patches import add_metal_conversion_workflow as workflow_patch

_REAL_GET_ALL = frappe.get_all

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


class TestValidateIssue(_ChainCase):
	def setUp(self):
		super().setUp()
		self.batch_item = {"B-1": "M-G-22KT-91.6-Y", "B-2": "M-G-22KT-91.6-Y"}
		self.available = {"B-1": 6.0, "B-2": 4.0}

		def _get_all(doctype, *args, **kwargs):
			if doctype == "Batch":
				return [[name, item] for name, item in self.batch_item.items()]
			return _REAL_GET_ALL(doctype, *args, **kwargs)

		def _exists(doctype, name=None, *args, **kwargs):
			if doctype == "Item":
				return name == cs.CHAIN_SC_PO_ITEM
			raise AssertionError(f"unexpected exists({doctype!r})")

		for p in (
			patch.object(cs.frappe, "get_all", side_effect=_get_all),
			patch.object(cs.frappe.db, "exists", side_effect=_exists),
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
		with patch.object(cs.frappe.db, "exists", return_value=False):
			with self.assertRaises(ValidationError):
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


class TestCancelChain(_ChainCase):
	def _cancel(self, po_status):
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

		with (
			patch.object(cs.frappe.db, "get_value", side_effect=_get_value),
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
		self.assertEqual(order, ["SE-TRANSFER-1", "loss", "repack", "SE-ISSUE-1"])
		delete_doc.assert_called_once_with(
			"Purchase Order", "PO-1", ignore_permissions=True
		)
		doc.db_set.assert_called_once_with("chain_purchase_order", None)

	def test_submitted_po_stops_everything(self):
		with self.assertRaises(ValidationError):
			self._cancel(po_status=1)

	def test_issued_doc_cannot_be_deleted(self):
		with self.assertRaises(ValidationError):
			cs.guard_trash(_chain_doc(chain_issue_stock_entry="SE-ISSUE-1"))
		cs.guard_trash(_chain_doc())


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
			for state, action, _next, condition in workflow_patch.TRANSITIONS
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
