# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Settle on a customer-goods-return Material Request works by customer AND the row's batch.

The request's own transfer is batch-strict, so only the row's batch counts as already in
the source warehouse, and only that batch's own metal (itself or metal produced from it)
may make up a shortfall. Another receipt's batch of the same customer never settles it.

Shaped on KGJPL-MR-MT-26-03988: 5 g of batch -01 asked from Central RM, where -01 holds
nothing (all 19 g sit in RM Procurement, the request's target) while the customer's other
batch -02 holds 10 g.

When the lineage cannot cover a row, the refusal says where the batch's metal is and how much
the row can be settled for -- shaped on KGJPL-MR-MT-26-03997, whose 24KT receipt was converted
to 22KT and partly issued to a work order. And a conversion into an item without the Customer
Goods flag is refused before any entry is posted. Pure; for CI.
"""

import re
import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.sub_utils import cg_settle, snc
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log import mop_log

_C = "jewellery_erpnext.customer_subcontracting.sub_utils.cg_settle"
_LOCK = "jewellery_erpnext.jewellery_erpnext.lock_order"
_MOP_LOG = "jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log"
_MOP = (
	"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation."
	"manufacturing_operation"
)
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

#: MR-03997: customer GJCU0009's 5 g of 24KT (-02) was converted to 5.45 g of 22KT (-02-A),
#: and 2.36 g of that issued to a work order operation in Waxing WO.
WAXING_WO = "Waxing WO - KGJPL"
ITEM_22KT = "M-G-22KT-91.75-Y"
CUSTOMER_03997 = "GJCU0009"
R02 = "GJCU0009-2F10-M-G-24KT-99.9-Y-02"
R02_A = "GJCU0009-2F10-M-G-22KT-91.75-Y-02-A"
#: MR-03999: KACU0040's 10 g of 24KT (-03), half of it converted to 5.45 g of 22KT (-03-A),
#: both still in Waxing RM -- no single holding covers the 10 g.
R03 = "KACU0040-2F10-M-G-24KT-99.9-Y-03"
R03_A = "KACU0040-2F10-M-G-22KT-91.75-Y-03-A"
#: The receipt that minted -02.
RECEIPT = "KGJPL-SE-CGR-26-00007"
#: The company-gold order -02-A was issued to (MAT-STE-57625), and its operation.
WORK_ORDER = "MWO-KGJPL-RI00413-004-1-91.75-Y-01"
OPERATION = "MOP-2610-H1LFBE"
#: Another work-order warehouse, for a second holding.
CASTING_WO = "Casting WO - KGJPL"


def _customer_batch(customer, minted_by=RECEIPT):
	return {
		"item": ITEM,
		"custom_customer": customer,
		"disabled": 0,
		"custom_inventory_type": "Customer Goods",
		"reference_doctype": "Stock Entry",
		"reference_name": minted_by,
	}


#: The batch master as the validation and the Customer Goods guard read it.
BATCHES = {
	B01: _customer_batch(CUSTOMER),
	B02: _customer_batch(CUSTOMER),
	R02: _customer_batch(CUSTOMER_03997),
	R03: _customer_batch(CUSTOMER),
}

#: Stock Entry Type of each entry a batch above was minted by.
ENTRY_TYPES = {RECEIPT: "Customer Goods Received"}

#: What each batch is, and each warehouse's type, for the lineage holdings.
BATCH_ITEMS = {
	B01: ITEM,
	B02: ITEM,
	R02: ITEM,
	R02_A: ITEM_22KT,
	R03: ITEM,
	R03_A: ITEM_22KT,
}
WAREHOUSE_TYPES = {
	CENTRAL: "Raw Material",
	PROCUREMENT: "Raw Material",
	WAXING: "Raw Material",
	WAXING_WO: "Manufacturing",
	CASTING_WO: "Manufacturing",
}

#: MR-03988's ledger: -01 is back in RM Procurement, -02 sits in Central RM.
LEDGER_03988 = {(B01, PROCUREMENT): 19.0, (B02, CENTRAL): 10.0}

#: MR-03997's ledger: -02 is empty; -02-A is free in Waxing RM and inside the work order.
LEDGER_03997 = {(R02_A, WAXING): 3.09, (R02_A, WAXING_WO): 2.36}


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


def _mr(rows=None, name="KGJPL-MR-MT-26-03988"):
	return _Doc(
		name=name,
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
def _world(
	ledger,
	lineage=frozenset({B01}),
	finder=None,
	batches=None,
	mr=None,
	work_orders=None,
):
	"""Patch every read Settle's planner makes, plus the writers, around one request.

	``ledger`` is {(batch, warehouse): consumable qty}, and also the lineage's holdings the
	refusal lists. ``finder`` scripts find_owner_rm_warehouse; by default nothing is found
	anywhere. The Customer Goods flag (``mocks.flag``) is on unless a test turns it off.
	``work_orders`` are the work-order holdings of the lineage (_holding); taking one back
	(``mocks.pull``) moves it in ``ledger`` from its work-order warehouse to its landing.
	``mocks.events`` records the pulls and the settle, in order.
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
		if doctype == "Stock Entry" and args[2:3] == ("stock_entry_type",):
			return ENTRY_TYPES.get(args[1])
		return real_db_get_value(*args, **kwargs)

	def consumable_map(batch_nos, item_code, warehouses):
		return {
			(batch, warehouse): qty
			for (batch, warehouse), qty in ledger.items()
			if batch in batch_nos and warehouse in warehouses
		}

	def lineage_holdings(batch_nos):
		return [
			frappe._dict(
				batch_no=batch,
				item_code=BATCH_ITEMS[batch],
				warehouse=warehouse,
				warehouse_type=WAREHOUSE_TYPES.get(warehouse),
				qty=qty,
			)
			for (batch, warehouse), qty in ledger.items()
			if batch in batch_nos and qty > cg_settle.TOLERANCE
		]

	events = []

	def pull(mr_doc, holding):
		ledger[(holding.batch_no, holding.warehouse)] = flt(
			ledger.get((holding.batch_no, holding.warehouse), 0) - holding.qty, 3
		)
		ledger[(holding.batch_no, holding.landing)] = flt(
			ledger.get((holding.batch_no, holding.landing), 0) + holding.qty, 3
		)
		events.append(("pull", holding.work_order))
		return frappe._dict(holding, regular_entry="SE-REG", customer_entry="SE-CUST")

	def get_doc(doctype, *args, **kwargs):
		if doctype == "Material Request" and mr is not None:
			return mr
		if doctype == "Batch" and args and args[0] in batches:
			return frappe._dict(batches[args[0]], name=args[0])
		return _REAL_GET_DOC(doctype, *args, **kwargs)

	mocks = SimpleNamespace(
		finder=MagicMock(side_effect=finder or (lambda *a, **k: None)),
		gather=MagicMock(return_value=None),
		owner_qty=MagicMock(return_value=10.0),
		execute=MagicMock(side_effect=lambda *a, **k: events.append(("execute",))),
		lineage=MagicMock(return_value=set(lineage)),
		flag=MagicMock(return_value=True),
		work_orders=MagicMock(
			side_effect=lambda *a, **k: [frappe._dict(h) for h in work_orders or ()]
		),
		pull=MagicMock(side_effect=pull),
		events=events,
		# get_batch_qty nets reservations; by default nothing is reserved.
		batch_qty=MagicMock(
			side_effect=lambda batch_no=None,
			warehouse=None,
			item_code=None: ledger.get((batch_no, warehouse), 0)
		),
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
		stack.enter_context(
			patch(f"{_C}._fetch_lineage_holdings", side_effect=lineage_holdings)
		)
		stack.enter_context(patch(f"{_C}._get_item_purity", side_effect=_purity))
		stack.enter_context(patch(f"{_C}.can_be_customer_goods", mocks.flag))
		stack.enter_context(
			patch(f"{_C}._fetch_work_order_holdings", mocks.work_orders)
		)
		stack.enter_context(patch(f"{_C}._pull_from_work_order", mocks.pull))
		stack.enter_context(patch(f"{_C}.get_batch_qty", mocks.batch_qty))
		for name in ("preallocate_series_for_docs", "lock_bins", "series_stubs"):
			stack.enter_context(patch(f"{_LOCK}.{name}"))
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


def _convert_from(warehouse, batch, item):
	"""A finder that answers only the different-purity (customer) lookup."""

	def finder(owner_customer, item_code, required, **kwargs):
		if owner_customer and kwargs.get("search_different_purity"):
			return _source(warehouse, batch=batch, qty=required, item=item)
		return None

	return finder


def _purity(item_code):
	"""Purity from the item code, as _get_item_purity falls back to: M-G-22KT-91.75-Y."""
	return float(item_code.split("-")[3])


def _row_03997(**fields):
	values = {
		"qty": 3.5,
		"custom_pure_qty": 3.5,
		"batch_no": R02,
		"customer": CUSTOMER_03997,
	}
	values.update(fields)
	return _row(**values)


def _holding(**fields):
	"""MR-03997's work-order holding as _fetch_work_order_holdings returns it: 2.36 g of -02-A
	reserved in Waxing WO for a company-gold order whose operation has not started."""
	values = {
		"reservation": "2j9cif0g4c",
		"batch_no": R02_A,
		"item_code": ITEM_22KT,
		"warehouse": WAXING_WO,
		"qty": 2.36,
		"work_order": WORK_ORDER,
		"work_order_docstatus": 1,
		"operation": OPERATION,
		"operation_status": "Not Started",
		"department_ir_status": "",
		"department": "Waxing - KGJPL",
		"customer_gold": 0,
		"pending_elsewhere": False,
		"eod_target": None,
		"draft_ir": False,
		"operation_balance": 2.36,
		"in_warehouse": 2.36,
		"department_rm": WAXING,
	}
	values.update(fields)
	return values


def _ledger_finder(ledger):
	"""find_owner_rm_warehouse over the fake ledger as it stands when asked: Regular Stock
	("REG-22") wherever it is looked for, and one lineage batch in one warehouse that covers
	the requirement -- the same purity by weight, another by pure weight."""

	def finder(owner_customer, item_code, required, **kwargs):
		warehouses = kwargs.get("warehouses") or []
		if owner_customer is None:
			return _source(warehouses[0], batch="REG-22", qty=required, item=item_code)
		different = kwargs.get("search_different_purity")
		for (batch, warehouse), qty in ledger.items():
			batch_item = BATCH_ITEMS[batch]
			if warehouse not in warehouses or (batch_item != item_code) != bool(
				different
			):
				continue
			needed = (
				flt(required / (_purity(batch_item) / 100), 3)
				if different
				else required
			)
			if qty >= needed - cg_settle.TOLERANCE:
				return _source(warehouse, batch=batch, qty=needed, item=batch_item)
		return None

	return finder


def _text(message):
	"""The refusal without its HTML: list items on their own lines, tags dropped."""
	return re.sub(r"<[^>]+>", "", message.replace("<li>", "\n- "))


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


class TestTheRefusalSaysWhereTheMetalIs(_Base):
	"""MR-03997: 3.5 g of -02 asked from Central RM. -02 is empty; its metal is now 22KT,
	3.09 g free in Waxing RM and 2.36 g inside a work order."""

	def _refusal(self, ledger, rows=None):
		mr = _mr(rows or [_row_03997()], name="KGJPL-MR-MT-26-03997")
		with _world(ledger, lineage={R02, R02_A}, mr=mr) as w, self.assertRaises(
			frappe.ValidationError
		) as raised:
			cg_settle.settle_material_request(mr.name)
		w.execute.assert_not_called()
		return _text(str(raised.exception))

	def test_mr_03997_lists_free_and_work_order_metal_and_the_ceiling(self):
		text = self._refusal(LEDGER_03997)

		# Free: 3.09 g of 22KT is 2.835 g of 24KT on the row's pure basis (3.5 for 3.5).
		self.assertIn(
			f"- 3.09 of {ITEM_22KT} (batch {R02_A}) in {WAXING} = 2.835 of {ITEM}", text
		)
		# The work order's 2.36 g is shown, with its warehouse type, but never offered.
		self.assertIn(
			f"- 2.36 of {ITEM_22KT} (batch {R02_A}) in {WAXING_WO} (Manufacturing) "
			f"= 2.165 of {ITEM}",
			text,
		)
		self.assertIn("Outside Raw Material warehouses", text)
		self.assertIn(
			f"This row can be settled for at most 2.835 of {ITEM} now: reduce its "
			"quantity to that, or bring the metal outside Raw Material warehouses back",
			text,
		)

	def test_the_ceiling_adds_what_the_source_holds_and_rounds_down(self):
		# 0.5 g of -02 is already in Central RM, so 3.0 g is short. 1 g of 22KT is
		# 0.9175 g of 24KT: rounded up to 0.918, Settle would need 1.001 g of a 1 g batch,
		# so the ceiling rounds down to 0.917 and the row to 0.5 + 0.917.
		text = self._refusal({(R02, CENTRAL): 0.5, (R02_A, WAXING): 1.0})

		self.assertIn(
			f"at most 1.417 of {ITEM} now: reduce its quantity to that.", text
		)
		self.assertNotIn("Outside Raw Material warehouses", text)

	def test_enough_on_the_ledger_but_reserved_blames_reservations(self):
		# All 5.45 g are on the ledger in Waxing RM -- 5.0 g of 24KT, more than the 3.5 g
		# needed -- but Stock Reservation Entries hold all of it (get_batch_qty is 0).
		mr = _mr([_row_03997()], name="KGJPL-MR-MT-26-03997")
		with _world(
			{(R02_A, WAXING): 5.45}, lineage={R02, R02_A}, mr=mr
		) as w, self.assertRaises(frappe.ValidationError) as raised:
			w.batch_qty.side_effect = lambda **kwargs: 0
			cg_settle.settle_material_request(mr.name)

		text = _text(str(raised.exception))
		self.assertIn("part of it is reserved", text)
		self.assertNotIn("at most", text)
		w.execute.assert_not_called()

	def test_a_spent_batch_says_nothing_is_left(self):
		text = self._refusal({})

		self.assertIn("None of this batch's metal is left in any warehouse.", text)
		self.assertNotIn("at most", text)


class TestHoldingsAreSortedBySettlesRules(_Base):
	SOURCES = {CENTRAL, PROCUREMENT, WAXING}

	@staticmethod
	def _holding(batch, item, warehouse, qty):
		return frappe._dict(
			batch_no=batch,
			item_code=item,
			warehouse=warehouse,
			warehouse_type=WAREHOUSE_TYPES.get(warehouse),
			qty=qty,
		)

	def _summary(self, rows):
		with patch(f"{_C}._get_item_purity", side_effect=_purity):
			return cg_settle._summarise_holdings(
				rows, R02, ITEM, CENTRAL, PROCUREMENT, 1.0, self.SOURCES
			)

	def test_each_holding_lands_where_settle_can_or_cannot_use_it(self):
		finding = "F-G-22KT-91.75-Y-BA-1RNB"
		summary = self._summary(
			[
				self._holding(R02, ITEM, CENTRAL, 1.0),  # on hand, not listed
				self._holding(R02, ITEM, PROCUREMENT, 2.0),
				self._holding(R02_A, ITEM_22KT, WAXING, 3.09),
				self._holding(R02_A, ITEM_22KT, WAXING_WO, 2.36),
				self._holding("R02-B", ITEM, CENTRAL, 1.0),
				self._holding("R02-C", ITEM_22KT, PROCUREMENT, 1.0),
				self._holding("R02-D", finding, WAXING, 4.0),
			]
		)

		self.assertEqual([(h.batch_no, h.qty) for h in summary.at_dest], [(R02, 2.0)])
		self.assertEqual(
			[(h.batch_no, h.warehouse) for h in summary.outside], [(R02_A, WAXING_WO)]
		)
		notes = {h.batch_no: h.get("note") for h in summary.free}
		self.assertIsNone(notes[R02_A])
		self.assertIn("relabel", notes["R02-B"])
		self.assertIn("target warehouse", notes["R02-C"])
		self.assertIn("does not convert", notes["R02-D"])
		# Only the 22KT in Waxing RM counts: 3.09 x 0.9175 = 2.835075, rounded down.
		self.assertEqual(summary.coverable, 2.835)

	def test_every_usable_holding_adds_up(self):
		# Settle combines holdings (_plan_parts): 2 x 1.5 g of 18KT in the source warehouse
		# (2.25 g) and 2 g of 22KT elsewhere (1.835 g) together cover 4.085 g.
		item_18kt = "M-G-18KT-75.0-Y"
		summary = self._summary(
			[
				self._holding("R02-E", item_18kt, CENTRAL, 1.5),
				self._holding("R02-F", item_18kt, CENTRAL, 1.5),
				self._holding(R02_A, ITEM_22KT, WAXING, 2.0),
			]
		)

		self.assertEqual(summary.coverable, 4.085)

	def test_only_purities_settle_converts_count(self):
		self.assertTrue(cg_settle._converts_into(ITEM_22KT, ITEM))
		self.assertTrue(cg_settle._converts_into(ITEM, ITEM_22KT))
		# Another colour, the same karat, another karat family, a finding.
		self.assertFalse(cg_settle._converts_into("M-G-22KT-91.75-W", ITEM))
		self.assertFalse(cg_settle._converts_into("M-G-22KT-91.6-Y", ITEM_22KT))
		self.assertFalse(cg_settle._converts_into("M-G-14KT-58.5-Y", ITEM))
		self.assertFalse(cg_settle._converts_into("F-G-22KT-91.75-Y-BA", ITEM))


class TestCustomerGoodsFlagIsCheckedFirst(_Base):
	"""MR-03997 once the work-order metal is back: 5.45 g of 22KT free in Waxing RM, so
	Settle converts it into -02 -- a Customer Goods batch of M-G-24KT-99.9-Y, whose
	'Inventory Type Can be Customer Goods' was unticked."""

	LEDGER = {(R02_A, WAXING): 5.45}

	@contextmanager
	def _request(self, finder, flag, rows=None, batches=None, owner_qty=None):
		mr = _mr(rows or [_row_03997()], name="KGJPL-MR-MT-26-03997")
		with _world(
			self.LEDGER,
			lineage={R02, R02_A},
			finder=finder,
			batches=batches,
			mr=mr,
		) as w:
			w.flag.return_value = flag
			if owner_qty is not None:
				w.owner_qty.return_value = owner_qty
			yield w, mr

	def _settle(self, finder, flag, **kwargs):
		with self._request(finder, flag, **kwargs) as (w, mr):
			return w, cg_settle.settle_material_request(mr.name)

	def _refusal(self, finder, flag, **kwargs):
		with self._request(finder, flag, **kwargs) as (w, mr), self.assertRaises(
			frappe.ValidationError
		) as raised:
			cg_settle.settle_material_request(mr.name)
		w.execute.assert_not_called()
		return w, _text(str(raised.exception))

	def test_flag_off_refuses_before_any_entry(self):
		w, text = self._refusal(_convert_from(WAXING, R02_A, ITEM_22KT), flag=False)

		self.assertIn(f"into batch {R02} of {ITEM}", text)
		self.assertIn("'Inventory Type Can be Customer Goods'", text)
		w.flag.assert_called_once_with(ITEM)

	def test_flag_on_settles(self):
		w, result = self._settle(_convert_from(WAXING, R02_A, ITEM_22KT), flag=True)

		self.assertEqual(result["settled"], [1])
		w.execute.assert_called_once()
		self.assertEqual(
			w.execute.call_args.args[1][0]["level"], cg_settle.LEVEL_CONVERT
		)

	def test_a_batch_the_guard_exempts_is_not_refused(self):
		# -02 minted by a Subcontracting Repack: the Batch guard re-saves it without the
		# flag, so Settle must not refuse what the guard would let through.
		batches = dict(BATCHES, **{R02: _customer_batch(CUSTOMER_03997, "SE-SUB-1")})
		with patch.dict(ENTRY_TYPES, {"SE-SUB-1": "Subcontracting Repack"}):
			w, result = self._settle(
				_convert_from(WAXING, R02_A, ITEM_22KT), flag=False, batches=batches
			)

		self.assertEqual(result["settled"], [1])
		w.execute.assert_called_once()

	def test_moving_the_requests_own_batch_needs_no_flag(self):
		def own_batch(owner_customer, item_code, required, **kwargs):
			if owner_customer and not kwargs.get("search_different_purity"):
				return _source(WAXING, batch=R02, qty=required)
			return None

		w, result = self._settle(own_batch, flag=False)

		self.assertEqual(result["settled"], [1])
		w.flag.assert_not_called()

	def test_relabelling_another_batch_needs_the_flag(self):
		def other_batch(owner_customer, item_code, required, **kwargs):
			if owner_customer and not kwargs.get("search_different_purity"):
				return _source(WAXING, batch="R02-B", qty=required)
			return None

		_w, text = self._refusal(other_batch, flag=False)

		self.assertIn(f"into batch {R02} of {ITEM}", text)

	def test_a_row_without_a_batch_names_a_new_customer_batch(self):
		_w, text = self._refusal(
			_convert_from(WAXING, R02_A, ITEM_22KT),
			flag=False,
			rows=[_row_03997(batch_no=None)],
			owner_qty=0.0,
		)

		self.assertIn(f"into a new customer batch of {ITEM}", text)


class TestCoveringARowFromSeveralHoldings(_Base):
	"""MR-03999: 10 g of -03 asked from Central RM; -03 holds 5.0 g and its conversion -03-A
	5.45 g (5.0 g of 24KT), both in Waxing RM. Together they are exactly the 10 g."""

	LEDGER = {(R03, WAXING): 5.0, (R03_A, WAXING): 5.45}

	@staticmethod
	def _row(**fields):
		values = {
			"qty": 10.0,
			"custom_pure_qty": 10.0,
			"batch_no": R03,
			"customer": CUSTOMER,
		}
		values.update(fields)
		return _row(**values)

	def _plan(self, ledger, rows=None, batch_qty=None):
		with _world(dict(ledger), lineage={R03, R03_A}) as w:
			if batch_qty:
				w.batch_qty.side_effect = batch_qty
			return w, cg_settle._build_plan(_mr(rows or [self._row()]))

	def test_mr_03999_takes_the_batch_and_its_conversion_together(self):
		mr = _mr([self._row()], name="KGJPL-MR-MT-26-03999")
		with _world(dict(self.LEDGER), lineage={R03, R03_A}, mr=mr) as w:
			result = cg_settle.settle_material_request(mr.name)

		parts = w.execute.call_args.args[1]
		self.assertEqual(
			[
				(
					p["level"],
					p["src"]["batch_no"],
					p["src"]["warehouse"],
					p["src"]["qty"],
				)
				for p in parts
			],
			[
				(cg_settle.LEVEL_SAME_PURITY, R03, WAXING, 5.0),
				(cg_settle.LEVEL_CONVERT, R03_A, WAXING, 5.45),
			],
		)
		# Each part brings 5.0 g of the request's item, into the request's batch, and has
		# its own balancing Regular Stock.
		self.assertEqual([p["shortfall"] for p in parts], [5.0, 5.0])
		self.assertTrue(all(p["mr_batch"] == R03 for p in parts))
		self.assertTrue(all(p["regular"] for p in parts))
		self.assertEqual(result["settled"], [1])

	def test_a_single_holding_that_covers_is_still_used_alone(self):
		w, plan = self._plan({(R03, WAXING): 10.0})

		# The finder (scripted to find nothing here) is asked first; the one holding that
		# covers becomes one part.
		self.assertEqual(len(plan), 1)
		self.assertEqual(plan[0]["level"], cg_settle.LEVEL_SAME_PURITY)
		self.assertEqual(plan[0]["src"]["qty"], 10.0)

	def test_converting_in_the_source_warehouse_comes_first_then_same_purity(self):
		ledger = dict(self.LEDGER, **{})
		ledger[
			(R03_A, CENTRAL)
		] = 2.0  # 1.835 g of 24KT, converted in Central RM itself
		_w, plan = self._plan(ledger)

		self.assertEqual(
			[(p["level"], p["src"]["warehouse"], p["shortfall"]) for p in plan],
			[
				(cg_settle.LEVEL_CONVERT_IN_PLACE, CENTRAL, 1.835),
				(cg_settle.LEVEL_SAME_PURITY, WAXING, 5.0),
				(cg_settle.LEVEL_CONVERT, WAXING, 3.165),
			],
		)
		self.assertEqual(plan[0]["src"]["rows"], [{"batch_no": R03_A, "qty": 2.0}])
		# 3.165 g of 24KT needs 3.45 g of the 5.45 g of 22KT.
		self.assertEqual(plan[2]["src"]["qty"], 3.45)

	def test_no_part_beyond_what_the_row_needs(self):
		_w, plan = self._plan(
			{(R03, WAXING): 8.0, (R03_A, WAXING): 5.45}, rows=[self._row(qty=6.0)]
		)

		self.assertEqual(
			[(p["level"], p["shortfall"]) for p in plan],
			[(cg_settle.LEVEL_SAME_PURITY, 6.0)],
		)

	def test_reserved_metal_is_left_and_a_short_total_is_refused(self):
		# 2 g of -03 is reserved: 3 g + 5 g of conversion is 8 g, short of 10.
		def batch_qty(batch_no=None, warehouse=None, item_code=None):
			reserved = 2.0 if batch_no == R03 else 0.0
			return self.LEDGER.get((batch_no, warehouse), 0) - reserved

		mr = _mr([self._row()], name="KGJPL-MR-MT-26-03999")
		with _world(dict(self.LEDGER), lineage={R03, R03_A}, mr=mr) as w:
			w.batch_qty.side_effect = batch_qty
			with self.assertRaises(frappe.ValidationError) as raised:
				cg_settle.settle_material_request(mr.name)

		self.assertIn("part of it is reserved", _text(str(raised.exception)))
		w.execute.assert_not_called()

	def test_parts_claim_nothing_when_they_cannot_cover(self):
		allocated = {}
		with _world(dict(self.LEDGER), lineage={R03, R03_A}):
			holdings = [
				frappe._dict(
					batch_no=batch,
					item_code=BATCH_ITEMS[batch],
					warehouse=warehouse,
					warehouse_type="Raw Material",
					qty=qty,
				)
				for (batch, warehouse), qty in self.LEDGER.items()
			]
			parts = cg_settle._plan_parts(
				self._row(qty=12.0, custom_pure_qty=12.0),
				{"row": 1, "item": ITEM, "mr_batch": R03},
				ITEM,
				12.0,
				CENTRAL,
				[WAXING],
				"KG GK Jewellers Private Limited",
				allocated,
				holdings,
			)

		self.assertIsNone(parts)
		self.assertEqual(allocated, {})

	def test_a_conversion_never_consumes_more_than_is_free(self):
		for thousandths in range(1000, 3000):
			free = thousandths / 1000
			take, need = cg_settle._conversion_take(free, 10.0, 91.75, 1.0)
			self.assertLessEqual(need, free, msg=free)
			# What it makes needs no more than it consumes, by pure weight.
			self.assertLessEqual(take, need * 0.9175 + 0.0005, msg=free)


class TestTakingTheBatchBackFromAWorkOrder(_Base):
	"""MR-03997: 3.09 g of -02-A free in Waxing RM is short of the 3.815 g the return needs;
	the other 2.36 g sits in a company-gold order's operation that has not started."""

	def _settle(self, ledger, work_orders, rows=None, flag=True):
		mr = _mr(rows or [_row_03997()], name="KGJPL-MR-MT-26-03997")
		with _world(
			ledger,
			lineage={R02, R02_A},
			finder=_ledger_finder(ledger),
			mr=mr,
			work_orders=work_orders,
		) as w:
			w.flag.return_value = flag
			return w, cg_settle.settle_material_request(mr.name)

	def _refusal(self, ledger, work_orders, flag=True):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._settle(ledger, work_orders, flag=flag)
		return _text(str(raised.exception))

	def test_mr_03997_takes_the_work_order_metal_back_then_converts(self):
		ledger = dict(LEDGER_03997)
		w, result = self._settle(ledger, [_holding()])

		# The whole holding comes back to Waxing RM, where the rest of -02-A is, and Regular
		# Stock from there goes to the work order in its place.
		w.pull.assert_called_once()
		pull = w.pull.call_args.args[1]
		self.assertEqual(
			(pull.work_order, pull.batch_no, pull.qty, pull.warehouse, pull.landing),
			(WORK_ORDER, R02_A, 2.36, WAXING_WO, WAXING),
		)
		self.assertEqual(pull.regular["batch_no"], "REG-22")
		self.assertEqual(pull.regular["warehouse"], WAXING)
		self.assertEqual(pull.customer, CUSTOMER_03997)
		# Then, planned again on the ledger as it now stands: 5.45 g in Waxing RM, of which
		# 3.815 g converts into the request's 3.5 g.
		self.assertEqual(w.events, [("pull", WORK_ORDER), ("execute",)])
		entry = w.execute.call_args.args[1][0]
		self.assertEqual(entry["level"], cg_settle.LEVEL_CONVERT)
		self.assertEqual(
			(entry["src"]["batch_no"], entry["src"]["warehouse"], entry["src"]["qty"]),
			(R02_A, WAXING, 3.815),
		)
		self.assertEqual(result["settled"], [1])
		self.assertEqual(
			result["pulled"],
			[{"work_order": WORK_ORDER, "batch_no": R02_A, "qty": 2.36}],
		)

	def test_the_flag_is_checked_before_anything_is_taken(self):
		text = self._refusal(dict(LEDGER_03997), [_holding()], flag=False)

		self.assertIn("'Inventory Type Can be Customer Goods'", text)

	def test_nothing_is_taken_or_settled_when_the_flag_is_off(self):
		mr = _mr([_row_03997()], name="KGJPL-MR-MT-26-03997")
		ledger = dict(LEDGER_03997)
		with _world(
			ledger,
			lineage={R02, R02_A},
			finder=_ledger_finder(ledger),
			mr=mr,
			work_orders=[_holding()],
		) as w, self.assertRaises(frappe.ValidationError):
			w.flag.return_value = False
			cg_settle.settle_material_request(mr.name)

		w.pull.assert_not_called()
		w.execute.assert_not_called()

	def test_a_work_order_settle_may_not_touch_is_named_in_the_refusal(self):
		cases = {
			"it is a customer-gold order": {"customer_gold": 1},
			"the work order is not submitted": {"work_order_docstatus": 0},
			f"its operation {OPERATION} is WIP": {"operation_status": "WIP"},
			"its operation is In-Transit": {"department_ir_status": "In-Transit"},
			"its MOP Logs waiting for the end-of-day sync sit on another open "
			"operation": {"pending_elsewhere": True},
			"a draft Employee IR or Department IR holds it": {"draft_ir": True},
			f"its operation's MOP Log shows 1.0 and {WAXING_WO} holds 2.36, not the "
			"2.36 reserved": {"operation_balance": 1.0},
			# More on the MOP Log than reserved: the batch would not end at 0.
			f"its operation's MOP Log shows 3.0 and {WAXING_WO} holds 2.36, not the "
			"2.36 reserved": {"operation_balance": 3.0},
		}
		for reason, fields in cases.items():
			with self.subTest(reason=reason):
				text = self._refusal(dict(LEDGER_03997), [_holding(**fields)])
				self.assertIn(
					f"work order {WORK_ORDER}: not taken back, {reason}", text
				)

	def test_no_regular_stock_to_put_back_is_named(self):
		ledger = dict(LEDGER_03997)
		mr = _mr([_row_03997()], name="KGJPL-MR-MT-26-03997")
		finder = _ledger_finder(ledger)

		def no_regular(owner_customer, item_code, required, **kwargs):
			if owner_customer is None:
				return None
			return finder(owner_customer, item_code, required, **kwargs)

		with _world(
			ledger,
			lineage={R02, R02_A},
			finder=no_regular,
			mr=mr,
			work_orders=[_holding()],
		) as w, self.assertRaises(frappe.ValidationError) as raised:
			cg_settle.settle_material_request(mr.name)

		self.assertIn(
			f"not taken back, no free Regular Stock of {ITEM_22KT} in {WAXING} to put back",
			_text(str(raised.exception)),
		)
		w.pull.assert_not_called()

	def test_too_little_even_with_the_work_order_takes_nothing(self):
		# 0.5 g free + 1 g in the work order is 1.376 g of 24KT, short of 3.5 g.
		ledger = {(R02_A, WAXING): 0.5, (R02_A, WAXING_WO): 1.0}
		text = self._refusal(
			ledger, [_holding(qty=1.0, operation_balance=1.0, in_warehouse=1.0)]
		)

		self.assertIn(f"work order {WORK_ORDER}: Settle can take it back", text)
		# The ceiling counts the work order's metal: 1.5 g of 22KT is 1.376 g of 24KT.
		self.assertIn(f"at most 1.376 of {ITEM} now", text)

	def test_only_as_many_work_orders_as_the_row_needs_largest_first(self):
		ledger = {**LEDGER_03997, (R02_A, CASTING_WO): 0.5}
		second = _holding(
			reservation="sre-casting",
			work_order="MWO-CASTING",
			warehouse=CASTING_WO,
			qty=0.5,
			operation_balance=0.5,
			in_warehouse=0.5,
		)
		w, _result = self._settle(ledger, [second, _holding()])

		self.assertEqual(w.events, [("pull", WORK_ORDER), ("execute",)])

	def test_the_second_plan_never_takes_from_a_work_order(self):
		mr = _mr([_row_03997()], name="KGJPL-MR-MT-26-03997")
		ledger = dict(LEDGER_03997)
		with _world(
			ledger,
			lineage={R02, R02_A},
			finder=_ledger_finder(ledger),
			mr=mr,
			work_orders=[_holding()],
		) as w, self.assertRaises(frappe.ValidationError):
			# A pull that moved nothing: the plan after it is still short.
			w.pull.side_effect = lambda mr_doc, holding: frappe._dict(holding)
			cg_settle.settle_material_request(mr.name)

		w.work_orders.assert_called_once()
		w.execute.assert_not_called()

	def test_taking_back_the_requests_own_batch_needs_no_flag(self):
		# The receipt's 24KT itself was issued: it comes back to Waxing RM and moves to
		# Central RM as it is -- no conversion, no relabel.
		ledger = {(R02, WAXING_WO): 3.5}
		w, result = self._settle(
			ledger,
			[
				_holding(
					batch_no=R02,
					item_code=ITEM,
					qty=3.5,
					operation_balance=3.5,
					in_warehouse=3.5,
				)
			],
			flag=False,
		)

		w.flag.assert_not_called()
		entry = w.execute.call_args.args[1][0]
		self.assertEqual(entry["level"], cg_settle.LEVEL_SAME_PURITY)
		self.assertEqual(entry["src"]["batch_no"], R02)
		self.assertEqual(result["settled"], [1])


class TestWhereTheMetalGoesBack(_Base):
	SOURCES = {CENTRAL, PROCUREMENT, WAXING}

	@staticmethod
	def _free(batch_no, warehouse, qty):
		return frappe._dict(
			batch_no=batch_no, item_code=ITEM_22KT, warehouse=warehouse, qty=qty
		)

	def test_where_the_same_batch_already_has_the_most(self):
		holdings = [self._free(R02_A, CENTRAL, 1.0), self._free(R02_A, WAXING, 3.09)]
		landing = cg_settle._landing_warehouse(
			frappe._dict(_holding()), holdings, self.SOURCES, PROCUREMENT
		)

		self.assertEqual(landing, WAXING)

	def test_else_the_departments_raw_material_warehouse(self):
		landing = cg_settle._landing_warehouse(
			frappe._dict(_holding(department_rm=CENTRAL)), [], self.SOURCES, PROCUREMENT
		)

		self.assertEqual(landing, CENTRAL)

	def test_never_the_requests_destination(self):
		holdings = [self._free(R02_A, PROCUREMENT, 5.0)]
		landing = cg_settle._landing_warehouse(
			frappe._dict(_holding(department_rm=PROCUREMENT)),
			holdings,
			self.SOURCES,
			PROCUREMENT,
		)

		self.assertIsNone(landing)


class TestPullingFromAWorkOrder(_Base):
	"""_pull_from_work_order: Settle's own transfers, with the work order's bookkeeping."""

	def test_regular_in_reservation_moved_customer_out_logged_then_restamped(self):
		pull = frappe._dict(
			_holding(),
			landing=WAXING,
			customer=CUSTOMER_03997,
			regular=_source(WAXING, batch="REG-22", qty=2.36, item=ITEM_22KT),
		)
		calls = []

		def transfer(mr, item, qty, batch, from_wh, to_wh, customer):
			calls.append(("transfer", item, qty, batch, from_wh, to_wh, customer))
			return "SE-REG" if customer is None else "SE-CUST"

		with patch(f"{_C}._transfer", side_effect=transfer), patch(
			f"{_C}._move_reservation",
			side_effect=lambda *a: calls.append(("reservation", *a)) or "sre-new",
		), patch(
			f"{_C}._log_on_operation",
			side_effect=lambda entry, p, outward, flow_index: calls.append(
				("log", entry, outward, flow_index)
			),
		), patch(
			f"{_C}._restamp_snc_requirement",
			side_effect=lambda work_order: calls.append(("snc", work_order)),
		), patch(f"{_C}._highest_flow_index", return_value=2):
			done = cg_settle._pull_from_work_order(_mr(), pull)

		self.assertEqual(
			calls,
			[
				("transfer", ITEM_22KT, 2.36, "REG-22", WAXING, WAXING_WO, None),
				("reservation", "2j9cif0g4c", R02_A, "REG-22"),
				("transfer", ITEM_22KT, 2.36, R02_A, WAXING_WO, WAXING, CUSTOMER_03997),
				("log", "SE-REG", False, 2),
				("log", "SE-CUST", True, 2),
				("snc", WORK_ORDER),
			],
		)
		self.assertEqual(
			(done.regular_entry, done.customer_entry, done.new_reservation),
			("SE-REG", "SE-CUST", "sre-new"),
		)

	def test_the_reservation_keeps_its_line_and_swaps_one_batch(self):
		original = SimpleNamespace(name="2j9cif0g4c", flags=SimpleNamespace())
		order = []
		original.cancel = lambda: order.append("cancel")
		rows = [
			frappe._dict(batch_no=R02_A, qty=2.36, delivered_qty=0),
			frappe._dict(batch_no="OTHER", qty=1.0, delivered_qty=0.5),
		]

		def get_all(doctype, *args, **kwargs):
			if doctype == "Serial and Batch Entry":
				return rows
			return _REAL_GET_ALL(doctype, *args, **kwargs)

		def get_doc(doctype, *args, **kwargs):
			if doctype == "Stock Reservation Entry":
				return original
			return _REAL_GET_DOC(doctype, *args, **kwargs)

		with patch("frappe.get_all", side_effect=get_all), patch(
			"frappe.get_doc", side_effect=get_doc
		), patch(
			f"{_MOP}._build_replacement_sre",
			side_effect=lambda *a, **k: order.append("rebuild") or "sre-new",
		) as rebuild:
			name = cg_settle._move_reservation("2j9cif0g4c", R02_A, "REG-22")

		self.assertEqual(name, "sre-new")
		self.assertEqual(order, ["cancel", "rebuild"])
		args, kwargs = rebuild.call_args
		self.assertIs(args[0], original)
		self.assertAlmostEqual(args[1], 2.86, places=3)
		self.assertEqual(
			kwargs["sb_remaining"],
			[{"batch_no": "REG-22", "qty": 2.36}, {"batch_no": "OTHER", "qty": 0.5}],
		)

	def test_each_leg_is_logged_on_the_operation_with_its_direction(self):
		row = SimpleNamespace(
			item_code=ITEM_22KT,
			qty=2.36,
			batch_no=R02_A,
			s_warehouse=WAXING_WO,
			t_warehouse=WAXING,
			name="row-1",
			serial_and_batch_bundle="SBB-1",
		)
		entry = SimpleNamespace(
			doctype="Stock Entry",
			name="SE-CUST",
			stock_entry_type="Material Transfer",
			items=[row],
		)

		def get_doc(doctype, *args, **kwargs):
			if doctype == "Stock Entry":
				return entry
			return _REAL_GET_DOC(doctype, *args, **kwargs)

		with patch("frappe.get_doc", side_effect=get_doc), patch(
			f"{_MOP_LOG}.create_mop_log_for_stock_transfer_to_mo"
		) as write:
			cg_settle._log_on_operation(
				"SE-CUST", frappe._dict(_holding()), outward=True, flow_index=3
			)

		doc, logged = write.call_args.args
		self.assertEqual(
			(doc.name, doc.stock_entry_type, doc.manufacturing_work_order),
			("SE-CUST", "Material Transfer", WORK_ORDER),
		)
		self.assertEqual(
			(
				logged.manufacturing_operation,
				logged.batch_no,
				logged.qty,
				logged.pcs,
				logged.s_warehouse,
				logged.t_warehouse,
				logged.name,
				logged.serial_and_batch_bundle,
			),
			(OPERATION, R02_A, 2.36, 0, WAXING_WO, WAXING, "row-1", "SBB-1"),
		)
		self.assertEqual(
			write.call_args.kwargs,
			{"is_synced": True, "outward": True, "flow_index": 3},
		)

	def test_the_snc_requirement_follows_what_the_work_order_now_holds(self):
		for needs, values in (
			(False, {"snc_requirement": "Not Need"}),
			(True, {"snc_requirement": "Need", "snc_done": 0}),
		):
			with self.subTest(needs=needs), patch(
				f"{_C}._mwo_needs_settlement", return_value=needs
			), patch.object(frappe.db, "set_value") as set_value:
				cg_settle._restamp_snc_requirement(WORK_ORDER)

			set_value.assert_called_once_with(
				"Manufacturing Work Order", WORK_ORDER, values
			)

	def test_the_summary_names_the_work_order_and_the_entries(self):
		pulled = frappe._dict(
			_holding(),
			landing=WAXING,
			regular={"batch_no": "REG-22"},
			regular_entry="SE-REG",
			customer_entry="SE-CUST",
		)

		text = _text(cg_settle._pull_summary([pulled]))

		self.assertIn(
			f"Took 2.36 of {ITEM_22KT} (batch {R02_A}) back from work order {WORK_ORDER} "
			f"into {WAXING}, and gave the work order 2.36 of Regular Stock (batch REG-22) in "
			"its place: SE-REG, SE-CUST.",
			text,
		)


class TestWorkOrderMovesWaitingForTheSync(_Base):
	"""MR-04001: the work order's Department IR and Employee IR moves since 12:04 wait for the
	19:00 end-of-day sync. The ledger still has the 1.207 g in Waxing WO; the MOP Log has it
	on the current operation, bound for Model Making WO."""

	MODEL_MAKING_WO = "Model Making WO - KGJPL"

	def test_a_work_order_with_moves_waiting_for_the_sync_is_taken_back(self):
		mr = _mr([_row_03997()], name="KGJPL-MR-MT-26-03997")
		ledger = dict(LEDGER_03997)
		with _world(
			ledger,
			lineage={R02, R02_A},
			finder=_ledger_finder(ledger),
			mr=mr,
			work_orders=[_holding(eod_target=self.MODEL_MAKING_WO)],
		) as w:
			cg_settle.settle_material_request(mr.name)

		pull = w.pull.call_args.args[1]
		self.assertEqual(pull.eod_target, self.MODEL_MAKING_WO)
		self.assertEqual(w.events, [("pull", WORK_ORDER), ("execute",)])

	def _logged(self, eod_target):
		row = SimpleNamespace(
			item_code=ITEM_22KT,
			qty=2.36,
			batch_no="REG-22",
			s_warehouse=WAXING,
			t_warehouse=WAXING_WO,
			name="row-1",
			serial_and_batch_bundle="SBB-1",
		)
		entry = SimpleNamespace(
			doctype="Stock Entry",
			name="SE-REG",
			stock_entry_type="Material Transfer",
			items=[row],
		)

		def get_doc(doctype, *args, **kwargs):
			if doctype == "Stock Entry":
				return entry
			return _REAL_GET_DOC(doctype, *args, **kwargs)

		with patch("frappe.get_doc", side_effect=get_doc), patch(
			f"{_MOP_LOG}.create_mop_log_for_stock_transfer_to_mo"
		) as write:
			cg_settle._log_on_operation(
				"SE-REG",
				frappe._dict(_holding(eod_target=eod_target)),
				outward=False,
				flow_index=0,
			)
		return write.call_args.args[1], write.call_args.kwargs

	def test_with_moves_pending_the_rows_join_them(self):
		# Unsynced and bound for the sync's target: tonight the sync moves the regular batch
		# from Waxing WO to Model Making WO and skips the customer's, now at 0.
		logged, kwargs = self._logged(self.MODEL_MAKING_WO)

		self.assertEqual(logged.t_warehouse, self.MODEL_MAKING_WO)
		self.assertEqual(logged.s_warehouse, WAXING)
		self.assertEqual(
			kwargs, {"is_synced": False, "outward": False, "flow_index": 0}
		)

	def test_with_nothing_pending_the_rows_are_synced_as_before(self):
		logged, kwargs = self._logged(None)

		self.assertEqual(logged.t_warehouse, WAXING_WO)
		self.assertTrue(kwargs["is_synced"])

	def test_the_sync_target_is_where_the_pending_moves_end(self):
		def log(operation, to_warehouse, flow_index, creation):
			return frappe._dict(
				manufacturing_operation=operation,
				to_warehouse=to_warehouse,
				flow_index=flow_index,
				creation=creation,
			)

		# A finished predecessor's IR copies can sort higher (flow index 2) than the current
		# operation's rows; the sync still takes the target from the current operation.
		pending = [
			log("MOP-OLD", "Model Making Transit - KGJPL", 2, "2026-10-23 12:04:07"),
			log(OPERATION, self.MODEL_MAKING_WO, 0, "2026-10-23 12:06:28"),
			log(OPERATION, None, 0, "2026-10-23 12:06:29"),
		]
		self.assertEqual(
			cg_settle._eod_target(pending, OPERATION, "Model Making - KGJPL"),
			self.MODEL_MAKING_WO,
		)
		# Nothing on the current operation carries a target: the work order's latest.
		self.assertEqual(
			cg_settle._eod_target(pending[:1], OPERATION, "Model Making - KGJPL"),
			"Model Making Transit - KGJPL",
		)
		# No target logged at all: the department's Manufacturing warehouse.
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync."
			"_resolve_department_warehouse",
			return_value=self.MODEL_MAKING_WO,
		):
			self.assertEqual(
				cg_settle._eod_target(
					[log(OPERATION, None, 0, "x")], OPERATION, "Model Making - KGJPL"
				),
				self.MODEL_MAKING_WO,
			)

	def test_moves_pending_on_another_open_operation_hold_the_work_order(self):
		pending = [
			frappe._dict(manufacturing_operation=OPERATION),
			frappe._dict(manufacturing_operation="MOP-OTHER"),
		]
		for status, held in (("Finished", False), ("WIP", True), ("Not Started", True)):
			with self.subTest(status=status), patch(
				"frappe.get_all",
				side_effect=lambda doctype, *a, **k: [
					frappe._dict(status=status, department_ir_status="")
				]
				if doctype == "Manufacturing Operation"
				else _REAL_GET_ALL(doctype, *a, **k),
			):
				self.assertEqual(
					cg_settle._pending_on_another_open_operation(pending, OPERATION),
					held,
				)
		self.assertFalse(
			cg_settle._pending_on_another_open_operation(pending[:1], OPERATION)
		)

	def test_never_a_regular_batch_the_sync_target_already_holds(self):
		# REG-22 already sits in Model Making WO: the sync would think the regular batch was
		# already there and leave it in Waxing WO. REG-23 goes in instead.
		def finder(owner_customer, item_code, required, **kwargs):
			warehouse = kwargs["warehouses"][0]
			for batch in ("REG-22", "REG-23"):
				if kwargs["allocated"].get((batch, warehouse)) != float("inf"):
					return _source(warehouse, batch=batch, qty=required, item=item_code)
			return None

		ledger = {("REG-22", self.MODEL_MAKING_WO): 5.0}
		for target, expected in ((self.MODEL_MAKING_WO, "REG-23"), (None, "REG-22")):
			with self.subTest(target=target), _world(ledger, finder=finder):
				trial = {}
				holding = frappe._dict(_holding(eod_target=target), landing=WAXING)
				regular = cg_settle._regular_to_put_back(holding, "KGJPL", trial)

			self.assertEqual(regular["batch_no"], expected)
			self.assertEqual(trial, {(expected, WAXING): 2.36})


class TestMopLogDirection(_Base):
	"""create_mop_log_for_stock_transfer_to_mo: an explicit ``outward`` wins; without one,
	the entry type decides as before."""

	def _logged(self, stock_entry_type, outward=None, flow_index=None, last_index=0):
		saved = SimpleNamespace(save=MagicMock())
		doc = frappe._dict(
			doctype="Stock Entry",
			name="SE-1",
			stock_entry_type=stock_entry_type,
			manufacturing_work_order=WORK_ORDER,
		)
		row = frappe._dict(
			item_code=ITEM_22KT,
			qty=2.36,
			pcs=0,
			batch_no=R02_A,
			manufacturing_operation=OPERATION,
			name="row-1",
		)
		opening = dict.fromkeys(
			(
				"pcs_prefix",
				"pcs_item",
				"pcs_batch",
				"qty_prefix",
				"qty_item",
				"qty_batch",
			),
			0,
		)

		def new_doc(doctype, *args, **kwargs):
			self.assertEqual(doctype, "MOP Log")
			return saved

		with patch.object(
			mop_log, "get_mop_opening_balances", return_value=opening
		), patch.object(
			mop_log, "get_last_mop_index", return_value=last_index
		), patch.object(mop_log.frappe, "new_doc", side_effect=new_doc):
			mop_log.create_mop_log_for_stock_transfer_to_mo(
				doc, row, is_synced=True, outward=outward, flow_index=flow_index
			)
		saved.save.assert_called_once()
		return saved

	def test_an_explicit_outward_leg_is_negative(self):
		self.assertEqual(
			self._logged("Material Transfer", outward=True).qty_change, -2.36
		)
		self.assertEqual(
			self._logged("Material Transfer", outward=False).qty_change, 2.36
		)

	def test_without_it_the_entry_type_decides_as_before(self):
		self.assertEqual(
			self._logged("Material Receive (WORK ORDER)").qty_change, -2.36
		)
		self.assertEqual(
			self._logged("Material Transfer (WORK ORDER)").qty_change, 2.36
		)

	def test_an_explicit_flow_index_wins_and_the_old_rule_stays_the_default(self):
		self.assertEqual(
			self._logged("Material Transfer", flow_index=3, last_index=3).flow_index, 3
		)
		# The alternating rule: after 0 comes 1, after anything else 0.
		self.assertEqual(self._logged("Material Transfer", last_index=0).flow_index, 1)
		self.assertEqual(self._logged("Material Transfer", last_index=3).flow_index, 0)


if __name__ == "__main__":
	unittest.main()
