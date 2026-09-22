from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.utils.diamond_conversion_batches import (
	get_diamond_conversion_target_batches,
)
from jewellery_erpnext.jewellery_erpnext.doctype.diamond_conversion.diamond_conversion import (
	SIEVE_TO_SIEVE,
	_parse_sieve_bounds,
	validate_purity,
	validate_sieve_size_band,
	validate_source_batches,
)

# Every test below patches ``frappe.db.get_all`` rather than ``frappe.get_all``.
# ``frappe.db.get_all`` is a one-line staticmethod delegating to ``frappe.get_all``, and frappe
# internals (Meta.apply_customization's Custom Field / Property Setter reads, and so on) call
# ``frappe.get_all`` DIRECTLY -- so patching the former is invisible to them, while patching the
# latter would hand frappe's own machinery an empty list. That is the same meta-hijack that makes
# blanket-patching ``frappe.db.get_value`` unsafe. The production code under test therefore also
# calls ``frappe.db.get_all``, and every side_effect here falls through to the real
# ``frappe.get_all`` for a doctype it does not recognise rather than returning [].


def _row(**kwargs):
	row = MagicMock()
	for key, value in kwargs.items():
		setattr(row, key, value)
	return row


def _attribute_value(name, height=0.0, weight=0.0, is_range=1):
	return frappe._dict(
		{
			"name": name,
			"height": height,
			"weight": weight,
			"is_diamond_sieve_size_range": is_range,
		}
	)


class DiamondConversionUnitTestCase(IntegrationTestCase):
	"""Base for the fully-mocked cases below -- no fixtures, so no test-record generation.

	``IntegrationTestCase.setUpClass`` infers the doctype from the module path and calls
	``make_test_records("Diamond Conversion")``, which walks every link field and tries to build
	records for Employee, Company, Warehouse, Batch and the rest. On CI that import chain reaches
	``erpnext.setup.doctype.employee.test_employee`` -> ``erpnext.tests.utils``, which runs
	``BootStrapTestData()`` at module scope and blows up before any of our assertions run.

	These cases mock every query they make, so they need none of those records. Stubbing
	``setUpClass`` is the same thing ``TestDiamondConversion`` below already does for the
	``validate_purity`` cases.
	"""

	@classmethod
	def setUpClass(cls):
		pass


