# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pure-logic tests for the customer child-batch name allocator in ``batch_rename``.

No site data and no DB: names in, names out. The incident these pin is
EMP-IR-Labh-2026-14111 (2026-09-29): a 0.01 g process loss on
``GJCU0009-2F09-M-G-22KT-91.75-Y-12-A`` failed with "already uses all 26 child
suffixes" although that parent had no children at all. The old base kept only the
parent's LAST segment, so every ``...-NN-A`` parent shared one 26-slot pool, and the
pool stopped at Z.
"""

from frappe.tests import UnitTestCase

from jewellery_erpnext.customer_subcontracting import batch_rename

# Real parent names from the incident site (kg-gk copy, 2026-09-29).
RECEIPT_24KT_12 = "GJCU0009-2F09-M-G-24KT-99.9-Y-12"
CONVERSION_12_A = "GJCU0009-2F09-M-G-22KT-91.75-Y-12-A"
RECEIPT_22KT_04 = "GJCU0009-2F09-M-G-22KT-91.75-Y-04"
CONVERSION_04_A = "GJCU0009-2F09-M-G-22KT-91.75-Y-04-A"
LOSS_ITEM = "ML-G-22KT-91.75-Y"
ITEM_22KT = "M-G-22KT-91.75-Y"


class TestChildSuffixCodec(UnitTestCase):
	def test_encode_matches_the_ordinal_table(self):
		table = {
			1: "A",
			2: "B",
			25: "Y",
			26: "Z",
			27: "AA",
			28: "AB",
			52: "AZ",
			53: "BA",
			702: "ZZ",
			703: "AAA",
		}
		for ordinal, suffix in table.items():
			self.assertEqual(batch_rename.encode_child_suffix(ordinal), suffix, ordinal)

	def test_decode_is_the_exact_inverse(self):
		for ordinal in range(1, 20001):
			suffix = batch_rename.encode_child_suffix(ordinal)
			self.assertEqual(batch_rename.decode_child_suffix(suffix), ordinal, suffix)

	def test_successor_of_z_is_aa_not_the_next_ascii_character(self):
		z = batch_rename.decode_child_suffix("Z")
		self.assertEqual(batch_rename.encode_child_suffix(z + 1), "AA")

	def test_encode_rejects_non_positive_ordinals(self):
		for bad in (0, -1):
			with self.assertRaises(ValueError):
				batch_rename.encode_child_suffix(bad)

	def test_decode_rejects_anything_but_ascii_letters(self):
		for bad in ("", None, "01", "A1", "A-B", "Ä", " A"):
			self.assertIsNone(batch_rename.decode_child_suffix(bad), repr(bad))

	def test_decode_is_case_insensitive_like_the_name_column(self):
		# tabBatch.name is utf8mb4_unicode_ci: '...-a' occupies the '...-A' slot.
		self.assertEqual(batch_rename.decode_child_suffix("a"), 1)
		self.assertEqual(batch_rename.decode_child_suffix("aB"), 28)


class TestChildBatchBase(UnitTestCase):
	def test_receipt_parent_keeps_its_serial(self):
		self.assertEqual(
			batch_rename.child_batch_base(
				RECEIPT_24KT_12, "M-G-24KT-99.9-Y", "GJCU0009", ITEM_22KT
			),
			"GJCU0009-2F09-M-G-22KT-91.75-Y-12",
		)

	def test_conversion_parent_keeps_its_whole_serial_path(self):
		"""The incident: the base must not collapse to the trailing 'A'."""
		self.assertEqual(
			batch_rename.child_batch_base(
				CONVERSION_12_A, ITEM_22KT, "GJCU0009", LOSS_ITEM
			),
			"GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A",
		)

	def test_parents_sharing_a_trailing_segment_get_distinct_pools(self):
		serials = ("03", "05", "07", "08", "10", "11", "12")
		bases = {
			batch_rename.child_batch_base(
				f"GJCU0009-2F09-M-G-22KT-91.75-Y-{serial}-A",
				ITEM_22KT,
				"GJCU0009",
				LOSS_ITEM,
			)
			for serial in serials
		}
		self.assertEqual(len(bases), len(serials))

	def test_receipt_and_its_conversion_child_get_distinct_pools(self):
		receipt = batch_rename.child_batch_base(
			RECEIPT_22KT_04, ITEM_22KT, "GJCU0009", LOSS_ITEM
		)
		conversion = batch_rename.child_batch_base(
			CONVERSION_04_A, ITEM_22KT, "GJCU0009", LOSS_ITEM
		)
		self.assertEqual(receipt, "GJCU0009-2F09-ML-G-22KT-91.75-Y-04")
		self.assertEqual(conversion, "GJCU0009-2F09-ML-G-22KT-91.75-Y-04-A")

	def test_hyphenated_customer_keeps_the_year_month(self):
		self.assertEqual(
			batch_rename.child_batch_base(
				"CG-TEST-CUSTOMER-B-2F09-CG-TEST-GOLD-99.9-01",
				"CG-TEST-GOLD-99.9",
				"CG-TEST-CUSTOMER-B",
				"CG-TEST-GOLD-75.4",
			),
			"CG-TEST-CUSTOMER-B-2F09-CG-TEST-GOLD-75.4-01",
		)

	def test_three_segment_autoname_is_not_extended(self):
		self.assertIsNone(
			batch_rename.child_batch_base(
				"KG2F093-MGL229175Y0-604EO", ITEM_22KT, "GJCU0009", LOSS_ITEM
			)
		)

	def test_unparseable_parent_falls_back_to_the_legacy_base(self):
		"""The consume row's item not matching the name keeps today's name exactly.

		This is also what keeps the generation-1 pins in test_conversion_lane_downstream
		byte-identical: their fake source rows carry an 18KT item under a 24KT name.
		"""
		self.assertEqual(
			batch_rename.child_batch_base(
				"TNCU0001-2F06-M-G-24KT-99.9-Y-01",
				"M-G-18KT-75.0-Y",
				"TNCU0001",
				"M-G-18KT-75.0-Y",
			),
			"TNCU0001-2F06-M-G-18KT-75.0-Y-01",
		)
		self.assertEqual(
			batch_rename.child_batch_base(CONVERSION_12_A, None, "GJCU0009", LOSS_ITEM),
			"GJCU0009-2F09-ML-G-22KT-91.75-Y-A",
		)

	def test_missing_customer_uses_the_parent_owner(self):
		self.assertEqual(
			batch_rename.child_batch_base(CONVERSION_12_A, ITEM_22KT, None, LOSS_ITEM),
			"GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A",
		)


class TestHighestChildOrdinal(UnitTestCase):
	BASE = "GJCU0009-2F09-ML-G-22KT-91.75-Y-A"

	def _names(self, *suffixes):
		return [f"{self.BASE}-{s}" for s in suffixes]

	def test_empty_pool(self):
		self.assertEqual(batch_rename.highest_child_ordinal(self.BASE, []), 0)

	def test_full_single_letter_pool_continues_past_z(self):
		names = self._names(*(chr(c) for c in range(ord("A"), ord("Z") + 1)))
		self.assertEqual(batch_rename.highest_child_ordinal(self.BASE, names), 26)
		self.assertEqual(batch_rename.encode_child_suffix(26 + 1), "AA")

	def test_two_letter_suffix_is_read_numerically(self):
		# Lexically 'Z' > 'AA'; numerically AA (27) is the highest.
		names = self._names("Y", "Z", "AA")
		self.assertEqual(batch_rename.highest_child_ordinal(self.BASE, names), 27)

	def test_lone_z_does_not_exhaust_the_pool(self):
		self.assertEqual(
			batch_rename.highest_child_ordinal(self.BASE, self._names("Z")), 26
		)

	def test_gaps_are_never_refilled(self):
		self.assertEqual(
			batch_rename.highest_child_ordinal(self.BASE, self._names("A", "C", "D")), 4
		)

	def test_only_exact_children_count(self):
		names = [
			f"{self.BASE}-C",
			f"{self.BASE}-Z-A",  # a deeper descendant
			f"{self.BASE}-01",  # not a letter suffix
			f"{self.BASE}A-Z",  # a prefix sibling
			"GJCU0009-2F09-ML-G-22KT-91.75-Y-12-A-Z",  # another pool
		]
		self.assertEqual(batch_rename.highest_child_ordinal(self.BASE, names), 3)

	def test_lowercase_names_occupy_their_slot(self):
		self.assertEqual(
			batch_rename.highest_child_ordinal(self.BASE, [f"{self.BASE.lower()}-b"]), 2
		)

	def test_result_is_independent_of_order(self):
		names = self._names("B", "AA", "C", "Z")
		self.assertEqual(
			batch_rename.highest_child_ordinal(self.BASE, names),
			batch_rename.highest_child_ordinal(self.BASE, list(reversed(names))),
		)


class TestHighestReceiptSerial(UnitTestCase):
	def test_counts_receipts_across_items(self):
		"""Serials are per (customer, month), not per item: 22KT-04 and 24KT-04 collided."""
		names = [
			"GJCU0009-2F09-M-G-24KT-99.9-Y-12",
			"GJCU0009-2F09-M-G-24KT-99.9-Y-13",
			"GJCU0009-2F09-M-G-22KT-91.75-Y-04",
			"GJCU0009-2F09-M-G-22KT-91.75-Y-12-A",  # a child ends in a letter
			"GJCU0009-2F09-RI00210-004-A-A",
		]
		self.assertEqual(batch_rename.highest_receipt_serial(names), 13)

	def test_no_receipts(self):
		self.assertEqual(batch_rename.highest_receipt_serial([]), 0)
		self.assertEqual(
			batch_rename.highest_receipt_serial(
				["GJCU0009-2F09-ML-G-22KT-91.75-Y-A-Z"]
			),
			0,
		)
