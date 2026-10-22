# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""The order's customer stamped on the finished-good Serial No a Serial Number Creator makes.

update_new_serial_no takes the serial the SNC's Manufacture entry has just created (the stock
ledger leaves its customer empty), stamps the Parent Manufacturing Order's customer on it, adds
the order's HUID rows and the stamping number, and saves it.

ERPNext 16.52 (frappe/erpnext#60162) refuses a save() that changes a Serial No's customer, status,
warehouse, ...: only the stock ledger may change them. The serial below is created by a real
Material Receipt, so it is in exactly the state the Manufacture entry leaves it in, and every save
goes through ERPNext's own validate().

Run: bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_snc_serial_customer_stamp
"""

from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

import frappe
from erpnext.stock.doctype.serial_no import serial_no as erpnext_serial_no
from frappe.tests import IntegrationTestCase
from frappe.utils import today

from jewellery_erpnext.jewellery_erpnext.doc_events.serial_no import STAMPING_NO_FIELD
from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import (
	update_new_serial_no,
)

_SERVER_SCRIPT_MAP = (
	"frappe.core.doctype.server_script.server_script_utils.get_server_script_map"
)
_SAVEPOINT = "snc_serial_customer_test"

PREFIX = "_Test SNC"
HUID = "TSNC01"

# ERPNext 16.52 introduced the managed-field check; the control test has nothing to observe before it.
HAS_MANAGED_FIELDS = "customer" in getattr(
	erpnext_serial_no, "STOCK_MANAGED_FIELDS", ()
)


class TestSerialNumberCreatorStampsTheOrdersCustomer(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# super() keeps IntegrationTestCase's class-level rollback: every fixture below is undone
		# when the class finishes, even on a site with real data.
		super().setUpClass()
		cls.addClassCleanup(frappe.clear_cache)

		# Server Scripts are a site's own customisations, not this app's (e.g. one on Item, and one
		# on Serial No that only fills custom_bom_no). A site that has some but runs with server
		# scripts switched off refuses every save of those doctypes, so they are left out here.
		server_scripts = patch(_SERVER_SCRIPT_MAP, return_value={})
		server_scripts.start()
		cls.addClassCleanup(server_scripts.stop)

		cls.company = _company()
		cls.customer = frappe.db.get_value("Customer", {"disabled": 0}, "name")
		cls.warehouse = _warehouse("FG", cls.company)
		cls.item = _serialized_item()
		cls.pmo_with_customer = _parent_manufacturing_order(
			"A", cls.customer, huid=HUID
		)
		cls.pmo_without_customer = _parent_manufacturing_order("B", None)

	def setUp(self):
		super().setUp()
		frappe.db.savepoint(_SAVEPOINT)
		self.addCleanup(frappe.db.rollback, save_point=_SAVEPOINT)

	def _new_serial(self):
		"""A finished-good serial created by the stock ledger, as the Manufacture entry creates it."""
		se = _receive(self.company, self.warehouse, self.item)
		serial_no = frappe.db.get_value(
			"Serial No",
			{"item_code": self.item, "purchase_document_no": se.name},
			"name",
		) or frappe.db.get_value(
			"Serial No", {"item_code": self.item}, "name", order_by="creation desc"
		)
		self.assertTrue(serial_no)
		self.assertFalse(frappe.db.get_value("Serial No", serial_no, "customer"))
		return serial_no

	def _run_snc(self, serial_no, pmo):
		update_new_serial_no(
			SimpleNamespace(
				fg_serial_no=serial_no,
				parent_manufacturing_order=pmo,
				serial_no=None,
				fg_details=[],
				manufacturing_work_order=None,
				design_id_bom=None,
			)
		)
		return frappe.get_doc("Serial No", serial_no)

	def test_the_orders_customer_is_stamped_on_the_new_serial(self):
		"""The serial gets the order's customer; HUID rows and the stamping number still go in."""
		serial = self._run_snc(self._new_serial(), self.pmo_with_customer)

		self.assertEqual(serial.customer, self.customer)
		self.assertIn(HUID, [row.huid for row in serial.huid])
		if serial.meta.has_field(STAMPING_NO_FIELD):
			self.assertTrue(serial.get(STAMPING_NO_FIELD))

	def test_an_order_without_a_customer_leaves_it_empty(self):
		serial = self._run_snc(self._new_serial(), self.pmo_without_customer)

		self.assertFalse(serial.customer)

	def test_a_rerun_on_a_stamped_serial_changes_nothing(self):
		serial_no = self._new_serial()
		first = self._run_snc(serial_no, self.pmo_with_customer)

		second = self._run_snc(serial_no, self.pmo_with_customer)

		self.assertEqual(second.customer, self.customer)
		self.assertEqual(second.get(STAMPING_NO_FIELD), first.get(STAMPING_NO_FIELD))
		self.assertEqual([row.huid for row in second.huid].count(HUID), 1)

	def test_a_site_that_fetches_the_customer_from_the_bom_is_left_to_it(self):
		"""Where Serial No.customer is fetched from the serial's BOM (a Property Setter, as the CI
		fixtures have), the save fills it from there as before. The order's customer is not
		stamped over it, so the save sees no change. Stamping it anyway is what failed in CI:
		the fetch put the BOM's empty customer back and ERPNext saw a change."""
		serial_no = self._new_serial()
		frappe.db.set_value("Serial No", serial_no, "custom_bom_no", _bom(self.item))
		customer_field = frappe.get_meta("Serial No").get_field("customer")
		with patch.object(customer_field, "fetch_from", "custom_bom_no.customer"):
			serial = self._run_snc(serial_no, self.pmo_with_customer)

		self.assertFalse(serial.customer)

	@skipUnless(HAS_MANAGED_FIELDS, "needs ERPNext 16.52+")
	def test_erpnext_refuses_a_save_that_changes_the_customer(self):
		"""Control: the plain save the SNC used to do is what ERPNext 16.52 refuses."""
		serial = frappe.get_doc("Serial No", self._new_serial())
		serial.customer = self.customer

		with self.assertRaisesRegex(
			erpnext_serial_no.SerialNoCannotCannotChangeError, "cannot be changed"
		):
			serial.save()


def _company():
	return frappe.db.get_single_value(
		"Global Defaults", "default_company"
	) or frappe.db.get_value("Company", {}, "name", order_by="creation asc")


def _warehouse(label, company):
	"""A plain warehouse with no department, employee or type (doc_events.warehouse.validate)."""
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"{PREFIX} {label} - {abbr}"
	if not frappe.db.exists("Warehouse", name):
		frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": f"{PREFIX} {label}",
				"company": company,
				"parent_warehouse": frappe.db.get_value(
					"Warehouse",
					{
						"company": company,
						"is_group": 1,
						"parent_warehouse": ("is", "not set"),
					},
				),
			}
		).insert()
	return name