class TestDiamondConversionSieveBand(DiamondConversionUnitTestCase):
	"""``validate_sieve_size_band`` -- the "Sieve Size to Sieve Size" band containment rule."""

	def _doc(self, sources, targets, conversion_type=SIEVE_TO_SIEVE):
		doc = MagicMock()
		doc.conversion_type = conversion_type
		doc.sc_source_table = sources
		doc.sc_target_table = targets
		return doc

	def _side_effect(self, item_attributes, attribute_values):
		"""item_attributes: {item_code: sieve value}. attribute_values: [frappe._dict, ...]."""

		def get_all(doctype, *args, **kwargs):
			if doctype == "Item Variant Attribute":
				return [
					frappe._dict({"parent": item, "attribute_value": value})
					for item, value in item_attributes.items()
				]
			if doctype == "Attribute Value":
				return list(attribute_values)
			return frappe.get_all(doctype, *args, **kwargs)

		return get_all

	# -- the happy path ------------------------------------------------------------------

	@patch("frappe.db.get_all")
	def test_target_inside_source_band_passes(self, mock_get_all):
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC-+14-16")],
			[_row(idx=1, item_code="D-TGT-+14-16")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-SRC-+14-16": "+14-16", "D-TGT-+14-16": "+14-16"},
			[_attribute_value("+14-16", height=17.5, weight=13.0)],
		)

		validate_sieve_size_band(doc)

	@patch("frappe.db.get_all")
	def test_zero_down_is_a_real_bound_not_unset(self, mock_get_all):
		"""``+0-2``'s natural DOWN is 0; only BOTH endpoints at zero means "never filled in"."""
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC-+0-2")],
			[_row(idx=1, item_code="D-TGT-+0-2")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-SRC-+0-2": "+0-2", "D-TGT-+0-2": "+0-2"},
			[_attribute_value("+0-2", height=2.5, weight=0.0)],
		)

		validate_sieve_size_band(doc)

	@patch("frappe.db.get_all")
	def test_trailing_space_attribute_value_still_resolves(self, mock_get_all):
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC")],
			[_row(idx=1, item_code="D-TGT")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-SRC": "+14-16 ", "D-TGT": "+14-16"},
			[_attribute_value("+14-16", height=17.5, weight=13.0)],
		)

		validate_sieve_size_band(doc)

	@patch("frappe.db.get_all")
	def test_other_conversion_type_is_a_no_op(self, mock_get_all):
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC")],
			[_row(idx=1, item_code="D-TGT")],
			conversion_type="Sieve Size Range to Sieve Size",
		)

		validate_sieve_size_band(doc)

		mock_get_all.assert_not_called()

	@patch("frappe.db.get_all")
	def test_blank_conversion_type_is_a_no_op(self, mock_get_all):
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC")], [_row(idx=1, item_code="D-TGT")], ""
		)

		validate_sieve_size_band(doc)

		mock_get_all.assert_not_called()

	# -- band rejections -----------------------------------------------------------------

	@patch("frappe.db.get_all")
	def test_target_below_source_band_throws(self, mock_get_all):
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC-+14-16")],
			[_row(idx=1, item_code="D-TGT-+11-14")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-SRC-+14-16": "+14-16", "D-TGT-+11-14": "+11-14"},
			[
				_attribute_value("+14-16", height=17.5, weight=13.0),
				_attribute_value("+11-14", height=14.5, weight=10.5),
			],
		)

		self.assertRaises(frappe.ValidationError, validate_sieve_size_band, doc)

	@patch("frappe.db.get_all")
	def test_target_above_source_band_throws(self, mock_get_all):
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC-+4-6.5")],
			[_row(idx=1, item_code="D-TGT-+6.5-8")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-SRC-+4-6.5": "+4-6.5", "D-TGT-+6.5-8": "+6.5-8"},
			[
				_attribute_value("+4-6.5", height=7.0, weight=3.5),
				_attribute_value("+6.5-8", height=8.5, weight=6.0),
			],
		)

		self.assertRaises(frappe.ValidationError, validate_sieve_size_band, doc)

	@patch("frappe.db.get_all")
	def test_target_bounds_fit_but_its_own_up_down_do_not(self, mock_get_all):
		"""``+A-B`` sits inside the band while the target's own UP overshoots it."""
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC")],
			[_row(idx=1, item_code="D-TGT")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-SRC": "+14-16", "D-TGT": "+15-16"},
			[
				_attribute_value("+14-16", height=17.5, weight=13.0),
				_attribute_value("+15-16", height=19.0, weight=14.5),
			],
		)

		self.assertRaises(frappe.ValidationError, validate_sieve_size_band, doc)

	@patch("frappe.db.get_all")
	def test_target_must_fit_every_distinct_source_band(self, mock_get_all):
		"""Intersection, not union: a Repack melts every source into every target."""
		doc = self._doc(
			[
				_row(idx=1, item_code="D-SRC-WIDE"),
				_row(idx=2, item_code="D-SRC-NARROW"),
			],
			[_row(idx=1, item_code="D-TGT")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-SRC-WIDE": "+11-14", "D-SRC-NARROW": "+13-14", "D-TGT": "+11.5-12"},
			[
				_attribute_value("+11-14", height=14.5, weight=10.5),
				_attribute_value("+13-14", height=14.5, weight=12.5),
				_attribute_value("+11.5-12", height=12.5, weight=11.0),
			],
		)

		self.assertRaises(frappe.ValidationError, validate_sieve_size_band, doc)

	# -- unusable master data ------------------------------------------------------------

	@patch("frappe.db.get_all")
	def test_source_up_down_unset_throws(self, mock_get_all):
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC")],
			[_row(idx=1, item_code="D-TGT")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-SRC": "+6.5-8", "D-TGT": "+4-6.5"},
			[
				_attribute_value("+6.5-8"),
				_attribute_value("+4-6.5", height=7.0, weight=3.5),
			],
		)

		with self.assertRaises(frappe.ValidationError) as caught:
			validate_sieve_size_band(doc)
		self.assertIn("UP/DOWN", str(caught.exception))

	@patch("frappe.db.get_all")
	def test_target_up_down_unset_throws(self, mock_get_all):
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC")],
			[_row(idx=1, item_code="D-TGT")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-SRC": "+14-16", "D-TGT": "+15-15.5"},
			[
				_attribute_value("+14-16", height=17.5, weight=13.0),
				_attribute_value("+15-15.5"),
			],
		)

		self.assertRaises(frappe.ValidationError, validate_sieve_size_band, doc)

	@patch("frappe.db.get_all")
	def test_missing_attribute_value_record_says_it_does_not_exist(self, mock_get_all):
		"""Distinct from the "fill UP/DOWN" message -- there is no record to open."""
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC")],
			[_row(idx=1, item_code="D-TGT")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-SRC": "+14-16", "D-TGT": "+15-15.5"}, []
		)

		with self.assertRaises(frappe.ValidationError) as caught:
			validate_sieve_size_band(doc)
		self.assertIn("does not exist", str(caught.exception))

	@patch("frappe.db.get_all")
	def test_value_not_marked_as_sieve_size_range_throws(self, mock_get_all):
		"""A plain sieve-size value parses as +A-B and has real UP/DOWN, so it must be caught."""
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC")],
			[_row(idx=1, item_code="D-TGT")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-SRC": "+14-16", "D-TGT": "+6-6.5"},
			[
				_attribute_value("+14-16", height=17.5, weight=13.0),
				_attribute_value("+6-6.5", height=7.0, weight=5.5, is_range=0),
			],
		)

		self.assertRaises(frappe.ValidationError, validate_sieve_size_band, doc)

	@patch("frappe.db.get_all")
	def test_inverted_band_throws(self, mock_get_all):
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC")],
			[_row(idx=1, item_code="D-TGT")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-SRC": "+14-16", "D-TGT": "+14-16"},
			[_attribute_value("+14-16", height=13.0, weight=17.5)],
		)

		self.assertRaises(frappe.ValidationError, validate_sieve_size_band, doc)

	@patch("frappe.db.get_all")
	def test_item_without_the_sieve_range_attribute_throws(self, mock_get_all):
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC")],
			[_row(idx=1, item_code="D-TGT")],
		)
		mock_get_all.side_effect = self._side_effect(
			{"D-TGT": "+14-16"},
			[_attribute_value("+14-16", height=17.5, weight=13.0)],
		)

		self.assertRaises(frappe.ValidationError, validate_sieve_size_band, doc)

	@patch("frappe.db.get_all")
	def test_malformed_target_value_throws_rather_than_raising_valueerror(
		self, mock_get_all
	):
		for malformed in ("6.5-8", "abc", "+1-2-3", "+-2", ""):
			with self.subTest(value=malformed):
				doc = self._doc(
					[_row(idx=1, item_code="D-SRC")],
					[_row(idx=1, item_code="D-TGT")],
				)
				mock_get_all.side_effect = self._side_effect(
					{"D-SRC": "+0-20", "D-TGT": malformed},
					[
						_attribute_value("+0-20", height=20.0, weight=0.0),
						_attribute_value(malformed, height=5.0, weight=1.0),
					],
				)

				self.assertRaises(frappe.ValidationError, validate_sieve_size_band, doc)

	# -- the parser on its own -----------------------------------------------------------

	def test_parse_sieve_bounds(self):
		self.assertEqual(_parse_sieve_bounds("+13-17.5"), (13.0, 17.5))
		self.assertEqual(_parse_sieve_bounds("+6.5-8"), (6.5, 8.0))
		self.assertEqual(_parse_sieve_bounds(" +0-2 "), (0.0, 2.0))
		for malformed in ("6.5-8", "abc", "+1-2-3", "+-2", "+2-", "", None):
			with self.subTest(value=malformed):
				self.assertIsNone(_parse_sieve_bounds(malformed))


