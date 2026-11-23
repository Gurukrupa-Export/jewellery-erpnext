"""C-01 (lock_order RULE F): cascades pin only the naming counters they will mint.

Portable unit tests: every database touch is mocked or a plain read, nothing is written.
The held-row and end-to-end cases live in the ``_c01_*`` harnesses for a throwaway site.
"""

import copy
import random
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import CallbackManager, cint, flt

from jewellery_erpnext.jewellery_erpnext import lock_order
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir import (
	employee_ir as eir_mod,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	main_slip_inject,
	receive_naming,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.finding_repack import (
	finding_pairs,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.precision import (
	round_employee_ir_weights_to_precision,
)
from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator import (
	serial_number_creator as snc_mod,
)
from jewellery_erpnext.jewellery_erpnext.tests import naming_lock_probe as probe_mod

MT = "Material Transfer (WORK ORDER)"
PL = "Process Loss"
RP = "Repack"


class _Row(frappe._dict):
	"""A child-row stand-in that also has Document.set (the rounding helper calls it)."""

	def set(self, key, value):
		self[key] = value


def _eir(
	rows=(),
	*,
	type="Receive",
	is_raw_material=0,
	employee_loss=(),
	manual_loss=(),
):
	return _Row(
		doctype="Employee IR",
		name="EIR-C01-1",
		type=type,
		operation="OP-C01",
		is_raw_material=is_raw_material,
		employee_ir_operations=[_Row(idx=i + 1, **r) for i, r in enumerate(rows)],
		employee_loss_details=[_Row(proportionally_loss=p) for p in employee_loss],
		manually_book_loss_details=[_Row(proportionally_loss=p) for p in manual_loss],
	)


class _C01TestCase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.flags.pop(lock_order.PLANNED_COUNTERS_FLAG, None)
		self.addCleanup(frappe.flags.pop, lock_order.PLANNED_COUNTERS_FLAG, None)


# --------------------------------------------------------------------------- U1
class TestReceiveMintTypes(_C01TestCase):
	def _types(self, eir, *, finding_flag=False, precision=3):
		with (
			patch.object(
				receive_naming, "is_finding_repack_eir", return_value=finding_flag
			),
			patch.object(frappe, "get_precision", return_value=precision),
		):
			return receive_naming.receive_mint_types(eir)

	def test_u1_01_flat_receive_mints_nothing(self):
		self.assertEqual(
			self._types(_eir([{"gross_wt": 2.9, "received_gross_wt": 2.9}])), ()
		)

	def test_u1_02_flat_raw_material_receive_mints_nothing(self):
		eir = _eir([{"gross_wt": 2.9, "received_gross_wt": 2.9}], is_raw_material=1)
		self.assertEqual(self._types(eir), ())

	def test_u1_03_gain_plans_transfer_and_repack(self):
		eir = _eir([{"gross_wt": 2.9, "received_gross_wt": 3.9}], is_raw_material=1)
		self.assertEqual(self._types(eir), (MT, RP))

	def test_u1_04_gain_without_raw_material_mints_nothing(self):
		self.assertEqual(
			self._types(_eir([{"gross_wt": 2.9, "received_gross_wt": 3.9}])), ()
		)

	def test_u1_05_sub_precision_gain_is_planned_unrounded(self):
		# The cascade compares unrounded, so 0.0004 is a gain there and must be planned here
		# (a superset); after validate's rounding the same row is flat and plans nothing.
		eir = _eir([{"gross_wt": 2.9, "received_gross_wt": 2.9004}], is_raw_material=1)
		self.assertEqual(self._types(eir), (MT, RP))
		round_employee_ir_weights_to_precision(eir)
		self.assertEqual(self._types(eir), ())

	def test_u1_06_gain_that_survives_rounding(self):
		eir = _eir([{"gross_wt": 2.9, "received_gross_wt": 2.9006}], is_raw_material=1)
		round_employee_ir_weights_to_precision(eir)
		self.assertEqual(self._types(eir), (MT, RP))

	def test_u1_07_finding_on_a_finding_repack_operation(self):
		eir = _eir(
			[
				{
					"gross_wt": 2.9,
					"received_gross_wt": 2.9,
					"finding_item1": "F-X",
					"finding_wt1": 0.5,
				}
			]
		)
		self.assertEqual(self._types(eir, finding_flag=True), (MT, RP))

	def test_u1_08_finding_on_another_operation_mints_nothing(self):
		eir = _eir(
			[
				{
					"gross_wt": 2.9,
					"received_gross_wt": 2.9,
					"finding_item1": "F-X",
					"finding_wt1": 0.5,
				}
			]
		)
		self.assertEqual(self._types(eir, finding_flag=False), ())

	def test_u1_08b_flag_is_only_read_when_a_finding_exists(self):
		eir = _eir([{"gross_wt": 2.9, "received_gross_wt": 2.9}])
		with (
			patch.object(receive_naming, "is_finding_repack_eir") as flag,
			patch.object(frappe, "get_precision", return_value=3),
		):
			receive_naming.receive_mint_types(eir)
		flag.assert_not_called()

	def test_u1_09_employee_loss(self):
		self.assertEqual(self._types(_eir(employee_loss=[0.1])), (PL,))

	def test_u1_10_sub_precision_manual_loss_is_dropped(self):
		self.assertEqual(self._types(_eir(manual_loss=[0.0004])), ())

	def test_u1_11_half_unit_loss_follows_the_cascade_rounding(self):
		expected = (
			flt(0.0005, 3) > 0
		)  # loss_stock_entry._prepare_loss_row, site rounding
		self.assertEqual(PL in self._types(_eir(manual_loss=[0.0005])), expected)

	def test_u1_12_gain_and_loss(self):
		eir = _eir(
			[{"gross_wt": 2.9, "received_gross_wt": 3.9}],
			is_raw_material=1,
			employee_loss=[0.2],
		)
		self.assertEqual(self._types(eir), (MT, PL, RP))

	def test_u1_13_issue_mints_nothing(self):
		eir = _eir(
			[{"gross_wt": 2.9, "received_gross_wt": 3.9}],
			type="Issue",
			is_raw_material=1,
			employee_loss=[0.2],
		)
		self.assertEqual(self._types(eir), ())

	def test_u1_14_transfer_qty_precision_is_honoured(self):
		self.assertEqual(self._types(_eir(employee_loss=[0.004]), precision=2), ())
		# Rounded twice (validate to 3 dp, then the Stock Entry precision) 0.0049 survives.
		self.assertEqual(
			PL in self._types(_eir(employee_loss=[0.0049]), precision=2),
			flt(flt(0.0049, 3), 2) > 0,
		)

	def test_u1_15_property_superset_of_the_mint_gates(self):
		rng = random.Random(20261117)
		deltas = (0, 0.0004, -0.0004, 0.0005, -0.0005, 0.0006, 0.001, -0.3, 0.7)
		losses = (0, 0.0004, 0.0005, 0.00049, 0.0006, 0.001, 0.0049, 0.25)
		for _case in range(500):
			precision = rng.choice((2, 3))
			finding_flag = rng.random() < 0.3
			rows = []
			for _r in range(rng.randint(0, 4)):
				gross = round(rng.uniform(0, 5), 4)
				row = {
					"gross_wt": gross,
					"received_gross_wt": gross + rng.choice(deltas),
				}
				if rng.random() < 0.2:
					row["finding_item1"] = rng.choice(("F-X", None))
					row["finding_wt1"] = rng.choice((0, 0.3))
				if rng.random() < 0.1:
					row["finding_item2"] = "F-Y"
					row["finding_wt2"] = rng.choice((0, 0.2))
				rows.append(row)
			raw = _eir(
				rows,
				type=rng.choice(("Receive", "Receive", "Issue")),
				is_raw_material=rng.choice((0, 1)),
				employee_loss=[rng.choice(losses) for _ in range(rng.randint(0, 2))],
				manual_loss=[rng.choice(losses) for _ in range(rng.randint(0, 2))],
			)
			rounded = copy.deepcopy(raw)
			round_employee_ir_weights_to_precision(rounded)
			truth = self._ground_truth(rounded, finding_flag, precision)
			on_rounded = set(
				self._types(rounded, finding_flag=finding_flag, precision=precision)
			)
			on_raw = set(
				self._types(raw, finding_flag=finding_flag, precision=precision)
			)
			self.assertEqual(on_rounded, truth, rounded)
			self.assertTrue(truth <= on_raw, (raw, truth, on_raw))

	@staticmethod
	def _ground_truth(eir, finding_flag, precision):
		"""The literal mint gates of the cascade, on the document validate leaves behind."""
		if eir.type != "Receive":
			return set()
		types = set()
		for row in eir.employee_ir_operations:
			has_gain = (
				cint(eir.is_raw_material)
				and (flt(row.received_gross_wt) - flt(row.gross_wt)) > 0
			)
			if has_gain or (finding_flag and finding_pairs(row)):
				types.update((MT, RP))
		for row in eir.employee_loss_details + eir.manually_book_loss_details:
			if flt(row.proportionally_loss, precision) > 0:
				types.add(PL)
		return types


# --------------------------------------------------------------------------- U2
class TestPreallocateStockEntryCounters(_C01TestCase):
	def test_u2_1_empty_plan_takes_no_lock(self):
		with (
			patch.object(lock_order, "series_stubs") as stubs,
			patch.object(lock_order, "preallocate_series_for_docs") as pre,
		):
			self.assertEqual(lock_order.preallocate_stock_entry_counters("C", []), ())
			self.assertEqual(lock_order.preallocate_stock_entry_counters("C", None), ())
		stubs.assert_not_called()
		pre.assert_not_called()

	def test_u2_2_types_are_deduplicated_and_sorted(self):
		with (
			patch.object(
				lock_order, "series_stubs", return_value=["s1", "s2"]
			) as stubs,
			patch.object(lock_order, "preallocate_series_for_docs") as pre,
		):
			out = lock_order.preallocate_stock_entry_counters("C", [RP, PL, RP, None])
		self.assertEqual(out, (PL, RP))
		stubs.assert_called_once_with("C", PL, RP)
		pre.assert_called_once_with("s1", "s2")

	def test_u2_3_rule_rows_before_series_rows(self):
		stubs = lock_order.series_stubs(
			"C", PL, RP
		)  # built before frappe.db is patched
		calls = []

		def _get_value(doctype, name, field, for_update=False):
			calls.append(("rule", name, for_update))

		def _sql(query, values=()):
			calls.append(("series", values[0], "FOR UPDATE" in query))

		with (
			patch.object(lock_order, "series_stubs", return_value=stubs),
			patch.object(
				lock_order,
				"document_naming_rule_for_doc",
				side_effect=lambda d: "a0h37l6l3v"
				if d.stock_entry_type == RP
				else None,
			),
			patch.object(lock_order, "series_prefix_for_doc", return_value="MAT-STE-"),
			patch.object(frappe.db, "get_value", side_effect=_get_value),
			patch.object(frappe.db, "sql", side_effect=_sql),
		):
			lock_order.preallocate_stock_entry_counters("C", [RP, PL])
		self.assertEqual(
			calls, [("rule", "a0h37l6l3v", True), ("series", "MAT-STE-", True)]
		)

	def test_u2_4_plan_is_recorded_per_parent_and_forgotten_at_transaction_end(self):
		after_commit, after_rollback = CallbackManager(), CallbackManager()
		parent = frappe._dict(doctype="Employee IR", name="EIR-1")
		with (
			patch.object(lock_order, "series_stubs", return_value=[]),
			patch.object(lock_order, "preallocate_series_for_docs"),
			patch.object(frappe.db, "after_commit", after_commit),
			patch.object(frappe.db, "after_rollback", after_rollback),
		):
			lock_order.preallocate_stock_entry_counters("C", [PL], plan_for=parent)
			lock_order.preallocate_stock_entry_counters(
				"C", [], plan_for=frappe._dict(doctype="Employee IR", name="EIR-2")
			)
			lock_order.preallocate_stock_entry_counters(
				"C", [PL], plan_for=frappe._dict(doctype="Employee IR", name=None)
			)
			plans = frappe.flags[lock_order.PLANNED_COUNTERS_FLAG]
			self.assertEqual(
				plans,
				{
					("Employee IR", "EIR-1"): {("C", PL)},
					("Employee IR", "EIR-2"): set(),
				},
			)
			after_rollback.run()
		self.assertEqual(frappe.flags[lock_order.PLANNED_COUNTERS_FLAG], {})


# --------------------------------------------------------------------------- U6
class TestUnplannedCounterGuard(_C01TestCase):
	def _se(self, se_type=RP, company="C", **links):
		links.setdefault("employee_ir", "EIR-1")
		return frappe._dict(
			doctype="Stock Entry", stock_entry_type=se_type, company=company, **links
		)

	def _guard(self, doc, *, rule=None, prefix="MAT-STE-", sql=None):
		sql = sql or MagicMock(return_value=())
		with (
			patch.object(lock_order, "document_naming_rule_for_doc", return_value=rule),
			patch.object(lock_order, "series_prefix_for_doc", return_value=prefix),
			patch.object(frappe.db, "sql", sql),
			patch.object(frappe, "logger"),
		):
			lock_order.guard_unplanned_stock_entry_counter(doc)
		return sql

	def _plan(self, *counters, parent=("Employee IR", "EIR-1")):
		frappe.flags[lock_order.PLANNED_COUNTERS_FLAG] = {parent: set(counters)}
		return frappe.flags[lock_order.PLANNED_COUNTERS_FLAG][parent]

	def test_u6_1_no_plan_runs_no_sql(self):
		self._guard(self._se()).assert_not_called()

	def test_u6_2_planned_type_runs_no_sql(self):
		self._plan(("C", RP))
		self._guard(self._se(RP), rule="a0h37l6l3v").assert_not_called()

	def test_u6_3_unplanned_rule_counter_is_taken_nowait(self):
		plan = self._plan(("C", PL))
		sql = self._guard(self._se(RP), rule="a0h37l6l3v")
		query, values = sql.call_args.args
		self.assertIn("`tabDocument Naming Rule`", query)
		self.assertIn("FOR UPDATE NOWAIT", query)
		self.assertEqual(values, ("a0h37l6l3v",))
		self.assertIn(("C", RP), plan)

	def test_u6_4_unplanned_series_counter_is_taken_nowait(self):
		self._plan()
		sql = self._guard(self._se(MT))
		query, values = sql.call_args.args
		self.assertIn("`tabSeries`", query)
		self.assertIn("FOR UPDATE NOWAIT", query)
		self.assertEqual(values, ("MAT-STE-",))

	def test_u6_5_busy_counter_refuses_the_submit(self):
		self._plan(("C", PL))
		busy = MagicMock(side_effect=frappe.QueryTimeoutError("1205"))
		with self.assertRaises(lock_order.NamingCounterBusy) as ctx:
			self._guard(self._se(RP), rule="a0h37l6l3v", sql=busy)
		self.assertIn(RP, str(ctx.exception))

	def test_u6_6_other_parent_is_not_guarded(self):
		self._plan(parent=("Employee IR", "EIR-OTHER"))
		self._guard(self._se(RP)).assert_not_called()

	def test_u6_7_company_mismatch_is_unplanned(self):
		self._plan(("C", RP))
		sql = self._guard(self._se(RP, company="OTHER"), rule="r-other")
		self.assertEqual(sql.call_args.args[1], ("r-other",))

	def test_u6_8_second_entry_of_a_type_passes_freely(self):
		self._plan()
		sql = MagicMock(return_value=())
		self._guard(self._se(MT), sql=sql)
		self._guard(self._se(MT), sql=sql)
		self.assertEqual(sql.call_count, 1)

	def test_u6_9_amended_entry_takes_no_counter(self):
		self._plan()
		self._guard(self._se(MT, amended_from="SE-OLD")).assert_not_called()

	def test_u6_10_snc_and_pc_links_are_recognised(self):
		self._plan(parent=("Serial Number Creator", "SNC-1"))
		sql = self._guard(
			self._se(RP, employee_ir=None, custom_serial_number_creator="SNC-1"),
			rule="a0h37l6l3v",
		)
		self.assertEqual(sql.call_count, 1)
		self._plan(parent=("Product Certification", "PC-1"))
		sql = self._guard(self._se(RP, employee_ir=None, product_certification="PC-1"))
		self.assertEqual(sql.call_count, 1)

	def test_u6_11_hook_is_wired(self):
		from jewellery_erpnext import hooks

		self.assertEqual(
			hooks.doc_events["Stock Entry"]["before_naming"],
			"jewellery_erpnext.jewellery_erpnext.lock_order.guard_unplanned_stock_entry_counter",
		)


# --------------------------------------------------------------------------- U3
class TestReceivePrelockWiring(_C01TestCase):
	def _receive(self, rows=()):
		doc = frappe.new_doc("Employee IR")
		doc.name = "EIR-C01-WIRING"
		doc.type = "Receive"
		doc.company = "C01-COMPANY"
		doc.department = "C01-NO-DEPARTMENT"
		doc.employee = "C01-NO-EMPLOYEE"
		doc.operation = "C01-NO-OPERATION"
		doc.subcontracting = "No"
		for row in rows:
			doc.append("employee_ir_operations", row)
		return doc

	def _run(self, doc, plan):
		calls = []
		stub_args = []

		def _rec(tag):
			return lambda *a, **k: calls.append(tag)

		def _stubs(*args):
			stub_args.append(args)
			return []

		with (
			patch.object(eir_mod, "lock_trees_for_eir", side_effect=_rec("tree")),
			patch.object(
				eir_mod, "lock_finding_repack_trees", side_effect=_rec("tree")
			),
			patch.object(eir_mod, "finding_bin_pairs", return_value=[]),
			patch.object(
				main_slip_inject,
				"_resolve_source_warehouse_raw_material",
				return_value=None,
			),
			patch.object(receive_naming, "receive_mint_types", return_value=plan),
			patch.object(lock_order, "series_stubs", side_effect=_stubs),
			patch.object(
				lock_order, "preallocate_series_for_docs", side_effect=_rec("series")
			),
			patch.object(lock_order, "lock_bins", side_effect=_rec("bins")),
			patch.object(
				eir_mod, "create_loss_stock_entries", side_effect=_rec("loss")
			),
			patch.object(eir_mod, "pin_tree_numbers_on_receive"),
			patch.object(eir_mod, "update_tree_on_receive"),
			patch.object(eir_mod.EmployeeIR, "_refresh_msl_tracking"),
		):
			doc.on_submit_receive()
		return calls, stub_args

	def test_u3_1_nothing_to_mint_takes_no_naming_lock(self):
		doc = self._receive()
		calls, stub_args = self._run(doc, ())
		self.assertEqual(calls, ["tree", "tree", "bins", "loss"])
		self.assertEqual(stub_args, [])
		self.assertEqual(doc.flags.planned_stock_entry_types, ())

	def test_u3_2_process_loss_only_pins_one_counter_before_the_bins(self):
		doc = self._receive()
		calls, stub_args = self._run(doc, (PL,))
		self.assertEqual(calls, ["tree", "tree", "series", "bins", "loss"])
		self.assertEqual(stub_args, [("C01-COMPANY", PL)])

	def test_u3_3_plan_is_recorded_for_the_guard(self):
		doc = self._receive()
		self._run(doc, (PL,))
		self.assertEqual(
			frappe.flags[lock_order.PLANNED_COUNTERS_FLAG][("Employee IR", doc.name)],
			{("C01-COMPANY", PL)},
		)

	def test_u3_4_missing_received_weight_fails_before_any_lock(self):
		doc = self._receive([{"gross_wt": 1.5, "received_gross_wt": 0}])
		calls = []
		with patch.object(
			eir_mod, "lock_trees_for_eir", side_effect=lambda d: calls.append(1)
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.on_submit_receive()
		self.assertIn("Received Gross Wt Missing", str(ctx.exception))
		self.assertEqual(calls, [])


# --------------------------------------------------------------------------- U4
class _Stop(Exception):
	pass


def _fake_get_all(sres, kids):
	"""frappe.get_all over in-memory SRE / Serial and Batch Entry rows, for the filters the
	SNC reservation readers use."""

	def _match(row, filters):
		for key, cond in filters.items():
			value = row.get(key)
			if isinstance(cond, (list, tuple)):
				op, arg = cond
				if op == "in" and value not in arg:
					return False
				if op == "not in" and value in arg:
					return False
			elif value != cond:
				return False
		return True

	def get_all(doctype, filters=None, fields=None, **kwargs):
		rows = sres if doctype == "Stock Reservation Entry" else kids
		return [frappe._dict(r) for r in rows if _match(r, filters or {})]

	return get_all


class TestSncPlannedCounters(_C01TestCase):
	def _snc(self):
		doc = frappe.new_doc("Serial Number Creator")
		doc.name = "SNC-C01-1"
		doc.company = "C01-COMPANY"
		doc.manufacturing_work_order = "MWO-C01-NONE"
		doc.append(
			"source_table",
			{
				"row_material": "C01-ITEM",
				"qty": 3.0,
				"uom": "Gram",
				"s_warehouse": "C01-WH",
			},
		)
		return doc

	def _types(self, may_book_loss):
		stub_args = []
		with (
			patch.object(snc_mod, "_pmo_mwo_names", return_value=("PMO-1", ["MWO-1"])),
			patch.object(
				snc_mod, "_snc_may_book_loss", return_value=may_book_loss
			) as gate,
			patch.object(snc_mod, "_reserved_warehouse_caps", return_value=[]),
			patch.object(
				lock_order,
				"series_stubs",
				side_effect=lambda *a: stub_args.append(a) or [],
			),
			patch.object(lock_order, "preallocate_series_for_docs"),
			patch.object(lock_order, "lock_bins", side_effect=_Stop),
		):
			with self.assertRaises(_Stop):
				snc_mod.to_prepare_data_for_make_mnf_stock_entry(self._snc())
		groups = gate.call_args.args[1]
		self.assertEqual(list(groups), [("C01-ITEM", None)])
		return stub_args

	def test_u4_1_no_possible_loss_pins_manufacture_only(self):
		self.assertEqual(self._types(False), [("C01-COMPANY", "Manufacture")])

	def test_u4_2_possible_loss_pins_repack_too(self):
		self.assertEqual(self._types(True), [("C01-COMPANY", "Manufacture", RP)])

	def _bound(self, sres, kids, groups, pc_rows=()):
		snc = frappe._dict(name="SNC-1", manufacturing_work_order="MWO-1")
		with (
			patch.object(frappe, "get_all", side_effect=_fake_get_all(sres, kids)),
			patch.object(
				frappe.db, "sql", return_value=[frappe._dict(r) for r in pc_rows]
			),
		):
			return snc_mod._snc_may_book_loss(snc, groups, ["MWO-1"], "PMO-1")

	@staticmethod
	def _sre(name, reserved, delivered, item="I1", status="Reserved", mwo="MWO-1"):
		return {
			"name": name,
			"item_code": item,
			"warehouse": "W1",
			"reserved_qty": reserved,
			"delivered_qty": delivered,
			"docstatus": 1,
			"status": status,
			"manufacturing_work_order": mwo,
		}

	def test_u4_3_worked_example(self):
		sres = [self._sre("S1", 5, 1), self._sre("S2", 2, 2)]
		self.assertFalse(self._bound(sres, [], {("I1", None): [{"qty": 4.0}]}))
		self.assertTrue(self._bound(sres, [], {("I1", None): [{"qty": 3.9}]}))
		# A PC Receive row larger than the group also makes a loss possible.
		self.assertTrue(
			self._bound(
				[],
				[],
				{("I1", None): [{"qty": 4.0}]},
				[{"item_code": "I1", "qty": 4.5}],
			)
		)

	def test_u4_4_bound_covers_the_loss_step_randomised(self):
		rng = random.Random(1117)
		snc = frappe._dict(name="SNC-1", manufacturing_work_order="MWO-1")
		for _case in range(300):
			sres, kids = [], []
			for i in range(rng.randint(0, 6)):
				reserved = round(rng.uniform(0, 6), 3)
				delivered = round(rng.choice((0, 0, rng.uniform(0, reserved))), 3)
				sres.append(
					self._sre(
						f"S{i}",
						reserved,
						delivered,
						item=rng.choice(("I1", "I2")),
						status=rng.choice(
							("Reserved", "Partially Delivered", "Delivered")
						),
						mwo=rng.choice(("MWO-1", "MWO-1", "MWO-X")),
					)
				)
				if rng.random() < 0.6:
					for batch in rng.sample(("B1", "B2", "B3"), rng.randint(1, 2)):
						qty = round(rng.uniform(0, reserved), 3)
						kids.append(
							{
								"parent": f"S{i}",
								"parenttype": "Stock Reservation Entry",
								"batch_no": batch,
								"qty": qty,
								"delivered_qty": round(
									rng.choice((0, rng.uniform(0, qty))), 3
								),
							}
						)
			groups = {}
			for item, batch in rng.sample(
				[(i, b) for i in ("I1", "I2") for b in ("B1", "B2", None)],
				rng.randint(1, 4),
			):
				groups[(item, batch)] = [{"qty": round(rng.uniform(0, 6), 3)}]
			pc_latest = {
				item: round(rng.uniform(0, 6), 3)
				for item in ("I1", "I2")
				if rng.random() < 0.3
			}

			with (
				patch.object(frappe, "get_all", side_effect=_fake_get_all(sres, kids)),
				patch.object(
					frappe.db,
					"sql",
					return_value=[
						frappe._dict(item_code=k, qty=v) for k, v in pc_latest.items()
					],
				),
			):
				bounds = snc_mod._snc_loss_upper_bounds(snc, groups, ["MWO-1"], "PMO-1")
				may_book = snc_mod._snc_may_book_loss(snc, groups, ["MWO-1"], "PMO-1")
				consumed = set()
				loop_books_loss = False
				for (item, batch), rows in groups.items():
					# The loss step's figure, read the way the loop reads it.
					active = snc_mod._active_sres_for(
						item, batch, ["MWO-1"], exclude=consumed
					)
					if active:
						total = sum(rem for _sre, rem in active)
						consumed.update(sre["name"] for sre, _rem in active)
					else:
						total = flt(pc_latest.get(item))
					self.assertGreaterEqual(bounds[(item, batch)] + 1e-9, total)
					group_qty = flt(sum(flt(r["qty"]) for r in rows), 3)
					if flt(total - group_qty, 3) > 0:
						loop_books_loss = True
			if loop_books_loss:
				self.assertTrue(may_book, (sres, kids, groups, pc_latest))

	def test_u4_5_fails_open_but_never_swallows_a_lock_error(self):
		snc = frappe._dict(name="SNC-1", manufacturing_work_order="MWO-1")
		groups = {("I1", None): [{"qty": 1}]}
		with (
			patch.object(snc_mod, "_snc_loss_upper_bounds", side_effect=KeyError("x")),
			patch.object(frappe, "logger") as logger,
		):
			self.assertTrue(snc_mod._snc_may_book_loss(snc, groups, ["MWO-1"], "PMO-1"))
		logger.return_value.warning.assert_called_once()
		with patch.object(
			snc_mod,
			"_snc_loss_upper_bounds",
			side_effect=frappe.QueryDeadlockError("1213"),
		):
			with self.assertRaises(frappe.QueryDeadlockError):
				snc_mod._snc_may_book_loss(snc, groups, ["MWO-1"], "PMO-1")

	def test_u4_6_no_groups_reads_nothing(self):
		with patch.object(snc_mod, "_snc_loss_upper_bounds") as bound:
			self.assertFalse(snc_mod._snc_may_book_loss(frappe._dict(), {}, ["M"], "P"))
		bound.assert_not_called()


# --------------------------------------------------------------------------- U5
class _FakeStockEntry:
	def __init__(self):
		self.items = []
		self.flags = frappe._dict()

	def append(self, _table, row):
		self.items.append(frappe._dict(row))
		return self.items[-1]

	def save(self, *args, **kwargs):
		pass

	def submit(self):
		pass


class TestPcPlannedCounters(_C01TestCase):
	def _plan(self, exploded):
		from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.doc_events import (
			utils as pc_utils,
		)

		doc = frappe._dict(
			doctype="Product Certification",
			name="PC-RECEIVE-C01",
			type="Receive",
			service_type="Fire Assy Service",
			company="C01-COMPANY",
			department="Dept",
			supplier="Supp",
			receive_against="PC-ISSUE-1",
			product_details=[
				frappe._dict(
					idx=1,
					item_code="M22",
					tree_no="TREE-A",
					total_weight=100.0,
					pure_item="M24",
					loss_item="ML22",
				)
			],
			exploded_product_details=[
				frappe._dict(idx=i + 1, **row) for i, row in enumerate(exploded)
			],
		)
		real_get_value = frappe.db.get_value
		real_cached_value = frappe.get_cached_value
		planned = []

		def _get_value(*args, **kwargs):
			doctype = args[0] if args else kwargs.get("doctype")
			return (
				"SE-ISSUE-1"
				if doctype == "Stock Entry"
				else real_get_value(*args, **kwargs)
			)

		def _cached_value(doctype, *args, **kwargs):
			if doctype == "Item":
				return (0, 0, 0)
			return real_cached_value(doctype, *args, **kwargs)

		with (
			patch.object(
				pc_utils, "_get_department_rm_warehouse", return_value="RM-WH"
			),
			patch.object(
				pc_utils, "_get_department_scrap_warehouse", return_value="SCRAP-WH"
			),
			patch.object(
				pc_utils, "_get_supplier_certification_warehouse", return_value="SUP-WH"
			),
			patch.object(
				pc_utils, "_get_issue_stock_entry_details", return_value=({}, {})
			),
			patch.object(pc_utils, "_receipt_precision", return_value=3),
			patch.object(frappe.db, "get_value", side_effect=_get_value),
			patch.object(frappe, "get_cached_value", side_effect=_cached_value),
			patch.object(
				frappe, "new_doc", side_effect=lambda *a, **k: _FakeStockEntry()
			),
			patch.object(lock_order, "lock_bins"),
			patch.object(
				lock_order,
				"preallocate_stock_entry_counters",
				side_effect=lambda company, types, plan_for=None: planned.append(
					(company, list(types), plan_for.name)
				),
			),
		):
			pc_utils.create_material_receipt_for_certification(doc)
		return planned

	def test_u5_1_main_rows_only(self):
		planned = self._plan(
			[{"item_code": "M22", "tree_no": "TREE-A", "gross_weight": 60.0}]
		)
		self.assertEqual(
			planned,
			[("C01-COMPANY", ["Material Receipt for Certification"], "PC-RECEIVE-C01")],
		)

	def test_u5_2_repack_rows_only(self):
		planned = self._plan(
			[{"item_code": "ML22", "tree_no": "TREE-A", "gross_weight": 7.0}]
		)
		self.assertEqual(planned, [("C01-COMPANY", [RP], "PC-RECEIVE-C01")])

	def test_u5_3_both(self):
		planned = self._plan(
			[
				{"item_code": "M22", "tree_no": "TREE-A", "gross_weight": 60.0},
				{"item_code": "ML22", "tree_no": "TREE-A", "gross_weight": 40.0},
			]
		)
		self.assertEqual(
			planned,
			[
				(
					"C01-COMPANY",
					["Material Receipt for Certification", RP],
					"PC-RECEIVE-C01",
				)
			],
		)


# --------------------------------------------------------------------------- probe
class _FakeDB:
	"""Just enough of a Database for the probe to wrap, executing nothing."""

	_disable_transaction_control = 0

	def __init__(self):
		self.executed = []

	def sql(self, query, values=(), *args, **kwargs):
		self.executed.append(query)
		return ()

	def commit(self, *args, **kwargs):
		pass

	def rollback(self, *args, save_point=None, **kwargs):
		pass


BIN_LOCK = (
	"SELECT name FROM `tabBin` WHERE item_code = %s AND warehouse = %s FOR UPDATE"
)
SERIES_LOCK = "SELECT `current` FROM `tabSeries` WHERE `name`=%(param1)s FOR UPDATE"
RULE_LOCK = (
	"SELECT `counter` FROM `tabDocument Naming Rule` WHERE `name`=%(param1)s FOR UPDATE"
)


class TestNamingLockProbe(_C01TestCase):
	def _probe(self):
		return probe_mod.NamingLockProbe(db=_FakeDB())

	def test_p1_counter_after_bins_is_flagged_and_unknown(self):
		with self._probe() as probe:
			probe.db.sql(BIN_LOCK, ("I", "W"))
			probe.db.sql(SERIES_LOCK, {"param1": "C01PROBE-"})
		(event,) = probe.events
		self.assertEqual(
			(event.kind, event.key, event.after_bins), ("series", "C01PROBE-", True)
		)
		self.assertEqual(probe_mod.classify(event, set(), {"MAT-STE-"}), "unknown")
		with self.assertRaises(AssertionError):
			probe_mod.assert_naming_order(probe, "eir", [], set(), {"MAT-STE-"})

	def test_p2_prelocked_counter_is_one_early_event(self):
		with self._probe() as probe:
			probe.db.sql(
				"SELECT `current` FROM `tabSeries` WHERE name = %s FOR UPDATE",
				("C01PROBE2-",),
			)
			probe.db.sql(BIN_LOCK, ("I", "W"))
			probe.db.sql(SERIES_LOCK, {"param1": "C01PROBE2-"})
			probe.db.sql(
				"UPDATE `tabSeries` SET `current`=%(param1)s WHERE `name`=%(param2)s",
				{"param1": 5, "param2": "C01PROBE2-"},
			)
		self.assertEqual(
			[(e.key, e.after_bins) for e in probe.events], [("C01PROBE2-", False)]
		)
		out = probe_mod.assert_naming_order(
			probe, "eir", [("series", "C01PROBE2-")], set(), {"C01PROBE2-"}
		)
		self.assertEqual(out["pre_bins_se"], [("series", "C01PROBE2-")])

	def test_p3_rule_counter_after_bins_is_a_late_se_counter(self):
		with self._probe() as probe:
			probe.db.sql(
				"UPDATE `tabBin` SET actual_qty = 1 WHERE name = %s", ("BIN-1",)
			)
			probe.db.sql(RULE_LOCK, {"param1": "a0h37l6l3v"})
		(event,) = probe.events
		self.assertTrue(event.after_bins)
		self.assertEqual(probe_mod.classify(event, {"a0h37l6l3v"}, set()), "se-counter")
		with self.assertRaises(AssertionError):
			probe_mod.assert_naming_order(probe, "eir", [], {"a0h37l6l3v"}, set())

	def test_p4_nowait_acquisition_is_not_a_violation(self):
		with self._probe() as probe:
			probe.db.sql(BIN_LOCK, ("I", "W"))
			probe.db.sql(
				"SELECT `current` FROM `tabSeries` WHERE name = %s FOR UPDATE NOWAIT",
				("MAT-STE-",),
			)
		self.assertTrue(probe.events[0].nowait)
		probe_mod.assert_naming_order(probe, "eir", [], set(), {"MAT-STE-"})

	def test_p5_savepoint_rollback_keeps_state_full_rollback_resets(self):
		with self._probe() as probe:
			probe.db.sql(BIN_LOCK, ("I", "W"))
			probe.db.sql(SERIES_LOCK, {"param1": "K-"})
			probe.db.rollback(save_point="sp1")
			self.assertTrue(probe.bins_held)
			probe.db.sql(SERIES_LOCK, {"param1": "K-"})
			self.assertEqual(len(probe.events), 1)
			probe.db.rollback()
			self.assertFalse(probe.bins_held)
			probe.db.sql(SERIES_LOCK, {"param1": "K-"})
		self.assertEqual([e.after_bins for e in probe.events], [True, False])

	def test_p6_counter_update_after_its_lock_is_not_a_new_event(self):
		with self._probe() as probe:
			probe.db.sql(RULE_LOCK, {"param1": "a0h37l6l3v"})
			probe.db.sql(BIN_LOCK, ("I", "W"))
			probe.db.sql(
				"UPDATE `tabDocument Naming Rule` SET `counter`=%(param1)s,`modified`=%(param2)s "
				"WHERE `name`=%(param3)s",
				{"param1": 2, "param2": "x", "param3": "a0h37l6l3v"},
			)
		self.assertEqual(len(probe.events), 1)

	def test_p7_insert_and_positional_keys(self):
		with self._probe() as probe:
			probe.db.sql(BIN_LOCK, ("I", "W"))
			probe.db.sql(
				"insert into `tabSeries` (name, current) values (%s, 1)", ("NEW-",)
			)
			probe.db.sql(
				"update `tabSeries` set current = %s where name = %s", (3, "OTHER-")
			)
			probe.db.sql("SELECT * FROM `tabSeries` WHERE name = %s", ("PLAIN-",))
		self.assertEqual([e.key for e in probe.events], ["NEW-", "OTHER-"])

	def test_p8_probe_restores_the_connection(self):
		db = _FakeDB()
		original = db.sql
		with probe_mod.NamingLockProbe(db=db):
			self.assertNotEqual(db.sql, original)
		self.assertEqual(db.sql, original)
		self.assertNotIn("sql", db.__dict__)

	def test_p9_late_allowlist_by_stack(self):
		event = probe_mod.NamingEvent(
			seq=1,
			kind="series",
			key="KG2F075-",
			after_bins=True,
			nowait=False,
			sql="",
			stack=[("/x/erpnext/stock/doctype/batch/batch.py", 130, "autoname")],
		)
		self.assertEqual(probe_mod.classify(event, set(), set()), "batch-series")
		probe = self._probe()
		probe.events = [event]
		probe_mod.assert_naming_order(probe, "eir", [], set(), set())
