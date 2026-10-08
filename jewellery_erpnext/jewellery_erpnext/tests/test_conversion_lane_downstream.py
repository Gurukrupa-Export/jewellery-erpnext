# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the two voucher-scoped mechanisms a mixed-ownership Stock Entry breaks.

Pure-logic tests: no DB / no site (``setUpClass`` neutralised per the suite's
convention), every DB call patched.

A Metal Conversion can now emit ONE Stock Entry carrying several ownership lanes.
Two pieces of machinery previously assumed one ownership per voucher:

1. ``batch_rename.create_child_batches`` stamped every batch-less produce row as
   "Customer Goods" owned by the SE **header**'s ``_customer`` -- so the Regular
   lane's target batch would be minted as the customer's. It also derived the
   parent batch from the voucher's *first* source row and bailed on
   ``len(name.split("-")) < 4``; since Regular autonames have three hyphen segments
   and customer names four or more, whether anything was mislabelled depended on
   which lane FIFO happened to return first.
2. ``serial_and_batch_bundle.update_parent_batch_id`` copied **all** the voucher's
   outward entries into **every** inward batch's ``custom_origin_entries``, which is
   the sole input to the qty-weighted Batch Rate blend -- giving both target batches
   one identical cross-lane average rate, and leaking provenance across customers.

Both are now lane-scoped, and must stay byte-identical for single-ownership
vouchers -- which is every pre-existing caller.

Since a customer batch became a lane of its own (MCON00332), two more readers follow
the builder's lane tag (``Stock Entry Detail.custom_conversion_lane``). The classes
after ``TestUpdateParentBatchIdLaneScoping`` pin them:

3. ``create_child_batches`` keys parents by the tag on a tagged voucher: each customer
   batch's target is minted from THAT batch, never from the company alloy consumed
   under the same tag, and a tagged voucher never takes the single-lane path. Untagged
   vouchers keep their ``(inventory_type, customer)`` grouping.
4. ``subcontracting_report.get_linked_batches`` follows a tagged row's own lane only.

Their fakes answer only what they own and hand every other call to the real function.
"""

import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting import batch_rename
from jewellery_erpnext.customer_subcontracting.report.subcontracting_report import (
	subcontracting_report,
)
from jewellery_erpnext.jewellery_erpnext.customization.serial_and_batch_bundle.doc_events import (
	utils as sbb_utils,
)

_SBB_PATH = (
	"jewellery_erpnext.jewellery_erpnext.customization.serial_and_batch_bundle."
	"doc_events.utils"
)

# A customer-goods batch named by batch_rename: customer-yearmonth-item-serial.
CUSTOMER_PARENT = "TNCU0001-2F06-M-G-24KT-99.9-Y-01"
# A Regular Stock batch named by Batch.autoname: exactly three hyphen segments.
REGULAR_PARENT = "GE2F063-MGL22919Y0-O5H44"


def _row(**fields):
	defaults = {
		"name": fields.get("name", "row-x"),
		"item_code": "M-G-18KT-75.0-Y",
		# A metal row. create_child_batches withholds a Batch Rate only from a Manufacture's
		# finished piece (batch_rename._source_row_rate), so this is descriptive, not required.
		"custom_variant_of": "M",
		"s_warehouse": None,
		"t_warehouse": None,
		"batch_no": None,
		"inventory_type": "Regular Stock",
		"customer": None,
		"basic_rate": 100.0,
		"custom_metal_rate": 0.0,
	}
	defaults.update(fields)
	row = SimpleNamespace(**defaults)
	row.get = lambda k, default=None: getattr(row, k, default)
	return row


class _FakeSE:
	def __init__(self, items, customer=None):
		self.doctype = "Stock Entry"
		self.name = "MAT-STE-99999"
		self.items = items
		self._customer = customer


class _FakeBatch:
	"""Captures what create_child_batches would insert."""

	def __init__(self):
		self.batch_id = None
		self.item = None
		self.custom_customer = None
		self.custom_inventory_type = None
		self.custom_metal_rate = 0.0
		self.custom_voucher_detail_no = None
		self.reference_doctype = None
		self.reference_name = None
		self.custom_customer_voucher_type = None

	def insert(self, ignore_permissions=False):
		return self


class TestCreateChildBatches(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		if not hasattr(frappe, "db") or not frappe.db:
			frappe.db = MagicMock()

	def _run(self, doc):
		"""Run create_child_batches with all DB access stubbed; return minted batches."""
		minted = []

		def _new_doc(doctype):
			batch = _FakeBatch()
			minted.append(batch)
			return batch

		with (
			patch("frappe.new_doc", side_effect=_new_doc),
			patch("frappe.db.sql", return_value=[]),
			patch("frappe.db.exists", return_value=False),
		):
			batch_rename.create_child_batches(doc)

		return minted

	def test_noop_without_any_customer(self):
		doc = _FakeSE(
			[
				_row(name="s1", s_warehouse="W", batch_no=REGULAR_PARENT),
				_row(name="t1", t_warehouse="W"),
			]
		)
		self.assertEqual(self._run(doc), [])
		self.assertIsNone(doc.items[1].batch_no)

	def test_single_lane_voucher_unchanged(self):
		"""The pre-existing shape: one ownership, one parent, customer child batch.

		Customer Goods Received and Subcontracting Repack build this, as SNC's
		create_repack_metal_conversion did until it tagged a customer's conversion with
		the owner's lane -- so this path must not start declining.
		"""
		doc = _FakeSE(
			[
				_row(
					name="s1",
					s_warehouse="W",
					batch_no=CUSTOMER_PARENT,
					inventory_type="Customer Goods",
					customer="TNCU0001",
				),
				_row(
					name="t1",
					t_warehouse="W",
					inventory_type="Customer Goods",
					customer="TNCU0001",
				),
			],
			customer="TNCU0001",
		)
		minted = self._run(doc)

		self.assertEqual(len(minted), 1)
		self.assertEqual(minted[0].batch_id, "TNCU0001-2F06-M-G-18KT-75.0-Y-01-A")
		self.assertEqual(minted[0].custom_customer, "TNCU0001")
		self.assertEqual(minted[0].custom_inventory_type, "Customer Goods")
		self.assertEqual(doc.items[1].batch_no, "TNCU0001-2F06-M-G-18KT-75.0-Y-01-A")

	def test_mixed_voucher_mints_only_the_customer_lane(self):
		"""The Regular lane must be left for the Serial-and-Batch path.

		That path is the only one that stamps ownership from the row itself and runs
		the Customer-Goods item guard, so leaving batch_no empty is deliberate.
		"""
		doc = _FakeSE(
			[
				_row(name="s1", s_warehouse="W", batch_no=REGULAR_PARENT),
				_row(name="t1", t_warehouse="W"),
				_row(
					name="s2",
					s_warehouse="W",
					batch_no=CUSTOMER_PARENT,
					inventory_type="Customer Goods",
					customer="TNCU0001",
				),
				_row(
					name="t2",
					t_warehouse="W",
					inventory_type="Customer Goods",
					customer="TNCU0001",
				),
			],
			customer="TNCU0001",
		)
		minted = self._run(doc)

		self.assertEqual(len(minted), 1)
		self.assertEqual(minted[0].custom_customer, "TNCU0001")
		# Regular lane target left alone...
		self.assertIsNone(doc.items[1].batch_no)
		# ...customer lane target named from its OWN lane's parent.
		self.assertEqual(doc.items[3].batch_no, "TNCU0001-2F06-M-G-18KT-75.0-Y-01-A")

	def test_outcome_is_independent_of_fifo_order(self):
		"""The old code's mislabelling depended on which lane came first."""
		customer_first = _FakeSE(
			[
				_row(
					name="s2",
					s_warehouse="W",
					batch_no=CUSTOMER_PARENT,
					inventory_type="Customer Goods",
					customer="TNCU0001",
				),
				_row(
					name="t2",
					t_warehouse="W",
					inventory_type="Customer Goods",
					customer="TNCU0001",
				),
				_row(name="s1", s_warehouse="W", batch_no=REGULAR_PARENT),
				_row(name="t1", t_warehouse="W"),
			],
			customer="TNCU0001",
		)
		minted = self._run(customer_first)

		self.assertEqual(len(minted), 1)
		self.assertEqual(minted[0].custom_customer, "TNCU0001")
		# The Regular lane's target is STILL not minted as the customer's.
		self.assertIsNone(customer_first.items[3].batch_no)

	def test_two_customers_each_get_their_own_parent(self):
		doc = _FakeSE(
			[
				_row(
					name="s1",
					s_warehouse="W",
					batch_no="CUSTA-2F06-M-G-24KT-99.9-Y-01",
					inventory_type="Customer Goods",
					customer="CUSTA",
				),
				_row(
					name="t1",
					t_warehouse="W",
					inventory_type="Customer Goods",
					customer="CUSTA",
				),
				_row(
					name="s2",
					s_warehouse="W",
					batch_no="CUSTB-2F07-M-G-24KT-99.9-Y-07",
					inventory_type="Customer Goods",
					customer="CUSTB",
				),
				_row(
					name="t2",
					t_warehouse="W",
					inventory_type="Customer Goods",
					customer="CUSTB",
				),
			],
			customer="CUSTA",
		)
		minted = self._run(doc)

		self.assertEqual(len(minted), 2)
		self.assertEqual([b.custom_customer for b in minted], ["CUSTA", "CUSTB"])
		# Each name takes the year-month and serial of its OWN lane's parent.
		self.assertEqual(doc.items[1].batch_no, "CUSTA-2F06-M-G-18KT-75.0-Y-01-A")
		self.assertEqual(doc.items[3].batch_no, "CUSTB-2F07-M-G-18KT-75.0-Y-07-A")

	def test_short_parent_name_skips_only_its_own_lane(self):
		"""A Customer Goods batch not named by this module has no serial to extend.

		Historically that aborted child-batch minting for the whole voucher.
		"""
		doc = _FakeSE(
			[
				# Customer Goods created by a customer Purchase Receipt: 3 segments.
				_row(
					name="s1",
					s_warehouse="W",
					batch_no=REGULAR_PARENT,
					inventory_type="Customer Goods",
					customer="CUSTA",
				),
				_row(
					name="t1",
					t_warehouse="W",
					inventory_type="Customer Goods",
					customer="CUSTA",
				),
				_row(
					name="s2",
					s_warehouse="W",
					batch_no="CUSTB-2F07-M-G-24KT-99.9-Y-07",
					inventory_type="Customer Goods",
					customer="CUSTB",
				),
				_row(
					name="t2",
					t_warehouse="W",
					inventory_type="Customer Goods",
					customer="CUSTB",
				),
			],
			customer="CUSTA",
		)
		minted = self._run(doc)

		self.assertEqual(len(minted), 1)
		self.assertEqual(minted[0].custom_customer, "CUSTB")
		self.assertIsNone(doc.items[1].batch_no)
		self.assertEqual(doc.items[3].batch_no, "CUSTB-2F07-M-G-18KT-75.0-Y-07-A")

	def test_row_customer_alone_opens_the_gate(self):
		"""A mixed conversion has no single owning customer on the header."""
		doc = _FakeSE(
			[
				_row(
					name="s1",
					s_warehouse="W",
					batch_no=CUSTOMER_PARENT,
					inventory_type="Customer Goods",
					customer="TNCU0001",
				),
				_row(
					name="t1",
					t_warehouse="W",
					inventory_type="Customer Goods",
					customer="TNCU0001",
				),
			],
			customer=None,
		)
		minted = self._run(doc)
		self.assertEqual(len(minted), 1)
		self.assertEqual(minted[0].custom_customer, "TNCU0001")


