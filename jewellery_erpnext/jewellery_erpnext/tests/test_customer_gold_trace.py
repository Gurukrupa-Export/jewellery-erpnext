# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Pure tests of the customer-gold attribution replay (``customer_gold_trace``).

The replay follows every customer-gold receipt row through transfers, conversions, production
and dispatch. Its rule: inside one (batch, warehouse) holding metal is mixed, so an outflow of q
from Q carries q/Q of every receipt's share; a production pools each ownership lane's inputs and
hands the pool to that lane's outputs weighted by output fine gold (or by quantity when an output
has no purity).

No document is created and nothing touches the database: the engine is fed synthetic movement
dicts, and the loader's reads (``_bundle_entries``, ``_stock_entry_rows``, ``_batch_owners``,
``frappe.get_all`` ...) are patched. Every expected number is worked out by hand in the test
(99.9 % of 20 g is 19.98 fine, and so on), never obtained from the code under test. Every
scenario also checks conservation: ``received - held - disposed == 0`` per receipt.
"""

import itertools
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import frappe

from jewellery_erpnext.customer_subcontracting import customer_gold_trace as cgt
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions import (
	lane_tag,
)

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


SETTINGS_MODULE = "jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings"

C1_LANE = "Customer Goods|C1"
C2_LANE = "Customer Goods|C2"
COMPANY_LANE = "Regular Stock|"

G24 = "M-G-24KT-99.9"
G22 = "M-G-22KT-91.75"
G18 = "M-G-18KT-75"
ALLOY = "M-AL"
DIAMOND = "D-DIA"
PIECE = "FG-RING"

FINE = {"unit": "fine"}
QTY = {"unit": "qty"}

R1 = "SE-R1|d1"
R2 = "SE-R2|d1"

TOL = 1e-9


# -- movement builders --------------------------------------------------------------------------


def _row(
	voucher_no,
	batch,
	warehouse,
	qty,
	*,
	voucher_type="Stock Entry",
	kind=cgt.KIND_MOVEMENT,
	detail=None,
	item=G24,
	purity=99.9,
	lane=None,
	disposition=cgt.DISPOSITION_OTHER,
	receipt_key=None,
	is_return=False,
	is_loss=False,
):
	return {
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"detail_no": detail or f"{voucher_no}-{batch}-{warehouse}",
		"posting": None,
		"batch_no": batch,
		"warehouse": warehouse,
		"item_code": item,
		"qty": qty,
		"purity": purity,
		"kind": kind,
		"lane": lane,
		"disposition": disposition,
		"receipt_key": receipt_key,
		"is_return": is_return,
		"is_loss": is_loss,
	}


def receipt(voucher, detail, batch, warehouse, qty, item=G24, purity=99.9, key=True):
	"""One receipt row. ``key=False`` models a row whose receipt event is not traced."""
	return [
		_row(
			voucher,
			batch,
			warehouse,
			qty,
			kind=cgt.KIND_RECEIPT,
			detail=detail,
			item=item,
			purity=purity,
			receipt_key=cgt.receipt_key(voucher, detail) if key else None,
		)
	]


def transfer(voucher, batch, src, dst, qty, detail="t1", item=G24, purity=99.9):
	return [
		_row(voucher, batch, src, -qty, detail=detail, item=item, purity=purity),
		_row(voucher, batch, dst, qty, detail=detail, item=item, purity=purity),
	]


def leg(batch, warehouse, qty, purity=99.9, lane=C1_LANE, item=G24):
	return {
		"batch": batch,
		"warehouse": warehouse,
		"qty": qty,
		"purity": purity,
		"lane": lane,
		"item": item,
	}


def production(voucher, inputs, outputs, is_loss=False):
	rows = []
	for n, source in enumerate(inputs):
		rows.append(
			_row(
				voucher,
				source["batch"],
				source["warehouse"],
				-source["qty"],
				kind=cgt.KIND_PRODUCTION,
				detail=f"{voucher}-in{n}",
				item=source["item"],
				purity=source["purity"],
				lane=source["lane"],
				is_loss=is_loss,
			)
		)
	for n, target in enumerate(outputs):
		rows.append(
			_row(
				voucher,
				target["batch"],
				target["warehouse"],
				target["qty"],
				kind=cgt.KIND_PRODUCTION,
				detail=f"{voucher}-out{n}",
				item=target["item"],
				purity=target["purity"],
				lane=target["lane"],
				is_loss=is_loss,
			)
		)
	return rows


def outflow(
	voucher,
	batch,
	warehouse,
	qty,
	disposition,
	voucher_type="Stock Entry",
	item=G24,
	purity=99.9,
):
	return [
		_row(
			voucher,
			batch,
			warehouse,
			-qty,
			voucher_type=voucher_type,
			item=item,
			purity=purity,
			disposition=disposition,
		)
	]


def deliver(voucher, batch, warehouse, qty, item=G24, purity=99.9):
	return outflow(
		voucher,
		batch,
		warehouse,
		qty,
		cgt.DISPOSITION_DELIVERED,
		voucher_type="Delivery Note",
		item=item,
		purity=purity,
	)


def inflow(
	voucher,
	batch,
	warehouse,
	qty,
	voucher_type="Stock Entry",
	is_return=False,
	item=G24,
	purity=99.9,
):
	return [
		_row(
			voucher,
			batch,
			warehouse,
			qty,
			voucher_type=voucher_type,
			item=item,
			purity=purity,
			is_return=is_return,
			disposition=cgt.DISPOSITION_DELIVERED
			if voucher_type == "Delivery Note"
			else cgt.DISPOSITION_OTHER,
		)
	]


def delivery_return(voucher, batch, warehouse, qty, item=G24, purity=99.9):
	return inflow(
		voucher,
		batch,
		warehouse,
		qty,
		voucher_type="Delivery Note",
		is_return=True,
		item=item,
		purity=purity,
	)


def stock_alloy(voucher, batch, warehouse, qty):
	"""Company alloy bought in: stock the engine sees arrive, owned by no traced receipt."""
	return inflow(
		voucher,
		batch,
		warehouse,
		qty,
		voucher_type="Purchase Receipt",
		item=ALLOY,
		purity=None,
	)


def run(receipts, *voucher_lists):
	movements = [row for rows in voucher_lists for row in rows]
	return cgt.AttributionReplay(receipts).run(movements)


# -- assertions ---------------------------------------------------------------------------------


class _TraceCase(unittest.TestCase):
	def assertClose(self, actual, expected, msg=None):
		self.assertAlmostEqual(
			actual, expected, delta=TOL * max(1.0, abs(expected)), msg=msg
		)

	def share(self, replay, batch, warehouse, key):
		holding = replay.holdings.get((batch, warehouse))
		return holding.shares.get(key, 0.0) if holding else 0.0

	def qty_of(self, replay, batch, warehouse):
		holding = replay.holdings.get((batch, warehouse))
		return holding.qty if holding else 0.0

	def disposed(self, replay, key, kind):
		return replay.dispositions.get(key, {}).get(kind, 0.0)

	def assertPositions(self, replay, expected):
		"""``expected`` = {(batch, warehouse): {receipt_key: share}}, the whole picture."""
		actual = {(b, w): shares for b, w, _qty, shares in replay.positions()}
		self.assertEqual(
			set(actual), set(expected), "holdings carrying a receipt share"
		)
		for place, shares in expected.items():
			self.assertEqual(set(actual[place]), set(shares), f"receipts in {place}")
			for key, amount in shares.items():
				self.assertClose(actual[place][key], amount, f"{key} in {place}")

	def assertConserved(self, replay, *keys):
		for key in keys:
			self.assertAlmostEqual(
				replay.balance(key), 0.0, delta=TOL, msg=f"balance of {key}"
			)

	def assertNoExceptions(self, replay):
		self.assertEqual(replay.exceptions, [])


# -- lineage scenarios --------------------------------------------------------------------------


class TestReceiptAndTransfers(_TraceCase):
	def test_receipt_with_no_movement_stays_where_it_arrived(self):
		"""LIN-01: 20 g of 99.9 % received, nothing else -> 19.98 fine held in the receipt batch."""
		replay = run({R1: FINE}, receipt("SE-R1", "d1", "B1", "W1", 20))

		self.assertPositions(replay, {("B1", "W1"): {R1: 19.98}})
		self.assertClose(self.qty_of(replay, "B1", "W1"), 20)
		self.assertClose(replay.received[R1], 19.98)
		self.assertClose(replay.held(R1), 19.98)
		for kind in cgt.DISPOSITIONS:
			self.assertEqual(self.disposed(replay, R1, kind), 0.0)
		self.assertEqual([t["action"] for t in replay.trail], ["Receipt"])
		self.assertClose(replay.trail[0]["amount"], 19.98)
		self.assertEqual(replay.trail[0]["to_warehouse"], "W1")
		self.assertEqual(replay.batch_origin["B1"], ("SE-R1", 0, G24))
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1)

	def test_transfers_across_three_warehouses_carry_the_share(self):
		"""LIN-02: W1 -> W2 (all 20 g), then 8 g W2 -> W3; the total stays 19.98 fine."""
		replay = run(
			{R1: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 20),
			transfer("SE-T1", "B1", "W1", "W2", 20),
			transfer("SE-T2", "B1", "W2", "W3", 8),
		)

		# 12 g x 0.999 = 11.988 ; 8 g x 0.999 = 7.992
		self.assertPositions(
			replay, {("B1", "W2"): {R1: 11.988}, ("B1", "W3"): {R1: 7.992}}
		)
		self.assertEqual(self.qty_of(replay, "B1", "W1"), 0.0)
		self.assertClose(self.qty_of(replay, "B1", "W2"), 12)
		self.assertClose(self.qty_of(replay, "B1", "W3"), 8)
		self.assertClose(replay.held(R1), 19.98)
		self.assertEqual(
			[t["action"] for t in replay.trail], ["Receipt", "Transfer", "Transfer"]
		)
		self.assertClose(replay.trail[2]["amount"], 7.992)
		self.assertEqual(
			(replay.trail[2]["from_warehouse"], replay.trail[2]["to_warehouse"]),
			("W2", "W3"),
		)
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1)

	def test_same_batch_split_across_warehouses_is_pro_rata_per_holding(self):
		"""LIN-09: one batch, R1 in W1 and R2 in W2; each holding keeps its own composition."""
		replay = run(
			{R1: FINE, R2: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 10),
			receipt("SE-R2", "d1", "B1", "W2", 10),
			transfer("SE-T1", "B1", "W1", "W3", 5),
			transfer("SE-T2", "B1", "W2", "W3", 5),
			deliver("DN-1", "B1", "W3", 4),
			outflow("SE-RET", "B1", "W1", 1, cgt.DISPOSITION_RETURNED),
		)

		# W3 held 5 g of each (4.995 fine each); 4 of its 10 g left -> 1.998 of each delivered.
		self.assertClose(self.disposed(replay, R1, cgt.DISPOSITION_DELIVERED), 1.998)
		self.assertClose(self.disposed(replay, R2, cgt.DISPOSITION_DELIVERED), 1.998)
		# The 1 g raw return came out of W1, which only ever held R1.
		self.assertClose(self.disposed(replay, R1, cgt.DISPOSITION_RETURNED), 0.999)
		self.assertEqual(self.disposed(replay, R2, cgt.DISPOSITION_RETURNED), 0.0)
		self.assertPositions(
			replay,
			{
				("B1", "W1"): {R1: 3.996},
				("B1", "W2"): {R2: 4.995},
				("B1", "W3"): {R1: 2.997, R2: 2.997},
			},
		)
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1, R2)

	def test_two_rows_of_one_receipt_voucher_stay_distinct(self):
		"""LIN-08: rows d1 (12 g) and d2 (8 g) of SE-R1 land in the same batch and warehouse."""
		d1, d2 = cgt.receipt_key("SE-R1", "d1"), cgt.receipt_key("SE-R1", "d2")
		replay = run(
			{d1: FINE, d2: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 12)
			+ receipt("SE-R1", "d2", "B1", "W1", 8),
			deliver("DN-1", "B1", "W1", 5),
		)

		self.assertEqual((d1, d2), ("SE-R1|d1", "SE-R1|d2"))
		self.assertClose(replay.received[d1], 11.988)
		self.assertClose(replay.received[d2], 7.992)
		# 5 of 20 g delivered = a quarter of each row.
		self.assertClose(self.disposed(replay, d1, cgt.DISPOSITION_DELIVERED), 2.997)
		self.assertClose(self.disposed(replay, d2, cgt.DISPOSITION_DELIVERED), 1.998)
		self.assertPositions(replay, {("B1", "W1"): {d1: 8.991, d2: 5.994}})
		self.assertNoExceptions(replay)
		self.assertConserved(replay, d1, d2)

	def test_untraced_row_of_a_receipt_voucher_arrives_unowned(self):
		"""LIN-08: a row whose receipt event is not traced dilutes the holding but owns nothing."""
		d1, d2 = "SE-R1|d1", "SE-R1|d2"
		replay = run(
			{d1: FINE, d2: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 12)
			+ receipt("SE-R1", "d2", "B1", "W1", 8)
			+ receipt("SE-R1", "d3", "B1", "W1", 5, key=False),
			deliver("DN-1", "B1", "W1", 5),
		)

		self.assertClose(self.qty_of(replay, "B1", "W1"), 20)
		# 5 of 25 g delivered = a fifth: 11.988 / 5 and 7.992 / 5.
		self.assertClose(self.disposed(replay, d1, cgt.DISPOSITION_DELIVERED), 2.3976)
		self.assertClose(self.disposed(replay, d2, cgt.DISPOSITION_DELIVERED), 1.5984)
		self.assertPositions(replay, {("B1", "W1"): {d1: 9.5904, d2: 6.3936}})
		self.assertConserved(replay, d1, d2)


class TestConversions(_TraceCase):
	# F3: 19.98 fine re-cast at 91.75 % is 19.98 / 0.9175 = 21.776566757493188 g.
	G22_QTY = 21.776566757493188
	ALLOY_1 = 1.776566757493188
	# ... and re-cast again at 75 %: 19.98 / 0.75 = 26.64 g.
	G18_QTY = 26.64
	ALLOY_2 = 4.863433242506812

	def _f3(self):
		return [
			receipt("SE-R1", "d1", "B24", "W1", 20),
			stock_alloy("PR-AL1", "ALY1", "W1", self.ALLOY_1),
			production(
				"SE-CONV",
				[
					leg("B24", "W1", 20),
					leg("ALY1", "W1", self.ALLOY_1, None, item=ALLOY),
				],
				[leg("B22", "W1", self.G22_QTY, 91.75, item=G22)],
			),
		]

	def test_whole_batch_conversion_with_alloy_then_split(self):
		"""LIN-03 / F3: 20 g 99.9 % + alloy -> 21.7766 g 91.75 %, then split 50/50 in two."""
		half = self.G22_QTY / 2
		split = transfer(
			"SE-SPLIT", "B22", "W1", "W2", half, detail="s1", item=G22, purity=91.75
		)
		split += transfer(
			"SE-SPLIT", "B22", "W1", "W3", half, detail="s2", item=G22, purity=91.75
		)
		replay = run({R1: FINE}, *self._f3(), split)

		# The receipt batch is empty -- the gold is not "missing", it is in the descendant.
		self.assertEqual(self.qty_of(replay, "B24", "W1"), 0.0)
		self.assertEqual(dict(replay.holdings[("B24", "W1")].shares), {})
		self.assertEqual(self.qty_of(replay, "ALY1", "W1"), 0.0)
		self.assertEqual(replay.batch_origin["B22"], ("SE-CONV", 1, G22))
		self.assertPositions(
			replay, {("B22", "W2"): {R1: 9.99}, ("B22", "W3"): {R1: 9.99}}
		)
		self.assertClose(replay.held(R1), 19.98)
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1)

	def test_descendant_before_split_holds_all_the_fine(self):
		"""LIN-03 / F3: right after the conversion the 22KT child holds exactly 19.98 fine."""
		replay = run({R1: FINE}, *self._f3())
		self.assertPositions(replay, {("B22", "W1"): {R1: 19.98}})
		self.assertClose(self.qty_of(replay, "B22", "W1"), self.G22_QTY)
		self.assertConserved(replay, R1)

	def test_multi_level_conversion_keeps_the_original_receipt(self):
		"""LIN-04: 24KT -> 22KT -> 18KT; the grandchild still belongs to R1, generation 2."""
		replay = run(
			{R1: FINE},
			*self._f3(),
			stock_alloy("PR-AL2", "ALY2", "W1", self.ALLOY_2),
			production(
				"SE-CONV2",
				[
					leg("B22", "W1", self.G22_QTY, 91.75, item=G22),
					leg("ALY2", "W1", self.ALLOY_2, None, item=ALLOY),
				],
				[leg("B18", "W1", self.G18_QTY, 75.0, item=G18)],
			),
			deliver("DN-1", "B18", "W1", 10, item=G18, purity=75.0),
		)

		self.assertEqual(replay.batch_origin["B24"], ("SE-R1", 0, G24))
		self.assertEqual(replay.batch_origin["B22"], ("SE-CONV", 1, G22))
		self.assertEqual(replay.batch_origin["B18"], ("SE-CONV2", 2, G18))
		# 10 g of 18KT is 7.5 fine; 16.64 g stay = 12.48 fine.
		self.assertClose(self.disposed(replay, R1, cgt.DISPOSITION_DELIVERED), 7.5)
		self.assertPositions(replay, {("B18", "W1"): {R1: 12.48}})
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1)

	def test_one_origin_split_into_outputs_weighted_by_fine(self):
		"""LIN-05: 19.98 fine -> 12 g @ 91.6 % (10.992 fine) + 11.984 g @ 75 % (8.988 fine).

		By quantity the first output would get 19.98 x 12 / 23.984 = 9.9967; by fine it gets 10.992.
		"""
		replay = run(
			{R1: FINE},
			receipt("SE-R1", "d1", "B24", "W1", 20),
			stock_alloy("PR-AL1", "ALY1", "W1", 3.984),
			production(
				"SE-CONV",
				[leg("B24", "W1", 20), leg("ALY1", "W1", 3.984, None, item=ALLOY)],
				[
					leg("OA", "W1", 12, 91.6, item=G22),
					leg("OB", "W2", 11.984, 75.0, item=G18),
				],
			),
		)

		self.assertPositions(
			replay, {("OA", "W1"): {R1: 10.992}, ("OB", "W2"): {R1: 8.988}}
		)
		self.assertClose(
			self.share(replay, "OA", "W1", R1) + self.share(replay, "OB", "W2", R1),
			19.98,
		)
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1)

	def test_cycle_back_into_the_ancestor_terminates_and_conserves(self):
		"""LIN-17: A -> B -> back into A. The replay is finite and A is still R1's receipt batch."""
		replay = run(
			{R1: FINE},
			receipt("SE-R1", "d1", "A", "W1", 10),
			production("SE-P1", [leg("A", "W1", 10)], [leg("B", "W1", 10)]),
			production("SE-P2", [leg("B", "W1", 10)], [leg("A", "W1", 10)]),
		)

		self.assertPositions(replay, {("A", "W1"): {R1: 9.99}})
		self.assertEqual(self.qty_of(replay, "B", "W1"), 0.0)
		self.assertEqual(replay.batch_origin["A"], ("SE-R1", 0, G24))
		self.assertEqual(replay.batch_origin["B"], ("SE-P1", 1, G24))
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1)


