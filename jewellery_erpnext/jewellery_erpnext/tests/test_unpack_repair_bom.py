"""
Pure-logic unit tests for the repair-unpack BOM resolver
(ManufacturingWorkOrder._resolve_repair_order_bom).

Run with:
  bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.tests.test_unpack_repair_bom

Note: frappe.db is a LocalProxy, so a default patch() mints async-mock children.
We inject an explicit MagicMock via patch(target, obj).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	ManufacturingWorkOrder,
)

MOD = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order"


def _pinned_rounding():
	"""Pin the rounding method for the duration of an unpack.

	Each target row's qty is ``flt(..., 3)``, and a precision makes flt call rounded() ->
	frappe.get_system_settings("rounding_method"). A process that has not loaded System
	Settings yet -- CI boots every module in a fresh one -- resolves it through
	frappe.client_cache -> frappe.get_doc, which _unpack has replaced with new_se, and
	frappe.db, which is a MagicMock there. The lookup raises, flt swallows it and returns
	0.0, so every unpacked qty is 0 and the rates are never scaled. A dev session whose
	cache is already warm hides all of it.

	"Banker's Rounding" is what the production site uses; no value in this suite sits on a
	tie, so every valid method gives the same answers.
	"""
	return patch.object(frappe, "get_system_settings", return_value="Banker's Rounding")


def _resolve(pmo_value, ro_bom, requested_fields=None):
	"""Call _resolve_repair_order_bom with frappe.db.get_value mocked.

	pmo_value: what get_value returns for the PMO row -- (order_form_type, order_form_id) or None.
	ro_bom   : what get_value returns for the Repair Order's BOM field (the design BOM).
	requested_fields: optional list the caller can inspect to assert WHICH Repair Order field
	    was read (guards against regressing to new_bom).
	"""

	def gv(dt, name, fieldname):
		if dt == "Parent Manufacturing Order":
			return pmo_value
		if dt == "Repair Order":
			if requested_fields is not None:
				requested_fields.append(fieldname)
			return ro_bom
		return None

	mock_db = MagicMock()
	mock_db.get_value.side_effect = gv
	fake_self = SimpleNamespace(manufacturing_order="PMO-1")
	with patch(f"{MOD}.frappe.db", mock_db):
		return ManufacturingWorkOrder._resolve_repair_order_bom(fake_self)


class TestResolveRepairOrderBom(IntegrationTestCase):
	def test_returns_new_bom_for_repair_order(self):
		bom, pmo = _resolve(("Repair Order", "RO-1"), "BOM-X")
		self.assertEqual(bom, "BOM-X")
		self.assertEqual(pmo, "PMO-1")

	def test_reads_the_design_bom_field_not_new_bom(self):
		"""new_bom is only ever written when required_design == 'No', so a Manual/CAD
		repair never has one. Requiring it blocked the unpack outright -- the resolver
		must read the design ``bom`` link instead."""
		asked = []
		_resolve(("Repair Order", "RO-1"), "BOM-X", requested_fields=asked)
		self.assertEqual(asked, ["bom"])

	def test_resolves_when_repair_order_has_no_new_bom(self):
		# The Manual-design case: new_bom absent is irrelevant, only `bom` is consulted.
		bom, _ = _resolve(("Repair Order", "ORD/RO/00021-2"), "BOM-EA02216-001-001")
		self.assertEqual(bom, "BOM-EA02216-001-001")

	def test_throws_when_order_form_is_not_repair_order(self):
		# order_form_type is "Order", not "Repair Order".
		with self.assertRaises(frappe.ValidationError):
			_resolve(("Order", "ORD-1"), "BOM-X")

	def test_throws_when_no_order_form_id(self):
		with self.assertRaises(frappe.ValidationError):
			_resolve(("Repair Order", None), "BOM-X")

	def test_throws_when_pmo_missing(self):
		# get_value returns None (PMO row not found) -> (None, None) -> not a Repair Order.
		with self.assertRaises(frappe.ValidationError):
			_resolve(None, "BOM-X")

	def test_throws_when_repair_order_has_no_bom(self):
		with self.assertRaises(frappe.ValidationError):
			_resolve(("Repair Order", "RO-1"), None)


def _check_dept(sn_dept, mwo_dept):
	"""Run _assert_serial_in_department with Warehouse.department mocked to sn_dept."""
	mock_db = MagicMock()
	mock_db.get_value.return_value = sn_dept
	fake_self = SimpleNamespace(department=mwo_dept, serial_no="SN1")
	with patch(f"{MOD}.frappe.db", mock_db):
		ManufacturingWorkOrder._assert_serial_in_department(fake_self, "WH-1")


class TestAssertSerialInDepartment(IntegrationTestCase):
	def test_matching_department_ok(self):
		_check_dept("Dept A", "Dept A")  # no throw

	def test_mismatched_department_throws(self):
		with self.assertRaises(frappe.ValidationError):
			_check_dept("Dept A", "Dept B")

	def test_empty_string_and_none_are_equal(self):
		# "" (warehouse) and None (MWO) normalize together -> allowed.
		_check_dept("", None)
		_check_dept(None, None)

	def test_serial_wh_without_department_but_mwo_has_throws(self):
		with self.assertRaises(frappe.ValidationError):
			_check_dept(None, "Dept A")


def _check_bookable(item_tracking):
	"""Run _assert_components_bookable with Item (has_serial_no, serial_no_series) mocked.

	item_tracking: dict item_code -> (has_serial_no, serial_no_series).
	"""

	def gv(dt, name, fieldname):
		return item_tracking.get(name)

	mock_db = MagicMock()
	mock_db.get_value.side_effect = gv
	fake_self = SimpleNamespace()
	with patch(f"{MOD}.frappe.db", mock_db):
		ManufacturingWorkOrder._assert_components_bookable(
			fake_self, list(item_tracking.keys()), "BOM-X"
		)


class TestAssertComponentsBookable(IntegrationTestCase):
	def test_batch_and_seriesed_serial_items_ok(self):
		# batch item (0, None) and serial item WITH a series -> allowed.
		_check_bookable({"BATCH-ITEM": (0, None), "SER-ITEM": (1, "SER-.###")})

	def test_serial_item_without_series_throws(self):
		with self.assertRaises(frappe.ValidationError):
			_check_bookable({"M-METAL": (0, None), "STONE-DV": (1, None)})

	def test_empty_bom_ok(self):
		_check_bookable({})


# ---------------------------------------------------------------------------
# The unpack reads the SERIAL's own BOM, rates it and places it by MOP state
# ---------------------------------------------------------------------------
class _FakeUnpackSE:
	"""Captures the Repair Unpack Stock Entry the method builds.

	``save`` mimics ERPNext valuing the serial on the first save: the source row takes
	``source_rate`` then.
	"""

	name = "STE-UNPACK-1"

	def __init__(self, doc, source_rate):
		self.doc = doc
		self.items = []
		self.submitted = False
		self.saves = 0
		self._source_rate = source_rate

	def append(self, _table, row):
		item = frappe._dict(row)
		self.items.append(item)
		return item

	def save(self):
		self.saves += 1
		for row in self.items:
			if row.get("s_warehouse") and not row.get("basic_rate"):
				row.basic_rate = self._source_rate

	def submit(self):
		self.submitted = True


def _unpack(
	serial_bom="BOM-SERIAL",
	serial_wh="RM-WH - X",
	mop=None,
	warehouses=None,
	components=None,
	creation_rows=None,
	last_purchase_rate=0,
	source_rate=1000.0,
	qty=1,
):
	"""Run create_unpack_serial_no_stock_entry with every read mocked.

	``mop`` is the work order's current operation (status/employee/department);
	``warehouses`` maps ("department"|"employee", name) to the Manufacturing warehouse.
	Returns (se, components_bom, master_bom_writes).
	"""
	seen = {}
	mop = {"status": "Not Started", "employee": None, "department": "Dept"} if mop is None else mop
	if warehouses is None:
		warehouses = {("department", "Dept"): "MFG-WH - X"}
	fake = SimpleNamespace(
		name="MWO-1",
		manufacturing_order="PMO-1",
		manufacturing_operation="MOP-1",
		serial_no="SN-1",
		item_code="EA02144-006",
		qty=qty,
		docstatus=1,
		department="Dept",
		company="C",
		branch="B",
		manufacturer="M",
	)
	fake.find_existing_unpack_entry = lambda: None
	fake._assert_serial_matches_item = lambda: None
	fake._assert_serial_in_department = lambda wh: None
	fake._assert_components_bookable = lambda *a, **k: None
	fake._resolve_repair_order_bom = lambda: ("BOM-DESIGN", "PMO-1")
	fake._resolve_serial_bom = lambda: ManufacturingWorkOrder._resolve_serial_bom(fake)
	fake._resolve_unpack_target_warehouse = (
		lambda: ManufacturingWorkOrder._resolve_unpack_target_warehouse(fake)
	)
	fake._unpack_component_rates = lambda items: ManufacturingWorkOrder._unpack_component_rates(
		fake, items
	)
	fake._rate_unpacked_components = lambda se: ManufacturingWorkOrder._rate_unpacked_components(
		fake, se
	)
	fake.db_set = lambda *a, **k: None

	def resolve_components(pmo_name, bom):
		seen["bom"] = bom
		return (
			components or [{"item_code": "M-G-18KT", "qty": 6.0, "pcs": 0}],
			7.91,
			1.0,
		)

	fake._resolve_full_repair_components = resolve_components

	def gv(doctype, name=None, fieldname=None, *a, **k):
		if doctype == "Parent Manufacturing Order":
			return "Repair"
		if doctype == "Manufacturing Operation":
			return frappe._dict(mop)
		if doctype == "Serial No":
			return {"custom_bom_no": serial_bom, "warehouse": serial_wh}.get(fieldname)
		if doctype == "Item":
			if fieldname == "last_purchase_rate":
				return last_purchase_rate
			return (0, None)
		if doctype == "Warehouse":
			for key in ("department", "employee"):
				if name.get(key) is not None:
					return warehouses.get((key, name[key]))
		return None

	def get_all(doctype, filters=None, fields=None, **k):
		if doctype == "Stock Entry Detail":
			return [frappe._dict(r) for r in (creation_rows or [])]
		return []

	mock_db = MagicMock()
	mock_db.get_value.side_effect = gv
	mock_db.get_all.side_effect = get_all
	mock_db.sql.return_value = [("STE-CREATE",)] if creation_rows else []
	created = []

	def new_se(doc):
		se = _FakeUnpackSE(doc, source_rate)
		created.append(se)
		return se

	with (
		patch(f"{MOD}.frappe.db", mock_db),
		patch(f"{MOD}.frappe.get_doc", side_effect=new_se),
		patch(
			f"{MOD}._resolve_unpack_inventory",
			return_value=("Regular Stock", None, "CUST-1"),
		),
		_pinned_rounding(),
	):
		# Fail here, naming the cause, rather than as a misleading 0.0 != 2.0 below.
		assert (
			flt(1.0, 3) == 1.0
		), "flt is not rounding under _unpack's mocks (see _pinned_rounding)"
		ManufacturingWorkOrder.create_unpack_serial_no_stock_entry(fake)

	master_writes = [
		c.args for c in mock_db.set_value.call_args_list if c.args[2] == "master_bom"
	]
	return created[0], seen.get("bom"), master_writes


def _targets(se):
	return [r for r in se.items if r.get("t_warehouse")]


class TestUnpackUsesTheSerialsOwnBom(IntegrationTestCase):
	def test_components_come_from_the_serials_bom_not_the_design_bom(self):
		se, components_bom, _writes = _unpack(serial_bom="BOM-SERIAL")
		self.assertEqual(components_bom, "BOM-SERIAL")
		self.assertTrue(se.submitted)

	def test_the_design_bom_is_still_persisted_as_master_bom(self):
		# master_bom drives repair pricing/quotation; only the unpacked components changed.
		_se, _bom, writes = _unpack()
		self.assertEqual(
			writes,
			[("Parent Manufacturing Order", "PMO-1", "master_bom", "BOM-DESIGN")],
		)

	def test_a_serial_without_a_bom_stops_the_unpack_and_builds_nothing(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			_unpack(serial_bom=None)
		self.assertIn("SN-1", str(ctx.exception))

	def test_resolve_serial_bom_reads_custom_bom_no(self):
		asked = []

		def gv(doctype, name, fieldname):
			asked.append((doctype, name, fieldname))
			return "BOM-SERIAL"

		mock_db = MagicMock()
		mock_db.get_value.side_effect = gv
		fake = SimpleNamespace(serial_no="SN-1")
		with patch(f"{MOD}.frappe.db", mock_db):
			self.assertEqual(
				ManufacturingWorkOrder._resolve_serial_bom(fake), "BOM-SERIAL"
			)
		self.assertEqual(asked, [("Serial No", "SN-1", "custom_bom_no")])


class TestUnpackWarehouses(IntegrationTestCase):
	def test_the_source_is_the_serials_own_warehouse(self):
		se, _bom, _writes = _unpack(serial_wh="TRANSIT-WH - X")
		source = [r for r in se.items if r.get("s_warehouse")]
		self.assertEqual([r.s_warehouse for r in source], ["TRANSIT-WH - X"])

	def test_a_not_started_operation_takes_the_department_warehouse(self):
		se, _bom, _writes = _unpack(
			mop={"status": "Not Started", "employee": None, "department": "Dept"},
			warehouses={("department", "Dept"): "MFG-WH - X"},
		)
		self.assertEqual({r.t_warehouse for r in _targets(se)}, {"MFG-WH - X"})

	def test_a_wip_operation_takes_the_employees_warehouse(self):
		se, _bom, _writes = _unpack(
			mop={"status": "WIP", "employee": "EMP-1", "department": "Dept"},
			warehouses={
				("department", "Dept"): "MFG-WH - X",
				("employee", "EMP-1"): "EMP-WH - X",
			},
		)
		self.assertEqual({r.t_warehouse for r in _targets(se)}, {"EMP-WH - X"})

	def test_a_wip_operation_without_an_employee_falls_back_to_the_department(self):
		se, _bom, _writes = _unpack(
			mop={"status": "WIP", "employee": None, "department": "Dept"},
		)
		self.assertEqual({r.t_warehouse for r in _targets(se)}, {"MFG-WH - X"})

	def test_a_wip_employee_with_no_warehouse_stops_the_unpack(self):
		with self.assertRaises(frappe.ValidationError):
			_unpack(
				mop={"status": "WIP", "employee": "EMP-1", "department": "Dept"},
				warehouses={("department", "Dept"): "MFG-WH - X"},
			)

	def test_no_department_warehouse_stops_the_unpack(self):
		with self.assertRaises(frappe.ValidationError):
			_unpack(warehouses={})


	def test_the_departments_manufacturing_warehouse_is_not_used(self):
		se, _bom, _writes = _unpack(serial_wh="RM-WH - X", dept_wh="MFG-WH - X")
		self.assertNotIn("MFG-WH - X", {r.get("t_warehouse") for r in se.items})
class TestUnpackRates(IntegrationTestCase):
	COMPONENTS = [
		{"item_code": "METAL", "qty": 10.0, "pcs": 0},
		{"item_code": "DIA", "qty": 2.0, "pcs": 0},
	]
	# What the serial's creation entry consumed: 10 g at 100 and 2 ct at 500 = 2000.
	CREATION = [
		{"item_code": "METAL", "qty": 10.0, "basic_rate": 100.0},
		{"item_code": "DIA", "qty": 2.0, "basic_rate": 500.0},
	]

	def _rates(self, **kw):
		se, _bom, _w = _unpack(components=self.COMPONENTS, **kw)
		return {r.item_code: r.basic_rate for r in _targets(se)}, se

	def test_rates_come_from_the_serials_creation_entry(self):
		rates, _se = self._rates(creation_rows=self.CREATION, source_rate=2000.0)
		self.assertEqual(rates, {"METAL": 100.0, "DIA": 500.0})

	def test_rates_are_scaled_to_the_value_of_the_serial(self):
		# The piece is worth 2200 (2000 of materials plus 200 of costs): every component
		# is scaled by the same factor so nothing is left for the Difference Account.
		rates, se = self._rates(creation_rows=self.CREATION, source_rate=2200.0)
		self.assertEqual(
			round(sum(r.basic_rate * r.qty for r in _targets(se)), 2), 2200.0
		)
		self.assertAlmostEqual(rates["METAL"] / rates["DIA"], 100.0 / 500.0, places=3)

	def test_no_component_is_left_at_zero_when_the_creation_entry_is_known(self):
		rates, _se = self._rates(creation_rows=self.CREATION, source_rate=2200.0)
		self.assertTrue(all(rate > 0 for rate in rates.values()))

	def test_an_item_the_creation_entry_lacks_uses_its_last_purchase_rate(self):
		creation = [{"item_code": "METAL", "qty": 10.0, "basic_rate": 100.0}]
		rates, _se = self._rates(
			creation_rows=creation, last_purchase_rate=500.0, source_rate=2000.0
		)
		self.assertEqual(rates, {"METAL": 100.0, "DIA": 500.0})

	def test_the_entry_is_saved_twice_so_the_serial_is_valued_before_re_rating(self):
		_rates, se = self._rates(creation_rows=self.CREATION, source_rate=2000.0)
		self.assertEqual(se.saves, 2)
		self.assertTrue(se.submitted)

	def test_rows_stay_manual_rated(self):
		_rates, se = self._rates(creation_rows=self.CREATION, source_rate=2000.0)
		self.assertTrue(all(r.set_basic_rate_manually == 1 for r in _targets(se)))


class TestUnpackPieceCounts(IntegrationTestCase):
	def test_a_stone_row_carries_the_boms_piece_count(self):
		se, _bom, _w = _unpack(
			components=[
				{"item_code": "DIA-A", "qty": 0.983, "pcs": 35},
				{"item_code": "DIA-B", "qty": 0.676, "pcs": 20},
			]
		)
		self.assertEqual({r.item_code: r.pcs for r in _targets(se)}, {"DIA-A": 35, "DIA-B": 20})

	def test_pieces_scale_with_the_work_order_quantity(self):
		# The BOM describes ONE piece; the entry books qty * BOM, like the carats.
		se, _bom, _w = _unpack(
			components=[{"item_code": "DIA-A", "qty": 1.0, "pcs": 35}], qty=2
		)
		self.assertEqual(_targets(se)[0].pcs, 70)
		self.assertEqual(_targets(se)[0].qty, 2.0)

	def test_a_row_without_pieces_keeps_the_fields_default(self):
		se, _bom, _w = _unpack(components=[{"item_code": "METAL", "qty": 6.0, "pcs": 0}])
		self.assertNotIn("pcs", _targets(se)[0])