class _FakeBundle:
	def __init__(self, entries, voucher_detail_no, voucher_type="Stock Entry"):
		self.type_of_transaction = "Inward"
		self.voucher_type = voucher_type
		self.voucher_no = "MAT-STE-99999"
		self.entries = entries
		self.voucher_detail_no = voucher_detail_no

	def get(self, key, default=None):
		return getattr(self, key, default)


class _CapturingBatch:
	def __init__(self, name):
		self.name = name
		self.custom_origin_entries = []
		self.flags = frappe._dict()
		self.saved = False

	def append(self, table, row):
		self.custom_origin_entries.append(frappe._dict(row))

	def save(self):
		self.saved = True


class TestUpdateParentBatchIdLaneScoping(IntegrationTestCase):
	"""Each target batch's origin entries must come from its OWN lane only."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		if not hasattr(frappe, "db") or not frappe.db:
			frappe.db = MagicMock()

	# Outward Serial and Batch Entries of a two-lane metal conversion, plus the alloy
	# row that funds the customer lane.
	OUTWARD = [
		frappe._dict(
			batch_no="REG-SRC", qty=-8.0, incoming_rate=100.0, voucher_detail_no="s1"
		),
		frappe._dict(
			batch_no="CG-SRC", qty=-12.0, incoming_rate=200.0, voucher_detail_no="s2"
		),
		frappe._dict(
			batch_no="ALLOY", qty=-4.0, incoming_rate=10.0, voucher_detail_no="a2"
		),
	]
	LANES = {
		"s1": "Regular Stock|",
		"t1": "Regular Stock|",
		"s2": "Customer Goods|TNCU0001",
		"t2": "Customer Goods|TNCU0001",
		"a2": "Customer Goods|TNCU0001",
	}

	def _run(self, bundle, lane_rows, has_column=True):
		"""Drive update_parent_batch_id; return the captured target Batch docs."""
		captured = {}

		def _get_doc(doctype, name):
			batch = _CapturingBatch(name)
			captured[name] = batch
			return batch

		def _get_all(doctype, filters=None, fields=None, **kwargs):
			# DISPATCH ON THE FILTER SHAPE, not on the doctype alone.
			#
			# Two different callers now query Stock Entry Detail with different filters:
			#   * _conversion_lane_map      -> {"name": ["in", [...]]}
			#   * _lane_output_share (C09)  -> {"parent": ..., "custom_conversion_lane": ...}
			# A stub keyed only on the doctype answered the first and raised KeyError('name') on
			# the second -- and because the caller swallows exceptions, that surfaced as a
			# confusing secondary failure inside frappe.log_error rather than as anything about
			# lane scoping. Same lesson as the popup/validator parity fixture: a double must
			# dispatch on what is actually asked.
			filters = filters or {}
			if doctype == "Stock Entry Detail" and "name" in filters:
				wanted = set(filters["name"][1])
				return [
					frappe._dict(name=n, custom_conversion_lane=lane_rows.get(n))
					for n in wanted
				]
			if doctype == "Stock Entry Detail" and "custom_conversion_lane" in filters:
				# The lane's produced rows. This class is about lane SCOPING of origin entries,
				# not about C09 apportionment, so a single produced row is the right fixture:
				# it makes the share exactly 1.0 and leaves the assertions below unchanged.
				return [
					frappe._dict(name=n, qty=10.0, t_warehouse="TGT-WH")
					for n, tag in lane_rows.items()
					if tag == filters["custom_conversion_lane"] and n.startswith("t")
				]
			return []

		def _db_get_all(doctype, filters=None, fields=None, **kwargs):
			if doctype == "Serial and Batch Bundle":
				return ["OUT-BUNDLE"]
			if doctype == "Serial and Batch Entry":
				return self.OUTWARD
			return []

		with (
			patch("frappe.get_doc", side_effect=_get_doc),
			patch("frappe.db.get_all", side_effect=_db_get_all),
			patch("frappe.get_all", side_effect=_get_all),
			patch("frappe.db.has_column", return_value=has_column),
			patch(
				"frappe.db.get_value",
				return_value=("Repack", "Repack-Metal Conversion"),
			),
			# Pin out the C09 component writer. This class is about ONE thing: that each
			# target batch's origin entries come from its own lane. record_batch_components
			# is a separate concern that update_parent_batch_id now also calls, and it has its
			# own suites (tests/test_customer_gold_components.py and TestBatchComponents in the
			# integration suite).
			#
			# Pinned rather than accommodated, because the wholesale frappe.db.get_value patch
			# above returns a fixed TUPLE for every call -- so anything new that reads a
			# document through it gets a tuple back. Letting the writer run here would test the
			# mock, not the code; and because the hook swallows its exceptions, a genuine
			# failure would surface as a confusing secondary error inside frappe.log_error
			# rather than as anything about lane scoping. This is the narrow dependency
			# boundary C03 prescribes.
			patch.object(sbb_utils, "record_batch_components"),
		):
			sbb_utils.update_parent_batch_id(bundle)

		return captured

	def test_regular_lane_target_gets_only_regular_sources(self):
		bundle = _FakeBundle([frappe._dict(batch_no="REG-TGT")], voucher_detail_no="t1")
		captured = self._run(bundle, self.LANES)

		origins = {e.batch_no for e in captured["REG-TGT"].custom_origin_entries}
		self.assertEqual(origins, {"REG-SRC"})

	def test_customer_lane_target_gets_its_source_and_its_alloy(self):
		bundle = _FakeBundle([frappe._dict(batch_no="CG-TGT")], voucher_detail_no="t2")
		captured = self._run(bundle, self.LANES)

		origins = {e.batch_no for e in captured["CG-TGT"].custom_origin_entries}
		# The alloy row is booked "Regular Stock" yet funds this lane -- it is
		# attributable only because the builder tagged it.
		self.assertEqual(origins, {"CG-SRC", "ALLOY"})
		self.assertNotIn("REG-SRC", origins)

	def test_untagged_voucher_keeps_voucher_wide_behaviour(self):
		"""Every non-conversion flow must be completely unaffected."""
		bundle = _FakeBundle([frappe._dict(batch_no="TGT")], voucher_detail_no="t1")
		captured = self._run(bundle, lane_rows={})

		origins = {e.batch_no for e in captured["TGT"].custom_origin_entries}
		self.assertEqual(origins, {"REG-SRC", "CG-SRC", "ALLOY"})

	def test_missing_column_falls_back_to_voucher_wide(self):
		"""A site where the patch has not run yet must not lose origin entries."""
		bundle = _FakeBundle([frappe._dict(batch_no="TGT")], voucher_detail_no="t1")
		captured = self._run(bundle, self.LANES, has_column=False)

		origins = {e.batch_no for e in captured["TGT"].custom_origin_entries}
		self.assertEqual(origins, {"REG-SRC", "CG-SRC", "ALLOY"})

	def test_partially_tagged_voucher_falls_back(self):
		"""If the produced row itself is untagged, scoping would silently drop rows."""
		bundle = _FakeBundle([frappe._dict(batch_no="TGT")], voucher_detail_no="t9")
		captured = self._run(bundle, self.LANES)

		origins = {e.batch_no for e in captured["TGT"].custom_origin_entries}
		self.assertEqual(origins, {"REG-SRC", "CG-SRC", "ALLOY"})

	def test_batch_appearing_twice_is_not_double_counted(self):
		"""The dedupe snapshot was taken once before the loop, so duplicates slipped in.

		A double-counted origin row skews the qty-weighted Batch Rate blend.
		"""
		bundle = _FakeBundle([frappe._dict(batch_no="TGT")], voucher_detail_no="t1")

		duplicated = [
			frappe._dict(
				batch_no="REG-SRC",
				qty=-4.0,
				incoming_rate=100.0,
				voucher_detail_no="s1",
			),
			frappe._dict(
				batch_no="REG-SRC",
				qty=-4.0,
				incoming_rate=100.0,
				voucher_detail_no="s1",
			),
		]
		with patch.object(self, "OUTWARD", duplicated):
			captured = self._run(bundle, self.LANES)

		entries = captured["TGT"].custom_origin_entries
		self.assertEqual(len(entries), 1)
		self.assertEqual(entries[0].batch_no, "REG-SRC")

	def test_skips_non_repack_purposes(self):
		bundle = _FakeBundle([frappe._dict(batch_no="TGT")], voucher_detail_no="t1")
		with (
			patch("frappe.db.get_value", return_value=("Material Transfer", "MT")),
			patch("frappe.get_doc") as get_doc,
		):
			sbb_utils.update_parent_batch_id(bundle)
		get_doc.assert_not_called()


# ---------------------------------------------------------------------------------------
# Lane-tagged vouchers: what create_child_batches and get_linked_batches do with the tag
# ---------------------------------------------------------------------------------------

#: The real functions, saved at import -- before any test patches them. The fakes below
#: answer only what they own (the ``Batch`` doctype and its child-name lookup, ``Batch
#: MultiSelect``, the report's repack-children query) and hand every other call to the
#: real one. A fake that answered everything would also answer the System Settings read
#: behind ``flt(x, precision)``, which swallows the error and returns 0 -- but only while
#: the cache is cold, so a suite built that way passes or fails by run order.
_REAL_NEW_DOC = frappe.new_doc
_REAL_GET_ALL = frappe.get_all

#: Marks a row built with no ``custom_conversion_lane`` attribute at all.
_ABSENT = object()

WH = "Casting RM - GK"
CUST1 = "CUST1"
SOURCE_ITEM = "M-G-24KT-99.9-Y"
TARGET_ITEM = "M-G-22KT-91.75-Y"
ALLOY_ITEM = "M-Genia-221"

#: MCON00332's two batches of ONE customer, named the way create_parent_batches names
#: them: customer - year code and month - item - serial.
BATCH_11 = "CUST1-2F09-M-G-24KT-99.9-Y-11"
BATCH_12 = "CUST1-2F09-M-G-24KT-99.9-Y-12"
#: Company alloy, booked Regular Stock. FOUR hyphen segments on purpose: it passes the
#: ``len(parts) < 4`` guard, so were it ever taken as a customer lane's parent the child
#: would be named after it ("CUST1-2D082-...-04-<letter>") instead of being skipped.
ALLOY_BATCH = "AL-2D082-X-04"

#: The lane tags ``metal_conversions.lane_tag`` writes, spelled out by hand.
TAG_11 = "Customer Goods|CUST1|CUST1-2F09-M-G-24KT-99.9-Y-11"
TAG_12 = "Customer Goods|CUST1|CUST1-2F09-M-G-24KT-99.9-Y-12"
TAG_REGULAR = "Regular Stock|"


def _src(name, batch_no, customer=CUST1, **fields):
	"""A consumed customer-batch row."""
	return _row(
		name=name,
		item_code=SOURCE_ITEM,
		s_warehouse=WH,
		batch_no=batch_no,
		inventory_type="Customer Goods",
		customer=customer,
		**fields,
	)


def _alloy(name, **fields):
	"""A consumed company-alloy row: Regular Stock even while it funds a customer lane."""
	return _row(
		name=name, item_code=ALLOY_ITEM, s_warehouse=WH, batch_no=ALLOY_BATCH, **fields
	)


def _tgt(name, customer=CUST1, **fields):
	"""A produced customer row: batch-less until create_child_batches names it."""
	return _row(
		name=name,
		item_code=TARGET_ITEM,
		t_warehouse=WH,
		inventory_type="Customer Goods",
		customer=customer,
		**fields,
	)


def _reg_src(name, **fields):
	"""A consumed Regular Stock row (a three-segment autonamed batch)."""
	return _row(
		name=name,
		item_code=SOURCE_ITEM,
		s_warehouse=WH,
		batch_no=REGULAR_PARENT,
		**fields,
	)


def _reg_tgt(name, **fields):
	"""A produced Regular Stock row."""
	return _row(name=name, item_code=TARGET_ITEM, t_warehouse=WH, **fields)


def _voucher(items, customer=CUST1, name="MAT-STE-99999"):
	"""A Repack-Metal Conversion Stock Entry; ``customer`` is its header ``_customer``."""
	doc = _FakeSE(items, customer=customer)
	doc.name = name
	doc.purpose = "Repack"
	doc.stock_entry_type = "Repack-Metal Conversion"
	return doc


def _mat_ste_19453(alloy_first=False):
	"""MAT-STE-19453 as the fixed builder now emits it for MCON00332.

	Three lanes in FIFO order -- batch 11, the Regular Stock pool, batch 12 -- each one
	run of its source, the company alloy it consumes and its target, every row tagged
	with its lane. The header ``_customer`` is the first customer lane's
	(``_conversion_header``).

	``alloy_first`` puts each lane's alloy row ahead of its source. The builder never
	emits that order, which is exactly why it is the probe: in builder order the source
	row comes first and would hide an alloy row wrongly eligible as the lane's parent.
	"""
	lanes = (
		(
			_src("src-11", BATCH_11, custom_conversion_lane=TAG_11),
			_alloy("alloy-11", custom_conversion_lane=TAG_11),
			_tgt("tgt-11", custom_conversion_lane=TAG_11, basic_rate=7164.83),
		),
		(
			_reg_src("src-reg", custom_conversion_lane=TAG_REGULAR),
			_alloy("alloy-reg", custom_conversion_lane=TAG_REGULAR),
			_reg_tgt("tgt-reg", custom_conversion_lane=TAG_REGULAR),
		),
		(
			_src("src-12", BATCH_12, custom_conversion_lane=TAG_12),
			_alloy("alloy-12", custom_conversion_lane=TAG_12),
			_tgt("tgt-12", custom_conversion_lane=TAG_12, basic_rate=6925.0),
		),
	)
	items = []
	for source, alloy, target in lanes:
		items += [alloy, source, target] if alloy_first else [source, alloy, target]
	return _voucher(items, name="MAT-STE-19453")


def _by_name(doc):
	return {row.name: row for row in doc.items}


class _MintedBatch:
	"""The Batch create_child_batches fills in; ``insert`` books its name in the table."""

	def __init__(self, table):
		self._table = table
		self.batch_id = None
		self.item = None
		self.custom_customer = None
		self.custom_inventory_type = None
		self.custom_metal_rate = 0.0
		self.custom_voucher_detail_no = None
		self.reference_doctype = None
		self.reference_name = None
		self.custom_customer_voucher_type = None

	def __repr__(self):
		return (
			f"<Batch {self.batch_id} for row {self.custom_voucher_detail_no}: "
			f"{self.custom_inventory_type} of {self.custom_customer}>"
		)

	def insert(self, ignore_permissions=False):
		# A real insert would raise DuplicateEntryError; say what happened instead.
		if self.batch_id in self._table:
			raise AssertionError(f"Batch {self.batch_id} would be inserted twice")
		self._table.add(self.batch_id)
		return self


class _BatchTableCase(IntegrationTestCase):
	"""create_child_batches against an in-memory ``tabBatch``.

	TestCreateChildBatches answers every lookup with "nothing exists", so each child
	there is "-A". Here the child-name lookups read a set that every insert adds to --
	the view a real submit has inside its own transaction -- so two outputs of one
	parent get two letters, and an output minted from the WRONG parent shows up as the
	wrong base rather than hiding behind a collision. Only the ``Batch`` doctype and the
	child-name LIKE query are answered; every other call goes to the real function.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _mint(self, doc, existing=()):
		"""Run create_child_batches on ``doc``; return the Batch docs it inserted, in order."""
		table = set(existing)
		minted = []
		# frappe.db proxies the connection of the moment (frappe.local.db), so its real
		# methods are taken per call rather than pinned at import like frappe.new_doc.
		real_sql = frappe.db.sql
		real_exists = frappe.db.exists

		def _new_doc(doctype, *args, **kwargs):
			if doctype != "Batch":
				return _REAL_NEW_DOC(doctype, *args, **kwargs)
			batch = _MintedBatch(table)
			minted.append(batch)
			return batch

		def _sql(query, values=(), *args, **kwargs):
			if "FROM `tabBatch` WHERE name LIKE %s" not in " ".join(query.split()):
				return real_sql(query, values, *args, **kwargs)
			(pattern,) = values
			prefix = pattern[:-1]
			# A prefix match is exactly LIKE only while "%" trails and nothing else is a
			# wildcard: fail loudly rather than answer a lookup this fake cannot model.
			self.assertTrue(pattern.endswith("-%"), pattern)
			self.assertFalse({"%", "_"} & set(prefix), pattern)
			names = sorted((n for n in table if n.startswith(prefix)), reverse=True)
			# frappe.db.sql(..., pluck=True) returns the names themselves.
			if kwargs.get("pluck"):
				return names
			return [frappe._dict(name=n) for n in names]

		def _exists(doctype, name=None, *args, **kwargs):
			if doctype != "Batch":
				return real_exists(doctype, name, *args, **kwargs)
			return name if name in table else None

		with (
			patch("frappe.new_doc", side_effect=_new_doc),
			patch("frappe.db.sql", side_effect=_sql),
			patch("frappe.db.exists", side_effect=_exists),
		):
			batch_rename.create_child_batches(doc)

		return minted

	def _assert_child_of(self, batch_no, base):
		"""``batch_no`` is ``base`` plus ONE suffix letter -- which letter is not pinned."""
		self.assertRegex(batch_no or "", rf"^{re.escape(base)}-[A-Z]$")