class TestMergeAndSplit(_TraceCase):
	def _merge_split(self):
		return [
			receipt("SE-R1", "d1", "B1", "W1", 10),
			receipt("SE-R2", "d1", "B2", "W1", 10),
			production(
				"SE-MERGE",
				[leg("B1", "W1", 10), leg("B2", "W1", 10)],
				[leg("M", "W1", 20)],
			),
			production(
				"SE-SPLIT",
				[leg("M", "W1", 20)],
				[leg("X", "W1", 8), leg("Y", "W1", 12)],
			),
		]

	def test_two_receipts_merged_then_split_8_12(self):
		"""LIN-06 / F5: R1 and R2 (10 g each) -> 20 g -> 8 g + 12 g: 3.996 and 5.994 fine each."""
		replay = run({R1: FINE, R2: FINE}, *self._merge_split())

		self.assertPositions(
			replay,
			{
				("X", "W1"): {R1: 3.996, R2: 3.996},
				("Y", "W1"): {R1: 5.994, R2: 5.994},
			},
		)
		self.assertEqual(self.qty_of(replay, "M", "W1"), 0.0)
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1, R2)

	def test_merge_split_transfer_and_raw_return_reconcile(self):
		"""LIN-07: ... then 5 g of Y to W2, and 3 g of it returned raw to the customer."""
		replay = run(
			{R1: FINE, R2: FINE},
			*self._merge_split(),
			transfer("SE-T", "Y", "W1", "W2", 5),
			outflow("SE-RET", "Y", "W2", 3, cgt.DISPOSITION_RETURNED),
		)

		# Y@W2 got 5/12 of 5.994 = 2.4975 of each; 3 of its 5 g returned = 1.4985 of each.
		for key in (R1, R2):
			self.assertClose(
				self.disposed(replay, key, cgt.DISPOSITION_RETURNED), 1.4985
			)
			self.assertEqual(self.disposed(replay, key, cgt.DISPOSITION_DELIVERED), 0.0)
			# 3.996 (X) + 3.4965 (Y@W1) + 0.999 (Y@W2) = 8.4915; + 1.4985 returned = 9.99.
			self.assertClose(replay.held(key), 8.4915)
		self.assertPositions(
			replay,
			{
				("X", "W1"): {R1: 3.996, R2: 3.996},
				("Y", "W1"): {R1: 3.4965, R2: 3.4965},
				("Y", "W2"): {R1: 0.999, R2: 0.999},
			},
		)
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1, R2)


