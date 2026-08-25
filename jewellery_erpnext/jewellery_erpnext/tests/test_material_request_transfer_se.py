# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for the deferred Material Request -> 'Material Transfer From Reserve'
Stock Entry (material_request.on_submit / materialize_transfer_se / _create_transfer_se)."""

from collections import defaultdict
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext import bounded_retry, serialize
from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
	prefetch as prefetch_mod,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils import metal_utils
from jewellery_erpnext.jewellery_erpnext.doc_events import material_request as mr_mod
from jewellery_erpnext.jewellery_erpnext.serialize import LockTimeoutError

_MR = "jewellery_erpnext.jewellery_erpnext.doc_events.material_request"


class TestOnSubmitDefersTransferSE(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MR}.frappe.enqueue")
	def test_enqueues_with_dedup_and_job_id(self, mock_enqueue):
		mr = MagicMock(name="MR")
		mr.name = "MR-001"
		mr.custom_reserve_se = "SE-RESERVE"
		mr.custom_transfer_se = None
		mr_mod.on_submit(mr)

		mock_enqueue.assert_called_once()
		args, kwargs = mock_enqueue.call_args
		self.assertIs(args[0], mr_mod.materialize_transfer_se)
		self.assertEqual(kwargs["queue"], "long")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["job_id"], "mr_transfer_se::MR-001")
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["mr_name"], "MR-001")
		mr.db_set.assert_called_once_with(
			"custom_transfer_se_state", "Pending", update_modified=False
		)

	@patch(f"{_MR}.frappe.enqueue")
	def test_skips_when_no_reserve_se(self, mock_enqueue):
		mr = MagicMock()
		mr.custom_reserve_se = None
		mr_mod.on_submit(mr)
		mock_enqueue.assert_not_called()

	@patch(f"{_MR}.frappe.enqueue")
	def test_skips_when_transfer_already_materialized(self, mock_enqueue):
		mr = MagicMock()
		mr.custom_reserve_se = "SE-RESERVE"
		mr.custom_transfer_se = "SE-TRANSFER"
		mr_mod.on_submit(mr)
		mock_enqueue.assert_not_called()

	def tearDown(self):
		return super().tearDown()


class TestMaterializeTransferSE(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_lock_timeout_is_swallowed(self):
		@contextmanager
		def _raise_lock(*a, **k):
			raise LockTimeoutError("held")
			yield  # pragma: no cover

		with patch.object(serialize, "conflict_lock", _raise_lock), patch.object(
			bounded_retry, "run_with_retry"
		) as mock_run:
			# Should NOT raise.
			mr_mod.materialize_transfer_se("MR-001")
		mock_run.assert_not_called()

	def test_generic_error_marks_failed_and_reraises(self):
		@contextmanager
		def _noop(*a, **k):
			yield

		with patch.object(serialize, "conflict_lock", _noop), patch.object(
			bounded_retry, "run_with_retry", side_effect=ValueError("boom")
		), patch(f"{_MR}.frappe.db") as mock_db, patch(f"{_MR}.frappe.log_error"):
			with self.assertRaises(ValueError):
				mr_mod.materialize_transfer_se("MR-001")

		# Failure recorded on the MR for reconciliation.
		mock_db.set_value.assert_called_once()
		args = mock_db.set_value.call_args[0]
		self.assertEqual(args[0], "Material Request")
		self.assertEqual(args[1], "MR-001")
		self.assertEqual(args[2]["custom_transfer_se_state"], "Failed")
		self.assertIn("boom", args[2]["custom_transfer_se_error"])

	def tearDown(self):
		return super().tearDown()


class TestCreateTransferSEIdempotency(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{_MR}.frappe.copy_doc")
	@patch(f"{_MR}.frappe.db.sql")
	@patch(f"{_MR}.frappe.get_doc")
	def test_returns_when_transfer_se_already_set(
		self, mock_get_doc, mock_sql, mock_copy
	):
		mr = MagicMock()
		mr.custom_reserve_se = "SE-RESERVE"
		mr.get = MagicMock(return_value="SE-TRANSFER")  # already linked
		mock_get_doc.return_value = mr

		mr_mod._create_transfer_se("MR-001")

		mock_sql.assert_not_called()
		mock_copy.assert_not_called()

	@patch(f"{_MR}.frappe.copy_doc")
	@patch(f"{_MR}.frappe.db.sql")
	@patch(f"{_MR}.frappe.get_doc")
	def test_links_existing_submitted_transfer_se(
		self, mock_get_doc, mock_sql, mock_copy
	):
		mr = MagicMock()
		mr.custom_reserve_se = "SE-RESERVE"
		mr.get = MagicMock(return_value=None)
		mock_get_doc.return_value = mr
		mock_sql.return_value = [("SE-TRANSFER-9",)]  # an existing transfer SE

		mr_mod._create_transfer_se("MR-001")

		# Linked + marked Done, without copying/creating a new SE.
		mr.db_set.assert_any_call(
			"custom_transfer_se", "SE-TRANSFER-9", update_modified=False
		)
		mr.db_set.assert_any_call(
			"custom_transfer_se_state", "Done", update_modified=False
		)
		mock_copy.assert_not_called()

	def tearDown(self):
		return super().tearDown()


class MockMaterialRequest:
	def __init__(
		self,
		set_from_warehouse=None,
		set_warehouse=None,
		custom_transfer_type=None,
		material_request_type=None,
	):
		self.set_from_warehouse = set_from_warehouse
		self.set_warehouse = set_warehouse
		self.custom_transfer_type = custom_transfer_type
		self.material_request_type = material_request_type
		self.custom_manufacturing_operation = None


class TestMaterialRequestTransferType(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, mr, branches):
		"""Call before_validate with branch lookups and the unrelated
		downstream validators stubbed out."""

		def _gv(doctype, name, field):
			return branches.get(name)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.material_request.frappe.db.get_value",
			side_effect=_gv,
		), patch.object(mr_mod, "update_pure_qty"), patch.object(
			mr_mod, "validate_target_item"
		), patch.object(mr_mod, "validate_warehouse"), patch.object(
			mr_mod, "set_reservation_warehouse"
		):
			mr_mod.before_validate(mr, None)
		return mr.custom_transfer_type

	def test_existing_value_preserved(self):
		"""A manually-chosen value is not overwritten even when the branches
		differ -- the original bug on KGJPL-MR-MT-26-02528."""
		mr = MockMaterialRequest(
			set_from_warehouse="Central RM - KGJPL",
			set_warehouse="Waxing RM - KGJPL",
			custom_transfer_type="Transfer To Department",
		)
		result = self._run(mr, {"Central RM - KGJPL": "", "Waxing RM - KGJPL": None})
		self.assertEqual(result, "Transfer To Department")

	def test_blank_same_branch(self):
		mr = MockMaterialRequest(set_from_warehouse="W1", set_warehouse="W2")
		result = self._run(mr, {"W1": "BR-A", "W2": "BR-A"})
		self.assertEqual(result, "Transfer To Department")

	def test_blank_different_branches(self):
		mr = MockMaterialRequest(set_from_warehouse="W1", set_warehouse="W2")
		result = self._run(mr, {"W1": "BR-A", "W2": "BR-B"})
		self.assertEqual(result, "Transfer To Branch")

	def test_blank_branches_empty_vs_null_treated_as_same(self):
		"""Regression: "" and NULL branch are normalised so two no-branch
		warehouses default to Transfer To Department, not Transfer To Branch."""
		mr = MockMaterialRequest(set_from_warehouse="W1", set_warehouse="W2")
		result = self._run(mr, {"W1": "", "W2": None})
		self.assertEqual(result, "Transfer To Department")

	def test_manufacture_defaults_to_reserve(self):
		mr = MockMaterialRequest(material_request_type="Manufacture")
		result = self._run(mr, {})
		self.assertEqual(result, "Transfer to Reserve")

	def tearDown(self):
		return super().tearDown()


class TestValidateTargetItem(IntegrationTestCase):
	"""validate_target_item's bulk sieve-size / dimension lookup.

	Covers the guard itself and the collation tolerance the bulk rewrite has to preserve:
	the per-row frappe.db.get_value it replaced compared in SQL, where utf8mb4_unicode_ci
	forgives case and trailing spaces, and Item Variant Attribute.attribute_value is
	free-text Data with no guarantee of matching an Attribute Value name exactly.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, rows, iva_rows, av_rows):
		"""Call validate_target_item with both bulk reads stubbed.

		Returns the mocked frappe.throw so callers can inspect the message.
		"""

		def _get_all(doctype, filters=None, fields=None, **kwargs):
			if doctype == "Item Variant Attribute":
				return [frappe._dict(d) for d in iva_rows]
			if doctype == "Attribute Value":
				return [frappe._dict(d) for d in av_rows]
			raise AssertionError(f"unexpected get_all on {doctype}")

		mr = SimpleNamespace(items=rows)
		with patch.object(mr_mod.frappe, "get_all", side_effect=_get_all), patch.object(
			mr_mod.frappe, "throw", side_effect=RuntimeError
		) as throw:
			try:
				mr_mod.validate_target_item(mr)
			except RuntimeError:
				pass
		return throw

	@staticmethod
	def _row(item_code, alternative):
		return SimpleNamespace(item_code=item_code, custom_alternative_item=alternative)

	def test_throws_when_height_is_out_of_range(self):
		throw = self._run(
			[self._row("ITEM-A", "ITEM-B")],
			[
				{"parent": "ITEM-A", "attribute_value": "+1-2"},
				{"parent": "ITEM-B", "attribute_value": "+9-10"},
			],
			[
				{"name": "+1-2", "height": 1.0, "weight": 1.0},
				{"name": "+9-10", "height": 9.0, "weight": 1.0},
			],
		)
		self.assertIn("ITEM-A", throw.call_args[0][0])
		self.assertIn("ITEM-B", throw.call_args[0][0])

	def test_no_throw_when_within_range(self):
		throw = self._run(
			[self._row("ITEM-A", "ITEM-B")],
			[
				{"parent": "ITEM-A", "attribute_value": "+1-2"},
				{"parent": "ITEM-B", "attribute_value": "+1-2b"},
			],
			[
				{"name": "+1-2", "height": 1.0, "weight": 1.0},
				{"name": "+1-2b", "height": 1.2, "weight": 1.1},
			],
		)
		throw.assert_not_called()

	def test_trailing_space_attribute_value_still_resolves(self):
		"""Regression: a byte-exact dict lookup would miss here and skip the guard.

		The SQL this replaced matched under a PAD SPACE collation, so '+9-10 ' found the
		Attribute Value named '+9-10' and the size check ran. It must still run.
		"""
		throw = self._run(
			[self._row("ITEM-A", "ITEM-B")],
			[
				{"parent": "ITEM-A", "attribute_value": "+1-2"},
				{"parent": "ITEM-B", "attribute_value": "+9-10 "},
			],
			[
				{"name": "+1-2", "height": 1.0, "weight": 1.0},
				{"name": "+9-10", "height": 9.0, "weight": 1.0},
			],
		)
		self.assertIn("ITEM-A", throw.call_args[0][0])

	def test_case_difference_still_resolves(self):
		"""utf8mb4_unicode_ci is case-insensitive, so 'sieve-a' must find 'SIEVE-A'."""
		throw = self._run(
			[self._row("ITEM-A", "ITEM-B")],
			[
				{"parent": "ITEM-A", "attribute_value": "sieve-small"},
				{"parent": "ITEM-B", "attribute_value": "sieve-big"},
			],
			[
				{"name": "SIEVE-SMALL", "height": 1.0, "weight": 1.0},
				{"name": "SIEVE-BIG", "height": 9.0, "weight": 1.0},
			],
		)
		self.assertIn("ITEM-A", throw.call_args[0][0])

	def test_exact_key_wins_over_normalised_one(self):
		"""Two Attribute Values differing only by case each resolve to themselves."""
		throw = self._run(
			[self._row("ITEM-A", "ITEM-B")],
			[
				{"parent": "ITEM-A", "attribute_value": "sz"},
				{"parent": "ITEM-B", "attribute_value": "SZ"},
			],
			[
				{"name": "sz", "height": 1.0, "weight": 1.0},
				{"name": "SZ", "height": 1.1, "weight": 1.0},
			],
		)
		throw.assert_not_called()

	def test_pooling_does_not_cross_contaminate(self):
		"""item_code and custom_alternative_item share one query but stay distinct."""
		throw = self._run(
			[self._row("ITEM-A", "ITEM-B"), self._row("ITEM-C", "ITEM-D")],
			[
				{"parent": "ITEM-A", "attribute_value": "+1-2"},
				{"parent": "ITEM-B", "attribute_value": "+1-2"},
				{"parent": "ITEM-C", "attribute_value": "+1-2"},
				{"parent": "ITEM-D", "attribute_value": "+9-10"},
			],
			[
				{"name": "+1-2", "height": 1.0, "weight": 1.0},
				{"name": "+9-10", "height": 9.0, "weight": 1.0},
			],
		)
		# Only the ITEM-C / ITEM-D pair is out of range.
		self.assertIn("ITEM-C", throw.call_args[0][0])
		self.assertIn("ITEM-D", throw.call_args[0][0])

	def test_row_without_sieve_attribute_is_skipped(self):
		throw = self._run(
			[self._row("ITEM-A", "ITEM-B")],
			[{"parent": "ITEM-B", "attribute_value": "+9-10"}],
			[{"name": "+9-10", "height": 9.0, "weight": 1.0}],
		)
		throw.assert_not_called()

	def test_returns_before_querying_when_no_alternative_items(self):
		mr = SimpleNamespace(items=[self._row("ITEM-A", None)])
		with patch.object(mr_mod.frappe, "get_all") as get_all:
			mr_mod.validate_target_item(mr)
		get_all.assert_not_called()

	def tearDown(self):
		return super().tearDown()


