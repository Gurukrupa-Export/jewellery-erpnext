# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""A serialized Stock Entry row is booked in the lane its serial was last received in.

KG-PHS-26-00078's submit built MAT-STE-22127 with every finished piece as "Regular Stock",
no customer. Serial KLHGX62F1257 had last been received as GJCU0009's Customer Goods, and
erpnext's ``validate_serial_no_inventory_dimension`` refused the outward SLE::

    Customer: expected "GJCU0009", got "None",
    Inventory Type: expected "Customer Goods", got "Regular Stock"

Pure-logic apart from the last class: fake rows, the ledger lookup stubbed, nothing written.
"""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jewellery_erpnext.jewellery_erpnext.customization.utils import serial_ownership

CUSTOMER = "GJCU0009"
OTHER_CUSTOMER = "GJCU0010"
ITEM = "RI00210-004"
CUSTOMER_LANE = ("Customer Goods", CUSTOMER)
COMPANY_LANE = ("Regular Stock", None)


class _Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


def _row(serial_no="KLHGX62F1257", **kwargs):
	"""Product Certification's issue row: a serial, a source, no lane of its own."""
	fields = {
		"item_code": ITEM,
		"serial_no": serial_no,
		"serial_and_batch_bundle": None,
		"s_warehouse": "Product Certification WO - KGJPL",
		"t_warehouse": "Sadguru Hallmarking Centre WIP WH - KGJPL",
		"inventory_type": None,
		"customer": None,
	}
	fields.update(kwargs)
	return _Doc(**fields)


def _stamp(rows, lanes, **doc_fields):
	"""Run the stamp with ``lanes`` as the ledger; returns the lookup's calls."""
	doc = _Doc(items=rows, set_posting_time=0, posting_date=None, posting_time=None, **doc_fields)
	with patch.object(serial_ownership, "last_inward_lanes", return_value=lanes) as lookup:
		serial_ownership.stamp_serial_row_ownership(doc)
	return lookup


def _lane(row):
	return (row.inventory_type, row.customer)


class TestTheSerialDecidesTheLane(unittest.TestCase):
	def test_a_customers_piece_is_booked_as_the_customers(self):
		"""The KG-PHS-26-00078 shape."""
		row = _row()
		_stamp([row], {(ITEM, "KLHGX62F1257"): CUSTOMER_LANE})
		self.assertEqual(_lane(row), CUSTOMER_LANE)

	def test_the_frameworks_regular_stock_default_is_not_a_choice(self):
		"""A re-save arrives after the blanket default already wrote "Regular Stock"."""
		row = _row(inventory_type="Regular Stock")
		_stamp([row], {(ITEM, "KLHGX62F1257"): CUSTOMER_LANE})
		self.assertEqual(_lane(row), CUSTOMER_LANE)

	def test_a_company_piece_stays_regular_stock(self):
		row = _row()
		_stamp([row], {(ITEM, "KLHGX62F1257"): COMPANY_LANE})
		self.assertEqual(_lane(row), COMPANY_LANE)

	def test_every_serial_of_a_row_is_looked_up_for_that_rows_item(self):
		row = _row(serial_no="S-1\nS-2")
		lookup = _stamp([row], {(ITEM, "S-1"): CUSTOMER_LANE, (ITEM, "S-2"): CUSTOMER_LANE})
		self.assertEqual(lookup.call_args.args[0], {(ITEM, "S-1"), (ITEM, "S-2")})
		self.assertEqual(_lane(row), CUSTOMER_LANE)


