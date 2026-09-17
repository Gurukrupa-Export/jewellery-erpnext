# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""End-to-end Customer Gold evidence: real documents, real SLEs, real GL Entries.

Every other Customer Gold suite in this app is pure-logic: docs are ``frappe._dict``
fakes and every DB read is patched. Those suites prove the rules in isolation and prove
nothing about what erpnext's valuation and GL machinery actually does with a receipt.
This suite submits genuine Stock Entries and reads back ``Stock Ledger Entry`` and
``GL Entry`` rows.

WHY IT NEEDS ITS OWN SITE
-------------------------
It cannot safely run on a site carrying real data, for two independent reasons:

1. ``IntegrationTestCase`` rollback is CLASS-scoped, not per-test -- queued through
   ``addClassCleanup(_rollback_db)``, and ``setUpClass`` calls ``frappe.db.commit()``
   first. Anything that commits mid-test therefore survives, and this app enqueues
   background work on Stock Entry submit.
2. Configuring the feature means writing ``Subcontracting Settings``, a **Single**, and
   creating a Company, Items and a Manufacturing Setting.

So the suite skips unless the site sets ``customer_gold_disposable_site: 1`` in its
``site_config.json``. It raises ``SkipTest`` rather than an error, so a whole-app run on
an ordinary site reports skips instead of four class-level ERRORs.

The flag is an explicit operator opt-in, NOT proof the site is empty -- nothing here can
verify that. Set it only on a site created for this purpose.

WHAT IT ASSERTS
---------------
Documents, Stock Ledger Entries and General Ledger Entries -- not just return values.

Valuation assertions deliberately pin the CURRENT behaviour (customer gold enters stock at
zero value) rather than a nominal-valuation behaviour, because that accounting policy is
still open as D01 in ``docs-customer-gold/DECISIONS.md``. When D01 is decided, the
zero-valuation cases here are the ones to revisit; the ownership, rate-evidence and purity
cases are independent of it and stand either way.

NEGATIVE TESTS ASSERT ON THE MESSAGE, NOT THE CLASS. ``frappe.ValidationError`` is far too
broad here: ``DoesNotExistError`` subclasses it, and so does every unrelated erpnext
validation (a missing difference account, for one). A bare
``assertRaises(frappe.ValidationError)`` would go green while the receipt failed for a
completely different reason. Every negative case below therefore matches a distinctive
fragment of the specific message it is testing.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, nowdate

from jewellery_erpnext.customer_subcontracting import (
	customer_gold_components as cgc,
)
from jewellery_erpnext.customer_subcontracting import (
	customer_gold_fulfilment as cgf,
)
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	ENABLE_FLAG,
	SETTINGS_DOCTYPE,
)

from . import customer_gold_purity_fixtures as cg_purity

DISPOSABLE_FLAG = "customer_gold_disposable_site"

COMPANY = "CG Integration Co"
MANUFACTURER = "CG Test Manufacturer"
ABBR = "CGIC"
CUSTOMER = "CG-TEST-CUSTOMER-A"
OTHER_CUSTOMER = "CG-TEST-CUSTOMER-B"
SE_TYPE = "CG Test Customer Goods Received"
REPACK_SE_TYPE = "CG Test Repack"
RETURN_SE_TYPE = "CG Test Customer Gold Return"
TRANSFER_SE_TYPE = "CG Test Transfer"
MANUFACTURE_SE_TYPE = "CG Test Manufacture"
#: Must be named exactly "Process Loss" — the dispatcher keys on the type, mirroring
#: ``loss_stock_entry.py:32``.
LOSS_SE_TYPE = "Process Loss"
SALES_TYPE = "Outwork"
RATE_SOURCE = "CG Test Bullion"

#: The metal template. Its code matters: ``Stock Entry Detail.custom_variant_of`` is
#: ``fetch_from: item_code.variant_of``, and the pure-quantity block in
#: ``doc_events.stock_entry`` runs only for ``custom_variant_of in ("M", "F")``. A variant
#: of a template named anything else silently skips the purity computation entirely.
METAL_TEMPLATE = "M"

#: A second, non-metal template. Its only job is to make the C07 reproduction faithful:
#: ``custom_variant_of`` is a Link to Item, so a forged value must name a REAL Item to get
#: past link validation. On gk and in production "D" (Diamond), "F", "G" and "ML" all
#: exist, so an attacker has real values to choose from. Without this the forgery fails
#: for the wrong reason -- LinkValidationError rather than the gate being bypassed.
NON_METAL_TEMPLATE = "D"

#: Gold in unwrought forms. india_compliance makes HSN/SAC mandatory on Item AND requires
#: 6 or 8 digits, so the fixtures carry the real 8-digit code rather than a placeholder.
GST_HSN_CODE = "71081200"

#: Per 10 g, so the per-gram rate is a non-round 7,164.83 and a lost divisor is visible.
RAW_RATE = 71648.30
EXPECTED_PER_GRAM = 7164.83


def _require_disposable_site():
	"""Skip unless the site explicitly opts in."""
	if not frappe.conf.get(DISPOSABLE_FLAG):
		raise unittest.SkipTest(
			f"needs {DISPOSABLE_FLAG!r} in site_config.json -- this suite submits real "
			f"Stock Entries and writes the {SETTINGS_DOCTYPE} Single"
		)


class _CustomerGoldIntegrationCase(IntegrationTestCase):
	"""Shared setup: company, metal masters, feature configuration, a rate for today."""

	@classmethod
	def setUpClass(cls):
		"""Set up WITHOUT ``super().setUpClass()`` -- deliberately, and explained here.

		``IntegrationTestCase.setUpClass`` calls ``make_test_records`` for every dependency
		it infers from the test module, then ``frappe.db.commit()``. Two problems:

		* the inferred set pulls in unrelated jewellery masters and dies building them
		  (``LinkValidationError: Could not find Reason for Design Code : New Design``),
		  and ``IGNORE_TEST_RECORD_DEPENDENCIES`` is only honoured for test modules that
		  live inside a doctype folder -- this one does not; and
		* this suite builds every fixture it needs explicitly, by design, so that what it
		  asserts is traceable to a known input rather than to a shared global record.

		The one thing that base method does that genuinely matters here is registering the
		rollback, so that is registered directly. Note it is CLASS-scoped either way -- the
		fixtures below are inserted inside the class transaction and disappear with it.
		"""
		_require_disposable_site()
		cls._primary_connection = frappe.local.db
		cls._secondary_connection = None
		cls.addClassCleanup(frappe.db.rollback)
		cls.posting_date = nowdate()
		cls._ensure_company()
		cls._ensure_metal_masters()
		cls._ensure_manufacturing_setting()
		cls._ensure_customers()
		cls._ensure_stock_entry_type()
		cls._ensure_gold_rate(cls.posting_date, RAW_RATE)
		cls._configure_feature()

	# ------------------------------------------------------------------ company
	@classmethod
	def _ensure_warehouse_types(cls):
		"""Warehouse Types erpnext's Company insert expects to already exist.

		Normally created by the desk setup wizard, which a scripted disposable site never runs.
		Company creation builds its default warehouse tree and links "Transit", so without this
		the very first Company insert dies with
		``LinkValidationError: Could not find Warehouse Type: Transit`` -- and every one of the
		suite's nine classes fails in setUpClass with zero tests executed.

		This is exactly the class of gap REC-T005 exists to surface: the previous
		``cg-integration.test`` was hand-built, so somebody had created these by hand and the
		dependency was invisible. A suite that cannot bootstrap itself on a fresh site is not
		reproducible evidence.
		"""
		for name in ("Transit",):
			if not frappe.db.exists("Warehouse Type", name):
				frappe.get_doc({"doctype": "Warehouse Type", "name": name}).insert(
					ignore_permissions=True
				)

	@classmethod
	def _ensure_company(cls):
		cls._ensure_warehouse_types()
		if not frappe.db.exists("Company", COMPANY):
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": COMPANY,
					"abbr": ABBR,
					"default_currency": "INR",
					"country": "India",
					"enable_perpetual_inventory": 1,
				}
			).insert(ignore_permissions=True)

		cls._ensure_fiscal_year()
		cls.warehouse = cls._ensure_warehouse("CG Test Custody")
		cls.liability_account = cls._ensure_account(
			"CG Test Customer Gold Liability", "Liability"
		)
		cls.cogs_account = cls._ensure_account(
			"CG Test Customer Gold COGS Adj", "Expense"
		)
		# erpnext's validate_difference_account throws for every row with no
		# expense_account once perpetual inventory is on. Resolve the company's own
		# Stock Adjustment account and stamp it on every receipt row.
		cls.difference_account = frappe.db.get_value(
			"Company", COMPANY, "stock_adjustment_account"
		) or cls._ensure_account("CG Test Stock Adjustment", "Expense")

	@classmethod
	def _ensure_fiscal_year(cls):
		"""A Fiscal Year covering the posting date, linked to this company.

		erpnext resolves one on every stock posting; a brand-new company is in none of the
		site's existing years, so posting throws ``FiscalYearError`` before any customer
		gold logic runs.
		"""
		year = getdate(nowdate()).year
		name = f"CG Test FY {year}"
		if frappe.db.exists("Fiscal Year", name):
			doc = frappe.get_doc("Fiscal Year", name)
		else:
			doc = frappe.new_doc("Fiscal Year")
			doc.year = name
			doc.year_start_date = f"{year}-01-01"
			doc.year_end_date = f"{year}-12-31"

		if not any(r.company == COMPANY for r in doc.get("companies") or []):
			doc.append("companies", {"company": COMPANY})
		doc.flags.ignore_mandatory = True
		doc.save(ignore_permissions=True)

	@classmethod
	def _ensure_warehouse(cls, name):
		full = f"{name} - {ABBR}"
		if not frappe.db.exists("Warehouse", full):
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": name,
					"company": COMPANY,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		return full

	@classmethod
	def _ensure_account(cls, name, root_type):
		full = f"{name} - {ABBR}"
		if frappe.db.exists("Account", full):
			return full
		parent = frappe.db.get_value(
			"Account",
			{"company": COMPANY, "root_type": root_type, "is_group": 1},
			"name",
			order_by="lft",
		)
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": name,
				"company": COMPANY,
				"parent_account": parent,
				"root_type": root_type,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		return full

	@classmethod
	def _ensure_department(cls, name):
		full = f"{name} - {ABBR}"
		if not frappe.db.exists("Department", full):
			frappe.get_doc(
				{
					"doctype": "Department",
					"department_name": name,
					"company": COMPANY,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		return full

	# ------------------------------------------------------------ metal masters
	@classmethod
	def _ensure_metal_masters(cls):
		"""Build the full purity chain, not just the Attribute Values.

		``get_purity_percentage`` joins
		``Item -> Item Variant Attribute (attribute="Metal Purity") -> Attribute Value``.
		Creating ``Attribute Value`` rows alone leaves that join empty, so purity resolves
		to None and the pure-quantity block is skipped without ever erroring. Every link in
		the chain has to exist for the computation under test to run at all.
		"""
		cls._ensure_stock_settings()
		cls._ensure_base_masters()
		cg_purity.ensure_attribute_values()

		# The Item Attribute and its allowed-value list: frappe validates a variant's
		# attribute values against this child table.
		if frappe.db.exists("Item Attribute", cg_purity.METAL_PURITY_ATTRIBUTE):
			attribute = frappe.get_doc(
				"Item Attribute", cg_purity.METAL_PURITY_ATTRIBUTE
			)
		else:
			attribute = frappe.new_doc("Item Attribute")
			attribute.attribute_name = cg_purity.METAL_PURITY_ATTRIBUTE

		known = {
			row.attribute_value for row in attribute.get("item_attribute_values") or []
		}
		for value in cg_purity.PURITY_VALUES:
			if value not in known:
				attribute.append(
					"item_attribute_values", {"attribute_value": value, "abbr": value}
				)
		attribute.save(ignore_permissions=True)

		cls.item_group = cls._ensure_item_group()

		if not frappe.db.exists("Item", METAL_TEMPLATE):
			template = frappe.new_doc("Item")
			template.item_code = METAL_TEMPLATE
			template.item_name = "CG Test Metal Template"
			template.item_group = cls.item_group
			template.stock_uom = "Gram"
			template.is_stock_item = 0
			template.has_variants = 1
			template.gst_hsn_code = GST_HSN_CODE
			template.append(
				"attributes", {"attribute": cg_purity.METAL_PURITY_ATTRIBUTE}
			)
			template.insert(ignore_permissions=True)

		if not frappe.db.exists("Item", NON_METAL_TEMPLATE):
			other = frappe.new_doc("Item")
			other.item_code = NON_METAL_TEMPLATE
			other.item_name = "CG Test Non-Metal Template"
			other.item_group = cls.item_group
			other.stock_uom = "Gram"
			other.is_stock_item = 0
			other.has_variants = 1
			other.gst_hsn_code = GST_HSN_CODE
			other.append("attributes", {"attribute": cg_purity.METAL_PURITY_ATTRIBUTE})
			other.insert(ignore_permissions=True)

		cls.item = cls._ensure_variant(cg_purity.DEFAULT_REFERENCE_ITEM, "CG-TEST-99.9")
		cls.operating_item = cls._ensure_variant(
			cg_purity.OPERATING_ITEM, "CG-TEST-75.4"
		)

	@classmethod
	def _ensure_stock_settings(cls):
		"""Batch-tracked stock needs the Serial and Batch Bundle machinery switched on.

		Off by default on a fresh site, and erpnext then refuses to build a bundle for a
		batch-tracked item -- which every customer gold receipt is. On this bench's other
		sites it is already on, so it is invisible until a genuinely fresh install.
		"""
		frappe.db.set_single_value(
			"Stock Settings", "enable_serial_and_batch_no_for_item", 1
		)
		frappe.clear_cache(doctype="Stock Settings")

	@classmethod
	def _ensure_base_masters(cls):
		"""Masters a fresh install does not create but the app's own defaults require.

		Both of these are C04 in miniature:

		* ``UOM: Gram`` -- the stock UOM every metal item uses, absent on a fresh site.
		* ``Attribute Value: New Design`` -- ``Item.custom_reason_for_design_code_`` is a
		  Link to ``Attribute Value`` whose **default** is the literal ``"New Design"``, so
		  every Item insert fails link validation until that value exists.

		Neither is created by ``install-app`` or ``migrate``.
		"""
		if not frappe.db.exists("UOM", "Gram"):
			frappe.get_doc(
				{"doctype": "UOM", "uom_name": "Gram", "must_be_whole_number": 0}
			).insert(ignore_permissions=True)

		# The ownership lanes themselves are records, not an enum. A fresh site has NONE,
		# so every row tagged "Customer Goods" fails link validation. The names must match
		# row_ownership.CUSTOMER_INVENTORY_TYPES / DEFAULT_INVENTORY_TYPE exactly.
		for inventory_type in (
			"Regular Stock",
			"Customer Goods",
			"Customer Stock",
			"Pure Metal",
		):
			if not frappe.db.exists("Inventory Type", inventory_type):
				frappe.get_doc(
					{"doctype": "Inventory Type", "inventory_type": inventory_type}
				).insert(ignore_permissions=True)

		if not frappe.db.exists("Attribute Value", "New Design"):
			frappe.get_doc(
				{"doctype": "Attribute Value", "attribute_value": "New Design"}
			).insert(ignore_permissions=True)

	@classmethod
	def _ensure_item_group(cls):
		"""Deterministic, not 'whichever leaf group sorts first'.

		Picking an arbitrary existing group makes the fixture depend on unrelated masters --
		and on a jewellery bench it can land on a group whose own hooks rewrite the item.
		"""
		name = "CG Test Metal"
		if not frappe.db.exists("Item Group", name):
			parent = frappe.db.get_value(
				"Item Group", {"is_group": 1}, "name", order_by="lft"
			)
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": name,
					"parent_item_group": parent,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		return name

	@classmethod
	def _ensure_variant(cls, item_code, purity_value):
		if frappe.db.exists("Item", item_code):
			return item_code
		item = frappe.new_doc("Item")
		item.item_code = item_code
		item.item_name = item_code
		item.item_group = cls.item_group
		item.stock_uom = "Gram"
		item.is_stock_item = 1
		item.has_batch_no = 1
		item.create_new_batch = 1
		item.gst_hsn_code = GST_HSN_CODE
		item.variant_of = METAL_TEMPLATE
		# Without this the customer-goods guard at
		# ``customization/batch/doc_events/utils.py:63-89`` throws
		# "Item ... is not allowed as Customer Goods" whenever a batch of this item is created
		# or re-saved outside one of that guard's bypasses. The receipt path happens to qualify
		# for a bypass, which is why the suite got this far without it; a Repack's child-batch
		# creation does not. The flag is simply true of this fixture: it exists to hold
		# customer gold.
		item.custom_inventory_type_can_be_customer_goods = 1
		item.append(
			"attributes",
			{
				"attribute": cg_purity.METAL_PURITY_ATTRIBUTE,
				"attribute_value": purity_value,
			},
		)
		item.insert(ignore_permissions=True)
		return item_code

	@classmethod
	def _ensure_manufacturing_setting(cls):
		"""Required by the pure-quantity path, which resolves the reference purity from it.

		Without one, ``before_validate`` throws "Set Pure Gold Item in the Manufacturing
		Setting ..." on every save for this brand-new company.
		"""
		existing = frappe.db.get_value(
			"Manufacturing Setting", {"company": COMPANY}, "name"
		)
		doc = (
			frappe.get_doc("Manufacturing Setting", existing)
			if existing
			else frappe.new_doc("Manufacturing Setting")
		)
		# ``Manufacturing Setting`` autonames ``field:manufacturer``, so a Manufacturer is
		# effectively mandatory. Exactly ONE setting exists for this company, which is what
		# the company-wide fallback in ``doc_events.stock_entry`` requires to resolve the
		# reference item without a manufacturer on the Stock Entry itself.
		if not frappe.db.exists("Manufacturer", MANUFACTURER):
			frappe.get_doc(
				{"doctype": "Manufacturer", "short_name": MANUFACTURER}
			).insert(ignore_permissions=True)

		doc.manufacturer = MANUFACTURER
		doc.company = COMPANY
		# A Material Transfer runs ``doc_events.stock_entry``'s main-slip validator, which looks
		# the Manufacturing Setting up BY MANUFACTURER and throws
		# "Please set all validation options ..." unless all three are set. They are Selects
		# ("", M, F, Both), not checkboxes -- a 1 is rejected outright.
		# (``doc_events/stock_entry.py:668-677``). Without them no transfer fixture can submit.
		doc.check_purity = "Both"
		doc.check_colour = "Both"
		doc.check_touch = "Both"
		doc.pure_gold_item = cls.item
		doc.default_gemstone_department = cls._ensure_department("CG Test Gemstone")
		doc.default_diamond_department = cls._ensure_department("CG Test Diamond")
		doc.flags.ignore_mandatory = True
		doc.save(ignore_permissions=True)
		cls.manufacturing_setting = doc.name

	# ---------------------------------------------------------------- the rest
	@classmethod
	def _ensure_customers(cls):
		for customer in (CUSTOMER, OTHER_CUSTOMER):
			if not frappe.db.exists("Customer", customer):
				frappe.get_doc(
					{
						"doctype": "Customer",
						"customer_name": customer,
						"customer_type": "Company",
					}
				).insert(ignore_permissions=True)

	@classmethod
	def _ensure_stock_entry_type(cls):
		if not frappe.db.exists("Stock Entry Type", SE_TYPE):
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = SE_TYPE
			doc.purpose = "Material Receipt"
			doc.insert(ignore_permissions=True)

		# A Repack type, for the C09 apportionment cases. The component writer fires from
		# Serial and Batch Bundle ``after_insert`` for any Stock Entry whose purpose is
		# ``Manufacture`` or ``Repack`` (``serial_and_batch_bundle/doc_events/utils.py:116``) --
		# it is NOT specific to Metal Conversions. A Repack is therefore the cheapest real
		# document that exercises the production path.
		if not frappe.db.exists("Stock Entry Type", REPACK_SE_TYPE):
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = REPACK_SE_TYPE
			doc.purpose = "Repack"
			doc.insert(ignore_permissions=True)

		# Material Issue: a raw return takes metal OUT and creates nothing, so Repack (which
		# demands a target row) is the wrong purpose for it.
		if not frappe.db.exists("Stock Entry Type", RETURN_SE_TYPE):
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = RETURN_SE_TYPE
			doc.purpose = "Material Issue"
			doc.insert(ignore_permissions=True)

		if not frappe.db.exists("Stock Entry Type", TRANSFER_SE_TYPE):
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = TRANSFER_SE_TYPE
			doc.purpose = "Material Transfer"
			doc.insert(ignore_permissions=True)

		if not frappe.db.exists("Stock Entry Type", MANUFACTURE_SE_TYPE):
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = MANUFACTURE_SE_TYPE
			doc.purpose = "Manufacture"
			doc.insert(ignore_permissions=True)

		if not frappe.db.exists("Stock Entry Type", LOSS_SE_TYPE):
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = LOSS_SE_TYPE
			doc.purpose = "Repack"
			doc.insert(ignore_permissions=True)

	@classmethod
	def _ensure_gold_rate(cls, date, raw_rate, source=RATE_SOURCE):
		"""Create an approved rate record without contacting a bullion feed.

		The ``Gold Rates`` controller issues external provider calls from ``validate``, and
		``validate`` is the ONLY place those calls live. ``flags.ignore_validate`` makes
		``save`` skip it (``frappe/model/document.py`` returns immediately after
		``before_validate`` when the flag is set), so the row is persisted normally with no
		network access. That external feed is the single boundary this suite controls.
		"""
		existing = frappe.db.get_value("Gold Rates", {"date": date}, "name")
		if existing:
			doc = frappe.get_doc("Gold Rates", existing)
		else:
			doc = frappe.new_doc("Gold Rates")
			doc.date = date

		for row in doc.get("table_djrm") or []:
			if row.particulars == source:
				row.live_rate = raw_rate
				break
		else:
			doc.append("table_djrm", {"particulars": source, "live_rate": raw_rate})

		doc.flags.ignore_validate = True
		doc.flags.ignore_mandatory = True
		doc.save(ignore_permissions=True)
		return doc.name

	@classmethod
	def _configure_feature(cls):
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.set(ENABLE_FLAG, 1)
		settings.customer_24kt_item = cls.item
		settings.customer_goods_stock_entry_type = SE_TYPE
		settings.gold_rate_source = RATE_SOURCE
		settings.gold_rate_field = "live_rate"
		settings.gold_rate_unit = "Per 10 Gram"
		settings.set("company_accounts", [])
		settings.append(
			"company_accounts",
			{
				"company": COMPANY,
				"customer_gold_liability_account": cls.liability_account,
				"customer_gold_cogs_adjustment_account": cls.cogs_account,
			},
		)
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype=SETTINGS_DOCTYPE)

	# ---------------------------------------------------------------- helpers
	def assertThrowsContaining(self, fragment):
		"""Assert a ValidationError whose message contains ``fragment``.

		A bare ``assertRaises(frappe.ValidationError)`` is not good enough in this suite --
		see the module docstring. This keeps the negative cases honest.
		"""
		case = self

		class _Ctx:
			def __enter__(self):
				self.ctx = case.assertRaises(frappe.ValidationError)
				self.raised = self.ctx.__enter__()
				return self.raised

			def __exit__(self, exc_type, exc, tb):
				handled = self.ctx.__exit__(exc_type, exc, tb)
				if handled:
					message = str(self.raised.exception)
					case.assertIn(
						fragment,
						message,
						f"raised for the wrong reason: {message!r}",
					)
				return handled

		return _Ctx()

	def _receipt(
		self,
		qty=100,
		customer=CUSTOMER,
		posting_date=None,
		row_customer=None,
		item_code=None,
		**row_overrides,
	):
		"""Build a receipt. ``customer`` is the HEADER customer; ``row_customer`` sets a
		per-row customer, which cannot go through ``row_overrides`` because the name
		collides with the header parameter."""
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = SE_TYPE
		se.purpose = "Material Receipt"
		se.company = COMPANY
		se.posting_date = posting_date or self.posting_date
		se.set_posting_time = 1
		se._customer = customer
		row = {
			"item_code": item_code or self.item,
			"qty": qty,
			"t_warehouse": self.warehouse,
			"uom": "Gram",
			"stock_uom": "Gram",
			"conversion_factor": 1,
			"expense_account": self.difference_account,
		}
		if row_customer is not None:
			row["customer"] = row_customer
		row.update(row_overrides)
		se.append("items", row)
		return se

	def _submit(self, se):
		"""Save the draft, then submit -- in that order, deliberately.

		``se.submit()`` on a doc that has never been saved routes through ``insert()``, and
		``before_submit`` hooks then run BEFORE the row reaches ``tabStock Entry``.
		``batch_rename.create_parent_batches`` is one of those hooks, and the Batch it
		creates sets ``reference_name`` -- a **Dynamic Link** -- to the Stock Entry's name.
		Validating that link finds no row yet::

		    LinkValidationError: Could not find Source Document Name: MAT-STE-00001

		A user always saves a draft before submitting, so the row exists by then and the
		link resolves. These tests mirror that real sequence rather than the shortcut.
		(The shortcut failing is arguably a defect in its own right -- an API client
		submitting a new receipt in one call hits it -- but it is pre-existing and out of
		scope here.)
		"""
		se.save()
		se.submit()
		return se

	def _sles(self, voucher_no):
		return frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": voucher_no, "is_cancelled": 0},
			fields=[
				"item_code",
				"warehouse",
				"actual_qty",
				"valuation_rate",
				"stock_value_difference",
			],
			order_by="creation",
		)

	def _gles(self, voucher_no):
		return frappe.get_all(
			"GL Entry",
			filters={"voucher_no": voucher_no, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
			order_by="account",
		)


class TestCustomerGoldReceiptPostsCorrectly(_CustomerGoldIntegrationCase):
	"""A clean receipt: the document, its Stock Ledger Entries and its GL Entries."""

	def test_receipt_submits(self):
		se = self._receipt()
		self._submit(se)
		self.assertEqual(se.docstatus, 1)

	def test_row_is_tagged_customer_goods_and_owned(self):
		se = self._receipt()
		self._submit(se)
		row = se.items[0]
		self.assertEqual(row.inventory_type, "Customer Goods")
		self.assertEqual(row.customer, CUSTOMER)

	def test_batch_carries_ownership(self):
		se = self._receipt()
		self._submit(se)
		batch = se.items[0].batch_no
		self.assertTrue(batch, "receipt produced no batch")
		self.assertEqual(
			frappe.db.get_value("Batch", batch, "custom_inventory_type"),
			"Customer Goods",
		)
		self.assertEqual(
			frappe.db.get_value("Batch", batch, "custom_customer"), CUSTOMER
		)

	def test_rate_snapshot_is_frozen_on_the_document(self):
		se = self._receipt()
		self._submit(se)
		self.assertEqual(se.custom_gold_rate_source, RATE_SOURCE)
		self.assertEqual(se.custom_gold_rate_field, "live_rate")
		self.assertEqual(flt(se.custom_gold_rate_raw), RAW_RATE)
		self.assertEqual(se.custom_gold_rate_unit, "Per 10 Gram")
		self.assertAlmostEqual(
			flt(se.custom_gold_rate_per_gram), EXPECTED_PER_GRAM, places=2
		)

	# -- the ledgers -------------------------------------------------------------
	def test_stock_ledger_entry_is_created_for_the_receipt(self):
		se = self._receipt(qty=100)
		self._submit(se)
		sles = self._sles(se.name)
		self.assertEqual(len(sles), 1, f"expected one SLE, got {sles}")
		self.assertEqual(sles[0].warehouse, self.warehouse)
		self.assertEqual(flt(sles[0].actual_qty), 100.0)

	def test_stock_ledger_entry_is_zero_valued_under_current_policy(self):
		"""D01-DEPENDENT. Pins today's behaviour, not an endorsement of it.

		Customer gold currently enters stock at zero value: the company never bought it, so
		no cost is recognised. If D01 decides on nominal valuation, THIS assertion is the
		one that changes -- see docs-customer-gold/DECISIONS.md.
		"""
		se = self._receipt(qty=100)
		self._submit(se)
		sle = self._sles(se.name)[0]
		self.assertEqual(flt(sle.valuation_rate), 0.0)
		self.assertEqual(flt(sle.stock_value_difference), 0.0)

	def test_no_gl_entries_under_current_policy(self):
		"""D01-DEPENDENT. A zero-valued receipt posts no GL.

		Asserted explicitly rather than left unstated, so enabling nominal valuation cannot
		start posting to the ledger unnoticed.
		"""
		se = self._receipt(qty=100)
		self._submit(se)
		self.assertEqual(self._gles(se.name), [])

	# -- purity, through the real masters -----------------------------------------
	def test_purity_chain_resolves(self):
		"""Guard the fixture: if the Item Variant Attribute link is missing, purity is
		None and every purity assertion below would pass vacuously against 0."""
		from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
			get_purity_percentage,
		)

		self.assertEqual(
			flt(get_purity_percentage(self.item)), cg_purity.DEFAULT_REFERENCE_PURITY
		)
		self.assertEqual(
			flt(get_purity_percentage(self.operating_item)), cg_purity.OPERATING_PURITY
		)

	def test_pure_qty_at_the_reference_purity(self):
		"""Reference item against itself: exactly the gross weight, by the equal-purity branch."""
		se = self._receipt(qty=100)
		self._submit(se)
		self.assertEqual(flt(se.items[0].custom_pure_qty), 100.0)

	def test_pure_qty_for_the_operating_purity(self):
		"""100 g of 75.4% metal against a 99.9 reference = 75.475 REFERENCE grams.

		The same metal contains 75.400 FINE grams. custom_pure_qty holds the former.
		"""
		se = self._receipt(qty=100, item_code=self.operating_item)
		# Not the configured 24KT item, so the receipt validator rejects it -- assert the
		# computation on the saved draft instead of forcing an unrealistic submit.
		with self.assertThrowsContaining("Customer 24KT Item"):
			se.save()


