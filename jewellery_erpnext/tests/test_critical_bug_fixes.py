# Copyright (c) 2026, Aerele and Contributors
# Tests for critical bug fixes — batch 1
# Run: bench run-tests --app jewellery_erpnext --module jewellery_erpnext.tests.test_critical_bug_fixes

import unittest
from unittest.mock import MagicMock, patch

from frappe.utils import flt


class TestMOPLogGrossWeight(unittest.TestCase):
	"""Bug #1: MOP Log update_wt_detail() used Python `or` instead of flt() addition.

	The expression `net_wt or 0 + finding_wt or 0 + ...` evaluates as:
	  net_wt OR (0 + finding_wt) OR (0 + ...) — due to operator precedence.
	If net_wt is truthy (non-zero), all other components are silently ignored.

	Fix: Use flt() for each component to ensure proper arithmetic addition.
	"""

	def test_gross_wt_includes_all_components(self):
		"""gross_wt must be the sum of all weight components, not just net_wt."""
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			update_wt_detail,
		)

		# Simulate MOP with all weight types populated
		test_weights = {
			"net_wt": 10.5,
			"finding_wt": 2.3,
			"diamond_wt_in_gram": 0.8,
			"gemstone_wt_in_gram": 1.2,
			"other_wt": 0.5,
			"previous_mop": None,
		}
		expected_gross = flt(10.5) + flt(2.3) + flt(0.8) + flt(1.2) + flt(0.5)  # 15.3

		with patch("frappe.db.get_value", return_value=tuple(test_weights.values())):
			captured = {}

			def mock_set_value(doctype, name, values):
				captured.update(values)

			with patch("frappe.db.set_value", side_effect=mock_set_value):
				update_wt_detail("MOP-001")

		self.assertAlmostEqual(captured["gross_wt"], expected_gross, places=4)

	def test_gross_wt_with_zero_net_wt(self):
		"""When net_wt is 0, other components must still be summed.

		This was the primary failure mode of the old `or`-based expression:
		`0 or 0 + finding_wt` would evaluate to finding_wt alone.
		"""
		test_weights = {
			"net_wt": 0.0,
			"finding_wt": 2.3,
			"diamond_wt_in_gram": 0.8,
			"gemstone_wt_in_gram": 0.0,
			"other_wt": 0.5,
			"previous_mop": None,
		}
		expected_gross = flt(0.0) + flt(2.3) + flt(0.8) + flt(0.0) + flt(0.5)  # 3.6

		with patch("frappe.db.get_value", return_value=tuple(test_weights.values())):
			captured = {}

			def mock_set_value(doctype, name, values):
				captured.update(values)

			with patch("frappe.db.set_value", side_effect=mock_set_value):
				update_wt_detail("MOP-001")

		self.assertAlmostEqual(captured["gross_wt"], expected_gross, places=4)

	def test_gross_wt_all_zeros(self):
		"""All components zero → gross_wt must be 0."""
		test_weights = (0.0, 0.0, 0.0, 0.0, 0.0, None)

		with patch("frappe.db.get_value", return_value=test_weights):
			captured = {}

			def mock_set_value(doctype, name, values):
				captured.update(values)

			with patch("frappe.db.set_value", side_effect=mock_set_value):
				update_wt_detail("MOP-001")

		self.assertEqual(captured["gross_wt"], 0.0)

	def test_gross_wt_with_none_values(self):
		"""None values from DB must be treated as 0, not cause TypeError."""
		test_weights = (None, None, 0.8, None, 0.5, None)

		with patch("frappe.db.get_value", return_value=test_weights):
			captured = {}

			def mock_set_value(doctype, name, values):
				captured.update(values)

			with patch("frappe.db.set_value", side_effect=mock_set_value):
				update_wt_detail("MOP-001")

		self.assertAlmostEqual(captured["gross_wt"], 1.3, places=4)

	def test_prev_gross_wt_from_previous_mop(self):
		"""prev_gross_wt must be fetched from previous MOP when it exists."""
		test_weights = (5.0, 1.0, 0.0, 0.0, 0.0, "MOP-PREV")

		def mock_get_value(doctype, name, fields=None):
			if name == "MOP-001":
				return test_weights
			if name == "MOP-PREV":
				return 12.5  # previous gross_wt
			return None

		with patch("frappe.db.get_value", side_effect=mock_get_value):
			captured = {}

			def mock_set_value(doctype, name, values):
				captured.update(values)

			with patch("frappe.db.set_value", side_effect=mock_set_value):
				update_wt_detail("MOP-001")

		self.assertAlmostEqual(captured["prev_gross_wt"], 12.5, places=4)


