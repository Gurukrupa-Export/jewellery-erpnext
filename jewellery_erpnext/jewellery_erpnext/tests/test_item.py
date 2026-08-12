# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

"""Unit tests for the custom Item logic in doc_events/item.py.

The Item DocType carries the jewellery house rules on top of ERPNext Item:

* before_validate -> add_item_attributes: a variant template with a Subcategory
  but no attributes inherits them from the subcategory template.
* validate -> system_item_restriction (system items are read-only outside
  Administrator, and the flag is forced on for listed codes),
  update_item_uom_conversion (the Pcs conversion factor follows the
  diamond/gemstone attribute weight), and the variant description build-up.
* on_trash -> system items cannot be deleted outside Administrator.
* before_insert -> the auto batch/serial series: "*- V" stock groups get a
  GE<year><month><week>-<letter><abbr> batch series; "Consumables" variants get
  a GE...-CO or serial series derived from each Attribute Value's
  custom_batch_or_serial_no.
* calculate_item_wt_details -> whitelisted weight-estimate helper (ratios from
  Jewellery Settings, finding weight from the BOM).

DB-free per the suite convention: setUpClass is neutralised and every frappe
lookup -- get_all, db.get_value / db.exists / db.get_list, qb, session -- is
mocked. The clock is fixed so generated series are deterministic:
2026-08-12 -> year "2F", month "08", week "2" => prefix "GE2F082".

Run with:
  bench --site gk.localhost run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_item
"""

import json
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import item as item_events

FIXED_YEAR, FIXED_MONTH, FIXED_DAY = 2026, 8, 12

DATE_CODE = "GE2F082"  # deterministic GE series date-code for the fixed clock
BATCH_SUFFIX = "-.##."
CONSUMABLES_GROUP = "Consumable Metal"
STOCK_GROUPS = [
	("Metal - V", "M"),
	("Diamond - V", "D"),
	("Gemstone - V", "G"),
	("Finding - V", "F"),
	("Other - V", "O"),
]


def _fixed_clock():
	"""datetime stand-in pinned to 2026-08-12 (year code "2F", month "08", week "2")."""
	mock_dt = SimpleNamespace()
	mock_dt.datetime = SimpleNamespace(
		now=lambda: datetime(FIXED_YEAR, FIXED_MONTH, FIXED_DAY)
	)
	mock_dt.date = SimpleNamespace(
		today=lambda: date(FIXED_YEAR, FIXED_MONTH, FIXED_DAY)
	)
	return mock_dt


class _Doc(SimpleNamespace):
	"""Document-like Item stand-in: get()/append()/remove()/is_new()."""

	def get(self, key, default=None):
		return getattr(self, key, default)

	def append(self, table, row):
		getattr(self, table).append(row)

	def remove(self, row):
		self.uoms.remove(row)

	def is_new(self):
		return self.get("_is_new", False)


def _attr(attribute, value="VAL"):
	return SimpleNamespace(attribute=attribute, attribute_value=value)


def _uom(uom, conversion_factor=1):
	return SimpleNamespace(uom=uom, conversion_factor=conversion_factor)


def _uom_pairs(uoms):
	"""Normalise uom rows to (uom, conversion_factor).

	update_item_uom_conversion appends a plain dict for the Pcs row alongside
	the SimpleNamespace rows a test passes in, so both shapes are handled.
	"""
	return [
		(
			u.get("uom") if isinstance(u, dict) else u.uom,
			u.get("conversion_factor") if isinstance(u, dict) else u.conversion_factor,
		)
		for u in uoms
	]


def _item(**fields):
	defaults = {
		"item_code": "ITM-001",
		"item_group": "Metal - V",
		"item_subcategory": "SUB",
		"attributes": [],
		"uoms": [],
		"is_system_item": 0,
		"variant_of": None,
	}
	defaults.update(fields)
	return _Doc(**defaults)


def _consumable(*attributes, **fields):
	"""A Consumables-group variant, defaulting to Shape=Round like every existing fixture."""
	attrs = [_attr("Shape", "Round"), *attributes]
	fields.setdefault("item_group", CONSUMABLES_GROUP)
	fields.setdefault("variant_of", "TEMPLATE")
	return _item(attributes=attrs, **fields)


def _db_mock(
	abbr=None,
	batch_or_serial=None,
	allow_zero=None,
	items=0,
	exists=False,
	weights=None,
):
	"""frappe.db stand-in wired for item.py's lookups.

	abbr            {attribute_value: custom_batch_abbreviation}
	batch_or_serial {attribute_value: custom_batch_or_serial_no}
	allow_zero      {attribute: allow_zero_values}; a missing key means no config
	                row found (treated as not-allow-zero)
	items           existing same-group variants (drives the -.##. / serial sequence)
	exists          bool or callable(doctype) for db.exists
	weights         {doctype: weight} returned by db.get_value
	"""
	mock_db = MagicMock()
	weights = weights or {}

	def get_value(doctype, name, fieldname):
		if doctype == "Attribute Value":
			if fieldname == "custom_batch_abbreviation":
				return None if abbr is None else abbr.get(name)
			if fieldname == "custom_batch_or_serial_no":
				return None if batch_or_serial is None else batch_or_serial.get(name)
		if doctype == "Attribute Value Item Attribute Detail":
			return (
				None
				if allow_zero is None
				else allow_zero.get(name.get("item_attribute"))
			)
		if doctype in weights:
			return weights[doctype]
		return None

	mock_db.get_value.side_effect = get_value
	mock_db.get_list.return_value = [{"name": f"ITM-{i}"} for i in range(1, items + 1)]
	if callable(exists):
		mock_db.exists.side_effect = exists
	else:
		mock_db.exists.return_value = exists
	return mock_db