class TestDiamondConversionSourceBatches(DiamondConversionUnitTestCase):
	"""``validate_source_batches`` -- conversion output may not be conversion input."""

	def _doc(self, rows, conversion_type=SIEVE_TO_SIEVE):
		doc = MagicMock()
		doc.conversion_type = conversion_type
		doc.sc_source_table = rows
		return doc

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.diamond_conversion.diamond_conversion.get_diamond_conversion_target_batches"
	)
	def test_conversion_produced_batch_throws(self, mock_provenance):
		doc = self._doc([_row(idx=1, item_code="D-SRC", batch="BATCH-A")])
		mock_provenance.return_value = {"BATCH-A": "DCON00086"}

		with self.assertRaises(frappe.ValidationError) as caught:
			validate_source_batches(doc)
		self.assertIn("DCON00086", str(caught.exception))

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.diamond_conversion.diamond_conversion.get_diamond_conversion_target_batches"
	)
	def test_unrelated_batch_passes(self, mock_provenance):
		doc = self._doc([_row(idx=1, item_code="D-SRC", batch="BATCH-PR")])
		mock_provenance.return_value = {}

		validate_source_batches(doc)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.diamond_conversion.diamond_conversion.get_diamond_conversion_target_batches"
	)
	def test_row_without_a_batch_throws(self, mock_provenance):
		"""update_batch_details leaves a batch-less row behind when every candidate was barred."""
		doc = self._doc([_row(idx=1, item_code="D-SRC", batch=None)])

		self.assertRaises(frappe.ValidationError, validate_source_batches, doc)
		mock_provenance.assert_not_called()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.diamond_conversion.diamond_conversion.get_diamond_conversion_target_batches"
	)
	def test_other_conversion_type_is_a_no_op(self, mock_provenance):
		doc = self._doc(
			[_row(idx=1, item_code="D-SRC", batch="BATCH-A")],
			conversion_type="Sieve Size Range to Sieve Size",
		)

		validate_source_batches(doc)

		mock_provenance.assert_not_called()