class TestCustomerGoldReceiptLifecycle(_CustomerGoldIntegrationCase):
	"""Cancellation and amendment must not strand or invent value."""

	def test_cancellation_reverses_the_stock_ledger(self):
		se = self._receipt(qty=50)
		self._submit(se)
		se.cancel()
		self.assertEqual(se.docstatus, 2)
		self.assertEqual(
			self._sles(se.name), [], "cancellation left live SLE rows behind"
		)

	def test_cancellation_keeps_the_rate_evidence(self):
		"""A cancelled receipt must still show the rate it actually used."""
		se = self._receipt(qty=50)
		self._submit(se)
		se.cancel()
		se.reload()
		self.assertEqual(se.custom_gold_rate_source, RATE_SOURCE)
		self.assertAlmostEqual(
			flt(se.custom_gold_rate_per_gram), EXPECTED_PER_GRAM, places=2
		)

	def test_amendment_resolves_its_own_rate(self):
		"""Snapshot fields are no_copy, so an amendment must resolve afresh."""
		se = self._receipt(qty=50)
		self._submit(se)
		se.cancel()

		other_date = add_days(self.posting_date, -1)
		self._ensure_gold_rate(other_date, 80000.0)

		amended = frappe.copy_doc(se)
		# copy_doc carries the source docstatus across, so the copy of a CANCELLED receipt
		# arrives as docstatus 2 and saving it raises DocstatusTransitionError (0 -> 2).
		# An amendment starts as a fresh draft.
		amended.docstatus = 0
		amended.amended_from = se.name
		amended.posting_date = other_date
		amended.save()
		self.assertAlmostEqual(flt(amended.custom_gold_rate_per_gram), 8000.0, places=2)

	def test_editing_gold_rates_after_submit_does_not_rerate_the_receipt(self):
		"""CG-T031: a submitted receipt's evidence is frozen.

		The rate is restored afterwards because this class shares one transaction --
		unittest orders methods alphabetically and this is the last, but the restore keeps
		the fixture honest if a case is added later.
		"""
		se = self._receipt(qty=50)
		self._submit(se)
		frozen = flt(se.custom_gold_rate_per_gram)

		self._ensure_gold_rate(self.posting_date, 99999.0)
		se.reload()
		self.assertEqual(flt(se.custom_gold_rate_per_gram), frozen)

		self._ensure_gold_rate(self.posting_date, RAW_RATE)


class TestCustomerGoldOwnershipIsEnforced(_CustomerGoldIntegrationCase):
	"""Ownership failures must block, against a real database.

	Each case matches the specific message, so an unrelated erpnext validation failing
	first cannot make these pass.
	"""

	def test_missing_customer_blocks(self):
		se = self._receipt(customer=None)
		with self.assertThrowsContaining("Customer is mandatory"):
			se.save()

	def test_row_customer_conflicting_with_header_blocks(self):
		se = self._receipt(customer=CUSTOMER, row_customer=OTHER_CUSTOMER)
		with self.assertThrowsContaining("does not match the receipt Customer"):
			se.save()

	def test_deliberate_non_customer_inventory_type_blocks(self):
		se = self._receipt(inventory_type="Pure Metal")
		with self.assertThrowsContaining("requires Inventory Type"):
			se.save()

	def test_zero_quantity_blocks(self):
		se = self._receipt(qty=0)
		with self.assertThrowsContaining("must be greater than zero"):
			se.save()

	def test_batch_of_another_customer_blocks(self):
		"""Reusing another customer's batch is refused -- but NOT by the guard you expect.

		``validate_customer_gold_batches`` has a "Batch ... belongs to Customer ..." throw
		for exactly this case. It never fires here, because
		``CustomStockEntry.update_batches`` runs earlier in the ``before_validate`` chain
		and overwrites ``row.customer`` from the batch. By the time validation runs the row
		says CUSTOMER-A while the header says CUSTOMER-B, so the row/header mismatch check
		rejects it first.

		The receipt is still correctly blocked, so this is not a hole -- but the dedicated
		batch-ownership message is unreachable on this path, which matters for anyone
		debugging from the error text. Asserted as it actually behaves rather than as the
		code reads.
		"""
		first = self._receipt(qty=10, customer=CUSTOMER)
		self._submit(first)
		stolen = first.items[0].batch_no

		second = self._receipt(qty=10, customer=OTHER_CUSTOMER, batch_no=stolen)
		with self.assertThrowsContaining("does not match the receipt Customer"):
			self._submit(second)

	def test_the_mismatch_message_names_the_batch(self):
		"""Since the dedicated batch-ownership message is unreachable (above), the message the
		user DOES get must carry the diagnosis.

		Without the batch in it, the error says only that two customer names differ and gives
		no clue which batch caused it -- on a receipt with several rows that is close to
		useless. Fixing the hook ordering instead would mean reordering a ``before_validate``
		chain that is load-bearing for unrelated flows, so the message is enriched instead.
		"""
		first = self._receipt(qty=10, customer=CUSTOMER)
		self._submit(first)
		stolen = first.items[0].batch_no

		second = self._receipt(qty=10, customer=OTHER_CUSTOMER, batch_no=stolen)
		with self.assertThrowsContaining(stolen):
			self._submit(second)


class TestCustomerGoldRateSourceIntegrity(_CustomerGoldIntegrationCase):
	"""Rate-source failures, against real Gold Rates records."""

	def test_posting_date_without_a_rate_blocks(self):
		missing = add_days(self.posting_date, -400)
		self.assertFalse(frappe.db.exists("Gold Rates", {"date": missing}))
		se = self._receipt(posting_date=missing)
		with self.assertThrowsContaining("is not available for Posting Date"):
			se.save()

	def test_zero_quote_blocks(self):
		date = add_days(self.posting_date, -2)
		self._ensure_gold_rate(date, 0.0)
		se = self._receipt(posting_date=date)
		with self.assertThrowsContaining("A positive rate is required"):
			se.save()

	def test_duplicate_exact_date_records_block(self):
		"""CG-T025 against the real schema, which permits this via rename."""
		date = add_days(self.posting_date, -3)
		first = self._ensure_gold_rate(date, RAW_RATE)

		frappe.rename_doc("Gold Rates", first, f"{first}-RENAMED", force=True)
		second = frappe.new_doc("Gold Rates")
		second.date = date
		second.append("table_djrm", {"particulars": RATE_SOURCE, "live_rate": RAW_RATE})
		second.flags.ignore_validate = True
		second.flags.ignore_mandatory = True
		second.save(ignore_permissions=True)

		self.assertEqual(
			frappe.db.count("Gold Rates", {"date": date}),
			2,
			"the duplicate fixture did not take effect",
		)

		se = self._receipt(posting_date=date)
		with self.assertThrowsContaining("records exist for Posting Date"):
			se.save()


class TestSubmitTimeRecompute(_CustomerGoldIntegrationCase):
	"""C07 -- values altered through the submit request must not persist.

	``before_validate`` DOES re-run on the submit transition
	(``frappe/model/document.py:1404-1405``, with ``_action = "submit"`` set at ``:1133``),
	and ``frappe/desk/form/save.py`` accepts the full client payload on submit. So altering
	a row between draft and submit is a real vector, not a hypothetical one.

	These cases drive the real document lifecycle -- save a draft, mutate the row the way a
	crafted submit payload would, then submit -- rather than calling ``before_validate`` on
	a fabricated dict.
	"""

	def test_forged_pure_qty_is_recomputed_at_submit(self):
		se = self._receipt(qty=100)
		se.save()

		se.items[0].custom_pure_qty = 1
		se.submit()

		se.reload()
		self.assertEqual(
			flt(se.items[0].custom_pure_qty),
			100.0,
			"a forged custom_pure_qty survived the submit transition",
		)

	def test_forged_variant_of_cannot_skip_the_recompute(self):
		"""The hole: ``custom_variant_of`` gates the pure-qty block and is not re-fetched.

		It is a ``fetch_from`` field with ``allow_on_submit = 0``. The server re-fetch in
		``base_document.py:1063`` is guarded by
		``is_new() or not docstatus.is_submitted() or allow_on_submit``, and ``_save``
		runs ``set_docstatus()`` BEFORE ``_validate_links()`` -- so on the submit
		transition the child row is already docstatus 1 and the re-fetch is skipped.
		``read_only`` is a UI property; the server accepts whatever was posted.

		A payload that sets ``custom_variant_of`` to something outside ("M", "F") therefore
		skips the whole computation, and the forged quantity persists.
		"""
		se = self._receipt(qty=100)
		se.save()

		se.items[0].custom_variant_of = NON_METAL_TEMPLATE
		se.items[0].custom_pure_qty = 1
		se.submit()

		se.reload()
		self.assertEqual(
			flt(se.items[0].custom_pure_qty),
			100.0,
			"forging custom_variant_of skipped the pure-qty recompute at submit",
		)

	def test_variant_of_is_restored_from_the_item(self):
		"""The row's own ``custom_variant_of`` must not be trusted as the gate."""
		se = self._receipt(qty=100)
		se.save()

		se.items[0].custom_variant_of = NON_METAL_TEMPLATE
		se.submit()

		se.reload()
		self.assertEqual(se.items[0].custom_variant_of, METAL_TEMPLATE)

	def test_allow_zero_valuation_survives_to_the_ledger(self):
		"""C15 end-to-end: a server-created receipt must still post at zero value."""
		se = self._receipt(qty=100)
		self._submit(se)

		self.assertEqual(flt(se.items[0].allow_zero_valuation_rate), 1.0)
		sle = self._sles(se.name)[0]
		self.assertEqual(flt(sle.valuation_rate), 0.0)


class TestNominalValuation(_CustomerGoldIntegrationCase):
	"""C01 / P02 — does the frozen rate actually reach the Stock Ledger?

	This is the spec's P02 gate, and it comes before any liability wiring on purpose. My own
	C15 finding is the reason: a positive rate field on the document is **not** evidence that
	the SLE carries value. Only the ledger settles it.

	Nominal is exercised here and nowhere else. The policy defaults to ``Zero Value``, every
	other suite asserts that default, and this class runs only on the disposable site. D01 —
	whether nominal is the approved policy at all — is untouched by these tests passing.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.customer_gold_valuation_policy = "Nominal"
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype=SETTINGS_DOCTYPE)

	def test_the_policy_is_actually_nominal_for_this_class(self):
		"""Guard the guard — if the setting did not stick, everything below is vacuous."""
		from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
			get_customer_gold_valuation_policy,
		)

		self.assertEqual(get_customer_gold_valuation_policy(), "Nominal")

	def test_row_carries_the_entered_rate_and_the_manual_flag(self):
		se = self._receipt(qty=100)
		self._submit(se)
		row = se.items[0]
		self.assertAlmostEqual(flt(row.basic_rate), EXPECTED_PER_GRAM, places=2)
		self.assertEqual(flt(row.set_basic_rate_manually), 1.0)
		self.assertEqual(flt(row.allow_zero_valuation_rate), 0.0)

	def test_basic_amount_is_qty_times_rate(self):
		"""erpnext computes this inside the ``set_basic_rate_manually`` branch itself."""
		se = self._receipt(qty=100)
		self._submit(se)
		self.assertAlmostEqual(
			flt(se.items[0].basic_amount), 100 * EXPECTED_PER_GRAM, places=2
		)

	# -- the gate ------------------------------------------------------------------
	def test_stock_ledger_entry_carries_the_nominal_value(self):
		"""THE P02 GATE. Everything monetary depends on this one assertion."""
		se = self._receipt(qty=100)
		self._submit(se)
		sle = self._sles(se.name)[0]
		self.assertAlmostEqual(flt(sle.valuation_rate), EXPECTED_PER_GRAM, places=2)
		self.assertAlmostEqual(
			flt(sle.stock_value_difference), 100 * EXPECTED_PER_GRAM, places=2
		)

	def test_nominal_value_is_not_zero(self):
		"""Stated separately and bluntly, because zero is the failure this guards against."""
		se = self._receipt(qty=100)
		self._submit(se)
		sle = self._sles(se.name)[0]
		self.assertNotEqual(flt(sle.valuation_rate), 0.0)

	def test_gl_entries_exist_and_balance(self):
		se = self._receipt(qty=100)
		self._submit(se)
		gles = self._gles(se.name)
		self.assertTrue(gles, "nominal receipt posted no GL entries")
		debit = sum(flt(g.debit) for g in gles)
		credit = sum(flt(g.credit) for g in gles)
		self.assertAlmostEqual(debit, credit, places=2, msg=f"unbalanced GL: {gles}")
		self.assertAlmostEqual(debit, 100 * EXPECTED_PER_GRAM, places=2)

	def test_there_is_exactly_one_stock_debit(self):
		"""§7.1: if stock is already debited, no second debit may be created."""
		se = self._receipt(qty=100)
		self._submit(se)
		debits = [g for g in self._gles(se.name) if flt(g.debit) > 0]
		self.assertEqual(len(debits), 1, f"expected one stock debit, got {debits}")

	def test_cancellation_reverses_the_nominal_value(self):
		se = self._receipt(qty=100)
		self._submit(se)
		se.cancel()
		self.assertEqual(self._sles(se.name), [])
		self.assertEqual(self._gles(se.name), [])


class TestFulfilmentLedger(_CustomerGoldIntegrationCase):
	"""C12 — a real Delivery Note writes a real customer-gold event.

	The unit suite covers the predicate and the key shape. This covers what a mock cannot:
	that the hook actually fires on submit, that the row reaches the database, and that the
	UNIQUE constraint on ``cg_event_key`` really is what stops a duplicate.
	"""

	def _stocked_batch(self, qty=100):
		"""Receive customer gold and return the batch now holding it."""
		se = self._receipt(qty=qty)
		self._submit(se)
		return se.items[0].batch_no

	def _stocked_batch_for(self, customer, qty=100):
		"""Same, but owned by a specific customer -- for the attribution tests."""
		se = self._receipt(qty=qty, customer=customer)
		self._submit(se)
		batch = se.items[0].batch_no
		# Assert the fixture before relying on it: if the receipt did not actually tag the
		# batch with this customer, the attribution test below would pass or fail for
		# reasons that have nothing to do with the code under test.
		self.assertEqual(
			frappe.db.get_value("Batch", batch, "custom_customer"),
			customer,
			"fixture did not tag the batch with the intended owner",
		)
		return batch

	def _sales_order(self, qty=30):
		"""Every Delivery Note row must carry ``against_sales_order``.

		gke_customization's DN validator throws "Delivery can be created from Delivery Note
		Company" on any row without one. That is a real production constraint, so the fixture
		goes through a Sales Order rather than around the validator.
		"""
		# Sales Type is mandatory on a Sales Order in this app. "Outwork" is the SOP's own
		# name for work done on customer-supplied material, so it is the honest choice here
		# rather than an arbitrary placeholder.
		if not frappe.db.exists("Sales Type", SALES_TYPE):
			frappe.get_doc(
				{"doctype": "Sales Type", "type": SALES_TYPE, "tax_rate": 0}
			).insert(ignore_permissions=True)

		# ``validate_item_dharm`` (sales_order.py:3257) runs for every sales_type in its
		# allowed tuple -- "Outwork" among them -- and its first act is an unguarded
		# ``frappe.get_doc("Customer Payment Terms", {"customer": ...})``, which raises
		# DoesNotExistError when the customer has none.
		#
		# The record is created with NO ``customer_payment_details`` rows on purpose. That
		# is not a shortcut around the validator: it is what an unbilled customer actually
		# looks like. With an empty detail list the function's ``e_invoice_items`` stays
		# empty, every aggregation loop iterates nothing, and it writes back an empty
		# ``custom_invoice_item`` -- the same outcome as a customer whose configured item
		# types all fail the sales-type match at :3312. Populating it would mean inventing
		# an ``E Invoice Item`` master (5 mandatory fields, including a GST HSN Code and a
		# Default Charges link) to drive billing arithmetic that C12 does not touch: this
		# finding is about stock custody, not invoicing.
		if not frappe.db.exists("Customer Payment Terms", {"customer": CUSTOMER}):
			frappe.get_doc(
				{"doctype": "Customer Payment Terms", "customer": CUSTOMER}
			).insert(ignore_permissions=True)

		so = frappe.new_doc("Sales Order")
		so.company = COMPANY
		so.customer = CUSTOMER
		so.sales_type = SALES_TYPE
		so.transaction_date = self.posting_date
		so.delivery_date = self.posting_date
		so.append(
			"items",
			{
				"item_code": self.item,
				"qty": qty,
				"rate": 0,
				"delivery_date": self.posting_date,
				"warehouse": self.warehouse,
				"uom": "Gram",
				"stock_uom": "Gram",
				"conversion_factor": 1,
			},
		)
		so.flags.ignore_mandatory = True
		so.save()
		so.submit()
		return so

	def _delivery(self, batch_no, qty=30, is_return=False):
		so = self._sales_order(qty=qty)
		dn = frappe.new_doc("Delivery Note")
		dn.company = COMPANY
		dn.customer = CUSTOMER
		dn.posting_date = self.posting_date
		dn.set_posting_time = 1
		dn.is_return = 1 if is_return else 0
		dn.append(
			"items",
			{
				"item_code": self.item,
				"qty": -qty if is_return else qty,
				"rate": 0,
				"warehouse": self.warehouse,
				"batch_no": batch_no,
				"uom": "Gram",
				"stock_uom": "Gram",
				"conversion_factor": 1,
				"against_sales_order": so.name,
				"so_detail": so.items[0].name,
			},
		)
		dn.flags.ignore_mandatory = True
		return dn

	def _events(self, voucher_no, kind=None):
		filters = {"reference_docname": voucher_no}
		if kind:
			filters["cg_event_kind"] = kind
		return frappe.get_all(
			"Customer Gold Ledger Entry",
			filters=filters,
			fields=[
				"name",
				"cg_event_kind",
				"cg_event_key",
				"customer",
				"batch_no",
				"cg_gross_qty_delta",
				"cg_fine_gold_delta",
				"cg_carrying_value_delta",
				"cg_settlement_voucher",
				"cg_stage",
				"cg_reversal_of",
				# A field absent from this list reads back as None, which an assertion for
				# "unmeasured" would accept without the column ever having been consulted.
				"cg_fine_measurement_status",
				"cg_reference_measurement_status",
				"cg_measurement_reason",
			],
		)

	# -- the event itself ----------------------------------------------------------
	def test_delivery_writes_one_event(self):
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()

		events = self._events(dn.name)
		self.assertEqual(len(events), 1, f"expected one event, got {events}")
		self.assertEqual(events[0].cg_event_kind, "Delivery")

	def test_the_event_records_the_owning_customer(self):
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		self.assertEqual(self._events(dn.name)[0].customer, CUSTOMER)

	def test_delivery_reduces_the_customer_position(self):
		"""The sign is the whole point — delivered metal leaves the customer's holding."""
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		self.assertEqual(flt(self._events(dn.name)[0].cg_gross_qty_delta), -30.0)

	def test_the_delivered_position_closes_the_stage(self):
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		self.assertEqual(self._events(dn.name)[0].cg_stage, "Closed")

	# -- idempotency, against the real constraint ----------------------------------
	def test_replaying_the_hook_creates_no_second_event(self):
		"""CG-T137. The UNIQUE key is the authority, not a read-then-write check."""
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		self.assertEqual(len(self._events(dn.name)), 1)

		cgf.record_fulfilment(dn)
		cgf.record_fulfilment(dn)
		self.assertEqual(len(self._events(dn.name)), 1)

	def test_the_unique_constraint_is_real(self):
		"""CG-T138. Prove the database refuses the duplicate, not just our code path."""
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		key = self._events(dn.name)[0].cg_event_key

		duplicate = frappe.get_doc(
			{
				"doctype": "Customer Gold Ledger Entry",
				"cg_event_key": key,
				"cg_event_kind": "Delivery",
				"company": COMPANY,
				"reference_doctype": "Delivery Note",
				"reference_docname": dn.name,
			}
		)
		# UniqueValidationError, not DuplicateEntryError: the collision is on the UNIQUE
		# *column* ``cg_event_key``, raised at ``base_document.py:873`` from MariaDB error
		# 1062. DuplicateEntryError is a NameError subclass for a colliding document *name*,
		# which cannot happen here -- the doctype is ``autoname: hash``. Asserting on the
		# message as well, because UniqueValidationError subclasses ValidationError and a
		# bare assertRaises would also pass on any unrelated validation failure.
		with self.assertRaises(frappe.UniqueValidationError) as caught:
			duplicate.insert(ignore_permissions=True)

		self.assertIn("cg_event_key", str(caught.exception))

	# -- cancellation --------------------------------------------------------------
	def test_cancelling_writes_a_reversal_not_a_delete(self):
		"""CG-T143 — the original event is history and stays."""
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		original = self._events(dn.name)[0]

		dn.cancel()

		self.assertTrue(
			frappe.db.exists("Customer Gold Ledger Entry", original.name),
			"cancelling deleted the original event",
		)
		reversals = self._events(dn.name, kind="Reversal")
		self.assertEqual(len(reversals), 1)
		self.assertEqual(reversals[0].cg_reversal_of, original.name)

	def test_the_reversal_exactly_undoes_the_original(self):
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		dn.cancel()

		total = sum(flt(e.cg_gross_qty_delta) for e in self._events(dn.name))
		self.assertEqual(total, 0.0, "delivery and reversal do not net to zero")

	def test_reversing_twice_is_harmless(self):
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		dn.cancel()

		cgf.reverse_fulfilment(dn)
		self.assertEqual(len(self._events(dn.name, kind="Reversal")), 1)

	# -- ownership attribution (CG-T139) -------------------------------------------
	def test_wrong_customer_delivery_is_blocked(self):
		"""CG-T139 / REC-T020. The spec's required outcome is **Block**, not re-attribution.

		An earlier version of this test asserted the opposite: it let the Delivery Note submit
		and checked only that the event named the metal's owner rather than the billed party.
		That is correct bookkeeping of an incorrect movement. The spec card reads *"Reject owner
		mismatch before effective physical or nominal closure. Required acceptance outcome:
		Block."* Customer B's gold must not ship on customer A's delivery at all.

		Asserted at ``before_submit``, so nothing physical survives: no SLE, no GL, no event.
		"""
		other_batch = self._stocked_batch_for(OTHER_CUSTOMER)

		dn = self._delivery(other_batch, qty=30)  # header customer is still CUSTOMER
		dn.save()

		with self.assertRaises(frappe.ValidationError) as caught:
			dn.submit()

		message = str(caught.exception)
		self.assertIn("not entitled", message)

		# The error must not disclose who the owner is (spec §6.3).
		self.assertNotIn(OTHER_CUSTOMER, message)

		# And nothing physical may survive the rejection.
		self.assertEqual(self._sles(dn.name), [])
		self.assertEqual(self._events(dn.name), [])
		self.assertEqual(frappe.db.get_value("Delivery Note", dn.name, "docstatus"), 0)

	def test_the_rightful_owner_can_still_be_delivered(self):
		"""The positive control for the guard above. A block that blocks everything is not a
		guard, it is an outage -- so the valid case is proved in the same breath."""
		batch = self._stocked_batch_for(CUSTOMER)

		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()

		events = self._events(dn.name)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0].customer, CUSTOMER)

	def test_a_company_owned_batch_writes_no_event(self):
		"""The converse, and it is what keeps the ledger honest: only customer-owned metal
		creates a custody obligation. A batch with no ``custom_customer`` (or not tagged
		``Customer Goods``) is the company's own stock and must produce nothing at all."""
		se = self._receipt(qty=100)
		self._submit(se)
		batch = se.items[0].batch_no
		frappe.db.set_value("Batch", batch, "custom_inventory_type", None)

		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()

		self.assertEqual(self._events(dn.name), [])

	# -- deletion (the on_trash decision) ------------------------------------------
	def test_deleting_a_settled_delivery_is_refused(self):
		"""``reference_doctype``/``reference_docname`` is a Dynamic Link, which the database
		does not enforce and ``delete_doc`` does not clean up. Deleting the voucher would
		leave the custody event pointing at nothing while still counting toward the position.
		The delete is refused; cancelling is the supported action."""
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		dn.cancel()

		with self.assertRaises(frappe.ValidationError) as caught:
			frappe.delete_doc("Delivery Note", dn.name)

		self.assertIn("Customer Gold", str(caught.exception))
		self.assertTrue(frappe.db.exists("Delivery Note", dn.name))

	def test_the_guard_is_silent_when_there_are_no_events(self):
		"""The guard must not become a blanket ban on deleting delivery notes.

		Asserted against the hook directly rather than through ``frappe.delete_doc``, because
		``delete_doc`` never gets that far: core's ``check_if_doc_is_linked`` raises
		``LinkExistsError`` for ANY Delivery Note that has posted a Stock Ledger Entry,
		cancelled or not. Going through ``delete_doc`` would therefore prove only that core
		blocks it, and would keep passing even if this guard were deleted.

		That core check is also why this guard is defence in depth rather than the sole
		barrier: it exists to give a custody-specific reason, to fire *first*, and to hold
		when a caller passes ``ignore_links`` or the SLE rows have been purged -- none of
		which core's link check survives.
		"""
		se = self._receipt(qty=100)
		self._submit(se)
		batch = se.items[0].batch_no
		frappe.db.set_value("Batch", batch, "custom_inventory_type", None)

		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		self.assertEqual(
			self._events(dn.name), [], "fixture wrote an event it should not have"
		)

		# Must not raise.
		cgf.block_delete_with_customer_gold_events(dn)

	def test_the_guard_fires_when_there_are_events(self):
		"""The matching positive case, isolated the same way, so the pair is symmetric."""
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		self.assertTrue(self._events(dn.name))

		with self.assertRaises(frappe.ValidationError) as caught:
			cgf.block_delete_with_customer_gold_events(dn)

		self.assertIn("Customer Gold", str(caught.exception))

	# -- the position projection ---------------------------------------------------
	def test_position_reflects_the_delivery(self):
		batch = self._stocked_batch()
		before = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		after = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)
		self.assertEqual(flt(after - before), -30.0)