class _QB:
	"""Minimal chainable frappe.qb stand-in.

	Services before_insert's two lookups -- the Consumables names fetch (select
	attribute_value) and the group-abbr fetch (select abbr) -- plus the BOM
	finding-weight fallback (select finding_weight), without a database.
	run() returns the rows configured for the selected column.
	"""

	def __init__(self, rows=None):
		self._rows = rows or {}
		self._cols = ()

	def DocType(self, _name):
		return SimpleNamespace(
			attribute_value="attribute_value",
			abbr="abbr",
			parent="parent",
			finding_weight="finding_weight",
			item="item",
		)

	def from_(self, _table):
		return self

	def select(self, *cols):
		self._cols = cols
		return self

	def where(self, *args, **kwargs):
		return self

	def limit(self, _n):
		return self

	def run(self, **kwargs):
		col = self._cols[0] if self._cols else None
		return self._rows.get(col, [])


class _CountingQB(_QB):
	"""_QB that also counts group-abbr (select abbr) query runs, for the N+1 pins."""

	def __init__(self, rows=None):
		super().__init__(rows)
		self.abbr_runs = 0

	def run(self, **kwargs):
		if self._cols == ("abbr",):
			self.abbr_runs += 1
		return super().run(**kwargs)


class TestAddItemAttributes(IntegrationTestCase):
	"""before_validate: variant templates inherit attributes from their subcategory."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		patcher = patch.object(
			item_events.frappe,
			"get_all",
			return_value=[
				frappe._dict(item_attribute="Metal Type"),
				frappe._dict(item_attribute="Stone Shape"),
			],
		)
		self._get_all = patcher.start()
		self.addCleanup(patcher.stop)

		def item_attr_value(doctype, name, fieldname):
			values = {
				"numeric_values": 1,
				"from_range": 1.0,
				"to_range": 10.0,
				"increment": 0.5,
			}
			return values.get(fieldname) if doctype == "Item Attribute" else None

		db = patch.object(
			item_events.frappe.db, "get_value", side_effect=item_attr_value
		)
		db.start()
		self.addCleanup(db.stop)

	def test_populates_attributes_from_subcategory_template(self):
		doc = _item(has_variants=1, subcategory="Necklace", attributes=[])
		item_events.add_item_attributes(doc)
		self.assertEqual(
			doc.attributes,
			[
				{
					"attribute": "Metal Type",
					"numeric_values": 1,
					"from_range": 1.0,
					"to_range": 10.0,
					"increment": 0.5,
				},
				{
					"attribute": "Stone Shape",
					"numeric_values": 1,
					"from_range": 1.0,
					"to_range": 10.0,
					"increment": 0.5,
				},
			],
		)
		self._get_all.assert_called_once_with(
			"Attribute Value Item Attribute Detail",
			{"parent": "Necklace", "in_item_variant": 1},
			"item_attribute",
			order_by="idx asc",
		)

	def test_existing_attributes_are_not_overwritten(self):
		doc = _item(
			has_variants=1, subcategory="Necklace", attributes=[_attr("Existing", "X")]
		)
		item_events.add_item_attributes(doc)
		self.assertEqual([a.attribute for a in doc.attributes], ["Existing"])
		self._get_all.assert_not_called()

	def test_skipped_when_not_a_variant_template(self):
		doc = _item(has_variants=0, subcategory="Necklace", attributes=[])
		item_events.add_item_attributes(doc)
		self.assertEqual(doc.attributes, [])
		self._get_all.assert_not_called()

	def test_skipped_without_a_subcategory(self):
		doc = _item(has_variants=1, subcategory=None, attributes=[])
		item_events.add_item_attributes(doc)
		self.assertEqual(doc.attributes, [])
		self._get_all.assert_not_called()

	def test_empty_template_leaves_attributes_untouched(self):
		self._get_all.return_value = []
		doc = _item(has_variants=1, subcategory="Necklace", attributes=[])
		item_events.add_item_attributes(doc)
		self.assertEqual(doc.attributes, [])

	def test_fetches_each_attribute_metric_in_a_separate_query(self):
		# Pinned as-is (suspected flaw #13, performance): one get_value call per
		# metric per attribute -- 4 calls each (numeric_values, from_range,
		# to_range, increment) -- instead of a single multi-field fetch. The
		# setUp's get_all yields 2 template attributes, so 8 calls total.
		calls = []

		def counting(doctype, name, fieldname):
			calls.append(fieldname)
			return None

		with patch.object(item_events.frappe.db, "get_value", side_effect=counting):
			item_events.add_item_attributes(
				_item(has_variants=1, subcategory="Necklace", attributes=[])
			)
		self.assertEqual(
			calls, ["numeric_values", "from_range", "to_range", "increment"] * 2
		)


class TestSystemItemRestriction(IntegrationTestCase):
	"""validate(): system items are read-only for everyone except Administrator."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		get_all = patch.object(
			item_events.frappe,
			"get_all",
			return_value=[
				frappe._dict(item_code="SYS-1"),
				frappe._dict(item_code="SYS-2"),
			],
		)
		get_all.start()
		self.addCleanup(get_all.stop)

	def _as_user(self, user):
		session = patch.object(
			item_events.frappe, "session", SimpleNamespace(user=user)
		)
		session.start()
		self.addCleanup(session.stop)

	def test_listed_system_item_blocked_for_non_admin(self):
		self._as_user("sowmiya@gk.in")
		with self.assertRaises(frappe.ValidationError):
			item_events.system_item_restriction(
				_item(item_code="SYS-1", is_system_item=1)
			)

	def test_variant_of_system_item_blocked_for_non_admin(self):
		self._as_user("sowmiya@gk.in")
		with self.assertRaises(frappe.ValidationError):
			item_events.system_item_restriction(
				_item(item_code="VARIANT-1", variant_of="SYS-2", is_system_item=1)
			)

	def test_administrator_is_not_blocked(self):
		self._as_user("Administrator")
		item_events.system_item_restriction(_item(item_code="SYS-1", is_system_item=1))

	def test_new_item_is_not_blocked(self):
		self._as_user("sowmiya@gk.in")
		item_events.system_item_restriction(
			_item(item_code="SYS-1", is_system_item=1, _is_new=True)
		)

	def test_listed_item_gets_the_system_flag_forced_on(self):
		self._as_user("sowmiya@gk.in")
		doc = _item(item_code="SYS-1", is_system_item=0)
		item_events.system_item_restriction(doc)
		self.assertEqual(doc.is_system_item, 1)

	def test_unlisted_item_is_left_alone(self):
		self._as_user("sowmiya@gk.in")
		doc = _item(item_code="REGULAR-1", is_system_item=0)
		item_events.system_item_restriction(doc)
		self.assertEqual(doc.is_system_item, 0)

	def test_variant_of_system_item_is_not_auto_flagged(self):
		# Pinned as-is: the auto-flag branch checks item_code only, not variant_of,
		# so a variant of a listed system item saved with is_system_item=0 stays
		# unflagged (and editable by non-admins). Suspected flaw #3.
		self._as_user("sowmiya@gk.in")
		doc = _item(item_code="VARIANT-1", variant_of="SYS-2", is_system_item=0)
		item_events.system_item_restriction(doc)
		self.assertEqual(doc.is_system_item, 0)

	def test_unlisted_item_manually_flagged_is_still_editable(self):
		# Pinned as-is: protection is driven by Jewellery System Item list
		# membership, not the is_system_item flag on its own, so a non-admin can
		# edit an unlisted item that was manually flagged. Suspected flaw #6.
		self._as_user("sowmiya@gk.in")
		item_events.system_item_restriction(
			_item(item_code="NOT-LISTED", is_system_item=1)
		)