class TestDiamondConversionBatchProvenance(DiamondConversionUnitTestCase):
	"""``get_diamond_conversion_target_batches`` -- the two-hop Batch -> SE -> conversion walk."""

	def _side_effect(self, batches, details, stock_entries):
		def get_all(doctype, *args, **kwargs):
			if doctype == "Batch":
				return [frappe._dict(row) for row in batches]
			if doctype == "Stock Entry Detail":
				return [frappe._dict(row) for row in details]
			if doctype == "Stock Entry":
				return [frappe._dict(row) for row in stock_entries]
			return frappe.get_all(doctype, *args, **kwargs)

		return get_all

	@patch("frappe.db.get_all")
	def test_empty_input_does_not_query(self, mock_get_all):
		self.assertEqual(get_diamond_conversion_target_batches([]), {})
		self.assertEqual(get_diamond_conversion_target_batches([None, ""]), {})

		mock_get_all.assert_not_called()

	@patch("frappe.db.get_all")
	def test_resolves_via_reference_name(self, mock_get_all):
		mock_get_all.side_effect = self._side_effect(
			[
				{
					"name": "BATCH-A",
					"reference_doctype": "Stock Entry",
					"reference_name": "STE-1",
					"custom_voucher_detail_no": None,
				}
			],
			[],
			[{"name": "STE-1", "custom_diamond_conversion": "DCON00086"}],
		)

		self.assertEqual(
			get_diamond_conversion_target_batches(["BATCH-A"]), {"BATCH-A": "DCON00086"}
		)

	@patch("frappe.db.get_all")
	def test_resolves_via_voucher_detail_no_when_reference_name_was_nulled(
		self, mock_get_all
	):
		"""ERPNext NULLs reference_name on Stock Entry cancellation; the detail row survives."""
		mock_get_all.side_effect = self._side_effect(
			[
				{
					"name": "BATCH-A",
					"reference_doctype": None,
					"reference_name": None,
					"custom_voucher_detail_no": "SED-1",
				}
			],
			[{"name": "SED-1", "parent": "STE-1"}],
			[{"name": "STE-1", "custom_diamond_conversion": "DCON00086"}],
		)

		self.assertEqual(
			get_diamond_conversion_target_batches(["BATCH-A"]), {"BATCH-A": "DCON00086"}
		)

	@patch("frappe.db.get_all")
	def test_batch_from_an_unrelated_stock_entry_is_not_barred(self, mock_get_all):
		mock_get_all.side_effect = self._side_effect(
			[
				{
					"name": "BATCH-A",
					"reference_doctype": "Purchase Receipt",
					"reference_name": "PR-1",
					"custom_voucher_detail_no": None,
				}
			],
			[],
			[],
		)

		self.assertEqual(get_diamond_conversion_target_batches(["BATCH-A"]), {})


