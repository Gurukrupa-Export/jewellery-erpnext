# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for create_finished_goods_bom carat-gram rounding / gross_weight.

The FG BOM's carat->gram weights must be rounded ONCE per family via carat_to_gram
(never the historical raw ``x / 5``) and the gross_weight total rounded once at the
end -- the same round-of-sum mop_log.update_wt_detail and sync_mwo_weights use. The
Serial No's ``custom_gross_wt`` (fetch_from: custom_bom_no.gross_weight) mirrors this
value, so an unrounded BOM drifted the serial +0.001 from the MWO MOP it matches.

Regression: piece MWO-KGJPL-NE05395-003-1-01 -- diamond 4.256 ct -> 0.851 g (not
0.8512), gross 26.798 + 3.550 + 0.851 = 31.199 (not 31.1992).
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	create_finished_goods_bom,
)
from jewellery_erpnext.utils import carat_to_gram

ROUNDING = "Banker's Rounding (legacy)"


class _FlagHolder:
	pass


class _FakeBom:
	"""Minimal Document stand-in: header fields + child-table append/get.

	append() wraps each row in frappe._dict so the KG cost sums that read
	``row.se_rate`` / ``row.quantity`` by attribute work, as on a real table row.
	"""

	def __init__(self, initial=None):
		object.__setattr__(self, "_fields", dict(initial or {}))
		object.__setattr__(self, "flags", _FlagHolder())

	def __getattr__(self, name):
		fields = object.__getattribute__(self, "_fields")
		if name not in fields:
			raise AttributeError(name)
		return fields[name]

	def __setattr__(self, name, value):
		object.__getattribute__(self, "_fields")[name] = value

	def get(self, field, default=None):
		return object.__getattribute__(self, "_fields").get(field, default)

	def append(self, child, row):
		object.__getattribute__(self, "_fields").setdefault(child, []).append(
			frappe._dict(row)
		)

	def insert(self, *args, **kwargs):
		return self

	def submit(self, *args, **kwargs):
		return self


class _SncDoc:
	"""Serial Number Creator stand-in (self for the builder)."""

	def __init__(self, fg_details):
		self.doctype = "Serial Number Creator"
		self.company = "KG GK Jewellers Private Limited"
		self.name = "SNC-001"
		self.parent_manufacturing_order = None
		self.new_item = None
		self.design_id_bom = "BOM-DESIGN"
		self.fg_details = fg_details
		self.fg_bom = None
		self.db_set = Mock()

	def get(self, key, default=None):
		return getattr(self, key, default)


def _fg_row(item_code, qty):
	"""A prepared fg_details row: weight (grams for M/F, carats for D/G) lands in qty."""
	return SimpleNamespace(
		row_material=item_code,
		qty=qty,
		pcs=0,
		uom="Gram",
		rate=None,
		sub_setting_type=None,
	)


