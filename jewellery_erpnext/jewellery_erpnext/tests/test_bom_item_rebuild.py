"""Deterministic BOM hook regressions; database readers are simulated.

Run with unittest or bench run-tests. These test real hook functions with named,
explicit fixtures, and make no claim about SQL plans or full document saves.
"""

import itertools
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from jewellery_erpnext.jewellery_erpnext.doc_events import bom as bom_events


class BomFixture(SimpleNamespace):
	def __init__(self, **values):
		defaults = {
			"bom_type": "Sales Order",
			"customer": "Customer A",
			"items": [],
			"metal_detail": [],
			"diamond_detail": [],
			"gemstone_detail": [],
			"finding_detail": [],
			"other_detail": [],
			"doc_pricision": 3,
			"diamond_pricision": 3,
			"gemstone_pricision": 3,
		}
		defaults.update(values)
		super().__init__(**defaults)
		self.writes = []

	def append(self, field, values):
		row = frappe._dict(values)
		getattr(self, field).append(row)
		return row

	def remove(self, row):
		self.items.remove(row)

	def get(self, field, default=None):
		return getattr(self, field, default)

	def db_set(self, field, value=None):
		values = field if isinstance(field, dict) else {field: value}
		self.writes.append(dict(values))
		for key, val in values.items():
			setattr(self, key, val)


