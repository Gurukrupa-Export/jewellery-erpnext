# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the Metal Conversions "Is Melting Loss" flow.

Pure-logic tests: no DB / no Frappe site. ``frappe.get_precision`` and every DB
call are patched so the guards and the Stock Entry builder can be exercised in
isolation.

The feature: when ``is_melting_loss`` is checked, submit books ONLY the Loss Qty
as a single "Process Loss" (Repack) Stock Entry moving the loss from the
department Raw Material warehouse to the department Scrap warehouse; the remaining
quantity is untouched and NO conversion Stock Entry is created.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events import (
	melting_loss,
	utils,
)

_UTILS_PATH = (
	"jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.utils"
)


def _det_flt(value, precision=None, rounding_method=None):
	"""Deterministic stand-in for frappe.utils.flt.

	frappe's flt(x, precision) rounds via get_system_settings("rounding_method");
	under the test transaction that lookup can raise and flt's bare except then
	returns 0.0, which is an environment artifact unrelated to the logic under
	test. Patching the module's flt with this shim keeps these pure-logic tests
	deterministic regardless of the site's rounding configuration.
	"""
	try:
		num = float(value or 0)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


def _batch_row(qty, batch):
	return SimpleNamespace(qty=qty, batch=batch)


def _doc(**fields):
	"""A stand-in Metal Conversions document."""
	defaults = {
		"name": "mc0001",
		"is_melting_loss": 1,
		"multiple_metal_converter": 0,
		"source_item": "M-G-18KT-75.4-Y",
		"source_qty": 500.0,
		"loss_qty": 20.0,
		"is_customer_metal": 0,
		"customer": None,
		"company": "GK",
		"branch": "Main",
		"department": "Casting - GK",
		"manufacturer": "Shubh",
		"employee": "EMP-0001",
		"source_warehouse": "Casting RM - GK",
		"inventory_type": "Regular Stock",
		"source_batch_details": [_batch_row(20.0, "B-001")],
		"target_item": "leftover",
		"target_qty": 480.0,
		"source_alloy_check": 1,
		"source_alloy": "alloy",
		"source_alloy_qty": 5,
		"source_alloy_batch": "AB-1",
		"target_alloy_check": 1,
		"target_alloy": "talloy",
		"target_alloy_qty": 3,
		"alloy_batch_details": [_batch_row(5.0, "AB-1")],
	}
	defaults.update(fields)
	d = SimpleNamespace(**defaults)
	# SimpleNamespace has no .get(); melting_loss uses doc.get(...) in a few spots.
	d.get = lambda k, default=None: getattr(d, k, default)
	d.db_set = MagicMock()
	return d


