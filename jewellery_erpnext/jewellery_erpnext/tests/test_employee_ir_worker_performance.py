# Copyright (c) 2026, Nirali and contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir import employee_ir


class _Row(SimpleNamespace):
	"""An employee_ir_operations row."""

	def get(self, key, default=None):
		return getattr(self, key, default)


def _eir(type="Receive", operation="Filing", name="EMP-IR-1", rows=None, **fields):
	"""An Employee IR carrying just enough for set_repeat_receive_flag."""
	defaults = {
		"type": type,
		"operation": operation,
		"name": name,
		"employee_ir_operations": rows if rows is not None else [],
		"is_repeat_receive": 0,
		"worker_performance": None,
	}
	defaults.update(fields)
	doc = SimpleNamespace(**defaults)
	# Bind the real method to the stand-in; we are testing the method, not the ORM.
	doc.set_repeat_receive_flag = (
		lambda: employee_ir.EmployeeIR.set_repeat_receive_flag(doc)
	)
	return doc


def _row(mwo="MWO-0001", mop="MOP-0001"):
	return _Row(manufacturing_work_order=mwo, manufacturing_operation=mop)


class TestResolveWorkOrders(IntegrationTestCase):
	"""_resolve_work_orders: rows may reach us without their work order."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_uses_the_row_value_without_a_lookup(self):
		with patch.object(employee_ir.frappe, "get_all") as get_all:
			out = employee_ir._resolve_work_orders(
				[
					{
						"manufacturing_work_order": "MWO-1",
						"manufacturing_operation": "MOP-1",
					}
				]
			)
		self.assertEqual(out, {"MWO-1"})
		get_all.assert_not_called()

	def test_blank_work_order_is_resolved_through_its_mop(self):
		# A freshly scanned row carries only the MOP (manufacturing_work_order is
		# fetch_from + fetch_if_empty). Skipping it would hide the field on exactly
		# the entries a shop-floor user creates by scanning.
		with patch.object(
			employee_ir.frappe,
			"get_all",
			return_value=[
				SimpleNamespace(name="MOP-9", manufacturing_work_order="MWO-9")
			],
		) as get_all:
			out = employee_ir._resolve_work_orders(
				[{"manufacturing_work_order": None, "manufacturing_operation": "MOP-9"}]
			)
		self.assertEqual(out, {"MWO-9"})
		get_all.assert_called_once()
		self.assertEqual(get_all.call_args[0][0], "Manufacturing Operation")

	def test_only_the_missing_rows_are_looked_up(self):
		with patch.object(
			employee_ir.frappe,
			"get_all",
			return_value=[
				SimpleNamespace(name="MOP-B", manufacturing_work_order="MWO-B")
			],
		) as get_all:
			out = employee_ir._resolve_work_orders(
				[
					{
						"manufacturing_work_order": "MWO-A",
						"manufacturing_operation": "MOP-A",
					},
					{
						"manufacturing_work_order": "",
						"manufacturing_operation": "MOP-B",
					},
				]
			)
		self.assertEqual(out, {"MWO-A", "MWO-B"})
		self.assertEqual(get_all.call_args[1]["filters"], {"name": ["in", ["MOP-B"]]})

	def test_rows_with_neither_reference_are_ignored(self):
		with patch.object(employee_ir.frappe, "get_all") as get_all:
			out = employee_ir._resolve_work_orders(
				[{"manufacturing_work_order": None, "manufacturing_operation": None}]
			)
		self.assertEqual(out, set())
		get_all.assert_not_called()

	def test_duplicate_work_orders_collapse(self):
		out = employee_ir._resolve_work_orders(
			[
				{
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-1",
				},
				{
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-2",
				},
			]
		)
		self.assertEqual(out, {"MWO-1"})


class TestRepeatQuery(IntegrationTestCase):
	"""The predicates that decide what counts as a completed prior cycle."""

	@classmethod
	def setUpClass(cls):
		pass

	def _sql(self, **kwargs):
		kwargs.setdefault("mwos", {"MWO-1"})
		kwargs.setdefault("operation", "Filing")
		return employee_ir._repeat_query(
			kwargs["mwos"], kwargs["operation"], kwargs.get("employee_ir")
		).get_sql()

	def test_counts_only_submitted_receives(self):
		sql = self._sql()
		# docstatus=1, NOT <>2: a draft or cancelled Receive did not complete a cycle,
		# so it must not make the next Receive look like rework.
		self.assertIn("`docstatus`=1", sql.replace(" ", ""))
		self.assertIn("Receive", sql)

	def test_scopes_to_the_operation(self):
		self.assertIn("Polishing", self._sql(operation="Polishing"))

	def test_filters_on_the_requested_work_orders(self):
		sql = self._sql(mwos={"MWO-7", "MWO-8"})
		self.assertIn("MWO-7", sql)
		self.assertIn("MWO-8", sql)

	def test_excludes_the_current_document(self):
		sql = self._sql(employee_ir="EMP-IR-42")
		self.assertIn("EMP-IR-42", sql)
		self.assertIn("<>", sql)

	def test_no_self_exclusion_for_an_unsaved_document(self):
		self.assertNotIn("<>", self._sql(employee_ir=None))

	def test_joins_the_child_table_on_parent(self):
		sql = self._sql()
		self.assertIn("Employee IR Operation", sql)
		self.assertIn("JOIN", sql.upper())


class TestGetRepeatWorkOrders(IntegrationTestCase):
	"""The whitelisted resolver's short-circuits."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_accepts_a_json_string_from_the_client(self):
		with patch.object(employee_ir, "_repeat_query") as query, patch.object(
			employee_ir.frappe, "get_all"
		):
			query.return_value.run.return_value = ["MWO-1"]
			out = employee_ir.get_repeat_work_orders(
				'[{"manufacturing_work_order": "MWO-1", "manufacturing_operation": "MOP-1"}]',
				"Filing",
			)
		self.assertEqual(out, ["MWO-1"])

	def test_no_operation_short_circuits(self):
		with patch.object(employee_ir, "_repeat_query") as query:
			self.assertEqual(employee_ir.get_repeat_work_orders([{"x": 1}], None), [])
		query.assert_not_called()

	def test_no_resolvable_work_orders_short_circuits(self):
		with patch.object(employee_ir, "_repeat_query") as query, patch.object(
			employee_ir.frappe, "get_all", return_value=[]
		):
			self.assertEqual(employee_ir.get_repeat_work_orders([], "Filing"), [])
		query.assert_not_called()