class TestFulfilmentValuation(TestFulfilmentLedger):
	"""C12 monetary half — does the event carry the value that actually left the books?

	Inherits every fixture and every behavioural test from ``TestFulfilmentLedger`` and re-runs
	them under the Nominal policy, which is the cheapest way to prove the value fields did not
	change any of the non-monetary behaviour. The three tests added at the bottom are the
	monetary claims themselves.

	Nominal runs here and in ``TestNominalValuation`` only, on the disposable site only. D01 is
	untouched by these passing.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.customer_gold_valuation_policy = "Nominal"
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype=SETTINGS_DOCTYPE)

	def test_the_policy_is_actually_nominal_for_this_class(self):
		"""Guard the guard. Without this, every monetary assertion below could pass vacuously
		on a NULL that Zero Value would have written anyway."""
		from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
			get_customer_gold_valuation_policy,
		)

		self.assertEqual(get_customer_gold_valuation_policy(), "Nominal")

	def test_carrying_value_matches_the_stock_ledger(self):
		"""The event's value IS the SLE's, not a second opinion.

		This also pins the hook ordering the implementation depends on: if ``doc_events``
		``on_submit`` ever ran BEFORE the controller's own ``update_stock_ledger``, there
		would be no SLE to read and the value would silently be NULL. That regression fails
		here rather than shipping as a quiet gap in the ledger.
		"""
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()

		sle_value = flt(
			frappe.db.get_value(
				"Stock Ledger Entry",
				{
					"voucher_type": "Delivery Note",
					"voucher_no": dn.name,
					"voucher_detail_no": dn.items[0].name,
					"is_cancelled": 0,
				},
				"stock_value_difference",
			)
		)

		events = self._events(dn.name)
		self.assertEqual(len(events), 1)
		recorded = frappe.db.get_value(
			"Customer Gold Ledger Entry", events[0].name, "cg_carrying_value_delta"
		)
		self.assertIsNotNone(
			recorded,
			"nominal policy wrote no carrying value -- hook ordering may have changed",
		)
		self.assertAlmostEqual(flt(recorded), sle_value, places=2)

	def test_the_value_is_negative_on_the_way_out(self):
		"""Direction is erpnext's, not ours — an outward move reduces carrying value."""
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()

		recorded = flt(
			frappe.db.get_value(
				"Customer Gold Ledger Entry",
				self._events(dn.name)[0].name,
				"cg_carrying_value_delta",
			)
		)
		self.assertLess(recorded, 0.0)

	def test_currency_is_stamped(self):
		"""A value without its currency is not a value."""
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()

		self.assertEqual(
			frappe.db.get_value(
				"Customer Gold Ledger Entry",
				self._events(dn.name)[0].name,
				"cg_currency",
			),
			frappe.get_cached_value("Company", COMPANY, "default_currency"),
		)

	def test_the_reversal_undoes_the_value_too(self):
		"""CG-T143 — a reversal must net the money to zero, not just the grams."""
		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		dn.cancel()

		rows = frappe.get_all(
			"Customer Gold Ledger Entry",
			filters={"reference_docname": dn.name},
			fields=["cg_carrying_value_delta"],
		)
		self.assertEqual(len(rows), 2, "expected the delivery and its reversal")

		# Assert the legs are non-zero BEFORE asserting they cancel. Two NULLs also sum to
		# zero, so the netting assertion alone passes vacuously the moment the value stops
		# being written -- which is exactly what a mutation run of ``_row_carrying_value``
		# demonstrated.
		values = [flt(r.cg_carrying_value_delta) for r in rows]
		self.assertTrue(
			all(v != 0.0 for v in values),
			f"a leg carries no value, so the netting below proves nothing: {values}",
		)
		self.assertAlmostEqual(flt(sum(values)), 0.0, places=2)


class TestBatchComponents(_CustomerGoldIntegrationCase):
	"""C09 — component provenance against a real database.

	The unit suite (``test_customer_gold_components``) patches the two DB readers and proves
	the arithmetic. This proves the part it cannot: that the rows actually reach
	``tabBatch Component``, that ``parent``/``parenttype``/``parentfield`` are right so
	``_recorded_components`` can read them back, and that the recursion works across genuinely
	stored rows rather than an in-memory dict.
	"""

	def _customer_batch(self, qty=100, customer=None):
		se = self._receipt(qty=qty, customer=customer or CUSTOMER)
		self._submit(se)
		return se.items[0].batch_no

	def _company_batch(self, qty=50):
		"""A batch that is NOT customer-owned — the company alloy side of a mixed melt."""
		se = self._receipt(qty=qty)
		self._submit(se)
		batch = se.items[0].batch_no
		frappe.db.set_value(
			"Batch", batch, {"custom_inventory_type": None, "custom_customer": None}
		)
		return batch

	# -- writing -------------------------------------------------------------------
	def test_components_reach_the_database_and_read_back(self):
		customer_batch = self._customer_batch(qty=100)
		company_batch = self._company_batch(qty=50)
		target = self._customer_batch(qty=10)

		cgc.record_batch_components(
			target,
			[(customer_batch, 20.0), (company_batch, 1.763)],
			voucher_type="Stock Entry",
		)

		rows = cgc._recorded_components(target)
		self.assertEqual(len(rows), 2, f"components did not persist: {rows}")
		self.assertAlmostEqual(sum(flt(r["qty"]) for r in rows), 21.763, places=3)

	def test_the_owner_is_stored_per_component(self):
		"""The whole point of C09: one batch, two owners, both recorded."""
		customer_batch = self._customer_batch(qty=100)
		company_batch = self._company_batch(qty=50)
		target = self._customer_batch(qty=10)

		cgc.record_batch_components(
			target, [(customer_batch, 20.0), (company_batch, 1.763)]
		)

		owners = {r["customer"] for r in cgc._recorded_components(target)}
		self.assertEqual(owners, {CUSTOMER, None})

	def test_the_company_share_is_readable_for_the_carve_out(self):
		"""This is the exact number the Metal Conversion carve-out consumes."""
		customer_batch = self._customer_batch(qty=100)
		company_batch = self._company_batch(qty=50)
		target = self._customer_batch(qty=10)

		cgc.record_batch_components(
			target, [(customer_batch, 20.0), (company_batch, 1.763)]
		)

		self.assertAlmostEqual(cgc.get_company_component_qty(target), 1.763, places=3)

	def test_rewriting_converges_instead_of_accumulating(self):
		"""Idempotency. The table is REPLACED, not appended to — a re-submit or a repost must
		not double the batch's recorded contents."""
		customer_batch = self._customer_batch(qty=100)
		target = self._customer_batch(qty=10)

		for _ in range(3):
			cgc.record_batch_components(target, [(customer_batch, 20.0)])

		rows = cgc._recorded_components(target)
		self.assertEqual(len(rows), 1)
		self.assertAlmostEqual(flt(rows[0]["qty"]), 20.0, places=3)

	# -- reading back --------------------------------------------------------------
	def test_a_batch_with_no_components_reports_zero(self):
		"""The default that makes the carve-out safe on every existing site."""
		self.assertEqual(
			cgc.get_company_component_qty(self._customer_batch(qty=10)), 0.0
		)

	def test_resolution_is_transitive_across_stored_rows(self):
		"""CG-T083, against the database rather than a dict.

		Generation 2 is made entirely from the mixed batch. Resolving it must see through to
		the ORIGINAL customer and company batches — if provenance stopped one level down, the
		company alloy inside would become invisible and a reverse conversion would hand it all
		back to the customer.
		"""
		customer_batch = self._customer_batch(qty=100)
		company_batch = self._company_batch(qty=50)
		mixed = self._customer_batch(qty=10)
		gen2 = self._customer_batch(qty=10)

		cgc.record_batch_components(
			mixed, [(customer_batch, 20.0), (company_batch, 1.763)]
		)
		cgc.record_batch_components(gen2, [(mixed, 21.763)])

		resolved = cgc.resolve_components(gen2, 21.763)

		sources = {c["source_batch"] for c in resolved}
		self.assertIn(customer_batch, sources)
		self.assertIn(company_batch, sources)
		self.assertNotIn(mixed, sources, "provenance stopped at the mixed batch")

		company = sum(flt(c["qty"]) for c in resolved if not c["customer"])
		self.assertAlmostEqual(company, 1.763, places=2)

	def test_zero_value_policy_records_no_money(self):
		"""D01 stays untouched: the ownership half of C09 is complete with no value at all.

		Asserts 0.0 rather than None, and that distinction is the point. Frappe renders every
		Currency column ``decimal(21,9) NOT NULL DEFAULT 0``, so a Currency field CANNOT hold
		NULL -- an earlier version of this test asserted None and failed with
		``AssertionError: 0.0 is not None``, which is how the limitation was found. The
		unambiguous "was this valued at all" marker lives on
		``Customer Gold Ledger Entry.cg_currency``, which is a varchar and really is nullable.
		"""
		customer_batch = self._customer_batch(qty=100)
		target = self._customer_batch(qty=10)

		cgc.record_batch_components(target, [(customer_batch, 20.0)])

		rows = cgc._recorded_components(target)
		self.assertEqual(flt(rows[0]["rate"]), 0.0)
		self.assertEqual(flt(rows[0]["amount"]), 0.0)

		# The ownership half -- the part C09 is actually about -- is fully populated.
		self.assertEqual(rows[0]["customer"], CUSTOMER)
		self.assertAlmostEqual(flt(rows[0]["qty"]), 20.0, places=3)


class TestFulfilmentReturns(TestFulfilmentLedger):
	"""REC-T014 — the return path, which had NO coverage at all until now.

	Mutation-checking the sign repair exposed this: reverting
	``signed = -per_serial`` to the old ``per_serial if is_return else -per_serial`` broke
	nothing, because every existing test delivered and none returned. A repair no test can
	detect the absence of is not verified, so this class exists to make that mutation fail.

	It uses erpnext's real ``make_sales_return`` rather than hand-building a return document,
	which matters for a second reason: a return built from a reference document goes through
	``StockController.make_bundle_for_non_rejected_qty`` (``stock_controller.py:556-565``), which
	does ``row.db_set({... "batch_no": ""})`` -- it WIPES ``batch_no`` before this module's
	handler runs. A hand-built fixture that sets ``batch_no`` directly cannot see that, which is
	precisely why the defect survived.
	"""

	def _return_of(self, delivery_note, qty):
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

		ret = make_sales_return(delivery_note.name)
		ret.items[0].qty = -abs(qty)
		# Keep against_sales_order: gke_customization's Delivery Note validator
		# (gke_customization/doc_events/delivery_note.py:12-17) throws
		# "Delivery can be created from Delivery Note Company" on any row without one, and
		# make_sales_return maps it across from the original. Clearing it breaks a real
		# production constraint the fixture must respect.
		ret.flags.ignore_mandatory = True
		ret.save()
		ret.submit()
		return ret

	def test_a_return_restores_custody_exactly_once(self):
		"""Deliver 3 of 10, return 1. The delta must be POSITIVE one, not negative one.

		erpnext builds a return row with an already-negative qty
		(``sales_and_purchase_return.py:540``) and enforces it (``:236``), then negates once more
		when it posts the SLE (``selling_controller.py:707``). Mirroring that means negating
		unconditionally. Branching on ``is_return`` applied the sign twice and made a return
		REDUCE the customer's position again.
		"""
		batch = self._stocked_batch(qty=10)

		dn = self._delivery(batch, qty=3)
		dn.save()
		dn.submit()
		delivered = self._events(dn.name)
		self.assertEqual(len(delivered), 1)
		self.assertLess(
			flt(delivered[0].cg_gross_qty_delta), 0, "a delivery must reduce custody"
		)

		ret = self._return_of(dn, 1)
		returned = self._events(ret.name)
		self.assertEqual(
			len(returned), 1, f"no custody event for the return: {returned}"
		)
		self.assertGreater(
			flt(returned[0].cg_gross_qty_delta),
			0,
			"a return must RESTORE custody; a negative delta is the double-negation defect",
		)
		self.assertEqual(returned[0].cg_event_kind, "Delivery Return")

	def test_the_return_nets_against_the_delivery(self):
		"""Deliver 3, return all 3 -> the two events cancel exactly."""
		batch = self._stocked_batch(qty=10)

		dn = self._delivery(batch, qty=3)
		dn.save()
		dn.submit()
		ret = self._return_of(dn, 3)

		total = flt(
			sum(flt(e.cg_gross_qty_delta) for e in self._events(dn.name))
			+ sum(flt(e.cg_gross_qty_delta) for e in self._events(ret.name)),
			3,
		)
		self.assertEqual(total, 0.0, "delivery and full return do not net to zero")

	def test_the_return_resolves_its_batch_from_the_bundle(self):
		"""The repair without which the sign fix is inert.

		``make_sales_return`` clears ``row.batch_no`` and moves the batch into a Serial and Batch
		Bundle. ``record_fulfilment`` used to read only ``row.batch_no``, so the row was skipped
		and NO event was written for any reference-built return.
		"""
		batch = self._stocked_batch(qty=10)

		dn = self._delivery(batch, qty=3)
		dn.save()
		dn.submit()
		ret = self._return_of(dn, 1)

		events = self._events(ret.name)
		self.assertEqual(len(events), 1)
		self.assertEqual(
			events[0].batch_no,
			batch,
			"the event did not resolve the batch from the bundle",
		)


class TestComponentApportionment(_CustomerGoldIntegrationCase):
	"""REC-T009 — a lane's sources must be attributed across its outputs EXACTLY ONCE.

	THE DEFECT, and why it needed a real document to catch
	------------------------------------------------------
	``record_batch_components`` is called from Serial and Batch Bundle ``after_insert``, and there
	is one bundle per produced Stock Entry Detail row. Every call used to receive the FULL lane
	source list, so a lane with two outputs recorded its inputs TWICE -- the exact double-counting
	the component table was built to fix (gap 3 in the ``Batch Component`` docstring), reproduced
	in the writer's own call site.

	Mutation-checking exposed that no test could see it: forcing ``_lane_output_share`` to return
	``1.0`` -- restoring the defect outright -- left the entire suite green. Hence this class.

	WHY A REPACK AND NOT A METAL CONVERSION
	---------------------------------------
	The writer fires for any Stock Entry whose purpose is ``Manufacture`` or ``Repack``
	(``serial_and_batch_bundle/doc_events/utils.py:116``); Metal Conversions is merely one
	producer of such entries. A Repack reaches the identical production path -- real document,
	real hooks, real bundles, real ``Batch Component`` rows -- without the Metal Conversions
	fixture chain. Nothing is monkeypatched: §4.4 explicitly forbids asserting the carve-out with
	``get_company_component_qty`` patched to a constant, and nothing here does.

	The lane tag is written directly onto the rows. That is data, not a mock: it is a real field
	on Stock Entry Detail and stamping it is exactly what ``metal_conversions.make_metal_stock_entry``
	does at ``:530``.
	"""

	LANE = f"Customer Goods|{CUSTOMER}"

	def _stocked_batch(self, qty=100):
		"""Receive customer gold and return the batch now holding it."""
		se = self._receipt(qty=qty)
		self._submit(se)
		return se.items[0].batch_no

	def _repack_two_outputs(self, source_batch, consumed=10.0, split=(6.0, 4.0)):
		"""One lane in, two output batches out, in a ``split`` ratio of the consumed quantity."""
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = REPACK_SE_TYPE
		se.purpose = "Repack"
		se.company = COMPANY
		se.posting_date = self.posting_date
		se.set_posting_time = 1
		se._customer = CUSTOMER

		se.append(
			"items",
			{
				"item_code": self.item,
				"qty": consumed,
				"s_warehouse": self.warehouse,
				"uom": "Gram",
				"stock_uom": "Gram",
				"conversion_factor": 1,
				"batch_no": source_batch,
				"use_serial_batch_fields": 1,
				"inventory_type": "Customer Goods",
				"customer": CUSTOMER,
				"custom_conversion_lane": self.LANE,
				"expense_account": self.difference_account,
			},
		)
		for qty in split:
			se.append(
				"items",
				{
					"item_code": self.item,
					"qty": qty,
					"t_warehouse": self.warehouse,
					"uom": "Gram",
					"stock_uom": "Gram",
					"conversion_factor": 1,
					"inventory_type": "Customer Goods",
					"customer": CUSTOMER,
					"custom_conversion_lane": self.LANE,
					"expense_account": self.difference_account,
				},
			)

		se.flags.ignore_mandatory = True
		se.save()
		se.submit()
		return se

	@staticmethod
	def _components(batch_no):
		return frappe.get_all(
			"Batch Component",
			filters={
				"parent": batch_no,
				"parenttype": "Batch",
				"parentfield": "custom_batch_components",
			},
			fields=["source_batch", "qty", "customer", "inventory_type"],
		)

	def test_each_source_is_attributed_across_the_outputs_exactly_once(self):
		"""The conservation property. 10 g consumed, split 6/4, must record 10 g total."""
		source = self._stocked_batch(qty=100)
		se = self._repack_two_outputs(source, consumed=10.0, split=(6.0, 4.0))

		outputs = [row.batch_no for row in se.items if row.t_warehouse and row.batch_no]
		self.assertEqual(len(outputs), 2, f"expected two output batches, got {outputs}")

		per_output = {b: self._components(b) for b in outputs}
		for batch, rows in per_output.items():
			self.assertTrue(rows, f"output batch {batch} recorded no components at all")

		total = flt(sum(flt(r["qty"]) for rows in per_output.values() for r in rows), 3)
		self.assertAlmostEqual(
			total,
			10.0,
			places=2,
			msg=(
				f"the lane's 10 g source was recorded as {total} g across its outputs. "
				f"20 g means the full source list reached BOTH outputs -- the apportionment "
				f"defect. Per output: "
				+ ", ".join(
					f"{b}={sum(flt(r['qty']) for r in rs):.3f}"
					for b, rs in per_output.items()
				)
			),
		)

	def test_each_output_receives_its_own_share(self):
		"""A 6/4 split must allocate 6/10 and 4/10 of the source, not the whole thing twice."""
		source = self._stocked_batch(qty=100)
		se = self._repack_two_outputs(source, consumed=10.0, split=(6.0, 4.0))

		by_qty = {}
		for row in se.items:
			if row.t_warehouse and row.batch_no:
				by_qty[flt(row.qty)] = flt(
					sum(flt(r["qty"]) for r in self._components(row.batch_no))
				)

		self.assertAlmostEqual(by_qty.get(6.0, 0.0), 6.0, places=2, msg=f"{by_qty}")
		self.assertAlmostEqual(by_qty.get(4.0, 0.0), 4.0, places=2, msg=f"{by_qty}")

	def test_ownership_survives_apportionment(self):
		"""Splitting a source must not lose whose metal it is."""
		source = self._stocked_batch(qty=100)
		se = self._repack_two_outputs(source, consumed=10.0, split=(6.0, 4.0))

		for row in se.items:
			if not (row.t_warehouse and row.batch_no):
				continue
			for component in self._components(row.batch_no):
				self.assertEqual(component["customer"], CUSTOMER)
				self.assertEqual(component["inventory_type"], "Customer Goods")