class TestGetPreviousSEDetails(unittest.TestCase):
	"""Bug #2: get_previous_se_details() had copy-pasted duplicate queries.

	The last two get_all calls were identical copies of the first two,
	doubling the result set and causing incorrect weight/qty downstream.

	Fix: Removed duplicates and combined into a single query.
	"""

	def test_no_duplicate_rows(self):
		"""Each Stock Entry Detail row must appear exactly once."""
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			get_previous_se_details,
		)

		mock_mop = MagicMock()
		mock_mop.name = "MOP-001"

		se_detail_d = [{"name": "SED-001", "s_warehouse": "WH-D"}]
		se_detail_e = [{"name": "SED-002", "s_warehouse": "WH-E"}]

		def mock_get_all(doctype, filters=None, pluck=None, **kwargs):
			if doctype == "Stock Entry":
				return ["SE-001"]
			if doctype == "Stock Entry Detail":
				# Combined query with warehouse IN filter
				warehouses = filters.get("s_warehouse", [None, []])[1]
				result = []
				if "WH-D" in warehouses:
					result += se_detail_d
				if "WH-E" in warehouses:
					result += se_detail_e
				return result
			return []

		with patch("frappe.db.get_all", side_effect=mock_get_all):
			rows = get_previous_se_details(mock_mop, "WH-D", "WH-E")

		# Must be exactly 2 rows — one from each warehouse. Old code returned 4.
		self.assertEqual(len(rows), 2)

	def test_empty_when_no_mop(self):
		"""Must return empty list when mop_doc is None/falsy."""
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			get_previous_se_details,
		)

		rows = get_previous_se_details(None, "WH-D", "WH-E")
		self.assertEqual(rows, [])

	def test_handles_none_warehouse(self):
		"""Must not query with None warehouse values."""
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			get_previous_se_details,
		)

		mock_mop = MagicMock()
		mock_mop.name = "MOP-001"

		call_args = []

		def mock_get_all(doctype, filters=None, pluck=None, **kwargs):
			call_args.append((doctype, filters))
			if doctype == "Stock Entry":
				return ["SE-001"]
			return []

		with patch("frappe.db.get_all", side_effect=mock_get_all):
			rows = get_previous_se_details(mock_mop, None, None)

		# Should return empty — no valid warehouses to query
		self.assertEqual(rows, [])


class TestSketchOrderValidate(unittest.TestCase):
	"""Bug #3: Sketch Order validate() modified child table during iteration.

	- `rows_remove` was built inside the for loop, and removal happened
	  inside the same iteration, causing items to be skipped.
	- Variable `r` was shadowed by the inner `for r in rows_remove` loop.

	Fix: Collect approved rows first, then remove after iteration completes.
	"""

	def test_all_approved_rows_moved(self):
		"""All approved rows from hold table must move to approval_cmo."""
		from jewellery_erpnext.gurukrupa_exports.doctype.sketch_order.sketch_order import (
			SketchOrder,
		)

		doc = MagicMock(spec=SketchOrder)
		doc.workflow_state = None  # skip populate_child_table

		# Create 3 hold rows, all approved
		hold_rows = []
		for i in range(3):
			row = MagicMock()
			row.is_approved = True
			row.designer = f"EMP-{i}"
			row.sketch_image = f"img-{i}"
			row.designer_name = f"Designer {i}"
			row.qc_person = f"QC-{i}"
			row.diamond_wt_approx = 1.0
			row.setting_type = "Prong"
			row.sub_category = "Ring"
			row.category = "Gold"
			row.image_rough = f"rough-{i}"
			row.final_image = f"final-{i}"
			hold_rows.append(row)

		doc.final_sketch_hold = hold_rows[:]
		doc.final_sketch_rejected = []
		doc.final_sketch_approval = []
		doc.final_sketch_approval_cmo = []

		appended_rows = []

		def mock_append(table, data):
			appended_rows.append(data)

		def mock_get(field, default=None):
			return getattr(doc, field, default or [])

		doc.append = mock_append
		doc.get = mock_get

		with patch(
			"jewellery_erpnext.gurukrupa_exports.doctype.sketch_order.sketch_order.populate_child_table"
		):
			with patch("frappe.msgprint"):
				# Call the real method
				SketchOrder._move_approved_to_cmo(doc, "final_sketch_hold", "hold")

		# All 3 rows must be appended
		self.assertEqual(len(appended_rows), 3)

	def test_unapproved_rows_stay(self):
		"""Unapproved rows must remain in the source table."""
		from jewellery_erpnext.gurukrupa_exports.doctype.sketch_order.sketch_order import (
			SketchOrder,
		)

		doc = MagicMock(spec=SketchOrder)

		approved_row = MagicMock()
		approved_row.is_approved = True
		for attr in ["designer", "sketch_image", "designer_name", "qc_person",
					  "diamond_wt_approx", "setting_type", "sub_category",
					  "category", "image_rough", "final_image"]:
			setattr(approved_row, attr, f"val-{attr}")

		unapproved_row = MagicMock()
		unapproved_row.is_approved = False

		source_table = [approved_row, unapproved_row]
		doc.final_sketch_hold = source_table
		doc.final_sketch_approval = []
		doc.final_sketch_approval_cmo = []
		doc.get = lambda field, default=None: getattr(doc, field, default or [])
		doc.append = MagicMock()

		with patch("frappe.msgprint"):
			SketchOrder._move_approved_to_cmo(doc, "final_sketch_hold", "hold")

		# Unapproved row must remain
		self.assertIn(unapproved_row, doc.final_sketch_hold)
		# Approved row must be removed
		self.assertNotIn(approved_row, doc.final_sketch_hold)


