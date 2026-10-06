# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Customer-batch-only findings for Hybrid sales orders (``customer_subcontracting/hybrid_findings``).

Every KG GK order is GK Export's (GJCU0009); the end customer is the order's Ref Customer. In a
Hybrid order, a finding listed in Subcontracting Settings may draw only a Customer Goods batch of
GJCU0009 received for that Ref Customer. Covers the rule, the Stock Entry validator, the batch
picker, both FIFO allocators, Ref Customer stamping on parent / child / ERPNext-minted batches,
and the SNC exemption.

Pure logic in the suite's idiom: fake docs, every site read stubbed by key (anything else goes to
the real call), nothing written.
"""

from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting import batch_rename
from jewellery_erpnext.customer_subcontracting import hybrid_findings as H
from jewellery_erpnext.customer_subcontracting.sub_utils import snc
from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events import (
	utils as batch_utils,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events import (
	se_utils,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	main_slip_inject as msi,
)
from jewellery_erpnext.jewellery_erpnext.tests import (
	test_create_child_batches_namespace as namespace,
)

GK_EXPORT = "GJCU0009"
REF = "TNCU0085"
OTHER_REF = "MHCU0024"
MT_WO = "Material Transfer (WORK ORDER)"
RESERVE = "Material transfer to Reserve"

HYBRID_PMO = "PMO-HYBRID-1"
OTHER_HYBRID_PMO = "PMO-HYBRID-2"
NO_REF_PMO = "PMO-HYBRID-NOREF"
OUTRIGHT_PMO = "PMO-OUTRIGHT-1"

CHAIN = "F-G-22KT-91.75-Y-CHA-6SC-8.00 INCH"
GOLUSU_BANGLE = "F-G-22KT-91.75-Y-GL-GLB-1.00 INCH"
GOLUSU_OTHER = "F-G-22KT-91.75-Y-GL-GLX-1.00 INCH"
POST = "F-G-22KT-91.75-Y-PO-SOP-1.60*10.00 MM"
METAL = "M-G-22KT-91.75-Y"

# Nakshi / Chains whole; Golusu narrowed to Golusu Bangles -- the four values of the request.
RULES = {"Nakshi": None, "Chains": None, "Golusu": {"Golusu Bangles"}}

ATTRIBUTES = {
	CHAIN: ("Chains", "6 Sadak Chain"),
	GOLUSU_BANGLE: ("Golusu", "Golusu Bangles"),
	GOLUSU_OTHER: ("Golusu", "Golusu Chain"),
	POST: ("Posts", "Screw Post"),
}

PMOS = {
	HYBRID_PMO: frappe._dict(
		name=HYBRID_PMO, sales_type="Hybrid", customer=GK_EXPORT, ref_customer=REF
	),
	OTHER_HYBRID_PMO: frappe._dict(
		name=OTHER_HYBRID_PMO,
		sales_type="Hybrid",
		customer=GK_EXPORT,
		ref_customer=OTHER_REF,
	),
	NO_REF_PMO: frappe._dict(
		name=NO_REF_PMO, sales_type="Hybrid", customer=GK_EXPORT, ref_customer=None
	),
	OUTRIGHT_PMO: frappe._dict(
		name=OUTRIGHT_PMO, sales_type="Outright", customer=GK_EXPORT, ref_customer=REF
	),
}

OWN = "GJCU0009-2F09-F-CHA-17"
OTHER = "GJCU0009-2F09-F-CHA-18"
NO_REF = "GJCU0009-2F09-F-CHA-19"
COMPANY = "KG2F094-FCHA-A1"

BATCHES = {
	OWN: frappe._dict(
		name=OWN,
		custom_inventory_type="Customer Goods",
		custom_customer=GK_EXPORT,
		custom_ref_customer=REF,
	),
	OTHER: frappe._dict(
		name=OTHER,
		custom_inventory_type="Customer Goods",
		custom_customer=GK_EXPORT,
		custom_ref_customer=OTHER_REF,
	),
	NO_REF: frappe._dict(
		name=NO_REF,
		custom_inventory_type="Customer Goods",
		custom_customer=GK_EXPORT,
		custom_ref_customer=None,
	),
	COMPANY: frappe._dict(
		name=COMPANY,
		custom_inventory_type="Regular Stock",
		custom_customer=None,
		custom_ref_customer=None,
	),
}

_REAL_GET_ALL = frappe.get_all


class _Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


class _Row(_Doc):
	def db_set(self, key, value):
		setattr(self, key, value)

	def as_dict(self):
		return dict(self.__dict__)


def _entry(rows=(), stock_entry_type=MT_WO, **fields):
	values = {
		"doctype": "Stock Entry",
		"name": "MAT-STE-TEST",
		"stock_entry_type": stock_entry_type,
		"manufacturing_order": None,
		"manufacturing_work_order": None,
		"custom_request_id": None,
		"items": list(rows),
		"flags": frappe._dict(),
	}
	values.update(fields)
	return _Doc(**values)


def _row(item_code=CHAIN, batch_no=OWN, pmo=HYBRID_PMO, idx=1, **fields):
	values = {
		"idx": idx,
		"item_code": item_code,
		"batch_no": batch_no,
		"s_warehouse": "Waxing RM - KGJPL",
		"t_warehouse": "Waxing WO - KGJPL",
		"custom_parent_manufacturing_order": pmo,
		"custom_manufacturing_work_order": None,
		"serial_and_batch_bundle": None,
		"qty": 1.0,
	}
	values.update(fields)
	return _Row(**values)


def _requirement(**fields):
	values = {
		"pmo": HYBRID_PMO,
		"customer": GK_EXPORT,
		"ref_customer": REF,
		"problem": None,
		"item_code": CHAIN,
		"finding_category": "Chains",
		"finding_sub_category": "6 Sadak Chain",
	}
	values.update(fields)
	return frappe._dict(values)


def _keyed_get_all(pmos, batches):
	"""``frappe.get_all`` answering the PMO and Batch reads; everything else is real."""

	def get_all(doctype, filters=None, fields=None, **kwargs):
		if doctype in ("Parent Manufacturing Order", "Batch") and isinstance(
			filters, dict
		):
			table = pmos if doctype == "Parent Manufacturing Order" else batches
			wanted = filters["name"][1]
			return [table[name] for name in wanted if name in table]
		return _REAL_GET_ALL(doctype, filters=filters, fields=fields, **kwargs)

	return get_all


def _keyed_get_value(answers):
	"""``frappe.db.get_value`` answering ``answers[doctype]``; every other read is real.

	Never a blanket stub: Frappe loads DocType meta through ``get_value`` too.
	"""
	real = frappe.db.get_value

	def get_value(doctype, *args, **kwargs):
		if doctype in answers:
			return answers[doctype](*args, **kwargs)
		return real(doctype, *args, **kwargs)

	return get_value


def _site(stack, rules=RULES, pmos=PMOS, batches=BATCHES, attributes=ATTRIBUTES):
	"""Enter the patches that stand in for Subcontracting Settings, PMOs, Batches and Items."""
	rules_mock = stack.enter_context(
		patch.object(H, "get_hybrid_finding_rules", return_value=rules)
	)
	stack.enter_context(
		patch.object(
			H,
			"get_finding_attribute_map",
			side_effect=lambda codes: {
				c: attributes[c] for c in codes if c in attributes
			},
		)
	)
	stack.enter_context(
		patch.object(H, "batch_has_ref_customer_field", return_value=True)
	)
	stack.enter_context(
		patch("frappe.get_all", side_effect=_keyed_get_all(pmos, batches))
	)
	return rules_mock


class _Case(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# Build the translation cache before any frappe.get_all stub: the first _() reads
		# Translation through get_all and would cache a broken map if the stub caught it.
		frappe._("Customer Batch Required")


class TestRules(_Case):
	def test_a_bare_category_covers_every_sub_category_and_wins_over_a_narrow_row(self):
		settings = frappe._dict(
			hybrid_finding_categories=[
				frappe._dict(finding_category="Nakshi", finding_sub_category=None),
				frappe._dict(
					finding_category="Golusu", finding_sub_category="Golusu Bangles"
				),
				frappe._dict(
					finding_category="Chains", finding_sub_category="6 Sadak Chain"
				),
				frappe._dict(finding_category="Chains", finding_sub_category=None),
			]
		)
		with (
			patch.object(H, "has_settings_capability", return_value=True),
			patch.object(H, "get_customer_gold_settings", return_value=settings),
		):
			rules = H.get_hybrid_finding_rules()
		self.assertEqual(rules, RULES)

	def test_an_unmigrated_site_has_no_rules(self):
		with (
			patch.object(H, "has_settings_capability", return_value=False),
			patch.object(H, "get_customer_gold_settings") as settings,
		):
			self.assertEqual(H.get_hybrid_finding_rules(), {})
		settings.assert_not_called()

	def test_matching(self):
		self.assertTrue(H.matches_rule(RULES, "Chains", "Kodi Chain"))
		self.assertTrue(H.matches_rule(RULES, "Nakshi", None))
		self.assertTrue(H.matches_rule(RULES, "Golusu", "Golusu Bangles"))
		self.assertFalse(H.matches_rule(RULES, "Golusu", "Golusu Chain"))
		self.assertFalse(H.matches_rule(RULES, "Posts", "Screw Post"))
		self.assertFalse(H.matches_rule(RULES, None, None))
		self.assertFalse(H.matches_rule({}, "Chains", None))


class TestHybridRequirement(_Case):
	def _requirement(self, row, entry=None, **site):
		entry = entry or _entry([row])
		with ExitStack() as stack:
			rules = _site(stack, **site)
			return H.hybrid_requirement(entry, row), rules

	def test_a_listed_finding_of_a_hybrid_order_needs_the_ref_customers_batch(self):
		requirement, _rules = self._requirement(_row())
		self.assertEqual(requirement.customer, GK_EXPORT)
		self.assertEqual(requirement.ref_customer, REF)
		self.assertEqual(requirement.pmo, HYBRID_PMO)
		self.assertIsNone(requirement.problem)
		self.assertEqual(requirement.finding_category, "Chains")

	def test_a_sub_category_row_covers_only_that_sub_category(self):
		self.assertIsNotNone(self._requirement(_row(item_code=GOLUSU_BANGLE))[0])
		self.assertIsNone(self._requirement(_row(item_code=GOLUSU_OTHER))[0])

	def test_an_unlisted_finding_is_left_alone(self):
		self.assertIsNone(self._requirement(_row(item_code=POST))[0])

	def test_other_sales_types_are_left_alone(self):
		self.assertIsNone(self._requirement(_row(pmo=OUTRIGHT_PMO))[0])

	def test_an_empty_table_switches_the_rule_off(self):
		self.assertIsNone(self._requirement(_row(), rules={})[0])

	def test_an_out_of_scope_entry_never_reads_the_settings(self):
		row = _row()
		requirement, rules = self._requirement(
			row, entry=_entry([row], stock_entry_type="Material Transfer From Reserve")
		)
		self.assertIsNone(requirement)
		rules.assert_not_called()

	def test_a_non_finding_row_never_reads_the_settings(self):
		requirement, rules = self._requirement(_row(item_code=METAL))
		self.assertIsNone(requirement)
		rules.assert_not_called()

	def test_the_order_falls_back_to_the_header_then_the_work_order(self):
		row = _row(pmo=None)
		on_header = self._requirement(
			row, entry=_entry([row], manufacturing_order=HYBRID_PMO)
		)[0]
		self.assertEqual(on_header.pmo, HYBRID_PMO)

		row = _row(pmo=None)
		entry = _entry([row], manufacturing_work_order="MWO-1")
		with patch(
			"frappe.db.get_value",
			side_effect=_keyed_get_value(
				{"Manufacturing Work Order": lambda *a, **k: HYBRID_PMO}
			),
		):
			on_mwo = self._requirement(row, entry=entry)[0]
		self.assertEqual(on_mwo.ref_customer, REF)

	def test_an_order_without_ref_customer_matches_nothing(self):
		requirement = self._requirement(_row(pmo=NO_REF_PMO))[0]
		self.assertEqual(requirement.problem, "no_ref_customer")
		self.assertIsNone(requirement.ref_customer)
		self.assertFalse(H.batch_is_eligible(BATCHES[OWN], requirement))

	def test_a_row_for_two_orders_of_different_ref_customers_matches_nothing(self):
		requirement = self._requirement(_row(pmo=f"{HYBRID_PMO}, {OTHER_HYBRID_PMO}"))[
			0
		]
		self.assertEqual(requirement.problem, "conflict")
		self.assertFalse(H.batch_is_eligible(BATCHES[OWN], requirement))


class TestBatchEligibility(_Case):
	def test_only_the_ref_customers_customer_goods_batch_is_eligible(self):
		requirement = _requirement()
		self.assertTrue(H.batch_is_eligible(BATCHES[OWN], requirement))
		self.assertFalse(H.batch_is_eligible(BATCHES[OTHER], requirement))
		self.assertFalse(H.batch_is_eligible(BATCHES[NO_REF], requirement))
		self.assertFalse(H.batch_is_eligible(BATCHES[COMPANY], requirement))
		self.assertFalse(H.batch_is_eligible(None, requirement))

	def test_another_owners_batch_is_not_eligible_even_for_the_same_ref_customer(self):
		batch = frappe._dict(BATCHES[OWN], custom_customer="KACU0043")
		self.assertFalse(H.batch_is_eligible(batch, _requirement()))


class TestValidator(_Case):
	def _validate(self, entry):
		with ExitStack() as stack:
			_site(stack)
			stack.enter_context(
				patch(
					"frappe.get_cached_value",
					side_effect=lambda doctype, name, field, *a, **k: 1
					if (doctype, field) == ("Item", "has_batch_no")
					else None,
				)
			)
			H.validate_hybrid_finding_batches(entry)

	def test_the_ref_customers_batch_passes(self):
		self._validate(_entry([_row(batch_no=OWN)]))

	def test_a_company_batch_is_rejected_naming_the_ref_customer(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._validate(_entry([_row(batch_no=COMPANY)]))
		message = str(raised.exception)
		self.assertIn(COMPANY, message)
		self.assertIn(REF, message)

	def test_another_ref_customers_batch_is_rejected(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._validate(_entry([_row(batch_no=OTHER)]))
		self.assertIn(OTHER_REF, str(raised.exception))

	def test_a_row_with_no_batch_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(_entry([_row(batch_no=None)]))

	def test_the_reserve_entry_is_checked_too(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(_entry([_row(batch_no=COMPANY)], stock_entry_type=RESERVE))

	def test_snc_settlement_transfers_are_left_to_snc(self):
		self._validate(
			_entry([_row(batch_no=COMPANY)], custom_request_id="SNC-0123456789")
		)

	def test_the_from_reserve_copy_is_not_checked(self):
		self._validate(
			_entry(
				[_row(batch_no=COMPANY)],
				stock_entry_type="Material Transfer From Reserve",
			)
		)

	def test_other_sales_types_and_unlisted_findings_are_not_checked(self):
		self._validate(
			_entry(
				[
					_row(batch_no=COMPANY, pmo=OUTRIGHT_PMO),
					_row(item_code=POST, batch_no=COMPANY, idx=2),
				]
			)
		)

	def test_inward_rows_are_not_checked(self):
		self._validate(_entry([_row(batch_no=COMPANY, s_warehouse=None)]))


class TestPicker(_Case):
	ERPNEXT_LIST = [(OWN, 5.0), (OTHER, 3.0), (COMPANY, 9.0)]

	def _pick(self, filters):
		with ExitStack() as stack:
			_site(stack)
			erpnext = stack.enter_context(
				patch.object(
					H, "erpnext_get_batch_no", return_value=list(self.ERPNEXT_LIST)
				)
			)
			result = H.get_batch_no("Batch", "", "name", 0, 20, filters)
		return result, erpnext.call_args.args[-1]

	def test_anything_else_gets_erpnexts_list_unchanged(self):
		result, passed = self._pick({"item_code": CHAIN, "warehouse": "WH"})
		self.assertEqual(result, self.ERPNEXT_LIST)
		self.assertEqual(dict(passed), {"item_code": CHAIN, "warehouse": "WH"})

	def test_a_hybrid_finding_lists_only_its_customers_batches(self):
		result, passed = self._pick(
			{
				"item_code": CHAIN,
				"warehouse": "WH",
				"stock_entry_type": MT_WO,
				"parent_manufacturing_order": HYBRID_PMO,
				"manufacturing_order": None,
				"manufacturing_work_order": None,
			}
		)
		self.assertEqual(result, [(OWN, 5.0)])
		# The context keys never reach ERPNext's query.
		self.assertEqual(dict(passed), {"item_code": CHAIN, "warehouse": "WH"})


class TestFifo(_Case):
	"""``se_utils.get_fifo_batches`` -- the Reserve / Department / desk MT(WO) fill."""

	def _allocate(self, fifo, pmo=HYBRID_PMO, throw=False):
		entry = _entry(
			stock_entry_type=RESERVE,
			date=None,
			posting_time=None,
			posting_date=None,
			source_warehouse=None,
			main_slip=None,
			to_main_slip=None,
			flags=frappe._dict(throw_batch_error=throw),
		)
		row = _row(
			batch_no=None,
			pmo=pmo,
			qty=1.0,
			inventory_type=None,
			customer=None,
			custom_variant_of="F",
			manufacturing_operation=None,
		)

		def pmo_flags(*args, **kwargs):
			# A Hybrid PMO is not customer gold, and the manufacturer allows company stock
			# in place of customer goods -- the fallback the rule must override.
			return frappe._dict(
				is_customer_gold=0,
				is_customer_diamond=0,
				is_customer_gemstone=0,
				is_customer_material=0,
				customer=GK_EXPORT,
				manufacturer="MFR",
			)

		with ExitStack() as stack:
			_site(stack)
			stack.enter_context(
				patch.object(
					se_utils,
					"get_auto_batch_nos",
					return_value=[frappe._dict(batch_no=b, qty=q) for b, q in fifo],
				)
			)
			stack.enter_context(
				patch.object(se_utils, "is_customer_sample_batch", return_value=False)
			)
			stack.enter_context(
				patch.object(se_utils, "bulk_map", return_value=BATCHES)
			)
			stack.enter_context(
				patch(
					"frappe.db.get_value",
					side_effect=_keyed_get_value(
						{
							"Parent Manufacturing Order": pmo_flags,
							"Manufacturer": lambda *a, **k: 1,
						}
					),
				)
			)
			rows = se_utils.get_fifo_batches(entry, row)
		return {
			r.get("batch_no"): (r.get("inventory_type"), r.get("customer"))
			for r in rows
		}

	def test_a_hybrid_finding_skips_earlier_batches_for_its_customers_own(self):
		lanes = self._allocate([(COMPANY, 5.0), (OTHER, 5.0), (OWN, 5.0)])
		self.assertEqual(lanes, {OWN: ("Customer Goods", GK_EXPORT)})

	def test_other_sales_types_keep_plain_fifo(self):
		lanes = self._allocate([(COMPANY, 5.0), (OWN, 5.0)], pmo=OUTRIGHT_PMO)
		self.assertEqual(list(lanes), [COMPANY])

	def test_a_shortfall_names_the_ref_customer(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._allocate([(COMPANY, 5.0), (OTHER, 5.0)], throw=True)
		self.assertIn(REF, str(raised.exception))


class TestEmployeeIrInjection(_Case):
	"""``main_slip_inject._expand_source_rows_for_fifo`` -- the Employee IR receive leg."""

	def _expand(self, pool):
		entry = _entry(
			manufacturing_order=HYBRID_PMO,
			posting_date="2026-10-06",
			posting_time="10:00:00",
		)
		row = {
			"item_code": CHAIN,
			"s_warehouse": "Labh Main Slip - KGJPL",
			"t_warehouse": "Waxing WO - KGJPL",
			"qty": 1.0,
			"serial_no": None,
			"batch_no": None,
		}
		with ExitStack() as stack:
			stack.enter_context(
				patch.object(
					msi,
					"capped_auto_batch_nos",
					return_value=[frappe._dict(batch_no=b, qty=q) for b, q in pool],
				)
			)
			stack.enter_context(
				patch.object(
					msi, "_select_fifo_batches_reservable_at_dest", return_value=None
				)
			)
			stack.enter_context(
				patch(
					"frappe.get_cached_value",
					side_effect=lambda doctype, name, field, *a, **k: 1
					if (doctype, field) == ("Item", "has_batch_no")
					else None,
				)
			)
			stack.enter_context(
				patch.object(msi, "hybrid_requirement", return_value=_requirement())
			)
			stack.enter_context(
				patch.object(msi, "get_batch_ownership_map", return_value=BATCHES)
			)
			stack.enter_context(
				patch.object(
					msi,
					"batch_priority_map",
					side_effect=lambda names: {
						name: {
							"inventory_type": BATCHES[name].custom_inventory_type,
							"customer": BATCHES[name].custom_customer,
						}
						for name in names
					},
				)
			)
			stack.enter_context(
				patch.object(msi, "batch_sort_key", side_effect=lambda *a, **k: 0)
			)
			return msi._expand_source_rows_for_fifo(entry, row)

	def test_only_the_customers_own_batch_is_drawn(self):
		out = self._expand([(COMPANY, 5.0), (OTHER, 5.0), (OWN, 5.0)])
		self.assertEqual([line["batch_no"] for line in out], [OWN])
		self.assertEqual(out[0]["qty"], 1.0)

	def test_no_own_batch_throws_naming_the_ref_customer(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._expand([(COMPANY, 5.0), (OTHER, 5.0)])
		self.assertIn(REF, str(raised.exception))

	def test_too_little_own_stock_throws_naming_the_ref_customer(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._expand([(OWN, 0.4), (COMPANY, 5.0)])
		self.assertIn(REF, str(raised.exception))


class TestRefCustomerOnBatches(_Case):
	def test_a_customer_goods_receipt_stamps_its_ref_customer_on_the_new_batch(self):
		inserted = []

		def new_doc(doctype):
			batch = frappe._dict(insert=MagicMock())
			inserted.append(batch)
			return batch

		doc = _Doc(
			doctype="Stock Entry",
			stock_entry_type="Customer Goods Received",
			_customer=GK_EXPORT,
			ref_customer=REF,
			name="KGJPL-SE-CGR-TEST",
			items=[
				_Row(
					name="row-1",
					item_code="M-G-24KT-99.9-Y",
					batch_no=None,
					customer=GK_EXPORT,
				)
			],
		)
		today = MagicMock()
		today.today.return_value = datetime(2026, 10, 6)
		with (
			patch.object(batch_rename, "get_year_code", return_value="2F"),
			patch.object(batch_rename, "get_next_serial", return_value="01"),
			patch.object(batch_rename, "_source_row_rate", return_value=0.0),
			patch.object(
				batch_rename, "get_customer_gold_receipt_type", return_value=None
			),
			patch.object(batch_rename.frappe, "new_doc", side_effect=new_doc),
			patch.object(batch_rename.frappe.db, "exists", return_value=False),
			patch.object(batch_rename, "datetime", today),
		):
			batch_rename.create_parent_batches(doc)

		self.assertEqual(len(inserted), 1)
		self.assertEqual(inserted[0].custom_ref_customer, REF)
		self.assertEqual(inserted[0].custom_customer, GK_EXPORT)

	def _mint_children(self, doc, ref_customers):
		table = namespace._BatchTable(
			{row.batch_no for row in doc.items if row.batch_no}
		)
		with (
			patch(
				"frappe.new_doc",
				side_effect=lambda doctype: namespace._FakeBatch(table),
			),
			patch("frappe.db.sql", side_effect=table.sql),
			patch("frappe.db.exists", side_effect=table.exists),
			patch.object(
				batch_rename,
				"get_batch_ref_customer_map",
				side_effect=lambda names: {n: ref_customers.get(n) for n in names},
			),
		):
			batch_rename.create_child_batches(doc)
		return table.minted

	def test_each_process_loss_keeps_the_ref_customer_of_its_own_source(self):
		first = "GJCU0009-2F09-M-G-22KT-91.75-Y-03-A"
		second = "GJCU0009-2F09-M-G-22KT-91.75-Y-04"
		doc = namespace._entry(
			[
				namespace._consume("c1", first),
				namespace._produce("p1"),
				namespace._consume("c2", second),
				namespace._produce("p2"),
			]
		)
		minted = self._mint_children(doc, {first: REF, second: OTHER_REF})
		self.assertEqual([b.custom_ref_customer for b in minted], [REF, OTHER_REF])

	def test_a_conversion_of_one_ref_customers_material_keeps_it(self):
		a = "GJCU0009-2F09-M-G-24KT-99.9-Y-12"
		b = "GJCU0009-2F09-M-G-24KT-99.9-Y-13"
		doc = namespace._entry(
			[
				namespace._consume("c1", a, item_code="M-G-24KT-99.9-Y"),
				namespace._consume("c2", b, item_code="M-G-24KT-99.9-Y"),
				namespace._produce("p1", item_code="M-G-22KT-91.75-Y"),
			],
			stock_entry_type="Repack-Metal Conversion",
		)
		minted = self._mint_children(doc, {a: REF, b: REF})
		self.assertEqual([m.custom_ref_customer for m in minted], [REF])

	def test_a_conversion_mixing_two_ref_customers_is_left_blank(self):
		a = "GJCU0009-2F09-M-G-24KT-99.9-Y-12"
		b = "GJCU0009-2F09-M-G-24KT-99.9-Y-13"
		doc = namespace._entry(
			[
				namespace._consume("c1", a, item_code="M-G-24KT-99.9-Y"),
				namespace._consume("c2", b, item_code="M-G-24KT-99.9-Y"),
				namespace._produce("p1", item_code="M-G-22KT-91.75-Y"),
			],
			stock_entry_type="Repack-Metal Conversion",
		)
		minted = self._mint_children(doc, {a: REF, b: OTHER_REF})
		self.assertEqual([m.custom_ref_customer for m in minted], [None])

	def _stamp(self, batch, ref_customer=REF, columns=True, is_customer_inventory=True):
		db = MagicMock()
		db.has_column.return_value = columns
		db.get_value.side_effect = (
			lambda doctype, name=None, fieldname=None, **kw: ref_customer
			if (doctype, fieldname) == ("Stock Entry", "ref_customer")
			else None
		)
		with patch.object(batch_utils.frappe, "db", db):
			batch_utils._stamp_ref_customer(batch, is_customer_inventory)
		return batch

	def _batch(self, **fields):
		values = {
			"reference_doctype": "Stock Entry",
			"reference_name": "KGJPL-SE-CGR-TEST",
			"custom_ref_customer": None,
		}
		values.update(fields)
		return SimpleNamespace(**values)

	def test_an_erpnext_minted_customer_batch_takes_its_receipts_ref_customer(self):
		self.assertEqual(self._stamp(self._batch()).custom_ref_customer, REF)

	def test_a_stamped_batch_is_never_restamped(self):
		batch = self._stamp(self._batch(custom_ref_customer=OTHER_REF))
		self.assertEqual(batch.custom_ref_customer, OTHER_REF)

	def test_company_batches_and_unmigrated_sites_are_left_alone(self):
		self.assertIsNone(
			self._stamp(self._batch(), is_customer_inventory=False).custom_ref_customer
		)
		self.assertIsNone(self._stamp(self._batch(), columns=False).custom_ref_customer)
		self.assertIsNone(
			self._stamp(
				self._batch(reference_doctype="Purchase Receipt")
			).custom_ref_customer
		)


class TestSncExemption(_Case):
	MWO = _Doc(
		name="MWO-HYBRID-1",
		docstatus=1,
		manufacturing_order=HYBRID_PMO,
		manufacturing_operation="MOP-1",
		customer=GK_EXPORT,
	)

	@staticmethod
	def _held(item_code, batch_ref_customer, batch_customer=GK_EXPORT):
		return {
			"item_code": item_code,
			"batch_no": f"B-{item_code}-{batch_ref_customer}",
			"qty": 1.0,
			"inventory_type": "Customer Goods" if batch_customer else "Regular Stock",
			"batch_customer": batch_customer,
			"customer": batch_customer,
			"batch_voucher_type": "Customer Subcontracting",
			"batch_ref_customer": batch_ref_customer,
		}

	def _needs_settlement(self, held, pmo=HYBRID_PMO, rules=RULES):
		with ExitStack() as stack:
			_site(stack, rules=rules)
			stack.enter_context(
				patch(
					"frappe.db.get_value",
					side_effect=_keyed_get_value(
						{"Parent Manufacturing Order": lambda *a, **k: PMOS[pmo]}
					),
				)
			)
			stack.enter_context(patch.object(snc, "_get_mwo", return_value=self.MWO))
			stack.enter_context(patch.object(snc, "_is_customer_gold", return_value=0))
			stack.enter_context(
				patch.object(snc, "_get_receivable_gold_rows", return_value=held)
			)
			return snc._mwo_needs_settlement(self.MWO)

	def test_the_customers_own_listed_finding_is_not_borrowed(self):
		self.assertFalse(self._needs_settlement([self._held(CHAIN, REF)]))

	def test_another_ref_customers_finding_is_still_borrowed(self):
		self.assertTrue(self._needs_settlement([self._held(CHAIN, OTHER_REF)]))

	def test_an_unlisted_customer_finding_is_still_borrowed(self):
		self.assertTrue(self._needs_settlement([self._held(POST, REF)]))

	def test_other_sales_types_are_unchanged(self):
		self.assertTrue(
			self._needs_settlement([self._held(CHAIN, REF)], pmo=OUTRIGHT_PMO)
		)

	def test_an_empty_table_leaves_settlement_unchanged(self):
		self.assertTrue(self._needs_settlement([self._held(CHAIN, REF)], rules={}))

	def test_metal_rows_never_read_the_hybrid_settings(self):
		with patch.object(H, "get_hybrid_finding_rules") as rules:
			self.assertIsNone(
				H.hybrid_settlement_context(HYBRID_PMO, ["M-G-22KT-91.75-Y"])
			)
		rules.assert_not_called()
