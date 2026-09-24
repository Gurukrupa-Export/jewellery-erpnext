# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""F2 -- a delivered piece settles the customer gold inside it, by component lineage.

KLHGX62F1119: 5.000 g of the customer's 24KT plus 0.450 g of company alloy became 5.450 g of
22KT; 0.010 g was lost; 5.440 g went into one earring, counted in Nos, with 0.396 ct of the
company's diamond. The customer's gold in the piece is 5.000 / 5.450 x 5.440 = 4.991 g of 24KT.
The delivery released Rs.11.58 of a Rs.7,86,828.62 obligation: settlement restated the components
through the purity of the DELIVERED item, a Nos piece with no Metal Purity, got ``None``, and
fell back to the piece's stock value, which F3 had zeroed.

Pure-logic per the suite convention: no document is created and every DB read is patched. What
needs real documents is in ``test_customer_gold_integration``. The arithmetic -- the unit of a
component, the denominator of a draw, the sign of a return -- is exercised for real.
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from jewellery_erpnext.customer_subcontracting import customer_gold_components as cgc
from jewellery_erpnext.customer_subcontracting import customer_gold_fulfilment as cgf
from jewellery_erpnext.customer_subcontracting import customer_gold_return as cgr
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	VALUATION_NOMINAL,
)

CUSTOMER = "CG-CUST-A"
OTHER = "CG-CUST-B"
COMPANY = "CG Co"

GOLD_24 = "M-G-24KT-99.9"
GOLD_22 = "M-G-22KT-91.8"
ALLOY = "M-AL"
DIAMOND = "D-DIA"
PIECE = "EA02652-001"

PURITY = {GOLD_24: 99.9, GOLD_22: 91.8, ALLOY: 0.0, PIECE: None, DIAMOND: None}
UOM = {GOLD_24: "Gram", GOLD_22: "Gram", ALLOY: "Gram", DIAMOND: "Carat", PIECE: "Nos"}

#: Rupees per gram of 24KT, as a receipt would have booked it.
RATE_A = 7164.83
RATE_B = 6900.00

#: The customer's gold in the KLHGX62F1119 piece, to the precision components are stored at.
CUSTOMER_IN_PIECE = 4.991


def _component(qty, item, customer=None, source_batch=None):
	return frappe._dict(
		item_code=item,
		inventory_type=cgc.CUSTOMER_GOODS if customer else cgc.REGULAR_STOCK,
		customer=customer,
		qty=qty,
		pure_qty=0,
		source_batch=source_batch,
	)


