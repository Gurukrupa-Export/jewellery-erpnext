# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Batch form Repack (``customization/batch/repack.py``).

Most classes are DB-free, per the suite convention: ``setUpClass`` is neutralized, the
module's DB helpers are patched, and ``frappe.new_doc`` hands out recording fakes, so the
tests can assert what is created, in what order and with which arguments. They pin:

* every refusal, and that a refused request creates nothing;
* the permission semantics (Batch is not submittable, so it is checked for create and
  write; write because the bundle hooks save the new batch during submit);
* the order of operations: draft Stock Entry first, then a Batch that references it and
  its produce row, then save and submit;
* the Branch accounting dimension on the Stock Entry, its rows and both Journal Entry rows;
* the Journal Entry sides for a positive, negative and rounded-away net.

``TestBatchRepackIntegration`` runs the real flow on the CI seed company, with
``REPACK_COMPANIES`` patched to it, inside a savepoint that is rolled back.
"""

import unittest
from contextlib import ExitStack, contextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe.exceptions import FrappeTypeError
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, get_timedelta, getdate, today

from jewellery_erpnext.jewellery_erpnext.customization.batch import repack
from jewellery_erpnext.jewellery_erpnext.doc_events import (
	stock_entry as stock_entry_events,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings import eod_lock
from jewellery_erpnext.patches import (
	add_journal_entry_repack_stock_entry_field as je_link_patch,
)

COMPANY = repack.REPACK_COMPANIES[0]
OTHER_COMPANY = "Another Test Company"
ITEM = "M-G-24KT-99.9-Y"
WAREHOUSE = "Central RM - TC"
BATCH = "SRC-BATCH-1"

_MISSING = object()


class _Meta:
	def __init__(self, fields=()):
		self._fields = set(fields)

	def has_field(self, fieldname):
		return fieldname in self._fields


class _Row:
	"""A Document stand-in: unset fields read as None, like a real child row."""

	def __init__(self, meta=None, **fields):
		self.__dict__["meta"] = meta or _Meta()
		self.__dict__.update(fields)

	def __getattr__(self, name):
		if name.startswith("__"):
			raise AttributeError(name)
		return None

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value


_NAMES = {
	"Stock Entry": ("SE-REPACK-0001", "SED"),
	"Batch": ("NEW-BATCH-0001", None),
	"Journal Entry": ("JE-0001", "JEA"),
}


class _FakeDoc(_Row):
	"""Records insert / save / submit (with their arguments) in the shared recorder."""

	def __init__(self, doctype, recorder, fields=()):
		super().__init__(meta=_Meta(fields), doctype=doctype, items=[], accounts=[])
		self.__dict__["_recorder"] = recorder

	def append(self, table, row):
		child = _Row(meta=self.meta, **row)
		self.__dict__[table].append(child)
		return child

	def _log(self, action, args, kwargs):
		self._recorder.log.append(
			(
				self.doctype,
				action,
				args,
				kwargs,
				[row.batch_no for row in self.items],
			)
		)

	def insert(self, *args, **kwargs):
		self._log("insert", args, kwargs)
		name, row_prefix = _NAMES[self.doctype]
		self.name = self.get("batch_id") or name
		for idx, row in enumerate(self.items or self.accounts, 1):
			row.name = f"{row_prefix}-{idx}"
		return self

	def save(self, *args, **kwargs):
		self._log("save", args, kwargs)
		return self

	def submit(self, *args, **kwargs):
		self._log("submit", args, kwargs)
		return self


class _Recorder:
	def __init__(self, fields=()):
		self.fields = fields
		self.docs = []
		self.log = []
		self.permission_calls = []

	def new_doc(self, doctype, *args, **kwargs):
		doc = _FakeDoc(doctype, self, self.fields)
		self.docs.append(doc)
		return doc

	def of(self, doctype):
		return [doc for doc in self.docs if doc.doctype == doctype]

	def one(self, doctype):
		docs = self.of(doctype)
		assert len(docs) == 1, f"expected one {doctype}, got {len(docs)}"
		return docs[0]


def _batch(**fields):
	defaults = {
		"name": BATCH,
		"item": ITEM,
		"custom_company": COMPANY,
		"disabled": 0,
		"custom_customer": None,
		"custom_inventory_type": "Regular Stock",
		"reference_doctype": "Purchase Receipt",
		"reference_name": "PR-0001",
		"custom_voucher_detail_no": "PRI-1",
	}
	defaults.update(fields)
	return _Row(**defaults)


def _item(**fields):
	defaults = {
		"name": ITEM,
		"item_name": ITEM,
		"has_batch_no": 1,
		"create_new_batch": 1,
		"batch_number_series": None,
		"stock_uom": "Gram",
		"gst_hsn_code": "7108",
	}
	defaults.update(fields)
	return frappe._dict(defaults)


def _warehouse(**fields):
	defaults = {"name": WAREHOUSE, "company": COMPANY, "is_group": 0}
	defaults.update(fields)
	return frappe._dict(defaults)


def _accounts():
	return frappe._dict(
		stock_adjustment_account="Stock Adjustment - TC",
		cost_center="Main - TC",
		stock_received_but_not_billed="Stock Received But Not Billed - TC",
	)


def _dimension(**fields):
	defaults = {
		"fieldname": "branch",
		"label": "Branch",
		"document_type": "Branch",
		"mandatory": False,
		"company_default": None,
	}
	defaults.update(fields)
	return frappe._dict(defaults)


@contextmanager
def _repack_env(
	batch=None,
	item=_MISSING,
	warehouse=_MISSING,
	available=10,
	company_accounts=None,
	dimension=None,
	default_branch=None,
	net=0,
	denied=(),
	type_allowed=True,
	fields=(),
	link_ready=True,
):
	"""Patch every DB boundary of repack.py and record the documents it builds.

	``denied`` lists (doctype, ptype) pairs the user lacks. On top of it, the permission
	fake keeps frappe's own rule that ``submit`` on a non-submittable doctype (Batch) is
	always False. ``company_accounts`` runs the real ``_company_accounts`` against that
	Company row instead of patching it.
	"""
	recorder = _Recorder(fields)
	denied = set(denied)

	def has_permission(
		doctype=None, ptype="read", doc=None, user=None, throw=False, **kwargs
	):
		recorder.permission_calls.append((doctype, ptype))
		allowed = (doctype, ptype) not in denied and (doctype, ptype) != (
			"Batch",
			"submit",
		)
		if not allowed and throw:
			raise frappe.PermissionError
		return allowed

	with ExitStack() as stack:

		def patch_repack(name, **kwargs):
			return stack.enter_context(patch.object(repack, name, **kwargs))

		stack.enter_context(
			patch.object(repack.frappe, "has_permission", side_effect=has_permission)
		)
		stack.enter_context(
			patch.object(repack.frappe, "new_doc", side_effect=recorder.new_doc)
		)
		patch_repack("_get_batch", return_value=batch or _batch())
		patch_repack("is_type_allowed", return_value=type_allowed)
		patch_repack("_get_item", return_value=_item() if item is _MISSING else item)
		patch_repack(
			"_get_warehouse",
			return_value=_warehouse() if warehouse is _MISSING else warehouse,
		)
		patch_repack("_qty_precision", return_value=3)
		patch_repack("_available_qty", return_value=available)
		if company_accounts is None:
			patch_repack("_company_accounts", return_value=_accounts())
		else:
			stack.enter_context(
				patch.object(
					repack.frappe.db,
					"get_value",
					return_value=frappe._dict(company_accounts),
				)
			)
		patch_repack("_branch_dimension", return_value=dimension)
		patch_repack("_default_branch", return_value=default_branch)
		patch_repack("_net_stock_adjustment", return_value=net)
		patch_repack("get_name_from_hash", return_value="HASHBATCH")
		patch_repack("make_autoname", return_value="SERIES-0001")
		patch_repack("batch_uses_naming_series", return_value=False)
		patch_repack("_journal_entry_link_ready", return_value=link_ready)
		yield recorder


def _create(**kwargs):
	args = {
		"batch_no": BATCH,
		"warehouse": WAREHOUSE,
		"qty": 4,
		"target_rate": 250,
		"posting_datetime": "2026-10-10 10:15:30",
	}
	args.update(kwargs)
	return repack.create_repack_stock_entry(**args)


class TestRepackRefusals(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def assertRefused(
		self, env_kwargs=None, exc=frappe.ValidationError, **create_kwargs
	):
		with _repack_env(**(env_kwargs or {})) as recorder:
			with self.assertRaises(exc):
				_create(**create_kwargs)
		self.assertEqual(
			recorder.docs, [], "a refused repack must not build any document"
		)
		self.assertEqual(recorder.log, [])
		return recorder

	def test_batch_of_another_company_is_refused(self):
		self.assertRefused({"batch": _batch(custom_company=OTHER_COMPANY)})

	def test_batch_without_a_resolvable_company_is_refused(self):
		self.assertRefused(
			{
				"batch": _batch(
					custom_company=None, reference_doctype=None, reference_name=None
				)
			}
		)

	def test_disabled_batch_is_refused(self):
		self.assertRefused({"batch": _batch(disabled=1)})

	def test_customer_owned_batch_is_refused(self):
		for fields in (
			{"custom_customer": "CUST-0001"},
			{"custom_inventory_type": "Customer Goods"},
			{"custom_inventory_type": "Customer Stock"},
		):
			with self.subTest(**fields):
				self.assertRefused({"batch": _batch(**fields)})

	def test_warehouse_of_another_company_is_refused(self):
		self.assertRefused({"warehouse": _warehouse(company=OTHER_COMPANY)})

	def test_group_warehouse_is_refused(self):
		self.assertRefused({"warehouse": _warehouse(is_group=1)})

	def test_unknown_warehouse_is_refused(self):
		self.assertRefused({"warehouse": None})

	def test_qty_above_the_warehouse_batch_balance_is_refused(self):
		self.assertRefused({"available": 3.5}, qty=3.6)

	def test_qty_equal_to_the_balance_is_accepted(self):
		with _repack_env(available=3.5) as recorder:
			_create(qty=3.5)
		self.assertEqual(len(recorder.of("Stock Entry")), 1)

	def test_non_positive_qty_or_rate_is_refused(self):
		for kwargs in (
			{"qty": 0},
			{"qty": -1},
			{"target_rate": 0},
			{"target_rate": -5},
		):
			with self.subTest(**kwargs):
				self.assertRefused(**kwargs)

	def test_non_finite_qty_or_rate_is_refused(self):
		for kwargs in (
			{"qty": "nan"},
			{"qty": float("inf")},
			{"target_rate": "nan"},
			{"target_rate": "-inf"},
		):
			with self.subTest(**kwargs):
				self.assertRefused(**kwargs)

	def test_malformed_posting_datetime_is_refused(self):
		self.assertRefused(posting_datetime="not a date")

	def test_non_string_warehouse_is_refused_by_the_type_hints(self):
		# Whitelisted methods validate annotated arguments in requests and tests.
		self.assertRefused(exc=FrappeTypeError, warehouse={"company": COMPANY})

	def test_warehouse_the_user_cannot_see_is_refused(self):
		with patch.object(repack.frappe, "get_list", return_value=[]) as get_list:
			self.assertIsNone(repack._get_warehouse(WAREHOUSE))
		get_list.assert_called_once_with(
			"Warehouse", filters={"name": WAREHOUSE}, pluck="name", limit=1
		)

	def test_item_without_batch_tracking_is_refused(self):
		self.assertRefused({"item": _item(has_batch_no=0)})

	def test_missing_item_is_refused(self):
		self.assertRefused({"item": None})

	def test_missing_company_account_is_refused(self):
		for fieldname in repack.COMPANY_ACCOUNT_FIELDS:
			with self.subTest(missing=fieldname):
				accounts = dict(_accounts())
				accounts[fieldname] = None
				self.assertRefused({"company_accounts": accounts})

	def test_repack_type_not_granted_raises_permission_error(self):
		self.assertRefused({"type_allowed": False}, exc=frappe.PermissionError)

	def test_missing_doctype_permission_raises_permission_error(self):
		for pair in repack.REQUIRED_PERMISSIONS:
			with self.subTest(denied=pair):
				self.assertRefused({"denied": [pair]}, exc=frappe.PermissionError)

	def test_site_without_the_journal_entry_link_field_is_refused(self):
		"""Without it a cancelled Repack would leave its Journal Entry posted."""
		self.assertRefused({"link_ready": False})

	def test_mandatory_branch_without_a_value_is_refused(self):
		self.assertRefused(
			{"dimension": _dimension(mandatory=True), "default_branch": None}
		)


class TestRepackPermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_batch_is_checked_for_create_and_write(self):
		"""has_permission(Batch, submit) is always False; asking for it would block everyone.

		Write is needed because the bundle hooks save the new batch during submit."""
		with _repack_env() as recorder:
			self.assertEqual(repack._missing_permissions(), [])

		self.assertIn(("Batch", "create"), recorder.permission_calls)
		self.assertIn(("Batch", "write"), recorder.permission_calls)
		self.assertNotIn(("Batch", "submit"), recorder.permission_calls)
		for doctype in ("Stock Entry", "Journal Entry"):
			for ptype in ("create", "submit"):
				self.assertIn((doctype, ptype), recorder.permission_calls)

	def test_batch_read_permission_is_checked_with_throw(self):
		with _repack_env(denied=[("Batch", "read")]) as recorder:
			with self.assertRaises(frappe.PermissionError):
				_create()
			with self.assertRaises(frappe.PermissionError):
				repack.get_batch_repack_context(BATCH)
		self.assertEqual(recorder.docs, [])


class TestRepackOrderOfOperations(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_draft_entry_then_referencing_batch_then_save_and_submit(self):
		with _repack_env() as recorder:
			result = _create()

		steps = [(doctype, action) for doctype, action, *_ in recorder.log]
		self.assertEqual(
			steps,
			[
				("Stock Entry", "insert"),
				("Batch", "insert"),
				("Stock Entry", "save"),
				("Stock Entry", "submit"),
			],
		)
		se = recorder.one("Stock Entry")
		batch = recorder.one("Batch")
		target = [row for row in se.items if row.t_warehouse][0]

		self.assertEqual(batch.item, ITEM)
		self.assertEqual(batch.reference_doctype, "Stock Entry")
		self.assertEqual(batch.reference_name, se.name)
		self.assertEqual(batch.custom_voucher_detail_no, target.name)
		self.assertEqual(batch.custom_company, COMPANY)

		# The target row carries the new batch before the entry is saved and submitted.
		batch_nos_at = {(d, a): nos for d, a, _args, _kw, nos in recorder.log}
		self.assertEqual(batch_nos_at[("Stock Entry", "insert")], [BATCH, None])
		self.assertEqual(batch_nos_at[("Stock Entry", "save")], [BATCH, batch.name])
		self.assertEqual(batch_nos_at[("Stock Entry", "submit")], [BATCH, batch.name])

		self.assertEqual(result, {"stock_entry": se.name, "journal_entry": None})

	def test_nothing_is_called_with_ignore_permissions(self):
		with _repack_env(net=10) as recorder:
			_create()

		self.assertTrue(recorder.log)
		for doctype, action, args, kwargs, _batch_nos in recorder.log:
			with self.subTest(doctype=doctype, action=action):
				self.assertEqual(args, ())
				self.assertEqual(kwargs, {})
		for doc in recorder.docs:
			self.assertNotIn("flags", doc.__dict__)

	def test_entry_header_and_rows(self):
		with _repack_env() as recorder:
			_create(qty=4, target_rate=250)

		se = recorder.one("Stock Entry")
		self.assertEqual(se.stock_entry_type, "Repack")
		self.assertEqual(se.purpose, "Repack")
		self.assertEqual(se.company, COMPANY)
		self.assertEqual(se.auto_created, 0)

		source, target = se.items
		accounts = _accounts()
		for row in (source, target):
			self.assertEqual(row.item_code, ITEM)
			self.assertEqual(row.qty, 4)
			self.assertEqual(row.transfer_qty, 4)
			self.assertEqual(row.uom, "Gram")
			self.assertEqual(row.stock_uom, "Gram")
			self.assertEqual(row.conversion_factor, 1)
			self.assertEqual(row.gst_hsn_code, "7108")
			self.assertEqual(row.use_serial_batch_fields, 1)
			self.assertEqual(row.expense_account, accounts.stock_adjustment_account)
			self.assertEqual(row.cost_center, accounts.cost_center)
			self.assertNotIn("pcs", row.__dict__)

		self.assertEqual(source.s_warehouse, WAREHOUSE)
		self.assertIsNone(source.t_warehouse)
		self.assertEqual(source.batch_no, BATCH)
		self.assertEqual(source.is_finished_item, 0)

		self.assertEqual(target.t_warehouse, WAREHOUSE)
		self.assertIsNone(target.s_warehouse)
		self.assertEqual(target.basic_rate, 250)
		self.assertEqual(target.set_basic_rate_manually, 1)
		self.assertEqual(target.is_finished_item, 1)

	def test_no_manufacturing_references_are_set(self):
		"""Without them doc_events.stock_entry.onsubmit skips reservations and the MOP Log."""
		with _repack_env() as recorder:
			_create()

		se = recorder.one("Stock Entry")
		for doc in (se, *se.items):
			for fieldname in (
				"manufacturing_order",
				"manufacturing_work_order",
				"manufacturing_operation",
			):
				self.assertNotIn(fieldname, doc.__dict__)

	def test_posting_datetime_sets_the_posting_time(self):
		with _repack_env() as recorder:
			_create(posting_datetime="2026-10-10 10:15:30")

		se = recorder.one("Stock Entry")
		self.assertEqual(se.set_posting_time, 1)
		self.assertEqual(se.posting_date, date(2026, 10, 10))
		self.assertEqual(se.posting_time, "10:15:30")

	def test_without_posting_datetime_the_entry_posts_now(self):
		with _repack_env() as recorder, patch.object(
			repack, "today", return_value="2026-10-12"
		), patch.object(repack, "nowtime", return_value="11:00:00"):
			_create(posting_datetime=None)

		se = recorder.one("Stock Entry")
		self.assertEqual(se.set_posting_time, 0)
		self.assertEqual(se.posting_date, "2026-10-12")
		self.assertEqual(se.posting_time, "11:00:00")

	def test_item_that_creates_its_own_batches_leaves_naming_to_the_hooks(self):
		with _repack_env(item=_item(create_new_batch=1)) as recorder:
			_create()

		self.assertNotIn("batch_id", recorder.one("Batch").__dict__)

	def test_item_without_create_new_batch_gets_an_explicit_batch_id(self):
		"""ERPNext's Batch.autoname raises "Batch ID is mandatory" otherwise."""
		with _repack_env(item=_item(create_new_batch=0)) as recorder:
			_create()
		self.assertEqual(recorder.one("Batch").batch_id, "HASHBATCH")

		with _repack_env(
			item=_item(create_new_batch=0, batch_number_series="RPK-.#####")
		) as recorder, patch.object(
			repack, "make_autoname", return_value="RPK-00001"
		) as make_autoname:
			_create()
		batch = recorder.one("Batch")
		self.assertEqual(batch.batch_id, "RPK-00001")
		make_autoname.assert_called_once_with("RPK-.#####", doc=batch)

	def test_explicit_batch_id_skips_ids_that_are_taken(self):
		taken = {"RPK-00001"}

		def exists(doctype, name=None, *args, **kwargs):
			return doctype == "Batch" and name in taken

		with _repack_env(
			item=_item(create_new_batch=0, batch_number_series="RPK-.#####")
		) as recorder, patch.object(
			repack, "make_autoname", side_effect=["RPK-00001", "RPK-00002"]
		), patch.object(repack.frappe.db, "exists", side_effect=exists):
			_create()
		self.assertEqual(recorder.one("Batch").batch_id, "RPK-00002")