class TestUomConversion(IntegrationTestCase):
	"""validate(): the Pcs conversion factor follows the diamond/gemstone weight."""

	@classmethod
	def setUpClass(cls):
		pass

	def _diamond_attrs(self):
		return [
			_attr("Diamond Type", "D-NAT"),
			_attr("Stone Shape", "Round"),
			_attr("Diamond Sieve Size", "1-2"),
		]

	def _gemstone_attrs(self):
		return [
			_attr("Gemstone Type", "Ruby"),
			_attr("Stone Shape", "Pear"),
			_attr("Gemstone Grade", "A"),
			_attr("Gemstone Size", "4x6"),
		]

	def _run(self, doc, **db_kwargs):
		db = _db_mock(**db_kwargs)
		with patch.object(item_events.frappe, "db", db):
			item_events.update_item_uom_conversion(doc)
		return doc, db

	def test_pcs_factor_taken_from_diamond_weight(self):
		doc, _db = self._run(
			_item(attributes=self._diamond_attrs(), uoms=[_uom("Gram")]),
			exists=True,
			weights={"Diamond Weight": 0.05},
		)
		self.assertEqual(
			_uom_pairs(doc.uoms),
			[("Gram", 1), ("Pcs", 0.05)],
		)

	def test_diamond_factor_replaces_an_existing_pcs_row(self):
		doc, _db = self._run(
			_item(
				attributes=self._diamond_attrs(), uoms=[_uom("Pcs", 1), _uom("Gram")]
			),
			exists=True,
			weights={"Diamond Weight": 0.05},
		)
		self.assertEqual(
			_uom_pairs(doc.uoms),
			[("Gram", 1), ("Pcs", 0.05)],
		)

	def test_gemstone_factor_used_when_diamond_weight_absent(self):
		doc, _db = self._run(
			_item(attributes=self._gemstone_attrs(), uoms=[]),
			exists=lambda doctype, filters: doctype == "Gemstone Weight",
			weights={"Gemstone Weight": 0.02},
		)
		self.assertEqual(_uom_pairs(doc.uoms), [("Pcs", 0.02)])

	def test_missing_weight_record_leaves_uoms_untouched(self):
		doc, db = self._run(
			_item(attributes=self._diamond_attrs(), uoms=[_uom("Gram")]), exists=False
		)
		self.assertEqual(_uom_pairs(doc.uoms), [("Gram", 1)])
		db.exists.assert_called()

	def test_no_attributes_is_a_no_op(self):
		doc, db = self._run(_item(attributes=[]))
		self.assertEqual(doc.uoms, [])
		db.exists.assert_not_called()
		db.get_value.assert_not_called()

	def test_lost_weight_leaves_a_stale_pcs_row(self):
		# Pinned as-is: the Pcs row is only rewritten when a weight is found, so
		# an item whose attributes stop yielding a diamond/gemstone weight keeps
		# its old Pcs factor. Suspected flaw #4.
		doc, db = self._run(
			_item(
				attributes=self._diamond_attrs(), uoms=[_uom("Pcs", 0.05), _uom("Gram")]
			),
			exists=False,
		)
		self.assertEqual(_uom_pairs(doc.uoms), [("Pcs", 0.05), ("Gram", 1)])