class TestCustomerGoldBalance(TestFulfilmentLedger):
	"""REC-T021 / SOP §5 — the custody ledger's opening balance.

	THE DEFECT THIS CLASS EXISTS TO PIN
	------------------------------------
	``Customer Gold Ledger Entry`` declares fifteen event kinds and code wrote three, none of
	them the receipt. So the ledger recorded only metal leaving and never metal arriving:
	``get_customer_gold_position`` returned ``0.0`` after a 10 g receipt and ``-3.0`` after
	delivering 3 g of that same gold — a negative holding for a customer who is owed metal.

	It survived because the one existing caller measured a *delta* across a single delivery,
	which is right whatever the baseline is. Every assertion below is on an ABSOLUTE position,
	which is the only kind that can fail on a missing opening balance.

	The SOP's first question is "how much of this customer's gold do we hold?" — these are that
	question.
	"""

	def _position(self, customer=CUSTOMER):
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		return cgf.get_customer_gold_position(COMPANY, customer, self.item)

	def test_a_receipt_creates_the_opening_balance(self):
		"""10 g in, position 10 g. Previously 0.0 — the ledger had no Receipt row at all."""
		opening = self._position()

		se = self._receipt(qty=10)
		self._submit(se)

		self.assertAlmostEqual(
			self._position() - opening,
			10.0,
			places=3,
			msg="the receipt wrote no opening balance; the ledger still only sees deliveries",
		)

	def test_delivery_reduces_the_balance_it_does_not_invert_it(self):
		"""Receive 10, deliver 3, hold 7. Previously -3.0."""
		opening = self._position()

		batch = self._stocked_batch(qty=10)
		dn = self._delivery(batch, qty=3)
		dn.save()
		dn.submit()

		held = self._position() - opening
		self.assertGreater(
			held, 0, f"position went negative ({held}) — no opening balance"
		)
		self.assertAlmostEqual(held, 7.0, places=3)

	def test_the_receipt_event_is_written_once_per_row(self):
		"""Row-level provenance, and idempotent under a repeated callback."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		se = self._receipt(qty=10)
		self._submit(se)

		def rows():
			return frappe.get_all(
				"Customer Gold Ledger Entry",
				filters={
					"reference_doctype": "Stock Entry",
					"reference_docname": se.name,
					"cg_event_kind": "Receipt",
				},
				fields=["cg_source_row", "cg_gross_qty_delta", "cg_stage", "customer"],
			)

		first = rows()
		self.assertEqual(len(first), 1)
		self.assertEqual(first[0].cg_source_row, se.items[0].name)
		self.assertEqual(first[0].cg_stage, "RM")
		self.assertEqual(first[0].customer, CUSTOMER)
		self.assertAlmostEqual(flt(first[0].cg_gross_qty_delta), 10.0, places=3)

		# A repeated callback must be absorbed by the cg_event_key uniqueness, not duplicated.
		cgf.record_receipt(se)
		self.assertEqual(
			len(rows()), 1, "a repeated callback wrote a second receipt event"
		)

	def test_cancelling_the_receipt_returns_the_balance_to_zero(self):
		"""The reversal is a new row, not a delete — and it nets the position back."""
		opening = self._position()

		se = self._receipt(qty=10)
		self._submit(se)
		self.assertAlmostEqual(self._position() - opening, 10.0, places=3)

		se.reload()
		se.cancel()

		self.assertAlmostEqual(
			self._position() - opening,
			0.0,
			places=3,
			msg="cancelling the receipt did not reverse its custody event",
		)
		reversals = frappe.get_all(
			"Customer Gold Ledger Entry",
			filters={"reference_docname": se.name, "cg_event_kind": "Reversal"},
		)
		self.assertEqual(len(reversals), 1, "expected exactly one reversal row")

	def test_two_customers_do_not_share_a_balance(self):
		"""The position is scoped by customer, not just company."""
		opening_a = self._position(CUSTOMER)
		opening_b = self._position(OTHER_CUSTOMER)

		se = self._receipt(qty=10, customer=OTHER_CUSTOMER)
		self._submit(se)

		self.assertAlmostEqual(
			self._position(OTHER_CUSTOMER) - opening_b, 10.0, places=3
		)
		self.assertAlmostEqual(
			self._position(CUSTOMER) - opening_a,
			0.0,
			places=3,
			msg="customer A's balance moved when customer B delivered gold",
		)


class TestLiabilitySettlement(TestFulfilmentLedger):
	"""SOP Example C — delivery CLEARS the liability. CG-T131/T132/T134/T137/T143.

	    THE HALF OF THE SOP THAT DID NOT EXIST
	    --------------------------------------
	    The SOP's spine is *"Receipt creates liability → … → Delivery/return clears liability"*.
	    Before this class there were **zero Journal Entries** anywhere in the module: the configured
	    COGS Adjustment account was validated and never posted to, and ``cg_settlement_voucher`` was
	    declared and never filled. Liability, once created, was permanent.

	These tests run under **Nominal**, which is now the approved policy (D01, 2026-09-15) but is
	    still not the default on any site. The oracle is the SOP's own: 10 g at ₹7,164.83/g.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.customer_gold_valuation_policy = "Nominal"
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype=SETTINGS_DOCTYPE)

	def _settlement_entries(self, voucher):
		names = {
			r.cg_settlement_voucher
			for r in frappe.get_all(
				"Customer Gold Ledger Entry",
				filters={
					"reference_docname": voucher,
					"cg_settlement_voucher": ["!=", ""],
				},
				fields=["cg_settlement_voucher"],
			)
			if r.cg_settlement_voucher
		}
		return sorted(names)

	def test_the_policy_is_actually_nominal_for_this_class(self):
		"""Guard the guard — without this, every assertion below is vacuous."""
		from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
			get_customer_gold_valuation_policy,
		)

		self.assertEqual(get_customer_gold_valuation_policy(), "Nominal")

	def test_delivery_posts_dr_liability_cr_cogs_adjustment(self):
		"""SOP Example C. 10 g delivered at ₹7,164.83/g → ₹71,648.30 cleared."""
		batch = self._stocked_batch(qty=10)

		dn = self._delivery(batch, qty=10)
		dn.save()
		dn.submit()

		entries = self._settlement_entries(dn.name)
		self.assertEqual(
			len(entries), 1, f"expected exactly one settlement JE, got {entries}"
		)

		je = frappe.get_doc("Journal Entry", entries[0])
		self.assertEqual(je.docstatus, 1)

		debits = {r.account: flt(r.debit_in_account_currency) for r in je.accounts}
		credits = {r.account: flt(r.credit_in_account_currency) for r in je.accounts}

		self.assertAlmostEqual(
			debits.get(self.liability_account, 0.0),
			71648.30,
			places=2,
			msg="the liability was not debited by the booked customer value",
		)
		self.assertAlmostEqual(
			credits.get(self.cogs_account, 0.0),
			71648.30,
			places=2,
			msg="the COGS adjustment was not credited",
		)

	def test_the_settlement_is_the_nominal_component_not_the_fg_value(self):
		"""S07 §7.2. Settling the item's full stock value would over-discharge the obligation."""
		batch = self._stocked_batch(qty=10)

		dn = self._delivery(batch, qty=4)
		dn.save()
		dn.submit()

		je = frappe.get_doc("Journal Entry", self._settlement_entries(dn.name)[0])
		settled = sum(flt(r.debit_in_account_currency) for r in je.accounts)

		events = self._events(dn.name)
		nominal = abs(sum(flt(e.cg_carrying_value_delta or 0) for e in events))

		self.assertAlmostEqual(
			settled,
			nominal,
			places=2,
			msg="the JE does not equal the customer's nominal component of the delivered rows",
		)
		self.assertAlmostEqual(settled, 4 * 7164.83, places=2)

	def test_undelivered_metal_stays_open(self):
		"""SOP Example D's first half — deliver 8 of 10, 2 g of obligation remains."""
		batch = self._stocked_batch(qty=10)

		dn = self._delivery(batch, qty=8)
		dn.save()
		dn.submit()

		je = frappe.get_doc("Journal Entry", self._settlement_entries(dn.name)[0])
		settled = sum(flt(r.debit_in_account_currency) for r in je.accounts)

		self.assertAlmostEqual(
			settled,
			57318.64,
			places=2,
			msg="8 g delivered must clear ₹57,318.64 — not the full ₹71,648.30",
		)

	def test_a_repeated_callback_does_not_settle_twice(self):
		"""Claimed per event via cg_settlement_voucher, so a retry finds nothing left."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch(qty=10)
		dn = self._delivery(batch, qty=10)
		dn.save()
		dn.submit()

		first = self._settlement_entries(dn.name)
		self.assertEqual(len(first), 1)

		cgf.record_fulfilment(dn)

		self.assertEqual(
			self._settlement_entries(dn.name),
			first,
			"a repeated callback posted a second settlement",
		)

	def test_cancelling_the_delivery_cancels_the_settlement(self):
		"""Custody and the GL must not disagree: a reversed event needs a cancelled JE."""
		batch = self._stocked_batch(qty=10)
		dn = self._delivery(batch, qty=10)
		dn.save()
		dn.submit()

		name = self._settlement_entries(dn.name)[0]
		self.assertEqual(frappe.db.get_value("Journal Entry", name, "docstatus"), 1)

		dn.reload()
		dn.cancel()

		self.assertEqual(
			frappe.db.get_value("Journal Entry", name, "docstatus"),
			2,
			"the delivery was cancelled but its settlement JE is still submitted",
		)


class TestZeroValuePostsNoSettlement(TestFulfilmentLedger):
	"""The default policy must post no accounting at all.

	Zero Value records custody only. A JE here would assert a settlement of an obligation that
	was never created — and this is the policy every real site runs today.
	"""

	def test_no_journal_entry_under_zero_value(self):
		from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
			get_customer_gold_valuation_policy,
		)

		self.assertEqual(get_customer_gold_valuation_policy(), "Zero Value")

		batch = self._stocked_batch(qty=10)
		dn = self._delivery(batch, qty=10)
		dn.save()
		dn.submit()

		self.assertTrue(self._events(dn.name), "custody must still be recorded")
		stamped = [
			e
			for e in frappe.get_all(
				"Customer Gold Ledger Entry",
				filters={"reference_docname": dn.name},
				fields=["cg_settlement_voucher"],
			)
			if e.cg_settlement_voucher
		]
		self.assertEqual(
			stamped, [], "Zero Value posted a settlement it should not have"
		)


class TestRawGoldReturn(TestFulfilmentLedger):
	"""SOP Example D — return unused gold at its BOOKED rate. CG-T147/T148/T149/T150/T151.

	The SOP states the rule twice, because it is the one most easily got wrong:

	    "If unused gold is returned, use its booked carrying nominal value; do not fetch a new rate."

	So the decisive test here is not that a return works — it is that a return of metal booked at
	Rs.7,164.83/g still clears Rs.14,329.66 **after the market rate has moved to Rs.7,500**. A
	market-rate return would clear Rs.15,000 and quietly hand the customer Rs.670.34 of GK's money.

	Before this class the flow did not exist in any form: no function, no doctype, no button, and
	Rs.14,329.66 appeared in no test.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.customer_gold_valuation_policy = "Nominal"
		settings.customer_gold_return_stock_entry_type = RETURN_SE_TYPE
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype=SETTINGS_DOCTYPE)

	def _return(self, batch, qty, **kw):
		from jewellery_erpnext.customer_subcontracting.customer_gold_return import (
			make_customer_gold_return,
		)

		return make_customer_gold_return(
			company=COMPANY,
			customer=kw.get("customer", CUSTOMER),
			batch_no=batch,
			qty=qty,
			warehouse=self.warehouse,
			item_code=self.item,
		)

	def test_the_booked_rate_is_recoverable_from_the_ledger(self):
		"""Everything else depends on this: the Receipt event must carry the rate."""
		from jewellery_erpnext.customer_subcontracting.customer_gold_return import (
			get_booked_rate,
		)

		batch = self._stocked_batch(qty=10)

		self.assertAlmostEqual(
			get_booked_rate(COMPANY, CUSTOMER, batch), 7164.83, places=2
		)

	def test_returning_two_grams_clears_the_booked_value(self):
		"""SOP Example D. 2 g booked at Rs.7,164.83/g clears Rs.14,329.66."""
		batch = self._stocked_batch(qty=10)

		se_name = self._return(batch, 2)
		se = frappe.get_doc("Stock Entry", se_name)

		self.assertEqual(se.docstatus, 1)
		self.assertAlmostEqual(flt(se.items[0].basic_rate), 7164.83, places=2)
		self.assertAlmostEqual(flt(se.items[0].basic_amount), 14329.66, places=2)

		events = self._events(se_name)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0].cg_event_kind, "Return")
		self.assertAlmostEqual(flt(events[0].cg_gross_qty_delta), -2.0, places=3)
		self.assertAlmostEqual(
			flt(events[0].cg_carrying_value_delta), -14329.66, places=2
		)

	def test_a_market_rate_move_does_not_change_the_return(self):
		"""THE point of the class. Rate moves to Rs.7,500 — the return is still Rs.14,329.66."""
		batch = self._stocked_batch(qty=10)

		# A genuine later quote, resolved the way a receipt would resolve it.
		self._ensure_gold_rate(add_days(self.posting_date, 1), 75000.00)

		se = frappe.get_doc("Stock Entry", self._return(batch, 2))

		self.assertAlmostEqual(
			flt(se.items[0].basic_amount),
			14329.66,
			places=2,
			msg="the return used a current market rate instead of the booked carrying value",
		)

	def test_the_liability_is_debited_not_a_second_expense(self):
		"""Cr Stock / Dr Customer Gold Liability — the receipt's posting in reverse."""
		batch = self._stocked_batch(qty=10)
		se = frappe.get_doc("Stock Entry", self._return(batch, 2))

		self.assertEqual(se.items[0].expense_account, self.liability_account)

		gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": se.name, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
		)
		debited = {r.account: flt(r.debit) for r in gl if flt(r.debit)}
		self.assertAlmostEqual(
			debited.get(self.liability_account, 0.0),
			14329.66,
			places=2,
			msg=f"liability was not debited by the booked value; GL was {gl}",
		)

	def test_returning_more_than_is_free_is_blocked(self):
		batch = self._stocked_batch(qty=10)

		with self.assertRaises(frappe.ValidationError) as caught:
			self._return(batch, 11)

		self.assertIn("free to return", str(caught.exception))

	def test_metal_already_delivered_cannot_also_be_returned(self):
		"""SOP Example D's whole shape: 8 g delivered, only 2 g may come back."""
		batch = self._stocked_batch(qty=10)

		dn = self._delivery(batch, qty=8)
		dn.save()
		dn.submit()

		# The 2 g that remain are returnable...
		se = frappe.get_doc("Stock Entry", self._return(batch, 2))
		self.assertAlmostEqual(flt(se.items[0].basic_amount), 14329.66, places=2)

		# ...and nothing is left after that.
		with self.assertRaises(frappe.ValidationError):
			self._return(batch, 0.5)

	def test_another_customers_batch_is_refused_without_naming_the_owner(self):
		batch = self._stocked_batch(qty=10)

		with self.assertRaises(frappe.ValidationError) as caught:
			self._return(batch, 2, customer=OTHER_CUSTOMER)

		message = str(caught.exception)
		self.assertIn("not held for", message)
		self.assertNotIn(batch, message)
		self.assertNotIn(CUSTOMER, message)

	def test_cancelling_the_return_restores_the_holding(self):
		batch = self._stocked_batch(qty=10)
		se_name = self._return(batch, 2)

		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		after_return = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)

		se = frappe.get_doc("Stock Entry", se_name)
		se.cancel()

		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item) - after_return,
			2.0,
			places=3,
			msg="cancelling the return did not give the 2 g back",
		)

	def test_metal_issued_to_the_floor_cannot_be_returned(self):
		"""The case the ledger ALONE cannot answer — and the reason physical stock is checked.

		Mutation testing found this gap: dropping the physical-stock half of the eligibility
		check broke no test, because in every other case the ledger already gave the right
		answer. It does not here.

		``Allocation``, ``Release`` and ``Production`` events are still never written, so metal
		issued to manufacturing leaves **no ledger trace at all** — the customer still "owns"
		10 g as far as the ledger is concerned, while only 4 g is physically in custody. Handing
		back 8 g of that would be handing back metal that is currently inside a job.

		Until those events exist, physical presence in the custody warehouse is the only honest
		test of "is this free", and this case is what holds that guard in place.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		# Relative to a baseline: the position is class-scoped and earlier cases have already
		# received metal for this customer.
		opening = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)
		batch = self._stocked_batch(qty=10)

		# 6 g leaves custody for a job, through an ordinary issue that is NOT the customer-gold
		# return path — so no custody event is written for it. That is the whole point: the
		# ledger will still say the customer owns 10 g.
		issue = frappe.new_doc("Stock Entry")
		issue.stock_entry_type = RETURN_SE_TYPE
		issue.purpose = "Material Issue"
		issue.company = COMPANY
		issue.posting_date = self.posting_date
		issue.set_posting_time = 1
		issue._customer = CUSTOMER
		issue.append(
			"items",
			{
				"item_code": self.item,
				"qty": 6,
				"s_warehouse": self.warehouse,
				"batch_no": batch,
				"use_serial_batch_fields": 1,
				"inventory_type": "Customer Goods",
				"customer": CUSTOMER,
				"allow_zero_valuation_rate": 1,
				"expense_account": self.difference_account,
			},
		)
		issue.flags.ignore_mandatory = True
		issue.save()
		issue.submit()

		# The ledger is unmoved — this is the divergence the physical check exists to catch.
		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item) - opening,
			10.0,
			places=3,
		)

		from jewellery_erpnext.customer_subcontracting.customer_gold_return import (
			get_returnable_qty,
		)

		self.assertAlmostEqual(
			get_returnable_qty(COMPANY, CUSTOMER, batch, self.warehouse, self.item),
			4.0,
			places=3,
			msg="metal on the shop floor is still being counted as free to return",
		)

		with self.assertRaises(frappe.ValidationError) as caught:
			self._return(batch, 8)
		self.assertIn("free to return", str(caught.exception))

		# The 4 g still in custody remains returnable.
		se = frappe.get_doc("Stock Entry", self._return(batch, 4))
		self.assertAlmostEqual(flt(se.items[0].basic_amount), 4 * 7164.83, places=2)


class TestNextOrderRevaluation(TestFulfilmentLedger):
	"""SOP Example E — revalue the leftover BEFORE it funds the next order. CG-T152/T153/T154.

	The SOP gives return and revaluation deliberately opposite rate rules, and this class exists
	to hold them apart:

	* a **return** uses the BOOKED rate (Example D) — the metal goes back;
	* a **revaluation** uses the CURRENT rate (Example E) — the metal stays and funds a new order.

	On the SOP's own figures the gap is Rs.670.34 on 2 g. Getting the two the wrong way round
	moves that amount between the customer and GK silently.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.customer_gold_valuation_policy = "Nominal"
		settings.customer_gold_return_stock_entry_type = RETURN_SE_TYPE
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype=SETTINGS_DOCTYPE)

	def _revalue(self, batch, new_rate=7500.00, customer=CUSTOMER):
		from jewellery_erpnext.customer_subcontracting.customer_gold_revaluation import (
			revalue_customer_gold,
		)

		return revalue_customer_gold(
			company=COMPANY,
			customer=customer,
			batch_no=batch,
			warehouse=self.warehouse,
			item_code=self.item,
			posting_date=self.posting_date,
			new_rate=new_rate,
		)

	def test_revaluing_two_grams_upward_posts_the_delta(self):
		"""SOP Example E. 2 g from Rs.7,164.83 to Rs.7,500 → Rs.15,000, an increase of Rs.670.34."""
		batch = self._stocked_batch(qty=2)

		entry, delta = self._revalue(batch)

		self.assertAlmostEqual(
			delta,
			670.34,
			places=2,
			msg="the revaluation delta is not the SOP's Rs.670.34",
		)

		sr = frappe.get_doc("Stock Reconciliation", entry)
		self.assertEqual(sr.docstatus, 1)
		self.assertAlmostEqual(flt(sr.items[0].valuation_rate), 7500.00, places=2)
		self.assertAlmostEqual(
			flt(sr.items[0].qty),
			2.0,
			places=3,
			msg="quantity moved during a revaluation",
		)

	def test_the_liability_is_credited_with_the_increase(self):
		"""Dr Stock / Cr Customer Gold Liability — the SOP's required direction."""
		batch = self._stocked_batch(qty=2)
		entry, _ = self._revalue(batch)

		gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": entry, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
		)
		credited = {r.account: flt(r.credit) for r in gl if flt(r.credit)}

		self.assertAlmostEqual(
			credited.get(self.liability_account, 0.0),
			670.34,
			places=2,
			msg=f"the liability was not credited with the increase; GL was {gl}",
		)

	def test_a_downward_revaluation_reverses_the_direction(self):
		"""Rs.7,000/g on 2 g booked at Rs.7,164.83 → a decrease of Rs.329.66."""
		batch = self._stocked_batch(qty=2)

		_, delta = self._revalue(batch, new_rate=7000.00)

		self.assertAlmostEqual(delta, -329.66, places=2)

	def test_the_same_rate_is_a_zero_delta(self):
		"""No artificial accounting for a revaluation that changes nothing."""
		batch = self._stocked_batch(qty=2)

		_, delta = self._revalue(batch, new_rate=7164.83)

		self.assertAlmostEqual(delta, 0.0, places=2)

	def test_the_event_records_value_without_quantity(self):
		"""A revaluation changes what the metal is worth, never how much there is."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch(qty=2)
		before = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)

		entry, _ = self._revalue(batch)

		events = self._events(entry)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0].cg_event_kind, "Revaluation")
		self.assertAlmostEqual(flt(events[0].cg_gross_qty_delta), 0.0, places=3)
		self.assertAlmostEqual(flt(events[0].cg_carrying_value_delta), 670.34, places=2)

		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item),
			before,
			places=3,
			msg="the customer's holding changed during a revaluation",
		)

	def test_a_revaluations_deliberate_zero_is_known(self):
		"""REC-T-CG-01 (local, not a CG-UPD case). A revaluation measures and finds no change.

		Its zeroes are a statement, not an absence. Left unlabelled they are byte-identical on
		disk to the zeroes a column default writes when nothing could be measured, and every
		revaluation in the history would then count against completeness -- reporting the ledger
		as full of gaps precisely where it is most certain.

		This reads the PERSISTED row rather than the source, because the first version of this
		test grepped ``customer_gold_revaluation`` for the assignment and a mutation that deleted
		that assignment survived it.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch(qty=2)
		entry, _ = self._revalue(batch)

		event = self._events(entry)[0]
		self.assertEqual(event.cg_fine_measurement_status, cgf.STATUS_KNOWN)
		self.assertEqual(event.cg_reference_measurement_status, cgf.STATUS_KNOWN)
		self.assertAlmostEqual(flt(event.cg_fine_gold_delta), 0.0, places=3)

		# ...and the consequence that actually matters: it does not spoil the subtotal.
		report = cgf.get_customer_gold_position_report(COMPANY, CUSTOMER, self.item)
		self.assertTrue(
			report["complete"],
			msg=f"a labelled revaluation made the position incomplete: {report['reasons']}",
		)

	def test_the_revaluation_history_endpoint_returns_what_it_posted(self):
		"""CG-UPD (local). ``get_revaluation_history`` had no caller and no decorator.

		It is the SOP Sec 8 "Revalued" report's query, so it was completed rather than deleted --
		but completing it means gating it, because it reads custody rows for whatever
		``company``/``customer`` the caller names.
		"""
		from jewellery_erpnext.customer_subcontracting.customer_gold_revaluation import (
			get_revaluation_history,
		)

		batch = self._stocked_batch(qty=2)
		entry, delta = self._revalue(batch)

		history = get_revaluation_history(COMPANY, CUSTOMER)
		self.assertTrue(
			history, msg="the revaluation it just posted is not in the history"
		)

		row = next(r for r in history if r.reference_docname == entry)
		self.assertAlmostEqual(flt(row.cg_carrying_value_delta), delta, places=2)
		self.assertEqual(row.batch_no, batch)

		# Scoped by batch, and it really filters rather than ignoring the argument.
		self.assertTrue(get_revaluation_history(COMPANY, CUSTOMER, batch_no=batch))
		self.assertFalse(
			get_revaluation_history(COMPANY, CUSTOMER, batch_no="CG-TEST-NO-SUCH-BATCH")
		)

	def test_the_revaluation_history_endpoint_refuses_a_user_without_ledger_read(self):
		"""Without the gate, any authenticated user could enumerate another customer's history."""
		from jewellery_erpnext.customer_subcontracting.customer_gold_revaluation import (
			get_revaluation_history,
		)

		# Control: it works for the privileged test user, so a refusal below is the gate and
		# not an unrelated failure.
		self.assertIsNotNone(get_revaluation_history(COMPANY, CUSTOMER))

		with patch.object(frappe, "has_permission", side_effect=frappe.PermissionError):
			with self.assertRaises(frappe.PermissionError):
				get_revaluation_history(COMPANY, CUSTOMER)

	def test_return_and_revaluation_use_opposite_rates(self):
		"""The heart of it: the same 2 g, valued two ways, differing by exactly Rs.670.34."""
		from jewellery_erpnext.customer_subcontracting.customer_gold_return import (
			make_customer_gold_return,
		)

		returned_batch = self._stocked_batch(qty=2)
		revalued_batch = self._stocked_batch(qty=2)

		# The market has moved to Rs.7,500 for both.
		se = frappe.get_doc(
			"Stock Entry",
			make_customer_gold_return(
				company=COMPANY,
				customer=CUSTOMER,
				batch_no=returned_batch,
				qty=2,
				warehouse=self.warehouse,
				item_code=self.item,
			),
		)
		_, delta = self._revalue(revalued_batch, new_rate=7500.00)

		# The return ignores it; the revaluation is entirely about it.
		self.assertAlmostEqual(flt(se.items[0].basic_amount), 14329.66, places=2)
		self.assertAlmostEqual(14329.66 + delta, 15000.00, places=2)

	def test_another_customers_batch_is_refused(self):
		batch = self._stocked_batch(qty=2)

		with self.assertRaises(frappe.ValidationError) as caught:
			self._revalue(batch, customer=OTHER_CUSTOMER)

		message = str(caught.exception)
		self.assertIn("not held for", message)
		self.assertNotIn(batch, message)

	def test_the_sop_narrative_end_to_end(self):
		"""SOP Examples D and E as one story — 10 g in, 8 g delivered, 2 g revalued for next time.

		This is the SOP's own worked sequence rather than an isolated case, and it is the one
		that shows the two rate rules co-existing: the delivery settles at the BOOKED rate, and
		the leftover is then restated at the CURRENT one.

		Note what is *correct* here and was initially mis-tested: after 8 g ships, the batch
		genuinely holds only 2 g, so revaluing "the whole batch" restates exactly the free
		metal. ``_reject_partial`` exists for the different case where part of the batch sits in
		another warehouse — that guard has no direct coverage yet and is recorded as such.
		"""
		batch = self._stocked_batch(qty=10)

		dn = self._delivery(batch, qty=8)
		dn.save()
		dn.submit()

		settled = sum(
			abs(flt(e.cg_carrying_value_delta or 0)) for e in self._events(dn.name)
		)
		self.assertAlmostEqual(settled, 57318.64, places=2)

		entry, delta = self._revalue(batch)

		self.assertAlmostEqual(
			delta,
			670.34,
			places=2,
			msg="the leftover 2 g was not revalued to Rs.7,500/g",
		)
		sr = frappe.get_doc("Stock Reconciliation", entry)
		self.assertAlmostEqual(flt(sr.items[0].qty), 2.0, places=3)
		self.assertAlmostEqual(flt(sr.items[0].valuation_rate), 7500.00, places=2)