class TestLanes(_TraceCase):
	def test_company_lane_and_empty_customer_lane_get_no_share(self):
		"""LIN-12: company inputs fund company outputs; a lane with no inputs gets nothing."""
		replay = run(
			{R1: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 10),
			inflow("PR-CG", "CG", "W1", 10, voucher_type="Purchase Receipt"),
			production(
				"SE-P",
				[leg("B1", "W1", 10), leg("CG", "W1", 10, lane=COMPANY_LANE)],
				[
					leg("X", "W1", 10),
					leg("Y", "W1", 10, lane=COMPANY_LANE),
					leg("Z", "W1", 1, lane=C2_LANE),
				],
			),
		)

		self.assertPositions(replay, {("X", "W1"): {R1: 9.99}})
		self.assertClose(self.qty_of(replay, "Y", "W1"), 10)
		self.assertClose(self.qty_of(replay, "Z", "W1"), 1)
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1)

	def test_company_alloy_lane_contributes_nothing_and_raises_nothing(self):
		"""LIN-12: alloy consumed in the "Regular Stock|" lane with no output there is silent."""
		replay = run(
			{R1: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 10),
			stock_alloy("PR-AL", "ALY", "W1", 1),
			production(
				"SE-P",
				[
					leg("B1", "W1", 10),
					leg("ALY", "W1", 1, None, lane=COMPANY_LANE, item=ALLOY),
				],
				[leg("X", "W1", 11, 90.818181818181818)],
			),
		)

		self.assertPositions(replay, {("X", "W1"): {R1: 9.99}})
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1)

	def test_lane_key_matches_the_conversion_lane_tag(self):
		"""LIN-12: the owner-derived lane and the stamped Metal Conversions tag compare equal."""
		self.assertEqual(cgt.lane_key("Customer Goods", "C1"), "Customer Goods|C1")
		self.assertEqual(
			cgt.lane_key("Customer Goods", "C1"), lane_tag("Customer Goods", "C1")
		)
		self.assertEqual(cgt.lane_key(None, None), "Regular Stock|")
		self.assertEqual(cgt.lane_key(None, None), lane_tag(None, None))
		self.assertEqual(
			cgt.lane_key("Regular Stock", None), lane_tag("Regular Stock", None)
		)


class TestLoss(_TraceCase):
	def test_process_loss_moves_the_share_into_the_loss_batch(self):
		"""LIN-14 / F6: 0.2 g lost from 10 g -> the loss batch holds 0.1998 fine, still R1's."""
		replay = run(
			{R1: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 10),
			production(
				"SE-PL", [leg("B1", "W1", 0.2)], [leg("LOSS1", "WL", 0.2)], is_loss=True
			),
		)

		self.assertIn("LOSS1", replay.loss_batches)
		self.assertNotIn("B1", replay.loss_batches)
		self.assertPositions(
			replay, {("B1", "W1"): {R1: 9.7902}, ("LOSS1", "WL"): {R1: 0.1998}}
		)
		self.assertEqual(self.disposed(replay, R1, cgt.DISPOSITION_LOSS), 0.0)
		self.assertEqual(cgt.stage_of("WL", "LOSS1", replay), "Loss / Scrap")
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1)

	def _lane_without_output(self, is_loss):
		return run(
			{R1: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 10),
			production(
				"SE-PL",
				[leg("B1", "W1", 0.2)],
				[leg("LOSS1", "WL", 0.2, lane=COMPANY_LANE)],
				is_loss=is_loss,
			),
		)

	def test_customer_lane_with_no_output_under_loss_is_a_loss(self):
		"""LIN-14 / F6: the customer lane is consumed and only the company lane produces."""
		replay = self._lane_without_output(is_loss=True)

		self.assertClose(self.disposed(replay, R1, cgt.DISPOSITION_LOSS), 0.1998)
		self.assertEqual(self.disposed(replay, R1, cgt.DISPOSITION_OTHER), 0.0)
		self.assertPositions(replay, {("B1", "W1"): {R1: 9.7902}})
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1)

	def test_customer_lane_with_no_output_otherwise_is_flagged(self):
		"""LIN-14 / F6: without the loss flag the metal goes to 'other' and is said out loud."""
		replay = self._lane_without_output(is_loss=False)

		self.assertClose(self.disposed(replay, R1, cgt.DISPOSITION_OTHER), 0.1998)
		self.assertEqual(self.disposed(replay, R1, cgt.DISPOSITION_LOSS), 0.0)
		self.assertEqual(len(replay.exceptions), 1)
		exc = replay.exceptions[0]
		self.assertEqual(exc["reason"], cgt.EXC_LANE_WITHOUT_OUTPUT)
		self.assertEqual(
			exc["reason"], "customer lane consumed with no output in the same lane"
		)
		self.assertEqual(exc["lane"], C1_LANE)
		self.assertEqual(set(exc["shares"]), {R1})
		self.assertClose(exc["shares"][R1], 0.1998)
		self.assertConserved(replay, R1)


