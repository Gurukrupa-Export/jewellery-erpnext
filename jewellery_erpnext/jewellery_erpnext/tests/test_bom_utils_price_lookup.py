"""Making-charge and diamond price lookups in the BOM pricing engine (bom_utils).

Making Charge Price headers are matched on the BOM row's metal touch. The match is
strict and null-safe: a row with a touch matches only that touch, and a row with no
touch matches only headers whose touch is NULL or blank. A row that matches nothing
keeps the making values it already carries, and the miss is logged.

For "Size (in mm)" diamond price lists, a row that has neither size_in_mm nor
diamond_size_in_mm is priced by its sieve size, and the sieve size is part of the
price cache key.

Most classes are DB-free per the suite convention: ``setUpClass`` is neutralised, the
Query Builder's ``run`` is replaced by a fake that reads the generated SQL, and the
fake returns ``frappe._dict`` rows, the type a real ``run(as_dict=True)`` returns.
``TestMakingChargeLookupAgainstDatabase`` runs the real queries on namespaced rows
inside a savepoint that is rolled back.
"""

import re
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from pypika.queries import QueryBuilder

from jewellery_erpnext.jewellery_erpnext.doc_events import bom_utils

MOD = "jewellery_erpnext.jewellery_erpnext.doc_events.bom_utils"

MCP_TOUCH = "`tabMaking Charge Price`.`metal_touch`"
# The blank-touch predicate as it must appear in the engine's queries: one
# parenthesised OR inside the AND chain, bound to the header table.
BLANK_TOUCH_SQL = f"AND ({MCP_TOUCH} IS NULL OR {MCP_TOUCH}='')"
_TOUCH_EQUALS = re.compile(r"`tabMaking Charge Price`\.`metal_touch`='([^']*)'")

ITEM = "item"
FINDING = "finding"
NO_TOUCH_PREDICATE = "<no touch predicate>"


def _touch_in(sql):
	"""The touch a Making Charge Price query asks for (None for the blank rule)."""
	if f"{MCP_TOUCH} IS NULL" in sql:
		return None
	match = _TOUCH_EQUALS.search(sql)
	return match.group(1) if match else NO_TOUCH_PREDICATE


def _child_in(sql):
	if "`tabMaking Charge Price Finding Subcategory`" in sql:
		return FINDING
	return ITEM


class _FakeMakingChargeTables:
	"""Answers the engine's Query Builder calls from an in-memory catalog.

	``catalog`` maps (child table, metal touch) to the row a lookup returns, where
	the child table is ITEM or FINDING and a blank touch is None. Count subqueries
	always report a match, so every lookup the engine can make is issued.
	"""

	def __init__(self, catalog=None):
		self.catalog = catalog or {}
		self.lookups = []

	def run(self, query, *args, **kwargs):
		sql = query.get_sql()
		if "COUNT(" in sql:
			return [(1,)]
		self.lookups.append(sql)
		found = self.catalog.get((_child_in(sql), _touch_in(sql)))
		if not found:
			return []
		row = frappe._dict(found)
		if "non_finding_rate" in sql:
			row.non_finding_rate = 1
		return [row]


def _making_row(**overrides):
	row = {
		"rate_per_gm": 0,
		"rate_per_pc": 0,
		"rate_per_gm_threshold": 0,
		"wastage": 0,
		"supplier_fg_purchase_rate": 0,
	}
	row.update(overrides)
	return row


def _metal(idx=1, **fields):
	row = frappe._dict(
		parentfield="metal_detail",
		idx=idx,
		metal_type="Gold",
		metal_touch="22KT",
		metal_purity="91.6",
		is_customer_item=0,
		quantity=10,
		amount=1000,
		making_rate=0,
		making_amount=0,
		wastage_rate=0,
		wastage_amount=0,
		fg_purchase_rate=0,
		fg_purchase_amount=0,
	)
	row.update(fields)
	return row


def _finding(idx=1, **fields):
	fields.setdefault("finding_type", "_TEST-FINDING-TYPE")
	row = _metal(idx=idx, **fields)
	row.parentfield = "finding_detail"
	return row


def _bom(metal_detail=(), finding_detail=(), metal_and_finding_weight=10):
	return frappe._dict(
		name="_TEST-BOM",
		customer="_TEST-CUSTOMER",
		metal_detail=list(metal_detail),
		finding_detail=list(finding_detail),
		metal_and_finding_weight=metal_and_finding_weight,
		compute_making_charges_on=None,
		total_diamond_weight=0,
	)


