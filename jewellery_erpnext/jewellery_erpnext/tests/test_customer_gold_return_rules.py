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


#: The real functions, saved before any test patches them. A fake answers only the doctypes it owns
#: and hands every other read to the real one: ``flt(x, precision)`` looks up the rounding method
#: through System Settings, and when a fake raises on (or mis-answers) that read, ``flt`` swallows
#: the error and returns 0 -- but only while the cache is cold, so the test passes or fails by run
#: order (CI ran this class first and read every amount as 0.0).
_REAL_GET_ALL = frappe.get_all
_REAL_GET_DOC = frappe.get_doc


def _owning(doctypes, fake, real):
	"""A side_effect that routes ``doctypes`` to ``fake`` and everything else to ``real``."""

	def side_effect(doctype, *args, **kwargs):
		if doctype in doctypes:
			return fake(doctype, *args, **kwargs)
		return real(doctype, *args, **kwargs)

	return side_effect


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
		with patch.object(
			cgr.frappe,
			"get_doc",
			side_effect=_owning({"Stock Entry"}, lambda *a, **k: entry, _REAL_GET_DOC),
		), patch.object(cgr, "is_customer_gold_enabled", return_value=False):
			self.assertEqual(
				cgr.get_customer_gold_return_preview("SE-1"),
				{"rows": [], "fallback": True},
			)

	def test_a_receipt_with_no_ledger_events_opens_the_classic_issue(self):
		entry = frappe._dict(company="C", items=[])
		entry.check_permission = lambda ptype: None
		with patch.object(
			cgr.frappe,
			"get_doc",
			side_effect=_owning({"Stock Entry"}, lambda *a, **k: entry, _REAL_GET_DOC),
		), patch.object(
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


class TestSharedBatchGuards(unittest.TestCase):
	"""Review P2s on batches two receipts share."""

	def _plan(self, qty=5.0, kind="receipt"):
		receipt = frappe._dict(
			name="EV-A", reference_docname="SE-A", cg_source_row="RA", batch_no="B1"
		)
		return frappe._dict(
			row=_row(qty=qty),
			batch_no="B1",
			warehouse="W",
			customer="CUST",
			receipt=receipt,
			kind=kind,
			booked_rate=7164.83,
			qty=qty,
		)

	def test_a_batch_of_receipts_at_different_rates_is_held(self):
		"""The stock credit is the batch's blended rate, not the named receipt's (D08)."""
		receipts = [
			frappe._dict(
				cg_gross_qty_delta=10,
				cg_carrying_value_delta=71648.30,
				cg_currency="INR",
			),
			frappe._dict(
				cg_gross_qty_delta=10,
				cg_carrying_value_delta=74000.00,
				cg_currency="INR",
			),
		]
		with patch(f"{ALLOC}.receipts_of_batch", return_value=receipts):
			with self.assertRaises(frappe.ValidationError) as caught:
				cgr._check_shared_batch_rates(_doc(), [self._plan()])
		self.assertIn("D08", str(caught.exception))

	def test_a_batch_of_receipts_at_one_rate_passes(self):
		receipts = [
			frappe._dict(
				cg_gross_qty_delta=10,
				cg_carrying_value_delta=71648.30,
				cg_currency="INR",
			),
			frappe._dict(
				cg_gross_qty_delta=5,
				cg_carrying_value_delta=35824.15,
				cg_currency="INR",
			),
		]
		with patch(f"{ALLOC}.receipts_of_batch", return_value=receipts):
			cgr._check_shared_batch_rates(_doc(), [self._plan()])

	def _replay(self, share_a, share_b):
		from jewellery_erpnext.customer_subcontracting import customer_gold_trace as cgt

		replay = cgt.AttributionReplay(
			{
				"SE-A|RA": {"unit": "fine", "per_unit": 0.999, "item_code": "G-24"},
				"SE-B|RB": {"unit": "fine", "per_unit": 0.999, "item_code": "G-24"},
			}
		)
		holding = replay.holdings[("B1", "W")]
		holding.put(10.0, {"SE-A|RA": share_a, "SE-B|RB": share_b})
		return replay

	def test_a_return_beyond_the_receipts_share_of_a_shared_holding_is_refused(self):
		"""5 g each in B1@W: 6 g against A would take 1 g of B's metal."""
		replay = self._replay(4.995, 4.995)
		with self.assertRaises(frappe.ValidationError) as caught:
			cgr._check_receipt_share([self._plan(qty=6.0)], replay)
		self.assertIn("only", str(caught.exception))

	def test_a_return_within_the_receipts_share_passes(self):
		cgr._check_receipt_share([self._plan(qty=5.0)], self._replay(4.995, 4.995))

	def test_a_holding_that_is_all_this_receipts_is_not_limited_by_this_check(self):
		cgr._check_receipt_share([self._plan(qty=10.0)], self._replay(9.99, 0.0))


class TestSettlementReconciliation(unittest.TestCase):
	"""Review P3: the JE rounds the customer total once; the allocations must tie to it."""

	def test_the_paisa_residual_lands_on_the_largest_allocation(self):
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_allocations as cga,
		)

		rows = [
			frappe._dict(name="A1", customer="CUST", amount=3582.42),
			frappe._dict(name="A2", customer="CUST", amount=3582.42),
		]
		with patch.object(
			cga, "is_allocation_schema_ready", return_value=True
		), patch.object(
			cga.frappe,
			"get_all",
			side_effect=_owning(
				{cga.ALLOCATION_DOCTYPE}, lambda *a, **k: rows, _REAL_GET_ALL
			),
		), patch.object(cga.frappe.db, "set_value") as set_value:
			cga.reconcile_to_settlement(["EV-1", "EV-2"], {"CUST": 7164.83})
		set_value.assert_called_once()
		name, field, value = set_value.call_args.args[1:4]
		self.assertEqual(field, "amount")
		self.assertAlmostEqual(value, 3582.41, places=2)

	def test_a_real_difference_is_left_for_the_report(self):
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_allocations as cga,
		)

		rows = [frappe._dict(name="A1", customer="CUST", amount=100.00)]
		with patch.object(
			cga, "is_allocation_schema_ready", return_value=True
		), patch.object(
			cga.frappe,
			"get_all",
			side_effect=_owning(
				{cga.ALLOCATION_DOCTYPE}, lambda *a, **k: rows, _REAL_GET_ALL
			),
		), patch.object(cga.frappe.db, "set_value") as set_value:
			cga.reconcile_to_settlement(["EV-1"], {"CUST": 150.00})
		set_value.assert_not_called()