class TestMriWarehouseMap(IntegrationTestCase):
	"""The shared Material Request Item -> warehouse prefetch."""

	@classmethod
	def setUpClass(cls):
		pass

	@staticmethod
	def _se_rows(*names):
		return [SimpleNamespace(material_request_item=n) for n in names]

	def test_resolves_from_loaded_mr_without_querying(self):
		mr = SimpleNamespace(
			items=[
				SimpleNamespace(name="MRI-1", warehouse="WH-1"),
				SimpleNamespace(name="MRI-2", warehouse="WH-2"),
			]
		)
		mr.get = lambda key, default=None: mr.items if key == "items" else default

		with patch.object(prefetch_mod.frappe, "get_all") as get_all:
			result = prefetch_mod.mri_warehouse_map(self._se_rows("MRI-1", "MRI-2"), mr)

		self.assertEqual(result, {"MRI-1": "WH-1", "MRI-2": "WH-2"})
		get_all.assert_not_called()

	def test_queries_only_the_names_the_document_does_not_cover(self):
		mr = SimpleNamespace(items=[SimpleNamespace(name="MRI-1", warehouse="WH-1")])
		mr.get = lambda key, default=None: mr.items if key == "items" else default

		with patch.object(
			prefetch_mod.frappe,
			"get_all",
			return_value=[frappe._dict(name="MRI-9", warehouse="WH-9")],
		) as get_all:
			result = prefetch_mod.mri_warehouse_map(self._se_rows("MRI-1", "MRI-9"), mr)

		self.assertEqual(result, {"MRI-1": "WH-1", "MRI-9": "WH-9"})
		# Only the uncovered name went to the database.
		self.assertEqual(get_all.call_args[1]["filters"], {"name": ("in", ["MRI-9"])})

	def test_queries_everything_when_no_document_is_passed(self):
		with patch.object(
			prefetch_mod.frappe,
			"get_all",
			return_value=[frappe._dict(name="MRI-1", warehouse="WH-1")],
		) as get_all:
			result = prefetch_mod.mri_warehouse_map(self._se_rows("MRI-1"))

		self.assertEqual(result, {"MRI-1": "WH-1"})
		get_all.assert_called_once()

	def test_no_rows_means_no_query(self):
		with patch.object(prefetch_mod.frappe, "get_all") as get_all:
			self.assertEqual(
				prefetch_mod.mri_warehouse_map(self._se_rows(None, None)), {}
			)
		get_all.assert_not_called()

	def tearDown(self):
		return super().tearDown()


