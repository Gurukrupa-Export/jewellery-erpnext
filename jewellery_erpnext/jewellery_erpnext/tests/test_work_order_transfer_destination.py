# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""A WORK ORDER transfer may put material in a worker's warehouse only for an operation issued to them.

``doc_events/stock_entry.validate_work_order_transfer_destination``. The incident is MAT-STE-72835
(kg-gk UAT, 2026-10-28): header Waxing RM -> Waxing WO, but its only row's target edited by hand to
``Casting KGJPL - 00294 WH - KGJPL``, a karigar's main-slip warehouse, for MOP-2610-ZFL5T8 -- Not
Started, issued to nobody. It submitted and posted 0.2 g into the karigar's warehouse.

TWO CLASSES
-----------
``TestDestinationRule`` runs anywhere, on the seedless world of ``tests/_current_operation_fixtures``.
Operations are issued and received through the REAL Employee IR controllers, and the rule is called
on in-memory Stock Entries, so it is judged against the operation state those controllers leave.

``TestBackendSubmission`` submits real Stock Entries through the whole hook chain -- ``insert`` and
``submit``, no browser handler -- and reads the Stock Ledger back. Stock needs a company with its
stock accounts and a Manufacturing Setting, so it runs only on a site that has one (a GK site); on a
bare test site it skips and says why. Its masters are prefixed and created inside the test.

Every test rolls the whole transaction back in ``tearDown``; ``frappe.db.commit`` is recorded and
must never be called, and RQ enqueues are recorded instead of reaching the bench workers.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from jewellery_erpnext.customer_subcontracting.sub_utils import cg_settle, snc
from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
	WorkOrderDestinationError,
	validate_work_order_transfer_destination,
)
from jewellery_erpnext.jewellery_erpnext.tests import _current_operation_fixtures as fx

WORK_ORDER_TRANSFER = "Material Transfer (WORK ORDER)"
HOOK = "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.validate_work_order_transfer_destination"
EOD_LOCK = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock.is_eod_sync_locked"
RECON_WINDOW = "jewellery_erpnext.jewellery_erpnext.stock_recon_window._enabled"


def _leaked_rows(prefix):
	"""``{doctype: n}`` of committed rows carrying ``prefix`` (items embed it after their family letter)."""
	leaked = {}
	for doctype, condition in (
		("Company", "name LIKE %(start)s"),
		("Department", "name LIKE %(start)s"),
		("Employee", "name LIKE %(start)s"),
		("Warehouse", "name LIKE %(start)s"),
		("Item", "name LIKE %(anywhere)s"),
		("Manufacturing Work Order", "name LIKE %(start)s"),
		(
			"Manufacturing Operation",
			"name LIKE %(start)s OR manufacturing_work_order LIKE %(start)s",
		),
		("Employee IR", "name LIKE %(start)s"),
		("MOP Log", "manufacturing_work_order LIKE %(start)s"),
		("Stock Entry", "manufacturing_work_order LIKE %(start)s"),
		("Stock Ledger Entry", "warehouse LIKE %(start)s"),
	):
		n = frappe.db.sql(
			f"SELECT COUNT(*) FROM `tab{doctype}` WHERE {condition}",
			{"start": f"{prefix}%", "anywhere": f"%{prefix}%"},
		)[0][0]
		if n:
			leaked[doctype] = n
	return leaked


class _RecordingQueue:
	def __init__(self, sink):
		self.sink = sink

	def enqueue_call(self, *args, **kwargs):
		self.sink.append((kwargs.get("kwargs") or {}).get("method"))