class TestConservedQuantityBasis(TestFulfilmentLedger):
	"""Spec §5.2 — the ledger must record fine gold and reference quantity, not only gross.

	WHY THIS IS NOT COSMETIC
	------------------------
	Gross quantity is **not conserved through a purity change**. The SOP's Example B turns 10 g of
	24KT into 11.1 g at 90% — 1.1 g of company alloy joined it. Only fine gold is conserved:
	9.99 g in, 9.99 g out.

	So a ledger carrying gross grams alone cannot state a conversion without appearing to create
	customer metal from nothing. `cg_fine_gold_delta`, `cg_reference_qty_delta` and
	`cg_reference_purity` are declared on the DocType for exactly this and, until now, **had zero
	writers** — the same declared-but-unwritten pattern as the nine missing event kinds.
	"""

	def _receipt_event(self, se_name):
		rows = self._events(se_name)
		self.assertEqual(len(rows), 1)
		return frappe.get_doc("Customer Gold Ledger Entry", rows[0].name)

	def test_a_receipt_records_all_three_bases(self):
		"""10 g of a 99.9% item: gross 10.0, fine 9.99, reference 10.0."""
		se = self._receipt(qty=10)
		self._submit(se)

		event = self._receipt_event(se.name)

		self.assertAlmostEqual(flt(event.cg_gross_qty_delta), 10.0, places=3)
		self.assertAlmostEqual(
			flt(event.cg_fine_gold_delta),
			9.99,
			places=3,
			msg="fine gold was not recorded — gross alone cannot survive a conversion",
		)
		# Reference purity IS the item purity in this fixture, so reference == gross.
		self.assertAlmostEqual(flt(event.cg_reference_qty_delta), 10.0, places=3)
		self.assertAlmostEqual(flt(event.cg_reference_purity), 99.9, places=2)

	def test_the_reference_purity_is_snapshot_not_looked_up_later(self):
		"""A later master edit must not restate history."""
		se = self._receipt(qty=10)
		self._submit(se)
		event = self._receipt_event(se.name)
		self.assertAlmostEqual(flt(event.cg_reference_purity), 99.9, places=2)

		# The stored denominator is on the row itself, so reference qty is reproducible from the
		# event alone without consulting any master.
		reproduced = (
			flt(event.cg_gross_qty_delta) * 99.9 / flt(event.cg_reference_purity)
		)
		self.assertAlmostEqual(reproduced, flt(event.cg_reference_qty_delta), places=3)

	def test_a_delivery_carries_the_same_sign_on_every_basis(self):
		"""A negative custody movement must be negative on all three measures, not just gross."""
		batch = self._stocked_batch(qty=10)

		dn = self._delivery(batch, qty=4)
		dn.save()
		dn.submit()

		events = self._events(dn.name)
		self.assertEqual(len(events), 1)
		event = frappe.get_doc("Customer Gold Ledger Entry", events[0].name)

		self.assertLess(flt(event.cg_gross_qty_delta), 0)
		self.assertLess(
			flt(event.cg_fine_gold_delta),
			0,
			msg="fine gold did not follow the gross sign",
		)
		self.assertAlmostEqual(flt(event.cg_fine_gold_delta), -3.996, places=3)

	def test_a_reversal_undoes_every_basis_from_the_original(self):
		"""Never recomputed from today's masters — copied and negated from what was written."""
		batch = self._stocked_batch(qty=10)
		dn = self._delivery(batch, qty=4)
		dn.save()
		dn.submit()

		original = frappe.get_doc(
			"Customer Gold Ledger Entry", self._events(dn.name)[0].name
		)

		dn.reload()
		dn.cancel()

		reversals = [e for e in self._events(dn.name) if e.cg_event_kind == "Reversal"]
		self.assertEqual(len(reversals), 1)
		reversal = frappe.get_doc("Customer Gold Ledger Entry", reversals[0].name)

		self.assertAlmostEqual(
			flt(reversal.cg_fine_gold_delta),
			-flt(original.cg_fine_gold_delta),
			places=3,
		)
		self.assertAlmostEqual(
			flt(reversal.cg_reference_qty_delta),
			-flt(original.cg_reference_qty_delta),
			places=3,
		)
		self.assertAlmostEqual(
			flt(reversal.cg_reference_purity),
			flt(original.cg_reference_purity),
			places=2,
		)

	def _stocked(self, qty=10):
		"""Return (batch, voucher) for a real submitted receipt.

		``reference_docname`` is a Dynamic Link and Frappe validates it, so the synthetic events
		below still need a genuine Stock Entry to point at.
		"""
		se = self._receipt(qty=qty)
		self._submit(se)
		return se.items[0].batch_no, se.name

	def test_every_declared_measurement_option_has_a_writer(self):
		"""REC-T-CG-03 (local). The status columns must not repeat the defect they close.

		An adversarial audit of this work found that the first version of these fields offered
		``Not Applicable`` on both Selects and ``legacy_source_without_snapshot`` as a reason,
		and **nothing could ever produce any of them** -- declared-and-unwritten, the exact
		shape of the problem the columns exist to close, one level down.

		So this does not check a hardcoded list. It EXERCISES every branch of ``quantity_basis``,
		collects what actually comes out, and asserts the doctype offers that and nothing more.
		A new option with no writer fails here; so does a writer emitting a value the Select
		would reject on save.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		produced_status, produced_reason = set(), set()

		def collect(basis):
			produced_status.add(basis["cg_fine_measurement_status"])
			produced_status.add(basis["cg_reference_measurement_status"])
			if basis["cg_measurement_reason"]:
				produced_reason.add(basis["cg_measurement_reason"])

		collect(cgf.quantity_basis(None, 10.0, COMPANY))  # no item purity
		collect(cgf.quantity_basis(self.item, 10.0, COMPANY))  # fully resolved
		with patch.object(cgf, "reference_purity", return_value=None):
			collect(cgf.quantity_basis(self.item, 10.0, COMPANY))  # ambiguous setting
		with patch.object(cgf, "reference_purity", return_value=0.0):
			collect(cgf.quantity_basis(self.item, 10.0, COMPANY))  # zero denominator

		meta = frappe.get_meta(cgf.LEDGER_DOCTYPE)

		def options(fieldname):
			return {
				o for o in (meta.get_field(fieldname).options or "").split("\n") if o
			}

		for fieldname in (
			"cg_fine_measurement_status",
			"cg_reference_measurement_status",
		):
			self.assertEqual(
				options(fieldname),
				produced_status,
				msg=f"{fieldname} offers an option no writer produces, or is missing one",
			)

		self.assertEqual(options("cg_measurement_reason"), produced_reason)

		# The one exception, and it is deliberate: a report-side label for rows that predate
		# these columns. It is never stored, so it must NOT appear in the stored vocabulary.
		self.assertNotIn(cgf.REASON_LEGACY_SOURCE, options("cg_measurement_reason"))

	def test_an_unresolvable_purity_is_marked_unknown_not_left_as_a_bare_zero(self):
		"""CG-UPD-008 (partial). 'Not measured' and 'measured as nothing' are different facts.

		THIS TEST USED TO ASSERT ``None`` AND WAS WRONG
		-----------------------------------------------
		It checked what ``quantity_basis`` RETURNED and never what the database KEPT. Every
		numeric column here is ``NOT NULL DEFAULT 0`` and ``base_document.get_valid_dict()``
		puts float-like values through ``flt()``, where ``flt(None)`` is ``0.0``. The ``None``
		this test was satisfied by became a ``0.000000`` on disk, indistinguishable from the
		zero it existed to distinguish. The assertion passed at the boundary it did not cross.

		So it now asserts the contract that survives persistence: a status column, which IS
		NULLable, carries the distinction and a reason says why.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		basis = cgf.quantity_basis(None, 10.0, COMPANY)

		self.assertEqual(basis["cg_fine_measurement_status"], cgf.STATUS_UNKNOWN)
		self.assertEqual(basis["cg_reference_measurement_status"], cgf.STATUS_UNKNOWN)
		self.assertEqual(basis["cg_measurement_reason"], cgf.REASON_MISSING_ITEM_PURITY)

	def test_a_gold_free_alloy_measures_known_zero_not_unknown(self):
		"""CG-UPD-003 (FAILS its stated expectation). The other half of the distinction.

		A resolved purity of 0% is a measurement whose answer is nothing. Marking it Unknown
		would be just as wrong as marking an Unknown as zero -- it would put a real, auditable
		figure into the "could not tell" bucket and make the ledger look less complete than it is.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		with patch.object(cgf, "get_purity_percentage", return_value=0.0):
			zero_purity = cgf.quantity_basis(self.item, 10.0, COMPANY)

		# 0% resolves falsy, which is the same branch a missing attribute takes. That is a real
		# limit of the current purity chain and it is recorded rather than papered over: a
		# genuinely gold-free alloy is not yet separable from an item with no purity at all.
		self.assertEqual(zero_purity["cg_fine_measurement_status"], cgf.STATUS_UNKNOWN)

		# What IS separable, and what the ledger actually relies on: a measured zero DELTA.
		measured = cgf.quantity_basis(self.item, 0.0, COMPANY)
		self.assertEqual(measured["cg_fine_gold_delta"], 0)
		self.assertEqual(measured["cg_fine_measurement_status"], cgf.STATUS_KNOWN)
		self.assertIsNone(measured["cg_measurement_reason"])

	def test_the_status_survives_the_round_trip_through_the_database(self):
		"""CG-UPD-001. The boundary the old assertion never reached.

		Read back three ways -- raw SQL, ``get_doc``, and the projection API -- because the
		failure being guarded against is precisely one that a Python-side check cannot see.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch, voucher = self._stocked(10)

		unknown = cgf._write_event(
			cg_event_key=frappe.generate_hash(length=24),
			cg_event_kind="Receipt",
			cg_stage="RM",
			company=COMPANY,
			customer=CUSTOMER,
			reference_doctype="Stock Entry",
			reference_docname=voucher,
			item_code=self.item,
			batch_no=batch,
			cg_gross_qty_delta=10.0,
			**cgf.quantity_basis(None, 10.0, COMPANY),
		)

		# 1. Raw SQL -- what MariaDB actually holds.
		row = frappe.db.sql(
			"""SELECT cg_fine_gold_delta, cg_fine_measurement_status, cg_measurement_reason
			   FROM `tabCustomer Gold Ledger Entry` WHERE name = %s""",
			unknown,
			as_dict=True,
		)[0]
		self.assertEqual(
			flt(row.cg_fine_gold_delta),
			0.0,
			msg="the column is NOT NULL DEFAULT 0 -- the zero is expected, and is the point",
		)
		self.assertEqual(row.cg_fine_measurement_status, cgf.STATUS_UNKNOWN)
		self.assertEqual(row.cg_measurement_reason, cgf.REASON_MISSING_ITEM_PURITY)

		# 2. Through the ORM.
		self.assertEqual(
			frappe.get_doc(cgf.LEDGER_DOCTYPE, unknown).cg_fine_measurement_status,
			cgf.STATUS_UNKNOWN,
		)

		# 3. Through the projection, which is where a caller would meet it.
		report = cgf.get_customer_gold_position_report(COMPANY, CUSTOMER, self.item)
		self.assertGreaterEqual(report["unknown_count"], 1)
		self.assertFalse(
			report["complete"],
			msg="a subtotal drawn from an unmeasured row is incomplete, not exact",
		)
		self.assertIn(cgf.REASON_MISSING_ITEM_PURITY, report["reasons"])

	def test_a_complete_history_reports_complete(self):
		"""CG-UPD-002 (control half). Without it, ``complete`` could be hardwired to False."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		self._stocked(10)

		report = cgf.get_customer_gold_position_report(COMPANY, CUSTOMER, self.item)
		self.assertTrue(report["complete"])
		self.assertEqual(report["unknown_count"], 0)
		self.assertEqual(report["reasons"], {})
		self.assertAlmostEqual(report["known_total"], report["total"], places=3)

	def test_two_manufacturer_settings_and_no_company_wide_one_is_a_real_ambiguity(
		self,
	):
		"""CG-UPD-004 — the ambiguity ``reference_purity`` was written for, built for real.

		§6 of the review is explicit that monkey-patching owner or reference resolution cannot
		be reported as integration evidence, and the companion test below does exactly that at
		a declared unit boundary. This one builds the condition out of masters:

		    two Manufacturing Settings for this company, each naming a manufacturer,
		    and no company-wide one

		``reference_purity`` then finds two rows, no unique company-wide fallback, and returns
		``None`` -- where ``doc_events/stock_entry.py`` deliberately THROWS on the same masters.
		Both are right: a stock field the document depends on must not guess, and a custody
		record must not refuse to record metal that already moved.

		The second setting is removed in ``tearDown`` so the ambiguity does not leak into the
		rest of the class, whose rollback is class-scoped.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		# Positive control FIRST, per §6: show the resolver works on the ordinary masters
		# before making them ambiguous, or a None below proves only that something broke.
		self.assertAlmostEqual(
			flt(cgf.reference_purity(COMPANY)),
			cg_purity.DEFAULT_REFERENCE_PURITY,
			places=2,
			msg="the single-setting precedence was already broken before this test began",
		)

		second = "CG Test Manufacturer Two"
		if not frappe.db.exists("Manufacturer", second):
			frappe.get_doc({"doctype": "Manufacturer", "short_name": second}).insert(
				ignore_permissions=True
			)
		rival = frappe.new_doc("Manufacturing Setting")
		rival.manufacturer = second
		rival.company = COMPANY
		rival.check_purity = rival.check_colour = rival.check_touch = "Both"
		# A DIFFERENT reference item, so picking either arbitrarily would be visible as a
		# wrong number rather than a coincidentally right one.
		rival.pure_gold_item = self.operating_item
		rival.flags.ignore_mandatory = True
		rival.insert(ignore_permissions=True)

		try:
			settings = frappe.get_all(
				"Manufacturing Setting",
				filters={"company": COMPANY},
				fields=["name", "manufacturer"],
			)
			self.assertEqual(len(settings), 2)
			self.assertTrue(
				all(row.manufacturer for row in settings),
				msg="a company-wide setting would resolve the ambiguity and void this case",
			)

			# No arbitrary reference item is selected -- the review's stated expectation.
			self.assertIsNone(cgf.reference_purity(COMPANY))

			basis = cgf.quantity_basis(self.item, 10.0, COMPANY)

			# "Preserve known item fine grams": the item's own purity is unaffected by the
			# company's ambiguity, so fine gold stays measured.
			self.assertEqual(basis["cg_fine_measurement_status"], cgf.STATUS_KNOWN)
			self.assertAlmostEqual(flt(basis["cg_fine_gold_delta"]), 9.99, places=3)

			# "...and record the exact reference ambiguity."
			self.assertEqual(
				basis["cg_reference_measurement_status"], cgf.STATUS_UNKNOWN
			)
			self.assertEqual(
				basis["cg_measurement_reason"], cgf.REASON_AMBIGUOUS_SETTING
			)
		finally:
			frappe.delete_doc(
				"Manufacturing Setting", rival.name, force=True, ignore_permissions=True
			)

		# Restored, and proven restored rather than assumed.
		self.assertAlmostEqual(
			flt(cgf.reference_purity(COMPANY)),
			cg_purity.DEFAULT_REFERENCE_PURITY,
			places=2,
		)

	def test_a_missing_reference_denominator_leaves_fine_gold_known(self):
		"""CG-UPD-004 (unit boundary). The two statuses separate in practice.

		A known item purity makes fine grams measurable no matter what the company's
		Manufacturing Setting says. Only the reference DENOMINATOR is lost. Collapsing both
		into one status would discard a figure that was successfully measured.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		with patch.object(cgf, "reference_purity", return_value=None):
			basis = cgf.quantity_basis(self.item, 10.0, COMPANY)

		self.assertEqual(basis["cg_fine_measurement_status"], cgf.STATUS_KNOWN)
		self.assertAlmostEqual(flt(basis["cg_fine_gold_delta"]), 9.99, places=3)
		self.assertEqual(basis["cg_reference_measurement_status"], cgf.STATUS_UNKNOWN)
		self.assertEqual(basis["cg_measurement_reason"], cgf.REASON_AMBIGUOUS_SETTING)

	def test_a_zero_reference_denominator_is_invalid_never_known(self):
		"""CG-UPD-007 (zero only; negative and nonfinite NOT RUN). Present and unusable.

		Dividing by it would yield an infinity or raise inside a submit. Neither is a
		measurement, and 'Unknown' would file a fixable data defect under 'cannot be told'.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		with patch.object(cgf, "reference_purity", return_value=0.0):
			basis = cgf.quantity_basis(self.item, 10.0, COMPANY)

		self.assertEqual(basis["cg_reference_measurement_status"], cgf.STATUS_INVALID)
		self.assertEqual(
			basis["cg_measurement_reason"], cgf.REASON_MISSING_REFERENCE_ITEM
		)
		self.assertEqual(basis["cg_reference_qty_delta"], 0)
		self.assertEqual(basis["cg_fine_measurement_status"], cgf.STATUS_KNOWN)

	def test_a_reversal_copies_the_status_instead_of_re_deriving_it(self):
		"""REC-T-CG-02 (local). Repairing a master must not retro-upgrade the reversal.

		The reversal negates whatever the original recorded. If it re-ran ``quantity_basis``
		today, a Manufacturing Setting fixed since would make the reversal Known while the
		original it cancels stays Unknown -- and the pair would no longer agree about what was
		measured.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch, voucher = self._stocked(10)
		original = cgf._write_event(
			cg_event_key=frappe.generate_hash(length=24),
			cg_event_kind="Receipt",
			cg_stage="RM",
			company=COMPANY,
			customer=CUSTOMER,
			reference_doctype="Stock Entry",
			reference_docname=voucher,
			item_code=self.item,
			batch_no=batch,
			cg_gross_qty_delta=10.0,
			**cgf.quantity_basis(None, 10.0, COMPANY),
		)

		se = frappe.get_doc("Stock Entry", voucher)
		se.cancel()

		reversal = frappe.db.get_value(
			cgf.LEDGER_DOCTYPE,
			{"cg_reversal_of": original},
			["cg_fine_measurement_status", "cg_measurement_reason"],
			as_dict=True,
		)
		self.assertIsNotNone(reversal, msg="the unknown receipt was never reversed")
		self.assertEqual(reversal.cg_fine_measurement_status, cgf.STATUS_UNKNOWN)
		self.assertEqual(reversal.cg_measurement_reason, cgf.REASON_MISSING_ITEM_PURITY)

	def test_reference_purity_never_throws_on_an_ambiguous_setting(self):
		"""The stock path throws here on purpose; a custody record must not.

		``doc_events/stock_entry.py:222-248`` throws when a company keeps one Manufacturing
		Setting per manufacturer and none is company-wide. That is right for ``custom_pure_qty``.
		Failing a delivery for it would be wrong — the metal already moved.
		"""
		from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
			reference_purity,
		)

		# A company with no Manufacturing Setting at all is the ambiguous case's limit.
		self.assertIsNone(reference_purity("CG Nonexistent Co"))
		self.assertIsNone(reference_purity(None))


class TestPositionIsKindFiltered(TestFulfilmentLedger):
	"""Spec §5.4 — the position is a short formula, not a sum of everything.

	    Accepted receipt/opening source
	    + valid physical returns
	    − valid physical fulfilment
	    − valid raw returns
	    ± explicit corrections/reversals

	THE BUG THIS PREVENTS
	---------------------
	`get_customer_gold_position` previously summed `cg_gross_qty_delta` over EVERY event. That was
	safe only while no kind changed gross quantity without an equal and opposite partner.

	Conversion does. The SOP's Example B turns 10 g of 24KT into 11.1 g at 90% — 1.1 g of company
	alloy joined it. Written as a Conversion Out of −10 and a Conversion In of +11.1, an unfiltered
	sum reports **+1.1 g of customer metal that nobody ever delivered**.

	These tests write that exact pair directly into the ledger, which is the only way to prove the
	filter before the conversion writer exists.
	"""

	def _stocked(self, qty=10):
		"""Return (batch, the real Stock Entry that received it).

		The Stock Entry is needed because ``reference_docname`` is a Dynamic Link and Frappe
		validates it — a synthetic voucher name is rejected outright.
		"""
		se = self._receipt(qty=qty)
		self._submit(se)
		return se.items[0].batch_no, se.name

	def _write(self, kind, gross, fine, batch, voucher, item=None):
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		return cgf._write_event(
			cg_event_key=frappe.generate_hash(length=24),
			cg_event_kind=kind,
			cg_stage="RM",
			company=COMPANY,
			customer=CUSTOMER,
			reference_doctype="Stock Entry",
			reference_docname=voucher,
			item_code=item or self.item,
			batch_no=batch,
			cg_gross_qty_delta=gross,
			cg_fine_gold_delta=fine,
		)

	def test_a_conversion_pair_does_not_create_metal(self):
		"""SOP Example B written as events: 10 g out, 11.1 g in, position unchanged."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch, voucher = self._stocked(10)
		before = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)

		self._write("Conversion Out", -10.0, -9.99, batch, voucher)
		self._write("Conversion In", 11.1, 9.99, batch, voucher)

		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item),
			before,
			places=3,
			msg="the conversion pair leaked +1.1 g into the holding — the position is not filtered",
		)

	def test_fine_gold_is_conserved_across_that_same_pair(self):
		"""Gross is not conserved through a purity change; absolute gold content is."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch, voucher = self._stocked(10)
		before = cgf.get_customer_gold_fine_position(COMPANY, CUSTOMER)

		self._write("Conversion Out", -10.0, -9.99, batch, voucher)
		self._write("Conversion In", 11.1, 9.99, batch, voucher)

		self.assertAlmostEqual(
			cgf.get_customer_gold_fine_position(COMPANY, CUSTOMER), before, places=3
		)

	def test_stage_changing_kinds_are_all_excluded(self):
		"""Transfer, Production, Allocation, Release and Revaluation move stage, not custody."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch, voucher = self._stocked(10)
		before = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)

		for kind in (
			"Transfer Out",
			"Transfer In",
			"Production",
			"Allocation",
			"Release",
		):
			self._write(kind, 5.0, 5.0, batch, voucher)

		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item),
			before,
			places=3,
			msg="a stage-changing kind was counted as custody",
		)

	def test_loss_and_recovery_DO_move_the_position(self):
		"""Metal genuinely leaves and genuinely comes back — §5.4 includes both."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch, voucher = self._stocked(10)
		before = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)

		self._write("Approved Loss", -2.0, -1.998, batch, voucher)
		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item),
			before - 2.0,
			places=3,
		)

		self._write("Recovery", 0.5, 0.4995, batch, voucher)
		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item),
			before - 1.5,
			places=3,
		)

	def test_the_position_kind_list_matches_the_declared_vocabulary(self):
		"""A typo in POSITION_KINDS would silently drop a whole kind from the balance."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		declared = set(
			frappe.get_meta("Customer Gold Ledger Entry")
			.get_field("cg_event_kind")
			.options.split("\n")
		)
		declared.discard("")

		for kind in cgf.POSITION_KINDS:
			self.assertIn(
				kind, declared, f"{kind!r} is not a declared cg_event_kind option"
			)


