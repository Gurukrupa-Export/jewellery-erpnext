# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the Employee Loss Entry engine (req #8).

Pure-logic tests: DB / warehouse / lock-order externals are patched so the
resolvers, the FIFO allocator and the "Process Loss" Stock Entry builder can be
exercised in isolation. Placed under tests/ (not the doctype folder) so the
runner does not auto-bootstrap link-dependency test records (india_compliance
Supplier GST setup fails in this environment).

On submit the engine consumes the loss qty from the employee MSL (Raw Material)
warehouse and produces the mapped loss-variant item into the department Scrap
warehouse.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_loss_entry import (
	employee_loss_entry as ele,
)


def _det_flt(value, precision=None, rounding_method=None):
	try:
		num = float(value or 0)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


def _row(item_code, qty, batch_no=None, idx=1):
	return SimpleNamespace(item_code=item_code, qty=qty, batch_no=batch_no, idx=idx)


def _doc(**fields):
	defaults = {
		"name": "EMP-LOSS-00001",
		"company": "GK",
		"branch": "Main",
		"department": "Casting - GK",
		"manufacturer": "Shubh",
		"employee": "EMP-0001",
		"posting_date": "2026-07-10",
		"posting_time": "10:00:00",
		"msl_warehouse": "EMP-0001 RM - GK",
		"scrap_warehouse": "Casting Scrap - GK",
		"stock_entry": None,
		"items": [_row("M-G-18KT-75.4-Y", 5.0)],
	}
	defaults.update(fields)
	d = SimpleNamespace(**defaults)
	d.db_set = MagicMock()
	return d


class _FakeSE:
	"""Captures the Stock Entry payload the builder constructs."""

	def __init__(self, payload):
		self.payload = payload
		self.items = []
		self.name = "SE-LOSS-0001"
		self.saved = False
		self.submitted = False
		self.flags = SimpleNamespace()

	def append(self, table, row):
		self.items.append(row)

	def save(self):
		self.saved = True

	def submit(self):
		self.submitted = True


class TestResolvers(IntegrationTestCase):
	def test_msl_warehouse_found(self):
		with patch("frappe.db.get_value", return_value="EMP RM - GK"):
			self.assertEqual(ele._resolve_msl_warehouse(_doc()), "EMP RM - GK")

	def test_msl_warehouse_missing_throws(self):
		with patch("frappe.db.get_value", return_value=None):
			with self.assertRaises(ValidationError):
				ele._resolve_msl_warehouse(_doc())

	def test_msl_warehouse_no_employee_throws(self):
		with self.assertRaises(ValidationError):
			ele._resolve_msl_warehouse(_doc(employee=None))

	def test_scrap_warehouse(self):
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion.get_scrap_warehouse",
			return_value="Scrap - GK",
		):
			self.assertEqual(ele._resolve_scrap_warehouse(_doc()), "Scrap - GK")

	def test_scrap_warehouse_no_department_throws(self):
		with self.assertRaises(ValidationError):
			ele._resolve_scrap_warehouse(_doc(department=None))

	def test_loss_item_no_variant_throws(self):
		with patch("frappe.db.get_value", return_value=None):
			with self.assertRaises(ValidationError):
				ele._resolve_loss_item(_doc(), "ITEM-X")

	def test_loss_item_no_manufacturer_throws(self):
		with patch("frappe.db.get_value", return_value="M"):
			with self.assertRaises(ValidationError):
				ele._resolve_loss_item(_doc(manufacturer=None), "M-G-18KT-75.4-Y")

	def test_loss_item_none_throws(self):
		with patch("frappe.db.get_value", return_value="M"), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.get_item_loss_item",
			return_value=None,
		):
			with self.assertRaises(ValidationError):
				ele._resolve_loss_item(_doc(), "M-G-18KT-75.4-Y")

	def test_loss_item_happy(self):
		with patch("frappe.db.get_value", return_value="M"), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.get_item_loss_item",
			return_value="ML-G-18KT-75.4-Y",
		):
			self.assertEqual(
				ele._resolve_loss_item(_doc(), "M-G-18KT-75.4-Y"), "ML-G-18KT-75.4-Y"
			)