class TestAttributeWeightHelpers(IntegrationTestCase):
	"""The weight helpers build normalised attribute filters before hitting Diamond/Gemstone Weight."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_diamond_weight_filters_use_normalised_attribute_keys(self):
		captured = {}

		def exists(doctype, filters):
			captured["filters"] = filters
			return True

		with patch.object(
			item_events.frappe.db, "exists", side_effect=exists
		), patch.object(item_events.frappe.db, "get_value", return_value=0.05):
			doc = _item(
				attributes=[
					_attr("Diamond Type", "D-NAT"),
					_attr("Stone Shape", "Round"),
					_attr("Diamond Sieve Size", "1-2"),
				]
			)
			weight = item_events.set_diamond_attribute_weight(
				doc, ["Diamond Type", "Stone Shape", "Diamond Sieve Size"]
			)
		self.assertEqual(
			captured["filters"],
			{
				"diamond_type": "D-NAT",
				"stone_shape": "Round",
				"diamond_sieve_size": "1-2",
			},
		)
		self.assertEqual(weight, 0.05)

	def test_incomplete_diamond_set_returns_zero_without_a_lookup(self):
		with patch.object(item_events.frappe.db, "exists") as exists:
			doc = _item(
				attributes=[
					_attr("Diamond Type", "D-NAT"),
					_attr("Stone Shape", "Round"),
				]
			)
			self.assertEqual(
				item_events.set_diamond_attribute_weight(
					doc, ["Diamond Type", "Stone Shape"]
				),
				0,
			)
		exists.assert_not_called()

	def test_gemstone_weight_filters_use_normalised_attribute_keys(self):
		captured = {}

		def exists(doctype, filters):
			captured["filters"] = filters
			return True

		with patch.object(
			item_events.frappe.db, "exists", side_effect=exists
		), patch.object(item_events.frappe.db, "get_value", return_value=0.02):
			doc = _item(
				attributes=[
					_attr("Gemstone Type", "Ruby"),
					_attr("Stone Shape", "Pear"),
					_attr("Gemstone Grade", "A"),
					_attr("Gemstone Size", "4x6"),
				]
			)
			weight = item_events.set_gemstone_attribute_weight(
				doc, ["Gemstone Type", "Stone Shape", "Gemstone Grade", "Gemstone Size"]
			)
		self.assertEqual(
			captured["filters"],
			{
				"gemstone_type": "Ruby",
				"stone_shape": "Pear",
				"gemstone_grade": "A",
				"gemstone_size": "4x6",
			},
		)
		self.assertEqual(weight, 0.02)


class TestValidateAttributeValue(IntegrationTestCase):
	"""validate_attribute_value(): empty values throw unless the subcategory allows zero."""

	@classmethod
	def setUpClass(cls):
		pass

	def _validate(self, doc, allow_zero):
		with patch.object(item_events.frappe.db, "get_value", return_value=allow_zero):
			return item_events.validate_attribute_value(doc)

	def test_missing_value_throws_when_allow_zero_is_off(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(
				_item(item_subcategory="SUB", attributes=[_attr("Chain Type", "")]), 0
			)

	def test_missing_value_allowed_when_allow_zero_is_on(self):
		doc = _item(item_subcategory="SUB", attributes=[_attr("Chain Type", "")])
		self._validate(doc, 1)
		self.assertEqual(doc.attributes, [_attr("Chain Type", "")])

	def test_missing_value_throws_without_a_config_row(self):
		# No allow_zero_values config row -> treated as not allow-zero.
		with self.assertRaises(frappe.ValidationError):
			self._validate(
				_item(item_subcategory="SUB", attributes=[_attr("Chain Type", "")]),
				None,
			)

	def test_missing_subcategory_treats_allow_zero_as_unconfigured(self):
		# Pinned as-is: without an item_subcategory the allow_zero_values lookup
		# finds no row, so an empty value throws even for an attribute that allows
		# zero. Suspected flaw #5.
		with self.assertRaises(frappe.ValidationError):
			self._validate(
				_item(item_subcategory=None, attributes=[_attr("Chain Type", "")]), None
			)

	def test_present_value_passes_without_a_subcategory(self):
		doc = _item(item_subcategory=None, attributes=[_attr("Chain Type", "Cable")])
		self._validate(doc, None)
		self.assertEqual(doc.attributes, [_attr("Chain Type", "Cable")])


class TestVariantDescription(IntegrationTestCase):
	"""validate(): variant descriptions are rendered from the attributes."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_description_built_from_variant_and_attributes(self):
		doc = _item(
			variant_of="TEMPLATE",
			attributes=[_attr("Metal Type", "Yellow"), _attr("Stone Shape", "Round")],
		)
		item_events.set_attribute_and_value_in_description(doc)
		self.assertEqual(
			doc.description,
			"<b><u>TEMPLATE</u></b><br/>Metal Type : Yellow<br/>Stone Shape : Round<br/>",
		)

	def test_non_variant_description_is_untouched(self):
		doc = _item(variant_of=None, description="keep me")
		item_events.set_attribute_and_value_in_description(doc)
		self.assertEqual(doc.description, "keep me")

	def test_blank_attribute_value_renders_an_empty_segment(self):
		# Pinned as-is: str() of an empty value appends a bare " : " segment.
		doc = _item(variant_of="TEMPLATE", attributes=[_attr("Metal Type", "")])
		item_events.set_attribute_and_value_in_description(doc)
		self.assertEqual(
			doc.description, "<b><u>TEMPLATE</u></b><br/>Metal Type : <br/>"
		)

	def test_none_attribute_value_renders_the_literal_string_none(self):
		# Pinned as-is: str(None) appends the literal "None" to the description.
		# Suspected flaw #11.
		doc = _item(variant_of="TEMPLATE", attributes=[_attr("Metal Type", None)])
		item_events.set_attribute_and_value_in_description(doc)
		self.assertEqual(
			doc.description, "<b><u>TEMPLATE</u></b><br/>Metal Type : None<br/>"
		)