class TestBOMItemRebuild(TestCase):
	def setUp(self):
		def throw(message):
			raise ValueError(message)

		self.items = {
			"DEFAULT": frappe._dict(name="DEFAULT", stock_uom="Nos", variant_of=None),
			"GOLD": frappe._dict(name="GOLD", stock_uom="Gram", variant_of="METAL"),
			"CORD": frappe._dict(name="CORD", stock_uom="Meter", variant_of=None),
			"BOX": frappe._dict(name="BOX", stock_uom="Nos", variant_of=None),
		}
		self.customer = frappe._dict(
			custom_consider_2_digit_for_bom=0,
			custom_consider_2_digit_for_diamond=0,
			custom_consider_2_digit_for_gemstone=0,
		)
		self.default_item = "DEFAULT"
		self.db = SimpleNamespace(
			get_value=Mock(side_effect=self.get_value),
			get_single_value=Mock(side_effect=lambda *args: self.default_item),
		)
		self.get_all = Mock(side_effect=self.read_all)
		self.patches = [
			patch.object(bom_events.frappe, "db", self.db),
			patch.object(bom_events.frappe, "get_all", self.get_all),
			patch.object(bom_events, "_", lambda text: text),
			patch.object(bom_events.frappe, "throw", side_effect=throw),
		]
		for patched in self.patches:
			patched.start()
			self.addCleanup(patched.stop)

	def get_value(self, doctype, name, fields, **kwargs):
		row = self.items.get(name) if doctype == "Item" else self.customer
		if row is None:
			return None
		if isinstance(fields, str):
			return row.get(fields)
		return frappe._dict({field: row.get(field) for field in fields})

	def read_all(self, doctype, filters=None, fields=None, **kwargs):
		if doctype == "Jewellery System Item":
			return [frappe._dict(item_code="METAL")]
		if doctype != "Item":
			raise AssertionError(f"Unexpected DocType {doctype}")
		return [self.items[name] for name in filters["name"][1] if name in self.items]

	def rebuild(self, doc):
		bom_events._set_bom_items_by_child_tables(doc, "DEFAULT")

	def test_other_only_uses_each_rows_stock_uom(self):
		doc = BomFixture(
			other_detail=[
				frappe._dict(item_code="CORD", quantity=2.125),
				frappe._dict(item_code="BOX", quantity=3),
			]
		)
		self.rebuild(doc)
		self.assertEqual(
			[(r.item_code, r.qty, r.uom, r.rate) for r in doc.items],
			[
				("CORD", 2.125, "Meter", 0),
				("BOX", 3, "Nos", 0),
			],
		)

	def test_other_row_does_not_inherit_material_uom(self):
		doc = BomFixture(
			metal_detail=[frappe._dict(item_variant="GOLD", quantity=0.001)],
			other_detail=[frappe._dict(item_code="CORD", quantity=2.5)],
		)
		self.rebuild(doc)
		self.assertEqual(
			[(r.item_code, r.qty, r.uom) for r in doc.items],
			[
				("GOLD", 0.001, "Gram"),
				("CORD", 2.5, "Meter"),
			],
		)

	def test_material_rebuild_preserves_manual_row_metadata(self):
		manual = frappe._dict(
			item_code="BOX", qty=4, uom="Box", rate=9, operation="Packing"
		)
		doc = BomFixture(
			items=[
				frappe._dict(item_code="DEFAULT"),
				frappe._dict(item_code="GOLD"),
				manual,
			],
			metal_detail=[frappe._dict(item_variant="GOLD", quantity=2.125)],
		)
		self.rebuild(doc)
		self.assertIs(doc.items[0], manual)
		self.assertEqual(
			dict(manual),
			{
				"item_code": "BOX",
				"qty": 4,
				"uom": "Box",
				"rate": 9,
				"operation": "Packing",
			},
		)
		self.assertEqual(
			[(r.item_code, r.qty) for r in doc.items], [("BOX", 4), ("GOLD", 2.125)]
		)

	def test_empty_source_tables_leave_existing_manual_rows_alone(self):
		manual = frappe._dict(item_code="BOX", qty=3, uom="Nos", rate=4)
		doc = BomFixture(items=[manual])
		self.rebuild(doc)
		self.assertEqual(doc.items, [manual])
		self.get_all.assert_not_called()

	def test_missing_item_metadata_stays_missing_for_standard_link_validation(self):
		doc = BomFixture(
			metal_detail=[frappe._dict(item_variant="UNKNOWN", quantity=1)],
		)
		self.rebuild(doc)
		self.assertEqual(doc.items[0].item_code, "UNKNOWN")
		self.assertIsNone(doc.items[0].uom)

	def test_null_uom_does_not_borrow_another_items_uom(self):
		self.items["CORD"].stock_uom = None
		doc = BomFixture(
			metal_detail=[frappe._dict(item_variant="GOLD", quantity=1)],
			other_detail=[frappe._dict(item_code="CORD", quantity=2)],
		)
		self.rebuild(doc)
		self.assertIsNone(doc.items[-1].uom)

	def test_noncanonical_link_spelling_keeps_database_lookup_semantics(self):
		original = self.db.get_value.side_effect

		def get_value(doctype, name, fields, **kwargs):
			return original(
				doctype, "GOLD" if name == "gold" else name, fields, **kwargs
			)

		self.db.get_value.side_effect = get_value
		doc = BomFixture(metal_detail=[frappe._dict(item_variant="gold", quantity=2)])
		self.rebuild(doc)
		self.assertEqual(
			[(r.item_code, r.qty, r.uom) for r in doc.items], [("gold", 2, "Gram")]
		)

	def test_metadata_is_refreshed_on_next_rebuild(self):
		for uom in ["Meter", "Foot"]:
			self.items["CORD"].stock_uom = uom
			doc = BomFixture(other_detail=[frappe._dict(item_code="CORD", quantity=1)])
			self.rebuild(doc)
			self.assertEqual(doc.items[0].uom, uom)

	def test_repeated_variant_in_one_table_carries_total_quantity(self):
		doc = BomFixture(
			metal_detail=[
				frappe._dict(item_variant="GOLD", quantity=2),
				frappe._dict(item_variant="GOLD", quantity=3),
			]
		)
		self.rebuild(doc)
		self.assertEqual(
			[(r.item_code, r.qty, r.uom) for r in doc.items], [("GOLD", 5, "Gram")]
		)

	def test_variant_repeated_across_tables_carries_total_quantity(self):
		doc = BomFixture(
			metal_detail=[frappe._dict(item_variant="GOLD", quantity=2)],
			finding_detail=[frappe._dict(item_variant="GOLD", quantity=3)],
		)
		self.rebuild(doc)
		self.assertEqual([(r.item_code, r.qty) for r in doc.items], [("GOLD", 5)])

	def test_repeated_small_weights_are_not_lost(self):
		doc = BomFixture(
			metal_detail=[
				frappe._dict(item_variant="GOLD", quantity=0.001),
				frappe._dict(item_variant="GOLD", quantity=0.002),
			]
		)
		self.rebuild(doc)
		self.assertAlmostEqual(doc.items[0].qty, 0.003, places=9)

	def test_repeated_variants_keep_first_seen_order(self):
		self.items["DIA"] = frappe._dict(
			name="DIA", stock_uom="Carat", variant_of="METAL"
		)
		doc = BomFixture(
			metal_detail=[
				frappe._dict(item_variant="GOLD", quantity=1),
				frappe._dict(item_variant="GOLD", quantity=1.5),
			],
			diamond_detail=[
				frappe._dict(item_variant="DIA", quantity=0.25),
				frappe._dict(item_variant="DIA", quantity=0),
			],
		)
		self.rebuild(doc)
		self.assertEqual(
			[(r.item_code, r.qty, r.uom) for r in doc.items],
			[("GOLD", 2.5, "Gram"), ("DIA", 0.25, "Carat")],
		)

	def test_repeated_rebuild_keeps_one_row_per_other_detail_row(self):
		doc = BomFixture(
			metal_detail=[frappe._dict(item_variant="GOLD", quantity=2)],
			other_detail=[frappe._dict(item_code="CORD", quantity=3)],
		)
		for _ in range(3):
			self.rebuild(doc)
			# ERPNext costing writes a valuation rate onto stock rows on save.
			for row in doc.items:
				row.rate = 12.5
		self.assertEqual(
			[(r.item_code, r.qty) for r in doc.items], [("GOLD", 2), ("CORD", 3)]
		)

	def test_other_only_repeated_rebuild_does_not_duplicate(self):
		doc = BomFixture(
			other_detail=[
				frappe._dict(item_code="CORD", quantity=2),
				frappe._dict(item_code="CORD", quantity=1),
			]
		)
		for _ in range(3):
			self.rebuild(doc)
		self.assertEqual(
			[(r.item_code, r.qty) for r in doc.items], [("CORD", 2), ("CORD", 1)]
		)

	def test_other_detail_rebuild_keeps_rows_for_other_items(self):
		manual = frappe._dict(item_code="BOX", qty=4, uom="Nos", rate=9)
		doc = BomFixture(
			items=[manual, frappe._dict(item_code="CORD", qty=7, rate=1)],
			other_detail=[frappe._dict(item_code="CORD", quantity=3)],
		)
		self.rebuild(doc)
		self.assertIs(doc.items[0], manual)
		self.assertEqual(
			[(r.item_code, r.qty) for r in doc.items], [("BOX", 4), ("CORD", 3)]
		)

	def test_repeated_item_metadata_is_read_once_per_call(self):
		doc = BomFixture(
			items=[frappe._dict(item_code="GOLD") for _ in range(40)],
			metal_detail=[frappe._dict(item_variant="GOLD", quantity=2)],
			other_detail=[
				frappe._dict(item_code="CORD", quantity=1) for _ in range(40)
			],
		)
		self.rebuild(doc)
		reads = [c for c in self.get_all.call_args_list if c.args[0] == "Item"]
		self.assertEqual(len(reads), 1)
		self.assertEqual(set(reads[0].kwargs["filters"]["name"][1]), {"GOLD", "CORD"})
		self.assertEqual(
			set(reads[0].kwargs["fields"]), {"name", "stock_uom", "variant_of"}
		)
		self.assertFalse(
			[c for c in self.db.get_value.call_args_list if c.args[0] == "Item"]
		)

	def test_many_unique_items_use_bounded_reads_without_truncation(self):
		details = []
		for index in range(451):
			name = f"METAL-{index:03d}"
			self.items[name] = frappe._dict(
				name=name, stock_uom="Gram", variant_of="METAL"
			)
			details.append(frappe._dict(item_variant=name, quantity=0.001))
		doc = BomFixture(metal_detail=details)
		self.rebuild(doc)
		self.assertEqual(len(doc.items), 451)
		self.assertEqual(
			[r.item_code for r in doc.items], [r.item_variant for r in details]
		)
		reads = [c for c in self.get_all.call_args_list if c.args[0] == "Item"]
		self.assertEqual(len(reads), 3)
		self.assertLessEqual(
			max(len(c.kwargs["filters"]["name"][1]) for c in reads), 200
		)

	def test_template_and_quotation_use_configured_default(self):
		for bom_type in ["Template", "Quotation"]:
			with self.subTest(bom_type=bom_type):
				doc = BomFixture(
					bom_type=bom_type, items=[frappe._dict(item_code="GOLD")]
				)
				bom_events.set_bom_items(doc)
				self.assertEqual(
					[dict(row) for row in doc.items],
					[{"item_code": "DEFAULT", "qty": 1, "uom": "Nos", "rate": 0}],
				)

	def test_missing_default_configuration_fails_before_clearing_items(self):
		self.default_item = None
		for bom_type in ["Template", "Quotation"]:
			with self.subTest(bom_type=bom_type):
				manual = frappe._dict(item_code="GOLD")
				doc = BomFixture(bom_type=bom_type, items=[manual])
				with self.assertRaisesRegex(ValueError, "Jewellery Settings"):
					bom_events.set_bom_items(doc)
				self.assertEqual(doc.items, [manual])

	def test_missing_configured_default_item_is_actionable(self):
		self.default_item = "MISSING"
		doc = BomFixture(bom_type="Quotation")
		with self.assertRaisesRegex(ValueError, "MISSING"):
			bom_events.set_bom_items(doc)
		self.assertEqual(doc.items, [])

	def test_default_item_without_stock_uom_fails_before_clearing_items(self):
		self.items["DEFAULT"].stock_uom = None
		manual = frappe._dict(item_code="GOLD")
		doc = BomFixture(bom_type="Quotation", items=[manual])
		with self.assertRaisesRegex(ValueError, "Stock UOM"):
			bom_events.set_bom_items(doc)
		self.assertEqual(doc.items, [manual])

	def test_normal_bom_does_not_require_default_item_configuration(self):
		self.default_item = None
		doc = BomFixture(metal_detail=[frappe._dict(item_variant="GOLD", quantity=2)])
		bom_events.set_bom_items(doc)
		self.assertEqual([(r.item_code, r.qty) for r in doc.items], [("GOLD", 2)])

	def test_precision_flags_are_independent_and_reset_to_three(self):
		for flags in itertools.product([0, 1], repeat=3):
			with self.subTest(flags=flags):
				for name, flag in zip(self.customer, flags, strict=True):
					self.customer[name] = flag
				doc = BomFixture(
					doc_pricision=2, diamond_pricision=2, gemstone_pricision=2
				)
				with (
					patch.object(bom_events, "set_bom_rate") as rates,
					patch.object(bom_events, "calculate_total") as totals,
				):
					bom_events.on_update_after_submit(doc, "on_update_after_submit")
				self.assertEqual(
					(doc.doc_pricision, doc.diamond_pricision, doc.gemstone_pricision),
					tuple(2 if flag else 3 for flag in flags),
				)
				rates.assert_called_once_with(doc)
				totals.assert_called_once_with(doc)

	def test_no_customer_resets_precision_without_reading_arbitrary_customer(self):
		doc = BomFixture(
			customer=None, doc_pricision=2, diamond_pricision=2, gemstone_pricision=2
		)
		with (
			patch.object(bom_events, "set_bom_rate"),
			patch.object(bom_events, "calculate_total"),
		):
			bom_events.on_update_after_submit(doc, "on_update_after_submit")
		self.assertEqual(
			(doc.doc_pricision, doc.diamond_pricision, doc.gemstone_pricision),
			(3, 3, 3),
		)
		self.db.get_value.assert_not_called()

	def test_customer_precision_uses_one_read_and_one_coherent_write(self):
		self.customer.custom_consider_2_digit_for_bom = 1
		self.customer.custom_consider_2_digit_for_gemstone = 1
		doc = BomFixture()
		with (
			patch.object(bom_events, "set_bom_rate"),
			patch.object(bom_events, "calculate_total"),
		):
			bom_events.on_update_after_submit(doc, "on_update_after_submit")
		self.db.get_value.assert_called_once()
		self.assertEqual(
			doc.writes,
			[{"doc_pricision": 2, "diamond_pricision": 3, "gemstone_pricision": 2}],
		)

	def test_missing_linked_customer_does_not_leak_another_customers_precision(self):
		self.customer = None
		doc = BomFixture(doc_pricision=2, diamond_pricision=2, gemstone_pricision=2)
		with (
			patch.object(bom_events, "set_bom_rate"),
			patch.object(bom_events, "calculate_total"),
		):
			bom_events.on_update_after_submit(doc, "on_update_after_submit")
		self.assertEqual(
			(doc.doc_pricision, doc.diamond_pricision, doc.gemstone_pricision),
			(3, 3, 3),
		)

	def test_precision_reset_reaches_real_rates_and_weight_totals(self):
		class PricingBomFixture(BomFixture):
			def db_set(self, field, value=None):
				super().db_set(field, value)
				# Frappe db.set_value adds these keys to a dictionary passed by
				# Document.db_set; set_bom_rate removes them before summing rates.
				if isinstance(field, dict):
					field.update(modified="fixture", modified_by="Administrator")

		original_get_value = self.db.get_value.side_effect

		def read_master(doctype, name, fields, **kwargs):
			if doctype == "Customer" and fields == "diamond_price_list":
				return "Sieve Size Range"
			if doctype == "Attribute Value":
				return frappe._dict(sieve_size_range="RANGE-A", diameter=1)
			if doctype == "Gemstone Multiplier":
				return 2 if name["parentfield"] == "gemstone_multiplier" else 1
			return original_get_value(doctype, name, fields, **kwargs)

		def read_price(doctype, **kwargs):
			if doctype == "Diamond Price List":
				return [frappe._dict(rate=100, supplier_fg_purchase_rate=80)]
			if doctype == "Gemstone Price List":
				return [frappe._dict(name="GEM-PRICE")]
			raise AssertionError(f"Unexpected price master {doctype}")

		self.db.get_value.side_effect = read_master
		doc = PricingBomFixture(item="BOX", selling_price_list="Selling")
		# Each transition receives fresh input quantities: changing precision
		# cannot reconstruct fractions already rounded away by an earlier save.
		for diamond_flag, gemstone_flag in [(1, 1), (0, 0), (1, 0), (0, 1)]:
			with self.subTest(diamond_flag=diamond_flag, gemstone_flag=gemstone_flag):
				self.customer.custom_consider_2_digit_for_diamond = diamond_flag
				self.customer.custom_consider_2_digit_for_gemstone = gemstone_flag
				doc.diamond_detail = [
					frappe._dict(
						quantity=quantity,
						pcs=1,
						diamond_sieve_size="SIZE-A",
						sieve_size_range="RANGE-A",
						diamond_type="Natural",
						stone_shape="Round",
						quality="A",
						is_customer_item=0,
					)
					for quantity in [0.001, 1.234, 1.235, 1.236]
				]
				doc.gemstone_detail = [
					frappe._dict(
						quantity=quantity,
						pcs=1,
						price_list_type="Multiplier",
						gemstone_pr="10",
						gemstone_quality="AA",
						gemstone_size="1",
						size_weight=1,
						size_height=1,
						is_customer_item=0,
					)
					for quantity in [0.001, 1.234, 1.235, 1.236]
				]
				with (
					patch.object(frappe, "get_list", side_effect=read_price),
					patch.object(
						frappe,
						"get_system_settings",
						return_value="Commercial Rounding",
					),
				):
					bom_events.on_update_after_submit(doc, "on_update_after_submit")
				expected_diamond = (
					[0, 1.23, 1.24, 1.24]
					if diamond_flag
					else [0.001, 1.234, 1.235, 1.236]
				)
				expected_gemstone = (
					[0, 1.23, 1.24, 1.24]
					if gemstone_flag
					else [0.001, 1.234, 1.235, 1.236]
				)
				self.assertEqual(
					[row.quantity for row in doc.diamond_detail], expected_diamond
				)
				self.assertEqual(
					[row.quantity for row in doc.gemstone_detail], expected_gemstone
				)
				# Independent master arithmetic: diamond 100/ct; gemstone
				# multiplier 2 * PR 10 = 20/ct; 5 carats = 1 gram.
				self.assertEqual(
					[row.total_diamond_rate for row in doc.diamond_detail], [100] * 4
				)
				self.assertEqual(
					[row.total_gemstone_rate for row in doc.gemstone_detail], [20] * 4
				)
				self.assertEqual(doc.diamond_weight, sum(expected_diamond))
				self.assertEqual(doc.gemstone_weight, sum(expected_gemstone))
				self.assertEqual(
					doc.total_diamond_weight_in_gms, sum(expected_diamond) / 5
				)
				self.assertEqual(
					doc.total_gemstone_weight_in_gms, sum(expected_gemstone) / 5
				)
				self.assertAlmostEqual(
					doc.total_bom_amount,
					sum(quantity * 100 for quantity in expected_diamond)
					+ sum(quantity * 20 for quantity in expected_gemstone),
					places=12,  # floating-point summation only; no business rounding tolerance
				)