class TestDeliveryAndReturn(_TraceCase):
	def test_delivery_return_restores_last_known_attribution(self):
		"""Delivery/return: 5 of 10 g delivered, 2 g come back; delivered falls by the 2 g share."""
		replay = run(
			{R1: FINE, R2: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 6),
			receipt("SE-R2", "d1", "B1", "W1", 4),
			deliver("DN-1", "B1", "W1", 5),
			delivery_return("DN-2", "B1", "W1", 2),
		)

		# Holding 10 g = R1 5.994 + R2 3.996 -> per gram 0.5994 / 0.3996.
		# Delivered 5 g: R1 2.997, R2 1.998. Returned 2 g: R1 1.1988, R2 0.7992.
		self.assertClose(self.disposed(replay, R1, cgt.DISPOSITION_DELIVERED), 1.7982)
		self.assertClose(self.disposed(replay, R2, cgt.DISPOSITION_DELIVERED), 1.1988)
		self.assertPositions(replay, {("B1", "W1"): {R1: 4.1958, R2: 2.7972}})
		self.assertClose(self.qty_of(replay, "B1", "W1"), 7)
		self.assertEqual(replay.trail[-1]["action"], "Delivery Return")
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1, R2)

	def test_full_return_brings_delivered_back_to_zero(self):
		"""Delivery/return: everything delivered comes back -> delivered 0, holding as received."""
		replay = run(
			{R1: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 10),
			deliver("DN-1", "B1", "W1", 10),
			delivery_return("DN-2", "B1", "W1", 10),
		)
		self.assertClose(self.disposed(replay, R1, cgt.DISPOSITION_DELIVERED), 0.0)
		self.assertPositions(replay, {("B1", "W1"): {R1: 9.99}})
		self.assertConserved(replay, R1)

	def test_return_with_no_earlier_attribution_is_flagged(self):
		"""Delivery/return: a returned batch the trace never saw leave arrives unowned + exception."""
		replay = run(
			{R1: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 10),
			delivery_return("DN-9", "FGX", "W1", 1, item=PIECE, purity=None),
			inflow("MR-1", "B9", "W1", 3),
		)

		self.assertEqual(len(replay.exceptions), 1)
		exc = replay.exceptions[0]
		self.assertEqual(exc["reason"], cgt.EXC_UNATTRIBUTED_RETURN)
		self.assertEqual((exc["batch_no"], exc["qty"], exc["shares"]), ("FGX", 1, {}))
		self.assertClose(self.qty_of(replay, "FGX", "W1"), 1)
		# A plain (non-return) arrival is unowned too, and is not an exception.
		self.assertClose(self.qty_of(replay, "B9", "W1"), 3)
		self.assertPositions(replay, {("B1", "W1"): {R1: 9.99}})
		self.assertEqual(self.disposed(replay, R1, cgt.DISPOSITION_DELIVERED), 0.0)
		self.assertConserved(replay, R1)

	def test_return_restores_what_was_delivered_not_a_later_take(self):
		"""Delivery/return: the docstring promises the attribution "as it stood when it left".

		Batch B1 holds R1 in W1 and R2 in W2. 2 g are delivered from W1 (pure R1), then 1 g of
		B1 is moved out of W2 (pure R2), then the 2 g delivered come back. Only R1's gold was
		delivered, so the return must restore R1 and must never leave R2 with a negative
		"delivered".
		"""
		replay = run(
			{R1: FINE, R2: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 10),
			receipt("SE-R2", "d1", "B1", "W2", 10),
			deliver("DN-1", "B1", "W1", 2),
			transfer("SE-T", "B1", "W2", "W3", 1),
			delivery_return("DN-2", "B1", "W1", 2),
		)

		self.assertClose(self.disposed(replay, R1, cgt.DISPOSITION_DELIVERED), 0.0)
		self.assertClose(self.disposed(replay, R2, cgt.DISPOSITION_DELIVERED), 0.0)
		self.assertClose(self.share(replay, "B1", "W1", R1), 9.99)
		self.assertClose(self.share(replay, "B1", "W1", R2), 0.0)
		self.assertConserved(replay, R1, R2)


class TestExceptions(_TraceCase):
	def test_outflow_beyond_traced_stock_invents_no_share(self):
		"""Outflow > traced stock: 12 g delivered from a 10 g holding -> only 9.99 fine moves."""
		replay = run(
			{R1: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 10),
			deliver("DN-1", "B1", "W1", 12),
		)

		self.assertClose(self.disposed(replay, R1, cgt.DISPOSITION_DELIVERED), 9.99)
		self.assertEqual(self.qty_of(replay, "B1", "W1"), 0.0)
		self.assertPositions(replay, {})
		self.assertEqual(len(replay.exceptions), 1)
		exc = replay.exceptions[0]
		self.assertEqual(exc["reason"], cgt.EXC_UNKNOWN_OUTFLOW)
		self.assertEqual(exc["voucher_no"], "DN-1")
		self.assertClose(exc["qty"], 2)
		self.assertEqual(exc["shares"], {})
		self.assertConserved(replay, R1)

	def test_outflow_from_an_unseen_holding_moves_nothing(self):
		"""Outflow > traced stock: a batch the replay never stocked has nothing to give."""
		replay = run(
			{R1: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 10),
			deliver("DN-1", "B1", "W9", 1),
		)
		self.assertEqual(self.disposed(replay, R1, cgt.DISPOSITION_DELIVERED), 0.0)
		self.assertEqual(
			[e["reason"] for e in replay.exceptions], [cgt.EXC_UNKNOWN_OUTFLOW]
		)
		self.assertPositions(replay, {("B1", "W1"): {R1: 9.99}})
		self.assertConserved(replay, R1)

	def _reconcile(self, out_qty, in_qty):
		return [
			_row(
				"SR-1",
				"B1",
				"W1",
				-out_qty,
				voucher_type="Stock Reconciliation",
				kind=cgt.KIND_RECONCILIATION,
			),
			_row(
				"SR-1",
				"B1",
				"W1",
				in_qty,
				voucher_type="Stock Reconciliation",
				kind=cgt.KIND_RECONCILIATION,
			),
		]

	def test_reconciliation_with_equal_qty_is_a_revaluation(self):
		"""Reconciliation: out 10 / in 10 changes nothing; the per-gram fraction is captured."""
		replay = run(
			{R1: FINE}, receipt("SE-R1", "d1", "B1", "W1", 10), self._reconcile(10, 10)
		)

		self.assertPositions(replay, {("B1", "W1"): {R1: 9.99}})
		self.assertClose(self.qty_of(replay, "B1", "W1"), 10)
		self.assertEqual(set(replay.revaluation_fractions), {"SR-1"})
		self.assertEqual(set(replay.revaluation_fractions["SR-1"]), {"B1"})
		self.assertClose(replay.revaluation_fractions["SR-1"]["B1"][R1], 0.999)
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1)

	def test_reconciliation_that_reduces_qty_is_flagged(self):
		"""Reconciliation: out 10 / in 8 -> 2 g (1.998 fine) leave as 'other' + exception."""
		replay = run(
			{R1: FINE}, receipt("SE-R1", "d1", "B1", "W1", 10), self._reconcile(10, 8)
		)

		self.assertClose(self.disposed(replay, R1, cgt.DISPOSITION_OTHER), 1.998)
		self.assertPositions(replay, {("B1", "W1"): {R1: 7.992}})
		self.assertEqual(
			[e["reason"] for e in replay.exceptions], [cgt.EXC_RECONCILIATION]
		)
		self.assertClose(replay.exceptions[0]["qty"], -2)
		self.assertClose(replay.exceptions[0]["shares"][R1], 1.998)
		self.assertConserved(replay, R1)

	def test_reconciliation_that_adds_qty_is_flagged_and_unowned(self):
		"""Reconciliation: out 10 / in 12 -> 2 g arrive owned by nobody + exception."""
		replay = run(
			{R1: FINE}, receipt("SE-R1", "d1", "B1", "W1", 10), self._reconcile(10, 12)
		)

		self.assertClose(self.qty_of(replay, "B1", "W1"), 12)
		self.assertPositions(replay, {("B1", "W1"): {R1: 9.99}})
		self.assertEqual(
			[e["reason"] for e in replay.exceptions], [cgt.EXC_RECONCILIATION]
		)
		self.assertClose(replay.exceptions[0]["qty"], 2)
		self.assertConserved(replay, R1)


