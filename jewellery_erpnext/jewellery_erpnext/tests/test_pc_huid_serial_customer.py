# Copyright (c) 2026, Nirali and contributors
# See license.txt
"""Product Certification adding a HUID to a Serial No on a site that fetches its customer.

add_to_serial_no appends the HUID row and saves the serial. On GEPL, Serial No.customer is
fetched from the serial's BOM (a Property Setter with fetch_from custom_bom_no.customer), so the
save puts the BOM's customer back, and ERPNext 16.52 (frappe/erpnext#60162) refuses that as a
customer change: "Customer cannot be changed for Serial No." failed the Product Certification
submit. stamp_fetched_customer writes the fetched customer first, so the save sees no change.

The serials are created by a real Material Receipt and every save goes through ERPNext's own
validate(). Everything is rolled back per test.

Run: bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_pc_huid_serial_customer
"""

from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

import frappe
from erpnext.stock.doctype.serial_no import serial_no as erpnext_serial_no
from frappe.tests import IntegrationTestCase
from frappe.utils import today

from jewellery_erpnext.jewellery_erpnext.doc_events.serial_no import (
	stamp_fetched_customer,
)
from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification import (
	add_to_serial_no,
)

_SERVER_SCRIPT_MAP = (
	"frappe.core.doctype.server_script.server_script_utils.get_server_script_map"
)
_EOD_LOCK = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock.is_eod_sync_locked"
_SAVEPOINT = "pc_huid_serial_customer_test"
PREFIX = "_Test PC HUID"
HUID = "TPCH01"

# ERPNext 16.52 introduced the guard; older versions have no STOCK_MANAGED_FIELDS.
HAS_MANAGED_FIELDS = "customer" in getattr(
	erpnext_serial_no, "STOCK_MANAGED_FIELDS", ()
)


class TestProductCertificationHuidOnFetchedCustomer(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.addClassCleanup(frappe.clear_cache)

		# A site's own Server Scripts are not this app's; with server scripts switched off they
		# refuse every save of their doctype, so they are left out here.
		server_scripts = patch(_SERVER_SCRIPT_MAP, return_value={})
		server_scripts.start()
		cls.addClassCleanup(server_scripts.stop)

		# An EOD sync lock left on a shared test site refuses the Material Receipt below.
		eod_lock = patch(_EOD_LOCK, return_value=False)
		eod_lock.start()
		cls.addClassCleanup(eod_lock.stop)

		cls.company = _company()
		cls.customer = frappe.db.get_value("Customer", {"disabled": 0}, "name")
		cls.warehouse = _warehouse("FG", cls.company)
		cls.item = _serialized_item()

	def setUp(self):
		super().setUp()
		frappe.db.savepoint(_SAVEPOINT)
		self.addCleanup(frappe.db.rollback, save_point=_SAVEPOINT)

	def _new_serial(self):
		"""A serial created by the stock ledger, linked to a BOM that carries a customer."""
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

	def _link_bom(self, serial_no, customer):
		bom = _bom(self.item)
		frappe.db.set_value("BOM", bom, "customer", customer)
		frappe.db.set_value("Serial No", serial_no, "custom_bom_no", bom)

	def _add_huid(self, serial_no):
		add_to_serial_no(
			serial_no,
			SimpleNamespace(date=today()),
			SimpleNamespace(huid=HUID),
		)
		return frappe.get_doc("Serial No", serial_no)

	def _fetching_customer(self):
		customer_field = frappe.get_meta("Serial No").get_field("customer")
		return patch.object(customer_field, "fetch_from", "custom_bom_no.customer")

	@skipUnless(HAS_MANAGED_FIELDS, "needs ERPNext 16.52+")
	def test_control_the_plain_save_is_refused(self):
		"""The reported failure: adding a HUID and saving, with the BOM's customer fetched."""
		serial_no = self._new_serial()
		self._link_bom(serial_no, self.customer)
		with self._fetching_customer():
			serial = frappe.get_doc("Serial No", serial_no)
			serial.append("huid", {"huid": HUID, "date": today()})
			with self.assertRaisesRegex(
				erpnext_serial_no.SerialNoCannotCannotChangeError, "cannot be changed"
			):
				serial.save()

	def test_the_huid_is_added_and_the_serial_keeps_the_boms_customer(self):
		serial_no = self._new_serial()
		self._link_bom(serial_no, self.customer)
		with self._fetching_customer():
			serial = self._add_huid(serial_no)

		self.assertIn(HUID, [row.huid for row in serial.huid])
		self.assertEqual(serial.customer, self.customer)

	def test_a_site_that_does_not_fetch_the_customer_is_left_alone(self):
		serial_no = self._new_serial()
		self._link_bom(serial_no, self.customer)
		customer_field = frappe.get_meta("Serial No").get_field("customer")
		with patch.object(customer_field, "fetch_from", None):
			serial = self._add_huid(serial_no)

		self.assertIn(HUID, [row.huid for row in serial.huid])
		self.assertFalse(serial.customer)

	def test_a_serial_with_no_bom_is_not_touched(self):
		serial_no = self._new_serial()
		with self._fetching_customer():
			serial = frappe.get_doc("Serial No", serial_no)
			with patch.object(type(serial), "db_set") as db_set:
				stamp_fetched_customer(serial)
		db_set.assert_not_called()


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
			"serial_no_series": "TPCHUID-.#####",
			# Not a sales item: India Compliance would want an HSN code.
			"is_sales_item": 0,
		}
	).insert()
	return item_code


def _bom(item_code):
	"""A BOM written straight to the database: the serial only links to it."""
	bom = frappe.new_doc("BOM")
	bom.update({"name": f"{PREFIX} BOM", "item": item_code, "docstatus": 1})
	bom.set_user_and_timestamp()
	bom.db_insert()
	return bom.name


def _receive(company, warehouse, item_code):
	"""Material Receipt of one serialised piece, following refining tests' gold_receipt."""
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