class _Harness(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.run_prefix = fx.new_prefix("TWOD")
		cls.count = 0

	@classmethod
	def tearDownClass(cls):
		# Every test rolls back and asserts nothing committed; this only proves it. fx.purge would
		# also clean up, but its whole-table LIKE scans take minutes on a site with real history.
		frappe.db.rollback()
		leaked = _leaked_rows(cls.run_prefix)
		super().tearDownClass()
		if leaked:
			raise AssertionError(f"{cls.__name__} committed rows: {leaked}")

	def setUp(self):
		super().setUp()
		type(self).count += 1
		self.prefix = f"{self.run_prefix}-{self.count:02d}"
		self.commits, self.enqueued = [], []
		for patcher in (
			patch.object(frappe.db, "commit", lambda *a, **k: self.commits.append(a)),
			patch(
				"frappe.utils.background_jobs.get_queue",
				lambda *a, **k: _RecordingQueue(self.enqueued),
			),
			patch(EOD_LOCK, return_value=False),
			patch(RECON_WINDOW, return_value=False),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		frappe.local.message_log = []

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.value_cache.clear()
		frappe.local.message_log = []
		super().tearDown()
		self.assertEqual(self.commits, [], "a cascade committed mid-transaction")
		self.assertEqual(self.enqueued, [], "a cascade enqueued a job")

	def assertRejected(self, fn, *args):
		with self.assertRaises(WorkOrderDestinationError) as ctx:
			fn(*args)
		return frappe.utils.strip_html(str(ctx.exception))


# ==============================================================================================
# the rule, against operation state left by the real Employee IR controllers
# ==============================================================================================


class TestDestinationRule(_Harness):
	def setUp(self):
		super().setUp()
		# A GK site carries DB-stored DocType-Event Server Scripts (Employee IR "Validate Employee",
		# the MOP stone-pcs totals) that raise ServerScriptNotEnabled where server scripts are off and
		# do not exist on a bare site. None of them sets an operation's status or employee, so the
		# world runs without them everywhere, as it does on the disposable site.
		patcher = patch("frappe.model.document.run_server_script_for_doc_event")
		patcher.start()
		self.addCleanup(patcher.stop)
		w = self.world = fx.make_world(self.prefix)
		self.addCleanup(fx.clear_caches, w)
		# A karigar's main-slip warehouse: theirs, but Raw Material -- what MAT-STE-72835 targeted.
		w.employee_msl = fx._insert(
			w,
			"Warehouse",
			{
				"name": f"{w.prefix} MM1 MSL - {w.abbr}",
				"warehouse_name": f"{w.prefix} MM1 MSL",
				"company": w.company,
				"employee": w.employees.MM1,
				"warehouse_type": "Raw Material",
				"is_group": 0,
				"disabled": 0,
			},
		)
		w.department_rm = fx._insert(
			w,
			"Warehouse",
			{
				"name": f"{w.prefix} Model Making RM - {w.abbr}",
				"warehouse_name": f"{w.prefix} Model Making RM",
				"company": w.company,
				"department": w.departments.MM,
				"warehouse_type": "Raw Material",
				"is_group": 0,
				"disabled": 0,
			},
		)
		self.mwo, self.mop = fx.make_work_order(w, "MM")

	def entry(
		self,
		*targets,
		mop=None,
		stock_entry_type=WORK_ORDER_TRANSFER,
		header_target=None,
	):
		"""An unsaved entry the way the form builds it: header routed by the operation, rows as given."""
		w, mop = self.world, mop or self.mop
		return frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": stock_entry_type,
				"purpose": "Material Transfer",
				"company": w.company,
				"manufacturing_work_order": self.mwo,
				"manufacturing_operation": mop,
				"from_warehouse": w.department_rm,
				"to_warehouse": header_target or w.department_warehouse.MM,
				"items": [
					{
						"item_code": w.item.metal,
						"qty": 0.2,
						"s_warehouse": w.department_rm,
						"t_warehouse": target,
						"manufacturing_operation": mop,
						"custom_manufacturing_work_order": self.mwo,
					}
					for target in targets
				],
			}
		)

	def check(self, se):
		validate_work_order_transfer_destination(se)

	# -- not issued ------------------------------------------------------------------------------

	def test_not_started_operation_to_its_department_warehouse_is_allowed(self):
		self.assertEqual(fx.operation(self.mop).status, "Not Started")
		self.check(self.entry(self.world.department_warehouse.MM))

	def test_not_started_operation_to_a_worker_warehouse_is_rejected(self):
		w = self.world
		message = self.assertRejected(self.check, self.entry(w.employee_warehouse.MM1))
		self.assertIn("Row #1", message)
		self.assertIn(self.mop, message)
		self.assertIn(w.employee_warehouse.MM1, message)
		self.assertIn("Not Started", message)
		# the destination it should have gone to, and the action that would make this one legal
		self.assertIn(w.department_warehouse.MM, message)
		self.assertIn("Employee IR", message)

	def test_the_incident_shape_is_rejected(self):
		"""Header routed correctly to the department, only the row edited to the karigar's MSL."""
		w = self.world
		se = self.entry(w.employee_msl, header_target=w.department_warehouse.MM)
		message = self.assertRejected(self.check, se)
		self.assertIn(w.employee_msl, message)

	def test_one_bad_row_among_several_is_rejected_and_named_alone(self):
		w = self.world
		dept = w.department_warehouse.MM
		message = self.assertRejected(
			self.check, self.entry(dept, w.employee_warehouse.MM1, dept)
		)
		self.assertIn("Row #2", message)
		self.assertNotIn("Row #1", message)
		self.assertNotIn("Row #3", message)

	def test_every_bad_row_is_reported(self):
		w = self.world
		message = self.assertRejected(
			self.check,
			self.entry(
				w.employee_warehouse.MM1, w.department_warehouse.MM, w.employee_msl
			),
		)
		self.assertIn("Row #1", message)
		self.assertIn("Row #3", message)

	def test_a_row_without_its_own_operation_is_judged_by_the_header_operation(self):
		se = self.entry(self.world.employee_warehouse.MM1)
		se.items[0].manufacturing_operation = None
		self.assertIn(self.mop, self.assertRejected(self.check, se))

	def test_a_worker_target_with_no_operation_anywhere_is_rejected(self):
		se = self.entry(self.world.employee_warehouse.MM1)
		se.manufacturing_operation = None
		se.items[0].manufacturing_operation = None
		self.assertIn("no Manufacturing Operation", self.assertRejected(self.check, se))

	# -- issued through the real Employee IR ------------------------------------------------------

	def test_issued_operation_to_the_assignees_warehouse_is_allowed(self):
		w = self.world
		fx.issue(w, self.mwo, department="MM", employee="MM1")
		op = fx.operation(self.mop)
		self.assertEqual((op.status, op.employee), ("WIP", w.employees.MM1))
		self.check(self.entry(w.employee_warehouse.MM1))

	def test_issued_operation_to_another_workers_warehouse_is_rejected(self):
		w = self.world
		fx.issue(w, self.mwo, department="MM", employee="MM1")
		message = self.assertRejected(self.check, self.entry(w.employee_warehouse.MM2))
		self.assertIn(w.employees.MM1, message)
		# where it should go: the assignee's own warehouse
		self.assertIn(w.employee_warehouse.MM1, message)

	def test_issued_operation_to_the_assignees_main_slip_warehouse_is_rejected(self):
		w = self.world
		fx.issue(w, self.mwo, department="MM", employee="MM1")
		message = self.assertRejected(self.check, self.entry(w.employee_msl))
		self.assertIn("not their Manufacturing warehouse", message)
		self.assertIn(w.employee_warehouse.MM1, message)

	def test_issued_operation_to_its_department_warehouse_is_still_allowed(self):
		"""The Employee IR receive injection books a still-WIP operation's metal into the department."""
		w = self.world
		fx.issue(w, self.mwo, department="MM", employee="MM1")
		self.check(self.entry(w.department_warehouse.MM))

	def test_a_draft_built_while_issued_is_rejected_once_the_operation_is_received(
		self,
	):
		w = self.world
		fx.issue(w, self.mwo, department="MM", employee="MM1")
		se = self.entry(w.employee_warehouse.MM1)
		self.check(se)
		fx.receive(w, self.mwo, department="MM", employee="MM1")
		self.assertEqual(fx.operation(self.mop).status, "Finished")
		self.assertIn("Finished", self.assertRejected(self.check, se))
		# and the successor the receive minted is Not Started, so it cannot go there either
		successor = fx.pointer(self.mwo)
		self.assertNotEqual(successor, self.mop)
		self.assertRejected(
			self.check, self.entry(w.employee_warehouse.MM1, mop=successor)
		)
		self.check(self.entry(w.department_warehouse.MM, mop=successor))

	# -- subcontracting --------------------------------------------------------------------------

	def test_subcontracted_operation_to_its_subcontractor_is_allowed_and_to_another_is_not(
		self,
	):
		w = self.world
		frappe.db.set_value(
			"Manufacturing Operation",
			self.mop,
			{
				"status": "WIP",
				"for_subcontracting": 1,
				"subcontractor": w.subcontractors.SC1,
			},
			update_modified=False,
		)
		self.check(self.entry(w.subcontractor_warehouse.SC1))
		self.assertRejected(self.check, self.entry(w.employee_warehouse.MM1))

	def test_unissued_operation_to_a_subcontractor_warehouse_is_rejected(self):
		self.assertRejected(
			self.check, self.entry(self.world.subcontractor_warehouse.SC1)
		)

	# -- scope -----------------------------------------------------------------------------------

	def test_other_entry_types_are_not_judged(self):
		w = self.world
		for stock_entry_type in (
			"Material Transfer",
			"Material Transfer to Department",
			"Material Receive (WORK ORDER)",
		):
			with self.subTest(stock_entry_type=stock_entry_type):
				self.check(
					self.entry(w.employee_msl, stock_entry_type=stock_entry_type)
				)

	def test_auto_created_entries_are_judged_too(self):
		se = self.entry(self.world.employee_warehouse.MM1)
		se.auto_created = 1
		self.assertRejected(self.check, se)

	def test_the_withdrawn_stock_flag_lets_an_equal_weight_swap_through(self):
		se = self.entry(self.world.employee_warehouse.MM1)
		se.flags.restores_withdrawn_stock = True
		self.check(se)

	def test_the_flag_cannot_arrive_from_a_request_payload(self):
		se = frappe.get_doc(
			{
				**self.entry(self.world.employee_warehouse.MM1).as_dict(),
				"flags": {"restores_withdrawn_stock": True},
			}
		)
		self.assertFalse(se.flags.get("restores_withdrawn_stock"))
		self.assertRejected(self.check, se)

	def test_snc_and_customer_gold_settle_swaps_set_the_flag(self):
		w = self.world
		seen = []

		def capture(doc, *args, **kwargs):
			seen.append(
				(doc.stock_entry_type, doc.flags.get("restores_withdrawn_stock"))
			)
			return doc

		with (
			patch(
				"frappe.model.document.Document.insert",
				autospec=True,
				side_effect=capture,
			),
			patch.object(snc, "_submit_consuming_stock_entry"),
			patch.object(cg_settle, "_submit_consuming_stock_entry"),
		):
			snc.create_material_transfer_work_order(
				frappe._dict(
					name=self.mwo,
					manufacturing_order=None,
					manufacturing_operation=self.mop,
				),
				frappe._dict(company=w.company, branch=None),
				[
					{
						"item_code": w.item.metal,
						"qty": 1,
						"custom_pure_qty": 1,
						"batch_no": None,
						"s_warehouse": w.department_rm,
						"t_warehouse": w.employee_warehouse.MM1,
					}
				],
			)
			cg_settle._work_order_transfer(
				frappe._dict(name=None, company=w.company, branch=None),
				frappe._dict(
					manufacturing_order=None,
					work_order=self.mwo,
					operation=self.mop,
					item_code=w.item.metal,
				),
				1,
				None,
				w.department_rm,
				w.employee_warehouse.MM1,
				None,
			)
		self.assertEqual(
			seen, [(WORK_ORDER_TRANSFER, True), (WORK_ORDER_TRANSFER, True)]
		)

	def test_the_rule_runs_at_validate_after_the_operation_pairing_check(self):
		validate = frappe.get_hooks("doc_events")["Stock Entry"]["validate"]
		self.assertIn(HOOK, validate)
		self.assertGreater(
			validate.index(HOOK),
			validate.index(
				"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.validate_mwo_mop_consistency"
			),
		)


