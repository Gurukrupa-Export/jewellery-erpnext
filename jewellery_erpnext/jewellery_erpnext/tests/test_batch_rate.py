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

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting import batch_rename
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