class TestNonGoldReceipt(_TraceCase):
	def test_stone_receipt_keeps_its_carat_measure_in_a_piece(self):
		"""Non-gold receipt: gold (fine g) and a customer diamond (ct) go into two pieces."""
		rg, rd = "SE-RG|d1", "SE-RD|d1"
		replay = run(
			{rg: FINE, rd: QTY},
			receipt("SE-RG", "d1", "B1", "W1", 10),
			receipt("SE-RD", "d1", "DIA1", "W1", 0.5, item=DIAMOND, purity=None),
			production(
				"SE-FG",
				[leg("B1", "W1", 5), leg("DIA1", "W1", 0.3, None, item=DIAMOND)],
				[
					leg("FG1", "W2", 1, None, item=PIECE),
					leg("FG2", "W2", 1, None, item=PIECE),
				],
			),
			deliver("DN-1", "FG1", "W2", 1, item=PIECE, purity=None),
		)

		self.assertClose(replay.received[rg], 9.99)
		self.assertClose(replay.received[rd], 0.5)
		# Each piece: half of 4.995 fine and half of 0.3 ct.
		self.assertClose(self.disposed(replay, rg, cgt.DISPOSITION_DELIVERED), 2.4975)
		self.assertClose(self.disposed(replay, rd, cgt.DISPOSITION_DELIVERED), 0.15)
		self.assertPositions(
			replay,
			{
				("B1", "W1"): {rg: 4.995},
				("DIA1", "W1"): {rd: 0.2},
				("FG2", "W2"): {rg: 2.4975, rd: 0.15},
			},
		)
		self.assertNoExceptions(replay)
		self.assertConserved(replay, rg, rd)


# -- properties ---------------------------------------------------------------------------------