class TestTaggedVoucherMintsEachCustomerBatch(_BatchTableCase):
	"""MCON00332: each customer batch's target is minted from THAT batch.

	On kg-gk, MAT-STE-19453 booked one customer target for batches 11 and 12 of one
	customer, named after 11 ("...-11-B"). The builder now emits a lane per customer
	batch; these tests take its voucher through create_child_batches, which keys parents
	by the lane tag. Every expected base is written out by hand -- customer, the parent's
	year-month segment, the TARGET item, the parent's serial -- and is followed by one
	suffix letter whose value is not pinned, only that names stay unique.
	"""

	def test_each_customer_target_is_minted_from_its_own_source_batch(self):
		"""T01/T03/T57, T38 (names). Detects R2 (parents keyed by customer without the
		batch: batch 12's target named "...-11-<letter>", the MAT-STE-19453 symptom) and
		R3 (the first batch's serial copied onto every customer output)."""
		doc = _mat_ste_19453()
		minted = self._mint(doc)
		rows = _by_name(doc)

		self._assert_child_of(rows["tgt-11"].batch_no, "CUST1-2F09-M-G-22KT-91.75-Y-11")
		self._assert_child_of(rows["tgt-12"].batch_no, "CUST1-2F09-M-G-22KT-91.75-Y-12")
		# One Batch per customer target, each booked against its own row.
		self.assertEqual(
			[(batch.custom_voucher_detail_no, batch.batch_id) for batch in minted],
			[("tgt-11", rows["tgt-11"].batch_no), ("tgt-12", rows["tgt-12"].batch_no)],
		)
		self.assertNotEqual(minted[0].batch_id, minted[1].batch_id)
		for batch in minted:
			with self.subTest(batch=batch.batch_id):
				self.assertEqual(batch.custom_customer, "CUST1")
				self.assertEqual(batch.custom_inventory_type, "Customer Goods")
				self.assertEqual(batch.item, "M-G-22KT-91.75-Y")
				self.assertEqual(batch.reference_name, "MAT-STE-19453")

	def test_the_regular_target_is_left_for_the_serial_and_batch_path(self):
		"""T04/T57. Detects R3 (a customer lane's ownership copied onto the Regular pool's
		output): only the Serial-and-Batch path stamps a Regular row's own ownership, so
		its batch_no must stay empty here. The consumed rows keep their batches."""
		doc = _mat_ste_19453()
		minted = self._mint(doc)
		rows = _by_name(doc)

		self.assertIsNone(rows["tgt-reg"].batch_no)
		self.assertNotIn(
			"tgt-reg", [batch.custom_voucher_detail_no for batch in minted]
		)
		consumed = ("src-11", "alloy-11", "src-reg", "alloy-reg", "src-12", "alloy-12")
		self.assertEqual(
			[rows[name].batch_no for name in consumed],
			[BATCH_11, ALLOY_BATCH, REGULAR_PARENT, ALLOY_BATCH, BATCH_12, ALLOY_BATCH],
		)

	def test_the_company_alloy_row_never_parents_a_customer_batch(self):
		"""T05/T57: the same voucher with each lane's alloy row ahead of its source.
		Detects R3 (the lane's first consumed row -- company alloy "AL-2D082-X-04" --
		lending its year-month and serial to the customer's batch, which would then read
		"CUST1-2D082-M-G-22KT-91.75-Y-04-<letter>")."""
		doc = _mat_ste_19453(alloy_first=True)
		minted = self._mint(doc)
		rows = _by_name(doc)

		self._assert_child_of(rows["tgt-11"].batch_no, "CUST1-2F09-M-G-22KT-91.75-Y-11")
		self._assert_child_of(rows["tgt-12"].batch_no, "CUST1-2F09-M-G-22KT-91.75-Y-12")
		self.assertIsNone(rows["tgt-reg"].batch_no)
		self.assertEqual(
			[batch.custom_voucher_detail_no for batch in minted], ["tgt-11", "tgt-12"]
		)
		self.assertEqual(
			[b.batch_id for b in minted if b.batch_id.startswith("CUST1-2D082-")], []
		)

	def test_names_stay_unique_beside_earlier_children_of_the_same_batch(self):
		"""T38 (unique names, correct lineage). Batch 11 already has two children from an
		earlier conversion: the new one must not reuse them, and batch 12's output must
		not continue batch 11's sequence. Detects R2 (batch 12's output named
		"...-11-<letter>" after 11's existing children)."""
		earlier = {
			"CUST1-2F09-M-G-22KT-91.75-Y-11-A",
			"CUST1-2F09-M-G-22KT-91.75-Y-11-B",
		}
		doc = _mat_ste_19453()
		minted = self._mint(doc, existing=earlier)
		rows = _by_name(doc)

		self._assert_child_of(rows["tgt-11"].batch_no, "CUST1-2F09-M-G-22KT-91.75-Y-11")
		self._assert_child_of(rows["tgt-12"].batch_no, "CUST1-2F09-M-G-22KT-91.75-Y-12")
		names = {batch.batch_id for batch in minted}
		self.assertEqual(len(names), 2)
		self.assertEqual(names & earlier, set())

	def test_each_customer_lane_is_minted_for_its_own_customer(self):
		"""T02/T04: two customers and the Regular pool in one voucher. The header's
		``_customer`` names only the first customer lane (``_conversion_header``). Detects
		R3 (the header's customer, or the first lane's parent, stamped on every customer
		output)."""
		cust2_batch = "CUST2-2F07-M-G-24KT-99.9-Y-07"
		cust2_tag = "Customer Goods|CUST2|CUST2-2F07-M-G-24KT-99.9-Y-07"
		doc = _voucher(
			[
				_src("src-11", BATCH_11, custom_conversion_lane=TAG_11),
				_tgt("tgt-11", custom_conversion_lane=TAG_11),
				_reg_src("src-reg", custom_conversion_lane=TAG_REGULAR),
				_reg_tgt("tgt-reg", custom_conversion_lane=TAG_REGULAR),
				_src(
					"src-07",
					cust2_batch,
					customer="CUST2",
					custom_conversion_lane=cust2_tag,
				),
				_tgt("tgt-07", customer="CUST2", custom_conversion_lane=cust2_tag),
			]
		)
		minted = self._mint(doc)
		rows = _by_name(doc)

		self._assert_child_of(rows["tgt-11"].batch_no, "CUST1-2F09-M-G-22KT-91.75-Y-11")
		self._assert_child_of(rows["tgt-07"].batch_no, "CUST2-2F07-M-G-22KT-91.75-Y-07")
		self.assertIsNone(rows["tgt-reg"].batch_no)
		self.assertEqual(
			[
				(batch.custom_voucher_detail_no, batch.custom_customer)
				for batch in minted
			],
			[("tgt-11", "CUST1"), ("tgt-07", "CUST2")],
		)

	def test_a_lane_whose_parent_has_no_serial_is_skipped_alone(self):
		"""T12 (an unusable batch). Customer Goods received on a Purchase Receipt has three
		hyphen segments and no serial to extend: that lane's output is left for the
		Serial-and-Batch path while batch 12's lane is minted as usual. Detects R3 (a lane
		with no usable parent borrowing another lane's)."""
		pr_batch = "CG2F084-MG24KT999Y0-K3P11"
		pr_tag = "Customer Goods|CUST1|CG2F084-MG24KT999Y0-K3P11"
		doc = _voucher(
			[
				_src("src-pr", pr_batch, custom_conversion_lane=pr_tag),
				_tgt("tgt-pr", custom_conversion_lane=pr_tag),
				_src("src-12", BATCH_12, custom_conversion_lane=TAG_12),
				_tgt("tgt-12", custom_conversion_lane=TAG_12),
			]
		)
		minted = self._mint(doc)
		rows = _by_name(doc)

		self.assertIsNone(rows["tgt-pr"].batch_no)
		self._assert_child_of(rows["tgt-12"].batch_no, "CUST1-2F09-M-G-22KT-91.75-Y-12")
		self.assertEqual(
			[batch.custom_voucher_detail_no for batch in minted], ["tgt-12"]
		)

	def test_each_minted_batch_takes_its_own_rows_rate(self):
		"""T27 (distinct outgoing rates; no first-row rate). Detects R5 (one combined rate
		stamped on every output): each lane is priced on its own, and each target batch
		takes the rate on its own row."""
		doc = _mat_ste_19453()
		minted = self._mint(doc)

		self.assertEqual(
			[
				(batch.custom_voucher_detail_no, batch.custom_metal_rate)
				for batch in minted
			],
			[("tgt-11", 7164.83), ("tgt-12", 6925.0)],
		)

	def test_a_second_pass_mints_nothing(self):
		"""T35/T36 (a repeated or retried submit). Detects R6 (duplicates on retry): every
		output the first pass named keeps its batch, and no Batch is inserted again."""
		doc = _mat_ste_19453()
		first = self._mint(doc)
		self.assertEqual(len(first), 2)
		named = [(row.name, row.batch_no) for row in doc.items]

		second = self._mint(doc, existing={batch.batch_id for batch in first})

		self.assertEqual(second, [])
		self.assertEqual([(row.name, row.batch_no) for row in doc.items], named)