class TestSchemaIncompleteGuards(TestFulfilmentLedger):
	"""The guards that were unreachable code, and are now reachable.

	THE DEFECT
	----------
	Three guards protect custody events on a site whose ledger schema is behind::

	    if not is_ledger_schema_ready():
	        if has_customer_gold_events(doc):
	            frappe.throw(... "Customer Gold Schema Incomplete" ...)
	        return

	``has_customer_gold_events`` opened with ``if not is_ledger_schema_ready(): return False``,
	and ``is_ledger_schema_ready`` is pure. Every caller asks the question from INSIDE the
	not-ready branch, so the inner call was always False and **the throw was dead code**. The
	comment directly above it reads *"failing silently is the one unacceptable outcome"*.

	It was not theoretical. ``gk`` and ``kg-gk`` carry an orphan ``Customer Gold Ledger Entry``
	DocType with 34 of the 35 shipped fields and no ``serial_no``, so on those sites cancelling a
	document that carried custody events returned SUCCESS and wrote no reversal, and deleting one
	was not blocked.

	WHY THESE TESTS PATCH ``is_ledger_schema_ready`` -- AND WHY THAT IS NOT CIRCULAR
	--------------------------------------------------------------------------------
	The bug lives in the interaction between two functions, so patching the one under test would
	hide it. These patch the OTHER one -- the schema probe -- to make this complete-schema site
	answer the way an orphan site answers, and then exercise the real ``has_customer_gold_events``
	and the real guards against real persisted rows. Nothing about the code path being asserted is
	stubbed.

	The alternative -- actually dropping ``serial_no`` from the live test site -- would prove the
	same thing and would leave the suite's own schema broken for every class after it, since
	rollback here is class-scoped.
	"""

	def _stocked(self, qty=10):
		se = self._receipt(qty=qty)
		self._submit(se)
		return se.items[0].batch_no, se.name

	def test_the_probe_sees_events_even_when_the_schema_is_behind(self):
		"""The unit of the fix: the probe no longer consults write-readiness at all."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		_, voucher = self._stocked(10)
		se = frappe.get_doc("Stock Entry", voucher)

		# Control first: it must see the events under normal conditions, or the negative below
		# would pass for the wrong reason.
		self.assertTrue(cgf.has_customer_gold_events(se))

		with patch.object(cgf, "is_ledger_schema_ready", return_value=False):
			self.assertTrue(
				cgf.has_customer_gold_events(se),
				msg="the probe still short-circuits on schema readiness -- the guards it "
				"feeds are unreachable again",
			)

	def test_cancelling_a_stock_entry_blocks_instead_of_silently_skipping(self):
		"""``reverse_receipt``'s guard. Previously returned None and wrote no reversal."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		_, voucher = self._stocked(10)
		se = frappe.get_doc("Stock Entry", voucher)

		with patch.object(cgf, "is_ledger_schema_ready", return_value=False):
			with self.assertRaises(frappe.ValidationError) as caught:
				cgf.reverse_receipt(se)

		self.assertIn("schema is incomplete", str(caught.exception).lower())

	def test_cancelling_a_delivery_blocks_instead_of_silently_skipping(self):
		"""``reverse_fulfilment``'s guard -- the same defect on the outbound side."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()
		self.assertTrue(self._events(dn.name), msg="no custody event to protect")

		with patch.object(cgf, "is_ledger_schema_ready", return_value=False):
			with self.assertRaises(frappe.ValidationError) as caught:
				cgf.reverse_fulfilment(dn)

		self.assertIn("schema is incomplete", str(caught.exception).lower())

	def test_a_document_with_no_events_still_cancels_quietly(self):
		"""The other half, and the one that keeps the blast radius honest.

		A site that never used the feature must see exactly its previous behaviour. If this
		throws, the guard has stopped being about custody events and started being about the
		schema, which would block every cancellation on every site running an older schema.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		se = self._receipt(qty=5)
		self._submit(se)
		frappe.db.delete(cgf.LEDGER_DOCTYPE, {"reference_docname": se.name})

		with patch.object(cgf, "is_ledger_schema_ready", return_value=False):
			self.assertIsNone(
				cgf.reverse_receipt(frappe.get_doc("Stock Entry", se.name))
			)

	def test_deletion_protection_is_not_disabled_by_an_incomplete_schema(self):
		"""``block_delete_with_customer_gold_events`` returned early on the same test.

		It reads ``name`` and the two reference columns -- all of which the 34-field orphan
		carries -- so gating it on WRITE-readiness switched deletion protection off on exactly
		the sites whose schema was behind.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch()
		dn = self._delivery(batch, qty=30)
		dn.save()
		dn.submit()

		with patch.object(cgf, "is_ledger_schema_ready", return_value=False):
			with self.assertRaises(frappe.ValidationError) as caught:
				cgf.block_delete_with_customer_gold_events(dn)

		self.assertIn("cannot be deleted", str(caught.exception).lower())


class TestDispatcherDiagnostics(TestRawGoldReturn):
	"""A dropped custody row must leave a trace -- and a handled one must not.

	The dispatcher used to drop rows with a bare ``continue`` and no diagnostic, while its two
	sibling writers ``log_error`` on the very same condition. Its test was ``!= 1``, so it also
	swallowed every ZERO-batch row: it dropped strictly more than the siblings while saying
	strictly less. A custody movement vanished and the position drifted from the stock ledger
	with nothing anywhere to say why.

	The second half of this class is the more important one. Adding a diagnostic to the
	unclassified branch immediately produced SEVEN false alarms across one run of the return
	suite, because a Customer Gold return is a one-sided customer-owned Material Issue and falls
	straight through to that branch -- despite being fully handled by ``_record_return_event``.
	A diagnostic that cries wolf on a legitimate path is worse than the silence it replaced.
	"""

	UNCLASSIFIED = "Customer Gold: unclassified one-sided movement"

	def _noise(self):
		return frappe.db.count("Error Log", {"method": self.UNCLASSIFIED})

	def test_a_handled_return_logs_nothing(self):
		"""The regression guard for the false alarms. Mutation-verified: removing the skip
		that this asserts puts all seven back."""
		batch = self._stocked_batch(qty=10)
		before = self._noise()

		self._return(batch, 2.0)

		self.assertEqual(
			self._noise(),
			before,
			msg="a fully handled Customer Gold return was reported as an unclassified "
			"movement -- the diagnostic is crying wolf on a legitimate path",
		)

	def test_a_genuinely_unclassified_customer_row_does_log(self):
		"""The positive half. Without it the test above passes on a dead diagnostic.

		A real, submitted, customer-owned, one-sided Stock Entry is driven into the branch by
		suppressing only the RECOGNITION step -- ``_is_customer_gold_return`` -- so the document,
		its batch, its owner and its warehouses are all genuine and only the "this one is already
		handled" answer changes. That is exactly the shape the branch exists for: customer metal
		that moved and that no writer claimed.

		Suppressing the predicate rather than the logging keeps the code under test real. The
		branch, its condition and its message are all executed for their own reasons.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch(qty=10)
		before = self._noise()

		with patch.object(cgf, "_is_customer_gold_return", return_value=False):
			self._return(batch, 2.0)

		self.assertGreater(
			self._noise(),
			before,
			msg="customer metal moved one-sided, no custody event was written, and nothing "
			"recorded that it had happened",
		)

	def test_the_diagnostic_names_the_document_and_the_row(self):
		"""A log nobody can act on is not much better than no log.

		It must also NOT name the owning customer, and must not name the batch either -- this
		app builds batch ids as ``{customer}-...-{item}-{seq}``, so naming the batch discloses
		the owner just as surely. Same rule the entitlement error follows.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch(qty=10)

		with patch.object(cgf, "_is_customer_gold_return", return_value=False):
			entry = self._return(batch, 2.0)

		logged = frappe.get_all(
			"Error Log",
			filters={"method": self.UNCLASSIFIED},
			fields=["error"],
			order_by="creation desc",
			limit=1,
		)
		self.assertTrue(logged, msg="no diagnostic was written at all")
		message = logged[0].error

		self.assertIn(entry, message, msg="the log does not name the document")
		self.assertIn("Material Issue", message)
		self.assertNotIn(CUSTOMER, message, msg="the log names the owning customer")
		self.assertNotIn(batch, message, msg="the batch id embeds the customer code")


class TestPolicyVersionIsStamped(TestFulfilmentLedger):
	"""``cg_policy_version`` had no writer, and the policy was read live everywhere.

	The field's own description is the requirement: *"The accounting policy in force when this
	event posted. Historical behaviour must never depend on today's configuration."* Until this
	was fixed the column was empty on every row while
	``get_customer_gold_valuation_policy()`` was consulted LIVE at a dozen call sites -- so
	switching Nominal to Zero Value silently reinterpreted every event ever written.
	"""

	def _receipt_event(self, se_name):
		return frappe.get_doc(
			"Customer Gold Ledger Entry",
			frappe.db.get_value(
				"Customer Gold Ledger Entry",
				{"reference_docname": se_name, "cg_event_kind": "Receipt"},
				"name",
			),
		)

	def test_an_event_records_the_policy_in_force_when_it_posted(self):
		from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
			get_customer_gold_valuation_policy,
		)

		se = self._receipt(qty=5)
		self._submit(se)

		event = self._receipt_event(se.name)
		self.assertEqual(event.cg_policy_version, get_customer_gold_valuation_policy())
		self.assertTrue(event.cg_policy_version, msg="the column is still empty")
		self.assertTrue(event.cg_recorded_at, msg="cg_recorded_at has no writer")

	def test_switching_the_policy_does_not_rewrite_history(self):
		"""The whole point. An event posted under one policy keeps saying so."""
		se = self._receipt(qty=5)
		self._submit(se)
		event = self._receipt_event(se.name)
		posted_under = event.cg_policy_version

		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		was = settings.customer_gold_valuation_policy
		other = "Nominal" if was != "Nominal" else "Zero Value"
		try:
			settings.customer_gold_valuation_policy = other
			settings.save(ignore_permissions=True)
			frappe.clear_cache(doctype=SETTINGS_DOCTYPE)

			self.assertEqual(
				frappe.db.get_value(
					"Customer Gold Ledger Entry", event.name, "cg_policy_version"
				),
				posted_under,
				msg="flipping the policy changed what a historical event claims it "
				"posted under",
			)
		finally:
			settings = frappe.get_doc(SETTINGS_DOCTYPE)
			settings.customer_gold_valuation_policy = was
			settings.save(ignore_permissions=True)
			frappe.clear_cache(doctype=SETTINGS_DOCTYPE)

	def test_a_reversal_copies_the_policy_rather_than_re_deriving_it(self):
		"""Same discipline the measurement statuses already follow.

		THE POLICY IS SWITCHED BETWEEN THE RECEIPT AND THE CANCEL, AND THAT IS THE TEST.
		The first version of this did not, and it passed a mutation that deleted the copy
		outright -- because ``_write_event``'s ``setdefault`` then stamped TODAY's policy, which
		with an unchanged policy is the same string. It asserted nothing. Copy and re-derive can
		only be told apart when the two answers differ.
		"""
		se = self._receipt(qty=5)
		self._submit(se)
		original = self._receipt_event(se.name)
		posted_under = original.cg_policy_version
		self.assertTrue(
			posted_under, msg="nothing was stamped, so nothing can be copied"
		)

		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		other = "Nominal" if posted_under != "Nominal" else "Zero Value"
		try:
			settings.customer_gold_valuation_policy = other
			settings.save(ignore_permissions=True)
			frappe.clear_cache(doctype=SETTINGS_DOCTYPE)

			from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
				get_customer_gold_valuation_policy,
			)

			self.assertEqual(
				get_customer_gold_valuation_policy(),
				other,
				msg="the policy did not actually change, so this test proves nothing",
			)

			frappe.get_doc("Stock Entry", se.name).cancel()

			reversal = frappe.db.get_value(
				"Customer Gold Ledger Entry",
				{"cg_reversal_of": original.name},
				"cg_policy_version",
			)
			self.assertEqual(
				reversal,
				posted_under,
				msg=f"the reversal re-derived today's policy ({other}) instead of copying "
				f"the original's ({posted_under})",
			)
			self.assertNotEqual(reversal, other)
		finally:
			settings = frappe.get_doc(SETTINGS_DOCTYPE)
			settings.customer_gold_valuation_policy = posted_under
			settings.save(ignore_permissions=True)
			frappe.clear_cache(doctype=SETTINGS_DOCTYPE)


class TestStateChangingEndpointsArePostOnly(_CustomerGoldIntegrationCase):
	"""Both new endpoints submit documents. Neither should be reachable by a GET.

	This change set already establishes the standard -- it hardens ``update_bom_detail`` and
	``update_tracking_bom_detail`` to ``methods=["POST"]`` with detailed reasoning -- and then
	did not apply it to its own two new endpoints.
	"""

	def _allowed(self, fn):
		return frappe.allowed_http_methods_for_whitelisted_func[fn]

	def test_the_return_endpoint_is_post_only(self):
		from jewellery_erpnext.customer_subcontracting.customer_gold_return import (
			make_customer_gold_return,
		)

		self.assertIn(make_customer_gold_return, frappe.whitelisted)
		self.assertEqual(self._allowed(make_customer_gold_return), ["POST"])

	def test_the_revaluation_endpoint_is_post_only(self):
		from jewellery_erpnext.customer_subcontracting.customer_gold_revaluation import (
			revalue_customer_gold,
		)

		self.assertIn(revalue_customer_gold, frappe.whitelisted)
		self.assertEqual(self._allowed(revalue_customer_gold), ["POST"])

	def test_neither_endpoint_allows_guest_access(self):
		from jewellery_erpnext.customer_subcontracting.customer_gold_return import (
			make_customer_gold_return,
		)
		from jewellery_erpnext.customer_subcontracting.customer_gold_revaluation import (
			get_revaluation_history,
			revalue_customer_gold,
		)

		for fn in (
			make_customer_gold_return,
			revalue_customer_gold,
			get_revaluation_history,
		):
			self.assertNotIn(fn, frappe.guest_methods)

	def test_the_read_endpoint_stays_readable_by_get(self):
		"""``get_revaluation_history`` changes nothing, so restricting it to POST would be
		wrong -- the distinction being asserted is state change, not blanket hardening."""
		from jewellery_erpnext.customer_subcontracting.customer_gold_revaluation import (
			get_revaluation_history,
		)

		self.assertIn(get_revaluation_history, frappe.whitelisted)
		self.assertIn("GET", self._allowed(get_revaluation_history))


class TestCrossItemProjection(TestPositionIsKindFiltered):
	"""§2.3 — the residual the kind filter left behind.

	Excluding conversion from ``POSITION_KINDS`` stopped the +1.1 g leak. It did not make the
	gross sum a custody balance, and a review traced the number that replaced the leak:

	    Receipt      item A 99.9%   +10.000   counted
	    Conversion Out item A       -10.000   NOT counted
	    Conversion In  item B        +13.249  NOT counted
	    Delivery       item B        -13.249  counted
	                                 -------
	                                  -3.249  <- an artificial shortfall

	The receipt is measured in item A's gross grams and the delivery in item B's. Adding them
	is adding two different units. Nothing was lost; the projection was wrong.

	Fine gold is the basis that survives, because it is the same unit on both sides of a
	conversion: +9.990 - 9.990 = 0.

	NOT COVERED BY THE EXISTING SUITE, which is why the defect lived: the conversion tests use a
	same-item repack, and the delivery tests deliver a raw receipt batch. Receipt, convert,
	deliver -- across two items -- appears nowhere else.
	"""

	#: 10 g of 99.9% carries 9.99 g of fine gold. Restated against a 75.4% item that same fine
	#: gold is 9.99 / 0.754 g gross. The numbers are the fixtures' real purities, not borrowed
	#: ones, so the arithmetic can be checked against the masters.
	FINE = 9.99
	GROSS_A = 10.0
	GROSS_B = 13.249

	def _receipt_convert_deliver(self):
		"""Build the exact three-item-code history above and return the receipt batch."""
		batch, voucher = self._stocked(self.GROSS_A)

		self._write("Conversion Out", -self.GROSS_A, -self.FINE, batch, voucher)
		self._write(
			"Conversion In",
			self.GROSS_B,
			self.FINE,
			None,
			voucher,
			item=self.operating_item,
		)
		self._write(
			"Delivery",
			-self.GROSS_B,
			-self.FINE,
			None,
			voucher,
			item=self.operating_item,
		)
		return batch, voucher

	def test_fine_gold_closes_to_zero_across_the_conversion(self):
		"""The authoritative custody basis. Everything the customer gave has gone back out."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		before = cgf.get_customer_gold_fine_position(COMPANY, CUSTOMER)
		self._receipt_convert_deliver()

		self.assertAlmostEqual(
			cgf.get_customer_gold_fine_position(COMPANY, CUSTOMER),
			before,
			places=3,
			msg="fine gold is conserved through a purity change; this must net to zero",
		)

	def test_a_customer_wide_gross_holding_cannot_be_asked_for(self):
		"""The -3.249 shape is unreachable because the question is no longer answerable.

		Not a clamp and not a corrected sum -- the sum across items of different purities has
		no unit, so there is no right number to return. Refusing is the only honest answer.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		self._receipt_convert_deliver()

		# Omitting it is a TypeError from the signature itself -- the earliest possible signal,
		# and one a linter sees without running anything.
		with self.assertRaises(TypeError) as omitted:
			cgf.get_customer_gold_position(COMPANY, CUSTOMER)
		self.assertIn("item_code", str(omitted.exception))

		# Passing it as None is a caller who meant "all items". That deserves the explanation,
		# not a signature error, so it is a separate check with a message that says where to go.
		with self.assertRaises(ValueError) as blank:
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, None)
		self.assertIn("item_code", str(blank.exception))
		self.assertIn("get_customer_gold_fine_position", str(blank.exception))

	def test_gross_stays_per_item_and_each_item_is_self_consistent(self):
		"""Scoped to one item the gross figure still means something exact.

		Item A: 10 g arrived as A and none left as A -- it left as B, after conversion.
		Item B: 13.249 g was delivered and none was ever received as B.

		Neither is a physical stock figure and neither claims to be; ``cg_stage`` says where
		the metal is and fine gold says how much of it is the customer's.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		a_before = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)
		b_before = cgf.get_customer_gold_position(
			COMPANY, CUSTOMER, self.operating_item
		)

		self._receipt_convert_deliver()

		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item) - a_before,
			self.GROSS_A,
			places=3,
		)
		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.operating_item)
			- b_before,
			-self.GROSS_B,
			places=3,
		)

	def test_free_quantity_inherits_the_same_requirement(self):
		"""``held - reserved`` is built from the gross position, so it carries the same limit."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		with self.assertRaises(TypeError):
			cgf.get_customer_gold_free_quantity(COMPANY, CUSTOMER)

		with self.assertRaises(ValueError):
			cgf.get_customer_gold_free_quantity(COMPANY, CUSTOMER, None)


class TestCustodyTransfer(TestFulfilmentLedger):
	"""Spec §5 — Transfer Out / Transfer In, the custody move that left no trace at all.

	Before this, moving a customer's metal from the vault to the shop floor was invisible to the
	ledger. That is why `customer_gold_return.get_returnable_qty` has to infer "is this free" from
	physical warehouse presence: `cg_stage` could not answer, because nothing ever wrote
	`Transit` or `WIP`.

	A transfer writes TWO events — one row is simultaneously a departure and an arrival, and
	collapsing them into one signed event would lose exactly the stage transition being recorded.
	They net to zero in the position; `cg_stage` carries the meaning.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.wip_warehouse = cls._ensure_warehouse("CG Test Floor")
		frappe.db.set_value(
			"Warehouse",
			cls.wip_warehouse,
			"department",
			cls._ensure_department("CG Test Dept"),
		)

	def _transfer(self, batch, qty, target):
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = TRANSFER_SE_TYPE
		se.purpose = "Material Transfer"
		se.company = COMPANY
		se.manufacturer = MANUFACTURER
		se.posting_date = self.posting_date
		se.set_posting_time = 1
		se.append(
			"items",
			{
				"item_code": self.item,
				"qty": qty,
				"s_warehouse": self.warehouse,
				"t_warehouse": target,
				"batch_no": batch,
				"use_serial_batch_fields": 1,
				"inventory_type": "Customer Goods",
				"customer": CUSTOMER,
				"allow_zero_valuation_rate": 1,
				"expense_account": self.difference_account,
			},
		)
		se.flags.ignore_mandatory = True
		se.save()
		se.submit()
		return se

	def test_a_transfer_writes_an_out_and_an_in(self):
		batch = self._stocked_batch(qty=10)

		se = self._transfer(batch, 6, self.wip_warehouse)
		kinds = {e.cg_event_kind: e for e in self._events(se.name)}

		self.assertIn("Transfer Out", kinds)
		self.assertIn("Transfer In", kinds)
		self.assertAlmostEqual(
			flt(kinds["Transfer Out"].cg_gross_qty_delta), -6.0, places=3
		)
		self.assertAlmostEqual(
			flt(kinds["Transfer In"].cg_gross_qty_delta), 6.0, places=3
		)

	def test_the_stage_ladder_finally_has_writers(self):
		"""`WIP` had no writer anywhere. A warehouse with a department is what supplies it."""
		batch = self._stocked_batch(qty=10)

		se = self._transfer(batch, 6, self.wip_warehouse)
		kinds = {e.cg_event_kind: e for e in self._events(se.name)}

		self.assertEqual(kinds["Transfer Out"].cg_stage, "RM")
		self.assertEqual(
			kinds["Transfer In"].cg_stage,
			"WIP",
			"a departmented warehouse must read as shop floor, not raw material",
		)

	def test_a_transit_warehouse_reads_as_transit(self):
		"""Derived from erpnext's own `warehouse_type`, not a second mapping to keep in step."""
		transit = self._ensure_warehouse("CG Test Transit")
		frappe.db.set_value("Warehouse", transit, "warehouse_type", "Transit")
		batch = self._stocked_batch(qty=10)

		se = self._transfer(batch, 3, transit)
		kinds = {e.cg_event_kind: e for e in self._events(se.name)}

		self.assertEqual(kinds["Transfer In"].cg_stage, "Transit")

	def test_a_transfer_does_not_change_the_holding(self):
		"""The metal moved; the customer still has exactly as much of it."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch(qty=10)
		before = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)

		self._transfer(batch, 6, self.wip_warehouse)

		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item),
			before,
			places=3,
			msg="an internal move changed the customer's holding",
		)

	def test_no_carrying_value_is_invented_on_an_internal_move(self):
		"""An internal move changes where metal is, never what it is worth.

		Asserted as ZERO rather than NULL, and that is a schema fact rather than a choice:
		Frappe's Currency columns are ``decimal(21,9) NOT NULL DEFAULT 0``, so "not valued"
		is simply not representable in one. The writer passes ``None`` and the column stores
		``0``. Zero is the right answer here anyway — the value genuinely did not change — but
		elsewhere this limitation is real, and it is why the carrying value is read back from the
		SLE rather than trusted from the ledger.
		"""
		batch = self._stocked_batch(qty=10)
		se = self._transfer(batch, 6, self.wip_warehouse)

		for event in self._events(se.name):
			self.assertAlmostEqual(
				flt(event.cg_carrying_value_delta),
				0.0,
				places=2,
				msg=f"{event.cg_event_kind} invented a value change for an internal move",
			)

	def test_company_owned_metal_writes_nothing(self):
		"""The blast radius: an ordinary company transfer must be completely unaffected."""
		se = self._receipt(qty=50)
		self._submit(se)
		batch = se.items[0].batch_no
		frappe.db.set_value(
			"Batch", batch, {"custom_inventory_type": None, "custom_customer": None}
		)

		transfer = self._transfer(batch, 10, self.wip_warehouse)

		self.assertEqual(
			self._events(transfer.name), [], "a company transfer wrote a custody event"
		)

	def test_cancelling_the_transfer_reverses_both_events(self):
		batch = self._stocked_batch(qty=10)
		se = self._transfer(batch, 6, self.wip_warehouse)
		self.assertEqual(len(self._events(se.name)), 2)

		se.reload()
		se.cancel()

		reversals = [e for e in self._events(se.name) if e.cg_event_kind == "Reversal"]
		self.assertEqual(
			len(reversals), 2, "both halves of the transfer must be reversed, not one"
		)


class TestConversionEvents(TestComponentApportionment):
	"""Spec §5 — Conversion Out / Conversion In, the movement that does not conserve grams.

	Reuses the real Repack fixture from `TestComponentApportionment`: a lane-tagged Stock Entry
	with one consumed row and two produced rows. That is the production path — the same
	`custom_conversion_lane` tag Metal Conversions stamps at `metal_conversions.py:530`.

	The point of the class is that gross quantity is ALLOWED to disagree across a conversion while
	the holding does not move. Both facts have to be true at once, and only a kind-filtered
	position can hold them.
	"""

	def _events(self, voucher_no):
		"""Local copy — this class extends the apportionment fixture, not the ledger one."""
		return frappe.get_all(
			"Customer Gold Ledger Entry",
			filters={"reference_docname": voucher_no},
			fields=[
				"name",
				"cg_event_kind",
				"cg_stage",
				"cg_gross_qty_delta",
				"cg_fine_gold_delta",
				"cg_carrying_value_delta",
			],
		)

	def test_a_lane_writes_out_and_in_events(self):
		source = self._stocked_batch(qty=100)
		se = self._repack_two_outputs(source, consumed=10.0, split=(6.0, 4.0))

		kinds = [e.cg_event_kind for e in self._events(se.name)]

		self.assertEqual(kinds.count("Conversion Out"), 1, f"got {kinds}")
		self.assertEqual(
			kinds.count("Conversion In"), 2, f"one event per produced row; got {kinds}"
		)

	def test_the_signs_follow_the_warehouse_not_the_row_order(self):
		"""Consume rows carry s_warehouse and go negative; produce rows carry t_warehouse."""
		source = self._stocked_batch(qty=100)
		se = self._repack_two_outputs(source, consumed=10.0, split=(6.0, 4.0))

		events = self._events(se.name)
		out = [e for e in events if e.cg_event_kind == "Conversion Out"]
		into = [e for e in events if e.cg_event_kind == "Conversion In"]

		self.assertAlmostEqual(flt(out[0].cg_gross_qty_delta), -10.0, places=3)
		self.assertAlmostEqual(
			sum(flt(e.cg_gross_qty_delta) for e in into), 10.0, places=3
		)

	def test_a_conversion_does_not_move_the_holding(self):
		"""THE assertion. Same metal, restated — the customer has neither more nor less."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		source = self._stocked_batch(qty=100)
		before = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)

		self._repack_two_outputs(source, consumed=10.0, split=(6.0, 4.0))

		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item),
			before,
			places=3,
			msg="a conversion changed the holding — the position is counting a transformation",
		)

	def test_fine_gold_nets_to_zero_for_a_same_purity_repack(self):
		"""Conservation, on the basis that actually conserves it.

		This fixture repacks one item into itself, so gross happens to balance too. The honest
		general statement is narrower: fine gold nets only while the added alloy carries no gold.
		A gold-bearing alloy would legitimately add fine gold to the lane, and `Batch Component`
		— not this ledger — is the authority on whose it is.
		"""
		source = self._stocked_batch(qty=100)
		se = self._repack_two_outputs(source, consumed=10.0, split=(6.0, 4.0))

		fine = sum(flt(e.cg_fine_gold_delta) for e in self._events(se.name))

		self.assertAlmostEqual(
			fine, 0.0, places=3, msg="fine gold was not conserved across the conversion"
		)

	def test_no_carrying_value_moves_through_a_conversion(self):
		"""The SOP is explicit: liability stays put through a conversion."""
		source = self._stocked_batch(qty=100)
		se = self._repack_two_outputs(source, consumed=10.0, split=(6.0, 4.0))

		for event in self._events(se.name):
			self.assertAlmostEqual(flt(event.cg_carrying_value_delta), 0.0, places=2)

	def test_the_conversion_stays_in_rm(self):
		"""Metal leaves RM as one item and re-enters RM as another. Not transit, not the floor."""
		source = self._stocked_batch(qty=100)
		se = self._repack_two_outputs(source, consumed=10.0, split=(6.0, 4.0))

		for event in self._events(se.name):
			self.assertEqual(event.cg_stage, "RM")

	def test_cancelling_reverses_every_conversion_event(self):
		"""Three events out, three reversals back — the keying defect Transfer exposed."""
		source = self._stocked_batch(qty=100)
		se = self._repack_two_outputs(source, consumed=10.0, split=(6.0, 4.0))
		self.assertEqual(len(self._events(se.name)), 3)

		se.reload()
		se.cancel()

		reversals = [e for e in self._events(se.name) if e.cg_event_kind == "Reversal"]
		self.assertEqual(
			len(reversals), 3, f"expected 3 reversals, got {len(reversals)}"
		)