class TestCreateIssueMapsOnlyWhatIsOwed(unittest.TestCase):
	"""Review F1 (29 Sep): a kggk_uat merge left Create > Issue mapping the transit remainder,
	so a receipt of 20 g already returned down to 2 g offered 20 g again. The mapper's two rules
	now live in helpers the merge cannot silently drop; these tests pin them."""

	@staticmethod
	def _row(name, qty=20.0, transferred=0.0, factor=1.0):
		return frappe._dict(
			name=name,
			qty=qty,
			transfer_qty=qty * factor,
			transferred_qty=transferred,
			conversion_factor=factor,
		)

	def _helpers(self):
		from jewellery_erpnext.jewellery_erpnext.doc_events import stock_entry as se

		return se._issue_row_qty, se._issue_row_mapped

	def test_a_receipt_row_maps_only_what_it_still_owes(self):
		row_qty, mapped = self._helpers()
		left = {"R1": 2.0}
		self.assertEqual(row_qty(self._row("R1"), left, 3), 2.0)
		self.assertTrue(mapped(self._row("R1"), left, 3))

	def test_an_exhausted_receipt_row_is_not_offered(self):
		row_qty, mapped = self._helpers()
		left = {"R1": 0.0, "R2": 1.5}
		self.assertFalse(mapped(self._row("R1"), left, 3))
		self.assertTrue(mapped(self._row("R2"), left, 3))

	def test_only_the_row_chosen_in_the_preview_is_mapped(self):
		"""The plan zeroes every row but the preview's; an unlisted row is not mapped either."""
		row_qty, mapped = self._helpers()
		left = {"R1": 0.0, "R2": 0.5}
		self.assertEqual(
			[r for r in ("R1", "R2", "R3") if mapped(self._row(r), left, 3)], ["R2"]
		)
		self.assertEqual(row_qty(self._row("R2"), left, 3), 0.5)

	def test_any_other_entry_keeps_the_transit_remainder(self):
		"""Not a Customer Gold receipt (no plan): End Transit maps what is not received yet,
		in the row's own UOM."""
		row_qty, mapped = self._helpers()
		row = self._row("T1", qty=10.0, transferred=4.0)
		self.assertEqual(row_qty(row, None, 3), 6.0)
		self.assertTrue(mapped(row, None, 3))
		done = self._row("T2", qty=10.0, transferred=10.0)
		self.assertFalse(mapped(done, None, 3))
		boxes = self._row("T3", qty=5.0, transferred=4.0, factor=2.0)
		self.assertEqual(row_qty(boxes, None, 3), 3.0)