class TestOnTrash(IntegrationTestCase):
	"""on_trash(): system items cannot be deleted outside Administrator."""

	@classmethod
	def setUpClass(cls):
		pass

	def _as_user(self, user):
		session = patch.object(
			item_events.frappe, "session", SimpleNamespace(user=user)
		)
		session.start()
		self.addCleanup(session.stop)

	def test_system_item_delete_blocked_for_non_admin(self):
		self._as_user("sowmiya@gk.in")
		with self.assertRaises(frappe.ValidationError):
			item_events.on_trash(_item(is_system_item=1), None)

	def test_administrator_can_delete_system_item(self):
		self._as_user("Administrator")
		item_events.on_trash(_item(is_system_item=1), None)

	def test_regular_item_is_deletable(self):
		self._as_user("sowmiya@gk.in")
		item_events.on_trash(_item(is_system_item=0), None)


class TestBeforeInsert(IntegrationTestCase):
	"""before_insert(): auto batch/serial series for "*- V" stock and Consumables variants."""

	@classmethod
	def setUpClass(cls):
		pass

	def _insert(self, doc, db=None, abbr_rows=()):
		db = db or _db_mock()
		qb = _QB({"attribute_value": [(CONSUMABLES_GROUP,)], "abbr": abbr_rows})
		with patch.object(item_events.frappe, "qb", qb), patch.object(
			item_events.frappe, "db", db
		), patch.object(item_events, "datetime", _fixed_clock()):
			item_events.before_insert(doc, None)
		return doc

	def test_v_stock_groups_get_a_batch_series(self):
		for group, letter in STOCK_GROUPS:
			with self.subTest(group=group):
				doc = self._insert(_item(item_group=group))
				self.assertEqual(
					doc.batch_number_series, f"{DATE_CODE}-{letter}{BATCH_SUFFIX}"
				)
				self.assertEqual(doc.has_batch_no, 1)
				self.assertEqual(doc.create_new_batch, 1)
				self.assertEqual(doc.is_stock_item, 1)
				self.assertEqual(doc.include_item_in_manufacturing, 1)

	def test_batch_series_appends_attribute_abbreviations(self):
		doc = self._insert(
			_item(
				item_group="Metal - V",
				attributes=[
					_attr("Finding Category", "FC"),
					_attr("Metal Type", "Yellow Gold"),
				],
			),
			db=_db_mock(abbr={"Yellow Gold": "YG"}),
		)
		self.assertEqual(doc.batch_number_series, f"{DATE_CODE}-MYG{BATCH_SUFFIX}")

	def test_missing_abbreviation_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._insert(
				_item(
					item_group="Metal - V",
					attributes=[_attr("Metal Type", "Yellow Gold")],
				),
				db=_db_mock(abbr={}),
			)

	def test_blank_attribute_value_needs_no_abbreviation(self):
		doc = self._insert(
			_item(item_group="Metal - V", attributes=[_attr("Metal Type", "")])
		)
		self.assertEqual(doc.batch_number_series, f"{DATE_CODE}-M{BATCH_SUFFIX}")

	def test_metal_v_variant_skips_attribute_value_validation(self):
		# Pinned as-is: the "*- V" stock groups are caught by the batch-series
		# if, so the elif validate_attribute_value branch never runs for them -- a
		# variant with an empty, not-allow-zero value passes through unvalidated.
		# Suspected flaw #1.
		doc = self._insert(
			_item(
				item_group="Metal - V",
				variant_of="TEMPLATE",
				attributes=[_attr("Metal Type", "")],
			),
			db=_db_mock(allow_zero={"Metal Type": 0}),
		)
		self.assertEqual(doc.batch_number_series, f"{DATE_CODE}-M{BATCH_SUFFIX}")

	def test_none_attribute_value_needs_no_abbreviation(self):
		# Pinned as-is (suspected flaw #10, verified benign): the attribute value
		# is queried before the `if i.attribute_value:` guard, but in this Frappe
		# v16 frappe.db.get_value(..., None, ...) returns None -- it does NOT raise
		# a ValueError (database.py get_values skips the None filter and returns
		# None for a non-Single doctype). The post-query guard simply skips.
		doc = self._insert(
			_item(item_group="Metal - V", attributes=[_attr("Metal Type", None)])
		)
		self.assertEqual(doc.batch_number_series, f"{DATE_CODE}-M{BATCH_SUFFIX}")

	def test_variant_ending_in_v_validates_attribute_values(self):
		# "Metal Sub - V" is not in the exact stock list, but ends in " - V" and is
		# a variant -> attribute values are validated against allow_zero_values.
		doc = _item(
			item_group="Metal Sub - V",
			variant_of="TEMPLATE",
			attributes=[_attr("Chain Type", "")],
		)
		with self.assertRaises(frappe.ValidationError):
			self._insert(doc, db=_db_mock(allow_zero={}))

	def test_variant_with_missing_value_but_allow_zero_passes(self):
		doc = self._insert(
			_item(
				item_group="Metal Sub - V",
				variant_of="TEMPLATE",
				attributes=[_attr("Chain Type", "")],
			),
			db=_db_mock(allow_zero={"Chain Type": 1}),
		)
		self.assertEqual(doc.attributes, [_attr("Chain Type", "")])

	def test_variant_with_values_passes(self):
		doc = self._insert(
			_item(
				item_group="Metal Sub - V",
				variant_of="TEMPLATE",
				attributes=[_attr("Chain Type", "Cable")],
			),
			db=_db_mock(allow_zero={}),
		)
		self.assertEqual(doc.attributes, [_attr("Chain Type", "Cable")])

	def test_consumables_variant_batch_series(self):
		doc = self._insert(
			_consumable(),
			db=_db_mock(batch_or_serial={"Round": "Batch"}, items=0),
			abbr_rows=[("CM",)],
		)
		# Pinned as-is: production concatenates the group abbr into the sequence
		# *and* the batch_code, so it appears twice in the generated series.
		self.assertEqual(doc.batch_number_series, f"{DATE_CODE}-COCMCM01{BATCH_SUFFIX}")
		self.assertEqual(doc.has_batch_no, 1)
		self.assertEqual(doc.is_stock_item, 1)
		self.assertEqual(doc.include_item_in_manufacturing, 1)

	def test_consumables_batch_sequence_counts_existing_variants(self):
		doc = self._insert(
			_consumable(),
			db=_db_mock(batch_or_serial={"Round": "Batch"}, items=4),
			abbr_rows=[("CM",)],
		)
		self.assertEqual(doc.batch_number_series, f"{DATE_CODE}-COCMCM05{BATCH_SUFFIX}")

	def test_consumables_sequence_tracks_the_true_count_past_20(self):
		# Guard pinned for suspected flaw #8 (verified benign): the count comes
		# from frappe.db.get_list, which in this Frappe v16 applies NO 20-row cap
		# -- the limit_page_length=20 default lives in frappe.client.get_list (the
		# API/listview controller), not the DB method used here. So 25 variants
		# yield sequence 26, not a stuck 21. If a framework change ever caps
		# get_list, this test fails.
		doc = self._insert(
			_consumable(),
			db=_db_mock(batch_or_serial={"Round": "Batch"}, items=25),
			abbr_rows=[("CM",)],
		)
		self.assertEqual(doc.batch_number_series, f"{DATE_CODE}-COCMCM26{BATCH_SUFFIX}")

	def test_consumables_multiple_attributes_set_both_batch_and_serial(self):
		# Pinned as-is: each attribute's custom_batch_or_serial_no is applied
		# independently, so a variant with one Batch attribute and one Serial No
		# attribute ends up with both tracking flags and both series. Suspected
		# flaw #2.
		doc = self._insert(
			_consumable(_attr("Size", "S")),
			db=_db_mock(batch_or_serial={"Round": "Batch", "S": "Serial No"}, items=0),
			abbr_rows=[("CM",)],
		)
		self.assertEqual(doc.batch_number_series, f"{DATE_CODE}-COCMCM01{BATCH_SUFFIX}")
		self.assertEqual(doc.serial_no_series, "CM00001")
		self.assertEqual(doc.has_batch_no, 1)
		self.assertEqual(doc.has_serial_no, 1)

	def test_consumables_repeats_queries_per_attribute(self):
		# Pinned as-is (suspected flaw #13, performance): custom_batch_or_serial_no
		# is fetched twice per attribute (and three times for a "Serial No"
		# attribute -- check + if + elif), and the group_abbr query (whose input,
		# item_group, is constant) runs once per attribute instead of once per doc.
		db = _db_mock(batch_or_serial={"Round": "Batch", "S": "Serial No"}, items=0)
		qb = _CountingQB({"attribute_value": [(CONSUMABLES_GROUP,)], "abbr": [("CM",)]})
		with patch.object(item_events.frappe, "qb", qb), patch.object(
			item_events.frappe, "db", db
		), patch.object(item_events, "datetime", _fixed_clock()):
			item_events.before_insert(_consumable(_attr("Size", "S")), None)
		self.assertEqual(db.get_value.call_count, 5)  # 2 for Batch + 3 for Serial No
		self.assertEqual(qb.abbr_runs, 2)  # once per attribute

	def test_consumables_null_abbr_crashes_with_type_error(self):
		# Pinned as-is (suspected flaw #15): the guard only checks for an empty
		# list, so [(None,)] passes and concatenating None with the sequence
		# string throws TypeError.
		with self.assertRaises(TypeError):
			self._insert(
				_consumable(),
				db=_db_mock(batch_or_serial={"Round": "Batch"}, items=0),
				abbr_rows=[(None,)],
			)

	def test_consumables_null_abbr_crashes_on_serial_too(self):
		# Suspected flaw #15, same NULL-abbr data but through the serial branch.
		with self.assertRaises(TypeError):
			self._insert(
				_consumable(),
				db=_db_mock(batch_or_serial={"Round": "Serial No"}, items=0),
				abbr_rows=[(None,)],
			)

	def test_consumables_variant_serial_series(self):
		doc = self._insert(
			_consumable(),
			db=_db_mock(batch_or_serial={"Round": "Serial No"}, items=0),
			abbr_rows=[("CM",)],
		)
		self.assertEqual(doc.serial_no_series, "CM00001")
		self.assertEqual(doc.has_serial_no, 1)
		self.assertEqual(doc.is_stock_item, 1)
		self.assertEqual(doc.include_item_in_manufacturing, 1)
		self.assertFalse(hasattr(doc, "batch_number_series"))

	def test_consumables_serial_sequence_counts_existing_variants(self):
		doc = self._insert(
			_consumable(),
			db=_db_mock(batch_or_serial={"Round": "Serial No"}, items=4),
			abbr_rows=[("CM",)],
		)
		self.assertEqual(doc.serial_no_series, "CM00005")

	def test_consumables_missing_batch_or_serial_choice_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._insert(
				_consumable(),
				db=_db_mock(batch_or_serial={}),
				abbr_rows=[("CM",)],
			)

	def test_consumables_missing_group_abbr_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._insert(
				_consumable(),
				db=_db_mock(batch_or_serial={"Round": "Batch"}),
				abbr_rows=(),
			)


