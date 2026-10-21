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
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting import customer_gold_components as cgc
from jewellery_erpnext.customer_subcontracting import customer_gold_fulfilment as cgf
from jewellery_erpnext.customer_subcontracting import customer_gold_return as cgr
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	VALUATION_NOMINAL,
)

#: The real ``frappe.get_all``, saved before any test patches it (see test_customer_gold_fulfilment).
_REAL_GET_ALL = frappe.get_all

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
#: Item templates: metal ``M``, stones ``D``; the piece has none.
TEMPLATE = {GOLD_24: "M", GOLD_22: "M", ALLOY: "M", DIAMOND: "D", PIECE: None}

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
			if doctype == "Item" and fieldname == "variant_of":
				return TEMPLATE.get(name)
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


class TestCustomerSuppliedStones(_LineageCase):
	"""A customer who supplies the stones as well as the gold: only the gold carries a liability."""

	graph = {
		"FG-1": [
			_component(
				CUSTOMER_IN_PIECE, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A"
			),
			_component(0.396, DIAMOND, customer=CUSTOMER, source_batch="CUST-DIA"),
		],
		"FG-STONES": [
			_component(0.396, DIAMOND, customer=CUSTOMER, source_batch="CUST-DIA")
		],
	}
	produced = {"FG-1": 1.0, "FG-STONES": 1.0}
	batch_item = {"FG-1": PIECE, "FG-STONES": PIECE, "RCPT-A": GOLD_24}
	#: The stone's source has no booked rate: only a Customer Gold receipt books one.
	booked = {(CUSTOMER, "RCPT-A"): RATE_A}

	def test_the_gold_settles_and_the_customers_stones_do_not_block_it(self):
		"""Before the fix the stone had no booked rate and no purity, so the whole piece went unsettled."""
		share = self.share("FG-1", 1)
		self.assertIsNone(share.reason)
		self.assertAlmostEqual(
			share.value, round(CUSTOMER_IN_PIECE * RATE_A, 2), places=2
		)
		self.assertAlmostEqual(share.fine, 4.986, places=3)

	def test_a_stones_only_share_carries_no_parts(self):
		"""Review P1: the stones-only early return had no ``parts``, so the allocation writer
		iterated None and the Delivery Note submit raised TypeError."""
		share = self.share("FG-STONES", 1)
		self.assertEqual(share.parts, [])
		with patch(
			"jewellery_erpnext.customer_subcontracting.customer_gold_allocations"
			".is_allocation_schema_ready",
			return_value=True,
		), patch(
			"jewellery_erpnext.customer_subcontracting.customer_gold_allocations.allocate_event"
		) as allocate:
			cgf._allocate_fulfilment(
				frappe._dict(company=COMPANY),
				frappe._dict(qty=1),
				frappe._dict(name="EV-1", customer=CUSTOMER),
				"FG-STONES",
				None,
				share,
				1.0,
				0.0,
				1,
				"INR",
			)
		allocate.assert_called_once()
		self.assertEqual(allocate.call_args.args[1], [])

	def test_a_piece_of_only_the_customers_stones_releases_nothing(self):
		share = self.share("FG-STONES", 1)
		self.assertEqual((share.value, share.fine), (0.0, 0.0))


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
			# Receipt allocation is a separate DB writer with its own integration tests; this
			# harness isolates the event and its value.
			patch.object(cgf, "_allocate_fulfilment"),
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

	def _deliver_raw(self, batch, is_receipt):
		self.batch_item[batch] = GOLD_24
		self.serials[batch] = [None]
		row = frappe._dict(
			name="ROW-1", item_code=GOLD_24, qty=4, batch_no=batch, stock_uom="Gram"
		)
		doc = frappe._dict(
			doctype="Delivery Note",
			name="DN-1",
			company=COMPANY,
			is_return=0,
			items=[row],
		)
		with patch.object(cgf, "_is_receipt_batch", return_value=is_receipt):
			cgf.record_fulfilment(doc)

	def test_a_raw_receipt_batch_still_settles_from_the_stock_ledger(self):
		self._deliver_raw("RCPT-A", is_receipt=True)
		self.ledger_value.assert_called_once()
		self.assertEqual(self.events[0].cg_carrying_value_delta, -11.58)

	def test_a_customer_batch_with_no_provenance_never_settles_its_stock_value(self):
		"""No component of the customer's, not their received batch: the stock value may hold
		company material, so nothing is released and the gap is logged."""
		self._deliver_raw("FG-NO-COMPONENTS", is_receipt=False)
		self.ledger_value.assert_not_called()
		self.assertIsNone(self.events[0].cg_carrying_value_delta)
		self.log_error.assert_called_once()


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