class TestProductionEvents(TestCustodyTransfer):
	"""Spec §5 — `Production`: customer metal embodied in finished goods.

	Driven by the CONSUMED rows, not the finished-goods row. That is not a shortcut — the FG row
	is hardcoded `"inventory_type": "Regular Stock"` with no customer
	(`manufacturing_operation.py:1118`), so it cannot answer who owns the metal. The consumed rows
	immediately above it carry real ownership and are, on the merits, the better source: what was
	actually consumed IS the composition the spec asks for.
	"""

	def _manufacture(self, batch, qty, fg_item=None, consume_item=None):
		"""A real Manufacture Stock Entry consuming customer metal.

		``consume_item`` defaults to ``self.item`` so every existing caller is unchanged. It
		exists because a batch that has been through a conversion holds the OTHER item, and
		erpnext rejects a row whose batch does not belong to its item code.
		"""
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = MANUFACTURE_SE_TYPE
		se.purpose = "Manufacture"
		se.company = COMPANY
		se.manufacturer = MANUFACTURER
		se.posting_date = self.posting_date
		se.set_posting_time = 1
		# NO se._customer. create_manufacturing_entry does not set one, and setting it here
		# made this fixture kinder than production: create_child_batches falls back to the
		# header when a row carries no customer, so a header would have masked exactly the
		# defect these tests exist to catch.
		consumed = [
			{
				"item_code": consume_item or self.item,
				"qty": qty,
				"s_warehouse": self.warehouse,
				"batch_no": batch,
				"use_serial_batch_fields": 1,
				"inventory_type": "Customer Goods",
				"customer": CUSTOMER,
				"allow_zero_valuation_rate": 1,
				"expense_account": self.difference_account,
			}
		]
		for row in consumed:
			se.append("items", row)

		# Exactly what create_manufacturing_entry now does: the finished row takes its
		# ownership from the metal consumed, rather than a hardcoded "Regular Stock".
		# Calling the real helper keeps this fixture honest -- if production's rule changes,
		# this changes with it instead of quietly drifting into testing a fiction.
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			_finished_goods_ownership,
		)

		fg_inventory_type, fg_customer = _finished_goods_ownership(consumed)
		se.append(
			"items",
			{
				"item_code": fg_item or self.item,
				"qty": qty,
				"t_warehouse": self.warehouse,
				"uom": "Gram",
				"stock_uom": "Gram",
				"conversion_factor": 1,
				"is_finished_item": 1,
				"inventory_type": fg_inventory_type,
				"customer": fg_customer,
				"allow_zero_valuation_rate": 1,
				"expense_account": self.difference_account,
			},
		)
		se.flags.ignore_mandatory = True
		se.save()
		se.submit()
		return se

	def test_the_consumed_row_writes_a_production_event(self):
		batch = self._stocked_batch(qty=20)

		se = self._manufacture(batch, 6)
		events = [e for e in self._events(se.name) if e.cg_event_kind == "Production"]

		self.assertEqual(
			len(events), 1, "expected one Production event per consumed row"
		)
		self.assertAlmostEqual(flt(events[0].cg_gross_qty_delta), 6.0, places=3)
		self.assertEqual(events[0].cg_stage, "FG")

	def test_the_finished_goods_row_writes_nothing(self):
		"""It is hardcoded Regular Stock — reading ownership off it would be wrong."""
		batch = self._stocked_batch(qty=20)

		se = self._manufacture(batch, 6)
		production = [
			e for e in self._events(se.name) if e.cg_event_kind == "Production"
		]

		self.assertEqual(
			len(production),
			1,
			"the FG row produced an event; it cannot know whose metal it holds",
		)

	def test_production_does_not_change_the_holding(self):
		"""The metal was already ours to account for before it was made into something."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch(qty=20)
		before = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)

		self._manufacture(batch, 6)

		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item),
			before,
			places=3,
		)

	def test_the_composition_version_is_recorded(self):
		"""`cg_composition_version` was declared and written by nothing."""
		batch = self._stocked_batch(qty=20)
		se = self._manufacture(batch, 6)

		event = frappe.get_doc(
			"Customer Gold Ledger Entry",
			[e for e in self._events(se.name) if e.cg_event_kind == "Production"][
				0
			].name,
		)
		# No Serial Number Creator in this fixture, so the field is empty — but the WRITE path
		# exists, which is what distinguishes it from the state before this step.
		self.assertIn("cg_composition_version", event.as_dict())

	def test_customer_metal_in_fg_is_answerable(self):
		"""§5.5 asks for 'customer material embodied in WIP/FG' as its own figure."""
		batch = self._stocked_batch(qty=20)
		self._manufacture(batch, 6)
		self._manufacture(batch, 4)

		in_fg = sum(
			flt(r.cg_gross_qty_delta)
			for r in frappe.get_all(
				"Customer Gold Ledger Entry",
				filters={
					"company": COMPANY,
					"customer": CUSTOMER,
					"cg_event_kind": "Production",
					"cg_stage": "FG",
				},
				fields=["cg_gross_qty_delta"],
			)
		)
		self.assertGreaterEqual(in_fg, 10.0)

	def test_an_ordinary_company_manufacture_writes_nothing(self):
		se = self._receipt(qty=20)
		self._submit(se)
		batch = se.items[0].batch_no
		frappe.db.set_value(
			"Batch", batch, {"custom_inventory_type": None, "custom_customer": None}
		)

		manufacture = self._manufacture(batch, 5)

		self.assertEqual(self._events(manufacture.name), [])

	def test_the_finished_goods_batch_carries_its_customer(self):
		"""The defect this class was built to settle -- now the other way round.

		WHAT THIS USED TO ASSERT, AND WHY THAT WAS THE WHOLE BUG
		--------------------------------------------------------
		Until this fix, ``create_manufacturing_entry`` hardcoded the finished row
		``"inventory_type": "Regular Stock"`` with no customer, so a piece made entirely from one
		customer's metal was minted as company stock. Measured on 2026-09-15::

		    batch id              CG-TEST-CUSTOMER-A-...-01-A
		    custom_customer       None
		    custom_inventory_type Regular Stock

		The batch was NAMED after the customer -- ``create_child_batches`` takes the name from the
		row -- which is exactly why it went unnoticed: the id looked right while the ownership
		fields said company stock. ``_batch_owner`` reads the FIELDS, so delivering that finished
		piece wrote no custody event and released no liability, and SOP Examples C and D could
		never close for anything manufactured.

		This test recorded that as fact. It now asserts the opposite, which is the point of the
		change: the finished piece belongs to whoever owned the metal that went into it.
		"""
		batch = self._stocked_batch(qty=20)
		se = self._manufacture(batch, 6)
		fg_row = [r for r in se.items if r.get("is_finished_item")][0]

		self.assertEqual(
			fg_row.inventory_type,
			"Customer Goods",
			msg="the finished row was booked as company stock",
		)
		self.assertEqual(fg_row.customer, CUSTOMER)

		self.assertTrue(
			fg_row.batch_no,
			msg="no batch was minted for the finished row, so ownership cannot be asserted "
			"-- this used to be a skipTest, which let the whole check pass silently",
		)

		owner = frappe.db.get_value(
			"Batch",
			fg_row.batch_no,
			["custom_customer", "custom_inventory_type"],
			as_dict=True,
		)
		self.assertEqual(owner.custom_customer, CUSTOMER)
		self.assertEqual(owner.custom_inventory_type, "Customer Goods")

		# And the consequence that actually matters: the ledger can now find an owner for it.
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		self.assertEqual(cgf._batch_owner(fg_row.batch_no), CUSTOMER)

	def test_a_mixed_owner_manufacture_stays_company_stock_and_says_so(self):
		"""Two customers' metal in one job is a question, not a calculation.

		Apportioning ONE finished piece across two owners -- whose grams does a delivery
		discharge, and in what ratio -- is a business rule nobody has specified. Guessing it
		would put a number in a liability account that no one computed, and liability entries
		are not cheap to unpick. So a mixed job stays company-owned, which is recoverable, and
		logs loudly rather than failing silently.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			_finished_goods_ownership,
		)

		before = frappe.db.count(
			"Error Log",
			{"method": "Customer Gold: mixed-owner manufacture left as company stock"},
		)

		mixed = [
			{"inventory_type": "Customer Goods", "customer": CUSTOMER},
			{"inventory_type": "Customer Goods", "customer": OTHER_CUSTOMER},
		]
		self.assertEqual(_finished_goods_ownership(mixed), ("Regular Stock", None))

		self.assertGreater(
			frappe.db.count(
				"Error Log",
				{
					"method": "Customer Gold: mixed-owner manufacture left as company stock"
				},
			),
			before,
			msg="a mixed-owner job was silently downgraded to company stock",
		)

		# The controls, so the rule above is not passing for an unrelated reason.
		self.assertEqual(
			_finished_goods_ownership(
				[{"inventory_type": "Customer Goods", "customer": CUSTOMER}] * 2
			),
			("Customer Goods", CUSTOMER),
			msg="two rows of the SAME customer is one owner, not a mixed job",
		)
		self.assertEqual(
			_finished_goods_ownership(
				[{"inventory_type": "Regular Stock", "customer": None}]
			),
			("Regular Stock", None),
		)
		self.assertEqual(_finished_goods_ownership([]), ("Regular Stock", None))


