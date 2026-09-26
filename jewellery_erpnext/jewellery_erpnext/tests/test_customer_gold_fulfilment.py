# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""C12 -- the fulfilment predicate, the event identity, and the serial resolution.

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