class TestFifoBatches(IntegrationTestCase):
	def setUp(self):
		self._patches = [
			patch.object(ele, "_loss_precision", return_value=3),
			patch.object(ele, "flt", _det_flt),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def test_non_batch_item(self):
		with patch("frappe.get_cached_value", return_value=0):
			out = ele._fifo_batches(_doc(), "PURE-G", "WH", 5.0)
		self.assertEqual(len(out), 1)
		self.assertIsNone(out[0].batch_no)
		self.assertEqual(out[0].qty, 5.0)

	def test_allocates(self):
		batches = [
			SimpleNamespace(batch_no="B1", qty=3.0),
			SimpleNamespace(batch_no="B2", qty=2.0),
		]
		with patch("frappe.get_cached_value", return_value=1), patch(
			"jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger.capped_auto_batch_nos",
			return_value=batches,
		):
			out = ele._fifo_batches(_doc(), "M-G", "WH", 5.0)
		self.assertEqual([b.batch_no for b in out], ["B1", "B2"])

	def test_insufficient_throws(self):
		batches = [SimpleNamespace(batch_no="B1", qty=3.0)]
		with patch("frappe.get_cached_value", return_value=1), patch(
			"jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger.capped_auto_batch_nos",
			return_value=batches,
		):
			with self.assertRaises(ValidationError):
				ele._fifo_batches(_doc(), "M-G", "WH", 5.0)


class TestBuilder(IntegrationTestCase):
	def setUp(self):
		self._patches = [
			patch.object(ele, "_loss_precision", return_value=3),
			patch.object(ele, "flt", _det_flt),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def _build(self, doc, existing=False, fifo=None):
		captured = {}

		def _get_doc(payload):
			se = _FakeSE(payload)
			captured["se"] = se
			return se

		default_fifo = fifo or [SimpleNamespace(batch_no="B-001", qty=5.0)]

		with patch("frappe.db.exists", return_value=existing), patch(
			"frappe.new_doc", return_value=SimpleNamespace()
		), patch("frappe.get_doc", side_effect=_get_doc), patch.object(
			ele, "_resolve_loss_item", return_value="ML-G-18KT-75.4-Y"
		), patch.object(ele, "_fifo_batches", return_value=default_fifo), patch(
			"jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"
		), patch(
			"jewellery_erpnext.jewellery_erpnext.lock_order.preallocate_series_for_docs"
		), patch(
			"jewellery_erpnext.jewellery_erpnext.lock_order.stock_lock_key",
			side_effect=lambda i, w, b=None: (i, w, b or ""),
		):
			ele.make_employee_loss_stock_entry(doc)
		return captured.get("se")

	def test_idempotency_guard_skips(self):
		doc = _doc(stock_entry="SE-OLD")
		se = self._build(doc, existing=True)
		self.assertIsNone(se)
		doc.db_set.assert_not_called()

	def test_se_header_and_rows(self):
		doc = _doc()
		se = self._build(doc)
		self.assertEqual(se.payload["stock_entry_type"], "Process Loss")
		self.assertEqual(se.payload["purpose"], "Repack")
		self.assertEqual(se.payload["auto_created"], 1)
		self.assertEqual(se.payload["manufacturer"], "Shubh")
		self.assertEqual(se.payload["company"], "GK")
		self.assertNotIn("_customer", se.payload)
		# 1 consume + 1 produce
		self.assertEqual(len(se.items), 2)
		consume, produce = se.items[0], se.items[1]
		self.assertEqual(consume["item_code"], "M-G-18KT-75.4-Y")
		self.assertEqual(consume["s_warehouse"], "EMP-0001 RM - GK")
		self.assertEqual(consume["batch_no"], "B-001")
		self.assertEqual(consume["use_serial_batch_fields"], 1)
		self.assertNotIn("t_warehouse", consume)
		self.assertEqual(produce["item_code"], "ML-G-18KT-75.4-Y")
		self.assertEqual(produce["qty"], 5.0)
		self.assertEqual(produce["t_warehouse"], "Casting Scrap - GK")
		self.assertEqual(produce["is_finished_item"], 1)
		self.assertEqual(produce["set_basic_rate_manually"], 1)
		self.assertEqual(produce["inventory_type"], "Regular Stock")
		self.assertNotIn("batch_no", produce)
		self.assertTrue(se.saved and se.submitted)
		doc.db_set.assert_called_once_with("stock_entry", "SE-LOSS-0001")

	def test_multiple_items_produce_per_row(self):
		doc = _doc(
			items=[_row("M-G-18KT-75.4-Y", 5.0), _row("F-G-18KT-75.4-Y", 2.0, idx=2)]
		)
		se = self._build(doc)
		# 2 consume + 2 produce
		self.assertEqual(len(se.items), 4)
		produce_rows = [r for r in se.items if r.get("t_warehouse")]
		self.assertEqual(len(produce_rows), 2)

	def test_batch_specified_on_row_used_directly(self):
		doc = _doc(items=[_row("M-G-18KT-75.4-Y", 4.0, batch_no="B-USER")])
		captured = {}

		def _get_doc(payload):
			se = _FakeSE(payload)
			captured["se"] = se
			return se

		with patch("frappe.db.exists", return_value=False), patch(
			"frappe.new_doc", return_value=SimpleNamespace()
		), patch("frappe.get_doc", side_effect=_get_doc), patch.object(
			ele, "_resolve_loss_item", return_value="ML-G-18KT-75.4-Y"
		), patch.object(
			ele, "_fifo_batches", side_effect=AssertionError("FIFO must not run")
		), patch("jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"), patch(
			"jewellery_erpnext.jewellery_erpnext.lock_order.preallocate_series_for_docs"
		), patch(
			"jewellery_erpnext.jewellery_erpnext.lock_order.stock_lock_key",
			side_effect=lambda i, w, b=None: (i, w, b or ""),
		):
			ele.make_employee_loss_stock_entry(doc)
		se = captured["se"]
		consume = se.items[0]
		self.assertEqual(consume["batch_no"], "B-USER")
		self.assertEqual(consume["qty"], 4.0)


class TestCancel(IntegrationTestCase):
	def test_noop_when_no_se(self):
		doc = _doc(stock_entry=None)
		with patch("frappe.get_doc") as m:
			ele.cancel_employee_loss_stock_entries(doc)
			m.assert_not_called()

	def test_cancels_linked_se(self):
		doc = _doc(stock_entry="SE-LOSS-0001")
		cancelled = []
		fake = SimpleNamespace(cancel=lambda: cancelled.append(True))
		with patch(
			"frappe.db.get_value",
			return_value=SimpleNamespace(name="SE-LOSS-0001", docstatus=1),
		), patch("frappe.get_doc", return_value=fake):
			ele.cancel_employee_loss_stock_entries(doc)
		self.assertEqual(len(cancelled), 1)

	def test_skips_already_cancelled_se(self):
		doc = _doc(stock_entry="SE-LOSS-0001")
		called = []
		with patch(
			"frappe.db.get_value",
			return_value=SimpleNamespace(name="SE-LOSS-0001", docstatus=2),
		), patch("frappe.get_doc", side_effect=lambda *a, **k: called.append(1)):
			ele.cancel_employee_loss_stock_entries(doc)
		self.assertEqual(called, [])