class _MakingChargeEngineCase(IntegrationTestCase):
	"""Runs get_metal_and_finding_making_rate against _FakeMakingChargeTables."""

	@classmethod
	def setUpClass(cls):
		pass

	def _price(self, bom, catalog=None, sub_category="_TEST-SUBCAT"):
		fake = _FakeMakingChargeTables(catalog)
		with (
			patch.object(
				QueryBuilder, "run", autospec=True, side_effect=fake.run
			) as run,
			patch(f"{MOD}.frappe.db.get_value", return_value=None),
			patch(f"{MOD}.frappe.logger") as logger,
		):
			amount = bom_utils.get_metal_and_finding_making_rate(
				bom, sub_category, "_TEST-SETTING"
			)
		self.run_calls = run.call_count
		self.miss_log = logger.return_value.error
		return fake, amount


class TestMetalTouchCondition(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _sql(self, metal_touch):
		mcp = frappe.qb.DocType("Making Charge Price")
		return (
			frappe.qb.from_(mcp)
			.select(mcp.name)
			.where(
				(mcp.customer == "_TEST-CUSTOMER")
				& bom_utils._metal_touch_condition(mcp, metal_touch)
			)
			.get_sql()
		)

	def test_a_touch_is_matched_by_equality(self):
		sql = self._sql("22KT")

		self.assertIn("`metal_touch`='22KT'", sql)
		self.assertNotIn("IS NULL", sql)

	def test_a_blank_touch_matches_only_null_or_blank_headers(self):
		for metal_touch in (None, ""):
			with self.subTest(metal_touch=metal_touch):
				sql = self._sql(metal_touch)

				self.assertIn(
					"AND (`metal_touch` IS NULL OR `metal_touch`='')",
					sql,
				)


class TestMakingChargeLookupByTouch(_MakingChargeEngineCase):
	def test_each_touch_gets_its_own_making_charge_price(self):
		row_18 = _metal(idx=1, metal_touch="18KT")
		row_22 = _metal(idx=2, metal_touch="22KT")
		catalog = {
			(ITEM, "18KT"): _making_row(rate_per_gm=10, supplier_fg_purchase_rate=8),
			(ITEM, "22KT"): _making_row(rate_per_gm=12, supplier_fg_purchase_rate=9),
		}

		fake, amount = self._price(_bom([row_18, row_22]), catalog)

		self.assertEqual([_touch_in(sql) for sql in fake.lookups], ["18KT", "22KT"])
		self.assertEqual((row_18.making_rate, row_18.fg_purchase_rate), (10, 8))
		self.assertEqual((row_22.making_rate, row_22.fg_purchase_rate), (12, 9))
		self.assertEqual(row_18.making_amount, 100)
		self.assertEqual(row_22.making_amount, 120)
		self.assertEqual(amount, 220)
		self.miss_log.assert_not_called()

	def test_silver_row_is_zeroed_and_issues_no_query(self):
		silver = _metal(
			metal_type="Silver",
			metal_touch="92.5",
			making_rate=5,
			making_amount=50,
			wastage_rate=1,
			wastage_amount=10,
			fg_purchase_rate=4,
			fg_purchase_amount=40,
		)

		fake, amount = self._price(_bom([silver]))

		self.assertEqual(self.run_calls, 0)
		self.assertEqual(fake.lookups, [])
		for field in (
			"making_rate",
			"making_amount",
			"wastage_rate",
			"wastage_amount",
			"fg_purchase_rate",
			"fg_purchase_amount",
		):
			self.assertEqual(silver[field], 0, field)
		self.assertEqual(amount, 0)

	def test_blank_touch_without_a_blank_header_keeps_the_stored_making(self):
		row = _metal(
			metal_touch=None,
			making_rate=7,
			making_amount=70,
			wastage_rate=2,
			fg_purchase_rate=3,
		)
		# Only a touched header exists: a blank-touch row must not borrow it.
		catalog = {(ITEM, "22KT"): _making_row(rate_per_gm=12)}

		fake, _amount = self._price(_bom([row]), catalog)

		self.assertEqual(len(fake.lookups), 1)
		self.assertIn(BLANK_TOUCH_SQL, fake.lookups[0])
		self.assertEqual(row.making_rate, 7)
		self.assertEqual(row.making_amount, 70)
		self.assertEqual(row.wastage_rate, 2)
		self.assertEqual(row.fg_purchase_rate, 3)
		self.miss_log.assert_called_once()

	def test_finding_fallback_query_carries_the_touch(self):
		finding = _finding(metal_touch="18KT")
		# No Finding Subcategory price, so the engine falls back to the
		# Item Subcategory table, and must still ask for the row's own touch.
		catalog = {(ITEM, "18KT"): _making_row(rate_per_gm=11)}

		fake, _amount = self._price(_bom(finding_detail=[finding]), catalog)

		self.assertEqual(
			[(_child_in(sql), _touch_in(sql)) for sql in fake.lookups],
			[(FINDING, "18KT"), (ITEM, "18KT")],
		)
		self.assertEqual(finding.making_rate, 11)
		self.assertEqual(finding.non_finding_rate, 1)

	def test_blank_touch_stays_parenthesised_in_both_lookup_queries(self):
		metal = _metal(metal_touch=None)
		finding = _finding(metal_touch="")

		fake, _amount = self._price(_bom([metal], [finding]))

		# metal lookup, finding lookup, finding fallback lookup
		self.assertEqual(len(fake.lookups), 3)
		for sql in fake.lookups:
			self.assertIn(BLANK_TOUCH_SQL, sql)
			self.assertIn("`tabMaking Charge Price`.`metal_type`='Gold' AND (", sql)
		self.assertEqual(self.miss_log.call_count, 2)

	def test_customer_item_miss_is_not_logged(self):
		row = _metal(is_customer_item=1, metal_touch="18KT", making_amount=40)

		self._price(_bom([row]))

		self.assertEqual(row.making_amount, 40)
		self.miss_log.assert_not_called()


class TestDiamondSizeInMm(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	PRICE = {
		"rate": 100,
		"handling_rate": 0,
		"supplier_fg_purchase_rate": 60,
		"outright_handling_charges_in_percentage": 0,
		"outright_handling_charges_rate": 0,
		"outwork_handling_charges_in_percentage": 0,
		"outwork_handling_charges_rate": 0,
	}

	def _bom(self):
		return frappe._dict(
			selling_price_list="Standard Selling",
			cust_diamond_price_list_type="Size (in mm)",
			customer="_TEST-CUSTOMER",
		)

	def _diamond(self, **fields):
		row = frappe._dict(
			diamond_type="Natural",
			stone_shape="Round",
			quality="_TEST-QUALITY",
			sieve_size_range=None,
			size_in_mm=None,
			diamond_size_in_mm=None,
			diamond_sieve_size=None,
			quantity=2,
			is_customer_item=0,
			total_diamond_rate=0,
		)
		row.update(fields)
		return row

	def _price(self, diamonds, prices=None):
		"""Price ``diamonds`` with one shared cache; return the captured filters."""
		captured = []

		def get_list(doctype, filters=None, **kwargs):
			captured.append(dict(filters))
			if prices is None:
				return [frappe._dict(self.PRICE)]
			return prices(filters)

		bom = self._bom()
		cache = frappe._dict()
		with (
			patch(f"{MOD}.frappe.get_list", side_effect=get_list),
			patch(f"{MOD}.frappe.msgprint") as msgprint,
		):
			amounts = [
				bom_utils._calculate_diamond_amount(bom, diamond, {}, cache)
				for diamond in diamonds
			]
		self.msgprint = msgprint
		return captured, amounts

	def test_both_sizes_keep_both_filters(self):
		diamond = self._diamond(
			size_in_mm=1.2, diamond_size_in_mm="_TEST-MM", diamond_sieve_size="SS-1"
		)

		(filters,), _amounts = self._price([diamond])

		self.assertEqual(filters["size_in_mm"], 1.2)
		self.assertEqual(filters["diamond_size_in_mm"], "_TEST-MM")

	def test_only_diamond_size_in_mm_filters_on_it(self):
		diamond = self._diamond(
			diamond_size_in_mm="_TEST-MM", diamond_sieve_size="SS-1"
		)

		(filters,), _amounts = self._price([diamond])

		self.assertNotIn("size_in_mm", filters)
		self.assertEqual(filters["diamond_size_in_mm"], "_TEST-MM")

	def test_only_size_in_mm_does_not_fall_back_to_the_sieve_size(self):
		diamond = self._diamond(size_in_mm=1.2, diamond_sieve_size="SS-1")

		(filters,), _amounts = self._price([diamond])

		self.assertEqual(filters["size_in_mm"], 1.2)
		self.assertNotIn("diamond_size_in_mm", filters)

	def test_neither_size_falls_back_to_the_sieve_size(self):
		diamond = self._diamond(diamond_sieve_size="SS-1")

		(filters,), amounts = self._price([diamond])

		self.assertNotIn("size_in_mm", filters)
		self.assertEqual(filters["diamond_size_in_mm"], "SS-1")
		self.assertEqual(amounts, [200])
		self.assertEqual(diamond.fg_purchase_rate, 60)

	def test_sieve_size_is_part_of_the_price_cache_key(self):
		first = self._diamond(diamond_sieve_size="SS-1")
		second = self._diamond(diamond_sieve_size="SS-2")

		def prices(filters):
			rate = {"SS-1": 100, "SS-2": 150}[filters["diamond_size_in_mm"]]
			return [frappe._dict(self.PRICE, rate=rate)]

		captured, amounts = self._price([first, second], prices)

		self.assertEqual(
			[filters["diamond_size_in_mm"] for filters in captured], ["SS-1", "SS-2"]
		)
		self.assertEqual(amounts, [200, 300])

	def test_no_price_row_keeps_the_row_rate(self):
		diamond = self._diamond(diamond_sieve_size="SS-1", total_diamond_rate=40)

		_captured, amounts = self._price([diamond], prices=lambda filters: [])

		self.assertEqual(amounts, [80])
		self.assertEqual(diamond.diamond_rate_for_specified_quantity, 80)
		self.msgprint.assert_called_once()


PREFIX = "_TEST-MCPTOUCH-"
CUSTOMER = f"{PREFIX}CUSTOMER"
SETTING_TYPE = f"{PREFIX}SETTING"
METAL_TYPE = f"{PREFIX}METAL"
SUBCATEGORY = f"{PREFIX}SUBCAT"
OTHER_SUBCATEGORY = f"{PREFIX}OTHER-SUBCAT"
SAVEPOINT = "test_bom_utils_price_lookup"


class TestMakingChargeLookupAgainstDatabase(IntegrationTestCase):
	"""Real Making Charge Price rows and the engine's real queries.

	Every value the queries filter on is namespaced, so no site data can match.
	The rows are written with ``db_insert`` (no link validation, no hooks) inside a
	savepoint that each test rolls back.
	"""

	def setUp(self):
		frappe.db.savepoint(SAVEPOINT)
		self.addCleanup(frappe.db.rollback, save_point=SAVEPOINT)

		self._header("18", "18KT", SUBCATEGORY, rate_per_gm=10, fg_rate=8)
		self._header("22", "22KT", SUBCATEGORY, rate_per_gm=12, fg_rate=9)
		# A 14KT header exists, but only for another subcategory: a 14KT row of
		# SUBCATEGORY must miss rather than borrow the 18KT or 22KT price.
		self._header("14", "14KT", OTHER_SUBCATEGORY, rate_per_gm=99, fg_rate=99)

	def _header(self, suffix, metal_touch, subcategory, rate_per_gm, fg_rate):
		name = f"{PREFIX}{suffix}"
		frappe.get_doc(
			{
				"doctype": "Making Charge Price",
				"name": name,
				"customer": CUSTOMER,
				"setting_type": SETTING_TYPE,
				"metal_type": METAL_TYPE,
				"metal_touch": metal_touch,
			}
		).db_insert()
		frappe.get_doc(
			{
				"doctype": "Making Charge Price Item Subcategory",
				"name": f"{name}-ROW-1",
				"parent": name,
				"parenttype": "Making Charge Price",
				"parentfield": "subcategory",
				"idx": 1,
				"subcategory": subcategory,
				"rate_per_gm": rate_per_gm,
				"rate_per_pc": 0,
				"rate_per_gm_threshold": 0,
				"wastage": 0,
				"supplier_fg_purchase_rate": fg_rate,
			}
		).db_insert()

	def test_rows_of_two_touches_get_their_own_rates(self):
		row_18 = _metal(idx=1, metal_type=METAL_TYPE, metal_touch="18KT")
		row_22 = _metal(idx=2, metal_type=METAL_TYPE, metal_touch="22KT")
		bom = _bom([row_18, row_22])
		bom.customer = CUSTOMER

		with patch(f"{MOD}.frappe.logger") as logger:
			bom_utils.get_metal_and_finding_making_rate(bom, SUBCATEGORY, SETTING_TYPE)

		self.assertEqual((row_18.making_rate, row_18.fg_purchase_rate), (10, 8))
		self.assertEqual((row_22.making_rate, row_22.fg_purchase_rate), (12, 9))
		logger.return_value.error.assert_not_called()

	def test_header_without_the_subcategory_child_leaves_the_row_unchanged(self):
		row_14 = _metal(
			idx=1,
			metal_type=METAL_TYPE,
			metal_touch="14KT",
			making_rate=7,
			making_amount=70,
			fg_purchase_rate=3,
			fg_purchase_amount=30,
		)
		bom = _bom([row_14])
		bom.customer = CUSTOMER

		with patch(f"{MOD}.frappe.logger") as logger:
			bom_utils.get_metal_and_finding_making_rate(bom, SUBCATEGORY, SETTING_TYPE)

		self.assertEqual((row_14.making_rate, row_14.making_amount), (7, 70))
		self.assertEqual((row_14.fg_purchase_rate, row_14.fg_purchase_amount), (3, 30))
		logger.return_value.error.assert_called_once()
		args = logger.return_value.error.call_args.args
		self.assertEqual(args[7], "14KT")
		self.assertEqual(args[8], SUBCATEGORY)
		self.assertIsNone(args[9])
