# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""C12 -- the fulfilment predicate, the event identity, the serial resolution, and which
Reversals count in the customer's position.

Pure-logic per the suite convention: no document is created and every DB read is patched.
The parts that genuinely need documents -- real SLEs, a real Delivery Note, the UNIQUE
constraint actually firing -- are in ``test_customer_gold_integration`` instead, because a
mock cannot prove a database constraint.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting import customer_gold_fulfilment as cgf

MOD = "jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment"

#: The real ``frappe.get_all``, saved before any test patches it. A fake answers only the ledger
#: and hands every other read to the real one (commit 5632d9f4): ``flt(x, precision)`` looks up
#: the rounding method through System Settings, and a fake that mis-answered that read would make
#: ``flt`` swallow the error and return 0 -- but only while the cache is cold.
_REAL_GET_ALL = frappe.get_all


def _owning(doctypes, fake, real):
	"""A side_effect that routes ``doctypes`` to ``fake`` and everything else to ``real``."""

	def side_effect(doctype, *args, **kwargs):
		if doctype in doctypes:
			return fake(doctype, *args, **kwargs)
		return real(doctype, *args, **kwargs)

	return side_effect


def _doc(doctype, **kw):
	d = frappe._dict(
		doctype=doctype, name=f"{doctype[:3].upper()}-1", company="GE", items=[]
	)
	d.update(kw)
	return d


class TestPhysicalFulfilmentPredicate(IntegrationTestCase):
	"""Which documents actually move metal. This is the whole gate for C12."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_delivery_note_always_counts(self):
		"""There is no ``update_stock`` field on a DN -- it always posts SLEs."""
		self.assertTrue(cgf.is_physical_fulfilment(_doc("Delivery Note")))

	def test_delivery_note_counts_even_without_update_stock_set(self):
		self.assertTrue(
			cgf.is_physical_fulfilment(_doc("Delivery Note", update_stock=0))
		)

	def test_sales_invoice_with_update_stock_counts(self):
		self.assertTrue(
			cgf.is_physical_fulfilment(_doc("Sales Invoice", update_stock=1))
		)

	def test_sales_invoice_without_update_stock_does_not(self):
		"""A bill moves no metal. This is also what makes CG-T145 fall out for free:
		a credit-only return has update_stock = 0, so no custody is restored."""
		self.assertFalse(
			cgf.is_physical_fulfilment(_doc("Sales Invoice", update_stock=0))
		)

	def test_sales_invoice_with_no_update_stock_field_does_not(self):
		self.assertFalse(cgf.is_physical_fulfilment(_doc("Sales Invoice")))

	def test_unrelated_doctypes_do_not(self):
		for doctype in (
			"Stock Entry",
			"Sales Order",
			"Purchase Receipt",
			"Journal Entry",
		):
			with self.subTest(doctype=doctype):
				self.assertFalse(
					cgf.is_physical_fulfilment(_doc(doctype, update_stock=1))
				)


class TestEventKey(IntegrationTestCase):
	"""The identity that the UNIQUE constraint enforces."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_same_operation_gives_the_same_key(self):
		"""A retry of the same business operation must compute the same key."""
		a = cgf.build_event_key("GE", "Delivery Note", "ROW-1", "SER-1", "Delivery")
		b = cgf.build_event_key("GE", "Delivery Note", "ROW-1", "SER-1", "Delivery")
		self.assertEqual(a, b)

	def test_every_component_changes_the_key(self):
		base = ("GE", "Delivery Note", "ROW-1", "SER-1", "Delivery")
		key = cgf.build_event_key(*base)
		variants = [
			("GE2", "Delivery Note", "ROW-1", "SER-1", "Delivery"),
			("GE", "Sales Invoice", "ROW-1", "SER-1", "Delivery"),
			("GE", "Delivery Note", "ROW-2", "SER-1", "Delivery"),
			("GE", "Delivery Note", "ROW-1", "SER-2", "Delivery"),
			("GE", "Delivery Note", "ROW-1", "SER-1", "Reversal"),
		]
		for v in variants:
			with self.subTest(variant=v):
				self.assertNotEqual(cgf.build_event_key(*v), key)

	def test_a_reversal_has_its_own_key(self):
		"""Otherwise cancelling would collide with the delivery it reverses."""
		delivery = cgf.build_event_key("GE", "Delivery Note", "ROW-1", "S", "Delivery")
		reversal = cgf.build_event_key("GE", "Delivery Note", "ROW-1", "S", "Reversal")
		self.assertNotEqual(delivery, reversal)

	def test_a_missing_serial_is_stable_not_random(self):
		"""A non-serialised row must still retry to the same key."""
		a = cgf.build_event_key("GE", "Delivery Note", "ROW-1", None, "Delivery")
		b = cgf.build_event_key("GE", "Delivery Note", "ROW-1", None, "Delivery")
		self.assertEqual(a, b)

	def test_the_key_carries_no_timestamp(self):
		"""A clock in the key would defeat retry-safety entirely."""
		import time

		a = cgf.build_event_key("GE", "Delivery Note", "ROW-1", "S", "Delivery")
		time.sleep(0.01)
		b = cgf.build_event_key("GE", "Delivery Note", "ROW-1", "S", "Delivery")
		self.assertEqual(a, b)


