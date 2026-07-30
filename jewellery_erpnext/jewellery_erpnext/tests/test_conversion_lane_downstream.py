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
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting import batch_rename
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

		Every existing caller (SNC's create_repack_metal_conversion, Customer Goods
		Received, Subcontracting Repack) builds this, and SNC throws if nothing is
		minted -- so this path must not start declining.
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
			if doctype == "Stock Entry Detail":
				wanted = set(filters["name"][1])
				return [
					frappe._dict(name=n, custom_conversion_lane=lane_rows.get(n))
					for n in wanted
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