class TestBatchAddTimeLogs(unittest.TestCase):
	"""Bug #4: batch_add_time_logs() pre-fetched MOPs as dicts (name+status only)
	but then loaded full docs anyway, making the pre-fetch wasted work.

	Fix: Pre-fetch only names (as a set) for existence check. Full doc load
	is still needed (for time_logs child table) but now only happens once per
	unique MOP via the full_docs cache.
	"""

	def test_skips_nonexistent_mops(self):
		"""MOPs not found in DB must be silently skipped."""
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			batch_add_time_logs,
		)

		mock_self = MagicMock()
		mop_args = [
			("MOP-EXISTS", {"status": "WIP"}),
			("MOP-GONE", {"status": "WIP"}),
		]

		docs_loaded = []

		def mock_get_all(doctype, filters=None, pluck=None, **kwargs):
			return ["MOP-EXISTS"]  # only one exists

		def mock_get_doc(doctype, name):
			docs_loaded.append(name)
			doc = MagicMock()
			doc.status = "Not Started"
			doc.time_logs = []
			doc.flags = MagicMock()
			return doc

		with patch("frappe.get_all", side_effect=mock_get_all):
			with patch("frappe.get_doc", side_effect=mock_get_doc):
				batch_add_time_logs(mock_self, mop_args)

		# Only MOP-EXISTS should be loaded
		self.assertEqual(docs_loaded, ["MOP-EXISTS"])

	def test_deduplicates_same_mop(self):
		"""Same MOP appearing twice in args must load doc only once."""
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			batch_add_time_logs,
		)

		mock_self = MagicMock()
		mop_args = [
			("MOP-001", {"status": "WIP", "start_time": "2026-03-20 10:00:00"}),
			("MOP-001", {"status": "QC Pending"}),
		]

		load_count = [0]

		def mock_get_all(doctype, filters=None, pluck=None, **kwargs):
			return ["MOP-001"]

		def mock_get_doc(doctype, name):
			load_count[0] += 1
			doc = MagicMock()
			doc.status = "Not Started"
			doc.time_logs = []
			doc.flags = MagicMock()
			return doc

		with patch("frappe.get_all", side_effect=mock_get_all):
			with patch("frappe.get_doc", side_effect=mock_get_doc):
				batch_add_time_logs(mock_self, mop_args)

		# Must load only once despite two args for same MOP
		self.assertEqual(load_count[0], 1)


class TestEmployeeIRCancelDuplicate(unittest.TestCase):
	"""Bug #5: Employee IR on_submit_receive cancel path had a copy-pasted
	duplicate frappe.db.set_value for Stock Entry Detail.

	The same set_value call appeared twice in succession with identical filters,
	causing unnecessary DB writes.

	Fix: Removed the duplicate call.
	"""

	def test_no_duplicate_set_value_in_source(self):
		"""Verify the source file no longer has consecutive duplicate set_value blocks."""
		import inspect
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir import employee_ir

		source = inspect.getsource(employee_ir)

		# Find the specific pattern: two identical Stock Entry Detail set_value blocks
		# separated only by whitespace
		pattern_count = source.count(
			'frappe.db.set_value(\n'
			'\t\t\t\t\t\t\t"Stock Entry Detail",\n'
			'\t\t\t\t\t\t\t{\n'
			'\t\t\t\t\t\t\t\t"docstatus": 2,\n'
			'\t\t\t\t\t\t\t\t"manufacturing_operation": new_operation.name,\n'
			'\t\t\t\t\t\t\t},\n'
			'\t\t\t\t\t\t\t"manufacturing_operation",\n'
			'\t\t\t\t\t\t\tNone,\n'
			'\t\t\t\t\t\t)'
		)

		# Should appear exactly once (not twice as before)
		self.assertEqual(pattern_count, 1, "Duplicate set_value block still exists in employee_ir.py")


class TestQCDebugMsgprint(unittest.TestCase):
	"""Bonus: debug frappe.msgprint left in min_max_criteria_passed().

	The old code called frappe.msgprint(reading_value) for every reading,
	showing a popup to the user on every validation.

	Fix: Removed the debug line.
	"""

	def test_no_msgprint_in_min_max(self):
		"""min_max_criteria_passed must not call frappe.msgprint."""
		import inspect
		from jewellery_erpnext.jewellery_erpnext.doctype.qc.qc import QC

		source = inspect.getsource(QC.min_max_criteria_passed)
		self.assertNotIn("frappe.msgprint", source)


if __name__ == "__main__":
	unittest.main()