class TestProperties(_TraceCase):
	def test_prop01_split_conservation(self):
		"""PROP-01: splitting a holding into n warehouses sums back to the original share."""
		cases = [
			(10, [5, 5]),
			(10, [1, 2, 3, 4]),
			(7.5, [0.001, 7.499]),
			(20, [3.3, 3.3, 3.3, 3.3, 3.3, 3.5]),
		]
		for qty, parts in cases:
			with self.subTest(qty=qty, parts=parts):
				movements = [receipt("SE-R1", "d1", "B1", "W0", qty)]
				for n, part in enumerate(parts):
					movements.append(
						transfer("SE-S", "B1", "W0", f"W{n + 1}", part, detail=f"s{n}")
					)
				replay = run({R1: FINE}, *movements)
				for n, part in enumerate(parts):
					self.assertClose(
						self.share(replay, "B1", f"W{n + 1}", R1), part * 0.999
					)
				self.assertClose(replay.held(R1), qty * 0.999)
				self.assertConserved(replay, R1)

	def test_prop02_merge_split_preserves_each_origin(self):
		"""PROP-02: merge R1 (a g) and R2 (b g), split into parts -> each origin keeps its total."""
		for a, b, parts in [
			(10, 10, [8, 12]),
			(3, 7, [1, 9]),
			(2.5, 0.5, [1, 1, 1]),
			(1, 99, [50, 50]),
		]:
			with self.subTest(a=a, b=b, parts=parts):
				total = a + b
				outputs = [leg(f"O{n}", "W1", p) for n, p in enumerate(parts)]
				replay = run(
					{R1: FINE, R2: FINE},
					receipt("SE-R1", "d1", "B1", "W1", a),
					receipt("SE-R2", "d1", "B2", "W1", b),
					production(
						"SE-M",
						[leg("B1", "W1", a), leg("B2", "W1", b)],
						[leg("M", "W1", total)],
					),
					production("SE-S", [leg("M", "W1", total)], outputs),
				)
				for n, p in enumerate(parts):
					self.assertClose(
						self.share(replay, f"O{n}", "W1", R1), a * 0.999 * p / total
					)
					self.assertClose(
						self.share(replay, f"O{n}", "W1", R2), b * 0.999 * p / total
					)
				self.assertClose(replay.held(R1), a * 0.999)
				self.assertClose(replay.held(R2), b * 0.999)
				self.assertConserved(replay, R1, R2)

	def test_prop03_direct_and_two_leg_transfers_agree(self):
		"""PROP-03: W1 -> W3 directly and W1 -> W2 -> W3 (empty W2) end with the same share."""
		for qty, moved in [(10, 10), (10, 4), (3.333, 1.111)]:
			with self.subTest(qty=qty, moved=moved):
				base = receipt("SE-R1", "d1", "B1", "W1", qty)
				direct = run(
					{R1: FINE}, base, transfer("SE-T", "B1", "W1", "W3", moved)
				)
				two_leg = run(
					{R1: FINE},
					base,
					transfer("SE-T1", "B1", "W1", "W2", moved),
					transfer("SE-T2", "B1", "W2", "W3", moved),
				)
				for wh in ("W1", "W3"):
					self.assertClose(
						self.share(two_leg, "B1", wh, R1),
						self.share(direct, "B1", wh, R1),
					)
				self.assertClose(self.share(direct, "B1", "W3", R1), moved * 0.999)
				self.assertClose(self.share(two_leg, "B1", "W2", R1), 0.0)
				self.assertConserved(direct, R1)
				self.assertConserved(two_leg, R1)

	def test_prop04_another_customers_movements_change_nothing(self):
		"""PROP-04: C2's receipts, lanes and deliveries -- even inside the same vouchers -- leave
		R1's positions and dispositions exactly as they were."""
		r1_merge = production(
			"SE-P", [leg("B1", "W1", 10)], [leg("X", "W1", 6), leg("Y", "W2", 4)]
		)
		c2_merge = production(
			"SE-P",
			[leg("C2B", "W1", 5, lane=C2_LANE)],
			[leg("C2X", "W1", 5, lane=C2_LANE)],
		)
		own = [
			receipt("SE-R1", "d1", "B1", "W1", 10),
			r1_merge,
			transfer("SE-T", "X", "W1", "W3", 2),
			deliver("DN-1", "Y", "W2", 1),
		]
		other = [
			receipt("SE-R2", "d1", "C2B", "W1", 5),
			transfer("SE-T9", "C2B", "W1", "W2", 1)
			+ transfer("SE-T9", "C2B", "W2", "W1", 1, detail="t2"),
			deliver("DN-9", "C2X", "W1", 2),
		]
		baseline = run({R1: FINE}, *own)
		interleavings = [
			[own[0], other[0], own[1] + c2_merge, other[1], own[2], other[2], own[3]],
			[other[0], other[1], own[0], own[1] + c2_merge, own[2], own[3], other[2]],
			[own[0], other[0], other[1], c2_merge + own[1], own[2], own[3], other[2]],
		]
		for n, order in enumerate(interleavings):
			with self.subTest(order=n):
				mixed = run({R1: FINE, R2: FINE}, *order)
				self.assertEqual(
					{(b, w) for b, w, _q, s in mixed.positions() if R1 in s},
					{(b, w) for b, w, _q, _s in baseline.positions()},
				)
				for b, w, _q, shares in baseline.positions():
					self.assertClose(self.share(mixed, b, w, R1), shares[R1])
				for kind in cgt.DISPOSITIONS:
					self.assertClose(
						self.disposed(mixed, R1, kind),
						self.disposed(baseline, R1, kind),
					)
				# Hand check of the baseline: Y got 4/10 of 9.99 = 3.996; 1 of its 4 g delivered.
				self.assertClose(
					self.disposed(mixed, R1, cgt.DISPOSITION_DELIVERED), 0.999
				)
				self.assertConserved(mixed, R1, R2)

	def test_prop06_row_order_inside_a_production_does_not_matter(self):
		"""PROP-06: every permutation of a production's rows gives the same per-output shares."""
		prefix = [
			receipt("SE-R1", "d1", "B1", "W1", 10),
			receipt("SE-R2", "d1", "B2", "W1", 5),
			stock_alloy("PR-AL", "ALY", "W1", 1.5),
		]
		rows = production(
			"SE-P",
			[
				leg("B1", "W1", 10),
				leg("B2", "W1", 5),
				leg("ALY", "W1", 1.5, None, item=ALLOY),
			],
			[leg("O1", "W1", 6.6, 90.8), leg("O2", "W2", 9.9, 90.8)],
		)
		# R1 9.99 and R2 4.995 split 6.6 : 9.9 = 0.4 : 0.6.
		expected = {
			("O1", "W1"): {R1: 3.996, R2: 1.998},
			("O2", "W2"): {R1: 5.994, R2: 2.997},
		}
		for order in itertools.permutations(rows):
			replay = run({R1: FINE, R2: FINE}, *prefix, list(order))
			self.assertPositions(replay, expected)
			self.assertConserved(replay, R1, R2)

	def _spread(self):
		return run(
			{R1: FINE, R2: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 10),
			receipt("SE-R2", "d1", "B2", "W1", 6),
			transfer("SE-T1", "B1", "W1", "W2", 3),
			transfer("SE-T2", "B2", "W1", "W3", 2),
			production(
				"SE-M", [leg("B1", "W1", 7), leg("B2", "W1", 4)], [leg("M", "W4", 11)]
			),
			deliver("DN-1", "M", "W4", 1.1),
		)

	def test_prop07_disjoint_warehouse_partitions_sum_to_the_whole(self):
		"""PROP-07: summing shares over any partition of the warehouses gives the total held."""
		replay = self._spread()
		# R1: 9.99 received, M carries 6.993 of it, 1/10 of M delivered -> 0.6993 delivered.
		self.assertClose(replay.held(R1), 9.2907)
		self.assertClose(replay.held(R2), 5.5944)
		partitions = [
			[{"W1", "W2", "W3", "W4"}],
			[{"W1"}, {"W2"}, {"W3"}, {"W4"}],
			[{"W1", "W4"}, {"W2", "W3"}],
			[{"W2"}, {"W1", "W3", "W4"}],
		]
		for partition in partitions:
			with self.subTest(partition=partition):
				for key in (R1, R2):
					parts = [
						sum(
							s.get(key, 0.0)
							for _b, w, _q, s in replay.positions()
							if w in block
						)
						for block in partition
					]
					self.assertClose(sum(parts), replay.held(key))
		self.assertConserved(replay, R1, R2)

	def test_prop08_per_receipt_totals_sum_to_the_combined_view(self):
		"""PROP-08: sum over receipts of held == sum over every holding of all its shares."""
		replay = self._spread()
		combined = sum(sum(s.values()) for _b, _w, _q, s in replay.positions())
		self.assertClose(replay.held(R1) + replay.held(R2), combined)
		# Received 9.99 + 5.994 = 15.984, of which 1.0989 was delivered.
		self.assertClose(combined, 14.8851)
		delivered = sum(
			self.disposed(replay, k, cgt.DISPOSITION_DELIVERED) for k in (R1, R2)
		)
		self.assertClose(delivered, 1.0989)
		self.assertConserved(replay, R1, R2)

	def test_prop10_zero_fine_alloy_adds_gross_not_fine_share(self):
		"""PROP-10: whatever the alloy weight, the descendant carries exactly 9.99 fine."""
		for alloy in (0.0, 0.5, 2.0, 5.0, 40.0):
			with self.subTest(alloy=alloy):
				gross = 10 + alloy
				inputs = [leg("B1", "W1", 10)]
				movements = [receipt("SE-R1", "d1", "B1", "W1", 10)]
				if alloy:
					inputs.append(leg("ALY", "W1", alloy, None, item=ALLOY))
					movements.append(stock_alloy("PR-AL", "ALY", "W1", alloy))
				movements.append(
					production("SE-C", inputs, [leg("C", "W1", gross, 999.0 / gross)])
				)
				replay = run({R1: FINE}, *movements)
				self.assertClose(self.qty_of(replay, "C", "W1"), gross)
				self.assertPositions(replay, {("C", "W1"): {R1: 9.99}})
				self.assertConserved(replay, R1)

	def test_prop11_scrap_recovered_to_stock_keeps_the_total(self):
		"""PROP-11: loss batch -> recovered stock by a normal Repack keeps R1's 9.99 whole."""
		replay = run(
			{R1: FINE},
			receipt("SE-R1", "d1", "B1", "W1", 10),
			production(
				"SE-PL", [leg("B1", "W1", 1)], [leg("SCRAP", "WL", 1)], is_loss=True
			),
			production("SE-REC", [leg("SCRAP", "WL", 1)], [leg("REC", "W1", 1)]),
		)
		self.assertIn("SCRAP", replay.loss_batches)
		self.assertNotIn("REC", replay.loss_batches)
		self.assertPositions(
			replay, {("B1", "W1"): {R1: 8.991}, ("REC", "W1"): {R1: 0.999}}
		)
		self.assertClose(replay.held(R1), 9.99)
		self.assertConserved(replay, R1)

	def test_prop12_one_return_equals_split_returns(self):
		"""PROP-12: returning 2 g at once and 0.5 g + 1.5 g give the same totals."""
		base = receipt("SE-R1", "d1", "B1", "W1", 10)
		once = run(
			{R1: FINE}, base, outflow("SE-RET", "B1", "W1", 2, cgt.DISPOSITION_RETURNED)
		)
		twice = run(
			{R1: FINE},
			base,
			outflow("SE-RET1", "B1", "W1", 0.5, cgt.DISPOSITION_RETURNED),
			outflow("SE-RET2", "B1", "W1", 1.5, cgt.DISPOSITION_RETURNED),
		)
		for replay in (once, twice):
			self.assertClose(self.disposed(replay, R1, cgt.DISPOSITION_RETURNED), 1.998)
			self.assertPositions(replay, {("B1", "W1"): {R1: 7.992}})
			self.assertConserved(replay, R1)

	def test_prop13_many_small_draws_and_a_residual_sum_exactly(self):
		"""PROP-13: 37 deliveries of 0.123 g plus the 5.449 g residual deliver all 9.99 fine."""
		movements = [receipt("SE-R1", "d1", "B1", "W1", 10)]
		for n in range(37):
			movements.append(deliver(f"DN-{n}", "B1", "W1", 0.123))
		movements.append(deliver("DN-last", "B1", "W1", 5.449))
		replay = run({R1: FINE}, *movements)

		self.assertClose(self.disposed(replay, R1, cgt.DISPOSITION_DELIVERED), 9.99)
		self.assertEqual(self.qty_of(replay, "B1", "W1"), 0.0)
		self.assertPositions(replay, {})
		self.assertNoExceptions(replay)
		self.assertConserved(replay, R1)

	def test_prop15_full_reversal_returns_to_baseline(self):
		"""PROP-15: transfer, 1:1 conversion, delivery -- then each undone -- is the baseline."""
		for delivered in (0.0, 1.0, 4.0, 6.0):
			with self.subTest(delivered=delivered):
				movements = [
					receipt("SE-R1", "d1", "B1", "W1", 6),
					receipt("SE-R2", "d1", "B2", "W1", 4),
					transfer("SE-T", "B1", "W1", "W2", 6),
					production("SE-C", [leg("B1", "W2", 6)], [leg("C", "W2", 6)]),
				]
				if delivered:
					movements.append(deliver("DN-1", "C", "W2", delivered))
					movements.append(delivery_return("DN-2", "C", "W2", delivered))
				movements += [
					production("SE-C-REV", [leg("C", "W2", 6)], [leg("B1", "W2", 6)]),
					transfer("SE-T-REV", "B1", "W2", "W1", 6),
				]
				replay = run({R1: FINE, R2: FINE}, *movements)
				self.assertPositions(
					replay, {("B1", "W1"): {R1: 5.994}, ("B2", "W1"): {R2: 3.996}}
				)
				for key in (R1, R2):
					for kind in cgt.DISPOSITIONS:
						self.assertClose(self.disposed(replay, key, kind), 0.0)
				self.assertNoExceptions(replay)
				self.assertConserved(replay, R1, R2)

	def test_prop30_scaling_every_quantity_scales_every_share(self):
		"""PROP-30: multiply every quantity by k > 0 -> every share and disposition scales by k."""

		def scenario(k):
			return run(
				{R1: FINE, R2: FINE},
				receipt("SE-R1", "d1", "B1", "W1", 10 * k),
				receipt("SE-R2", "d1", "B2", "W1", 10 * k),
				production(
					"SE-M",
					[leg("B1", "W1", 10 * k), leg("B2", "W1", 10 * k)],
					[leg("M", "W1", 20 * k)],
				),
				production(
					"SE-S",
					[leg("M", "W1", 20 * k)],
					[leg("X", "W1", 8 * k), leg("Y", "W1", 12 * k)],
				),
				transfer("SE-T", "Y", "W1", "W2", 5 * k),
				deliver("DN-1", "Y", "W2", 3 * k),
			)

		# At k = 1: X 3.996 each, Y@W1 3.4965 each, Y@W2 0.999 each, delivered 1.4985 each.
		unit = {("X", "W1"): 3.996, ("Y", "W1"): 3.4965, ("Y", "W2"): 0.999}
		for k in (0.5, 1.0, 2.0, 3.7, 1000.0):
			with self.subTest(k=k):
				replay = scenario(k)
				self.assertPositions(
					replay, {place: {R1: v * k, R2: v * k} for place, v in unit.items()}
				)
				for key in (R1, R2):
					self.assertClose(
						self.disposed(replay, key, cgt.DISPOSITION_DELIVERED),
						1.4985 * k,
					)
				self.assertConserved(replay, R1, R2)


# -- loader (every DB read patched) -------------------------------------------------------------


def _d(**kwargs):
	return frappe._dict(kwargs)


