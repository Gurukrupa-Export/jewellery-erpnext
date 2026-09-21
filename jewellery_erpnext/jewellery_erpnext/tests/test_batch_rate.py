# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Batch Rate stamping for newly created batches.

A new Batch must carry the rate of the voucher row that created it -- Stock Entry
Detail ``basic_rate``, or Purchase Receipt Item ``rate`` -- and a batch received
against a customer's supplier must be labelled Customer Subcontracting.

DB-free per the suite convention: ``setUpClass`` is neutralized and the logic runs
against ``SimpleNamespace`` docs with ``frappe.db`` mocked.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting import batch_rename
from jewellery_erpnext.jewellery_erpnext.customization.batch import (
	batch as batch_module,
)
from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events import (
	utils as batch_utils,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils import (
	party_link as party_link_utils,
)


def _batch(**fields):
	defaults = {
		"item": "M-G-24KT-Y",
		"reference_doctype": "Stock Entry",
		"reference_name": "GE-SE-26-00001",
		"custom_voucher_detail_no": "ROW-1",
		"custom_metal_rate": 0,
		"custom_alloy_rate": 0,
		"custom_customer": None,
		"custom_customer_voucher_type": None,
		"custom_inventory_type": None,
		"custom_employee": None,
		"batch_qty": 0,
		"name": "BATCH-NEW",
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


def _db(values, missing_columns=()):
	"""A frappe.db stand-in resolving get_value from a (doctype, fieldname) map.

	``update_inventory_dimentions`` walks the reference doctype's Table fields, so
	``get_all`` returns the one child table and ``exists`` confirms the row.
	``missing_columns`` names fields whose column does not exist on the child
	table, so ``has_column`` reports them absent the way a real site would.
	"""
	db = MagicMock()
	db.has_column.side_effect = lambda doctype, column: column not in missing_columns
	db.get_all.side_effect = lambda doctype, filters=None, fields=None, **kw: (
		[]
		if doctype == "Item Group" and (filters or {}).get("custom_is_alloy_group")
		else [SimpleNamespace(options=values["__child_doctype__"])]
		if doctype == "DocField"
		else []
	)
	db.exists.return_value = True

	def get_value(doctype, name=None, fieldname=None, **kw):
		return values.get((doctype, fieldname))

	db.get_value.side_effect = get_value
	return db


class TestBatchRateStamping(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	# --- Requirement A: Stock Entry -> basic_rate ---------------------------------

	def test_stock_entry_batch_takes_basic_rate(self):
		batch = _batch()
		values = {
			"__child_doctype__": "Stock Entry Detail",
			("Stock Entry Detail", "inventory_type"): "Regular Stock",
			("Stock Entry Detail", "customer"): None,
			("Stock Entry Detail", "employee"): None,
			("Item Variant Attribute", "attribute_value"): "24KT",
			("Attribute Value", "is_metal_type"): 1,
			# The produce row mints the batch, so its fetched custom_metal_rate is
			# still empty -- the value must come from basic_rate.
			("Stock Entry Detail", "custom_metal_rate"): None,
			("Stock Entry Detail", "basic_rate"): 6120.5,
			("Item", "custom_inventory_type_can_be_customer_goods"): 1,
		}
		with patch.object(batch_utils.frappe, "db", _db(values)):
			batch_utils.update_inventory_dimentions(batch)

		self.assertEqual(batch.custom_metal_rate, 6120.5)

	def test_stock_entry_prefers_maintained_rate_over_basic_rate(self):
		batch = _batch()
		values = {
			"__child_doctype__": "Stock Entry Detail",
			("Stock Entry Detail", "inventory_type"): "Regular Stock",
			("Stock Entry Detail", "customer"): None,
			("Stock Entry Detail", "employee"): None,
			("Item Variant Attribute", "attribute_value"): "24KT",
			("Attribute Value", "is_metal_type"): 1,
			("Stock Entry Detail", "custom_metal_rate"): 5900,
			("Stock Entry Detail", "basic_rate"): 6120.5,
			("Item", "custom_inventory_type_can_be_customer_goods"): 1,
		}
		with patch.object(batch_utils.frappe, "db", _db(values)):
			batch_utils.update_inventory_dimentions(batch)

		self.assertEqual(batch.custom_metal_rate, 5900)

	# --- Requirement B: Purchase Receipt -> rate ----------------------------------

	def test_purchase_receipt_batch_takes_item_rate(self):
		batch = _batch(
			reference_doctype="Purchase Receipt", reference_name="GE-PR-26-00007"
		)
		values = {
			"__child_doctype__": "Purchase Receipt Item",
			("Purchase Receipt Item", "inventory_type"): "Regular Stock",
			("Purchase Receipt Item", "customer"): None,
			("Purchase Receipt Item", "employee"): None,
			("Item Variant Attribute", "attribute_value"): "24KT",
			("Attribute Value", "is_metal_type"): 1,
			("Purchase Receipt Item", "rate"): 7420,
			("Item", "custom_inventory_type_can_be_customer_goods"): 1,
		}
		with patch.object(batch_utils.frappe, "db", _db(values)):
			batch_utils.update_inventory_dimentions(batch)

		self.assertEqual(batch.custom_metal_rate, 7420)

	def test_purchase_receipt_survives_missing_employee_column(self):
		# Purchase Receipt Item has no `employee` column (Stock Entry Detail does).
		# An unguarded read raised MariaDB 1054 inside Batch.validate and aborted the
		# whole Purchase Receipt submit -- every batch-tracked PR failed on submit.
		batch = _batch(
			reference_doctype="Purchase Receipt", reference_name="PR-26-00004"
		)
		values = {
			"__child_doctype__": "Purchase Receipt Item",
			("Purchase Receipt Item", "inventory_type"): "Regular Stock",
			("Purchase Receipt Item", "customer"): None,
			("Item Variant Attribute", "attribute_value"): "91.9",
			("Attribute Value", "is_metal_type"): 1,
			("Purchase Receipt Item", "rate"): 1345,
			("Item", "custom_inventory_type_can_be_customer_goods"): 1,
		}
		with patch.object(
			batch_utils.frappe, "db", _db(values, missing_columns={"employee"})
		):
			batch_utils.update_inventory_dimentions(batch)

		self.assertEqual(batch.custom_metal_rate, 1345)
		self.assertIsNone(batch.custom_employee)

	def test_missing_rate_column_does_not_abort_the_submit(self):
		batch = _batch(
			reference_doctype="Purchase Receipt", reference_name="PR-26-00004"
		)
		values = {
			"__child_doctype__": "Purchase Receipt Item",
			("Purchase Receipt Item", "inventory_type"): "Regular Stock",
			("Purchase Receipt Item", "customer"): None,
			("Item Variant Attribute", "attribute_value"): "91.9",
			("Attribute Value", "is_metal_type"): 1,
			("Item", "custom_inventory_type_can_be_customer_goods"): 1,
		}
		with patch.object(
			batch_utils.frappe,
			"db",
			_db(values, missing_columns={"employee", "rate"}),
		):
			batch_utils.update_inventory_dimentions(batch)

		self.assertIsNone(batch.custom_metal_rate)

	# --- Requirement C: non-metal items are stamped too ---------------------------

	def test_diamond_item_gets_the_row_rate(self):
		# KGJPL-SE-MR-26-00078: a Material Receipt of D-NT-RO-6B-+00-0 at basic_rate
		# 346 minted batch KG2F075-DNTROX6X20X10-0V2C3 with Batch Rate 0, because a
		# diamond carries no "Metal Type" attribute and the old code only stamped
		# items whose Attribute Value had is_metal_type.
		batch = _batch(item="D-NT-RO-6B-+00-0")
		values = {
			"__child_doctype__": "Stock Entry Detail",
			("Stock Entry Detail", "inventory_type"): "Regular Stock",
			("Stock Entry Detail", "customer"): None,
			("Stock Entry Detail", "employee"): None,
			("Item Variant Attribute", "attribute_value"): None,
			("Attribute Value", "is_metal_type"): 0,
			("Stock Entry Detail", "custom_metal_rate"): None,
			("Stock Entry Detail", "basic_rate"): 346,
			("Item", "custom_inventory_type_can_be_customer_goods"): 1,
		}
		with patch.object(batch_utils.frappe, "db", _db(values)):
			batch_utils.update_inventory_dimentions(batch)

		self.assertEqual(batch.custom_metal_rate, 346)

	def test_diamond_purchase_receipt_gets_the_item_rate(self):
		batch = _batch(
			item="D-NT-RO-6B-+0-2",
			reference_doctype="Purchase Receipt",
			reference_name="GE-PR-26-00011",
		)
		values = {
			"__child_doctype__": "Purchase Receipt Item",
			("Purchase Receipt Item", "inventory_type"): "Regular Stock",
			("Purchase Receipt Item", "customer"): None,
			("Purchase Receipt Item", "employee"): None,
			("Item Variant Attribute", "attribute_value"): None,
			("Attribute Value", "is_metal_type"): 0,
			("Purchase Receipt Item", "rate"): 32050,
			("Item", "custom_inventory_type_can_be_customer_goods"): 1,
		}
		with patch.object(batch_utils.frappe, "db", _db(values)):
			batch_utils.update_inventory_dimentions(batch)

		self.assertEqual(batch.custom_metal_rate, 32050)

	def test_consumable_item_gets_the_row_rate(self):
		# Nothing about a consumable is metal, but a batch with a rate is still
		# better than a batch with 0 -- there is no per-item-group narrowing.
		batch = _batch(item="Garbage Bags")
		values = {
			"__child_doctype__": "Stock Entry Detail",
			("Stock Entry Detail", "inventory_type"): "Regular Stock",
			("Stock Entry Detail", "customer"): None,
			("Stock Entry Detail", "employee"): None,
			("Item Variant Attribute", "attribute_value"): None,
			("Attribute Value", "is_metal_type"): 0,
			("Stock Entry Detail", "custom_metal_rate"): None,
			("Stock Entry Detail", "basic_rate"): 95,
			("Item", "custom_inventory_type_can_be_customer_goods"): 1,
		}
		with patch.object(batch_utils.frappe, "db", _db(values)):
			batch_utils.update_inventory_dimentions(batch)

		self.assertEqual(batch.custom_metal_rate, 95)

	def test_alloy_item_still_goes_to_the_alloy_rate(self):
		# The alloy/metal split is the ONE distinction that survives: batch.on_update
		# blends the two pools separately for a Repack-Metal Conversion, so an alloy
		# rate landing on custom_metal_rate would be double-counted there.
		batch = _batch(item="M-AL")
		values = {
			"__child_doctype__": "Stock Entry Detail",
			("Stock Entry Detail", "inventory_type"): "Regular Stock",
			("Stock Entry Detail", "customer"): None,
			("Stock Entry Detail", "employee"): None,
			("Stock Entry Detail", "custom_alloy_rate"): None,
			("Stock Entry Detail", "basic_rate"): 1000,
			("Item", "custom_inventory_type_can_be_customer_goods"): 1,
		}
		db = _db(values)
		# The alloy lookup is the only get_all("Item", ...) in this function.
		db.get_all.side_effect = lambda doctype, filters=None, fields=None, **kw: (
			["M-AL"]
			if doctype == "Item"
			else ["Alloy"]
			if doctype == "Item Group"
			else [SimpleNamespace(options="Stock Entry Detail")]
			if doctype == "DocField"
			else []
		)
		with patch.object(batch_utils.frappe, "db", db):
			batch_utils.update_inventory_dimentions(batch)

		self.assertEqual(batch.custom_alloy_rate, 1000)
		self.assertFalse(batch.custom_metal_rate)

	# --- A blended conversion rate must survive a later save ----------------------

	def test_existing_rate_is_not_overwritten_on_resave(self):
		# batch.on_update blends custom_metal_rate from custom_origin_entries for a
		# Repack-Metal Conversion; a later save must not reset it to basic_rate.
		batch = _batch(custom_metal_rate=5123.75, is_new=lambda: False)
		values = {
			"__child_doctype__": "Stock Entry Detail",
			("Stock Entry Detail", "inventory_type"): "Regular Stock",
			("Stock Entry Detail", "customer"): None,
			("Stock Entry Detail", "employee"): None,
			("Item Variant Attribute", "attribute_value"): "24KT",
			("Attribute Value", "is_metal_type"): 1,
			("Stock Entry Detail", "custom_metal_rate"): None,
			("Stock Entry Detail", "basic_rate"): 6120.5,
			("Item", "custom_inventory_type_can_be_customer_goods"): 1,
		}
		with patch.object(batch_utils.frappe, "db", _db(values)):
			batch_utils.update_inventory_dimentions(batch)

		self.assertEqual(batch.custom_metal_rate, 5123.75)

	def test_empty_rate_is_still_filled_on_an_existing_batch(self):
		batch = _batch(custom_metal_rate=0, is_new=lambda: False)
		values = {
			"__child_doctype__": "Stock Entry Detail",
			("Stock Entry Detail", "inventory_type"): "Regular Stock",
			("Stock Entry Detail", "customer"): None,
			("Stock Entry Detail", "employee"): None,
			("Item Variant Attribute", "attribute_value"): "24KT",
			("Attribute Value", "is_metal_type"): 1,
			("Stock Entry Detail", "custom_metal_rate"): None,
			("Stock Entry Detail", "basic_rate"): 6120.5,
			("Item", "custom_inventory_type_can_be_customer_goods"): 1,
		}
		with patch.object(batch_utils.frappe, "db", _db(values)):
			batch_utils.update_inventory_dimentions(batch)

		self.assertEqual(batch.custom_metal_rate, 6120.5)


class TestPurchaseReceiptVoucherType(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, batch, supplier="Supplier A", flag=1, primary_party="Customer A"):
		values = {
			("Purchase Receipt", "supplier"): supplier,
			("Supplier", "custom_consider_purchase_receipt_as_customergoods"): flag,
			("Party Link", "primary_party"): primary_party,
		}
		db = MagicMock()
		db.get_value.side_effect = lambda doctype, name=None, fieldname=None, **kw: (
			values.get((doctype, fieldname))
		)
		with patch.object(batch_utils.frappe, "db", db):
			return batch_utils._purchase_receipt_voucher_type(batch)

	def test_flagged_supplier_with_matching_party_link_is_subcontracting(self):
		batch = _batch(
			reference_doctype="Purchase Receipt",
			reference_name="GE-PR-26-00007",
			custom_customer="Customer A",
		)
		self.assertEqual(self._run(batch), "Customer Subcontracting")

	def test_unflagged_supplier_gets_no_voucher_type(self):
		batch = _batch(
			reference_doctype="Purchase Receipt",
			reference_name="GE-PR-26-00007",
			custom_customer="Customer A",
		)
		self.assertIsNone(self._run(batch, flag=0))

	def test_party_link_mismatch_does_not_relabel_the_batch(self):
		# The batch's ownership came from somewhere other than this supplier's
		# linked customer -- it must not be relabelled as their subcontracting stock.
		batch = _batch(
			reference_doctype="Purchase Receipt",
			reference_name="GE-PR-26-00007",
			custom_customer="Customer B",
		)
		self.assertIsNone(self._run(batch, primary_party="Customer A"))

	def test_no_party_link_gets_no_voucher_type(self):
		batch = _batch(
			reference_doctype="Purchase Receipt",
			reference_name="GE-PR-26-00007",
			custom_customer="Customer A",
		)
		self.assertIsNone(self._run(batch, primary_party=None))


class TestPartyLinkOrientation(IntegrationTestCase):
	"""A Party Link is valid in either orientation; both must resolve.

	ERPNext's PartyLink.validate only requires primary_role to be Customer or
	Supplier, so the same pair is stored two ways by the same UI.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _db_with_link(self, link):
		db = MagicMock()

		def get_value(doctype, filters=None, fieldname=None, **kw):
			if doctype != "Party Link" or not isinstance(filters, dict):
				return None
			if all(link.get(k) == v for k, v in filters.items()):
				return link.get(fieldname)
			return None

		db.get_value.side_effect = get_value
		return db

	def test_customer_primary_orientation(self):
		link = {
			"primary_role": "Customer",
			"primary_party": "DLCU0002",
			"secondary_role": "Supplier",
			"secondary_party": "DLSU0006",
		}
		with patch.object(party_link_utils.frappe, "db", self._db_with_link(link)):
			self.assertEqual(
				party_link_utils.get_linked_customer("DLSU0006"), "DLCU0002"
			)

	def test_supplier_primary_orientation(self):
		# Party Link ACC-PT-LNK-022 as it exists on site: the Supplier is primary.
		# The original single-orientation query returned nothing for this shape, so
		# PR-26-00005 booked Regular Stock with a blank customer.
		link = {
			"primary_role": "Supplier",
			"primary_party": "DLSU0006",
			"secondary_role": "Customer",
			"secondary_party": "DLCU0002",
		}
		with patch.object(party_link_utils.frappe, "db", self._db_with_link(link)):
			self.assertEqual(
				party_link_utils.get_linked_customer("DLSU0006"), "DLCU0002"
			)

	def test_supplier_with_no_party_link(self):
		link = {
			"primary_role": "Supplier",
			"primary_party": "OTHER-SUP",
			"secondary_role": "Customer",
			"secondary_party": "DLCU0002",
		}
		with patch.object(party_link_utils.frappe, "db", self._db_with_link(link)):
			self.assertIsNone(party_link_utils.get_linked_customer("DLSU0006"))

	def test_no_supplier_short_circuits(self):
		db = MagicMock()
		with patch.object(party_link_utils.frappe, "db", db):
			self.assertIsNone(party_link_utils.get_linked_customer(None))
		db.get_value.assert_not_called()


class TestSubcontractingBatchRate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_stock_entry_row_rate_falls_back_to_basic_rate(self):
		doc = SimpleNamespace(doctype="Stock Entry")
		row = SimpleNamespace(get=lambda f: {"basic_rate": 6120.5}.get(f))
		self.assertEqual(batch_rename._source_row_rate(doc, row), 6120.5)

	def test_stock_entry_row_prefers_maintained_rate(self):
		doc = SimpleNamespace(doctype="Stock Entry")
		row = SimpleNamespace(
			get=lambda f: {"custom_metal_rate": 5900, "basic_rate": 6120.5}.get(f)
		)
		self.assertEqual(batch_rename._source_row_rate(doc, row), 5900)

	def test_purchase_receipt_row_uses_rate(self):
		doc = SimpleNamespace(doctype="Purchase Receipt")
		row = SimpleNamespace(get=lambda f: {"rate": 7420}.get(f))
		self.assertEqual(batch_rename._source_row_rate(doc, row), 7420)


def _doc(**fields):
	defaults = {"item": "D-NT-RO-6B-+9-9.5"}
	defaults.update(fields)
	return frappe._dict(defaults)


class _Ctx:
	"""Patch the three sources the resolver reads: user default, global default and
	the Company table (``abbr`` lookup + the single-company probe)."""

	def __init__(self, user_default=None, global_default=None, companies=(), abbr=None):
		self.patches = [
			patch.object(
				frappe.defaults, "get_user_default", return_value=user_default
			),
			patch.object(
				frappe.defaults, "get_global_default", return_value=global_default
			),
			patch.object(frappe, "get_all", return_value=list(companies)),
			patch.object(frappe.db, "get_value", return_value=abbr),
		]

	def __enter__(self):
		for p in self.patches:
			p.start()
		return self

	def __exit__(self, *exc):
		for p in self.patches:
			p.stop()


class TestBatchCompanyAbbr(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_group_company_keeps_its_hand_picked_code(self):
		"""The four Gurukrupa companies must never drift to Company.abbr -- 20k+
		existing batch names carry these prefixes."""
		with _Ctx(abbr="GEPL"):
			self.assertEqual(
				batch_module.get_batch_company_abbr(
					_doc(custom_company="Gurukrupa Export Private Limited")
				),
				"GE",
			)
			self.assertEqual(
				batch_module.get_batch_company_abbr(
					_doc(custom_company="Sadguru Hallmarking Centre")
				),
				"SHC",
			)

	def test_batch_company_wins_over_session_default(self):
		"""A caller that knows which company owns the material (Refining Entry sets
		custom_company) must not be overridden by whoever is logged in."""
		with _Ctx(user_default="Gurukrupa Export Private Limited"):
			self.assertEqual(
				batch_module.get_batch_company_abbr(
					_doc(custom_company="KG GK Jewellers Private Limited")
				),
				"KG",
			)

	def test_unmapped_company_falls_back_to_company_abbr(self):
		"""A company nobody added to the map still mints batches -- the alternative is
		aborting the Stock Entry that created it."""
		with _Ctx(abbr="TC"):
			self.assertEqual(
				batch_module.get_batch_company_abbr(
					_doc(custom_company="Test_Company")
				),
				"TC",
			)

	def test_company_abbr_is_sanitized(self):
		"""Batch names are '-' delimited (batch_rename reads the trailing serial), so a
		prefix can never carry a separator."""
		with _Ctx(abbr="T-C 1"):
			self.assertEqual(
				batch_module.get_batch_company_abbr(
					_doc(custom_company="Test_Company")
				),
				"TC1",
			)

	def test_single_company_site_needs_no_default(self):
		"""A fresh bench / CI site has no default company anywhere; with exactly one
		Company there is nothing to disambiguate."""
		with _Ctx(companies=["Test_Company"], abbr="T"):
			self.assertEqual(batch_module.get_batch_company_abbr(_doc()), "T")

	def test_ambiguous_multi_company_site_is_refused(self):
		"""No company anywhere AND several to choose from: guessing would stamp one
		company's material with another's code, which is unrecoverable once the batch
		is transacted."""
		with _Ctx(companies=["Company A", "Company B"]):
			with self.assertRaises(frappe.ValidationError):
				batch_module.get_batch_company_abbr(_doc())


class TestCarryRatesFromSourceBatches(IntegrationTestCase):
	"""Batch Rate for a HAND-BUILT batch, minted before its Stock Entry exists.

	`update_inventory_dimentions` can only stamp a rate from inside
	`if frappe.db.exists(row.options, self.custom_voucher_detail_no)`. A batch created by
	`finding_repack._create_finding_batch` or `manufacturing_operation._create_scrap_batch`
	has no `custom_voucher_detail_no` to resolve, so that stamper never fires -- which is why
	all 241 batches ever minted by a plain `Repack` sat at rate 0. The rate has to be carried
	from the consumed batches explicitly.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _db_with_batches(self, batches):
		"""frappe.db stand-in whose get_value answers Batch rate lookups by batch name."""
		db = MagicMock()
		db.get_value.side_effect = lambda doctype, name=None, fieldname=None, **kw: (
			batches.get(name) if doctype == "Batch" else None
		)
		return db

	def test_single_source_carries_both_pools(self):
		batch = _batch(
			item="F-G-22KT-91.75-Y-SW", custom_metal_rate=0, custom_alloy_rate=0
		)
		db = self._db_with_batches(
			{"SRC-1": {"custom_metal_rate": 14546.9625, "custom_alloy_rate": 62.0}}
		)
		with patch.object(frappe, "db", db):
			batch_utils.carry_rates_from_source_batches(batch, [("SRC-1", 1.23)])
		self.assertAlmostEqual(batch.custom_metal_rate, 14546.9625, places=4)
		self.assertAlmostEqual(batch.custom_alloy_rate, 62.0, places=4)

	def test_several_sources_are_qty_weighted(self):
		batch = _batch(custom_metal_rate=0, custom_alloy_rate=0)
		db = self._db_with_batches(
			{
				"SRC-1": {"custom_metal_rate": 100.0, "custom_alloy_rate": 10.0},
				"SRC-2": {"custom_metal_rate": 200.0, "custom_alloy_rate": 20.0},
			}
		)
		with patch.object(frappe, "db", db):
			batch_utils.carry_rates_from_source_batches(
				batch, [("SRC-1", 1.0), ("SRC-2", 3.0)]
			)
		# (100*1 + 200*3) / 4 = 175 ; (10*1 + 20*3) / 4 = 17.5
		self.assertAlmostEqual(batch.custom_metal_rate, 175.0, places=6)
		self.assertAlmostEqual(batch.custom_alloy_rate, 17.5, places=6)

	def test_unvalued_source_dilutes_rather_than_being_dropped(self):
		"""Same "no invented value" policy loss_valuation states: a 0-valued input must pull
		the result down, not be skipped so the rest looks fully valued."""
		batch = _batch(custom_metal_rate=0, custom_alloy_rate=0)
		db = self._db_with_batches(
			{
				"SRC-1": {"custom_metal_rate": 100.0, "custom_alloy_rate": 0.0},
				"SRC-2": {"custom_metal_rate": 0.0, "custom_alloy_rate": 0.0},
			}
		)
		with patch.object(frappe, "db", db):
			batch_utils.carry_rates_from_source_batches(
				batch, [("SRC-1", 1.0), ("SRC-2", 1.0)]
			)
		self.assertAlmostEqual(batch.custom_metal_rate, 50.0, places=6)

	def test_no_sources_leaves_the_batch_untouched(self):
		batch = _batch(custom_metal_rate=0, custom_alloy_rate=0)
		db = self._db_with_batches({})
		with patch.object(frappe, "db", db):
			batch_utils.carry_rates_from_source_batches(batch, [])
			batch_utils.carry_rates_from_source_batches(batch, None)
			batch_utils.carry_rates_from_source_batches(batch, [(None, 1.0)])
		self.assertEqual(batch.custom_metal_rate, 0)
		self.assertEqual(batch.custom_alloy_rate, 0)

	def test_existing_rate_is_not_clobbered_on_a_later_save(self):
		"""_can_stamp_rate lets a non-zero rate be rewritten only while the batch is new; a
		batch already saved must keep the rate it carries."""
		batch = _batch(custom_metal_rate=999.0, custom_alloy_rate=88.0)
		batch.is_new = lambda: False
		db = self._db_with_batches(
			{"SRC-1": {"custom_metal_rate": 1.0, "custom_alloy_rate": 2.0}}
		)
		with patch.object(frappe, "db", db):
			batch_utils.carry_rates_from_source_batches(batch, [("SRC-1", 1.0)])
		self.assertEqual(batch.custom_metal_rate, 999.0)
		self.assertEqual(batch.custom_alloy_rate, 88.0)

	def test_zero_qty_sources_fall_back_to_a_plain_mean(self):
		"""Guards against ZeroDivisionError when every source came through at qty 0."""
		batch = _batch(custom_metal_rate=0, custom_alloy_rate=0)
		db = self._db_with_batches(
			{
				"SRC-1": {"custom_metal_rate": 100.0, "custom_alloy_rate": 0.0},
				"SRC-2": {"custom_metal_rate": 200.0, "custom_alloy_rate": 0.0},
			}
		)
		with patch.object(frappe, "db", db):
			batch_utils.carry_rates_from_source_batches(
				batch, [("SRC-1", 0), ("SRC-2", 0)]
			)
		self.assertAlmostEqual(batch.custom_metal_rate, 150.0, places=6)


class _BlendBatch:
	"""Batch stand-in for ``batch_module.on_update`` that records every ``db_set``.

	``db_set`` is recorded rather than mocked away because the defect is about a write that
	should not happen at all -- asserting only on the final value would pass even if the code
	wrote 0 and something else put the number back.
	"""

	def __init__(self, **fields):
		self.item = fields.pop("item", "M-G-22KT-91.75-Y")
		self.name = fields.pop("name", "TGT-1")
		self.reference_doctype = fields.pop("reference_doctype", "Stock Entry")
		self.reference_name = fields.pop("reference_name", "MAT-STE-17890")
		self.custom_voucher_detail_no = fields.pop("custom_voucher_detail_no", "ROW-1")
		self.custom_metal_rate = fields.pop("custom_metal_rate", 0.0)
		self.custom_alloy_rate = fields.pop("custom_alloy_rate", 0.0)
		self.custom_origin_entries = fields.pop("custom_origin_entries", [])
		self.flags = frappe._dict(is_update_origin_entries=True)
		self.writes = []
		for key, value in fields.items():
			setattr(self, key, value)

	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)

	def db_set(self, fieldname, value):
		self.writes.append((fieldname, value))
		setattr(self, fieldname, value)


def _origin(batch_no, item_code, qty, rate):
	"""One ``custom_origin_entries`` row, as ``update_parent_batch_id`` froze it."""
	return SimpleNamespace(batch_no=batch_no, item_code=item_code, qty=qty, rate=rate)


_ALLOY_ITEM = "M-Genia-221"


def _run_blend(batch, source_rates, se_type="Repack-Metal Conversion", items=None):
	"""Drive ``batch_module.on_update`` with the site reads stubbed.

	``source_rates`` maps batch name -> {custom_metal_rate, custom_alloy_rate}, i.e. what the
	source Batch masters actually hold today.

	``items`` overrides item facts per item code as ``{item_code: (item_group, attribute_count)}``.
	It exists because the default fixture gives the alloy item BOTH ``item_group == "Alloy"`` AND a
	single attribute, and every other item neither -- so the two arms of ``_is_alloy`` fire together
	and deleting either one is invisible. Real data has each arm alone: gk holds 3 items in the Alloy
	group with no attributes, and 385 one-attribute items outside it.
	"""
	db = MagicMock()
	db.get_value.side_effect = lambda doctype, name=None, fieldname=None, **kw: (
		se_type if fieldname == "stock_entry_type" else None
	)

	def _get_all(doctype, filters=None, fields=None, **kw):
		if doctype != "Batch":
			return []
		wanted = set((filters or {}).get("name", ("in", []))[1])
		return [
			frappe._dict(name=name, **source_rates[name])
			for name in source_rates
			if name in wanted
		]

	facts = items or {}

	def _get_doc(doctype, item_code=None, *a, **kw):
		if item_code in facts:
			group, attribute_count = facts[item_code]
			return SimpleNamespace(
				item_group=group, attributes=list(range(attribute_count))
			)
		if item_code == _ALLOY_ITEM:
			return SimpleNamespace(item_group="Alloy", attributes=[1])
		return SimpleNamespace(item_group="Metal - V", attributes=[1, 2, 3, 4])

	with patch.object(frappe, "db", db), patch.object(
		frappe, "get_all", _get_all
	), patch.object(frappe, "get_doc", _get_doc):
		batch_module.on_update(batch, None)

	return batch


class TestOriginRateBlend(IntegrationTestCase):
	"""The blend must read the SOURCE BATCH's rate, not the frozen ledger copy.

	``custom_origin_entries.rate`` is a copy of ``Serial and Batch Entry.incoming_rate`` taken on
	bundle ``after_insert``. ``StockEntry.on_submit`` creates those bundles before
	``update_stock_ledger`` prices them, and ERPNext never prices a draft Stock Entry bundle, so
	any produced row that already carries a ``batch_no`` -- every customer lane, because
	``batch_rename`` pre-mints one -- freezes 0.

	Measured on kg-gk: the frozen rate is 0 on all 24 customer-lane origin rows, while 33,512 of
	33,784 rows site-wide are fine. It is per-flow, not per-run.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_frozen_zero_recovers_from_the_source_batch(self):
		"""The reported defect. Source master holds 159000; the frozen copy holds nothing."""
		batch = _BlendBatch(
			item="M-G-24KT-99.9-Y",
			custom_origin_entries=[_origin("SRC-24KT", "M-G-24KT-99.9-Y", 5.0, 0.0)],
		)
		_run_blend(
			batch,
			{"SRC-24KT": {"custom_metal_rate": 159000.0, "custom_alloy_rate": 0.0}},
		)

		self.assertAlmostEqual(
			batch.custom_metal_rate,
			159000.0,
			places=4,
			msg="the blend fell back to the unpriced frozen copy",
		)

	def test_purity_conversion_applies_to_the_recovered_rate(self):
		"""24KT source into a 22KT target: the recovered rate converts, same as a frozen one."""
		batch = _BlendBatch(
			item="M-G-22KT-91.75-Y",
			custom_origin_entries=[_origin("SRC-24KT", "M-G-24KT-99.9-Y", 5.0, 0.0)],
		)
		_run_blend(
			batch,
			{"SRC-24KT": {"custom_metal_rate": 159000.0, "custom_alloy_rate": 0.0}},
		)

		self.assertAlmostEqual(
			batch.custom_metal_rate, 159000.0 * 91.75 / 100, places=4
		)

	def test_alloy_pool_recovers_from_the_source_alloy_rate(self):
		"""Pool-matched: an alloy source's rate lives on custom_alloy_rate, not custom_metal_rate."""
		batch = _BlendBatch(
			custom_origin_entries=[_origin("SRC-ALLOY", _ALLOY_ITEM, 0.45, 0.0)],
		)
		_run_blend(
			batch,
			{"SRC-ALLOY": {"custom_metal_rate": 999.0, "custom_alloy_rate": 62.0}},
		)

		self.assertAlmostEqual(batch.custom_alloy_rate, 62.0, places=4)
		self.assertEqual(
			batch.custom_metal_rate,
			0.0,
			msg="an alloy source leaked into the metal pool",
		)

	def test_the_two_pools_are_blended_separately(self):
		"""The real MAT-STE-17890 shape: 5 g of 24KT plus 0.45 g of alloy."""
		batch = _BlendBatch(
			item="M-G-24KT-99.9-Y",
			custom_origin_entries=[
				_origin("SRC-24KT", "M-G-24KT-99.9-Y", 5.0, 0.0),
				_origin("SRC-ALLOY", _ALLOY_ITEM, 0.45, 0.0),
			],
		)
		_run_blend(
			batch,
			{
				"SRC-24KT": {"custom_metal_rate": 159000.0, "custom_alloy_rate": 0.0},
				"SRC-ALLOY": {"custom_metal_rate": 0.0, "custom_alloy_rate": 62.0},
			},
		)

		self.assertAlmostEqual(batch.custom_metal_rate, 159000.0, places=4)
		self.assertAlmostEqual(batch.custom_alloy_rate, 62.0, places=4)

	def test_sources_are_qty_weighted(self):
		batch = _BlendBatch(
			item="M-G-24KT-99.9-Y",
			custom_origin_entries=[
				_origin("SRC-A", "M-G-24KT-99.9-Y", 1.0, 0.0),
				_origin("SRC-B", "M-G-24KT-99.9-Y", 3.0, 0.0),
			],
		)
		_run_blend(
			batch,
			{
				"SRC-A": {"custom_metal_rate": 100.0, "custom_alloy_rate": 0.0},
				"SRC-B": {"custom_metal_rate": 200.0, "custom_alloy_rate": 0.0},
			},
		)

		# (100*1 + 200*3) / 4
		self.assertAlmostEqual(batch.custom_metal_rate, 175.0, places=6)

	def test_source_without_a_maintained_rate_keeps_the_frozen_rate(self):
		"""Legacy no-regression: a source minted before rates were carried still blends as today."""
		batch = _BlendBatch(
			item="M-G-24KT-99.9-Y",
			custom_origin_entries=[_origin("SRC-OLD", "M-G-24KT-99.9-Y", 2.0, 4321.0)],
		)
		_run_blend(
			batch, {"SRC-OLD": {"custom_metal_rate": 0.0, "custom_alloy_rate": 0.0}}
		)

		self.assertAlmostEqual(batch.custom_metal_rate, 4321.0, places=4)

	def test_a_source_batch_that_no_longer_exists_falls_back_to_the_frozen_rate(self):
		"""``batch_rates.get(row.batch_no)`` returns None for a deleted or renamed source.

		Distinct from a source that EXISTS holding 0: that path exercises ``rate or flt(row.rate)``,
		this one exercises the ``or {}`` guard. Without it the lookup raises AttributeError inside
		a submit hook.
		"""
		batch = _BlendBatch(
			item="M-G-24KT-99.9-Y",
			custom_origin_entries=[_origin("SRC-GONE", "M-G-24KT-99.9-Y", 2.0, 777.0)],
		)
		_run_blend(batch, {})

		self.assertAlmostEqual(batch.custom_metal_rate, 777.0, places=4)

	def test_an_origin_row_with_no_batch_no_still_blends(self):
		"""A hand-built origin row can carry an item and a qty but no batch."""
		batch = _BlendBatch(
			item="M-G-24KT-99.9-Y",
			custom_origin_entries=[_origin(None, "M-G-24KT-99.9-Y", 2.0, 555.0)],
		)
		_run_blend(batch, {})

		self.assertAlmostEqual(batch.custom_metal_rate, 555.0, places=4)

	def test_a_non_conversion_voucher_still_does_not_blend(self):
		"""The gate is unchanged: only Repack-Metal Conversion reaches the blend."""
		batch = _BlendBatch(
			custom_metal_rate=145876.678899083,
			custom_origin_entries=[_origin("SRC-24KT", "M-G-24KT-99.9-Y", 5.0, 0.0)],
		)
		_run_blend(
			batch, {"SRC-24KT": {"custom_metal_rate": 159000.0}}, se_type="Repack"
		)

		self.assertEqual(batch.writes, [], msg="a non-conversion voucher wrote a rate")


class TestBlendGuard(IntegrationTestCase):
	"""A blend that carries no information must not overwrite a rate that does.

	``batch_rename.create_child_batches`` stamps the correct rate at ``before_submit``; the blend
	then ran at ``on_submit`` and wrote 0 over it unconditionally.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_blend_to_zero_does_not_clobber_a_stamped_rate(self):
		"""The regression test for the reported bug, with the real MAT-STE-17890 numbers."""
		batch = _BlendBatch(
			custom_metal_rate=145876.678899083,
			custom_origin_entries=[
				_origin("SRC-24KT", "M-G-24KT-99.9-Y", 5.0, 0.0),
				_origin("SRC-ALLOY", _ALLOY_ITEM, 0.45, 0.0),
			],
		)
		_run_blend(
			batch,
			{
				"SRC-24KT": {"custom_metal_rate": 0.0, "custom_alloy_rate": 0.0},
				"SRC-ALLOY": {"custom_metal_rate": 0.0, "custom_alloy_rate": 0.0},
			},
		)

		self.assertAlmostEqual(
			batch.custom_metal_rate,
			145876.678899083,
			places=6,
			msg="the stamped rate was overwritten by an uninformative blend",
		)
		self.assertNotIn(
			("custom_metal_rate", 0.0),
			batch.writes,
			msg="a zero was written to custom_metal_rate",
		)

	def test_empty_alloy_pool_leaves_the_alloy_rate_alone(self):
		"""A metal-only conversion says nothing about the target's alloy rate."""
		batch = _BlendBatch(
			item="M-G-24KT-99.9-Y",
			custom_alloy_rate=62.0,
			custom_origin_entries=[_origin("SRC-24KT", "M-G-24KT-99.9-Y", 5.0, 0.0)],
		)
		_run_blend(
			batch,
			{"SRC-24KT": {"custom_metal_rate": 159000.0, "custom_alloy_rate": 0.0}},
		)

		self.assertAlmostEqual(batch.custom_alloy_rate, 62.0, places=4)
		self.assertNotIn(
			"custom_alloy_rate",
			[field for field, _ in batch.writes],
			msg="an empty alloy pool still wrote a rate",
		)

	def test_a_real_blend_still_overwrites_the_stamped_rate(self):
		"""Proves the guard did not become 'never overwrite'."""
		batch = _BlendBatch(
			item="M-G-24KT-99.9-Y",
			custom_metal_rate=100.0,
			custom_origin_entries=[_origin("SRC-24KT", "M-G-24KT-99.9-Y", 5.0, 0.0)],
		)
		_run_blend(
			batch,
			{"SRC-24KT": {"custom_metal_rate": 159000.0, "custom_alloy_rate": 0.0}},
		)

		self.assertAlmostEqual(batch.custom_metal_rate, 159000.0, places=4)

	def test_zero_blend_on_an_unrated_batch_is_still_written(self):
		"""The contract is only 'a set rate is not clobbered', not 'never write zero'."""
		batch = _BlendBatch(
			item="M-G-24KT-99.9-Y",
			custom_metal_rate=0.0,
			custom_origin_entries=[_origin("SRC-OLD", "M-G-24KT-99.9-Y", 2.0, 0.0)],
		)
		_run_blend(
			batch, {"SRC-OLD": {"custom_metal_rate": 0.0, "custom_alloy_rate": 0.0}}
		)

		self.assertIn(("custom_metal_rate", 0.0), batch.writes)


class TestAlloyPoolFieldMismatch(IntegrationTestCase):
	"""An alloy batch's rate can sit on EITHER rate field, and the blend must find it.

	``batch/doc_events/utils._rate_field_for_item`` decides where to STAMP from the
	``Item Group.custom_is_alloy_group`` master flag; ``batch._is_alloy`` decides where to READ from
	the literal item group. That flag is a per-site master -- SET on gk, UNSET on kg-gk -- so the two
	agree on gk and disagree in production. Where it is unset, every alloy rate is stamped onto
	``custom_metal_rate`` while the blend looks only at ``custom_alloy_rate`` and reads 0. Measured on
	kg-gk: 0 of 6 Alloy-group batches carry ``custom_alloy_rate``; all 5 that are priced carry
	``custom_metal_rate``.

	That is MAT-STE-17967 -- one voucher, two lanes, the same alloy source batch, and the Customer
	Goods batch minted with Alloy Rate 0.00 while its Regular Stock sibling got 62.00.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_alloy_source_rate_on_the_metal_field_is_still_found(self):
		"""The kg-gk shape: KG2D082-ML7-04 holds 62 on custom_metal_rate and nothing on alloy."""
		batch = _BlendBatch(
			custom_origin_entries=[_origin("SRC-ALLOY", _ALLOY_ITEM, 0.899, 0.0)],
		)
		_run_blend(
			batch, {"SRC-ALLOY": {"custom_metal_rate": 62.0, "custom_alloy_rate": 0.0}}
		)

		self.assertAlmostEqual(
			batch.custom_alloy_rate,
			62.0,
			places=4,
			msg="the alloy pool ignored a rate stamped on custom_metal_rate",
		)

	def test_an_alloy_rate_still_wins_over_the_metal_field(self):
		"""Preference order, not a free-for-all: where both are set, the alloy field is the answer."""
		batch = _BlendBatch(
			custom_origin_entries=[_origin("SRC-ALLOY", _ALLOY_ITEM, 0.45, 0.0)],
		)
		_run_blend(
			batch,
			{"SRC-ALLOY": {"custom_metal_rate": 999.0, "custom_alloy_rate": 62.0}},
		)

		self.assertAlmostEqual(batch.custom_alloy_rate, 62.0, places=4)

	def test_a_metal_source_never_borrows_the_alloy_rate(self):
		"""The asymmetry, and the reason it has to be one.

		On gk, 27 non-alloy batches carry ``custom_alloy_rate`` with ``custom_metal_rate`` at 0. On a
		metal batch that field holds the alloy blended INTO the metal, not the metal's own rate, so a
		symmetric fallback would value gold at alloy prices.
		"""
		batch = _BlendBatch(
			item="M-G-24KT-99.9-Y",
			custom_origin_entries=[_origin("SRC-24KT", "M-G-24KT-99.9-Y", 10.0, 0.0)],
		)
		_run_blend(
			batch, {"SRC-24KT": {"custom_metal_rate": 0.0, "custom_alloy_rate": 62.0}}
		)

		self.assertEqual(
			batch.custom_metal_rate,
			0.0,
			msg="a metal source borrowed the alloy rate -- gold valued at alloy prices",
		)

	def test_the_mat_ste_17967_shape_end_to_end(self):
		"""Both sources keep their rate on custom_metal_rate, exactly as kg-gk holds them."""
		batch = _BlendBatch(
			item="M-G-22KT-91.75-Y",
			custom_origin_entries=[
				_origin(
					"GJCU0009-2F09-M-G-24KT-99.9-Y-05", "M-G-24KT-99.9-Y", 10.0, 0.0
				),
				_origin("KG2D082-ML7-04", _ALLOY_ITEM, 0.899, 0.0),
			],
		)
		_run_blend(
			batch,
			{
				"GJCU0009-2F09-M-G-24KT-99.9-Y-05": {
					"custom_metal_rate": 159000.0,
					"custom_alloy_rate": 0.0,
				},
				"KG2D082-ML7-04": {"custom_metal_rate": 62.0, "custom_alloy_rate": 0.0},
			},
		)

		# 159000 x 91.75 / 100 -- the Batch Rate the voucher actually produced
		self.assertAlmostEqual(batch.custom_metal_rate, 145882.5, places=4)
		self.assertAlmostEqual(
			batch.custom_alloy_rate,
			62.0,
			places=4,
			msg="the Customer Goods batch came out with Alloy Rate 0.00 again",
		)


class TestAlloyTargetNeverTakesTheMetalBlend(IntegrationTestCase):
	"""A conversion that PRODUCES an alloy item must not stamp the gold blend onto it.

	Nothing validates against a Repack-Metal Conversion producing an alloy item, and gk holds three
	such batches -- GE2D082-ML7-14, GE2D082-ML7-15 (M-Genia-221) and GE2D082-MAL-03 (M-AL), all
	Nov-Dec 2024. ``on_update`` classifies only the SOURCE rows, never ``doc.item``, so such a batch
	would be stamped with the blended rate of the gold it was made from.

	That is the single way an alloy batch can hold a ``custom_metal_rate`` that is not its own rate
	-- and it is exactly the case ``ALLOY_SOURCE_RATE_FIELDS`` cannot tell apart, because a later
	conversion consuming that batch as an alloy source would read the gold rate as the alloy price.
	Replayed on the GE2D082-ML7-14 shape: 6436.61 instead of 62.00, a 104x over-valuation.

	The shape has 0 instances on gk, kg-gk and alfarsi today, so this closes the precondition rather
	than repairing data.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_an_alloy_target_is_not_stamped_with_the_metal_blend(self):
		"""The GE2D082-ML7-14 shape: alloy item produced from a 22KT gold source."""
		batch = _BlendBatch(
			item=_ALLOY_ITEM,
			custom_origin_entries=[
				_origin("SRC-22KT", "M-G-22KT-91.9-Y", 4.17, 6436.61)
			],
		)
		_run_blend(
			batch,
			{"SRC-22KT": {"custom_metal_rate": 6436.61, "custom_alloy_rate": 0.0}},
		)

		self.assertEqual(
			batch.custom_metal_rate,
			0.0,
			msg="an alloy batch was stamped with the gold blend",
		)
		self.assertNotIn(
			"custom_metal_rate",
			[field for field, _ in batch.writes],
			msg="the metal blend was written onto an alloy target",
		)

	def test_a_metal_target_still_takes_both_rates(self):
		"""The gate is NOT symmetric, and this is the test that says so.

		MAT-STE-17967's 22KT batch legitimately carries its own metal rate AND the rate of the alloy
		blended into it. Gating the alloy stamp as well would break the ordinary conversion.
		"""
		batch = _BlendBatch(
			item="M-G-22KT-91.75-Y",
			custom_origin_entries=[
				_origin("SRC-24KT", "M-G-24KT-99.9-Y", 10.0, 0.0),
				_origin("SRC-ALLOY", _ALLOY_ITEM, 0.899, 0.0),
			],
		)
		_run_blend(
			batch,
			{
				"SRC-24KT": {"custom_metal_rate": 159000.0, "custom_alloy_rate": 0.0},
				"SRC-ALLOY": {"custom_metal_rate": 62.0, "custom_alloy_rate": 0.0},
			},
		)

		self.assertAlmostEqual(batch.custom_metal_rate, 145882.5, places=4)
		self.assertAlmostEqual(batch.custom_alloy_rate, 62.0, places=4)

	def test_an_alloy_target_still_gets_its_own_alloy_blend(self):
		"""The gate blocks only the metal pool; a genuine alloy-from-alloy blend still lands."""
		batch = _BlendBatch(
			item=_ALLOY_ITEM,
			custom_origin_entries=[_origin("SRC-ALLOY", _ALLOY_ITEM, 2.0, 0.0)],
		)
		_run_blend(
			batch,
			{"SRC-ALLOY": {"custom_metal_rate": 0.0, "custom_alloy_rate": 55.0}},
		)

		self.assertAlmostEqual(batch.custom_alloy_rate, 55.0, places=4)


class TestBlendPremisesThatNothingElsePinned(IntegrationTestCase):
	"""Behaviours the blend relies on that no test held, found by mutation testing.

	Each test here corresponds to a mutation that left the whole suite green. They are not new
	behaviour -- the code is already correct on every one -- but a wrong implementation used to pass.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_the_source_batch_master_wins_over_the_frozen_row_rate(self):
		"""The premise of ``_origin_row_rate``, and the entire point of preferring the master.

		Every other blend test either sets the frozen ``rate`` to 0 or sets it equal to the source
		master's rate, so putting the frozen copy FIRST kept the suite green. On gk, 1,129 conversion
		origin rows carry a non-zero frozen rate and 187 of those differ from their source Batch's
		``custom_metal_rate`` -- inverting the preference would restamp every one of them.
		"""
		batch = _BlendBatch(
			item="M-G-24KT-99.9-Y",
			custom_origin_entries=[
				_origin("SRC-24KT", "M-G-24KT-99.9-Y", 5.0, 150000.0)
			],
		)
		_run_blend(
			batch,
			{"SRC-24KT": {"custom_metal_rate": 159000.0, "custom_alloy_rate": 0.0}},
		)

		self.assertAlmostEqual(
			batch.custom_metal_rate,
			159000.0,
			places=4,
			msg="the frozen ledger copy won over the source Batch's maintained rate",
		)

	def test_the_item_group_arm_of_is_alloy_stands_alone(self):
		"""gk holds 3 items in the Alloy group with no attributes at all."""
		batch = _BlendBatch(
			custom_origin_entries=[_origin("SRC-ALLOY", "M-AL", 2.0, 0.0)],
		)
		_run_blend(
			batch,
			{"SRC-ALLOY": {"custom_metal_rate": 0.0, "custom_alloy_rate": 44.0}},
			items={"M-AL": ("Alloy", 4)},
		)

		self.assertAlmostEqual(
			batch.custom_alloy_rate,
			44.0,
			places=4,
			msg="an Alloy-group item with several attributes was not treated as alloy",
		)

	def test_the_single_attribute_arm_of_is_alloy_stands_alone(self):
		"""gk holds 385 one-attribute items outside the Alloy group."""
		batch = _BlendBatch(
			custom_origin_entries=[_origin("SRC-OTHER", "O-BB", 2.0, 0.0)],
		)
		_run_blend(
			batch,
			{"SRC-OTHER": {"custom_metal_rate": 0.0, "custom_alloy_rate": 7.5}},
			items={"O-BB": ("Other Material - V", 1)},
		)

		self.assertAlmostEqual(
			batch.custom_alloy_rate,
			7.5,
			places=4,
			msg="a single-attribute item outside the Alloy group was not treated as alloy",
		)

	def test_alloy_source_with_no_maintained_rate_keeps_the_frozen_rate(self):
		"""The alloy pool had no fallback test at all; the metal pool has three."""
		batch = _BlendBatch(
			custom_origin_entries=[_origin("SRC-ALLOY", _ALLOY_ITEM, 0.899, 62.0)],
		)
		_run_blend(
			batch, {"SRC-ALLOY": {"custom_metal_rate": 0.0, "custom_alloy_rate": 0.0}}
		)

		self.assertAlmostEqual(batch.custom_alloy_rate, 62.0, places=4)

	def test_a_missing_alloy_source_batch_falls_back_to_the_frozen_rate(self):
		"""Exercises the ``or {}`` guard on the alloy side; only the metal side had this."""
		batch = _BlendBatch(
			custom_origin_entries=[_origin("SRC-GONE", _ALLOY_ITEM, 0.899, 62.0)],
		)
		_run_blend(batch, {})

		self.assertAlmostEqual(batch.custom_alloy_rate, 62.0, places=4)

	def test_a_zero_qty_origin_row_still_blends(self):
		"""``row_qty = flt(row.qty) or 1.0``. 162 of gk's 1,429 conversion origin rows are qty 0/NULL.

		Without the guard the pool is empty, ``_stamp_blended_rate`` returns on its empty-pool check
		and the Batch Rate is never written -- silently.
		"""
		batch = _BlendBatch(
			item="M-G-22KT-91.75-Y",
			custom_origin_entries=[_origin("SRC-24KT", "M-G-24KT-99.9-Y", 0.0, 0.0)],
		)
		_run_blend(
			batch,
			{"SRC-24KT": {"custom_metal_rate": 159000.0, "custom_alloy_rate": 0.0}},
		)

		self.assertAlmostEqual(batch.custom_metal_rate, 145882.5, places=4)

	def test_an_alloy_target_with_both_pools_still_withholds_the_metal_blend(self):
		"""The gate must key on the TARGET, not on whether the alloy pool happens to be empty.

		Both existing gate tests are single-pool, so a gate weakened to
		``if not _is_alloy(doc.item) or alloy_qty:`` passed -- and under it this exact voucher stamps
		6436.61 onto an alloy batch, the 104x over-valuation the gate exists to prevent.
		"""
		batch = _BlendBatch(
			item=_ALLOY_ITEM,
			custom_origin_entries=[
				_origin("SRC-22KT", "M-G-22KT-91.9-Y", 4.17, 6436.61),
				_origin("SRC-ALLOY", _ALLOY_ITEM, 1.0, 62.0),
			],
		)
		_run_blend(
			batch,
			{
				"SRC-22KT": {"custom_metal_rate": 6436.61, "custom_alloy_rate": 0.0},
				"SRC-ALLOY": {"custom_metal_rate": 62.0, "custom_alloy_rate": 0.0},
			},
		)

		self.assertEqual(
			batch.custom_metal_rate,
			0.0,
			msg="the gate stopped firing once the alloy pool was non-empty",
		)
		self.assertAlmostEqual(batch.custom_alloy_rate, 62.0, places=4)
