# Copyright (c) 2026, Gurukrupa Exports and Contributors
# See license.txt

import frappe
from frappe.tests import UnitTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import (
	parse_gemstone_size,
)


class TestGemstoneSizeParser(UnitTestCase):
	"""Guards the removal of eval() from validate_gemstone_item.

	Gemstone Size is free text on Item Variant Attribute — a master record editable
	with ordinary permissions. It used to be passed to eval(), so editing an
	attribute value gave remote code execution on every Gemstone Conversion save.
	"""

	def test_single_factor(self):
		self.assertAlmostEqual(parse_gemstone_size("3.30 MM"), 3.30)

	def test_two_factors_multiply(self):
		self.assertAlmostEqual(parse_gemstone_size("4.00*3.00 MM"), 12.0)

	def test_three_factors_multiply(self):
		self.assertAlmostEqual(parse_gemstone_size("2*3*4 MM"), 24.0)

	def test_x_separator_accepted(self):
		"""Not present in the current corpus; accepted so a future master-data
		value cannot turn this fix into a production outage."""
		self.assertAlmostEqual(parse_gemstone_size("2 x 3 MM"), 6.0)
		self.assertAlmostEqual(parse_gemstone_size("2 X 3 MM"), 6.0)

	def test_unit_suffix_without_space(self):
		"""A plain .replace(" MM", "") silently mangles this into an unparseable
		string; the parser strips the unit case-insensitively instead."""
		self.assertAlmostEqual(parse_gemstone_size("3.30MM"), 3.30)
		self.assertAlmostEqual(parse_gemstone_size("3.30 mm"), 3.30)

	def test_no_unit_suffix(self):
		self.assertAlmostEqual(parse_gemstone_size("5.00*2.00"), 10.0)

	def test_code_injection_payload_is_rejected(self):
		"""The exact class of input that eval() would have executed."""
		for payload in (
			"__import__('os').system('id')",
			"1+1",
			"[].__class__",
			"3.30 MM; DROP TABLE `tabItem`",
		):
			with self.assertRaises(frappe.ValidationError):
				parse_gemstone_size(payload)

	def test_missing_value_raises_rather_than_returning_zero(self):
		"""Empty must be an error, not 0.0: the parsed source size feeds
		`s_gemstone_size < t_gemstone_size`, so a silent 0.0 would make every
		target row fail with a misleading 'should not bigger than source' message."""
		for empty in (None, "", "   "):
			with self.assertRaises(frappe.ValidationError):
				parse_gemstone_size(empty)

	def test_item_code_appears_in_error(self):
		with self.assertRaises(frappe.ValidationError):
			parse_gemstone_size("nonsense", item_code="G-TEST-ITEM")

	def test_production_corpus_all_parses(self):
		"""Regression guard: every distinct Gemstone Size value present on the `gk`
		dataset at the time eval() was removed (314 values) must parse without
		raising. If a future grammar change breaks a real value, this fails."""
		for value in GK_GEMSTONE_SIZE_CORPUS:
			try:
				result = parse_gemstone_size(value)
			except Exception as exc:  # noqa: BLE001 - surface the offending value
				self.fail(f"corpus value {value!r} failed to parse: {exc}")
			self.assertGreater(result, 0, f"corpus value {value!r} parsed to {result}")

	def test_corpus_is_intact(self):
		self.assertEqual(len(GK_GEMSTONE_SIZE_CORPUS), 314)