class TestTaggedOneLaneVoucherCarveOut(_BatchTableCase):
	"""C09 on a one-lane conversion: the company's share of released alloy stays company's.

	Raising a customer batch from 22KT to 24KT releases alloy, and C09 splits the release
	by the batch's RECORDED components: the customer's share is booked to the customer,
	the company's share back as Regular Stock -- both tagged with the lane they came out
	of. With a single parent such a voucher used to take create_child_batches'
	single-lane path, which mints EVERY batch-less output from that parent for the
	header's customer, so the company's alloy was minted as the customer's Customer Goods.
	"""

	SOURCE = "CUST1-2F09-M-G-22KT-91.75-Y-05"
	TAG = "Customer Goods|CUST1|CUST1-2F09-M-G-22KT-91.75-Y-05"

	def _carve_out_voucher(self):
		owned = {"inventory_type": "Customer Goods", "customer": CUST1}
		return _voucher(
			[
				_row(
					name="src",
					item_code="M-G-22KT-91.75-Y",
					s_warehouse=WH,
					batch_no=self.SOURCE,
					custom_conversion_lane=self.TAG,
					**owned,
				),
				_row(
					name="tgt",
					item_code="M-G-24KT-99.9-Y",
					t_warehouse=WH,
					custom_conversion_lane=self.TAG,
					**owned,
				),
				_row(
					name="alloy-customer",
					item_code=ALLOY_ITEM,
					t_warehouse=WH,
					custom_conversion_lane=self.TAG,
					**owned,
				),
				# The C09 carve-out: the same lane, the company's ownership.
				_row(
					name="alloy-company",
					item_code=ALLOY_ITEM,
					t_warehouse=WH,
					inventory_type="Regular Stock",
					customer=None,
					custom_conversion_lane=self.TAG,
				),
			]
		)

	def test_the_company_share_is_not_minted_as_customer_goods(self):
		"""T18/T30 (C09). Detects R3 (the lane's -- and header's -- customer ownership
		copied onto every output of the voucher, the company's alloy included)."""
		doc = self._carve_out_voucher()
		minted = self._mint(doc)

		self.assertIsNone(_by_name(doc)["alloy-company"].batch_no)
		self.assertNotIn(
			"alloy-company", [batch.custom_voucher_detail_no for batch in minted]
		)

	def test_the_customer_outputs_are_minted_from_the_lane_source(self):
		"""T18/T01 (C09). The per-row path must still mint both customer outputs -- the
		target and the customer's share of the alloy -- from the lane's own source batch.
		Regression guarded: the fix over-reaching into the customer's own outputs (not
		one of R1-R6)."""
		doc = self._carve_out_voucher()
		minted = self._mint(doc)
		rows = _by_name(doc)

		self._assert_child_of(rows["tgt"].batch_no, "CUST1-2F09-M-G-24KT-99.9-Y-05")
		self._assert_child_of(
			rows["alloy-customer"].batch_no, "CUST1-2F09-M-Genia-221-05"
		)
		self.assertEqual(
			[
				(b.custom_voucher_detail_no, b.custom_customer, b.custom_inventory_type)
				for b in minted
			],
			[
				("tgt", "CUST1", "Customer Goods"),
				("alloy-customer", "CUST1", "Customer Goods"),
			],
		)


