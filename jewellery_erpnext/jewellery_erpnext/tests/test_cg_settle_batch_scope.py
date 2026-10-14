# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Settle on a customer-goods-return Material Request works by customer AND the row's batch.

The request's own transfer is batch-strict, so only the row's batch counts as already in
the source warehouse, and only that batch's own metal (itself or metal produced from it)
may make up a shortfall. Another receipt's batch of the same customer never settles it.

Shaped on KGJPL-MR-MT-26-03988: 5 g of batch -01 asked from Central RM, where -01 holds
nothing (all 19 g sit in RM Procurement, the request's target) while the customer's other
batch -02 holds 10 g. Pure; for CI.
"""

import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from jewellery_erpnext.customer_subcontracting.sub_utils import cg_settle, snc

_C = "jewellery_erpnext.customer_subcontracting.sub_utils.cg_settle"
_TRACE = "jewellery_erpnext.customer_subcontracting.customer_gold_trace"

CENTRAL = "Central RM - KGJPL"
PROCUREMENT = "RM Procurement - KGJPL"
WAXING = "Waxing RM - KGJPL"
CUSTOMER = "KACU0040"
ITEM = "M-G-24KT-99.9-Y"
B01 = "KACU0040-2F10-M-G-24KT-99.9-Y-01"
B02 = "KACU0040-2F10-M-G-24KT-99.9-Y-02"

#: The real functions, saved before any test patches them. A fake answers only the doctypes it
#: owns and hands every other read to the real one (see test_customer_gold_return_rules).
_REAL_GET_ALL = frappe.get_all
_REAL_GET_DOC = frappe.get_doc

#: The batch master as the validation reads it.
BATCHES = {
	B01: {"item": ITEM, "custom_customer": CUSTOMER, "disabled": 0},
	B02: {"item": ITEM, "custom_customer": CUSTOMER, "disabled": 0},
}

#: MR-03988's ledger: -01 is back in RM Procurement, -02 sits in Central RM.
LEDGER_03988 = {(B01, PROCUREMENT): 19.0, (B02, CENTRAL): 10.0}


def _row(**fields):
	values = {
		"idx": 1,
		"item_code": ITEM,
		"qty": 5.0,
		"custom_pure_qty": 5.0,
		"batch_no": B01,
		"customer": CUSTOMER,
		"from_warehouse": CENTRAL,
		"warehouse": PROCUREMENT,
	}
	values.update(fields)
	return frappe._dict(values)


class _Doc(SimpleNamespace):
	"""A document stand-in: attribute access plus ``.get``. Not a dict, because a dict's
	``.items`` would shadow the Material Request's ``items`` child table."""

	def get(self, key, default=None):
		return getattr(self, key, default)


def _mr(rows=None):
	return _Doc(
		name="KGJPL-MR-MT-26-03988",
		company="KG GK Jewellers Private Limited",
		docstatus=1,
		material_request_type="Material Transfer",
		set_from_warehouse=CENTRAL,
		set_warehouse=PROCUREMENT,
		customer=None,
		_customer=None,
		items=rows if rows is not None else [_row()],
		check_permission=lambda *args, **kwargs: None,
	)


def _source(warehouse, batch=B01, qty=5.0, item=ITEM):
	return {
		"batch_no": batch,
		"item_code": item,
		"warehouse": warehouse,
		"available_qty": qty,
		"qty": qty,
	}


@contextmanager
def _world(ledger, lineage=frozenset({B01}), finder=None, batches=None, mr=None):
	"""Patch every read Settle's planner makes, plus the writer, around one request.

	``ledger`` is {(batch, warehouse): consumable qty}. ``finder`` scripts
	find_owner_rm_warehouse; by default nothing is found anywhere.
	"""
	batches = BATCHES if batches is None else batches
	real_db_get_value = frappe.db.get_value

	def db_get_value(*args, **kwargs):
		# Answer only the Batch read; hand every other call to the real get_value EXACTLY as
		# made. Re-mapping the arguments broke DocType meta loading, which calls
		# get_value(..., filters=..., as_dict=True): on a cold System Settings cache flt()
		# swallowed that TypeError and turned every quantity into 0.0.
		doctype = args[0] if args else kwargs.get("doctype")
		if doctype == "Batch":
			name = args[1] if len(args) > 1 else kwargs.get("filters")
			record = batches.get(name)
			return frappe._dict(record) if record else None
		return real_db_get_value(*args, **kwargs)

	def consumable_map(batch_nos, item_code, warehouses):
		return {
			(batch, warehouse): qty
			for (batch, warehouse), qty in ledger.items()
			if batch in batch_nos and warehouse in warehouses
		}

	def get_doc(doctype, *args, **kwargs):
		if doctype == "Material Request" and mr is not None:
			return mr
		return _REAL_GET_DOC(doctype, *args, **kwargs)

	mocks = SimpleNamespace(
		finder=MagicMock(side_effect=finder or (lambda *a, **k: None)),
		gather=MagicMock(return_value=None),
		owner_qty=MagicMock(return_value=10.0),
		execute=MagicMock(),
		lineage=MagicMock(return_value=set(lineage)),
	)
	with ExitStack() as stack:
		stack.enter_context(
			patch.object(frappe.db, "get_value", side_effect=db_get_value)
		)
		stack.enter_context(patch("frappe.get_doc", side_effect=get_doc))
		stack.enter_context(
			patch(f"{_C}._consumable_batch_qty_map", side_effect=consumable_map)
		)
		stack.enter_context(
			patch(
				f"{_C}._get_raw_material_warehouses",
				return_value=[CENTRAL, PROCUREMENT, WAXING],
			)
		)
		stack.enter_context(patch(f"{_C}.find_owner_rm_warehouse", mocks.finder))
		stack.enter_context(patch(f"{_C}._gather_convertible", mocks.gather))
		stack.enter_context(patch(f"{_C}._owner_qty_in_warehouse", mocks.owner_qty))
		stack.enter_context(patch(f"{_C}._batch_lineage", mocks.lineage))
		stack.enter_context(
			patch(
				f"{_C}._reserve_regular",
				return_value=_source(CENTRAL, batch="REG-24", qty=100.0),
			)
		)
		stack.enter_context(patch(f"{_C}._execute", mocks.execute))
		# flt(x, precision) reads the rounding method from System Settings and returns 0.0 if
		# that read raises. A fake above that broke it would silently zero every quantity, so
		# fail loudly here instead.
		assert (
			frappe.utils.flt(1.0, 3) == 1.0
		), "a fake broke System Settings: flt() zeroes"
		yield mocks


def _same_purity_at(warehouse):
	"""A finder that answers the same-purity (customer) lookup with ``warehouse``."""

	def finder(owner_customer, item_code, required, **kwargs):
		if owner_customer and not kwargs.get("search_different_purity"):
			return _source(warehouse, qty=required)
		return None

	return finder


class _Base(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Build the translation cache before any frappe.get_all fake is in place.
		frappe._("Settle")


class TestOnlyTheRowBatchCounts(_Base):
	def test_another_receipts_batch_in_the_source_does_not_count(self):
		"""MR-03988: -02 holds 10 g in Central RM, -01 holds none -- the whole 5 g is short."""
		with _world(LEDGER_03988, finder=_same_purity_at(WAXING)) as w:
			plan = cg_settle._build_plan(_mr())

		self.assertEqual(plan[0]["level"], cg_settle.LEVEL_SAME_PURITY)
		self.assertAlmostEqual(plan[0]["shortfall"], 5.0, places=3)
		w.owner_qty.assert_not_called()

	def test_enough_of_the_row_batch_in_the_source_is_nothing_to_settle(self):
		mr = _mr()
		with _world({(B01, CENTRAL): 5.0}, mr=mr) as w:
			result = cg_settle.settle_material_request(mr.name)

		self.assertEqual(result["settled"], [])
		w.finder.assert_not_called()
		w.execute.assert_not_called()

	def test_two_rows_on_the_same_batch_share_what_is_there(self):
		rows = [_row(idx=1, qty=5.0), _row(idx=2, qty=5.0)]
		with _world({(B01, CENTRAL): 7.0}, finder=_same_purity_at(WAXING)):
			plan = cg_settle._build_plan(_mr(rows))

		self.assertEqual(plan[0]["level"], cg_settle.LEVEL_AVAILABLE)
		self.assertEqual(plan[1]["level"], cg_settle.LEVEL_SAME_PURITY)
		self.assertAlmostEqual(plan[1]["shortfall"], 3.0, places=3)


class TestSourcingStaysInTheBatchLineage(_Base):
	def test_every_lookup_carries_the_lineage_and_skips_source_and_target(self):
		lineage = {B01, "KACU0040-2F10-M-G-22KT-91.6-Y-01"}
		with _world(
			{(B01, CENTRAL): 2.0}, lineage=lineage, finder=_same_purity_at(WAXING)
		) as w:
			plan = cg_settle._build_plan(_mr())

		self.assertAlmostEqual(plan[0]["shortfall"], 3.0, places=3)
		self.assertEqual(w.gather.call_args.kwargs["batch_nos"], lineage)
		same = w.finder.call_args_list[0].kwargs
		self.assertEqual(same["batch_nos"], lineage)
		# Neither the source warehouse nor the request's own target is ever a source.
		self.assertEqual(same["warehouses"], [WAXING])

	def test_a_batch_only_in_the_request_target_is_refused_with_no_entries(self):
		"""MR-03988 itself: -01 is only in RM Procurement, where the request delivers."""
		mr = _mr()
		with _world(LEDGER_03988, mr=mr) as w, self.assertRaises(
			frappe.ValidationError
		) as raised:
			cg_settle.settle_material_request(mr.name)

		message = str(raised.exception)
		self.assertIn(B01, message)
		self.assertIn(PROCUREMENT, message)
		self.assertIn("already in", message)
		self.assertIn("19", message)
		w.execute.assert_not_called()
		# The conversion lookup was also confined to the batch's lineage.
		different = [
			c.kwargs
			for c in w.finder.call_args_list
			if c.kwargs.get("search_different_purity")
		]
		self.assertEqual(different[0]["batch_nos"], {B01})


class TestTheRowBatchIsValidated(_Base):
	def _refused(self, batches):
		with _world({(B01, CENTRAL): 5.0}, batches=batches) as w, self.assertRaises(
			frappe.ValidationError
		) as raised:
			cg_settle._build_plan(_mr())
		w.lineage.assert_not_called()
		return str(raised.exception)

	def test_another_customers_batch_is_refused(self):
		message = self._refused(
			{B01: {"item": ITEM, "custom_customer": "OTHER", "disabled": 0}}
		)
		self.assertIn("not held for customer", message)

	def test_a_batch_of_another_item_is_refused(self):
		message = self._refused(
			{
				B01: {
					"item": "M-G-22KT-91.6-Y",
					"custom_customer": CUSTOMER,
					"disabled": 0,
				}
			}
		)
		self.assertIn("is not a batch of", message)

	def test_a_missing_or_disabled_batch_is_refused(self):
		self.assertIn("does not exist or is disabled", self._refused({}))


class TestRowsWithoutABatchAreUnchanged(_Base):
	def test_the_customer_wide_count_still_applies(self):
		with _world({}) as w:
			plan = cg_settle._build_plan(_mr([_row(batch_no=None)]))

		self.assertEqual(plan[0]["level"], cg_settle.LEVEL_AVAILABLE)
		w.owner_qty.assert_called_once()
		w.lineage.assert_not_called()

	def test_lookups_are_not_batch_scoped(self):
		with _world({}, finder=_same_purity_at(WAXING)) as w:
			w.owner_qty.return_value = 0.0
			cg_settle._build_plan(_mr([_row(batch_no=None)]))

		self.assertIsNone(w.gather.call_args.kwargs["batch_nos"])
		self.assertIsNone(w.finder.call_args_list[0].kwargs["batch_nos"])
		self.assertEqual(w.finder.call_args_list[0].kwargs["warehouses"], [WAXING])


class TestBatchLineage(_Base):
	def test_keeps_the_customers_batches_adds_the_root_and_caches(self):
		def get_all(doctype, *args, **kwargs):
			if doctype == "Batch":
				# C2 belongs to another customer, so the owner filter drops it.
				return ["C1"]
			return _REAL_GET_ALL(doctype, *args, **kwargs)

		cache = {}
		scope_of = {B01, "C1", "C2"}
		with patch(f"{_TRACE}.discover_scope", return_value=scope_of) as scope, patch(
			"frappe.get_all", side_effect=get_all
		):
			first = cg_settle._batch_lineage(B01, CUSTOMER, cache)
			second = cg_settle._batch_lineage(B01, CUSTOMER, cache)

		self.assertEqual(first, {B01, "C1"})
		self.assertIs(first, second)
		scope.assert_called_once()


class TestFinderBatchScope(_Base):
	def _filters(self, **kwargs):
		seen = []

		def get_all(doctype, *args, **kw):
			if doctype == "Batch":
				seen.append(kw.get("filters"))
				return []
			return _REAL_GET_ALL(doctype, *args, **kw)

		with patch("frappe.get_all", side_effect=get_all):
			result = snc._find_available_owner_batch(
				CUSTOMER, ITEM, 5.0, warehouses=[WAXING], **kwargs
			)
		self.assertIsNone(result)
		return seen

	def test_batch_nos_adds_a_name_filter(self):
		seen = self._filters(batch_nos={B01})
		self.assertEqual(seen[0]["name"], ["in", [B01]])
		self.assertEqual(seen[0]["custom_customer"], CUSTOMER)

	def test_an_empty_batch_scope_finds_nothing_without_querying(self):
		self.assertEqual(self._filters(batch_nos=set()), [])

	def test_no_batch_nos_keeps_the_old_filters(self):
		self.assertNotIn("name", self._filters()[0])


if __name__ == "__main__":
	unittest.main()