# Distinct `Item Variant Attribute.attribute_value` values for attribute
# "Gemstone Size" on site `gk`, captured when eval() was removed. Read-only fixture.
GK_GEMSTONE_SIZE_CORPUS = (
	"0.80 MM",
	"0.90 MM",
	"1.00 MM",
	"1.10 MM",
	"1.20 MM",
	"1.25 MM",
	"1.30 MM",
	"1.35 MM",
	"1.40 MM",
	"1.45 MM",
	"1.50 MM",
	"1.60 MM",
	"1.60*1.60 MM",
	"1.70 MM",
	"1.80 MM",
	"1.90 MM",
	"10.00 MM",
	"10.00*10.00 MM",
	"10.00*12.00 MM",
	"10.00*13.00 MM",
	"10.00*14.00 MM",
	"10.00*5.00 MM",
	"10.00*6.00 MM",
	"10.00*6.50 MM",
	"10.00*7.00 MM",
	"10.00*7.50 MM",
	"10.00*8.00 MM",
	"10.00*8.50 MM",
	"10.00*9.00 MM",
	"10.50 MM",
	"10.50*10.50 MM",
	"10.50*6.50 MM",
	"10.50*7.00 MM",
	"10.50*7.50 MM",
	"10.50*8.00 MM",
	"10.50*8.50 MM",
	"10.50*9.00 MM",
	"10.50*9.50 MM",
	"11.00 MM",
	"11.00*10.00 MM",
	"11.00*11.00 MM",
	"11.00*12.00 MM",
	"11.00*6.50 MM",
	"11.00*7.00 MM",
	"11.00*7.50 MM",
	"11.00*8.00 MM",
	"11.00*8.50 MM",
	"11.00*9.00 MM",
	"11.50 MM",
	"11.50*10.00 MM",
	"11.50*7.00 MM",
	"11.50*8.00 MM",
	"11.50*8.50 MM",
	"11.50*9.00 MM",
	"11.50*9.50 MM",
	"12.00 MM",
	"12.00*10.00 MM",
	"12.00*7.00 MM",
	"12.00*8.00 MM",
	"12.00*8.50 MM",
	"12.00*9.00 MM",
	"12.00*9.50 MM",
	"12.50 MM",
	"12.50*10.00 MM",
	"12.50*9.00 MM",
	"12.50*9.50 MM",
	"12.90*9.50 MM",
	"13.00 MM",
	"13.00*10.00 MM",
	"13.00*11.00 MM",
	"13.00*17.00 MM",
	"13.00*8.30 MM",
	"13.00*9.00 MM",
	"13.00*9.50 MM",
	"13.10*6.80 MM",
	"13.15*6.80 MM",
	"13.25*6.70 MM",
	"13.50 MM",
	"13.50*10.00 MM",
	"13.50*6.30 MM",
	"13.50*6.70 MM",
	"13.50*9.50 MM",
	"13.60*6.65 MM",
	"13.70*6.30 MM",
	"14.00 MM",
	"14.00*10.00 MM",
	"14.00*10.50 MM",
	"14.00*11.00 MM",
	"14.00*11.50 MM",
	"14.00*12.00 MM",
	"14.00*6.30 MM",
	"14.00*9.50 MM",
	"14.20*9.00 MM",
	"14.50*10.00 MM",
	"14.50*11.50 MM",
	"14.50*7.40 MM",
	"15.00*10.00 MM",
	"15.00*10.50 MM",
	"15.00*11.00 MM",
	"15.00*12.00 MM",
	"15.00*13.00 MM",
	"15.00*30.00 MM",
	"15.00*7.50 MM",
	"15.50*12.50 MM",
	"16.00 MM",
	"16.00*12.00 MM",
	"16.00*13.00 MM",
	"16.00*8.00 MM",
	"16.50*12.50 MM",
	"16.50*9.50 MM",
	"17.00 MM",
	"17.00*13.00 MM",
	"17.00*13.50 MM",
	"17.50*14.50 MM",
	"18.00 MM",
	"18.00*13.00 MM",
	"18.00*14.00 MM",
	"18.50*12.00 MM",
	"19.00*14.00 MM",
	"19.00*16.00 MM",
	"19.00*16.50 MM",
	"2.00 MM",
	"2.00*1.00 MM",
	"2.00*2.00 MM",
	"2.00*3.00 MM",
	"2.00*4.00 MM",
	"2.10 MM",
	"2.20 MM",
	"2.30 MM",
	"2.30*1.80 MM",
	"2.30*2.30 MM",
	"2.40 MM",
	"2.50 MM",
	"2.50*2.00 MM",
	"2.50*2.50 MM",
	"2.60 MM",
	"2.70 MM",
	"2.70*1.90 MM",
	"2.80 MM",
	"2.90 MM",
	"20.00*14.00 MM",
	"21.00*15.50 MM",
	"24.00*20.00 MM",
	"25.00*20.00 MM",
	"3.00 MM",
	"3.00*1.00 MM",
	"3.00*1.50 MM",
	"3.00*1.60 MM",
	"3.00*1.70 MM",
	"3.00*1.80 MM",
	"3.00*2.00 MM",
	"3.00*2.50 MM",
	"3.00*3.00 MM",
	"3.00*4.00 MM",
	"3.00*5.00 MM",
	"3.00*6.00 MM",
	"3.10 MM",
	"3.20 MM",
	"3.20*2.20 MM",
	"3.25 MM",
	"3.30 MM",
	"3.30*1.80 MM",
	"3.30*2.50 MM",
	"3.30*3.30 MM",
	"3.40 MM",
	"3.50 MM",
	"3.50*1.90 MM",
	"3.50*2.00 MM",
	"3.50*2.50 MM",
	"3.50*3.00 MM",
	"3.50*3.50 MM",
	"3.60 MM",
	"3.70 MM",
	"3.80 MM",
	"3.80*3.20 MM",
	"3.90 MM",
	"34.00*18.00 MM",
	"4.00 MM",
	"4.00*2.00 MM",
	"4.00*2.20 MM",
	"4.00*2.50 MM",
	"4.00*3.00 MM",
	"4.00*3.50 MM",
	"4.00*4.00 MM",
	"4.00*5.00 MM",
	"4.00*6.00 MM",
	"4.10 MM",
	"4.20 MM",
	"4.20*3.10 MM",
	"4.30 MM",
	"4.40 MM",
	"4.40*4.00 MM",
	"4.50 MM",
	"4.50*2.50 MM",
	"4.50*3.00 MM",
	"4.50*3.40 MM",
	"4.50*3.50 MM",
	"4.50*4.00 MM",
	"4.50*4.50 MM",
	"4.50*5.50 MM",
	"4.50*6.50 MM",
	"4.50*7.00 MM",
	"4.60 MM",
	"4.70 MM",
	"4.70*3.70 MM",
	"4.80 MM",
	"4.90 MM",
	"5.00 MM",
	"5.00*11.00 MM",
	"5.00*2.50 MM",
	"5.00*3.00 MM",
	"5.00*3.50 MM",
	"5.00*4.00 MM",
	"5.00*4.50 MM",
	"5.00*5.00 MM",
	"5.00*5.50 MM",
	"5.00*6.00 MM",
	"5.00*7.00 MM",
	"5.00*9.00 MM",
	"5.20 MM",
	"5.30 MM",
	"5.30*4.20 MM",
	"5.40 MM",
	"5.50 MM",
	"5.50*3.00 MM",
	"5.50*3.50 MM",
	"5.50*4.00 MM",
	"5.50*4.50 MM",
	"5.50*5.50 MM",
	"5.70 MM",
	"5.80 MM",
	"6.00 MM",
	"6.00*10.50 MM",
	"6.00*3.00 MM",
	"6.00*4.00 MM",
	"6.00*4.50 MM",
	"6.00*5.00 MM",
	"6.00*5.5 MM",
	"6.00*5.50 MM",
	"6.00*6.00 MM",
	"6.00*7.00 MM",
	"6.00*8.00 MM",
	"6.00*9.00 MM",
	"6.40 MM",
	"6.50 MM",
	"6.50*3.50 MM",
	"6.50*4.00 MM",
	"6.50*4.50 MM",
	"6.50*5.00 MM",
	"6.50*5.50 MM",
	"6.50*6.00 MM",
	"6.50*9.00 MM",
	"6.60 MM",
	"7.00 MM",
	"7.00*3.00 MM",
	"7.00*3.50 MM",
	"7.00*4.00 MM",
	"7.00*4.50 MM",
	"7.00*5.00 MM",
	"7.00*5.50 MM",
	"7.00*6.00 MM",
	"7.00*6.50 MM",
	"7.00*7.00 MM",
	"7.00*9.00 MM",
	"7.10*5.20 MM",
	"7.50 MM",
	"7.50*10.00 MM",
	"7.50*3.50 MM",
	"7.50*4.50 MM",
	"7.50*5.00 MM",
	"7.50*5.50 MM",
	"7.50*6.00 MM",
	"7.50*6.50 MM",
	"7.80 MM",
	"7.80*5.60 MM",
	"8.00 MM",
	"8.00*10.00 MM",
	"8.00*11.00 MM",
	"8.00*12.00 MM",
	"8.00*4.00 MM",
	"8.00*5.00 MM",
	"8.00*5.50 MM",
	"8.00*6.00 MM",
	"8.00*6.50 MM",
	"8.00*7.00 MM",
	"8.00*7.50 MM",
	"8.00*8.00 MM",
	"8.50 MM",
	"8.50*5.50 MM",
	"8.50*6.00 MM",
	"8.50*6.50 MM",
	"8.50*7.00 MM",
	"8.50*7.50 MM",
	"8.50*8.00 MM",
	"8.50*8.50 MM",
	"8.80*5.50 MM",
	"9.00 MM",
	"9.00*11.00 MM",
	"9.00*12.00 MM",
	"9.00*5.00 MM",
	"9.00*5.50 MM",
	"9.00*6.00 MM",
	"9.00*6.50 MM",
	"9.00*7.00 MM",
	"9.00*7.50 MM",
	"9.00*8.00 MM",
	"9.00*8.50 MM",
	"9.00*9.00 MM",
	"9.50 MM",
	"9.50*6.00 MM",
	"9.50*6.50 MM",
	"9.50*7.00 MM",
	"9.50*7.50 MM",
	"9.50*8.00 MM",
)