class TestBatchOwnerResolution(IntegrationTestCase):
	"""Ownership comes from the Batch, not from a free-form tag on the Serial No."""

	@classmethod
	def setUpClass(cls):
		pass

	def _owner(self, batch_row):
		with patch(f"{MOD}.frappe.db.get_value", return_value=batch_row):
			return cgf._batch_owner("B-1")

	def test_customer_goods_batch_returns_its_customer(self):
		row = frappe._dict(
			custom_customer="CUST-1", custom_inventory_type="Customer Goods"
		)
		self.assertEqual(self._owner(row), "CUST-1")

	def test_regular_stock_batch_is_not_customer_owned(self):
		row = frappe._dict(
			custom_customer="CUST-1", custom_inventory_type="Regular Stock"
		)
		self.assertIsNone(self._owner(row))

	def test_customer_goods_without_a_customer_is_not_claimable(self):
		"""Unresolved ownership must not be guessed at."""
		row = frappe._dict(custom_customer=None, custom_inventory_type="Customer Goods")
		self.assertIsNone(self._owner(row))

	def test_unknown_batch_is_not_customer_owned(self):
		self.assertIsNone(self._owner(None))

	def test_no_batch_short_circuits_without_a_query(self):
		with patch(f"{MOD}.frappe.db.get_value") as get_value:
			self.assertIsNone(cgf._batch_owner(None))
		get_value.assert_not_called()


class TestSerialResolution(IntegrationTestCase):
	"""Serials come from the bundle where there is one, else the plain field."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_bundle_serials_win(self):
		row = frappe._dict(serial_and_batch_bundle="BUN-1", serial_no="IGNORED")
		with patch(f"{MOD}.frappe.get_all", return_value=["S1", "S2"]):
			self.assertEqual(cgf._row_serials(row), ["S1", "S2"])

	def test_plain_field_is_split_on_newlines(self):
		row = frappe._dict(serial_and_batch_bundle=None, serial_no="S1\nS2\n")
		self.assertEqual(cgf._row_serials(row), ["S1", "S2"])

	def test_a_row_with_no_serials_still_yields_one_event(self):
		"""Returning [] here would silently drop the whole row."""
		row = frappe._dict(serial_and_batch_bundle=None, serial_no=None)
		self.assertEqual(cgf._row_serials(row), [None])

	def test_an_empty_bundle_falls_back_to_the_plain_field(self):
		row = frappe._dict(serial_and_batch_bundle="BUN-1", serial_no="S9")
		with patch(f"{MOD}.frappe.get_all", return_value=[]):
			self.assertEqual(cgf._row_serials(row), ["S9"])


class _FakeLedger:
	"""``Customer Gold Ledger Entry`` in memory, for ``frappe.get_all`` only.

	It answers equality and ``["in", values]`` filters, projects ``fields`` and honours
	``pluck`` -- what the position readers ask of the ledger -- and counts its queries.
	"""

	def __init__(self, *rows):
		self.rows = rows
		self.queries = 0

	def get_all(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
		self.queries += 1
		matched = [row for row in self.rows if self._matches(row, filters or {})]
		if pluck:
			return [row.get(pluck) for row in matched]
		return [
			frappe._dict({field: row.get(field) for field in fields or ("name",)})
			for row in matched
		]

	@staticmethod
	def _matches(row, filters):
		for field, wanted in filters.items():
			if isinstance(wanted, (list, tuple)):
				operator, values = wanted
				if operator != "in":
					raise AssertionError(
						f"the fake ledger does not answer {operator!r}"
					)
				if row.get(field) not in values:
					return False
			elif row.get(field) != wanted:
				return False
		return True


#: MCON00333 / MAT-STE-19749 (kg-gk, 29 Sep 2026): GJCU0009's 20 g of 24KT, drawn from two
#: receipts' batches (-12: 1.327 g, -13: 18.673 g), became 21.798 g of 22KT at 91.75%. The
#: 1.798 g of alloy is company metal and writes no custody event.
G24 = "M-G-24KT-99.9-Y"
G22 = "M-G-22KT-91.75-Y"
COMPANY = "GE"
CUSTOMER = "GJCU0009"


def _event(name, kind, item_code, gross, fine=None, reversal_of=None):
	"""One ledger row. Fine defaults to gross: the site reads 24KT '99.9' as 100%."""
	return frappe._dict(
		name=name,
		company=COMPANY,
		customer=CUSTOMER,
		item_code=item_code,
		cg_event_kind=kind,
		cg_gross_qty_delta=gross,
		cg_fine_gold_delta=gross if fine is None else fine,
		cg_fine_measurement_status=cgf.STATUS_KNOWN,
		cg_measurement_reason=None,
		cg_reversal_of=reversal_of,
	)


def _reversal(original):
	"""What ``_reverse_events`` writes: every basis negated, pointing at the original."""
	return _event(
		f"rev-{original.name}",
		cgf.EVENT_REVERSAL,
		original.item_code,
		-original.cg_gross_qty_delta,
		fine=-original.cg_fine_gold_delta,
		reversal_of=original.name,
	)


def _mcon00333_cancelled():
	"""MCON00333's ledger after its conversion is cancelled: 2 receipts, 3 events, 3 reversals."""
	conversion = (
		_event("4s86vck6gc", cgf.EVENT_CONVERSION_OUT, G24, -1.327),
		_event("4s86s02k9b", cgf.EVENT_CONVERSION_OUT, G24, -18.673),
		_event("4s86ctddm3", cgf.EVENT_CONVERSION_IN, G22, 21.798, fine=20.0),
	)
	return _FakeLedger(
		_event("rcpt-12", cgf.EVENT_RECEIPT, G24, 10.0),
		_event("rcpt-13", cgf.EVENT_RECEIPT, G24, 20.0),
		*conversion,
		*(_reversal(event) for event in conversion),
	)