class TestDeliveriesDrawOnOpenReceiptShares(unittest.TestCase):
	"""Review F2 (29 Sep): receipts A 5 g and B 5 g share batch B1. A raw return takes A's 5 g
	back, so the metal left in B1 is B's. The next 5 g delivered from B1 was split 50/50 by what
	each RECEIVED: A drawn to 7.5 g of 5, B left 2.5 g "open" with no metal behind it."""

	@staticmethod
	def _receipts():
		# Fresh rows per call, as receipts_of_batch returns them -- carrying the received
		# proportion -- since open_receipts_of_batch overwrites each row's share.
		return [
			frappe._dict(name="EV-A", cg_gross_qty_delta=5.0, share=0.5),
			frappe._dict(name="EV-B", cg_gross_qty_delta=5.0, share=0.5),
		]

	def _shares(self, drawn):
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_allocations as cga,
		)

		with patch.object(
			cga, "receipts_of_batch", side_effect=lambda *a: self._receipts()
		), patch.object(cga, "drawn_by_receipt", return_value=drawn):
			return {
				r.name: r.share for r in cga.open_receipts_of_batch("C", "CUST", "B1")
			}

	def test_a_receipt_returned_in_full_takes_none_of_the_next_delivery(self):
		shares = self._shares({"EV-A": frappe._dict(gross=5.0)})
		self.assertEqual(shares, {"EV-A": 0.0, "EV-B": 1.0})

	def test_with_nothing_drawn_the_received_proportions_stand(self):
		self.assertEqual(self._shares({}), {"EV-A": 0.5, "EV-B": 0.5})

	def test_a_partial_return_leaves_the_open_proportions(self):
		"""A returned 2 g: A has 3 open, B 5 -> 3/8 and 5/8."""
		shares = self._shares({"EV-A": frappe._dict(gross=2.0)})
		self.assertAlmostEqual(shares["EV-A"], 3 / 8, places=9)
		self.assertAlmostEqual(shares["EV-B"], 5 / 8, places=9)

	def test_when_nothing_is_open_the_received_proportions_stand(self):
		"""An over-draw the report flags: do not divide by zero, keep the old split."""
		drawn = {"EV-A": frappe._dict(gross=5.0), "EV-B": frappe._dict(gross=6.0)}
		self.assertEqual(self._shares(drawn), {"EV-A": 0.5, "EV-B": 0.5})

	def test_a_batch_of_one_receipt_reads_no_draws(self):
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_allocations as cga,
		)

		one = [frappe._dict(name="EV-A", cg_gross_qty_delta=5.0, share=1.0)]
		with patch.object(cga, "receipts_of_batch", return_value=one), patch.object(
			cga, "drawn_by_receipt"
		) as drawn:
			self.assertEqual(
				cga.open_receipts_of_batch("C", "CUST", "B1")[0].share, 1.0
			)
		drawn.assert_not_called()

	def test_the_delivery_after_the_return_is_allocated_to_b_alone(self):
		"""End to end through the allocation writer: 5 g delivered straight from B1 at
		Rs.7,000/g after A's return -> B 5 g / Rs.35,000, A nothing. With A's 5 g return, each
		receipt has drawn exactly its 5 g and neither is left pending."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_allocations as cga,
		)
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		with patch.object(
			cga, "receipts_of_batch", side_effect=lambda *a: self._receipts()
		), patch.object(
			cga, "drawn_by_receipt", return_value={"EV-A": frappe._dict(gross=5.0)}
		), patch.object(
			cga, "is_allocation_schema_ready", return_value=True
		), patch.object(cgf, "_is_receipt_batch", return_value=True), patch.object(
			cga, "allocate_event"
		) as allocate:
			cgf._allocate_fulfilment(
				frappe._dict(company="C", doctype="Delivery Note"),
				frappe._dict(qty=5.0),
				frappe._dict(name="EV-DN", customer="CUST"),
				"B1",
				None,
				None,
				5.0,
				-35000.0,
				1,
				"INR",
			)
		parts = {
			receipt.name: (qty, amount)
			for receipt, qty, amount in allocate.call_args.args[1]
		}
		self.assertEqual(parts, {"EV-A": (0.0, 0.0), "EV-B": (5.0, 35000.0)})