class _LineageCase(unittest.TestCase):
	"""An in-memory batch graph behind the DB readers ``_customer_share`` depends on."""

	#: batch -> recorded component rows
	graph = {}
	#: batch -> quantity its producing Stock Entries put into it
	produced = {}
	#: batch -> item
	batch_item = {}
	#: (customer, source batch) -> booked rate per source gram
	booked = {}

	def setUp(self):
		self.graph = dict(self.graph)
		self.produced = dict(self.produced)
		self.batch_item = dict(self.batch_item)
		self.booked = dict(self.booked)

		def shares_one_unit(batch_no, components):
			uom = UOM.get(self.batch_item.get(batch_no))
			return bool(uom) and all(
				UOM.get(c.get("item_code")) == uom for c in components
			)

		def get_value(doctype, name, fieldname=None, *args, **kwargs):
			if doctype == "Batch" and fieldname == "item":
				return self.batch_item.get(name)
			raise AssertionError(f"unexpected read: {doctype} {name} {fieldname}")

		patches = [
			patch.object(
				cgc,
				"_recorded_components",
				side_effect=lambda b: list(self.graph.get(b, [])),
			),
			patch.object(
				cgc, "produced_qty", side_effect=lambda b: self.produced.get(b, 0.0)
			),
			patch.object(cgc, "_shares_one_unit", side_effect=shares_one_unit),
			patch.object(
				cgr,
				"get_booked_rate",
				side_effect=lambda company, customer, batch: self.booked.get(
					(customer, batch)
				),
			),
			patch.object(
				cgf, "get_purity_percentage", side_effect=lambda item: PURITY.get(item)
			),
			patch(f"{cgf.__name__}.frappe.db.get_value", side_effect=get_value),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def share(self, batch, qty, customer=CUSTOMER, valued=True):
		return cgf._customer_share(
			frappe._dict(company=COMPANY), batch, customer, qty, valued
		)


class TestNosPieceSettlesItsCustomerGold(_LineageCase):
	"""F2-01, F2-05, F2-06: the KLHGX62F1119 shape. A Nos piece is not itself customer gold."""

	graph = {
		"FG-1": [
			_component(
				CUSTOMER_IN_PIECE, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A"
			),
			_component(0.449, ALLOY, source_batch="AL-1"),
			_component(0.396, DIAMOND, source_batch="DIA-1"),
		]
	}
	produced = {"FG-1": 1.0}
	batch_item = {"FG-1": PIECE, "RCPT-A": GOLD_24}
	booked = {(CUSTOMER, "RCPT-A"): RATE_A}

	def test_the_piece_releases_exactly_the_customer_gold_at_its_booked_rate(self):
		share = self.share("FG-1", 1)
		self.assertIsNone(share.reason)
		self.assertAlmostEqual(
			share.value, round(CUSTOMER_IN_PIECE * RATE_A, 2), places=2
		)

	def test_company_alloy_and_diamond_are_not_released(self):
		"""The piece's stock value holds them; the customer's obligation never did."""
		share = self.share("FG-1", 1)
		self.assertLess(share.value, (CUSTOMER_IN_PIECE + 0.449) * RATE_A)

	def test_the_piece_needs_no_metal_purity_of_its_own(self):
		"""The exact failure: the old restatement asked the piece for its purity and got None."""
		self.assertIsNone(PURITY[PIECE])
		self.assertIsNotNone(self.share("FG-1", 1).value)

	def test_the_customer_fine_gold_in_the_piece_is_measured(self):
		"""The delivery used to record 0.000 fine, Unknown. 4.991 g of 99.9% is 4.986 fine."""
		self.assertAlmostEqual(self.share("FG-1", 1).fine, 4.986, places=3)

	def test_another_customer_has_nothing_in_this_piece(self):
		self.assertIsNone(self.share("FG-1", 1, customer=OTHER))

	def test_zero_value_measures_fine_gold_but_values_nothing(self):
		share = self.share("FG-1", 1, valued=False)
		self.assertIsNone(share.value)
		self.assertAlmostEqual(share.fine, 4.986, places=3)


class TestTwoReceiptLayers(_LineageCase):
	"""F2-03 and F2-04: each customer source settles at its own booked rate; company gold at none."""

	graph = {
		"FG-2": [
			_component(3.0, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A"),
			_component(2.0, GOLD_24, customer=CUSTOMER, source_batch="RCPT-B"),
			_component(1.0, GOLD_24, source_batch="CO-GOLD"),
		]
	}
	produced = {"FG-2": 1.0}
	batch_item = {"FG-2": PIECE, "RCPT-A": GOLD_24, "RCPT-B": GOLD_24}
	booked = {(CUSTOMER, "RCPT-A"): RATE_A, (CUSTOMER, "RCPT-B"): RATE_B}

	def test_each_layer_is_released_at_its_own_rate(self):
		self.assertAlmostEqual(
			self.share("FG-2", 1).value, round(3.0 * RATE_A + 2.0 * RATE_B, 2), places=2
		)

	def test_a_layer_with_no_booked_value_releases_nothing_at_all(self):
		"""Never a partial sum: 3 g settled and 2 g silently dropped would look precise."""
		del self.booked[(CUSTOMER, "RCPT-B")]
		share = self.share("FG-2", 1)
		self.assertIsNone(share.value)
		self.assertIn("RCPT-B", share.reason)


class TestInstalmentsUseTheProducedQuantity(_LineageCase):
	"""K45 and F2-02: the denominator is fixed at production, not the live batch balance."""

	graph = {
		"FG-3": [_component(6.0, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A")]
	}
	produced = {"FG-3": 3.0}
	batch_item = {"FG-3": PIECE, "RCPT-A": GOLD_24}
	booked = {(CUSTOMER, "RCPT-A"): RATE_A}

	def test_three_single_deliveries_release_the_whole_value_once(self):
		"""With Batch.batch_qty falling 3 -> 2 -> 1 the three together released 166.7%."""
		released = sum(self.share("FG-3", 1).value for _ in range(3))
		self.assertAlmostEqual(released, round(6.0 * RATE_A, 2), delta=0.02)

	def test_delivering_one_of_two_serials_leaves_the_other_open(self):
		self.produced["FG-3"] = 2.0
		self.assertAlmostEqual(
			self.share("FG-3", 1).value, round(3.0 * RATE_A, 2), places=2
		)

	def test_no_production_record_means_no_denominator_for_a_nos_piece(self):
		"""Grams of gold and carats of stone do not add up to a piece count."""
		self.produced["FG-3"] = 0.0
		share = self.share("FG-3", 1)
		self.assertIsNone(share.value)
		self.assertIsNone(share.fine)
		self.assertTrue(share.reason)


class TestRawBatchPathIsUnchanged(_LineageCase):
	"""A batch with no recorded customer component is not this function's business."""

	graph = {}
	batch_item = {"RCPT-A": GOLD_24}

	def test_a_raw_batch_returns_none_so_the_stock_ledger_settles_it(self):
		self.assertIsNone(self.share("RCPT-A", 4.0))


class TestComponentUnits(_LineageCase):
	"""Component quantities are source grams, whatever the batch they sit in is counted in."""

	booked = {(CUSTOMER, "RCPT-A"): RATE_A}

	def test_a_loss_on_the_way_stays_where_it_happened(self):
		"""5.000 g customer + 0.450 g alloy -> 5.440 g after 0.010 g loss: 4.991 g reaches the piece.

		The lost part stays owed (§5.5, §8.2); a denominator of the produced 5.440 g would
		silently write it off by attributing all 5.000 g downstream.
		"""
		self.graph = {
			"G22-AFTER-LOSS": [
				_component(5.0, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A"),
				_component(0.45, ALLOY, source_batch="AL-1"),
			]
		}
		self.produced = {"G22-AFTER-LOSS": 5.44}
		self.batch_item = {"G22-AFTER-LOSS": GOLD_22, "RCPT-A": GOLD_24}

		out = cgc.resolve_components("G22-AFTER-LOSS", 5.44)
		customer = [c for c in out if c["customer"] == CUSTOMER][0]
		self.assertAlmostEqual(customer["qty"], CUSTOMER_IN_PIECE, places=3)

	def test_unrecorded_alloy_is_not_attributed_to_the_customer(self):
		"""6.000 g of 99.9% converted to 7.950 g of 75.4% with no alloy row consumed.

		Dividing by the component total attributed all 7.950 g to the customer as 24KT -- and
		settled Rs.56,960.40 against a Rs.42,988.98 obligation.
		"""
		self.graph = {
			"G18": [_component(6.0, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A")]
		}
		self.produced = {"G18": 7.95}
		self.batch_item = {"G18": GOLD_22, "RCPT-A": GOLD_24}

		out = cgc.resolve_components("G18", 7.95)
		self.assertAlmostEqual(out[0]["qty"], 6.0, places=3)
		self.assertAlmostEqual(
			self.share("G18", 7.95).value, round(6.0 * RATE_A, 2), places=2
		)

	def test_delivering_a_converted_batch_directly_is_not_restated_by_its_own_purity(
		self,
	):
		"""The old restatement read 6.0 x 75.4 / 99.9 here and under-released by a quarter."""
		self.graph = {
			"G18": [_component(6.0, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A")]
		}
		self.produced = {"G18": 7.95}
		self.batch_item = {"G18": GOLD_22, "RCPT-A": GOLD_24}

		self.assertAlmostEqual(
			self.share("G18", 7.95).value, round(6.0 * RATE_A, 2), places=2
		)

	def test_a_conserving_mix_resolves_exactly_as_before(self):
		"""Nothing lost, nothing unrecorded: basis = total = produced, and a draw sums to itself."""
		self.graph = {
			"G22": [
				_component(5.0, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A"),
				_component(0.45, ALLOY, source_batch="AL-1"),
			]
		}
		self.produced = {"G22": 5.45}
		self.batch_item = {"G22": GOLD_22}

		out = cgc.resolve_components("G22", 2.0)
		self.assertAlmostEqual(sum(c["qty"] for c in out), 2.0, places=3)

	def test_without_a_production_record_a_same_unit_batch_keeps_the_old_basis(self):
		self.graph = {
			"G22": [
				_component(5.0, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A"),
				_component(0.45, ALLOY, source_batch="AL-1"),
			]
		}
		self.produced = {}
		self.batch_item = {"G22": GOLD_22}

		out = cgc.resolve_components("G22", 5.45)
		self.assertAlmostEqual(out[0]["qty"], 5.0, places=3)

	def test_a_piece_drawn_whole_passes_its_components_through(self):
		"""A Nos piece re-used in another entry: drawing 1 of 1 is all of every component."""
		self.graph = {
			"FG-1": [
				_component(
					CUSTOMER_IN_PIECE, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A"
				),
				_component(0.396, DIAMOND, source_batch="DIA-1"),
			]
		}
		self.produced = {"FG-1": 1.0}
		self.batch_item = {"FG-1": PIECE}

		out = cgc.resolve_components("FG-1", 1.0)
		self.assertAlmostEqual(out[0]["qty"], CUSTOMER_IN_PIECE, places=3)
		self.assertAlmostEqual(out[1]["qty"], 0.396, places=3)


class TestAttributionBasis(unittest.TestCase):
	"""The denominator on its own, so a failure says which rule broke."""

	def _basis(self, produced, same_unit, total):
		components = [{"qty": total}]
		with (
			patch.object(cgc, "produced_qty", return_value=produced),
			patch.object(cgc, "_shares_one_unit", return_value=same_unit),
		):
			return cgc.attribution_basis("B", components)

	def test_same_unit_takes_the_larger_of_total_and_produced(self):
		self.assertEqual(self._basis(5.44, True, 5.45), 5.45)
		self.assertEqual(self._basis(7.95, True, 6.0), 7.95)

	def test_different_units_take_the_produced_quantity(self):
		self.assertEqual(self._basis(1.0, False, 5.866), 1.0)

	def test_different_units_without_production_have_no_basis(self):
		self.assertIsNone(self._basis(0.0, False, 5.866))

	def test_same_unit_without_production_falls_back_to_the_total(self):
		self.assertEqual(self._basis(0.0, True, 5.45), 5.45)


class TestDeliveryEvent(_LineageCase):
	"""``record_fulfilment`` end to end, with the writers captured instead of inserted."""

	graph = {
		"FG-1": [
			_component(
				CUSTOMER_IN_PIECE, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A"
			),
			_component(0.449, ALLOY, source_batch="AL-1"),
			_component(0.396, DIAMOND, source_batch="DIA-1"),
		],
		"FG-2": [_component(10.0, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A")],
	}
	produced = {"FG-1": 1.0, "FG-2": 2.0}
	batch_item = {"FG-1": PIECE, "FG-2": PIECE, "RCPT-A": GOLD_24}
	booked = {(CUSTOMER, "RCPT-A"): RATE_A}

	def setUp(self):
		super().setUp()
		self.events = []
		self.ledger_value = MagicMock(return_value=-11.58)
		self.log_error = MagicMock()
		self.serials = {"FG-1": ["KLHGX62F1119"], "FG-2": ["S-1", "S-2"]}

		def write_event(**kwargs):
			self.events.append(frappe._dict(kwargs))
			return f"EV-{len(self.events)}"

		patches = [
			patch.object(cgf, "is_physical_fulfilment", return_value=True),
			patch.object(cgf, "is_customer_gold_enabled", return_value=True),
			patch.object(
				cgf,
				"get_customer_gold_valuation_policy",
				return_value=VALUATION_NOMINAL,
			),
			patch.object(cgf, "_row_batches", side_effect=lambda row: [row.batch_no]),
			patch.object(cgf, "_batch_owner", return_value=CUSTOMER),
			patch.object(
				cgf, "_row_serials", side_effect=lambda row: self.serials[row.batch_no]
			),
			patch.object(cgf, "_write_event", side_effect=write_event),
			patch.object(cgf, "settle_customer_gold_liability"),
			patch.object(cgf, "_row_carrying_value", self.ledger_value),
			patch.object(cgf, "reference_purity", return_value=99.9),
			patch(f"{cgf.__name__}.frappe.get_cached_value", return_value="INR"),
			patch(f"{cgf.__name__}.frappe.log_error", self.log_error),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def _deliver(self, batch, qty, is_return=0):
		row = frappe._dict(
			name="ROW-1", item_code=PIECE, qty=qty, batch_no=batch, stock_uom="Nos"
		)
		doc = frappe._dict(
			doctype="Delivery Note",
			name="DN-1",
			company=COMPANY,
			is_return=is_return,
			items=[row],
		)
		cgf.record_fulfilment(doc)
		return self.events

	def test_the_delivery_event_carries_the_customer_value_not_the_stock_value(self):
		(event,) = self._deliver("FG-1", 1)
		self.assertAlmostEqual(
			event.cg_carrying_value_delta,
			-round(CUSTOMER_IN_PIECE * RATE_A, 2),
			places=2,
		)
		self.ledger_value.assert_not_called()

	def test_the_delivery_event_carries_known_customer_fine_gold(self):
		(event,) = self._deliver("FG-1", 1)
		self.assertAlmostEqual(event.cg_fine_gold_delta, -4.986, places=3)
		self.assertEqual(event.cg_fine_measurement_status, cgf.STATUS_KNOWN)

	def test_a_return_restores_the_same_value_and_fine_gold(self):
		"""F2-07. erpnext builds a return row with a negative qty; the sign comes off the row."""
		(event,) = self._deliver("FG-1", -1, is_return=1)
		self.assertAlmostEqual(
			event.cg_carrying_value_delta,
			round(CUSTOMER_IN_PIECE * RATE_A, 2),
			places=2,
		)
		self.assertAlmostEqual(event.cg_fine_gold_delta, 4.986, places=3)

	def test_an_unvalued_piece_is_logged_and_never_falls_back_to_the_stock_value(self):
		"""Rs.11.58 was the fallback. It must not come back when the booked rate is missing."""
		self.booked.clear()
		(event,) = self._deliver("FG-1", 1)
		self.assertIsNone(event.cg_carrying_value_delta)
		self.ledger_value.assert_not_called()
		self.log_error.assert_called_once()

	def test_a_multi_serial_row_splits_value_and_fine_gold_per_serial(self):
		"""F2-12. Two serials of one batch that produced two: each carries half."""
		events = self._deliver("FG-2", 2)
		self.assertEqual(len(events), 2)
		for event in events:
			self.assertAlmostEqual(
				event.cg_carrying_value_delta, -round(5.0 * RATE_A, 2), places=2
			)
			self.assertAlmostEqual(event.cg_fine_gold_delta, -4.995, places=3)

	def test_a_raw_batch_still_settles_from_the_stock_ledger(self):
		self.batch_item["RCPT-A"] = GOLD_24
		self.serials["RCPT-A"] = [None]
		row = frappe._dict(
			name="ROW-1", item_code=GOLD_24, qty=4, batch_no="RCPT-A", stock_uom="Gram"
		)
		doc = frappe._dict(
			doctype="Delivery Note",
			name="DN-1",
			company=COMPANY,
			is_return=0,
			items=[row],
		)
		cgf.record_fulfilment(doc)
		self.ledger_value.assert_called_once()
		self.assertEqual(self.events[0].cg_carrying_value_delta, -11.58)


class TestFineBasisMatchesQuantityBasis(unittest.TestCase):
	"""Every writer that measures fine gold from a purity must record what ``fine_basis`` records."""

	def test_the_two_agree_on_every_field(self):
		with (
			patch.object(cgf, "get_purity_percentage", return_value=91.8),
			patch.object(cgf, "reference_purity", return_value=99.9),
		):
			for gross in (5.44, -5.44, 0.001, 123.456):
				with self.subTest(gross=gross):
					self.assertEqual(
						cgf.quantity_basis(GOLD_22, gross, COMPANY),
						cgf.fine_basis(gross * 91.8 / 100.0, COMPANY),
					)
