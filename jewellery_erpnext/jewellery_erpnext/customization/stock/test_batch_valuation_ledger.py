# Copyright (c) 2026, Nirali and Contributors
# See license.txt
"""Unit tests for the authoritative-balance FIFO cap in ``batch_valuation_ledger``.

``capped_auto_batch_nos`` wraps erpnext ``get_auto_batch_nos`` so that a *phantom*
batch — one whose SLE-joined availability is inflated by an ORPHANED Serial-and-Batch
Bundle (``docstatus = 1`` with no Stock Ledger Entry) — is capped/dropped using the
SAME source erpnext's submit-time ``BatchNoValuation`` guard uses
(``Serial and Batch Entry`` where ``docstatus = 1``). This prevents the Tree Number
"Issue Material" ``BatchNegativeStockError`` where the allocator commits stock a batch
does not physically hold.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger import (
	capped_auto_batch_nos,
	get_authoritative_batch_qty,
)

_BVL = "jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger"


def _b(batch_no, qty, warehouse="WH-Src"):
	return frappe._dict(batch_no=batch_no, qty=qty, warehouse=warehouse)


class TestCappedAutoBatchNos(FrappeTestCase):
	@patch(f"{_BVL}.get_authoritative_batch_qty")
	@patch(f"{_BVL}.get_auto_batch_nos")
	def test_drops_phantom_batch(self, mock_auto, mock_auth):
		# get_auto_batch_nos over-reports PHANTOM by 5.0 (orphan bundle has no SLE, so
		# its -5 is never subtracted); the authoritative reader says PHANTOM is 0.
		mock_auto.return_value = [
			_b("PHANTOM", 5.0),
			_b("REAL1", 300.0),
			_b("REAL2", 271.129),
		]
		mock_auth.return_value = {
			"REAL1": 300.0,
			"REAL2": 271.129,
		}  # PHANTOM absent -> 0
		out = capped_auto_batch_nos(frappe._dict(item_code="I", warehouse="WH-Src"))
		self.assertEqual([b.batch_no for b in out], ["REAL1", "REAL2"])
		self.assertAlmostEqual(sum(b.qty for b in out), 571.129)

	@patch(f"{_BVL}.get_authoritative_batch_qty")
	@patch(f"{_BVL}.get_auto_batch_nos")
	def test_caps_partially_inflated_batch(self, mock_auto, mock_auth):
		# Reported 5.0 but authoritative only 2.0 -> cap the row to 2.0 (never dropped).
		mock_auto.return_value = [_b("B1", 5.0)]
		mock_auth.return_value = {"B1": 2.0}
		out = capped_auto_batch_nos(frappe._dict(item_code="I", warehouse="WH-Src"))
		self.assertEqual([(b.batch_no, b.qty) for b in out], [("B1", 2.0)])

	@patch(f"{_BVL}.get_authoritative_batch_qty")
	@patch(f"{_BVL}.get_auto_batch_nos")
	def test_qty_path_truncates_reals_only(self, mock_auto, mock_auth):
		# With a qty request the phantom is dropped first, then the real batches are
		# FIFO-truncated to the requested 10.
		mock_auto.return_value = [
			_b("PHANTOM", 5.0),
			_b("REAL1", 4.0),
			_b("REAL2", 100.0),
		]
		mock_auth.return_value = {"REAL1": 4.0, "REAL2": 100.0}
		out = capped_auto_batch_nos(
			frappe._dict(item_code="I", warehouse="WH-Src", qty=10)
		)
		self.assertNotIn("PHANTOM", [b.batch_no for b in out])
		self.assertEqual([b.batch_no for b in out], ["REAL1", "REAL2"])
		self.assertAlmostEqual(sum(b.qty for b in out), 10.0)

	@patch(f"{_BVL}.get_authoritative_batch_qty")
	@patch(f"{_BVL}.get_auto_batch_nos")
	def test_clean_data_is_unchanged(self, mock_auto, mock_auth):
		# authoritative == reported for every batch -> identical output (no behaviour change).
		mock_auto.return_value = [_b("B1", 300.0), _b("B2", 271.129), _b("B3", 1.286)]
		mock_auth.return_value = {"B1": 300.0, "B2": 271.129, "B3": 1.286}
		out = capped_auto_batch_nos(frappe._dict(item_code="I", warehouse="WH-Src"))
		self.assertEqual(
			[(b.batch_no, b.qty) for b in out],
			[("B1", 300.0), ("B2", 271.129), ("B3", 1.286)],
		)

	@patch(f"{_BVL}.get_authoritative_batch_qty")
	@patch(f"{_BVL}.get_auto_batch_nos")
	def test_strips_qty_before_availability_query(self, mock_auto, mock_auth):
		# `qty` must NOT reach get_auto_batch_nos, else a phantom could FIFO-truncate
		# real batches out of the candidate list before capping runs.
		mock_auto.return_value = [_b("REAL1", 100.0)]
		mock_auth.return_value = {"REAL1": 100.0}
		capped_auto_batch_nos(frappe._dict(item_code="I", warehouse="WH-Src", qty=10))
		passed_kwargs = mock_auto.call_args[0][0]
		self.assertNotIn("qty", passed_kwargs)


class TestGetAuthoritativeBatchQty(FrappeTestCase):
	@patch(f"{_BVL}.frappe.db.sql")
	def test_query_filters_match_submit_guard(self, mock_sql):
		mock_sql.return_value = [frappe._dict(batch_no="B1", qty=3.0)]
		out = get_authoritative_batch_qty("ITEM", "WH", ["B1", "B2"])
		self.assertEqual(out, {"B1": 3.0})
		query = mock_sql.call_args[0][0]
		params = mock_sql.call_args[0][1]
		# Same scope as BatchNoValuation.get_batch_stock_before_date (the submit guard):
		# submitted bundle entries, both directions, Pick List excluded.
		self.assertIn("docstatus = 1", query)
		self.assertIn("Inward", query)
		self.assertIn("Outward", query)
		self.assertIn("Pick List", query)
		self.assertEqual(params["item_code"], "ITEM")
		self.assertEqual(params["warehouse"], "WH")
		self.assertEqual(params["batch_nos"], ("B1", "B2"))

	def test_empty_inputs_short_circuit(self):
		self.assertEqual(get_authoritative_batch_qty("", "WH", ["B1"]), {})
		self.assertEqual(get_authoritative_batch_qty("I", "", ["B1"]), {})
		self.assertEqual(get_authoritative_batch_qty("I", "WH", []), {})