class TestDiamondConversion(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch("frappe.db.get_all")
	def test_purity_validation_success_same_purity(self, mock_get_all):
		doc = MagicMock()
		doc.manufacturer = "Test Manufacturer"

		source_row = MagicMock()
		source_row.item_code = "ITEM-VVS"
		doc.sc_source_table = [source_row]

		target_row = MagicMock()
		target_row.item_code = "ITEM-VVS"
		doc.sc_target_table = [target_row]

		def get_all_side_effect(doctype, *args, **kwargs):
			if doctype == "Diamond Conversion Purity":
				return []
			if doctype == "Item Variant Attribute":
				return [frappe._dict({"parent": "ITEM-VVS", "attribute_value": "VVS"})]
			return []

		mock_get_all.side_effect = get_all_side_effect

		# Should pass without throwing an error
		validate_purity(doc)

	@patch("frappe.db.get_all")
	def test_purity_validation_success_allowed_mapping(self, mock_get_all):
		doc = MagicMock()
		doc.manufacturer = "Test Manufacturer"

		source_row = MagicMock()
		source_row.item_code = "ITEM-VVS"
		doc.sc_source_table = [source_row]

		target_row = MagicMock()
		target_row.item_code = "ITEM-VS"
		doc.sc_target_table = [target_row]

		def get_all_side_effect(doctype, *args, **kwargs):
			if doctype == "Diamond Conversion Purity":
				return [frappe._dict({"from_purity": "VVS", "to_purity": "VS"})]
			if doctype == "Item Variant Attribute":
				return [
					frappe._dict({"parent": "ITEM-VVS", "attribute_value": "VVS"}),
					frappe._dict({"parent": "ITEM-VS", "attribute_value": "VS"}),
				]
			return []

		mock_get_all.side_effect = get_all_side_effect

		# Should pass without throwing an error
		validate_purity(doc)

	@patch("frappe.db.get_all")
	def test_purity_validation_failure_not_allowed(self, mock_get_all):
		doc = MagicMock()
		doc.manufacturer = "Test Manufacturer"

		source_row = MagicMock()
		source_row.item_code = "ITEM-VVS"
		doc.sc_source_table = [source_row]

		target_row = MagicMock()
		target_row.item_code = "ITEM-SI"
		doc.sc_target_table = [target_row]

		def get_all_side_effect(doctype, *args, **kwargs):
			if doctype == "Diamond Conversion Purity":
				return [frappe._dict({"from_purity": "VVS", "to_purity": "VS"})]
			if doctype == "Item Variant Attribute":
				return [
					frappe._dict({"parent": "ITEM-VVS", "attribute_value": "VVS"}),
					frappe._dict({"parent": "ITEM-SI", "attribute_value": "SI"}),
				]
			return []

		mock_get_all.side_effect = get_all_side_effect

		self.assertRaises(frappe.ValidationError, validate_purity, doc)

	@patch("frappe.db.get_all")
	def test_purity_validation_mixed_batch(self, mock_get_all):
		doc = MagicMock()
		doc.manufacturer = "Test Manufacturer"

		src1, src2 = MagicMock(), MagicMock()
		src1.item_code, src2.item_code = "ITEM-VVS", "ITEM-VS"
		doc.sc_source_table = [src1, src2]

		tgt1, tgt2 = MagicMock(), MagicMock()
		tgt1.item_code, tgt2.item_code = "ITEM-VVS", "ITEM-SI"
		doc.sc_target_table = [tgt1, tgt2]

		def get_all_side_effect(doctype, *args, **kwargs):
			if doctype == "Diamond Conversion Purity":
				return [frappe._dict({"from_purity": "VS", "to_purity": "SI"})]
			if doctype == "Item Variant Attribute":
				return [
					frappe._dict({"parent": "ITEM-VVS", "attribute_value": "VVS"}),
					frappe._dict({"parent": "ITEM-VS", "attribute_value": "VS"}),
					frappe._dict({"parent": "ITEM-SI", "attribute_value": "SI"}),
				]
			return []

		mock_get_all.side_effect = get_all_side_effect

		# Should pass without throwing an error
		validate_purity(doc)

	@patch("frappe.db.get_all")
	def test_purity_validation_missing_grade(self, mock_get_all):
		doc = MagicMock()
		doc.manufacturer = "Test Manufacturer"

		source_row = MagicMock()
		source_row.idx = 1
		source_row.item_code = "ITEM-NOGRADE"
		doc.sc_source_table = [source_row]
		doc.sc_target_table = []

		def get_all_side_effect(doctype, *args, **kwargs):
			if doctype == "Diamond Conversion Purity":
				return []
			if doctype == "Item Variant Attribute":
				return []  # Return empty for NOGRADE
			return []

		mock_get_all.side_effect = get_all_side_effect

		# Should raise validation error
		self.assertRaises(frappe.ValidationError, validate_purity, doc)

	def tearDown(self):
		return super().tearDown()
