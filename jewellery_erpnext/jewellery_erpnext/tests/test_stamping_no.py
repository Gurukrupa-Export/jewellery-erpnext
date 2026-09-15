# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Tests for the Serial No stamping number (jewellery_erpnext.doc_events.serial_no).

The bug these guard: two Serial Number Creator submits seconds apart handed both pieces the
SAME stamping number, because the sequence came from an unlocked ``MAX(...) + 1`` read.
"""

from unittest.mock import patch

import frappe
from frappe.model.naming import NamingSeries
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import serial_no as stamping
from jewellery_erpnext.jewellery_erpnext.doc_events.serial_no import (
	_STAMPING_SERIES_NS,
	reserve_stamping_sequence,
	stamping_prefix,
	stamping_series_key,
	stamping_year_code,
)

TEST_PREFIX = (
	"2Q"  # a year code no real data uses, so these tests never touch live counters
)
TEST_KEY = f"{_STAMPING_SERIES_NS}{TEST_PREFIX}"


def _drop_counter(key=TEST_KEY):
	frappe.db.sql("DELETE FROM `tabSeries` WHERE `name` = %s", (key,))


def _counter(key=TEST_KEY):
	row = frappe.db.sql("SELECT `current` FROM `tabSeries` WHERE `name` = %s", (key,))
	return row[0][0] if row else None


class TestStampingKey(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_year_code_maps_from_2021(self):
		self.assertEqual(stamping_year_code(2021), "A")
		self.assertEqual(stamping_year_code(2026), "F")
		self.assertEqual(stamping_year_code(2027), "G")

	def test_prefix_uses_current_year_by_default(self):
		with patch.object(stamping, "now_datetime") as now:
			now.return_value = frappe.utils.get_datetime("2026-05-01 10:00:00")
			self.assertEqual(stamping_prefix(), "2F")

	def test_prefix_honours_an_explicit_moment(self):
		"""The backfill labels a legacy piece by its creation year -- deliberately."""
		self.assertEqual(
			stamping_prefix(frappe.utils.get_datetime("2024-03-02 00:00:00")), "2D"
		)

	def test_series_key_is_namespaced(self):
		self.assertEqual(stamping_series_key("2F"), "#JWL-STAMP-2F")

	def test_series_key_cannot_collide_with_a_naming_series(self):
		"""The regression this namespace exists for.

		`tabSeries`.`name` is site-wide. A naming_series of "2F.####" claims the bare key
		"2F" -- which is exactly what the stamping prefix would have been. A key starting
		with "#" is unreachable from any naming series, because parse_naming_series consumes
		a "#"-leading part as the counter itself.
		"""
		self.assertEqual(NamingSeries("2F.####").get_prefix(), "2F")
		self.assertNotEqual(
			NamingSeries("2F.####").get_prefix(), stamping_series_key("2F")
		)
		self.assertTrue(stamping_series_key("2F").startswith("#"))


class TestReserveStampingSequence(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		_drop_counter()

	def tearDown(self):
		_drop_counter()
		frappe.db.commit()

	def test_successive_reserves_are_unique_and_increasing(self):
		got = [reserve_stamping_sequence(TEST_PREFIX) for _ in range(5)]
		self.assertEqual(got, sorted(got))
		self.assertEqual(len(set(got)), 5)

	def test_prefixes_have_independent_counters(self):
		other = "2R"
		_drop_counter(f"{_STAMPING_SERIES_NS}{other}")
		try:
			reserve_stamping_sequence(TEST_PREFIX)
			reserve_stamping_sequence(TEST_PREFIX)
			self.assertEqual(reserve_stamping_sequence(other), 1)
		finally:
			_drop_counter(f"{_STAMPING_SERIES_NS}{other}")

	def test_is_gapless_across_a_rollback(self):
		"""A burnt number would be a hole in a sequence that goes onto physical pieces.

		The counter is an ordinary InnoDB row updated in the caller's own transaction, so a
		failed submit takes the number back with it. This fails the moment anything starts
		committing mid-cascade.
		"""
		frappe.db.commit()
		first = reserve_stamping_sequence(TEST_PREFIX)
		frappe.db.rollback()
		self.assertEqual(reserve_stamping_sequence(TEST_PREFIX), first)

	def test_seeds_from_numbers_already_issued(self):
		"""Never re-issue a number that is already stamped on a piece."""
		serial = frappe.db.sql("SELECT `name` FROM `tabSerial No` LIMIT 1")
		if not serial:
			self.skipTest("no Serial No rows on this site")
		name = serial[0][0]
		before = frappe.db.get_value("Serial No", name, "custom_stamping_no")
		try:
			frappe.db.set_value(
				"Serial No",
				name,
				"custom_stamping_no",
				f"{TEST_PREFIX}0347",
				update_modified=False,
			)
			_drop_counter()
			self.assertEqual(reserve_stamping_sequence(TEST_PREFIX), 348)
		finally:
			frappe.db.set_value(
				"Serial No", name, "custom_stamping_no", before, update_modified=False
			)

	def test_widens_past_four_digits(self):
		"""Preserves the old f-string behaviour once a year passes 9,999 pieces."""
		frappe.db.sql(
			"INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 9999)", (TEST_KEY,)
		)
		self.assertEqual(
			f"{TEST_PREFIX}{reserve_stamping_sequence(TEST_PREFIX):04d}", "2Q10000"
		)

	def test_counter_row_keeps_the_max_scan_off_the_hot_path(self):
		reserve_stamping_sequence(TEST_PREFIX)  # first call seeds the row
		with patch.object(
			stamping.frappe.db, "sql", wraps=stamping.frappe.db.sql
		) as sql:
			reserve_stamping_sequence(TEST_PREFIX)
		issued = " ".join(str(c.args[0]) for c in sql.call_args_list)
		self.assertNotIn("MAX(", issued.upper().replace("MAX (", "MAX("))


class TestSetStampingNo(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_never_restamps_an_already_stamped_piece(self):
		doc = frappe.new_doc("Serial No")
		doc.set("custom_stamping_no", "2F0007")
		with patch.object(stamping, "reserve_stamping_sequence") as reserve:
			stamping.set_stamping_no(doc)
		reserve.assert_not_called()
		self.assertEqual(doc.get("custom_stamping_no"), "2F0007")

	def test_formats_prefix_plus_four_digits(self):
		doc = frappe.new_doc("Serial No")
		with patch.object(stamping, "stamping_prefix", return_value="2F"), patch.object(
			stamping, "reserve_stamping_sequence", return_value=3
		):
			stamping.set_stamping_no(doc)
		self.assertEqual(doc.get("custom_stamping_no"), "2F0003")