# ==============================================================================================
# real Stock Entries through the whole hook chain
# ==============================================================================================


class TestBackendSubmission(_Harness):
	"""``insert`` + ``submit`` as the API, a script or the Submission Queue worker would do them."""

	def setUp(self):
		super().setUp()
		setting = frappe.db.get_value(
			"Manufacturing Setting",
			{"company": ["is", "set"]},
			["company", "manufacturer"],
			as_dict=True,
		)
		company = setting and frappe.get_cached_doc("Company", setting.company)
		if not (company and company.default_inventory_account and company.cost_center):
			self.skipTest(
				"needs a company with stock accounts and a Manufacturing Setting"
			)
		self.company, self.manufacturer = company.name, setting.manufacturer
		frappe.set_user("Administrator")
		self.build_masters(company.abbr)

	def insert(self, doctype, **values):
		doc = frappe.get_doc({"doctype": doctype, **values})
		doc.db_insert()
		return doc.name

	def warehouse(self, label, warehouse_type, **owner):
		return (
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": f"{self.prefix} {label}",
					"company": self.company,
					"parent_warehouse": self.root_warehouse,
					"warehouse_type": warehouse_type,
					**owner,
				}
			)
			.insert()
			.name
		)

	def build_masters(self, abbr):
		p = self.prefix
		self.root_warehouse = frappe.db.get_value(
			"Warehouse", {"company": self.company, "is_group": 1}, order_by="lft asc"
		)
		department = self.insert(
			"Department",
			name=f"{p} Dept - {abbr}",
			department_name=f"{p} Dept",
			company=self.company,
			is_group=0,
		)
		self.employees = {
			key: self.insert(
				"Employee",
				name=f"{p}-{key}",
				first_name=f"{p} {key}",
				employee_name=f"{p} {key}",
				company=self.company,
				department=department,
				status="Active",
				gender="Male",
				date_of_birth="1990-01-01",
				date_of_joining="2020-01-01",
			)
			for key in ("A", "B")
		}
		self.rm = self.warehouse("RM", "Raw Material", department=department)
		self.wo = self.warehouse("WO", "Manufacturing", department=department)
		self.worker = {
			key: self.warehouse(f"{key} WIP", "Manufacturing", employee=employee)
			for key, employee in self.employees.items()
		}
		self.item = self.insert(
			"Item",
			name=f"{p}-ITEM",
			item_code=f"{p}-ITEM",
			item_name=f"{p}-ITEM",
			is_stock_item=1,
			stock_uom="Gram",
			item_group=frappe.db.get_value("Item Group", {"is_group": 0}),
		)
		self.mwo = self.insert(
			"Manufacturing Work Order",
			name=f"{p}-MWO",
			docstatus=1,
			company=self.company,
			manufacturer=self.manufacturer,
			department=department,
			status="Not Started",
			posting_date=nowdate(),
			qty=1,
		)
		self.mop = self.insert(
			"Manufacturing Operation",
			name=f"{p}-MOP",
			company=self.company,
			manufacturer=self.manufacturer,
			department=department,
			manufacturing_work_order=self.mwo,
			status="Not Started",
			type="Manufacturing Work Order",
			qty=1,
		)
		frappe.db.set_value(
			"Manufacturing Work Order",
			self.mwo,
			"manufacturing_operation",
			self.mop,
			update_modified=False,
		)
		receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"manufacturer": self.manufacturer,
				"items": [
					{
						"item_code": self.item,
						"qty": 10,
						"basic_rate": 100,
						"t_warehouse": self.rm,
						"uom": "Gram",
						"conversion_factor": 1,
					}
				],
			}
		)
		receipt.insert()
		receipt.submit()

	def transfer(self, *targets):
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": WORK_ORDER_TRANSFER,
				"company": self.company,
				"manufacturer": self.manufacturer,
				"manufacturing_work_order": self.mwo,
				"manufacturing_operation": self.mop,
				"from_warehouse": self.rm,
				"to_warehouse": self.wo,
				"items": [
					{
						"item_code": self.item,
						"qty": 1,
						"s_warehouse": self.rm,
						"t_warehouse": target,
						"uom": "Gram",
						"conversion_factor": 1,
						"manufacturing_operation": self.mop,
						"custom_manufacturing_work_order": self.mwo,
					}
					for target in targets
				],
			}
		)
		# The work-order reservation bridge runs at on_submit, after this rule, and needs a Sales
		# Order line this world does not have. It is the producers' own opt-out; the rule ignores it.
		se.flags.skip_mwo_reservation = True
		return se

	def issue_to(self, key):
		frappe.db.set_value(
			"Manufacturing Operation",
			self.mop,
			{"status": "WIP", "employee": self.employees[key]},
			update_modified=False,
		)

	def ledger(self, name):
		return [
			list(row)
			for row in frappe.get_all(
				"Stock Ledger Entry",
				filters={"voucher_no": name, "is_cancelled": 0},
				fields=["warehouse", "actual_qty"],
				order_by="actual_qty asc",
				as_list=True,
			)
		]

	def assertNothingPosted(self, name):
		self.assertEqual(self.ledger(name), [])
		self.assertFalse(
			frappe.db.exists("GL Entry", {"voucher_no": name, "is_cancelled": 0})
		)

	def test_department_warehouse_for_a_not_started_operation_submits(self):
		se = self.transfer(self.wo)
		se.insert()
		se.submit()
		self.assertEqual(se.docstatus, 1)
		self.assertEqual(self.ledger(se.name), [[self.rm, -1.0], [self.wo, 1.0]])

	def test_worker_warehouse_for_a_not_started_operation_is_never_stored(self):
		se = self.transfer(self.worker["A"])
		self.assertRejected(se.insert)
		self.assertFalse(
			frappe.db.exists("Stock Entry", {"manufacturing_operation": self.mop})
		)
		self.assertFalse(
			frappe.db.exists("Stock Ledger Entry", {"warehouse": self.worker["A"]})
		)

	def test_one_bad_row_rejects_the_whole_entry(self):
		se = self.transfer(self.wo, self.worker["A"], self.wo)
		self.assertIn("Row #2", self.assertRejected(se.insert))
		self.assertFalse(
			frappe.db.exists("Stock Entry", {"manufacturing_operation": self.mop})
		)

	def test_issued_operation_submits_to_its_worker_only(self):
		self.issue_to("A")
		self.assertRejected(self.transfer(self.worker["B"]).insert)
		se = self.transfer(self.worker["A"])
		se.insert()
		se.submit()
		self.assertEqual(
			self.ledger(se.name), [[self.rm, -1.0], [self.worker["A"], 1.0]]
		)

	def test_a_draft_saved_while_issued_cannot_submit_after_the_operation_moves(self):
		self.issue_to("A")
		se = self.transfer(self.worker["A"])
		se.insert()
		self.assertEqual(se.docstatus, 0)
		# re-issued to someone else after the draft was saved
		self.issue_to("B")
		draft = frappe.get_doc("Stock Entry", se.name)
		draft.flags.skip_mwo_reservation = True
		message = self.assertRejected(draft.submit)
		self.assertIn(self.employees["B"], message)
		self.assertEqual(frappe.db.get_value("Stock Entry", se.name, "docstatus"), 0)
		self.assertNothingPosted(se.name)

	def test_a_draft_saved_while_issued_cannot_submit_once_back_to_not_started(self):
		self.issue_to("A")
		se = self.transfer(self.worker["A"])
		se.insert()
		frappe.db.set_value(
			"Manufacturing Operation",
			self.mop,
			{"status": "Not Started", "employee": None},
			update_modified=False,
		)
		draft = frappe.get_doc("Stock Entry", se.name)
		draft.flags.skip_mwo_reservation = True
		self.assertIn("Not Started", self.assertRejected(draft.submit))
		self.assertEqual(frappe.db.get_value("Stock Entry", se.name, "docstatus"), 0)
		self.assertNothingPosted(se.name)