class TestFgBomGrossWeightRounding(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _new_bom(self):
		# The builder reads customer/setting_type/item_subcategory before setting them;
		# seed them empty (never touched because price-list lookups return []).
		return _FakeBom(
			{
				"name": "BOM-FG-0001",
				"customer": None,
				"setting_type": None,
				"item_subcategory": None,
				"item_category": None,
				"item": None,
				# Computed inside the gemstone branch, so a gemstone-less build
				# leaves them unset on a fresh copy_doc -- seed the template defaults.
				"gemstone_bom_amount": 0.0,
				"gemstone_fg_purchase": 0.0,
			}
		)

	def _items(self):
		items = {
			code: SimpleNamespace(
				name=code,
				custom_variant_of=None,
				variant_of=code,
				attributes=[],
				item_group="X",
				valuation_rate=0,
			)
			for code in ("M-GOLD", "F-FND", "G-STONE", "O-OTH")
		}
		# The D-branch only binds sieve_size_mm while scanning the item's attributes
		# for "Diamond Sieve Size", so diamond items must carry that attribute here.
		items["D-DIA"] = SimpleNamespace(
			name="D-DIA",
			custom_variant_of=None,
			variant_of="D-DIA",
			attributes=[
				SimpleNamespace(
					attribute="Diamond Sieve Size", attribute_value="2.0 MM"
				)
			],
			item_group="X",
			valuation_rate=0,
		)
		return items

	def _run_builder(self, *fg_rows):
		new_bom = self._new_bom()
		items = self._items()
		design_bom = SimpleNamespace(name="BOM-DESIGN")

		def fake_get_doc(doctype, name=None, *args, **kwargs):
			if doctype == "BOM":
				return design_bom
			if doctype == "Item":
				return items[name]
			return None

		def fake_get_value(doctype, *args, **kwargs):
			# Frappe's Meta.load_from_db reads a doctype as get_value("DocType", name, "*")
			# with the name in the filters kwarg; the builder's reads are all positional
			# (doctype, name, fieldname). Return None for anything that isn't one of ours.
			if not args and kwargs.get("filters"):
				return None
			fieldname = kwargs.get("fieldname", args[1] if len(args) > 1 else None)
			if doctype == "DocType" or fieldname is None or fieldname == "*":
				return None
			if isinstance(fieldname, list):
				# PMO lookup (as_dict): qty 1 keeps item rows unscaled.
				return {
					"qty": 1,
					"diamond_quality": None,
					"sales_order": None,
					"quotation": None,
					"customer": "CUST-REF",
				}
			if doctype == "Parent Manufacturing Order":
				return "CUST-REF"  # ref_customer
			if doctype == "Customer":
				if fieldname == "custom_gemstone_price_list_type":
					return "GEM-NONE"
				return "NONE"  # diamond_price_list
			return None

		def fake_get_all(doctype, *args, **kwargs):
			return []

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation._snc_se_detail_maps",
			return_value=({}, {}),
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_serial_no",
			return_value="SN-001",
		), patch.object(frappe.db, "sql", return_value=[]), patch.object(
			frappe.db, "get_value", side_effect=fake_get_value
		), patch.object(frappe.db, "exists", return_value=False), patch.object(
			frappe.db, "get_all", side_effect=fake_get_all
		), patch.object(frappe, "get_all", side_effect=fake_get_all), patch.object(
			frappe.db, "set_value"
		), patch.object(frappe, "get_doc", side_effect=fake_get_doc), patch.object(
			frappe, "copy_doc", return_value=new_bom
		), patch.object(frappe, "get_system_settings", return_value=ROUNDING):
			create_finished_goods_bom(_SncDoc(list(fg_rows)), "SE-001", None)

		return new_bom

	def _pinned(self, fn):
		with patch.object(frappe, "get_system_settings", return_value=ROUNDING):
			return fn()

	def test_carat_to_gram_rounding_and_gross_of_the_reported_piece(self):
		"""4.256 ct -> 0.851 g (0.2/ct), gross 26.798 + 3.550 + 0.851 = 31.199.

		The old builder stored total_diamond_weight_in_gms = 4.256 / 5 = 0.8512 and an
		unrounded sum 31.1992; carat_to_gram rounds the conversion once and flt(..., 3)
		rounds the total once.
		"""
		bom = self._run_builder(
			_fg_row("D-DIA", 4.256),
			_fg_row("M-GOLD", 26.798),
			_fg_row("F-FND", 3.550),
		)

		self.assertAlmostEqual(bom.total_diamond_weight_in_gms, 0.851, places=3)
		self.assertAlmostEqual(bom.total_gemstone_weight_in_gms, 0.0, places=3)
		self.assertAlmostEqual(bom.gross_weight, 31.199, places=3)
		self.assertNotEqual(bom.gross_weight, 31.1992)

	def test_gross_weight_is_round_of_sum_for_all_families(self):
		"""Each carat family converts once; gross = metal + finding + grams + other."""
		bom = self._run_builder(
			_fg_row("M-GOLD", 2.273),
			_fg_row("F-FND", 2.334),
			_fg_row("D-DIA", 1.0),
			_fg_row("G-STONE", 2.0),
			_fg_row("O-OTH", 0.5),
		)

		grams_d = self._pinned(lambda: carat_to_gram(1.0))
		grams_g = self._pinned(lambda: carat_to_gram(2.0))
		expected = self._pinned(lambda: flt(2.273 + 2.334 + 0.5 + grams_d + grams_g, 3))

		self.assertAlmostEqual(bom.total_diamond_weight_in_gms, grams_d, places=3)
		self.assertAlmostEqual(bom.total_gemstone_weight_in_gms, grams_g, places=3)
		self.assertAlmostEqual(bom.other_weight, 0.5, places=3)
		self.assertAlmostEqual(bom.gross_weight, expected, places=3)