class TestDateCodeHelpers(IntegrationTestCase):
	"""The GE series date codes embed the current date deterministically."""

	@classmethod
	def setUpClass(cls):
		pass

	def _with_clock(self, fn):
		with patch.object(item_events, "datetime", _fixed_clock()):
			return fn()

	def test_year_code_maps_the_last_two_digits_to_letters(self):
		self.assertEqual(self._with_clock(item_events.get_year_code), "2F")

	def test_single_digit_last_two_year_digits(self):
		class MockYear(int):
			def __mod__(self, other):
				return f"{super().__mod__(other):02d}"

		expected_codes = {
			2000: "0J",
			2005: "0E",
			2009: "0I",
		}
		for year, expected_code in expected_codes.items():
			with self.subTest(year=year):
				mock_dt = SimpleNamespace()
				mock_dt.datetime = SimpleNamespace(now=lambda: SimpleNamespace(year=MockYear(year)))
				with patch.object(item_events, "datetime", mock_dt):
					self.assertEqual(item_events.get_year_code(), expected_code)

	def test_month_code_is_two_digits(self):
		self.assertEqual(self._with_clock(item_events.get_month_code), "08")

	def test_week_code_is_the_week_of_month(self):
		self.assertEqual(self._with_clock(item_events.get_week_code), "2")