class TestWhatIsLeftAlone(unittest.TestCase):
	def test_a_row_that_names_an_owner_keeps_it(self):
		"""A real conflict stays the validator's to report, not this stamp's to paper over."""
		row = _row(inventory_type="Customer Goods", customer=OTHER_CUSTOMER)
		_stamp([row], {(ITEM, "KLHGX62F1257"): CUSTOMER_LANE})
		self.assertEqual(_lane(row), ("Customer Goods", OTHER_CUSTOMER))

	def test_serials_received_in_different_lanes_are_not_guessed_between(self):
		row = _row(serial_no="S-1\nS-2")
		_stamp([row], {(ITEM, "S-1"): CUSTOMER_LANE, (ITEM, "S-2"): COMPANY_LANE})
		self.assertEqual(_lane(row), (None, None))

	def test_a_serial_with_no_receipt_on_record_is_not_guessed(self):
		row = _row()
		_stamp([row], {})
		self.assertEqual(_lane(row), (None, None))

	def test_a_receipt_with_no_lane_is_not_copied(self):
		"""``expected "Not Set"`` is the target-dimension backfill's to repair."""
		row = _row()
		_stamp([row], {(ITEM, "KLHGX62F1257"): (None, None)})
		self.assertEqual(_lane(row), (None, None))

	def test_customer_goods_without_a_customer_is_not_copied(self):
		row = _row()
		_stamp([row], {(ITEM, "KLHGX62F1257"): ("Customer Goods", None)})
		self.assertEqual(_lane(row), (None, None))

	def test_an_inward_only_row_is_not_looked_up(self):
		lookup = _stamp([_row(s_warehouse=None)], {})
		lookup.assert_not_called()

	def test_rows_without_serials_cost_no_query(self):
		lookup = _stamp([_row(serial_no=None)], {})
		lookup.assert_not_called()


class TestWhereTheSerialsComeFrom(unittest.TestCase):
	def test_a_row_without_the_field_reads_its_bundle(self):
		row = _row(serial_no=None, serial_and_batch_bundle="SBB-1")
		entries = [frappe._dict(parent="SBB-1", serial_no="KLHGX62F1257")]
		with patch.object(serial_ownership.frappe, "get_all", return_value=entries):
			_stamp([row], {(ITEM, "KLHGX62F1257"): CUSTOMER_LANE})
		self.assertEqual(_lane(row), CUSTOMER_LANE)


class TestThePostingTime(unittest.TestCase):
	def test_a_backdated_entry_compares_at_its_own_posting_time(self):
		doc = _Doc(set_posting_time=1, posting_date="2026-10-05", posting_time="17:35:31")
		self.assertEqual(serial_ownership._as_of(doc), datetime(2026, 10, 5, 17, 35, 31))

	def test_an_entry_that_posts_now_compares_at_now(self):
		doc = _Doc(set_posting_time=0, posting_date="2026-01-01", posting_time="10:00:00")
		self.assertGreater(serial_ownership._as_of(doc), datetime(2026, 1, 2))


class TestTheLedgerQueryRuns(unittest.TestCase):
	"""Against this site's real tables: the query must compile and run, whatever data is there."""

	def test_unknown_serials_have_no_lane(self):
		lanes = serial_ownership.last_inward_lanes(
			{(ITEM, "NO-SUCH-SERIAL-FOR-THIS-TEST")}, datetime(2026, 10, 6)
		)
		self.assertEqual(lanes, {})

	def test_a_site_without_the_customer_dimension_still_reads_the_type(self):
		"""A fresh CI site has ``inventory_type`` on the ledger but no ``customer`` column."""
		with patch.object(serial_ownership, "_lane_columns", return_value=["inventory_type"]):
			lanes = serial_ownership.last_inward_lanes(
				{(ITEM, "NO-SUCH-SERIAL-FOR-THIS-TEST")}, datetime(2026, 10, 6)
			)
		self.assertEqual(lanes, {})

	def test_a_site_without_the_type_dimension_has_nothing_to_stamp(self):
		with (
			patch.object(serial_ownership, "_lane_columns", return_value=[]),
			patch.object(serial_ownership.frappe, "qb") as qb,
		):
			lanes = serial_ownership.last_inward_lanes({(ITEM, "S-1")}, datetime(2026, 10, 6))
		self.assertEqual(lanes, {})
		qb.DocType.assert_not_called()

	def test_nothing_asked_costs_no_query(self):
		with patch.object(serial_ownership.frappe, "qb") as qb:
			self.assertEqual(serial_ownership.last_inward_lanes(set(), datetime(2026, 10, 6)), {})
		qb.DocType.assert_not_called()