class TestCreateStockEntryReserveMemo(IntegrationTestCase):
	"""create_stock_entry resolves each department's Reserve warehouse once."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, from_warehouses, department_of):
		calls = []

		def _gv(doctype, filters, fieldname, *a, **kw):
			calls.append((doctype, fieldname))
			if doctype == "Transfer Type":
				return "SE Type"
			if doctype == "Warehouse" and fieldname == "department":
				return department_of[filters]
			if doctype == "Warehouse" and fieldname == "name":
				return f"RESERVE-{filters['department']}"
			raise AssertionError(f"unexpected get_value {doctype}.{fieldname}")

		mr = SimpleNamespace(
			name="MR-1",
			company="KGJPL",
			workflow_state="Material Reserved",
			custom_reserve_se=None,
			manufacturing_order="PMO-1",
			material_request_type="Material Transfer",
			custom_transfer_type="Transfer to Reserve",
			items=[
				SimpleNamespace(
					name=f"MRI-{i}",
					from_warehouse=wh,
					item_code="ITEM-1",
					custom_alternative_item=None,
					qty=1,
					inventory_type="Regular Stock",
					customer=None,
					batch_no=None,
					pcs=1,
					cost_center=None,
					custom_sub_setting_type=None,
				)
				for i, wh in enumerate(from_warehouses)
			],
		)

		with patch.object(mr_mod.frappe.db, "get_value", side_effect=_gv), patch.object(
			mr_mod.frappe, "new_doc", return_value=MagicMock()
		), patch.object(mr_mod.frappe, "msgprint"):
			mr_mod.create_stock_entry(mr, None)

		return calls

	def test_one_department_lookup_per_warehouse_one_reserve_per_department(self):
		"""Three source warehouses in one department: 3 department reads, 1 reserve read."""
		calls = self._run(
			["CAST-RM", "CAST-WIP", "CAST-SCRAP"],
			{"CAST-RM": "Casting", "CAST-WIP": "Casting", "CAST-SCRAP": "Casting"},
		)
		self.assertEqual(calls.count(("Warehouse", "department")), 3)
		self.assertEqual(calls.count(("Warehouse", "name")), 1)

	def test_repeated_warehouse_is_resolved_once(self):
		calls = self._run(["CAST-RM"] * 5, {"CAST-RM": "Casting"})
		self.assertEqual(calls.count(("Warehouse", "department")), 1)
		self.assertEqual(calls.count(("Warehouse", "name")), 1)

	def test_distinct_departments_each_resolve(self):
		calls = self._run(
			["CAST-RM", "SET-RM"], {"CAST-RM": "Casting", "SET-RM": "Setting"}
		)
		self.assertEqual(calls.count(("Warehouse", "department")), 2)
		self.assertEqual(calls.count(("Warehouse", "name")), 2)

	def test_missing_reserve_warehouse_still_throws(self):
		def _gv(doctype, filters, fieldname, *a, **kw):
			if doctype == "Transfer Type":
				return "SE Type"
			if fieldname == "department":
				return "Casting"
			return None  # no Reserve warehouse

		mr = SimpleNamespace(
			name="MR-1",
			company="KGJPL",
			workflow_state="Material Reserved",
			custom_reserve_se=None,
			manufacturing_order="PMO-1",
			material_request_type="Material Transfer",
			custom_transfer_type="Transfer to Reserve",
			items=[SimpleNamespace(from_warehouse="CAST-RM")],
		)

		with patch.object(mr_mod.frappe.db, "get_value", side_effect=_gv), patch.object(
			mr_mod.frappe, "new_doc", return_value=MagicMock()
		), patch.object(mr_mod.frappe, "throw", side_effect=RuntimeError) as throw:
			with self.assertRaises(RuntimeError):
				mr_mod.create_stock_entry(mr, None)

		self.assertIn("Casting", throw.call_args[0][0])

	def tearDown(self):
		return super().tearDown()


class TestPrefetchPurityPercentages(IntegrationTestCase):
	"""The bulk purity prefetch must warm get_purity_percentage's own request cache.

	That cache is what keeps the Stock Entry saved later in the same request from
	re-running the Metal Purity join once per row; a prefetch that bypassed it would
	move those queries rather than remove them.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		self._saved_cache = getattr(frappe.local, "request_cache", None)
		frappe.local.request_cache = defaultdict(dict)

	def test_prefetch_warms_the_cache_used_by_get_purity_percentage(self):
		rows = [("ITEM-A", 75.0), ("ITEM-B", 91.6)]
		with patch.object(
			metal_utils, "_fetch_purity_percentages", return_value=rows
		) as fetch:
			metal_utils.prefetch_purity_percentages(["ITEM-A", "ITEM-B"])
			self.assertEqual(fetch.call_count, 1)

			# Every later lookup is served from the cache -- no second query.
			self.assertEqual(metal_utils.get_purity_percentage("ITEM-A"), 75.0)
			self.assertEqual(metal_utils.get_purity_percentage("ITEM-B"), 91.6)
			self.assertEqual(fetch.call_count, 1)

	def test_items_without_a_purity_row_are_primed_as_none(self):
		"""Otherwise each miss goes back to the database for the same answer."""
		with patch.object(
			metal_utils, "_fetch_purity_percentages", return_value=[("ITEM-A", 75.0)]
		) as fetch:
			metal_utils.prefetch_purity_percentages(["ITEM-A", "ITEM-MISSING"])
			self.assertIsNone(metal_utils.get_purity_percentage("ITEM-MISSING"))
			self.assertEqual(fetch.call_count, 1)

	def test_first_row_wins_on_a_duplicate_attribute_row(self):
		rows = [("ITEM-A", 75.0), ("ITEM-A", 91.6)]
		with patch.object(metal_utils, "_fetch_purity_percentages", return_value=rows):
			metal_utils.prefetch_purity_percentages(["ITEM-A"])
		self.assertEqual(metal_utils.get_purity_percentage("ITEM-A"), 75.0)

	def test_prefetch_never_clobbers_a_value_already_resolved(self):
		with patch.object(
			metal_utils, "_fetch_purity_percentages", return_value=[("ITEM-A", 75.0)]
		):
			self.assertEqual(metal_utils.get_purity_percentage("ITEM-A"), 75.0)

		with patch.object(
			metal_utils, "_fetch_purity_percentages", return_value=[("ITEM-A", 99.9)]
		):
			metal_utils.prefetch_purity_percentages(["ITEM-A"])

		self.assertEqual(metal_utils.get_purity_percentage("ITEM-A"), 75.0)

	def test_empty_and_falsy_items_do_not_query(self):
		with patch.object(metal_utils, "_fetch_purity_percentages") as fetch:
			metal_utils.prefetch_purity_percentages([])
			metal_utils.prefetch_purity_percentages([None, ""])
		fetch.assert_not_called()

	def tearDown(self):
		if self._saved_cache is None:
			frappe.local.request_cache = defaultdict(dict)
		else:
			frappe.local.request_cache = self._saved_cache
		return super().tearDown()
