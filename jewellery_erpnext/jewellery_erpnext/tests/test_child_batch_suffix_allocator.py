# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Tests for the customer child-batch suffix allocator in ``batch_rename``.

No site data: names in, names out. The incident these pin is
MWO-GEPL-EA14705-002-28-75.4-Y-01 on gepl-prod (2026-11-03): Create SNC converted
customer TNCU0085's receipt batch ``TNCU0085-2F11-M-G-24KT-99.9-Y-01`` to 18KT for the
27th time that month and failed with "parent batch ... already uses all 26 child
suffixes (A-Z)". The suffix was a single letter, so a parent could have 26 children.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from jewellery_erpnext.customer_subcontracting import batch_rename

PARENT_24KT = "TNCU0085-2F11-M-G-24KT-99.9-Y-01"
ITEM_18KT = "M-G-18KT-75.4-Y"
BASE_18KT = "TNCU0085-2F11-M-G-18KT-75.4-Y-01"


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


class TestHighestChildOrdinal(UnitTestCase):
	BASE = BASE_18KT

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
			"TNCU0085-2F11-M-G-18KT-75.4-Y-02-Z",  # another pool
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


def _row(**fields):
	defaults = {
		"name": "row-x",
		"item_code": ITEM_18KT,
		"s_warehouse": None,
		"t_warehouse": None,
		"batch_no": None,
		"inventory_type": "Customer Goods",
		"customer": "TNCU0085",
	}
	defaults.update(fields)
	row = SimpleNamespace(**defaults)
	row.get = lambda k, default=None: getattr(row, k, default)
	return row


class TestCreateChildBatchesSuffix(UnitTestCase):
	"""``create_child_batches`` on the Create SNC conversion shape: one 24KT source row
	from the customer's receipt batch, one batch-less 18KT produce row."""

	def _run(self, existing, taken=(), child_item=ITEM_18KT):
		doc = SimpleNamespace(
			doctype="Stock Entry",
			name="MAT-STE-TEST",
			_customer="TNCU0085",
			items=[
				_row(
					name="src",
					item_code="M-G-24KT-99.9-Y",
					s_warehouse="RM - GEPL",
					batch_no=PARENT_24KT,
				),
				_row(name="tgt", t_warehouse="RM - GEPL", item_code=child_item),
			],
		)
		inserted = []

		def _new_doc(doctype):
			batch = frappe._dict()
			batch.insert = lambda ignore_permissions=False: inserted.append(batch)
			return batch

		with patch.object(
			batch_rename.frappe.db, "sql", return_value=list(existing)
		) as sql, patch.object(
			batch_rename.frappe.db, "exists", side_effect=lambda dt, name: name in taken
		), patch.object(
			batch_rename.frappe, "new_doc", side_effect=_new_doc
		), patch.object(batch_rename, "_source_row_rate", return_value=0.0):
			batch_rename.create_child_batches(doc)
		return doc, inserted, sql

	def test_27th_child_is_aa_instead_of_failing(self):
		full = [f"{BASE_18KT}-{chr(c)}" for c in range(ord("A"), ord("Z") + 1)]
		doc, inserted, _sql = self._run(full)
		self.assertEqual(doc.items[1].batch_no, f"{BASE_18KT}-AA")
		self.assertEqual([b.batch_id for b in inserted], [f"{BASE_18KT}-AA"])

	def test_next_after_aa_is_ab(self):
		existing = [f"{BASE_18KT}-{s}" for s in ("A", "B", "Z", "AA")]
		doc, _inserted, _sql = self._run(existing)
		self.assertEqual(doc.items[1].batch_no, f"{BASE_18KT}-AB")

	def test_first_child_is_still_a(self):
		doc, _inserted, _sql = self._run([])
		self.assertEqual(doc.items[1].batch_no, f"{BASE_18KT}-A")

	def test_name_taken_since_the_read_moves_to_the_next_suffix(self):
		existing = [f"{BASE_18KT}-{chr(c)}" for c in range(ord("A"), ord("Z") + 1)]
		doc, _inserted, _sql = self._run(existing, taken={f"{BASE_18KT}-AA"})
		self.assertEqual(doc.items[1].batch_no, f"{BASE_18KT}-AB")

	def test_like_pattern_escapes_wildcards(self):
		# '_' in the child item code must reach LIKE as a literal, not a wildcard.
		_doc, _inserted, sql = self._run([], child_item="M_G-18KT%75.4-Y")
		self.assertEqual(
			sql.call_args.args[1],
			("TNCU0085-2F11-M\\_G-18KT\\%75.4-Y-01-%",),
		)
		self.assertEqual(batch_rename._escape_like("A_B%C"), "A\\_B\\%C")
