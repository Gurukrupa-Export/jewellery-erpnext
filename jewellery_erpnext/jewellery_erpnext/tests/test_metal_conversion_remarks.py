# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the Metal Conversions percentage-driven Remarks dropdown.

Pure-logic tests: no DB / no Frappe site.

The feature: ``remarks`` is a Select whose option TEXT carries the document's
``percentage`` -- "NR 1.800% PLAIN ROUND BALLS LOSS BOOK". A Select's option list is
SCHEMA (stored once on the DocType, identical for every document), so a per-document
number cannot live in it and the option space is unbounded besides. The dropdown is
therefore rendered per document from ``render_remark_options``, the DocType JSON ships
the field with NO ``options`` (which makes frappe skip ``_validate_selects``), and
``MetalConversions.set_remarks`` is the replacement guard.

Two properties carry the design and are what these tests pin:

* ``render_remark_options`` is the ONLY renderer -- the client fills the dropdown from
  it and ``set_remarks`` re-renders the stored value from it, so the text the user
  picked and the text we store cannot drift.
* ``template_index`` matches on the fixed words only, so a remark rendered at one
  percentage is still recognised after the percentage is edited. That is what lets
  ``set_remarks`` RE-RENDER a stale remark instead of rejecting it.
"""

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions import (
	metal_conversions,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions import (
	REMARK_TEMPLATES,
	MetalConversions,
	render_remark_options,
	template_index,
)

_MODULE = (
	"jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions"
)
_DOCTYPE_JSON = os.path.join(
	os.path.dirname(metal_conversions.__file__), "metal_conversions.json"
)


def _doc(percentage=None, remarks=None, precision=3):
	"""A stand-in Metal Conversions carrying only what set_remarks touches."""
	doc = SimpleNamespace(
		percentage=percentage,
		remarks=remarks,
		precision=lambda fieldname: precision,
	)
	doc.set_remarks = MetalConversions.set_remarks.__get__(doc, SimpleNamespace)
	return doc


class TestRemarksFieldConfiguration(IntegrationTestCase):
	"""The two DocType JSON facts the feature rests on.

	Both read as tidy-up to anyone who does not know why they are there, and both are
	silently undone by a Customize Form export, so they are pinned here.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		with open(_DOCTYPE_JSON) as handle:
			self.fields = {df["fieldname"]: df for df in json.load(handle)["fields"]}

	def test_remarks_ships_no_options(self):
		"""Load-bearing: a falsy df.options is what makes frappe skip _validate_selects.

		Put any options back and every save of a rendered remark throws
		'Remarks cannot be "NR 1.80% ...". It should be one of "..."'.
		"""
		self.assertNotIn("options", self.fields["remarks"])

	def test_percentage_precision_pinned_to_two(self):
		"""Loss-book percentages are written to 2 decimals.

		Without this the sentence reads "NR 1.800%", because the site runs System
		Settings float_precision 3 for gram/carat weights (ensure_float_precision_three).
		"""
		self.assertEqual(self.fields["percentage"].get("precision"), "2")


class TestRenderRemarkOptions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_percentage_is_substituted_at_the_given_precision(self):
		self.assertEqual(
			render_remark_options(1.8, 2), ["NR 1.80% PLAIN ROUND BALLS LOSS BOOK"]
		)
		self.assertEqual(
			render_remark_options(1.8, 3), ["NR 1.800% PLAIN ROUND BALLS LOSS BOOK"]
		)

	def test_precision_comes_from_the_field_not_the_typed_digits(self):
		"""Trailing zeros are padded, extra digits rounded -- the field decides."""
		self.assertEqual(
			render_remark_options(1.8, 4), ["NR 1.8000% PLAIN ROUND BALLS LOSS BOOK"]
		)
		self.assertEqual(
			render_remark_options(1.8055, 2), ["NR 1.81% PLAIN ROUND BALLS LOSS BOOK"]
		)

	def test_blank_percentage_renders_zero_rather_than_raising(self):
		"""The dropdown is built on every refresh, including before anything is typed."""
		self.assertEqual(
			render_remark_options(None, 2), ["NR 0.00% PLAIN ROUND BALLS LOSS BOOK"]
		)
		self.assertEqual(
			render_remark_options("", 2), ["NR 0.00% PLAIN ROUND BALLS LOSS BOOK"]
		)

	def test_one_entry_rendered_per_template(self):
		"""Adding a sentence to REMARK_TEMPLATES must be the only change required."""
		self.assertEqual(len(render_remark_options(1.8, 2)), len(REMARK_TEMPLATES))