def _serialized_item():
	item_code = f"{PREFIX} Bangle"
	if frappe.db.exists("Item", item_code):
		return item_code
	item_group = f"{PREFIX} Finished Goods"
	if not frappe.db.exists("Item Group", item_group):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": item_group,
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}
		).insert()
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 1,
			"serial_no_series": "TSNCSTAMP-.#####",
			# Not a sales item: India Compliance would want an HSN code.
			"is_sales_item": 0,
		}
	).insert()
	return item_code


def _parent_manufacturing_order(label, customer, huid=None):
	"""Only the two things update_new_serial_no reads from the order: its customer and its HUID
	rows. The order is written straight to the database; its own lifecycle plays no part."""
	pmo = frappe.new_doc("Parent Manufacturing Order")
	pmo.update({"name": f"{PREFIX} PMO {label}", "customer": customer})
	if huid:
		pmo.append(
			"product_certification_details",
			{"name": frappe.generate_hash(length=10), "huid": huid, "date": today()},
		)
	for row in pmo.get("product_certification_details"):
		row.parent = pmo.name
	pmo.set_user_and_timestamp()
	pmo.db_insert()
	for row in pmo.get("product_certification_details"):
		row.db_insert()
	return pmo.name


def _bom(item_code):
	"""A BOM with no customer, written straight to the database: the serial only links to it."""
	bom = frappe.new_doc("BOM")
	bom.update({"name": f"{PREFIX} BOM", "item": item_code, "docstatus": 1})
	bom.set_user_and_timestamp()
	bom.db_insert()
	return bom.name


def _receive(company, warehouse, item_code):
	"""Material Receipt, following refining tests' gold_receipt."""
	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.stock_entry_type = "Material Receipt"
	se.manufacturer = frappe.db.get_value("Manufacturer", {}, "name")
	se.branch = frappe.db.get_value("Branch", {}, "name")
	se.append(
		"items",
		{
			"t_warehouse": warehouse,
			"item_code": item_code,
			"qty": 1,
			"basic_rate": 100,
			"pcs": "1",
		},
	)
	se.insert()
	se.submit()
	return se