class TestReversalCountsWhereItsOriginalCounts(IntegrationTestCase):
	"""D1a / R3: a Reversal moves the position only when the event it undoes did.

	``POSITION_KINDS`` lists ``Reversal`` beside the custody kinds, but a Reversal's own kind
	says nothing about what it undoes. Counted unconditionally, cancelling MCON00333's
	conversion reads 24KT +20.000 g and 22KT -21.798 g: the undo of conversion events the
	position never counted. The position readers are exercised against an in-memory ledger.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _read(self, ledger, reader, *args, **kwargs):
		with patch(
			f"{MOD}.frappe.get_all",
			side_effect=_owning({cgf.LEDGER_DOCTYPE}, ledger.get_all, _REAL_GET_ALL),
		):
			return reader(*args, **kwargs)

	def _gross(self, ledger, item_code):
		return self._read(
			ledger, cgf.get_customer_gold_position, COMPANY, CUSTOMER, item_code
		)

	def _report(self, ledger, item_code, basis):
		return self._read(
			ledger,
			cgf.get_customer_gold_position_report,
			COMPANY,
			CUSTOMER,
			item_code,
			basis=basis,
		)

	def test_a_cancelled_conversion_leaves_each_items_gross_position(self):
		"""Receipts of 10 + 20 g of 24KT; the conversion and its cancel leave exactly that."""
		ledger = _mcon00333_cancelled()

		for label, reader, args, expected in (
			("24KT gross", cgf.get_customer_gold_position, (G24,), 30.0),
			("22KT gross", cgf.get_customer_gold_position, (G22,), 0.0),
			("fine", cgf.get_customer_gold_fine_position, (), 30.0),
			# Built on the gross position, so it is fixed through the same reader.
			("24KT free", cgf.get_customer_gold_free_quantity, (G24,), 30.0),
		):
			with self.subTest(label):
				self.assertAlmostEqual(
					self._read(ledger, reader, COMPANY, CUSTOMER, *args),
					expected,
					places=3,
				)

	def test_the_position_report_drops_the_same_reversals(self):
		"""The report reads the same rows as the scalar, so its totals and counts agree."""
		ledger = _mcon00333_cancelled()

		source = self._report(ledger, G24, "gross")
		self.assertEqual((source["total"], source["rows"]), (30.0, 2))

		target = self._report(ledger, G22, "gross")
		self.assertEqual((target["total"], target["rows"]), (0.0, 0))

		target_fine = self._report(ledger, G22, "fine")
		self.assertEqual((target_fine["total"], target_fine["rows"]), (0.0, 0))
		self.assertTrue(target_fine["complete"])

		fine = self._report(ledger, None, "fine")
		self.assertEqual(
			(fine["total"], fine["known_total"], fine["rows"]), (30.0, 30.0, 2)
		)

	def test_reversals_of_stage_and_value_kinds_do_not_count(self):
		"""Their originals never moved the position, so undoing them must not move it either.

		Revaluation is zero on every quantity basis, so its Reversal shows only in the row
		count. Cancelling a Manufacture (Production) would otherwise lower the holding.
		"""
		for kind, gross in (
			(cgf.EVENT_TRANSFER_OUT, -5.0),
			(cgf.EVENT_TRANSFER_IN, 5.0),
			(cgf.EVENT_CONVERSION_OUT, -5.0),
			(cgf.EVENT_CONVERSION_IN, 5.0),
			(cgf.EVENT_PRODUCTION, 5.0),
			(cgf.EVENT_REVALUATION, 0.0),
		):
			with self.subTest(kind=kind):
				self.assertNotIn(kind, cgf.POSITION_KINDS)
				original = _event("orig", kind, G24, gross)
				ledger = _FakeLedger(
					_event("rcpt", cgf.EVENT_RECEIPT, G24, 20.0),
					original,
					_reversal(original),
				)
				self.assertAlmostEqual(self._gross(ledger, G24), 20.0, places=3)
				self.assertEqual(self._report(ledger, G24, "gross")["rows"], 1)

	def test_reversals_that_already_counted_still_count(self):
		"""Pin: passes before and after the fix, and stops it from going too far.

		Dropping every Reversal would leave a cancelled receipt in the holding. A Reversal
		with no ``cg_reversal_of``, or naming a row that is gone, is an explicit correction
		and moves the position by its own delta.
		"""
		# One level of lookup is exact only because no writer ever reverses a Reversal.
		self.assertNotIn(cgf.EVENT_REVERSAL, cgf.STOCK_ENTRY_KINDS)

		for kind, gross in (
			(cgf.EVENT_RECEIPT, 5.0),
			(cgf.EVENT_RETURN, -5.0),
			(cgf.EVENT_DELIVERY, -5.0),
			(cgf.EVENT_DELIVERY_RETURN, 5.0),
			(cgf.EVENT_APPROVED_LOSS, -5.0),
			(cgf.EVENT_RECOVERY, 5.0),
		):
			with self.subTest(kind=kind):
				self.assertIn(kind, cgf.POSITION_KINDS)
				original = _event("orig", kind, G24, gross)
				ledger = _FakeLedger(
					_event("rcpt", cgf.EVENT_RECEIPT, G24, 20.0),
					original,
					_reversal(original),
				)
				self.assertAlmostEqual(self._gross(ledger, G24), 20.0, places=3)
				self.assertEqual(self._report(ledger, G24, "gross")["rows"], 3)

		for label, reversal_of in (("unlinked", None), ("missing original", "gone")):
			with self.subTest(label):
				ledger = _FakeLedger(
					_event("rcpt", cgf.EVENT_RECEIPT, G24, 20.0),
					_event(
						"correction",
						cgf.EVENT_REVERSAL,
						G24,
						-3.0,
						reversal_of=reversal_of,
					),
				)
				self.assertAlmostEqual(self._gross(ledger, G24), 17.0, places=3)
				self.assertEqual(self._report(ledger, G24, "gross")["rows"], 2)

	def test_the_originals_are_read_once_and_only_for_linked_reversals(self):
		"""At most one batched lookup however many Reversals there are (a join on
		``cg_reversal_of`` needs none), and none when nothing links."""
		ledger = _mcon00333_cancelled()
		self._read(ledger, cgf.get_customer_gold_fine_position, COMPANY, CUSTOMER)
		self.assertLessEqual(
			ledger.queries, 2, "three reversals, at most one lookup of their originals"
		)

		for label, rows in (
			("no reversal", (_event("rcpt", cgf.EVENT_RECEIPT, G24, 20.0),)),
			(
				"unlinked reversal",
				(
					_event("rcpt", cgf.EVENT_RECEIPT, G24, 20.0),
					_event("correction", cgf.EVENT_REVERSAL, G24, -3.0),
				),
			),
		):
			with self.subTest(label):
				ledger = _FakeLedger(*rows)
				self._gross(ledger, G24)
				self.assertEqual(ledger.queries, 1)