class TestTemplateIndex(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_rendered_option_round_trips_to_its_template(self):
		for precision in (2, 3, 4):
			rendered = render_remark_options(1.8, precision)[0]
			self.assertEqual(template_index(rendered), 0, rendered)

	def test_remark_rendered_at_a_different_percentage_is_still_recognised(self):
		"""The whole point: a stale remark is re-rendered, not rejected."""
		self.assertEqual(template_index("NR 1.80% PLAIN ROUND BALLS LOSS BOOK"), 0)
		self.assertEqual(template_index("NR 99.999% PLAIN ROUND BALLS LOSS BOOK"), 0)

	def test_non_template_text_is_rejected(self):
		for value in (
			"junk",
			"",
			None,
			"NR 1.80% PLAIN ROUND BALLS LOSS BOOKS",  # trailing S
			"nr 1.80% plain round balls loss book",  # wrong case
			"NR % PLAIN ROUND BALLS LOSS BOOK",  # no number at all
			"XX NR 1.80% PLAIN ROUND BALLS LOSS BOOK",  # prefixed
		):
			self.assertIsNone(template_index(value), value)


class TestSetRemarks(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_blank_remark_is_left_alone(self):
		doc = _doc(percentage=1.8, remarks=None)
		doc.set_remarks()
		self.assertIsNone(doc.remarks)

	def test_stale_percentage_is_re_rendered_on_validate(self):
		"""Pick at 1.80, edit Percentage to 2.5 -- the stored sentence must follow."""
		doc = _doc(
			percentage=2.5, remarks="NR 1.800% PLAIN ROUND BALLS LOSS BOOK", precision=3
		)
		doc.set_remarks()
		self.assertEqual(doc.remarks, "NR 2.500% PLAIN ROUND BALLS LOSS BOOK")

	def test_already_current_remark_is_unchanged(self):
		doc = _doc(
			percentage=1.8, remarks="NR 1.800% PLAIN ROUND BALLS LOSS BOOK", precision=3
		)
		doc.set_remarks()
		self.assertEqual(doc.remarks, "NR 1.800% PLAIN ROUND BALLS LOSS BOOK")

	def test_arbitrary_remark_is_rejected(self):
		"""Replaces the frappe Select check the empty JSON options turned off."""
		doc = _doc(percentage=1.8, remarks="whatever I like")
		with self.assertRaises(ValidationError):
			doc.set_remarks()

	def test_guard_holds_for_a_value_written_around_the_form(self):
		"""API / import writes reach validate too -- the guard is server-side."""
		doc = _doc(percentage=1.8, remarks="NR 1.80% PLAIN ROUND BALLS LOSS BOO")
		with self.assertRaises(ValidationError):
			doc.set_remarks()

	def test_renderer_is_the_single_source_of_truth(self):
		"""set_remarks must go through render_remark_options, never its own f-string."""
		doc = _doc(percentage=1.8, remarks="NR 1.800% PLAIN ROUND BALLS LOSS BOOK")
		with patch(
			f"{_MODULE}.render_remark_options", return_value=["SENTINEL"]
		) as renderer:
			doc.set_remarks()
		renderer.assert_called_once()
		self.assertEqual(doc.remarks, "SENTINEL")