class TestSetRepeatReceiveFlag(IntegrationTestCase):
	"""The document-level stamp: the ANY rule, and clearing a stale verdict."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_first_ever_receive_is_not_a_repeat(self):
		doc = _eir(rows=[_row()])
		with patch.object(employee_ir, "get_repeat_work_orders", return_value=[]):
			doc.set_repeat_receive_flag()
		self.assertEqual(doc.is_repeat_receive, 0)

	def test_prior_submitted_receive_flags_it(self):
		doc = _eir(rows=[_row()])
		with patch.object(
			employee_ir, "get_repeat_work_orders", return_value=["MWO-0001"]
		):
			doc.set_repeat_receive_flag()
		self.assertEqual(doc.is_repeat_receive, 1)

	def test_any_repeat_row_flags_a_mixed_receive(self):
		# Pins the ANY rule: one repeat work order alongside a first-timer still asks
		# the question. Switching to ALL would make this assertion fail.
		doc = _eir(rows=[_row("MWO-NEW", "MOP-1"), _row("MWO-OLD", "MOP-2")])
		with patch.object(
			employee_ir, "get_repeat_work_orders", return_value=["MWO-OLD"]
		):
			doc.set_repeat_receive_flag()
		self.assertEqual(doc.is_repeat_receive, 1)

	def test_issue_never_asks_and_clears_any_value(self):
		doc = _eir(type="Issue", rows=[_row()], worker_performance="YES")
		with patch.object(employee_ir, "get_repeat_work_orders") as resolver:
			doc.set_repeat_receive_flag()
		self.assertEqual(doc.is_repeat_receive, 0)
		self.assertIsNone(doc.worker_performance)
		resolver.assert_not_called()

	def test_stale_verdict_is_cleared_when_no_longer_a_repeat(self):
		# The earlier Receive got cancelled after somebody answered; a hidden field
		# must not keep a verdict that is no longer being asked for.
		doc = _eir(rows=[_row()], worker_performance="YES")
		with patch.object(employee_ir, "get_repeat_work_orders", return_value=[]):
			doc.set_repeat_receive_flag()
		self.assertEqual(doc.is_repeat_receive, 0)
		self.assertIsNone(doc.worker_performance)

	def test_answer_survives_on_a_genuine_repeat(self):
		doc = _eir(rows=[_row()], worker_performance="YES")
		with patch.object(
			employee_ir, "get_repeat_work_orders", return_value=["MWO-0001"]
		):
			doc.set_repeat_receive_flag()
		self.assertEqual(doc.worker_performance, "YES")

	def test_empty_grid_is_not_a_repeat(self):
		doc = _eir(rows=[])
		with patch.object(employee_ir, "get_repeat_work_orders", return_value=[]):
			doc.set_repeat_receive_flag()
		self.assertEqual(doc.is_repeat_receive, 0)

	def test_passes_operation_and_own_name_to_the_resolver(self):
		doc = _eir(operation="Setting", name="EMP-IR-99", rows=[_row("MWO-5", "MOP-5")])
		with patch.object(
			employee_ir, "get_repeat_work_orders", return_value=[]
		) as resolver:
			doc.set_repeat_receive_flag()
		ops, operation, name = resolver.call_args[0]
		self.assertEqual(operation, "Setting")
		self.assertEqual(name, "EMP-IR-99")  # self-exclusion reaches the query
		self.assertEqual(
			ops,
			[{"manufacturing_operation": "MOP-5", "manufacturing_work_order": "MWO-5"}],
		)