class TestSerialOnlyDelivery(_LineageCase):
	"""A serial-only piece has no batch: owner and composition are resolved from the serial.

	DN-26-00039 / KLHGX62F1032: stock left, but ``_batch_owner(None)`` skipped the row and no
	custody event was written. The serial path must release exactly what a batch-tracked piece
	made from the same inputs releases.
	"""

	PIECE_COMPONENTS = [
		_component(
			CUSTOMER_IN_PIECE, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A"
		),
		_component(0.449, ALLOY, source_batch="AL-1"),
		_component(0.396, DIAMOND, source_batch="DIA-1"),
	]

	batch_item = {"RCPT-A": GOLD_24, "RCPT-22": GOLD_22}
	booked = {(CUSTOMER, "RCPT-A"): RATE_A, (CUSTOMER, "RCPT-22"): RATE_B}

	def setUp(self):
		super().setUp()
		self.events = []
		self.log_error = MagicMock()
		#: serial -> owner
		self.owners = {}
		#: serial -> (components, pieces produced)
		self.productions = {}
		#: serial -> the Delivery event it wrote (absent: none was written)
		self.delivered = {}
		self.owner_lookup = MagicMock(side_effect=lambda s, w=None: self.owners.get(s))

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
			patch.object(cgf, "_row_batches", return_value=[]),
			patch.object(
				cgf, "_row_serials", side_effect=lambda row: list(row.serials)
			),
			patch.object(cgf, "_serial_owner", self.owner_lookup),
			patch.object(
				cgf,
				"_serial_delivery_recorded",
				side_effect=lambda doc, s, customer: self.delivered.get(s),
			),
			patch.object(
				cgc,
				"resolve_serial_components",
				side_effect=lambda s: self.productions.get(s, ([], 0.0)),
			),
			patch.object(cgf, "_batch_owner", side_effect=AssertionError("no batch")),
			patch.object(cgf, "_write_event", side_effect=write_event),
			patch.object(cgf, "settle_customer_gold_liability"),
			patch.object(cgf, "_allocate_fulfilment"),
			patch.object(cgf, "_row_carrying_value", MagicMock(return_value=-11.58)),
			patch.object(cgf, "reference_purity", return_value=99.9),
			patch(f"{cgf.__name__}.frappe.get_cached_value", return_value="INR"),
			patch(f"{cgf.__name__}.frappe.log_error", self.log_error),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def _doc(self, serials, qty, customer=CUSTOMER, is_return=0):
		row = frappe._dict(
			name="ROW-1",
			idx=1,
			item_code=PIECE,
			qty=qty,
			batch_no=None,
			serials=serials,
			stock_uom="Nos",
		)
		return frappe._dict(
			doctype="Delivery Note",
			name="DN-1",
			company=COMPANY,
			customer=customer,
			is_return=is_return,
			items=[row],
		)

	def _deliver(self, serials, qty, is_return=0):
		cgf.record_fulfilment(self._doc(serials, qty, is_return=is_return))
		return self.events

	def test_a_serial_only_piece_writes_its_delivery_event(self):
		self.owners["SER-1"] = CUSTOMER
		self.productions["SER-1"] = (self.PIECE_COMPONENTS, 1.0)

		(event,) = self._deliver(["SER-1"], 1)

		self.assertEqual(event.customer, CUSTOMER)
		self.assertEqual(event.serial_no, "SER-1")
		self.assertIsNone(event.batch_no)
		self.assertEqual(event.cg_gross_qty_delta, -1)
		self.assertAlmostEqual(
			event.cg_carrying_value_delta,
			-round(CUSTOMER_IN_PIECE * RATE_A, 2),
			places=2,
		)
		self.assertAlmostEqual(event.cg_fine_gold_delta, -4.986, places=3)

	def test_only_the_customer_material_in_a_mixed_piece_is_released(self):
		"""KLHGX62F1032's shape: company gold and stones, 0.06 g of the customer's finding."""
		self.owners["SER-1"] = CUSTOMER
		self.productions["SER-1"] = (
			[
				_component(0.06, GOLD_22, customer=CUSTOMER, source_batch="RCPT-22"),
				_component(1.888, GOLD_22, source_batch="CO-22"),
				_component(0.483, DIAMOND, source_batch="DIA-1"),
			],
			1.0,
		)

		(event,) = self._deliver(["SER-1"], 1)

		self.assertAlmostEqual(
			event.cg_fine_gold_delta, -round(0.06 * 0.918, 3), places=3
		)
		self.assertAlmostEqual(
			event.cg_carrying_value_delta, -round(0.06 * RATE_B, 2), places=2
		)

	def test_a_company_owned_serial_writes_nothing(self):
		self.productions["SER-1"] = (self.PIECE_COMPONENTS, 1.0)
		self.assertEqual(self._deliver(["SER-1"], 1), [])

	def test_each_serial_on_a_row_is_priced_from_its_own_production(self):
		self.owners.update({"SER-1": CUSTOMER, "SER-2": CUSTOMER})
		self.productions["SER-1"] = (self.PIECE_COMPONENTS, 1.0)
		self.productions["SER-2"] = (
			[_component(2.0, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A")],
			1.0,
		)

		first, second = self._deliver(["SER-1", "SER-2"], 2)

		self.assertEqual((first.serial_no, second.serial_no), ("SER-1", "SER-2"))
		self.assertEqual(
			(first.cg_gross_qty_delta, second.cg_gross_qty_delta), (-1, -1)
		)
		self.assertAlmostEqual(
			first.cg_carrying_value_delta,
			-round(CUSTOMER_IN_PIECE * RATE_A, 2),
			places=2,
		)
		self.assertAlmostEqual(
			second.cg_carrying_value_delta, -round(2.0 * RATE_A, 2), places=2
		)

	def test_a_piece_with_no_production_record_is_logged_never_valued(self):
		"""No composition: the stock value may hold company material, so nothing is released."""
		self.owners["SER-1"] = CUSTOMER

		(event,) = self._deliver(["SER-1"], 1)

		self.assertIsNone(event.cg_carrying_value_delta)
		self.log_error.assert_called_once()
		self.assertIn("serial SER-1", self.log_error.call_args.kwargs["message"])

	def test_a_return_restores_the_same_value(self):
		self.owners["SER-1"] = CUSTOMER
		self.productions["SER-1"] = (self.PIECE_COMPONENTS, 1.0)
		self.delivered["SER-1"] = frappe._dict(
			cg_gross_qty_delta=-1,
			cg_carrying_value_delta=-round(CUSTOMER_IN_PIECE * RATE_A, 2),
		)

		(event,) = self._deliver(["SER-1"], -1, is_return=1)

		self.assertEqual(event.cg_event_kind, cgf.EVENT_DELIVERY_RETURN)
		self.assertAlmostEqual(
			event.cg_carrying_value_delta,
			round(CUSTOMER_IN_PIECE * RATE_A, 2),
			places=2,
		)

	def test_a_return_credits_the_delivered_value_not_todays_receipt_rate(self):
		"""Review Q2: the source batch took more metal at another rate between delivery and
		return, so the booked rate moved. The return must still restore what left."""
		self.owners["SER-1"] = CUSTOMER
		self.productions["SER-1"] = (self.PIECE_COMPONENTS, 1.0)
		self.delivered["SER-1"] = frappe._dict(
			cg_gross_qty_delta=-1,
			cg_carrying_value_delta=-round(CUSTOMER_IN_PIECE * RATE_A, 2),
		)
		self.booked[(CUSTOMER, "RCPT-A")] = RATE_A * 1.5

		(event,) = self._deliver(["SER-1"], -1, is_return=1)

		self.assertAlmostEqual(
			event.cg_carrying_value_delta,
			round(CUSTOMER_IN_PIECE * RATE_A, 2),
			places=2,
		)

	def test_a_return_restores_the_delivered_fine_gold_not_todays_composition(self):
		"""Review F1 (a392ee7a): the source batch was topped up between delivery and return, so
		today's composition says less of the customer's gold is in the piece. The return must
		restore the fine gold that left, as it does the value."""
		self.owners["SER-1"] = CUSTOMER
		self.productions["SER-1"] = (
			[_component(2.0, GOLD_24, customer=CUSTOMER, source_batch="RCPT-A")],
			1.0,
		)
		self.delivered["SER-1"] = frappe._dict(
			cg_gross_qty_delta=-1,
			cg_carrying_value_delta=-round(CUSTOMER_IN_PIECE * RATE_A, 2),
			cg_fine_gold_delta=-4.986,
			cg_fine_measurement_status=cgf.STATUS_KNOWN,
		)

		(event,) = self._deliver(["SER-1"], -1, is_return=1)

		self.assertAlmostEqual(event.cg_fine_gold_delta, 4.986, places=3)
		self.assertAlmostEqual(
			event.cg_carrying_value_delta,
			round(CUSTOMER_IN_PIECE * RATE_A, 2),
			places=2,
		)

	def test_a_return_whose_composition_is_gone_restores_the_original_event(self):
		"""Review F1 (a392ee7a): the production record cannot be rebuilt today, but the delivery
		debited the liability. The return restores the original event rather than nothing."""
		self.owners["SER-1"] = CUSTOMER
		self.delivered["SER-1"] = frappe._dict(
			cg_gross_qty_delta=-1,
			cg_carrying_value_delta=-round(CUSTOMER_IN_PIECE * RATE_A, 2),
			cg_fine_gold_delta=-4.986,
			cg_fine_measurement_status=cgf.STATUS_KNOWN,
		)

		(event,) = self._deliver(["SER-1"], -1, is_return=1)

		self.assertAlmostEqual(event.cg_fine_gold_delta, 4.986, places=3)
		self.assertAlmostEqual(
			event.cg_carrying_value_delta,
			round(CUSTOMER_IN_PIECE * RATE_A, 2),
			places=2,
		)
		self.log_error.assert_not_called()

	def test_a_standalone_return_mirrors_the_allocations_of_the_delivery_it_paired_with(
		self,
	):
		"""Review A1 (8d3267e2): with no ``return_against`` the return still names its original
		event, so the receipts it reopens are the ones that delivery closed, not today's."""
		self.owners["SER-1"] = CUSTOMER
		self.productions["SER-1"] = (self.PIECE_COMPONENTS, 1.0)
		self.delivered["SER-1"] = frappe._dict(
			name="EV-DELIVERY",
			cg_gross_qty_delta=-1,
			cg_carrying_value_delta=-round(CUSTOMER_IN_PIECE * RATE_A, 2),
			cg_fine_gold_delta=-4.986,
			cg_fine_measurement_status=cgf.STATUS_KNOWN,
		)

		self._deliver(["SER-1"], -1, is_return=1)

		share = cgf._allocate_fulfilment.call_args.args[5]
		self.assertEqual(share.original_event, "EV-DELIVERY")

	def test_a_return_of_an_unvalued_delivery_stays_unvalued(self):
		self.owners["SER-1"] = CUSTOMER
		self.productions["SER-1"] = (self.PIECE_COMPONENTS, 1.0)
		self.delivered["SER-1"] = frappe._dict(
			cg_gross_qty_delta=-1, cg_carrying_value_delta=None
		)

		(event,) = self._deliver(["SER-1"], -1, is_return=1)

		self.assertIsNone(event.cg_carrying_value_delta)

	def test_a_serial_owned_by_another_customer_is_blocked(self):
		self.owners["SER-1"] = OTHER
		with self.assertRaises(frappe.ValidationError):
			cgf.validate_customer_gold_entitlement(self._doc(["SER-1"], 1))

	def test_a_serial_owned_by_the_document_customer_passes(self):
		self.owners["SER-1"] = CUSTOMER
		cgf.validate_customer_gold_entitlement(self._doc(["SER-1"], 1))

	def test_a_return_of_a_delivery_that_released_nothing_restores_nothing(self):
		"""Review F1: delivered before serial-only pieces were recorded, so no event exists.
		Restoring a derived value would credit gold that never left the customer's position."""
		self.owners["SER-1"] = CUSTOMER
		self.productions["SER-1"] = (self.PIECE_COMPONENTS, 1.0)

		self.assertEqual(self._deliver(["SER-1"], -1, is_return=1), [])
		self.log_error.assert_called_once()
		self.assertIn("SER-1", self.log_error.call_args.kwargs["message"])

	def test_the_owner_is_read_once_for_both_hooks(self):
		"""Review O1: before_submit and on_submit share the document, so the serial's owner is
		looked up once."""
		self.owners["SER-1"] = CUSTOMER
		self.productions["SER-1"] = (self.PIECE_COMPONENTS, 1.0)
		doc = self._doc(["SER-1"], 1)
		doc.flags = frappe._dict()

		cgf.validate_customer_gold_entitlement(doc)
		cgf.record_fulfilment(doc)

		self.owner_lookup.assert_called_once()
		self.assertEqual(len(self.events), 1)


class TestSerialDeliveryRecorded(unittest.TestCase):
	"""Review F2 (a392ee7a) and V2 (8d3267e2): a standalone return pairs with a delivery still to
	be restored -- never one that was cancelled and reversed, nor one an earlier return already
	brought back."""

	def setUp(self):
		#: Delivery events, newest first, as the ledger query orders them.
		self.deliveries = []
		#: Delivery Return events of the serial.
		self.returns = []
		#: Event names that have a Reversal.
		self.reversed = set()

		def get_all(doctype, filters=None, **kwargs):
			kind = filters.get("cg_event_kind")
			if kind == cgf.EVENT_REVERSAL:
				names = filters["cg_reversal_of"][1]
				return [n for n in names if n in self.reversed]
			if kind == cgf.EVENT_DELIVERY_RETURN:
				return list(self.returns)
			return list(self.deliveries)

		p = patch(f"{cgf.__name__}.frappe.get_all", side_effect=get_all)
		p.start()
		self.addCleanup(p.stop)

	def _recorded(self):
		doc = frappe._dict(doctype="Delivery Note", name="DN-RET", return_against=None)
		return cgf._serial_delivery_recorded(doc, "SER-1", CUSTOMER)

	@staticmethod
	def _event(name, creation):
		return frappe._dict(name=name, creation=creation)

	def test_a_reversed_delivery_is_skipped_for_the_effective_one(self):
		self.deliveries = [self._event("EV-NEW", 2), self._event("EV-OLD", 1)]
		self.reversed = {"EV-NEW"}
		self.assertEqual(self._recorded().name, "EV-OLD")

	def test_only_reversed_deliveries_mean_none_was_recorded(self):
		self.deliveries = [self._event("EV-1", 1)]
		self.reversed = {"EV-1"}
		self.assertIsNone(self._recorded())

	def test_no_delivery_means_none_was_recorded(self):
		self.assertIsNone(self._recorded())

	def test_a_delivery_an_earlier_return_restored_is_not_restored_again(self):
		self.deliveries = [self._event("EV-1", 1)]
		self.returns = [self._event("RET-1", 2)]
		self.assertIsNone(self._recorded())

	def test_a_redelivery_after_a_return_is_the_one_restored(self):
		self.deliveries = [self._event("EV-2", 3), self._event("EV-1", 1)]
		self.returns = [self._event("RET-1", 2)]
		self.assertEqual(self._recorded().name, "EV-2")

	def test_a_cancelled_return_restored_nothing(self):
		self.deliveries = [self._event("EV-1", 1)]
		self.returns = [self._event("RET-1", 2)]
		self.reversed = {"RET-1"}
		self.assertEqual(self._recorded().name, "EV-1")


class TestStandaloneReturnAllocations(unittest.TestCase):
	"""Review A1 (8d3267e2): a return that names no ``return_against`` mirrors the allocations of
	the Delivery event it paired with, instead of deriving them from today's composition."""

	def test_the_paired_event_is_mirrored_without_return_against(self):
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_allocations as cga,
		)

		queries = []

		def get_all(doctype, filters=None, **kwargs):
			queries.append((doctype, filters))
			if doctype == cga.ALLOCATION_DOCTYPE:
				return [
					frappe._dict(
						name="AL-1",
						receipt_event="RCV-1",
						gross_qty=CUSTOMER_IN_PIECE,
						amount=100.0,
						basis="Component",
					)
				]
			if filters == {"name": "EV-DELIVERY"}:
				return [frappe._dict(name="EV-DELIVERY", cg_gross_qty_delta=-1)]
			if filters == {"name": ["in", ["RCV-1"]]}:
				return [frappe._dict(name="RCV-1")]
			raise AssertionError(f"unexpected read: {doctype} {filters}")

		doc = frappe._dict(doctype="Delivery Note", name="DN-RET", return_against=None)
		row = frappe._dict(name="ROW-1")
		with (
			patch(f"{cgf.__name__}.frappe.get_all", side_effect=get_all),
			patch.object(cga, "allocate_event") as allocate,
		):
			restored = cgf._restore_original_allocations(
				doc,
				row,
				frappe._dict(name="EV-RET"),
				"SER-1",
				1.0,
				-100.0,
				"INR",
				original_event="EV-DELIVERY",
			)

		self.assertTrue(restored)
		self.assertEqual(queries[0], (cgf.LEDGER_DOCTYPE, {"name": "EV-DELIVERY"}))
		((receipt, qty, amount),) = allocate.call_args.args[1]
		self.assertEqual(receipt.name, "RCV-1")
		self.assertAlmostEqual(qty, -CUSTOMER_IN_PIECE, places=3)
		self.assertAlmostEqual(amount, -100.0, places=2)

	def test_no_paired_event_and_no_return_against_derives_as_before(self):
		doc = frappe._dict(doctype="Delivery Note", name="DN-RET", return_against=None)
		with patch(f"{cgf.__name__}.frappe.get_all") as get_all:
			self.assertFalse(
				cgf._restore_original_allocations(
					doc, frappe._dict(name="ROW-1"), None, "SER-1", 1.0, None, None
				)
			)
		get_all.assert_not_called()


class TestSerialDeliveryRecordedOnTheDatabase(IntegrationTestCase):
	"""Review F3 (8d3267e2): the return-pairing lookup against real ledger rows.

	The mocked cases above fix the rule; these prove the SQL behind it -- the reversal and
	restored-by-return exclusions -- on the real schema. Rows are inserted raw and rolled back,
	so no policy, document or Journal Entry is needed and the shared CI site is left as found.
	"""

	SERIAL = "CG-LINEAGE-DB-SERIAL"

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.doc = frappe._dict(
			doctype="Delivery Note", name="DN-RET", return_against=None
		)

	def _event(self, name, kind, minute, reversal_of=None):
		frappe.get_doc(
			{
				"doctype": cgf.LEDGER_DOCTYPE,
				"name": name,
				"cg_event_key": f"{name}-KEY",
				"cg_event_kind": kind,
				"company": COMPANY,
				"customer": CUSTOMER,
				"reference_doctype": "Delivery Note",
				"reference_docname": f"DN-{name}",
				"serial_no": self.SERIAL,
				"cg_gross_qty_delta": 1 if kind == cgf.EVENT_DELIVERY_RETURN else -1,
				"cg_reversal_of": reversal_of,
				"creation": f"2026-01-01 10:{minute:02d}:00",
				"modified": f"2026-01-01 10:{minute:02d}:00",
			}
		).db_insert()

	def _recorded(self):
		found = cgf._serial_delivery_recorded(self.doc, self.SERIAL, CUSTOMER)
		return found.name if found else None

	def test_the_effective_delivery_is_found(self):
		self._event("CGT-DEL-1", cgf.EVENT_DELIVERY, 1)
		self.assertEqual(self._recorded(), "CGT-DEL-1")

	def test_a_reversed_delivery_is_not_paired(self):
		self._event("CGT-DEL-1", cgf.EVENT_DELIVERY, 1)
		self._event("CGT-DEL-2", cgf.EVENT_DELIVERY, 2)
		self._event("CGT-REV-2", cgf.EVENT_REVERSAL, 3, reversal_of="CGT-DEL-2")
		self.assertEqual(self._recorded(), "CGT-DEL-1")

	def test_a_delivery_already_returned_is_not_paired_again(self):
		self._event("CGT-DEL-1", cgf.EVENT_DELIVERY, 1)
		self._event("CGT-RET-1", cgf.EVENT_DELIVERY_RETURN, 2)
		self.assertIsNone(self._recorded())

	def test_a_redelivery_after_a_return_is_paired(self):
		self._event("CGT-DEL-1", cgf.EVENT_DELIVERY, 1)
		self._event("CGT-RET-1", cgf.EVENT_DELIVERY_RETURN, 2)
		self._event("CGT-DEL-2", cgf.EVENT_DELIVERY, 3)
		self.assertEqual(self._recorded(), "CGT-DEL-2")

	def test_a_cancelled_return_leaves_its_delivery_to_restore(self):
		self._event("CGT-DEL-1", cgf.EVENT_DELIVERY, 1)
		self._event("CGT-RET-1", cgf.EVENT_DELIVERY_RETURN, 2)
		self._event("CGT-REV-R1", cgf.EVENT_REVERSAL, 3, reversal_of="CGT-RET-1")
		self.assertEqual(self._recorded(), "CGT-DEL-1")


class TestSerialOwnerSource(unittest.TestCase):
	"""Review Q1/Q3: where a serial-only piece's owner comes from."""

	def _owner(self, holding, produced=(None, None), warehouse="FG - WH"):
		with (
			patch.object(cgf, "_serial_holding", return_value=holding) as holding_read,
			patch.object(
				cgf, "_produced_ownership", return_value=produced
			) as produced_read,
		):
			owner = cgf._serial_owner("SER-1", warehouse)
		return owner, holding_read, produced_read

	def test_the_ledger_entry_into_the_warehouse_answers(self):
		owner, _holding, produced = self._owner((cgf.CUSTOMER_GOODS, CUSTOMER))
		self.assertEqual(owner, CUSTOMER)
		produced.assert_not_called()

	def test_an_unstamped_ledger_entry_falls_back_to_the_producing_row(self):
		owner, _holding, produced = self._owner(
			(None, None), produced=(cgf.CUSTOMER_GOODS, CUSTOMER)
		)
		self.assertEqual(owner, CUSTOMER)
		produced.assert_called_once()

	def test_an_explicit_company_holding_is_never_overridden(self):
		owner, _holding, produced = self._owner(
			("Regular Stock", None), produced=(cgf.CUSTOMER_GOODS, CUSTOMER)
		)
		self.assertIsNone(owner)
		produced.assert_not_called()

	def test_no_warehouse_skips_the_ledger_read(self):
		owner, holding, _produced = self._owner(
			None, produced=(cgf.CUSTOMER_GOODS, CUSTOMER), warehouse=None
		)
		self.assertEqual(owner, CUSTOMER)
		holding.assert_not_called()


class TestSerialComposition(unittest.TestCase):
	"""Review Q2: a producing entry with several finished rows splits what it consumed."""

	def _production(self, finished_qtys, serial_row=0):
		finished = [
			frappe._dict(
				name=f"FG-{i}",
				s_warehouse=None,
				t_warehouse="FG - WH",
				is_finished_item=1,
				transfer_qty=qty,
			)
			for i, qty in enumerate(finished_qtys)
		]
		consumed = frappe._dict(
			name="RM-1",
			s_warehouse="WIP - WH",
			t_warehouse=None,
			is_finished_item=0,
			transfer_qty=4.0,
		)

		def get_value(doctype, name, fieldname=None, *args, **kwargs):
			if doctype == "Serial No":
				return frappe._dict(
					reference_doctype="Stock Entry", reference_name="SE-1"
				)
			if doctype == "Stock Entry":
				return frappe._dict(docstatus=1, purpose="Manufacture")
			raise AssertionError(f"unexpected read: {doctype} {name} {fieldname}")

		def get_all(doctype, *args, **kwargs):
			if doctype == "Stock Entry Detail":
				return [consumed, *finished]
			return _REAL_GET_ALL(doctype, *args, **kwargs)

		with (
			patch(f"{cgc.__name__}.frappe.db.get_value", side_effect=get_value),
			patch(f"{cgc.__name__}.frappe.get_all", side_effect=get_all),
			patch.object(cgc, "_finished_row_of", return_value=finished[serial_row]),
		):
			return cgc.serial_production("SER-1")

	def test_one_finished_row_takes_everything_consumed(self):
		production = self._production([1.0])
		self.assertEqual(production.share, 1.0)
		self.assertEqual(production.produced, 1.0)
		self.assertEqual(production.consumed_rows, ["RM-1"])

	def test_several_finished_rows_split_by_quantity(self):
		production = self._production([1.0, 3.0], serial_row=1)
		self.assertEqual(production.finished_row, "FG-1")
		self.assertAlmostEqual(production.share, 0.75)

	def test_components_are_scaled_by_the_rows_share(self):
		drawn = []

		def resolve(batch_no, qty, *args, **kwargs):
			drawn.append((batch_no, qty))
			return [_component(qty, GOLD_24, customer=CUSTOMER, source_batch=batch_no)]

		with (
			patch.object(
				cgc,
				"serial_production",
				return_value=frappe._dict(
					consumed_rows=["RM-1"], share=0.5, produced=1.0
				),
			),
			patch.object(cgc, "_consumed_batches", return_value=[("RCPT-A", -4.0)]),
			patch.object(cgc, "resolve_components", side_effect=resolve),
		):
			components, produced = cgc.resolve_serial_components("SER-1")

		self.assertEqual(drawn, [("RCPT-A", 2.0)])
		self.assertEqual(produced, 1.0)
		self.assertEqual(components[0]["qty"], 2.0)


class TestSerialLookupsRunOnTheDatabase(IntegrationTestCase):
	"""Review O2: every new serial-only query, against the real schema -- read-only.

	The cases above mock the queries; this proves the SQL itself is valid (tables, columns,
	joins) on whatever site the suite runs on, without writing anything.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_an_unknown_serial_has_no_holding_owner_or_production(self):
		serial = "CG-NO-SUCH-SERIAL"
		self.assertIsNone(cgf._serial_holding(serial, "CG-NO-SUCH-WAREHOUSE"))
		self.assertIsNone(cgf._serial_owner(serial, "CG-NO-SUCH-WAREHOUSE"))
		self.assertIsNone(cgc.serial_production(serial))
		self.assertEqual(cgc.resolve_serial_components(serial), ([], 0.0))

	def test_the_bundle_queries_run_for_unknown_rows(self):
		rows = [frappe._dict(name="CG-NO-SUCH-ROW")]
		self.assertIsNone(cgc._finished_row_of("CG-NO-SUCH-SERIAL", rows))
		self.assertEqual(list(cgc._consumed_batches(["CG-NO-SUCH-ROW"])), [])

	def test_a_real_serial_resolves_without_error(self):
		"""Exercises the joins on real rows where the site has any serial made by a Stock Entry."""
		serial = frappe.db.get_value(
			"Serial No",
			{"reference_doctype": "Stock Entry"},
			["name", "warehouse"],
			as_dict=True,
		)
		if not serial:
			self.skipTest("no serial created by a Stock Entry on this site")
		owner = cgf._serial_owner(serial.name, serial.warehouse)
		self.assertTrue(owner is None or isinstance(owner, str))
		components, produced = cgc.resolve_serial_components(serial.name)
		self.assertIsInstance(components, list)
		self.assertGreaterEqual(produced, 0.0)
		if components:
			self.assertGreater(produced, 0.0)

	def test_a_site_without_the_customer_dimension_reads_no_owner(self):
		"""CI's site has no Customer Inventory Dimension, so ``customer`` exists on neither the
		ledger nor Stock Entry Detail. The lookup must fall through to "not customer-owned",
		never fail the delivery on an unknown column."""
		real_has_column = frappe.db.has_column

		def has_column(doctype, column):
			return False if column == "customer" else real_has_column(doctype, column)

		serial = frappe.db.get_value(
			"Serial No",
			{"reference_doctype": "Stock Entry"},
			["name", "warehouse"],
			as_dict=True,
		) or frappe._dict(name="CG-NO-SUCH-SERIAL", warehouse="CG-NO-SUCH-WAREHOUSE")
		with patch.object(frappe.db, "has_column", side_effect=has_column):
			self.assertIsNone(cgf._serial_holding(serial.name, serial.warehouse or "W"))
			self.assertEqual(cgf._produced_ownership(serial.name), (None, None))
			self.assertIsNone(cgf._serial_owner(serial.name, serial.warehouse))