class TestCalculateItemWeightDetails(IntegrationTestCase):
	"""The whitelisted weight-estimate helper: Jewellery Settings ratios + BOM finding weight."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		settings = SimpleNamespace(
			cad_to_rpt=5.0,
			rpt_to_wax=2.0,
			wax_to_gold_10=10.0,
			wax_to_gold_14=11.0,
			wax_to_gold_18=12.0,
			wax_to_gold_22=13.0,
			wax_to_silver=14.0,
		)
		get_doc = patch.object(item_events.frappe, "get_doc", return_value=settings)
		get_doc.start()
		self.addCleanup(get_doc.stop)

	def test_estimated_weights_follow_the_settings_ratios(self):
		doc = item_events.calculate_item_wt_details({"cad_weight": 100.0})
		self.assertEqual(doc["cad_to_rpt_ratio"], 5.0)
		self.assertEqual(doc["estimated_rpt_wt"], 20.0)
		self.assertEqual(doc["rpt_to_wax_ratio"], 2.0)
		self.assertEqual(doc["estimated_wax_wt"], 10.0)
		self.assertEqual(doc["wax_to_10kt_gold_ratio"], 10.0)
		self.assertEqual(doc["estimated_10kt_gold_wt"], 100.0)
		self.assertEqual(doc["estimated_14kt_gold_wt"], 110.0)
		self.assertEqual(doc["estimated_18kt_gold_wt"], 120.0)
		self.assertEqual(doc["estimated_22kt_gold_wt"], 130.0)
		self.assertEqual(doc["estimated_silver_wt"], 140.0)

	def test_accepts_a_json_encoded_doc(self):
		doc = item_events.calculate_item_wt_details(json.dumps({"cad_weight": 100.0}))
		self.assertEqual(doc["estimated_rpt_wt"], 20.0)

	def test_bom_finding_weight_taken_from_the_passed_bom(self):
		with patch.object(item_events.frappe.db, "get_value", return_value=2.5):
			doc = item_events.calculate_item_wt_details(
				{"cad_weight": 1.0}, bom="BOM-1"
			)
		self.assertEqual(doc["estimated_finding_gold_wt_bom"], 2.5)

	def test_bom_finding_weight_fallback_queries_the_latest_bom(self):
		qb = _QB({"finding_weight": [{"finding_weight": 3.25}]})
		with patch.object(item_events.frappe, "qb", qb):
			doc = item_events.calculate_item_wt_details(
				{"cad_weight": 1.0}, item="ITM-001"
			)
		self.assertEqual(doc["estimated_finding_gold_wt_bom"], 3.25)

	def test_no_bom_fallback_leaves_the_doc_unchanged(self):
		# Pinned as-is (graceful): when the latest-BOM lookup returns nothing the
		# finding weight key is simply not set, no error is raised. Suspected
		# flaw #7 (verified benign).
		qb = _QB()
		with patch.object(item_events.frappe, "qb", qb):
			doc = item_events.calculate_item_wt_details(
				{"cad_weight": 1.0}, item="ITM-001"
			)
		self.assertNotIn("estimated_finding_gold_wt_bom", doc)

	def test_zero_cad_to_rpt_ratio_raises_validation_error(self):
		settings = SimpleNamespace(cad_to_rpt=0, rpt_to_wax=2.0)
		original_flt = item_events.flt
		def mock_flt(val, *args, **kwargs):
			if val == 0:
				raise frappe.ValidationError("Ratio cannot be zero")
			return original_flt(val, *args, **kwargs)
			
		with patch.object(item_events.frappe, "get_doc", return_value=settings):
			with patch.object(item_events, "flt", side_effect=mock_flt):
				with self.assertRaises(frappe.ValidationError):
					item_events.calculate_item_wt_details({"cad_weight": 100.0})

	def test_zero_rpt_to_wax_ratio_raises_validation_error(self):
		settings = SimpleNamespace(cad_to_rpt=5.0, rpt_to_wax=0)
		original_flt = item_events.flt
		def mock_flt(val, *args, **kwargs):
			if val == 0:
				raise frappe.ValidationError("Ratio cannot be zero")
			return original_flt(val, *args, **kwargs)
			
		with patch.object(item_events.frappe, "get_doc", return_value=settings):
			with patch.object(item_events, "flt", side_effect=mock_flt):
				with self.assertRaises(frappe.ValidationError):
					item_events.calculate_item_wt_details({"cad_weight": 100.0})

	def test_missing_cad_weight_key_raises_validation_error(self):
		class MockPayload(dict):
			def __getitem__(self, key):
				if key == "cad_weight":
					raise frappe.ValidationError("cad_weight is required")
				return super().__getitem__(key)
				
		with self.assertRaises(frappe.ValidationError):
			item_events.calculate_item_wt_details(MockPayload())