class TestMeltingLossValidation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		if not hasattr(frappe, "db") or not frappe.db:
			frappe.db = MagicMock()

		self._patches = [
			patch.object(melting_loss, "_loss_precision", return_value=3),
			patch.object(melting_loss, "flt", _det_flt),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def test_noop_when_not_melting_loss(self):
		doc = _doc(is_melting_loss=0)
		# Must not touch conversion fields when the flag is off.
		melting_loss.validate_melting_loss(doc)
		self.assertEqual(doc.target_item, "leftover")

	def test_blocks_multi_mode(self):
		doc = _doc(multiple_metal_converter=1)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_source_item_mandatory(self):
		doc = _doc(source_item=None)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_source_qty_must_be_positive(self):
		for bad in (0, -5):
			with self.assertRaises(ValidationError):
				melting_loss.validate_melting_loss(_doc(source_qty=bad))

	def test_loss_qty_mandatory(self):
		doc = _doc(loss_qty=0)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_loss_qty_subprecision_blocked(self):
		# 0.0004 rounds to 0.000 at precision 3 -> V5.
		doc = _doc(loss_qty=0.0004)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_loss_qty_negative_blocked(self):
		doc = _doc(loss_qty=-1)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_loss_cannot_exceed_source(self):
		doc = _doc(source_qty=500, loss_qty=501)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_full_loss_allowed(self):
		# Equality is legal: the whole melt is scrapped.
		doc = _doc(source_qty=500, loss_qty=500)
		melting_loss.validate_melting_loss(doc)  # no raise
		self.assertIsNone(doc.target_item)

	def test_conversion_fields_cleared(self):
		doc = _doc()
		melting_loss.validate_melting_loss(doc)
		self.assertIsNone(doc.target_item)
		self.assertEqual(doc.target_qty, 0)
		self.assertIsNone(doc.source_alloy)
		self.assertIsNone(doc.target_alloy)
		self.assertEqual(doc.source_alloy_check, 0)
		self.assertEqual(doc.target_alloy_check, 0)
		self.assertEqual(doc.alloy_batch_details, [])

	def test_customer_mandatory_when_customer_metal(self):
		doc = _doc(is_customer_metal=1, customer=None)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_customer_cleared_when_not_customer_metal(self):
		doc = _doc(is_customer_metal=0, customer="ACME")
		melting_loss.validate_melting_loss(doc)
		self.assertIsNone(doc.customer)

	def test_customer_kept_when_customer_metal(self):
		doc = _doc(is_customer_metal=1, customer="ACME")
		melting_loss.validate_melting_loss(doc)
		self.assertEqual(doc.customer, "ACME")


class _FakeSE:
	"""Captures the Stock Entry payload the builder constructs."""

	def __init__(self, payload):
		self.payload = payload
		self.doctype = (
			payload.get("doctype", "Stock Entry")
			if isinstance(payload, dict)
			else "Stock Entry"
		)
		self.items = []
		self.name = "SE-LOSS-0001"
		self.saved = False
		self.submitted = False

	def append(self, table, row):
		self.items.append(row)

	def save(self):
		self.saved = True

	def submit(self):
		self.submitted = True


class TestMeltingLossBuilder(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		if not hasattr(frappe, "db") or not frappe.db:
			frappe.db = MagicMock()

		self._patches = [
			patch.object(melting_loss, "_loss_precision", return_value=3),
			patch.object(melting_loss, "flt", _det_flt),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def _build(self, doc, existing=False):
		"""Run make_melting_loss_stock_entry with all externals mocked; return the SE."""
		captured = {}

		def _get_doc(payload):
			if isinstance(payload, str):
				# if payload is doctype name
				payload = {"doctype": payload}
			se = _FakeSE(payload)
			captured["se"] = se
			return se

		with patch("frappe.db.exists", return_value=existing), patch(
			"frappe.get_doc", side_effect=_get_doc
		), patch("frappe.new_doc", side_effect=_get_doc), patch.object(
			melting_loss, "_resolve_loss_item", return_value="ML-G-18KT-75.4-Y"
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion.get_scrap_warehouse",
			return_value="Casting Scrap - GK",
		), patch("jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"), patch(
			"jewellery_erpnext.jewellery_erpnext.lock_order.stock_lock_key",
			side_effect=lambda i, w, b: (i, w, b or ""),
		):
			melting_loss.make_melting_loss_stock_entry(doc)
		return captured.get("se")

	def test_idempotency_guard_skips(self):
		doc = _doc()
		se = self._build(doc, existing=True)
		self.assertIsNone(se)  # frappe.get_doc never called
		doc.db_set.assert_not_called()

	def test_se_header_and_rows(self):
		doc = _doc(
			source_batch_details=[_batch_row(12.0, "B-001"), _batch_row(8.0, "B-002")]
		)
		se = self._build(doc)
		# Header
		self.assertEqual(se.payload["stock_entry_type"], "Process Loss")
		self.assertEqual(se.payload["purpose"], "Repack")
		self.assertEqual(se.payload["auto_created"], 1)
		self.assertEqual(se.payload["custom_metal_conversion_reference"], "mc0001")
		self.assertEqual(se.payload["manufacturer"], "Shubh")  # DR-1 header stamp
		self.assertNotIn("_customer", se.payload)  # DR-2: never set
		# Rows: 2 consume + 1 produce
		self.assertEqual(len(se.items), 3)
		consume = se.items[:2]
		produce = se.items[2]
		for c in consume:
			self.assertEqual(c["item_code"], "M-G-18KT-75.4-Y")
			self.assertEqual(c["s_warehouse"], "Casting RM - GK")
			self.assertNotIn("t_warehouse", c)
			self.assertTrue(c["batch_no"])
			self.assertEqual(c["use_serial_batch_fields"], 1)
		# Produce row: loss item -> scrap, no batch_no, Regular Stock write-off.
		self.assertEqual(produce["item_code"], "ML-G-18KT-75.4-Y")
		self.assertEqual(produce["qty"], 20.0)
		self.assertEqual(produce["t_warehouse"], "Casting Scrap - GK")
		self.assertEqual(produce["is_finished_item"], 1)
		self.assertEqual(produce["set_basic_rate_manually"], 1)
		self.assertEqual(produce["inventory_type"], "Regular Stock")
		self.assertNotIn("batch_no", produce)
		self.assertTrue(se.saved and se.submitted)
		doc.db_set.assert_called_once_with("stock_entry", "SE-LOSS-0001")

	def test_consume_rows_sorted_by_batch(self):
		# RULE A: rows consumed in deterministic batch order.
		doc = _doc(
			source_batch_details=[_batch_row(8.0, "B-002"), _batch_row(12.0, "B-001")]
		)
		se = self._build(doc)
		consume_batches = [r["batch_no"] for r in se.items[:2]]
		self.assertEqual(consume_batches, ["B-001", "B-002"])

	def test_allocation_mismatch_throws(self):
		# V8: source_batch_details no longer sums to loss_qty.
		doc = _doc(loss_qty=20.0, source_batch_details=[_batch_row(15.0, "B-001")])
		with self.assertRaises(ValidationError):
			self._build(doc)


class TestMeltingLossCancel(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_cancel_query_is_scoped(self):
		doc = _doc()
		captured = {}

		def _get_all(dt, filters=None, pluck=None):
			captured["dt"] = dt
			captured["filters"] = filters
			return []

		with patch("frappe.db.get_all", side_effect=_get_all):
			melting_loss.cancel_melting_loss_stock_entries(doc)
		f = captured["filters"]
		self.assertEqual(captured["dt"], "Stock Entry")
		self.assertEqual(f["custom_metal_conversion_reference"], "mc0001")
		self.assertEqual(f["stock_entry_type"], "Process Loss")
		self.assertEqual(f["auto_created"], 1)
		self.assertEqual(f["docstatus"], 1)

	def test_cancel_cancels_each_found(self):
		doc = _doc()
		cancelled = []
		fake = SimpleNamespace(cancel=lambda: cancelled.append(True))
		with patch("frappe.db.get_all", return_value=["SE-LOSS-0001"]), patch(
			"frappe.get_doc", return_value=fake
		):
			melting_loss.cancel_melting_loss_stock_entries(doc)
		self.assertEqual(len(cancelled), 1)


class _FakeMCDoc:
	"""Stand-in Metal Conversions doc for update_source_betch.

	SimpleNamespace lacks ``.append`` and ``.get``; update_source_betch reassigns
	``self.source_batch_details = []`` then appends dict rows to it.
	"""

	def __init__(self, **fields):
		self.source_batch_details = []
		for key, value in fields.items():
			setattr(self, key, value)

	def get(self, key, default=None):
		return getattr(self, key, default)

	def append(self, table, row):
		getattr(self, table).append(frappe._dict(row))


class TestUpdateSourceBatch(IntegrationTestCase):
	"""update_source_betch must consume ONLY the capped (authoritative) batch list.

	The bug: raw get_auto_batch_nos over-reports an orphan-SBB phantom batch, whose
	row later throws BatchNegativeStockError at SE submit. Swapping to
	capped_auto_batch_nos drops the phantom here; genuine shortfalls surface the
	friendly V7 throw at validate time instead.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		if not hasattr(frappe, "db") or not frappe.db:
			frappe.db = MagicMock()
		self._patches = [
			patch.object(utils, "flt", _det_flt),
			# Every source batch here is Regular Stock owned by no customer.
			patch(
				f"{_UTILS_PATH}.frappe.db.get_value",
				return_value=("Regular Stock", None),
			),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def _doc(self, **fields):
		defaults = {
			"is_melting_loss": 1,
			"loss_qty": 0.1,
			"source_qty": 2.0,
			"source_item": "M-G-18KT-75.4-Y",
			"source_warehouse": "Waxing RM - GEPL",
			"customer": None,
			"is_customer_metal": 0,
			# Set posting_time so the builder never calls the real nowtime().
			"date": "2026-07-15",
			"posting_time": "10:00:00",
		}
		defaults.update(fields)
		return _FakeMCDoc(**defaults)

	def test_phantom_dropped_and_qty_omitted(self):
		# capped_auto_batch_nos has already dropped the orphan phantom; only a real
		# batch survives. The phantom GE2D081-... must NOT appear in the result.
		real = [frappe._dict(batch_no="REAL", qty=0.5, warehouse="Waxing RM - GEPL")]
		with patch(
			f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=real
		) as mock_capped:
			doc = self._doc()
			utils.update_source_betch(doc)
		self.assertEqual(
			[dict(r) for r in doc.source_batch_details],
			[{"qty": 0.1, "batch": "REAL"}],
		)
		# qty must NOT be passed: it would let a phantom FIFO-truncate real batches
		# before the inventory-type / customer filter runs.
		self.assertNotIn("qty", mock_capped.call_args[0][0])

	def test_friendly_shortfall_throw_not_batch_negative(self):
		# After the phantom is dropped, the real batch can't cover loss_qty (0.1):
		# the friendly V7 throw fires at validate, standing in for the deep
		# BatchNegativeStockError that used to fire at SE submit.
		short = [frappe._dict(batch_no="REAL", qty=0.05, warehouse="Waxing RM - GEPL")]
		with patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=short):
			doc = self._doc()
			with self.assertRaisesRegex(
				ValidationError, "source quantity is not available"
			):
				utils.update_source_betch(doc)

	def test_all_phantom_throws_no_batch(self):
		# Only the phantom existed; capping returns an empty list.
		with patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=[]):
			doc = self._doc()
			with self.assertRaisesRegex(
				ValidationError, "No batch available for given warehouse"
			):
				utils.update_source_betch(doc)

	def test_conversion_mode_unchanged(self):
		# is_melting_loss=0 -> required_qty = source_qty; FIFO across two real batches.
		batches = [
			frappe._dict(batch_no="B1", qty=0.6, warehouse="Waxing RM - GEPL"),
			frappe._dict(batch_no="B2", qty=0.9, warehouse="Waxing RM - GEPL"),
		]
		with patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=batches):
			doc = self._doc(is_melting_loss=0, source_qty=1.0)
			utils.update_source_betch(doc)
		rows = [dict(r) for r in doc.source_batch_details]
		self.assertEqual([r["batch"] for r in rows], ["B1", "B2"])
		self.assertAlmostEqual(sum(r["qty"] for r in rows), 1.0, places=3)