class TestDiscoverScope(unittest.TestCase):
	"""Receipt batch R0 -> SE-CONV1 (Repack) -> CH1 -> SE-MFG1 (Manufacture) -> FG1, and a
	cycle SE-CONV2: FG1 -> back into R0. SE-CONV1 also converts C2's batch OB into OTH1 and
	SE-MFG1 also turns company gold CO1 into company scrap SCR1; SE-XFER is a plain transfer."""

	ENTRIES = [
		# voucher, detail, batch, qty
		("SE-CONV1", "c1", "R0", -20),
		("SE-CONV1", "c2", "ALY", -1.78),
		("SE-CONV1", "c3", "CH1", 21.78),
		("SE-CONV1", "c4", "OB", -10),
		("SE-CONV1", "c5", "OTH1", 10),
		("SE-XFER", "x1", "CH1", -5),
		("SE-XFER", "x1", "CH1", 5),
		("SE-XFER2", "y1", "XT", 5),
		("SE-MFG1", "m1", "CH1", -5),
		("SE-MFG1", "m2", "FG1", 1),
		("SE-MFG1", "m3", "CO1", -2),
		("SE-MFG1", "m4", "SCR1", 0.1),
		("SE-CONV2", "k1", "FG1", -1),
		("SE-CONV2", "k2", "R0", 1),
	]
	PURPOSE = {
		"SE-CONV1": "Repack",
		"SE-XFER": "Material Transfer",
		"SE-XFER2": "Material Transfer",
		"SE-MFG1": "Manufacture",
		"SE-CONV2": "Repack",
	}
	SED = {
		# explicit Metal Conversions lane tags
		"c1": _d(parent="SE-CONV1", custom_conversion_lane=C1_LANE),
		"c2": _d(parent="SE-CONV1", custom_conversion_lane=C1_LANE),
		"c3": _d(parent="SE-CONV1", custom_conversion_lane=C1_LANE),
		"c4": _d(parent="SE-CONV1", custom_conversion_lane=C2_LANE),
		"c5": _d(parent="SE-CONV1", custom_conversion_lane=C2_LANE),
		# the row's own ownership
		"m1": _d(parent="SE-MFG1", inventory_type="Customer Goods", customer="C1"),
		"m3": _d(parent="SE-MFG1", inventory_type="Regular Stock", customer="C1"),
		"m4": _d(parent="SE-MFG1", inventory_type="Regular Stock"),
		# m2, k1, k2: no stamp -> the batch master's ownership
	}
	OWNERS = {
		"R0": _d(
			name="R0", custom_inventory_type="Customer Goods", custom_customer="C1"
		),
		"FG1": _d(
			name="FG1", custom_inventory_type="Customer Goods", custom_customer="C1"
		),
		"CH1": _d(
			name="CH1", custom_inventory_type="Customer Goods", custom_customer="C1"
		),
	}

	def setUp(self):
		self.calls = []

	def _entries(self):
		return [
			_d(
				voucher_type="Stock Entry",
				voucher_no=v,
				voucher_detail_no=d,
				batch_no=b,
				warehouse="W1",
				qty=q,
			)
			for v, d, b, q in self.ENTRIES
		]

	def fake_bundle_entries(
		self, batches, to_datetime=None, outward_only=False, voucher_nos=None
	):
		self.calls.append(
			(batches and set(batches), voucher_nos and set(voucher_nos), outward_only)
		)
		rows = self._entries()
		if batches is not None:
			rows = [r for r in rows if r.batch_no in batches]
		if voucher_nos is not None:
			rows = [r for r in rows if r.voucher_no in voucher_nos]
		if outward_only:
			rows = [r for r in rows if r.qty < 0]
		return rows

	def fake_get_all(self, doctype, filters=None, pluck=None, **kwargs):
		self.assertEqual(doctype, "Stock Entry")
		names = filters["name"][1]
		purposes = filters["purpose"][1]
		self.assertEqual(filters["docstatus"], 1)
		return [n for n in names if self.PURPOSE[n] in purposes]

	def fake_sed(self, vouchers):
		return {k: v for k, v in self.SED.items() if v.parent in vouchers}

	def fake_owners(self, batches):
		return {k: v for k, v in self.OWNERS.items() if k in batches}

	def _discover(self, roots, **kwargs):
		with (
			patch.object(cgt, "_bundle_entries", side_effect=self.fake_bundle_entries),
			patch.object(cgt, "_stock_entry_rows", side_effect=self.fake_sed),
			patch.object(cgt, "_batch_owners", side_effect=self.fake_owners),
			patch.object(
				cgt.frappe,
				"get_all",
				side_effect=_owning({"Stock Entry"}, self.fake_get_all, _REAL_GET_ALL),
			),
		):
			return cgt.discover_scope(roots, **kwargs)

	def test_scope_follows_descendants_in_the_customers_lane_only(self):
		"""discover_scope: R0 -> CH1 -> FG1; C2's OTH1, company scrap and alloy stay out."""
		scope = self._discover({"R0", None}, max_rounds=1000)

		self.assertEqual(scope, {"R0", "CH1", "FG1"})
		for outsider in ("OTH1", "OB", "SCR1", "CO1", "ALY", "XT"):
			self.assertNotIn(outsider, scope)

	def test_cycle_terminates_at_a_fixed_point(self):
		"""discover_scope / LIN-17: FG1 -> back into R0 does not loop; each voucher expands once."""
		self._discover({"R0"}, max_rounds=1000)

		# Three rounds of (outward lookup + producing-voucher expansion), then a fixed point --
		# nowhere near the 1000-round safety net.
		self.assertEqual(len(self.calls), 6)
		expanded = [
			voucher_nos for _b, voucher_nos, outward in self.calls if not outward
		]
		self.assertEqual(expanded, [{"SE-CONV1"}, {"SE-MFG1"}, {"SE-CONV2"}])

	def test_scope_from_a_leaf_is_the_leaf(self):
		"""discover_scope: a batch never consumed by a production is its own whole scope."""
		self.assertEqual(self._discover({"XT"}), {"XT"})