class TestRepackJournalEntry(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _accounts_by_name(self, je):
		return {row.account: row for row in je.accounts}

	def test_positive_net_credits_stock_adjustment_and_debits_srbnb(self):
		with _repack_env(net=125.5) as recorder:
			result = _create()

		se = recorder.one("Stock Entry")
		je = recorder.one("Journal Entry")
		self.assertEqual(result, {"stock_entry": se.name, "journal_entry": je.name})
		# The link is what lets the Stock Entry cancel find and cancel this entry.
		self.assertEqual(je.get(repack.JE_LINK_FIELD), se.name)
		self.assertEqual(je.company, COMPANY)
		self.assertEqual(je.voucher_type, "Journal Entry")
		self.assertEqual(je.posting_date, se.posting_date)
		self.assertIn(se.name, je.user_remark)

		accounts = _accounts()
		rows = self._accounts_by_name(je)
		adjustment = rows[accounts.stock_adjustment_account]
		srbnb = rows[accounts.stock_received_but_not_billed]
		self.assertEqual((adjustment.debit, adjustment.credit), (0, 125.5))
		self.assertEqual((srbnb.debit, srbnb.credit), (125.5, 0))
		for row in (adjustment, srbnb):
			self.assertEqual(row.debit_in_account_currency, row.debit)
			self.assertEqual(row.credit_in_account_currency, row.credit)
			self.assertEqual(row.cost_center, accounts.cost_center)

		steps = [(doctype, action) for doctype, action, *_ in recorder.log]
		self.assertEqual(
			steps[-2:], [("Journal Entry", "insert"), ("Journal Entry", "submit")]
		)

	def test_negative_net_is_the_reverse(self):
		with _repack_env(net=-80) as recorder:
			_create()

		accounts = _accounts()
		rows = self._accounts_by_name(recorder.one("Journal Entry"))
		adjustment = rows[accounts.stock_adjustment_account]
		srbnb = rows[accounts.stock_received_but_not_billed]
		self.assertEqual((adjustment.debit, adjustment.credit), (80, 0))
		self.assertEqual((srbnb.debit, srbnb.credit), (0, 80))

	def test_net_that_rounds_to_zero_creates_no_journal_entry(self):
		for net in (0, 0.004, -0.004):
			with self.subTest(net=net):
				with _repack_env(net=net) as recorder:
					result = _create()
				self.assertEqual(recorder.of("Journal Entry"), [])
				self.assertIsNone(result["journal_entry"])

	def test_net_is_read_from_the_company_stock_adjustment_account(self):
		with _repack_env() as recorder:
			_create()
			repack._net_stock_adjustment.assert_called_once_with(
				recorder.one("Stock Entry").name, _accounts().stock_adjustment_account
			)


class TestRepackBranchDimension(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_branch_is_set_on_entry_rows_and_both_journal_rows(self):
		with _repack_env(
			dimension=_dimension(), net=50, fields=("branch",)
		) as recorder:
			_create(branch="BR-SELECTED")

		se = recorder.one("Stock Entry")
		je = recorder.one("Journal Entry")
		self.assertEqual(se.branch, "BR-SELECTED")
		self.assertEqual([row.branch for row in se.items], ["BR-SELECTED"] * 2)
		self.assertEqual([row.branch for row in je.accounts], ["BR-SELECTED"] * 2)

	def test_default_branch_is_used_when_none_is_passed(self):
		with _repack_env(
			dimension=_dimension(mandatory=True),
			default_branch="BR-INWARD",
			fields=("branch",),
		) as recorder:
			_create(branch=None)

		se = recorder.one("Stock Entry")
		self.assertEqual(se.branch, "BR-INWARD")
		self.assertEqual([row.branch for row in se.items], ["BR-INWARD"] * 2)

	def test_dimension_fieldname_comes_from_the_metadata(self):
		with _repack_env(
			dimension=_dimension(fieldname="custom_site_branch"),
			fields=("custom_site_branch",),
		) as recorder:
			_create(branch="BR-1")

		se = recorder.one("Stock Entry")
		self.assertEqual(se.custom_site_branch, "BR-1")
		self.assertNotIn("branch", se.__dict__)

	def test_documents_without_the_field_are_left_alone(self):
		with _repack_env(dimension=_dimension(), net=50, fields=()) as recorder:
			_create(branch="BR-1")

		for doc in recorder.docs:
			self.assertNotIn("branch", doc.__dict__)
			for row in doc.items + doc.accounts:
				self.assertNotIn("branch", row.__dict__)

	def test_no_dimension_on_the_site_sets_nothing(self):
		with _repack_env(dimension=None, net=50, fields=("branch",)) as recorder:
			_create(branch="BR-1")

		for doc in recorder.docs:
			self.assertNotIn("branch", doc.__dict__)

	def test_branch_dimension_is_read_from_accounting_dimension_records(self):
		with patch.object(
			repack.frappe.db,
			"get_value",
			return_value=frappe._dict(
				name="Branch", fieldname="custom_site_branch", label="Site Branch"
			),
		) as get_value, patch.object(
			repack.frappe,
			"get_all",
			return_value=[
				frappe._dict(
					mandatory_for_pl=0, mandatory_for_bs=1, default_dimension="BR-DEF"
				)
			],
		) as get_all:
			dimension = repack._branch_dimension(COMPANY)

		get_value.assert_called_once()
		self.assertEqual(
			get_value.call_args.args[1], {"document_type": "Branch", "disabled": 0}
		)
		self.assertEqual(get_all.call_args.kwargs["filters"]["company"], COMPANY)
		self.assertEqual(dimension.fieldname, "custom_site_branch")
		self.assertEqual(dimension.label, "Site Branch")
		self.assertTrue(dimension.mandatory)
		self.assertEqual(dimension.company_default, "BR-DEF")

	def test_dimension_not_mandatory_for_the_company(self):
		with patch.object(
			repack.frappe.db,
			"get_value",
			return_value=frappe._dict(
				name="Branch", fieldname="branch", label="Branch"
			),
		), patch.object(
			repack.frappe,
			"get_all",
			return_value=[
				frappe._dict(
					mandatory_for_pl=0, mandatory_for_bs=0, default_dimension=None
				)
			],
		):
			self.assertFalse(repack._branch_dimension(COMPANY).mandatory)

	def test_site_without_a_branch_dimension(self):
		with patch.object(repack.frappe.db, "get_value", return_value=None):
			self.assertIsNone(repack._branch_dimension(COMPANY))

	def test_inward_voucher_row_value_wins(self):
		child_meta = SimpleNamespace(has_field=lambda fieldname: True)
		parent_meta = SimpleNamespace(
			has_field=lambda fieldname: True,
			get_table_fields=lambda: [SimpleNamespace(options="Purchase Receipt Item")],
		)

		def get_meta(doctype):
			return parent_meta if doctype == "Purchase Receipt" else child_meta

		def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			if doctype == "Purchase Receipt Item":
				self.assertEqual(filters, {"name": "PRI-1", "parent": "PR-0001"})
				return "BR-ROW"
			return "BR-HEADER"

		with patch.object(
			repack.frappe, "get_meta", side_effect=get_meta
		), patch.object(repack.frappe.db, "get_value", side_effect=get_value):
			self.assertEqual(repack._inward_voucher_value(_batch(), "branch"), "BR-ROW")

	def test_inward_voucher_header_when_the_row_has_none(self):
		parent_meta = SimpleNamespace(
			has_field=lambda fieldname: True,
			get_table_fields=lambda: [SimpleNamespace(options="Purchase Receipt Item")],
		)
		child_meta = SimpleNamespace(has_field=lambda fieldname: False)

		with patch.object(
			repack.frappe,
			"get_meta",
			side_effect=lambda dt: parent_meta
			if dt == "Purchase Receipt"
			else child_meta,
		), patch.object(repack.frappe.db, "get_value", return_value="BR-HEADER"):
			self.assertEqual(
				repack._inward_voucher_value(_batch(), "branch"), "BR-HEADER"
			)

	def test_batch_without_a_reference_has_no_inward_value(self):
		batch = _batch(reference_doctype=None, reference_name=None)
		self.assertIsNone(repack._inward_voucher_value(batch, "branch"))

	def test_default_branch_falls_back_to_user_then_company_default(self):
		dimension = _dimension(company_default="BR-COMPANY")
		with patch.object(
			repack, "_inward_voucher_value", return_value=None
		), patch.object(
			repack.frappe.defaults, "get_user_default", return_value="BR-USER"
		) as user_default:
			self.assertEqual(repack._default_branch(_batch(), dimension), "BR-USER")
		user_default.assert_called_once_with("Branch")

		with patch.object(
			repack, "_inward_voucher_value", return_value=None
		), patch.object(repack.frappe.defaults, "get_user_default", return_value=None):
			self.assertEqual(repack._default_branch(_batch(), dimension), "BR-COMPANY")

		with patch.object(
			repack, "_inward_voucher_value", return_value="BR-INWARD"
		), patch.object(
			repack.frappe.defaults, "get_user_default", return_value="BR-USER"
		):
			self.assertEqual(repack._default_branch(_batch(), dimension), "BR-INWARD")


class TestRepackCompanyResolution(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_batch_company_wins(self):
		with patch.object(repack.frappe.db, "get_value") as get_value:
			self.assertEqual(repack._resolve_batch_company(_batch()), COMPANY)
		get_value.assert_not_called()

	def test_blank_company_falls_back_to_the_reference_voucher(self):
		batch = _batch(
			custom_company=None, reference_doctype="Stock Entry", reference_name="SE-IN"
		)
		with patch.object(
			repack.frappe,
			"get_meta",
			return_value=SimpleNamespace(
				has_field=lambda fieldname: fieldname == "company"
			),
		), patch.object(
			repack.frappe.db, "get_value", return_value=COMPANY
		) as get_value:
			self.assertEqual(repack._resolve_batch_company(batch), COMPANY)
		get_value.assert_called_once_with("Stock Entry", "SE-IN", "company")

	def test_reference_without_a_company_field_resolves_nothing(self):
		batch = _batch(custom_company=None)
		with patch.object(
			repack.frappe,
			"get_meta",
			return_value=SimpleNamespace(has_field=lambda f: False),
		), patch.object(repack.frappe.db, "get_value") as get_value:
			self.assertIsNone(repack._resolve_batch_company(batch))
		get_value.assert_not_called()

	def test_blank_company_resolved_through_the_voucher_is_accepted(self):
		batch = _batch(
			custom_company=None, reference_doctype="Stock Entry", reference_name="SE-IN"
		)
		with _repack_env(batch=batch) as recorder, patch.object(
			repack, "_resolve_batch_company", return_value=COMPANY
		):
			_create()
		self.assertEqual(recorder.one("Batch").custom_company, COMPANY)
		self.assertEqual(recorder.one("Stock Entry").company, COMPANY)


class TestBatchRepackContext(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	WAREHOUSES = [{"warehouse": WAREHOUSE, "qty": 7.5}]

	def _context(self, **env_kwargs):
		with _repack_env(**env_kwargs) as recorder, patch.object(
			repack, "_warehouse_balances", return_value=self.WAREHOUSES
		):
			ctx = repack.get_batch_repack_context(BATCH)
		self.assertEqual(recorder.docs, [], "the context call is read-only")
		return ctx

	def test_allowed_batch_returns_company_item_warehouses_and_branch(self):
		ctx = self._context(
			dimension=_dimension(mandatory=True), default_branch="BR-ROW"
		)
		self.assertEqual(
			ctx,
			{
				"allowed": True,
				"company": COMPANY,
				"item_code": ITEM,
				"warehouses": self.WAREHOUSES,
				"branch": {
					"fieldname": "branch",
					"label": "Branch",
					"document_type": "Branch",
					"default": "BR-ROW",
					"reqd": 1,
				},
			},
		)

	def test_no_branch_dimension_returns_no_branch(self):
		self.assertIsNone(self._context(dimension=None)["branch"])

	def test_refused_batches_are_not_allowed(self):
		for case, env_kwargs in {
			"other company": {"batch": _batch(custom_company=OTHER_COMPANY)},
			"disabled": {"batch": _batch(disabled=1)},
			"customer": {"batch": _batch(custom_customer="CUST-0001")},
			"customer goods": {"batch": _batch(custom_inventory_type="Customer Goods")},
			"type not granted": {"type_allowed": False},
			"no JE submit": {"denied": [("Journal Entry", "submit")]},
			"no batch create": {"denied": [("Batch", "create")]},
			"no batch write": {"denied": [("Batch", "write")]},
			"not batch-tracked": {"item": _item(has_batch_no=0)},
		}.items():
			with self.subTest(case):
				self.assertEqual(self._context(**env_kwargs), {"allowed": False})

	def test_warehouse_balances_keep_positive_company_leaf_warehouses(self):
		rows = [
			{"batch_no": BATCH, "warehouse": "WH-A - TC", "qty": 2},
			{"batch_no": BATCH, "warehouse": "WH-A - TC", "qty": 1.5},
			{"batch_no": BATCH, "warehouse": "WH-EMPTY - TC", "qty": 0},
			{"batch_no": BATCH, "warehouse": "WH-NEG - TC", "qty": -1},
			{"batch_no": BATCH, "warehouse": "WH-OTHER - XX", "qty": 4},
		]
		with patch.object(
			repack, "get_batch_qty", return_value=rows
		) as batch_qty, patch.object(
			repack, "_qty_precision", return_value=3
		), patch.object(
			repack.frappe, "get_list", return_value=["WH-A - TC"]
		) as get_list:
			balances = repack._warehouse_balances(BATCH, ITEM, COMPANY)

		batch_qty.assert_called_once_with(batch_no=BATCH, item_code=ITEM)
		# get_list, so the Warehouse permission rules apply to the candidates.
		self.assertEqual(get_list.call_args.args, ("Warehouse",))
		filters = get_list.call_args.kwargs["filters"]
		self.assertEqual(filters["company"], COMPANY)
		self.assertEqual(filters["is_group"], 0)
		self.assertEqual(sorted(filters["name"][1]), ["WH-A - TC", "WH-OTHER - XX"])
		self.assertEqual(balances, [{"warehouse": "WH-A - TC", "qty": 3.5}])

	def test_no_positive_balance_skips_the_warehouse_query(self):
		# Only Warehouse queries are recorded: on a cold cache flt() can load System
		# Settings meta through the list API, which must still reach the real one.
		real_get_list = frappe.get_list
		warehouse_queries = []

		def get_list(doctype, *args, **kwargs):
			if doctype == "Warehouse":
				warehouse_queries.append(kwargs)
				return []
			return real_get_list(doctype, *args, **kwargs)

		with patch.object(
			repack, "get_batch_qty", return_value=[{"warehouse": "WH-A - TC", "qty": 0}]
		), patch.object(repack, "_qty_precision", return_value=3), patch.object(
			repack.frappe, "get_list", side_effect=get_list
		):
			self.assertEqual(repack._warehouse_balances(BATCH, ITEM, COMPANY), [])
		self.assertEqual(warehouse_queries, [])


class TestRepackCancelCancelsJournalEntries(IntegrationTestCase):
	"""cancel_linked_journal_entries: the Stock Entry on_cancel hook."""

	@classmethod
	def setUpClass(cls):
		pass

	def _cancel(self, se, linked=(), ready=True, denied=(), journal_entries=None):
		if journal_entries is None:
			journal_entries = {name: MagicMock(name=name) for name in linked}
		with patch.object(
			repack, "_journal_entry_link_ready", return_value=ready
		), patch.object(
			repack.frappe, "get_all", return_value=list(linked)
		) as get_all, patch.object(
			repack.frappe,
			"has_permission",
			side_effect=lambda doctype, ptype, doc=None, **k: doc not in denied,
		), patch.object(
			repack.frappe,
			"get_doc",
			side_effect=lambda doctype, name: journal_entries[name],
		):
			repack.cancel_linked_journal_entries(se, "on_cancel")
		return get_all, journal_entries

	def test_other_entry_types_are_not_looked_at(self):
		for se_type in ("Material Transfer", "Manufacture", "Repack-Metal Conversion"):
			with self.subTest(se_type=se_type):
				se = frappe._dict(name="SE-1", stock_entry_type=se_type)
				get_all, _journal_entries = self._cancel(se, linked=["JE-1"])
				get_all.assert_not_called()

	def test_site_without_the_link_field_does_nothing(self):
		se = frappe._dict(name="SE-1", stock_entry_type="Repack")
		get_all, _journal_entries = self._cancel(se, linked=["JE-1"], ready=False)
		get_all.assert_not_called()

	def test_each_submitted_linked_journal_entry_is_cancelled(self):
		se = frappe._dict(name="SE-REPACK-0001", stock_entry_type="Repack")
		get_all, journal_entries = self._cancel(se, linked=["JE-1", "JE-2"])

		get_all.assert_called_once_with(
			"Journal Entry",
			filters={repack.JE_LINK_FIELD: "SE-REPACK-0001", "docstatus": 1},
			pluck="name",
			order_by="creation asc",
		)
		for name in ("JE-1", "JE-2"):
			journal_entries[name].cancel.assert_called_once_with()

	def test_no_linked_journal_entry_cancels_nothing(self):
		se = frappe._dict(name="SE-1", stock_entry_type="Repack")
		_get_all, journal_entries = self._cancel(se, linked=[])
		self.assertEqual(journal_entries, {})

	def test_missing_journal_entry_cancel_permission_refuses_the_cancel(self):
		se = frappe._dict(name="SE-1", stock_entry_type="Repack")
		journal_entries = {"JE-1": MagicMock(), "JE-2": MagicMock()}
		with self.assertRaisesRegex(frappe.PermissionError, "JE-1"):
			self._cancel(
				se,
				linked=["JE-1", "JE-2"],
				denied=("JE-1",),
				journal_entries=journal_entries,
			)
		for journal_entry in journal_entries.values():
			journal_entry.cancel.assert_not_called()


class TestRepackJournalEntryFieldPatch(IntegrationTestCase):
	"""add_journal_entry_repack_stock_entry_field: create only where missing, never update."""

	@classmethod
	def setUpClass(cls):
		pass

	def _ensure(self, custom_field=False, column=False, docfield=False):
		exists = {"Custom Field": custom_field, "DocField": docfield}
		with patch.object(
			je_link_patch.frappe.db,
			"exists",
			side_effect=lambda doctype, filters: exists[doctype],
		), patch.object(
			je_link_patch.frappe.db, "has_column", return_value=column
		), patch.object(je_link_patch, "create_custom_fields") as creator, patch.object(
			je_link_patch.frappe, "logger"
		) as logger, patch("builtins.print") as printed:
			created = je_link_patch.ensure_repack_stock_entry_field()
		return created, creator, logger.return_value.error, printed

	def test_field_definition(self):
		(df,) = je_link_patch.CUSTOM_FIELDS["Journal Entry"]
		self.assertEqual(df["fieldname"], repack.JE_LINK_FIELD)
		self.assertEqual((df["fieldtype"], df["options"]), ("Link", "Stock Entry"))
		self.assertEqual((df["read_only"], df["no_copy"]), (1, 1))
		self.assertEqual(df["search_index"], 1)

	def test_missing_field_is_created_with_update_false(self):
		created, creator, log_error, _printed = self._ensure()
		self.assertTrue(created)
		creator.assert_called_once_with(
			{"Journal Entry": je_link_patch.CUSTOM_FIELDS["Journal Entry"]},
			ignore_validate=True,
			update=False,
		)
		log_error.assert_not_called()

	def test_existing_custom_field_is_left_alone(self):
		created, creator, _log_error, _printed = self._ensure(
			custom_field=True, column=True
		)
		self.assertFalse(created)
		creator.assert_not_called()

	def test_standard_column_is_left_alone(self):
		created, creator, log_error, _printed = self._ensure(column=True, docfield=True)
		self.assertFalse(created)
		creator.assert_not_called()
		log_error.assert_not_called()

	def test_orphan_column_is_logged_and_nothing_is_created(self):
		created, creator, log_error, printed = self._ensure(column=True)
		self.assertFalse(created)
		creator.assert_not_called()
		message = printed.call_args.args[0]
		self.assertIn("left as is", message)
		log_error.assert_called_once_with(message)


class TestBatchRepackRegistration(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_batch_form_script_is_registered(self):
		doctype_js = frappe.get_hooks("doctype_js", app_name="jewellery_erpnext")
		self.assertIn("public/js/doctype_js/batch.js", doctype_js["Batch"])

	def test_module_is_hooked_only_into_stock_entry_cancel(self):
		"""Whitelisted methods aside, the only hook is the Journal Entry cancel."""
		handler = (
			"jewellery_erpnext.jewellery_erpnext.customization.batch.repack."
			"cancel_linked_journal_entries"
		)
		doc_events = frappe.get_hooks("doc_events", app_name="jewellery_erpnext")
		on_cancel = doc_events["Stock Entry"]["on_cancel"]
		# After the existing Stock Entry cancel handler, so that one runs first.
		self.assertEqual(on_cancel[-1], handler)
		self.assertEqual(
			on_cancel[0],
			"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.on_cancel",
		)
		hooks = repr(frappe.get_hooks(app_name="jewellery_erpnext"))
		self.assertEqual(hooks.count("customization.batch.repack"), 1)

	def test_create_is_post_only(self):
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func.get(
				repack.create_repack_stock_entry
			),
			["POST"],
		)


class TestBatchRepackPermissionSemantics(IntegrationTestCase):
	def test_batch_is_not_submittable(self):
		"""Why REQUIRED_PERMISSIONS asks Batch for create and write, never submit."""
		self.assertFalse(frappe.get_meta("Batch").is_submittable)
		self.assertNotIn(("Batch", "submit"), repack.REQUIRED_PERMISSIONS)


# ---------------------------------------------------------------------------
# DB-backed: the real flow on the CI seed
# ---------------------------------------------------------------------------

CI_COMPANY = "Test_Company"
CI_ITEM = "M-G-22KT-91.6-Y"
CI_WAREHOUSE = "Central RM - T"
CI_MANUFACTURER = "Shubh"
SAVEPOINT = "batch_repack_integration"

# Commit-ordered dependency: the manual-Repack posting-time rule lives in
# doc_events/stock_entry.py. Where it is present, the entered time of day is asserted too.
KEEPS_ENTERED_TIME = hasattr(stock_entry_events, "KEEP_ENTERED_POSTING_TIME_COMPANIES")


class TestBatchRepackIntegration(IntegrationTestCase):
	"""Real Stock Entry, Batch and ledger rows for the CI seed company.

	Skipped where the seed (create_test_data) is absent. Each test runs inside a savepoint
	that is rolled back; ``frappe.db.commit`` is stubbed so nothing escapes it, stock
	reposts are not executed for the back-dated entries, and the EOD sync lock reads as
	not running (a lock left committed by earlier suites would refuse every Stock Entry).
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		missing = [
			f"{doctype} {name}"
			for doctype, name in (
				("Company", CI_COMPANY),
				("Item", CI_ITEM),
				("Warehouse", CI_WAREHOUSE),
			)
			if not frappe.db.exists(doctype, name)
		]
		if missing:
			raise unittest.SkipTest(
				"needs the CI seed (create_test_data): " + ", ".join(missing)
			)

	def setUp(self):
		frappe.db.savepoint(SAVEPOINT)
		self.addCleanup(self._clear_caches)
		self.addCleanup(frappe.db.rollback, save_point=SAVEPOINT)

		stack = ExitStack()
		self.addCleanup(stack.close)
		stack.enter_context(patch.object(frappe.db, "commit", lambda *a, **k: None))
		# Earlier suites in the same CI job can leave an EOD sync lock committed on the
		# shared site, and the Stock Entry EOD guard then refuses every entry. These tests
		# are not about the EOD lock, so it reads as not running here.
		stack.enter_context(
			patch.object(eod_lock, "is_eod_sync_locked", return_value=False)
		)
		stack.enter_context(patch.object(repack, "REPACK_COMPANIES", (CI_COMPANY,)))
		if KEEPS_ENTERED_TIME:
			stack.enter_context(
				patch.object(
					stock_entry_events,
					"KEEP_ENTERED_POSTING_TIME_COMPANIES",
					(CI_COMPANY,),
				)
			)

		previous = frappe.flags.get("dont_execute_stock_reposts")
		frappe.flags.dont_execute_stock_reposts = True

		def restore_repost_flag():
			frappe.flags.dont_execute_stock_reposts = previous

		self.addCleanup(restore_repost_flag)

		self.accounts = self._ensure_company_accounts()
		self.dimension = repack._branch_dimension(CI_COMPANY)
		self.branch = self._branch_value()

	@staticmethod
	def _clear_caches():
		# Runs after the savepoint rollback, so nothing re-caches the test's values.
		frappe.clear_document_cache("Company", CI_COMPANY)
		frappe.clear_document_cache("Item", CI_ITEM)
		frappe.db.value_cache.clear()

	def _ensure_company_accounts(self):
		"""Fill any of the three Company accounts the seed leaves blank (rolled back)."""
		fields = list(repack.COMPANY_ACCOUNT_FIELDS)
		current = frappe.db.get_value("Company", CI_COMPANY, fields, as_dict=True)
		lookups = {
			"stock_adjustment_account": (
				"Account",
				{
					"company": CI_COMPANY,
					"account_type": "Stock Adjustment",
					"is_group": 0,
				},
			),
			"stock_received_but_not_billed": (
				"Account",
				{
					"company": CI_COMPANY,
					"account_type": "Stock Received But Not Billed",
					"is_group": 0,
				},
			),
			"cost_center": ("Cost Center", {"company": CI_COMPANY, "is_group": 0}),
		}
		updates = {}
		for fieldname in fields:
			if current.get(fieldname):
				continue
			value = frappe.db.get_value(*lookups[fieldname])
			if not value:
				self.skipTest(f"{CI_COMPANY} has no account for {fieldname}")
			updates[fieldname] = value
		if updates:
			frappe.db.set_value("Company", CI_COMPANY, updates, update_modified=False)
			frappe.clear_document_cache("Company", CI_COMPANY)
		return frappe.db.get_value("Company", CI_COMPANY, fields, as_dict=True)

	def _branch_value(self):
		if not self.dimension:
			return None
		branch = frappe.db.get_value(self.dimension.document_type, {}, "name")
		if not branch and self.dimension.mandatory:
			self.skipTest("the Branch dimension is mandatory and no Branch exists")
		return branch

	def _receive(self, qty=5, rate=100):
		"""A back-dated Material Receipt that mints a fresh batch, as the seed's receipts do."""
		receipt = frappe.new_doc("Stock Entry")
		receipt.company = CI_COMPANY
		receipt.stock_entry_type = "Material Receipt"
		receipt.set_posting_time = 1
		receipt.posting_date = add_days(today(), -2)
		receipt.posting_time = "09:00:00"
		receipt.to_warehouse = CI_WAREHOUSE
		receipt.manufacturer = CI_MANUFACTURER
		receipt.append("items", {"item_code": CI_ITEM, "qty": qty, "basic_rate": rate})
		if self.branch:
			repack._apply_dimension(
				[receipt, *receipt.items], self.dimension.fieldname, self.branch
			)
		receipt.insert()
		receipt.submit()

		row = frappe.db.get_value(
			"Stock Entry Detail",
			receipt.items[0].name,
			["batch_no", "serial_and_batch_bundle"],
			as_dict=True,
		)
		batch_no = row.batch_no or frappe.db.get_value(
			"Serial and Batch Entry",
			{"parent": row.serial_and_batch_bundle},
			"batch_no",
		)
		self.assertTrue(batch_no, "the receipt minted no batch")
		return batch_no

	def _balance(self, batch_no):
		return flt(
			get_batch_qty(batch_no=batch_no, warehouse=CI_WAREHOUSE, item_code=CI_ITEM)
		)

	def test_repack_posts_one_submitted_entry_into_a_new_batch(self):
		source_batch = self._receive(qty=5, rate=100)
		posted_on = add_days(today(), -1)

		context = repack.get_batch_repack_context(source_batch)
		self.assertTrue(context["allowed"])
		self.assertIn(
			{"warehouse": CI_WAREHOUSE, "qty": 5.0},
			[
				{"warehouse": w["warehouse"], "qty": flt(w["qty"])}
				for w in context["warehouses"]
			],
		)

		result = repack.create_repack_stock_entry(
			source_batch,
			CI_WAREHOUSE,
			2,
			250,
			f"{posted_on} 10:15:00",
			branch=self.branch,
		)

		se = frappe.get_doc("Stock Entry", result["stock_entry"])
		self.assertEqual(se.docstatus, 1)
		self.assertEqual(se.stock_entry_type, "Repack")
		self.assertEqual(se.purpose, "Repack")
		self.assertEqual(se.company, CI_COMPANY)
		self.assertEqual(se.set_posting_time, 1)
		self.assertEqual(getdate(se.posting_date), getdate(posted_on))
		if KEEPS_ENTERED_TIME:
			self.assertEqual(get_timedelta(se.posting_time), get_timedelta("10:15:00"))
		for fieldname in ("manufacturing_order", "manufacturing_work_order"):
			self.assertFalse(se.get(fieldname))

		sources = [row for row in se.items if row.s_warehouse]
		targets = [row for row in se.items if row.t_warehouse]
		self.assertEqual(len(sources), 1)
		self.assertEqual(len(targets), 1)
		source, target = sources[0], targets[0]
		self.assertEqual(source.batch_no, source_batch)
		self.assertTrue(target.batch_no)
		self.assertNotEqual(target.batch_no, source_batch)
		# The entered rate is kept, not re-derived from the consumed row.
		self.assertEqual(flt(target.basic_rate), 250)

		new_batch = frappe.get_doc("Batch", target.batch_no)
		self.assertEqual(new_batch.item, CI_ITEM)
		self.assertEqual(new_batch.reference_doctype, "Stock Entry")
		self.assertEqual(new_batch.reference_name, se.name)
		if new_batch.meta.has_field("custom_voucher_detail_no"):
			self.assertEqual(new_batch.custom_voucher_detail_no, target.name)
		if new_batch.meta.has_field("custom_company"):
			self.assertEqual(new_batch.custom_company, CI_COMPANY)
		if new_batch.meta.has_field("custom_metal_rate"):
			# Stamped by the Batch hooks from the draft produce row.
			self.assertIn(
				250,
				(
					flt(new_batch.get("custom_metal_rate")),
					flt(new_batch.get("custom_alloy_rate")),
				),
			)

		self.assertEqual(self._balance(source_batch), 3)
		self.assertEqual(self._balance(target.batch_no), 2)

		if self.branch:
			fieldname = self.dimension.fieldname
			for doc in (se, *se.items):
				if doc.meta.has_field(fieldname):
					self.assertEqual(doc.get(fieldname), self.branch)
			if result["journal_entry"]:
				je_rows = frappe.get_doc(
					"Journal Entry", result["journal_entry"]
				).accounts
				self.assertEqual(len(je_rows), 2)
				for row in je_rows:
					if row.meta.has_field(fieldname):
						self.assertEqual(row.get(fieldname), self.branch)

		net = repack._net_stock_adjustment(
			se.name, self.accounts.stock_adjustment_account
		)
		if not flt(net, 2):
			self.assertIsNone(result["journal_entry"])
			return

		je = frappe.get_doc("Journal Entry", result["journal_entry"])
		self.assertEqual(je.docstatus, 1)
		self.assertEqual(getdate(je.posting_date), getdate(se.posting_date))
		adjustment = [
			row
			for row in je.accounts
			if row.account == self.accounts.stock_adjustment_account
		]
		self.assertEqual(len(adjustment), 1)
		# The journal reverses the entry's net on Stock Adjustment.
		self.assertAlmostEqual(
			flt(adjustment[0].debit) - flt(adjustment[0].credit), -net, places=2
		)

	def test_refused_repack_posts_nothing(self):
		source_batch = self._receive(qty=5, rate=100)

		def counts():
			return (
				frappe.db.count(
					"Stock Entry", {"company": CI_COMPANY, "stock_entry_type": "Repack"}
				),
				frappe.db.count("Batch", {"item": CI_ITEM}),
				frappe.db.count("Journal Entry", {"company": CI_COMPANY}),
			)

		before = counts()
		# Pinned to the balance refusal, so no other ValidationError can satisfy it.
		with self.assertRaisesRegex(frappe.ValidationError, "is more than the"):
			repack.create_repack_stock_entry(
				source_batch, CI_WAREHOUSE, 6, 250, branch=self.branch
			)
		self.assertEqual(counts(), before)
		self.assertEqual(self._balance(source_batch), 5)

	def test_item_without_create_new_batch_gets_an_explicit_batch_id(self):
		source_batch = self._receive(qty=5, rate=100)
		frappe.db.set_value(
			"Item", CI_ITEM, "create_new_batch", 0, update_modified=False
		)
		frappe.clear_document_cache("Item", CI_ITEM)
		frappe.db.value_cache.clear()

		result = repack.create_repack_stock_entry(
			source_batch, CI_WAREHOUSE, 1, 250, branch=self.branch
		)

		se = frappe.get_doc("Stock Entry", result["stock_entry"])
		target = [row for row in se.items if row.t_warehouse][0]
		self.assertEqual(se.docstatus, 1)
		self.assertTrue(frappe.db.get_value("Batch", target.batch_no, "batch_id"))
		self.assertEqual(self._balance(target.batch_no), 1)

	def _repack_with_journal_entry(self):
		"""A submitted Repack entry and its submitted, linked Journal Entry."""
		if not frappe.db.has_column("Journal Entry", repack.JE_LINK_FIELD):
			self.fail(
				f"Journal Entry.{repack.JE_LINK_FIELD} is not provisioned; "
				"see create_test_data"
			)
		source_batch = self._receive(qty=5, rate=100)
		result = repack.create_repack_stock_entry(
			source_batch, CI_WAREHOUSE, 2, 250, branch=self.branch
		)
		se = frappe.get_doc("Stock Entry", result["stock_entry"])
		if result["journal_entry"]:
			return se, result["journal_entry"]

		# The CI seed company has no perpetual inventory, so the entry posts no GL and
		# the tool makes no Journal Entry. Post a balanced one linked the way it links.
		je = frappe.new_doc("Journal Entry")
		je.company = CI_COMPANY
		je.posting_date = se.posting_date
		je.user_remark = f"Batch Repack cancel test for {se.name}"
		je.set(repack.JE_LINK_FIELD, se.name)
		for account, debit, credit in (
			(self.accounts.stock_adjustment_account, 0, 10),
			(self.accounts.stock_received_but_not_billed, 10, 0),
		):
			je.append(
				"accounts",
				{
					"account": account,
					"cost_center": self.accounts.cost_center,
					"debit_in_account_currency": debit,
					"credit_in_account_currency": credit,
				},
			)
		if self.branch:
			repack._apply_dimension(je.accounts, self.dimension.fieldname, self.branch)
		je.insert()
		je.submit()
		return se, je.name

	def test_cancelling_the_repack_entry_cancels_its_journal_entry(self):
		se, je_name = self._repack_with_journal_entry()
		self.assertEqual(
			frappe.db.get_value("Journal Entry", je_name, repack.JE_LINK_FIELD), se.name
		)

		se.reload()
		se.cancel()

		self.assertEqual(frappe.db.get_value("Stock Entry", se.name, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Journal Entry", je_name, "docstatus"), 2)

	def test_a_journal_entry_cancelled_first_does_not_block_the_entry_cancel(self):
		se, je_name = self._repack_with_journal_entry()
		frappe.get_doc("Journal Entry", je_name).cancel()

		se.reload()
		se.cancel()

		self.assertEqual(frappe.db.get_value("Stock Entry", se.name, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Journal Entry", je_name, "docstatus"), 2)