class TestUntaggedVouchersKeepTheirGrouping(_BatchTableCase):
	"""Every voucher without lane tags keeps exactly today's minting.

	Customer Goods Received, Subcontracting Repack and SNC's company-metal leg never tag
	rows, and neither did any voucher submitted before the column existed. The lane-tag
	rule is scoped to vouchers a conversion builder tagged; these pin that it stays so.
	"""

	def test_a_single_ownership_voucher_mints_every_output_from_its_one_parent(self):
		"""T46/T54 (unchanged baseline; old documents). Settle's ``_convert_multi`` shape:
		two batches of ONE customer consumed untagged -- one ownership, one parent (the
		first batch), and every batch-less output minted from it with the row's customer,
		else the header's. The untyped output is the probe: only the single-lane path
		mints it. A column read back as NULL or "" is untagged too. Regression guarded:
		the lane rule leaking into untagged vouchers (a compatibility pin, not R1-R6)."""
		for label, lane in (("absent", _ABSENT), ("NULL", None), ("empty", "")):
			with self.subTest(custom_conversion_lane=label):
				tag = {} if lane is _ABSENT else {"custom_conversion_lane": lane}
				doc = _voucher(
					[
						_src("src-11", BATCH_11, **tag),
						_src("src-12", BATCH_12, **tag),
						_tgt("tgt", **tag),
						# An output with no ownership of its own: the header decides.
						_row(
							name="untyped",
							item_code=ALLOY_ITEM,
							t_warehouse=WH,
							inventory_type=None,
							**tag,
						),
					]
				)
				minted = self._mint(doc)
				rows = _by_name(doc)

				self._assert_child_of(
					rows["tgt"].batch_no, "CUST1-2F09-M-G-22KT-91.75-Y-11"
				)
				self._assert_child_of(
					rows["untyped"].batch_no, "CUST1-2F09-M-Genia-221-11"
				)
				self.assertEqual(
					[
						(
							b.custom_voucher_detail_no,
							b.custom_customer,
							b.custom_inventory_type,
						)
						for b in minted
					],
					[
						("tgt", "CUST1", "Customer Goods"),
						("untyped", "CUST1", "Customer Goods"),
					],
				)

	def test_a_mixed_voucher_keys_parents_by_inventory_type_and_customer(self):
		"""T12/T46/T54. An untagged voucher spanning the Regular pool and two customers
		keeps its ``(inventory_type, customer)`` grouping: each customer's outputs are
		minted from that customer's FIRST batch -- batch 12 is no parent of its own here
		-- and the Regular output is left alone. Detects R1 (parents keyed by inventory
		type alone: CUST2's output named after CUST1's batch 11) and R3 (the Regular
		output minted as the header customer's)."""
		doc = _voucher(
			[
				# The Regular pool's first consumed row is the four-segment company alloy,
				# so the name guard cannot hide it: only the ownership check leaves the
				# Regular output alone.
				_alloy("alloy"),
				_reg_src("src-reg"),
				_src("src-11", BATCH_11),
				_src("src-07", "CUST2-2F07-M-G-24KT-99.9-Y-07", customer="CUST2"),
				_src("src-12", BATCH_12),
				_reg_tgt("tgt-reg"),
				_tgt("tgt-a"),
				_tgt("tgt-cust2", customer="CUST2"),
				_tgt("tgt-b"),
			]
		)
		minted = self._mint(doc)
		rows = _by_name(doc)

		self.assertIsNone(rows["tgt-reg"].batch_no)
		self._assert_child_of(rows["tgt-a"].batch_no, "CUST1-2F09-M-G-22KT-91.75-Y-11")
		self._assert_child_of(
			rows["tgt-cust2"].batch_no, "CUST2-2F07-M-G-22KT-91.75-Y-07"
		)
		self._assert_child_of(rows["tgt-b"].batch_no, "CUST1-2F09-M-G-22KT-91.75-Y-11")
		self.assertNotEqual(rows["tgt-a"].batch_no, rows["tgt-b"].batch_no)
		self.assertEqual(
			[
				(batch.custom_voucher_detail_no, batch.custom_customer)
				for batch in minted
			],
			[("tgt-a", "CUST1"), ("tgt-cust2", "CUST2"), ("tgt-b", "CUST1")],
		)