class TestLoader(unittest.TestCase):
	def test_load_movements_classifies_and_orders_vouchers(self):
		"""Loader: kinds, dispositions, lanes and posting order, then a conserving replay."""

		def t(minute, second=0):
			return datetime(2026, 9, 1, 10, minute, second)

		def e(
			voucher_type, voucher, detail, batch, wh, qty, item, posting, creation=None
		):
			return _d(
				voucher_type=voucher_type,
				voucher_no=voucher,
				voucher_detail_no=detail,
				posting_datetime=posting,
				creation=creation or posting,
				item_code=item,
				batch_no=batch,
				warehouse=wh,
				qty=qty,
				stock_value_difference=0,
			)

		se = "Stock Entry"
		entries = [
			e("Delivery Note", "DN-2", "n2", "CH", "W1", 1, G22, t(7)),
			e(se, "SE-P", "p2", "CH", "W1", 16, G22, t(2), t(2, 30)),
			e(se, "SE-P", "p1", "B0", "W1", -15, G24, t(2), t(2, 30)),
			e(se, "SE-X", "x1", "B0", "W2", 5, G24, t(2), t(2, 10)),
			e(se, "SE-X", "x1", "B0", "W1", -5, G24, t(2), t(2, 10)),
			e(se, "SE-R", "d1", "B0", "W1", 20, G24, t(1)),
			e(se, "SE-RET", "r1", "B0", "W2", -2, G24, t(4)),
			e(se, "SE-PL", "l1", "B0", "W2", -0.5, G24, t(5)),
			e("Delivery Note", "DN-1", "n1", "CH", "W1", -4, G22, t(6)),
			e("Stock Reconciliation", "SR-1", "s1", "B0", "W2", 2.5, G24, t(8)),
			e("Stock Reconciliation", "SR-1", "s1", "B0", "W2", -2.5, G24, t(8)),
			e(se, "SE-LP", "m2", "LS", "WL", 0.1, G22, t(9)),
			e(se, "SE-LP", "m1", "CH", "W1", -0.1, G22, t(9)),
		]
		se_info = {
			"SE-R": ("Material Receipt", "Customer Goods Received"),
			"SE-X": ("Material Transfer", "Material Transfer"),
			"SE-P": ("Repack", "Metal Conversion"),
			"SE-RET": ("Material Issue", "CG Return"),
			"SE-PL": ("Material Issue", cgt.PROCESS_LOSS_SE_TYPE),
			"SE-LP": ("Repack", cgt.PROCESS_LOSS_SE_TYPE),
		}
		sed = {
			"p1": _d(custom_conversion_lane=C1_LANE),
			"p2": _d(custom_conversion_lane=C1_LANE),
			"m1": _d(inventory_type="Customer Goods", customer="C1"),
		}
		owners = {
			"LS": _d(custom_inventory_type="Customer Goods", custom_customer="C1")
		}

		def fake_get_all(doctype, filters=None, fields=None, **kwargs):
			names = set(filters["name"][1])
			if doctype == "Stock Entry":
				return [
					_d(name=n, purpose=p, stock_entry_type=s)
					for n, (p, s) in se_info.items()
					if n in names
				]
			if doctype == "Delivery Note":
				return [
					_d(name=n, is_return=int(n == "DN-2"))
					for n in ("DN-1", "DN-2")
					if n in names
				]
			raise AssertionError(doctype)

		receipts = [_d(reference_docname="SE-R", cg_source_row="d1", key="SE-R|d1")]
		with (
			patch.object(cgt, "_bundle_entries", return_value=entries),
			patch.object(cgt, "_stock_entry_rows", return_value=sed),
			patch.object(cgt, "_batch_owners", return_value=owners),
			patch.object(
				cgt, "get_purity_percentage", side_effect={G24: 99.9, G22: 91.75}.get
			),
			patch.object(
				cgt.frappe,
				"get_all",
				side_effect=_owning(
					{"Stock Entry", "Delivery Note", "Sales Invoice"},
					fake_get_all,
					_REAL_GET_ALL,
				),
			),
			patch(
				f"{SETTINGS_MODULE}.get_customer_gold_settings",
				return_value=_d(customer_gold_return_stock_entry_type="CG Return"),
			),
		):
			movements = cgt.load_movements({"B0", "CH", "LS"}, receipts)

		order = [(m["voucher_no"], m["batch_no"], m["qty"]) for m in movements]
		self.assertEqual(
			order,
			[
				("SE-R", "B0", 20),
				("SE-X", "B0", -5),
				("SE-X", "B0", 5),
				("SE-P", "B0", -15),
				("SE-P", "CH", 16),
				("SE-RET", "B0", -2),
				("SE-PL", "B0", -0.5),
				("DN-1", "CH", -4),
				("DN-2", "CH", 1),
				("SR-1", "B0", -2.5),
				("SR-1", "B0", 2.5),
				("SE-LP", "CH", -0.1),
				("SE-LP", "LS", 0.1),
			],
		)
		by = {(m["voucher_no"], m["qty"]): m for m in movements}
		self.assertEqual(by[("SE-R", 20)]["kind"], cgt.KIND_RECEIPT)
		self.assertEqual(by[("SE-R", 20)]["receipt_key"], "SE-R|d1")
		self.assertEqual(by[("SE-R", 20)]["purity"], 99.9)
		self.assertEqual(by[("SE-X", 5)]["kind"], cgt.KIND_MOVEMENT)
		self.assertEqual(by[("SE-P", 16)]["kind"], cgt.KIND_PRODUCTION)
		self.assertEqual(by[("SE-P", 16)]["lane"], C1_LANE)
		self.assertFalse(by[("SE-P", 16)]["is_loss"])
		self.assertEqual(by[("SE-RET", -2)]["disposition"], cgt.DISPOSITION_RETURNED)
		self.assertEqual(by[("SE-PL", -0.5)]["disposition"], cgt.DISPOSITION_LOSS)
		self.assertEqual(by[("DN-1", -4)]["disposition"], cgt.DISPOSITION_DELIVERED)
		self.assertFalse(by[("DN-1", -4)]["is_return"])
		self.assertTrue(by[("DN-2", 1)]["is_return"])
		self.assertEqual(by[("SR-1", 2.5)]["kind"], cgt.KIND_RECONCILIATION)
		self.assertEqual(by[("SE-LP", 0.1)]["kind"], cgt.KIND_PRODUCTION)
		self.assertTrue(by[("SE-LP", 0.1)]["is_loss"])
		self.assertEqual(by[("SE-LP", 0.1)]["lane"], C1_LANE)
		self.assertEqual(by[("SE-LP", -0.1)]["lane"], C1_LANE)

		replay = cgt.AttributionReplay({"SE-R|d1": FINE}).run(movements)
		key = "SE-R|d1"
		self.assertEqual(replay.exceptions, [])
		# 20 g x 0.999 = 19.98; 2 g returned from W2 = 1.998; 0.5 g lost there = 0.4995.
		self.assertAlmostEqual(replay.received[key], 19.98, delta=TOL)
		self.assertAlmostEqual(
			replay.dispositions[key][cgt.DISPOSITION_RETURNED], 1.998, delta=TOL
		)
		self.assertAlmostEqual(
			replay.dispositions[key][cgt.DISPOSITION_LOSS], 0.4995, delta=TOL
		)
		# CH got 15 g = 14.985 fine; 4 of 16 g delivered (3.74625), 1 g back (0.9365625).
		self.assertAlmostEqual(
			replay.dispositions[key][cgt.DISPOSITION_DELIVERED], 2.8096875, delta=TOL
		)
		self.assertIn("LS", replay.loss_batches)
		self.assertAlmostEqual(replay.balance(key), 0.0, delta=TOL)

	def test_load_receipts_skips_reversed_and_cancelled(self):
		"""Loader: a reversed Receipt event and one on a cancelled entry are not traced."""
		events = [
			_d(
				name="E1",
				customer="C1",
				reference_docname="SE-R1",
				cg_source_row="d1",
				item_code=G24,
				batch_no="B1",
			),
			_d(
				name="E2",
				customer="C1",
				reference_docname="SE-R1",
				cg_source_row="d2",
				item_code=DIAMOND,
				batch_no="D1",
			),
			_d(
				name="E3",
				customer="C1",
				reference_docname="SE-R2",
				cg_source_row="d1",
				item_code=G24,
				batch_no="B2",
			),
			_d(
				name="E4",
				customer="C1",
				reference_docname="SE-R3",
				cg_source_row="d1",
				item_code=G24,
				batch_no="B3",
			),
		]
		docstatus = {"SE-R1": 1, "SE-R2": 1, "SE-R3": 2}

		def fake_get_all(doctype, filters=None, fields=None, pluck=None, **kwargs):
			if (
				doctype == cgt.LEDGER_DOCTYPE
				and filters["cg_event_kind"] == cgt.EVENT_RECEIPT
			):
				self.assertEqual(filters["company"], "CG Co")
				return events
			if (
				doctype == cgt.LEDGER_DOCTYPE
				and filters["cg_event_kind"] == cgt.EVENT_REVERSAL
			):
				return ["E3"]
			if doctype == "Stock Entry":
				return [
					_d(name=n, docstatus=s, posting_date="2026-09-01")
					for n, s in docstatus.items()
					if n in filters["name"][1]
				]
			raise AssertionError(doctype)

		fake_db = MagicMock()
		fake_db.has_column.return_value = False
		with (
			patch.object(cgt.frappe, "get_all", side_effect=fake_get_all),
			patch.object(cgt.frappe, "db", fake_db),
			patch.object(
				cgt, "get_purity_percentage", side_effect={G24: 99.9, DIAMOND: None}.get
			),
		):
			result = cgt.load_receipts("CG Co")

		self.assertEqual([r.key for r in result], ["SE-R1|d1", "SE-R1|d2"])
		self.assertEqual([r.unit for r in result], ["fine", "qty"])
		self.assertEqual([r.purity for r in result], [99.9, None])

	def test_trace_replays_every_receipt_of_the_asked_receipts_customer(self):
		"""Loader: asking for one receipt replays all of that customer's receipts (they may share
		a batch), and none of another customer's."""
		receipts = [
			_d(
				customer="C1",
				reference_docname="SE-R1",
				batch_no="B1",
				key="SE-R1|d1",
				unit="fine",
			),
			_d(
				customer="C1",
				reference_docname="SE-R2",
				batch_no="B1",
				key="SE-R2|d1",
				unit="fine",
			),
			_d(
				customer="C2",
				reference_docname="SE-R3",
				batch_no="B3",
				key="SE-R3|d1",
				unit="fine",
			),
		]
		movements = receipt("SE-R1", "d1", "B1", "W1", 10) + receipt(
			"SE-R2", "d1", "B1", "W1", 5
		)
		with (
			patch.object(cgt, "load_receipts", return_value=receipts) as load,
			patch.object(cgt, "discover_scope", return_value={"B1"}) as discover,
			patch.object(cgt, "load_movements", return_value=movements),
		):
			got, replay, scope = cgt.trace("CG Co", receipt="SE-R1")

		load.assert_called_once_with("CG Co", customer=None)
		discover.assert_called_once_with({"B1"})
		self.assertEqual([r.key for r in got], ["SE-R1|d1", "SE-R2|d1"])
		self.assertEqual(scope, {"B1"})
		self.assertAlmostEqual(replay.held("SE-R1|d1"), 9.99, delta=TOL)
		self.assertAlmostEqual(replay.held("SE-R2|d1"), 4.995, delta=TOL)
		self.assertNotIn("SE-R3|d1", replay.receipts)

	def test_trace_with_no_receipts_is_empty(self):
		"""Loader: nothing received -> nothing to replay, and no scope lookup at all."""
		with (
			patch.object(cgt, "load_receipts", return_value=[]),
			patch.object(cgt, "discover_scope") as discover,
		):
			got, replay, scope = cgt.trace("CG Co", customer="C9")
		self.assertEqual((got, scope, replay.positions()), ([], set(), []))
		discover.assert_not_called()