class TestManufacturedPieceSettles(TestProductionEvents):
	"""SOP steps 6-8, end to end -- the leg the whole flow exists for.

	    "Track through manufacturing -> Calculate final ownership split -> Sell and settle.
	     On Delivery Note submission, clear only the booked customer value included in the
	     delivered Serial Number."

	THIS COULD NOT PASS BEFORE, AND NOTHING TESTED IT
	--------------------------------------------------
	Every settlement proof in this suite delivered the RAW batch the customer handed over --
	``_stocked_batch`` straight into ``_delivery``. The moment metal was manufactured,
	``create_manufacturing_entry`` booked the finished row as ``Regular Stock`` with no customer,
	``_batch_owner`` returned ``None``, ``record_fulfilment`` skipped the row, and
	``settle_customer_gold_liability`` received an empty list and returned.

	So the liability raised at receipt was **permanent for the only business case the SOP
	describes**: the gold becomes jewellery, ships, is invoiced, and Customer Gold Liability
	never moves. The gap was measured and recorded rather than fixed, and this class is what
	proves it is now closed.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.customer_gold_valuation_policy = "Nominal"
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype=SETTINGS_DOCTYPE)

	def _settlement_entries(self, voucher):
		return sorted(
			{
				r.cg_settlement_voucher
				for r in frappe.get_all(
					"Customer Gold Ledger Entry",
					filters={
						"reference_docname": voucher,
						"cg_settlement_voucher": ["!=", ""],
					},
					fields=["cg_settlement_voucher"],
				)
				if r.cg_settlement_voucher
			}
		)

	def test_the_policy_is_actually_nominal_for_this_class(self):
		"""Without this, every settlement assertion below could pass by being skipped."""
		from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
			get_customer_gold_valuation_policy,
		)

		self.assertEqual(get_customer_gold_valuation_policy(), "Nominal")

	def test_delivering_a_manufactured_piece_writes_a_custody_event(self):
		"""Step 8, first half. Previously zero events: the FG batch had no owner."""
		batch = self._stocked_batch(qty=20)
		se = self._manufacture(batch, 6)
		fg_batch = [r for r in se.items if r.get("is_finished_item")][0].batch_no
		self.assertTrue(fg_batch, msg="no FG batch to deliver")

		dn = self._delivery(fg_batch, qty=6)
		dn.save()
		dn.submit()

		events = [e for e in self._events(dn.name) if e.cg_event_kind == "Delivery"]
		self.assertEqual(
			len(events),
			1,
			msg="delivering a piece made of customer gold wrote no custody event -- the "
			"finished batch is not owned by the customer",
		)
		self.assertEqual(events[0].customer, CUSTOMER)

	def test_a_manufactured_delivery_settles_only_the_booked_customer_value(self):
		"""SOP Example C, and the number the SOP is written around.

		    "On Delivery Note, clear only the booked customer value included in the delivered
		     Serial Number."

		The finished piece here is made from **6.000 g** of customer 24KT booked at
		Rs.7,164.83/g, so the settlement must be::

		    6.000 x 7,164.83 = Rs.42,988.98

		which is the SOP's own S1 figure. It is deliberately NOT the finished item's stock
		value: that also holds company alloy and production cost, which the invoice recovers,
		and settling it would discharge more obligation than was ever raised.

		Two separate defects had to fall for this to be assertable. The finished batch carried
		no owner, so no custody event was written and there was nothing to settle from; and once
		ownership was fixed, the amount still came from the delivery's own
		``stock_value_difference`` -- the whole FG value. ``_booked_customer_value`` makes it the
		customer's share, read from ``Batch Component`` and rated at the ORIGINAL receipt's
		booked rate rather than anything fetched today.
		"""
		batch = self._stocked_batch(qty=20)
		se = self._manufacture(batch, 6)
		fg_batch = [r for r in se.items if r.get("is_finished_item")][0].batch_no

		dn = self._delivery(fg_batch, qty=6)
		dn.save()
		dn.submit()

		events = frappe.get_all(
			"Customer Gold Ledger Entry",
			filters={"reference_docname": dn.name, "cg_event_kind": "Delivery"},
			fields=["customer", "cg_carrying_value_delta"],
		)
		self.assertEqual(len(events), 1, msg="the delivery wrote no custody event")
		self.assertEqual(events[0].customer, CUSTOMER)
		self.assertAlmostEqual(
			flt(events[0].cg_carrying_value_delta),
			-42988.98,
			places=2,
			msg="the event did not carry the booked customer value 6 x 7,164.83",
		)

		entries = self._settlement_entries(dn.name)
		self.assertEqual(
			len(entries), 1, msg=f"expected one settlement JE, got {entries}"
		)

		je = frappe.get_doc("Journal Entry", entries[0])
		self.assertEqual(je.docstatus, 1)
		debits = {r.account: flt(r.debit_in_account_currency) for r in je.accounts}
		credits = {r.account: flt(r.credit_in_account_currency) for r in je.accounts}
		self.assertAlmostEqual(
			debits.get(self.liability_account, 0.0),
			42988.98,
			places=2,
			msg="Dr Customer Gold Liability was not the booked customer value",
		)
		self.assertAlmostEqual(
			credits.get(self.cogs_account, 0.0),
			42988.98,
			places=2,
			msg="Cr Customer Gold COGS Adjustment was not the booked customer value",
		)

	def test_the_settled_value_is_not_the_finished_items_stock_value(self):
		"""The distinction the SOP spends a paragraph on, asserted directly.

		S1 settles Rs.42,988.98 against an FG stock value of Rs.43,186.98 -- the Rs.198.00
		difference being company alloy and production cost. A settlement that tracked the stock
		ledger would over-discharge by exactly that, and on this fixture the two numbers differ
		by the whole amount, because the fixture's finished batch is zero-valued.
		"""
		batch = self._stocked_batch(qty=20)
		se = self._manufacture(batch, 6)
		fg_batch = [r for r in se.items if r.get("is_finished_item")][0].batch_no

		dn = self._delivery(fg_batch, qty=6)
		dn.save()
		dn.submit()

		event_value = flt(
			frappe.db.get_value(
				"Customer Gold Ledger Entry",
				{"reference_docname": dn.name, "cg_event_kind": "Delivery"},
				"cg_carrying_value_delta",
			)
		)
		sle_value = flt(
			frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_no": dn.name, "is_cancelled": 0},
				"stock_value_difference",
			)
		)

		self.assertAlmostEqual(event_value, -42988.98, places=2)
		self.assertNotAlmostEqual(
			event_value,
			sle_value,
			places=2,
			msg="the settlement is tracking the stock ledger rather than the booked customer "
			"value -- on a piece with company alloy in it that over-discharges the liability",
		)

	def test_the_manufactured_delivery_does_not_settle_twice(self):
		"""SOP section 8: "Do not create duplicate revaluation or settlement entries"."""
		batch = self._stocked_batch(qty=20)
		se = self._manufacture(batch, 6)
		fg_batch = [r for r in se.items if r.get("is_finished_item")][0].batch_no

		dn = self._delivery(fg_batch, qty=6)
		dn.save()
		dn.submit()
		first = self._settlement_entries(dn.name)

		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		cgf.record_fulfilment(frappe.get_doc("Delivery Note", dn.name))

		self.assertEqual(
			self._settlement_entries(dn.name),
			first,
			msg="replaying the hook produced a second settlement",
		)


class TestConvertedPieceSettlesTheSourceValue(TestManufacturedPieceSettles):
	"""The same settlement, with a purity change in the middle. It over-discharged by 32%.

	``TestManufacturedPieceSettles`` proves the booked value is settled, but every fixture in
	it manufactures ``self.item`` out of ``self.item``. Real work does not: the customer hands
	over 24KT and the piece that ships is 18KT, because alloy went in. That is SOP Example B
	followed by Examples C and the settlement -- the ordinary case, and it appeared in no test.

	FOUND BY RUNNING THE SOP FOR REAL, NOT BY READING THE CODE
	----------------------------------------------------------
	On cg-integration.test: receive 10 g of 99.9%, convert 6 g of it to 7.95 g of 75.4%,
	manufacture, deliver. Expected Rs.42,988.98. Posted::

	    DN-26-00001  Delivery  -7.9500 g   value -56,960.40   ACC-JV-2026-00001
	    Dr CG Test Customer Gold Liability   56,960.40
	    Cr CG Test Customer Gold COGS Adj                56,960.40

	Rs.13,971.42 more liability discharged than the customer ever posted -- 32.5% over -- and
	the excess credited to COGS Adjustment, where it reads as margin.

	``resolve_components`` apportions to the quantity DRAWN, so the component said 7.950 g and
	named the 24KT receipt batch as its source. ``get_booked_rate`` returned that batch's
	Rs.7,164.83 per 24KT gram. Multiplying them multiplies 18KT grams by a 24KT rate.

	The three assertions below are the three places the wrong number surfaced -- the custody
	event, the debit, and the credit. All three are asserted because a fix that corrected the
	event while leaving the JE alone would be worse than the defect: the ledger and the GL would
	then disagree about the same delivery.
	"""

	#: The fixtures' own purities, so every figure below can be rechecked against the masters.
	SOURCE_PURITY = 99.9
	CONVERTED_PURITY = 75.4
	BOOKED_RATE = 7164.83

	SOURCE_QTY = 6.0
	#: 6.000 g of 99.9% carries 5.994 g of fine gold, which at 75.4% is 7.949602... g. Stock
	#: quantities persist at 2dp, so what the system can actually hold is 7.95.
	CONVERTED_QTY = 7.95

	#: What this fixture must settle: 7.95 x 75.4 / 99.9 x 7,164.83, recomputed here from the
	#: constants above rather than read back from the code under test.
	EXPECTED_VALUE = 42991.13
	#: The SOP's S1 figure for 6.000 g. EXPECTED_VALUE sits Rs.2.15 above it, and that gap is
	#: entirely the 7.949602 -> 7.95 quantity rounding: 0.0004 g of 24KT at Rs.7,164.83. It is
	#: asserted as a tolerance below, so a real regression cannot hide inside it.
	SOP_VALUE = 42988.98
	#: What the defect posted: 7.950 x 7,164.83, the 18KT gram count at the 24KT rate.
	OVERSTATED_VALUE = 56960.40

	def _convert(self, source_batch):
		"""A real lane-tagged conversion from the 99.9% item to the 75.4% one."""
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = REPACK_SE_TYPE
		se.purpose = "Repack"
		se.company = COMPANY
		se.posting_date = self.posting_date
		se.set_posting_time = 1
		se._customer = CUSTOMER
		se.append(
			"items",
			{
				"item_code": self.item,
				"qty": self.SOURCE_QTY,
				"s_warehouse": self.warehouse,
				"batch_no": source_batch,
				"use_serial_batch_fields": 1,
				"uom": "Gram",
				"stock_uom": "Gram",
				"conversion_factor": 1,
				"inventory_type": "Customer Goods",
				"customer": CUSTOMER,
				"custom_conversion_lane": f"Customer Goods|{CUSTOMER}",
				"expense_account": self.difference_account,
			},
		)
		se.append(
			"items",
			{
				"item_code": self.operating_item,
				"qty": self.CONVERTED_QTY,
				"t_warehouse": self.warehouse,
				"uom": "Gram",
				"stock_uom": "Gram",
				"conversion_factor": 1,
				"inventory_type": "Customer Goods",
				"customer": CUSTOMER,
				"custom_conversion_lane": f"Customer Goods|{CUSTOMER}",
				"expense_account": self.difference_account,
			},
		)
		se.flags.ignore_mandatory = True
		se.save()
		se.submit()
		return se.items[1].batch_no

	def _deliver_operating_item(self, batch_no, qty):
		"""``_delivery`` hardcodes ``self.item``; the converted piece is the other one."""
		if not frappe.db.exists("Sales Type", SALES_TYPE):
			frappe.get_doc(
				{"doctype": "Sales Type", "type": SALES_TYPE, "tax_rate": 0}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer Payment Terms", {"customer": CUSTOMER}):
			frappe.get_doc(
				{"doctype": "Customer Payment Terms", "customer": CUSTOMER}
			).insert(ignore_permissions=True)

		so = frappe.new_doc("Sales Order")
		so.company = COMPANY
		so.customer = CUSTOMER
		so.sales_type = SALES_TYPE
		so.transaction_date = self.posting_date
		so.delivery_date = self.posting_date
		so.append(
			"items",
			{
				"item_code": self.operating_item,
				"qty": qty,
				"rate": 0,
				"delivery_date": self.posting_date,
				"warehouse": self.warehouse,
				"uom": "Gram",
				"stock_uom": "Gram",
				"conversion_factor": 1,
			},
		)
		so.flags.ignore_mandatory = True
		so.save()
		so.submit()

		dn = frappe.new_doc("Delivery Note")
		dn.company = COMPANY
		dn.customer = CUSTOMER
		dn.posting_date = self.posting_date
		dn.set_posting_time = 1
		dn.append(
			"items",
			{
				"item_code": self.operating_item,
				"qty": qty,
				"rate": 0,
				"warehouse": self.warehouse,
				"batch_no": batch_no,
				"use_serial_batch_fields": 1,
				"uom": "Gram",
				"stock_uom": "Gram",
				"conversion_factor": 1,
				"against_sales_order": so.name,
				"so_detail": so.items[0].name,
			},
		)
		dn.flags.ignore_mandatory = True
		dn.save()
		dn.submit()
		return dn

	def _receipt_convert_manufacture_deliver(self):
		"""The whole SOP leg, with real documents at every step."""
		batch = self._stocked_batch(qty=10)
		converted = self._convert(batch)
		se = self._manufacture(
			converted,
			self.CONVERTED_QTY,
			fg_item=self.operating_item,
			consume_item=self.operating_item,
		)
		fg_batch = [r for r in se.items if r.get("is_finished_item")][0].batch_no
		return self._deliver_operating_item(fg_batch, self.CONVERTED_QTY)

	def test_the_component_is_restated_into_the_source_items_grams(self):
		"""The unit conversion on its own, so a failure says which half broke."""
		from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
			_restate_qty,
		)

		self.assertAlmostEqual(
			_restate_qty(self.CONVERTED_QTY, self.operating_item, self.item),
			self.CONVERTED_QTY * self.CONVERTED_PURITY / self.SOURCE_PURITY,
			places=6,
			msg="7.95 g of 75.4% is 6.0003 g of 99.9% -- same 5.9943 g of fine gold",
		)
		self.assertAlmostEqual(
			_restate_qty(self.CONVERTED_QTY, self.operating_item, self.item),
			self.SOURCE_QTY,
			places=3,
			msg="and that is the 6.000 g drawn, to the precision quantities are stored at",
		)

	def test_an_identical_item_restates_to_itself(self):
		"""The no-op path every same-item fixture in this suite depends on."""
		from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
			_restate_qty,
		)

		self.assertEqual(_restate_qty(6.0, self.item, self.item), 6.0)

	def test_an_unrestatable_component_refuses_rather_than_guessing(self):
		"""No source item means no defensible rate, so the caller must fall back."""
		from jewellery_erpnext.customer_subcontracting.customer_gold_fulfilment import (
			_restate_qty,
		)

		self.assertIsNone(_restate_qty(6.0, self.operating_item, None))
		self.assertIsNone(_restate_qty(6.0, None, self.item))

	def test_the_custody_event_settles_the_source_value_not_the_converted_grams(self):
		"""The defect, at the ledger. Rs.42,988.98, never Rs.56,960.40."""
		dn = self._receipt_convert_manufacture_deliver()

		events = frappe.get_all(
			"Customer Gold Ledger Entry",
			filters={"reference_docname": dn.name, "cg_event_kind": "Delivery"},
			fields=["customer", "cg_carrying_value_delta"],
		)
		self.assertEqual(len(events), 1, msg="the delivery wrote no custody event")
		self.assertEqual(events[0].customer, CUSTOMER)

		value = flt(events[0].cg_carrying_value_delta)
		self.assertNotAlmostEqual(
			value,
			-self.OVERSTATED_VALUE,
			places=2,
			msg="settled the 18KT gram count at the 24KT rate -- the original defect",
		)
		self.assertAlmostEqual(
			value,
			-self.EXPECTED_VALUE,
			places=2,
			msg="not 7.95 x 75.4 / 99.9 x 7,164.83",
		)
		self.assertAlmostEqual(
			value,
			-self.SOP_VALUE,
			delta=3.0,
			msg="a purity change must not change what the customer posted; only the "
			"2dp quantity rounding may move it, and that is worth Rs.2.15",
		)

	def test_the_journal_entry_agrees_with_the_custody_event(self):
		"""Both legs, because a ledger that disagrees with the GL is worse than either."""
		dn = self._receipt_convert_manufacture_deliver()

		entries = self._settlement_entries(dn.name)
		self.assertEqual(
			len(entries), 1, msg=f"expected one settlement JE, got {entries}"
		)

		je = frappe.get_doc("Journal Entry", entries[0])
		self.assertEqual(je.docstatus, 1)
		debits = {r.account: flt(r.debit_in_account_currency) for r in je.accounts}
		credits = {r.account: flt(r.credit_in_account_currency) for r in je.accounts}

		self.assertAlmostEqual(
			debits.get(self.liability_account, 0.0),
			self.EXPECTED_VALUE,
			places=2,
			msg="Dr Customer Gold Liability over-discharged the obligation",
		)
		self.assertAlmostEqual(
			credits.get(self.cogs_account, 0.0),
			self.EXPECTED_VALUE,
			places=2,
			msg="Cr COGS Adjustment credited margin that was never earned",
		)

	def test_the_fine_gold_is_conserved_across_the_whole_leg(self):
		"""The independent check: whatever the rupees do, the metal must balance.

		5.994 g of fine gold went into the conversion and 5.994 g shipped, so the customer's
		fine position returns to what it was before the 6 g was drawn -- the 4 g still in raw
		custody, and nothing else.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch(qty=10)
		after_receipt = cgf.get_customer_gold_fine_position(COMPANY, CUSTOMER)

		converted = self._convert(batch)
		se = self._manufacture(
			converted,
			self.CONVERTED_QTY,
			fg_item=self.operating_item,
			consume_item=self.operating_item,
		)
		fg_batch = [r for r in se.items if r.get("is_finished_item")][0].batch_no
		self._deliver_operating_item(fg_batch, self.CONVERTED_QTY)

		self.assertAlmostEqual(
			cgf.get_customer_gold_fine_position(COMPANY, CUSTOMER),
			after_receipt - self.CONVERTED_QTY * self.CONVERTED_PURITY / 100.0,
			places=3,
			msg="the fine gold delivered is not the fine gold that was converted",
		)


class TestLossAndRecovery(TestCustodyTransfer):
	"""Spec §5.5 — physical loss is NOT a liability reduction.

	    "Physical loss and monetary obligation are not automatically identical. If customer
	     material is lost and the business still owes replacement, the obligation remains even
	     though the physical stock is lower. Do not force reconciliation by silently reducing
	     liability for every physical loss."

	That sentence is the whole class. It is an easy rule to break by accident, because making the
	two books agree is the instinctive thing to do — and doing it here would quietly write off a
	customer's gold because it was dropped on the shop floor.
	"""

	def _process_loss(self, batch, lost, recovered, owned=True):
		"""A Process Loss Repack: metal booked as lost, scrap recovered from it.

		``owned=False`` builds the company control. Both the batch AND the rows have to be
		company-owned for that: leaving ``customer`` on the rows makes ``create_child_batches``
		mint a fresh CUSTOMER batch for the produce row, which then legitimately writes a
		Recovery event. The first version of this fixture did exactly that and the control
		caught it.
		"""
		ownership = (
			{"inventory_type": "Customer Goods", "customer": CUSTOMER}
			if owned
			else {"inventory_type": "Regular Stock"}
		)
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = LOSS_SE_TYPE
		se.purpose = "Repack"
		se.company = COMPANY
		se.manufacturer = MANUFACTURER
		se.posting_date = self.posting_date
		se.set_posting_time = 1
		if owned:
			se._customer = CUSTOMER
		se.append(
			"items",
			{
				"item_code": self.item,
				"qty": lost,
				"s_warehouse": self.warehouse,
				"batch_no": batch,
				"use_serial_batch_fields": 1,
				**ownership,
				"allow_zero_valuation_rate": 1,
				"expense_account": self.difference_account,
			},
		)
		se.append(
			"items",
			{
				"item_code": self.item,
				"qty": recovered,
				"t_warehouse": self.warehouse,
				"uom": "Gram",
				"stock_uom": "Gram",
				"conversion_factor": 1,
				"is_finished_item": 1,
				**ownership,
				"allow_zero_valuation_rate": 1,
				"expense_account": self.difference_account,
			},
		)
		se.flags.ignore_mandatory = True
		se.save()
		se.submit()
		return se

	def test_loss_and_recovery_are_both_written(self):
		batch = self._stocked_batch(qty=20)

		se = self._process_loss(batch, lost=2.0, recovered=0.5)
		kinds = {e.cg_event_kind: e for e in self._events(se.name)}

		self.assertIn("Approved Loss", kinds)
		self.assertIn("Recovery", kinds)
		self.assertAlmostEqual(
			flt(kinds["Approved Loss"].cg_gross_qty_delta), -2.0, places=3
		)
		self.assertAlmostEqual(flt(kinds["Recovery"].cg_gross_qty_delta), 0.5, places=3)

	def test_the_holding_falls_by_the_net_loss(self):
		"""Metal genuinely left — unlike a transfer or a conversion, this one moves the position."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch(qty=20)
		before = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)

		self._process_loss(batch, lost=2.0, recovered=0.5)

		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item),
			before - 1.5,
			places=3,
			msg="2 g lost and 0.5 g recovered must net to 1.5 g off the holding",
		)

	def test_no_liability_is_written_off_by_a_loss(self):
		"""§5.5. THE test. The customer is still owed the gold that was lost."""
		batch = self._stocked_batch(qty=20)

		se = self._process_loss(batch, lost=2.0, recovered=0.5)

		for event in self._events(se.name):
			self.assertAlmostEqual(
				flt(event.cg_carrying_value_delta),
				0.0,
				places=2,
				msg=(
					f"{event.cg_event_kind} reduced the customer's obligation. Losing metal on "
					f"the shop floor does not cancel what is owed for it — that is a restitution "
					f"decision with an approver, not arithmetic."
				),
			)

	def test_recovered_scrap_lands_in_its_own_stage(self):
		"""`Recoverable Scrap` was declared and written by nothing."""
		batch = self._stocked_batch(qty=20)

		se = self._process_loss(batch, lost=2.0, recovered=0.5)
		kinds = {e.cg_event_kind: e for e in self._events(se.name)}

		self.assertEqual(kinds["Recovery"].cg_stage, "Recoverable Scrap")
		self.assertEqual(kinds["Approved Loss"].cg_stage, "RM")

	def test_company_metal_loss_writes_nothing(self):
		se = self._receipt(qty=20)
		self._submit(se)
		batch = se.items[0].batch_no
		frappe.db.set_value(
			"Batch", batch, {"custom_inventory_type": None, "custom_customer": None}
		)

		loss = self._process_loss(batch, lost=2.0, recovered=0.5, owned=False)

		self.assertEqual(self._events(loss.name), [])


class TestAllocationAndRelease(TestCustodyTransfer):
	"""Spec §5.4's SECOND formula — allocation is its own dimension, not part of the holding.

	    Free eligible = eligible held − active unconsumed reservations − blocked/quarantined
	    "Do not subtract consumed reservations twice."

	Reserving metal promises it to an order. It does not move it, consume it or settle anything,
	so the customer has exactly as much with us afterwards as before. That is why Allocation and
	Release are excluded from `POSITION_KINDS` and feed `get_customer_gold_free_quantity` instead.

	The double-subtraction warning is the reason Release exists as its own kind rather than the
	reservation simply disappearing: a consumed reservation is RELEASED, its Release cancels its
	Allocation, and the movement that consumed it then reduces the holding once — not twice.
	"""

	def _reserve(self, batch, qty):
		"""A real Stock Reservation Entry against a customer batch."""
		sre = frappe.new_doc("Stock Reservation Entry")
		sre.item_code = self.item
		sre.warehouse = self.warehouse
		sre.company = COMPANY
		sre.stock_uom = "Gram"
		sre.voucher_type = "Sales Order"
		sre.reserved_qty = qty
		sre.available_qty = qty
		sre.reservation_based_on = "Serial and Batch"
		sre.append("sb_entries", {"batch_no": batch, "qty": qty})
		sre.flags.ignore_mandatory = True
		sre.flags.ignore_links = True
		sre.flags.ignore_validate = True
		sre.insert(ignore_permissions=True)
		sre.flags.ignore_validate = True
		sre.submit()
		return sre

	def _ledger(self, sre_name):
		return frappe.get_all(
			"Customer Gold Ledger Entry",
			filters={"reference_docname": sre_name},
			fields=[
				"name",
				"cg_event_kind",
				"cg_gross_qty_delta",
				"cg_reservation",
				"cg_stage",
			],
		)

	def test_reserving_writes_an_allocation(self):
		batch = self._stocked_batch(qty=20)

		sre = self._reserve(batch, 6)
		events = self._ledger(sre.name)

		self.assertEqual(len(events), 1)
		self.assertEqual(events[0].cg_event_kind, "Allocation")
		self.assertAlmostEqual(flt(events[0].cg_gross_qty_delta), 6.0, places=3)
		self.assertEqual(
			events[0].cg_reservation,
			sre.name,
			"cg_reservation was declared and never written",
		)

	def test_a_reservation_does_not_change_the_holding(self):
		"""Promising metal is not moving it."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch(qty=20)
		before = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)

		self._reserve(batch, 6)

		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item),
			before,
			places=3,
		)

	def test_free_quantity_is_held_minus_reserved(self):
		"""§5.4's formula, directly."""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		# Measured as CHANGES, not absolutes: the class rolls back once at the end, so earlier
		# cases in it have already left reservations standing. Asserting free == held here would
		# be asserting that no other test ran first.
		batch = self._stocked_batch(qty=20)
		held_before = cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item)
		free_before = cgf.get_customer_gold_free_quantity(COMPANY, CUSTOMER, self.item)

		self._reserve(batch, 6)

		self.assertAlmostEqual(
			cgf.get_customer_gold_free_quantity(COMPANY, CUSTOMER, self.item),
			free_before - 6.0,
			places=3,
			msg="the reservation did not reduce the free quantity",
		)
		self.assertAlmostEqual(
			cgf.get_customer_gold_position(COMPANY, CUSTOMER, self.item),
			held_before,
			places=3,
			msg="the reservation changed the holding, which it must not",
		)

	def test_releasing_gives_the_free_quantity_back(self):
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch(qty=20)
		free_before = cgf.get_customer_gold_free_quantity(COMPANY, CUSTOMER, self.item)

		sre = self._reserve(batch, 6)
		self.assertAlmostEqual(
			cgf.get_customer_gold_free_quantity(COMPANY, CUSTOMER, self.item),
			free_before - 6.0,
			places=3,
		)

		sre.reload()
		sre.flags.ignore_validate = True
		sre.cancel()

		kinds = [e.cg_event_kind for e in self._ledger(sre.name)]
		self.assertIn("Release", kinds)
		self.assertAlmostEqual(
			cgf.get_customer_gold_free_quantity(COMPANY, CUSTOMER, self.item),
			free_before,
			places=3,
			msg="releasing the reservation did not restore the free quantity",
		)

	def test_a_consumed_reservation_is_not_subtracted_twice(self):
		"""The spec's explicit warning, as an assertion.

		Release cancels Allocation, so once a reservation is consumed the pair nets to zero and
		the movement that consumed it reduces the holding exactly once.
		"""
		from jewellery_erpnext.customer_subcontracting import (
			customer_gold_fulfilment as cgf,
		)

		batch = self._stocked_batch(qty=20)
		free_before = cgf.get_customer_gold_free_quantity(COMPANY, CUSTOMER, self.item)

		sre = self._reserve(batch, 6)
		sre.reload()
		sre.flags.ignore_validate = True
		sre.cancel()

		# The metal is then actually delivered.
		dn = self._delivery(batch, qty=6)
		dn.save()
		dn.submit()

		self.assertAlmostEqual(
			cgf.get_customer_gold_free_quantity(COMPANY, CUSTOMER, self.item),
			free_before - 6.0,
			places=3,
			msg="6 g was subtracted twice — once as a reservation and again as a delivery",
		)

	def test_a_company_batch_reservation_writes_nothing(self):
		se = self._receipt(qty=20)
		self._submit(se)
		batch = se.items[0].batch_no
		frappe.db.set_value(
			"Batch", batch, {"custom_inventory_type": None, "custom_customer": None}
		)

		sre = self._reserve(batch, 5)

		self.assertEqual(self._ledger(sre.name), [])