#: The lane clause the report adds, written out by hand. Compared with every whitespace
#: character removed, so re-indenting the SQL does not break these tests while any change
#: to the condition -- or to the parentheses that keep its OR inside the JOIN -- does.
LANE_CLAUSE = (
	"AND ( IFNULL(parent_sed.custom_conversion_lane, '') = '' "
	"OR child_sed.custom_conversion_lane = parent_sed.custom_conversion_lane )"
)


def _squash(sql):
	return "".join(sql.split())


class _LinkedBatchesCase(IntegrationTestCase):
	"""Drives subcontracting_report.get_linked_batches with its three reads faked.

	``Batch MultiSelect`` (get_all), the lane-column probe (has_column) and the
	repack-children query (sql) are answered here; every other call goes to the real
	function.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _linked(self, batch_no, has_column, answer, multiselect=()):
		"""Return ``(sorted result, (query, values, args, kwargs), has_column probes)``.

		``answer(query, values)`` supplies the rows of the repack-children query.
		"""
		queries = []
		probes = []
		real_sql = frappe.db.sql
		real_has_column = frappe.db.has_column

		def _get_all(doctype, *args, **kwargs):
			if doctype != "Batch MultiSelect":
				return _REAL_GET_ALL(doctype, *args, **kwargs)
			self.assertEqual(kwargs.get("filters"), {"parent": batch_no})
			return [frappe._dict(batch_no=name) for name in multiselect]

		def _has_column(doctype, column):
			probes.append((doctype, column))
			if (doctype, column) != ("Stock Entry Detail", "custom_conversion_lane"):
				return real_has_column(doctype, column)
			return has_column

		def _sql(query, values=(), *args, **kwargs):
			if "child_sed.batch_no" not in query:
				return real_sql(query, values, *args, **kwargs)
			queries.append((query, values, args, kwargs))
			return answer(query, values)

		with (
			patch("frappe.get_all", side_effect=_get_all),
			patch("frappe.db.has_column", side_effect=_has_column),
			patch("frappe.db.sql", side_effect=_sql),
		):
			result = subcontracting_report.get_linked_batches(batch_no)

		self.assertEqual(len(queries), 1)
		return sorted(result), queries[0], probes


class TestLinkedBatchesLaneClause(_LinkedBatchesCase):
	"""The repack-children query get_linked_batches sends, with and without the column."""

	CHILD_11 = "CUST1-2F09-M-G-22KT-91.75-Y-11-A"

	def _children(self, query, values):
		return [frappe._dict(batch_no=self.CHILD_11), frappe._dict(batch_no=None)]

	def test_the_lane_clause_is_added_when_the_column_exists(self):
		"""T53/T60. Detects R1/R2 at the report: without the clause a consumed batch's
		children are every output of its voucher -- the Regular pool's and another
		customer batch's included."""
		_, (query, values, args, kwargs), probes = self._linked(
			BATCH_11, True, self._children
		)

		self.assertEqual(probes, [("Stock Entry Detail", "custom_conversion_lane")])
		self.assertIn(_squash(LANE_CLAUSE), _squash(query))
		self.assertEqual((values, args, kwargs), ((BATCH_11,), (), {"as_dict": True}))

	def test_no_lane_clause_without_the_column(self):
		"""T54/T60: a site the patch has not reached must not be sent the column at all --
		MariaDB would fail the whole report with "Unknown column"."""
		_, (query, values, args, kwargs), _ = self._linked(
			BATCH_11, False, self._children
		)

		self.assertNotIn("custom_conversion_lane", query)
		self.assertEqual((values, args, kwargs), ((BATCH_11,), (), {"as_dict": True}))

	def test_the_lane_clause_is_the_only_difference(self):
		"""T53/T60: the scope is purely additive -- the same parameters and placeholder,
		and the query less the clause is exactly what a site without the column gets."""
		_, (with_lane, *call_with), _ = self._linked(BATCH_11, True, self._children)
		_, (without, *call_without), _ = self._linked(BATCH_11, False, self._children)

		self.assertEqual(call_with, call_without)
		self.assertEqual((with_lane.count("%s"), without.count("%s")), (1, 1))
		self.assertEqual(
			_squash(with_lane).replace(_squash(LANE_CLAUSE), "", 1), _squash(without)
		)

	def test_the_result_is_the_batch_with_its_linked_and_repack_children(self):
		"""T53: the batch itself, its Batch MultiSelect links and the query's children,
		blanks dropped -- the same with or without the column."""
		for has_column in (True, False):
			with self.subTest(has_column=has_column):
				result, _, _ = self._linked(
					BATCH_11,
					has_column,
					self._children,
					multiselect=("CUST1-2F08-M-G-24KT-99.9-Y-09", None),
				)
				self.assertEqual(
					result,
					[
						"CUST1-2F08-M-G-24KT-99.9-Y-09",
						"CUST1-2F09-M-G-22KT-91.75-Y-11-A",
						"CUST1-2F09-M-G-24KT-99.9-Y-11",
					],
				)


