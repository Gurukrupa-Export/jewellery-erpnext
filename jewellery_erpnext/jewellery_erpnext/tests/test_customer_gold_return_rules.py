# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Receipt-linked return rules that need no documents: which rows are governed at all, the
preview's fallback, and routes that count only the receipt's own share. Pure; for CI."""

import unittest
from unittest.mock import patch

import frappe

from jewellery_erpnext.customer_subcontracting import customer_gold_return as cgr

FUL = "jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment"
ALLOC = "jewellery_erpnext.customer_subcontracting.customer_gold_allocations"


def _row(**fields):
	values = {"idx": 1, "item_code": "G-24", "qty": 1, "conversion_factor": 1}
	values.update(fields)
	return frappe._dict(values)


def _doc(**fields):
	values = {"company": "C", "stock_entry_type": "Return", "doctype": "Stock Entry"}
	values.update(fields)
	return frappe._dict(values)


class TestWhichRowsAreGoverned(unittest.TestCase):
	"""Review P2: the configured return type used to refuse every row that did not trace to a
	Customer Gold receipt, stranding customer goods received before the ledger existed."""

	def _resolve(
		self, row, doc=None, batches=("B1",), owner="CUST", history=False, receipts=()
	):
		with patch(f"{FUL}._row_batches", return_value=list(batches)), patch(
			f"{FUL}._batch_owner", return_value=owner
		), patch.object(cgr, "_has_ledger_history", return_value=history), patch(
			f"{ALLOC}.receipts_of_batch", return_value=list(receipts)
		), patch.object(cgr, "_voucher_has_receipt_events", return_value=False), patch(
			f"{ALLOC}.effective_receipt_events", return_value=[]
		):
			return cgr._resolve_return_row(doc or _doc(), row)

	def test_a_row_with_no_batch_is_not_governed(self):
		self.assertIsNone(self._resolve(_row(), batches=()))

	def test_company_stock_is_not_governed(self):
		self.assertIsNone(self._resolve(_row(), owner=None))

	def test_a_customer_batch_received_before_the_ledger_is_left_as_it_was(self):
		self.assertIsNone(self._resolve(_row(), history=False))

	def test_a_row_linked_to_a_pre_ledger_receipt_is_left_as_it_was(self):
		row = _row(against_stock_entry="SE-OLD", ste_detail="R1")
		self.assertIsNone(self._resolve(row, history=False))

	def test_a_tracked_batch_without_a_receipt_link_is_refused(self):
		"""A descendant batch (it has ledger history but no receipt of its own) must name the
		receipt it is returned against."""
		with self.assertRaises(frappe.ValidationError) as caught:
			self._resolve(_row(), history=True)
		self.assertIn("Create > Issue", str(caught.exception))

	def test_a_multi_batch_row_of_tracked_gold_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._resolve(_row(), batches=("B1", "B2"), history=True)

	def test_a_multi_batch_row_of_untracked_stock_is_not_governed(self):
		self.assertIsNone(self._resolve(_row(), batches=("B1", "B2"), history=False))


class TestPreviewFallback(unittest.TestCase):
	def test_flow_off_opens_the_classic_issue(self):
		"""Review P1: with the flow off the preview returned no rows and the desk showed
		'Nothing to return' instead of opening the Issue."""
		entry = frappe._dict(company="C", items=[])
		entry.check_permission = lambda ptype: None
		with patch.object(cgr.frappe, "get_doc", return_value=entry), patch.object(
			cgr, "is_customer_gold_enabled", return_value=False
		):
			self.assertEqual(
				cgr.get_customer_gold_return_preview("SE-1"),
				{"rows": [], "fallback": True},
			)

	def test_a_receipt_with_no_ledger_events_opens_the_classic_issue(self):
		entry = frappe._dict(company="C", items=[])
		entry.check_permission = lambda ptype: None
		with patch.object(cgr.frappe, "get_doc", return_value=entry), patch.object(
			cgr, "is_customer_gold_enabled", return_value=True
		), patch.object(cgr, "is_ledger_schema_ready", return_value=True), patch(
			f"{ALLOC}.effective_receipt_events", return_value=[]
		):
			self.assertEqual(
				cgr.get_customer_gold_return_preview("SE-1"),
				{"rows": [], "fallback": True},
			)


def _holding(
	batch, warehouse, free_receipt_qty, same_item=True, stage="RM", purity=99.9
):
	return {
		"batch_no": batch,
		"warehouse": warehouse,
		"free_receipt_qty": free_receipt_qty,
		"same_item": same_item,
		"stage": stage,
		"purity": purity,
	}


class TestRoutesCountOnlyThisReceipt(unittest.TestCase):
	"""Review P3: R1 and R2 share batch B1 in custody (5 g each after a production drew half of
	it). R1 still owes 10; only 5 of B1's 10 free grams are R1's, so the route is not Direct."""

	event = frappe._dict(batch_no="B1")

	def test_a_shared_batch_is_not_all_this_receipts(self):
		holdings = [
			_holding("B1", "CUSTODY", 5.0),
			_holding("B1-22", "WIP", 5.0, same_item=False, stage="WIP", purity=91.75),
		]
		route, _ = cgr._choose_route(self.event, holdings, 10.0, "CUSTODY")
		self.assertEqual(route, "Settle, then Issue")
		self.assertEqual(cgr._direct_available(self.event, holdings, "CUSTODY"), 5.0)

	def test_direct_when_the_custody_share_covers_it(self):
		holdings = [_holding("B1", "CUSTODY", 10.0)]
		self.assertEqual(
			cgr._choose_route(self.event, holdings, 10.0, "CUSTODY")[0], "Direct"
		)

	def test_finished_pieces_and_scrap_are_never_return_metal(self):
		holdings = [
			_holding("FG", "FG-WH", 8.0, same_item=False, stage="FG"),
			_holding("SCRAP", "SCRAP-WH", 2.0, same_item=False, stage="Loss / Scrap"),
		]
		route, limit = cgr._choose_route(self.event, holdings, 10.0, "CUSTODY")
		self.assertEqual(route, "Shortfall")
		self.assertIn("covers only 0", limit)