class TestLinkedBatchesFollowTheLane(_LinkedBatchesCase):
	"""T53/T61: what the lane clause MEANS, on MAT-STE-19453's rows.

	The class above pins the text; this runs the query get_linked_batches actually sends
	against an in-memory SQLite copy of the voucher's rows (plus one untagged repack).
	Nothing reaches the site database: sqlite3 lives in this process and each connection
	is closed after its test. SQLite and MariaDB agree on everything this query uses --
	inner joins, IFNULL, and NULL = x being unknown; MariaDB's case-insensitive collation
	is moot because every tag compared here matches byte for byte.
	"""

	CHILD_11 = "CUST1-2F09-M-G-22KT-91.75-Y-11-A"
	CHILD_12 = "CUST1-2F09-M-G-22KT-91.75-Y-12-A"
	# The Regular output, named by the Serial-and-Batch autoname (three segments).
	REGULAR_CHILD = "GE2F093-MGL22919Y0-R7K21"

	STOCK_ENTRIES = (
		("MAT-STE-19453", "Repack-Metal Conversion", 1),
		# Untagged: its column reads NULL on one row and "" on the others.
		("MAT-STE-20001", "Subcontracting Repack", 1),
	)
	DETAIL_ROWS = (
		# parent, batch_no, is_finished_item, custom_conversion_lane
		("MAT-STE-19453", BATCH_11, 0, TAG_11),
		("MAT-STE-19453", ALLOY_BATCH, 0, TAG_11),
		("MAT-STE-19453", CHILD_11, 1, TAG_11),
		("MAT-STE-19453", REGULAR_PARENT, 0, TAG_REGULAR),
		("MAT-STE-19453", ALLOY_BATCH, 0, TAG_REGULAR),
		("MAT-STE-19453", REGULAR_CHILD, 1, TAG_REGULAR),
		("MAT-STE-19453", BATCH_12, 0, TAG_12),
		("MAT-STE-19453", ALLOY_BATCH, 0, TAG_12),
		("MAT-STE-19453", CHILD_12, 1, TAG_12),
		("MAT-STE-20001", "CUST2-2F08-M-G-24KT-99.9-Y-03", 0, None),
		("MAT-STE-20001", "CUST2-2F08-M-G-24KT-99.9-Y-04", 0, ""),
		("MAT-STE-20001", "CUST9-2F08-M-G-24KT-99.9-Y-01", 1, ""),
	)

	def setUp(self):
		super().setUp()
		self.conn = sqlite3.connect(":memory:")
		self.addCleanup(self.conn.close)
		self.conn.execute(
			"CREATE TABLE `tabStock Entry` "
			"(name TEXT, stock_entry_type TEXT, docstatus INTEGER)"
		)
		self.conn.execute(
			"CREATE TABLE `tabStock Entry Detail` (parent TEXT, batch_no TEXT, "
			"is_finished_item INTEGER, custom_conversion_lane TEXT)"
		)
		self.conn.executemany(
			"INSERT INTO `tabStock Entry` VALUES (?, ?, ?)", self.STOCK_ENTRIES
		)
		self.conn.executemany(
			"INSERT INTO `tabStock Entry Detail` VALUES (?, ?, ?, ?)", self.DETAIL_ROWS
		)

	def _execute(self, query, values):
		"""The report's query as sent, with MariaDB's %s placeholder in SQLite's spelling."""
		rows = self.conn.execute(query.replace("%s", "?"), values).fetchall()
		return [frappe._dict(batch_no=batch_no) for (batch_no,) in rows]

	def test_a_customer_batch_links_only_to_its_own_lanes_output(self):
		"""T03/T53/T57/T61. Detects R2 (batch 11 linked to batch 12's output: one
		customer's two receipts merged again, this time in the report)."""
		for batch_no, linked in (
			(BATCH_11, [BATCH_11, "CUST1-2F09-M-G-22KT-91.75-Y-11-A"]),
			(BATCH_12, [BATCH_12, "CUST1-2F09-M-G-22KT-91.75-Y-12-A"]),
		):
			with self.subTest(batch_no=batch_no):
				result, _, _ = self._linked(batch_no, True, self._execute)
				self.assertEqual(result, sorted(linked))

	def test_the_regular_pool_links_only_to_the_regular_output(self):
		"""T04/T53/T61. Detects R1 (the company's Regular source linked to the customers'
		outputs: a grouping blind to ownership)."""
		result, _, _ = self._linked(REGULAR_PARENT, True, self._execute)

		self.assertEqual(result, sorted([REGULAR_PARENT, "GE2F093-MGL22919Y0-R7K21"]))

	def test_an_untagged_repack_keeps_the_voucher_wide_match(self):
		"""T54/T61: rows written before the column existed (NULL) or by an untagged
		caller ("") still link a consumed batch to every output of its voucher.
		Regression guarded: the lane scope dropping untagged history (not R1-R6)."""
		for batch_no in (
			"CUST2-2F08-M-G-24KT-99.9-Y-03",
			"CUST2-2F08-M-G-24KT-99.9-Y-04",
		):
			with self.subTest(batch_no=batch_no):
				result, _, _ = self._linked(batch_no, True, self._execute)
				self.assertEqual(
					result, sorted([batch_no, "CUST9-2F08-M-G-24KT-99.9-Y-01"])
				)

	def test_without_the_column_every_output_of_the_voucher_is_linked(self):
		"""T53/T61, the control: the query a site without the column gets -- the one the
		report sent before the fix -- links batch 11 to every output of MAT-STE-19453. It
		shows the fixture tells the two apart, so the lane-scoped answers are not vacuous."""
		result, _, _ = self._linked(BATCH_11, False, self._execute)

		self.assertEqual(
			result,
			sorted(
				[
					BATCH_11,
					"CUST1-2F09-M-G-22KT-91.75-Y-11-A",
					"GE2F093-MGL22919Y0-R7K21",
					"CUST1-2F09-M-G-22KT-91.75-Y-12-A",
				]
			),
		)
