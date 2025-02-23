import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc

from jewellery_erpnext.jewellery_erpnext.customization.sales_order.doc_events.branch_utils import (
	create_branch_so,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.bom_utils import (
	calculate_gst_rate,
	set_bom_item_details,
	set_bom_rate,
)


def validate(self, method):
	create_new_bom(self)
	# calculate_gst_rate(self)
	if not self.get("__islocal") and self.docstatus == 0:
		set_bom_item_details(self)


def on_submit(self, method):
	# submit_bom(self)
	create_branch_so(self)


def on_cancel(self, method):
	cancel_bom(self)


def create_new_bom(self):
	"""
	This Function Creates Sales Order Type BOM from Quotation Bom
	"""
	# diamond_grade_data = frappe._dict()
	for row in self.items:
		if not row.quotation_bom:
			create_serial_no_bom(self, row)
			if row.bom:
				doc = frappe.get_doc("BOM",row.bom)
				row.gold_bom_rate = doc.gold_bom_amount
				row.diamond_bom_rate = doc.diamond_bom_amount
				row.gemstone_bom_rate = doc.gemstone_bom_amount
				row.other_bom_rate = doc.other_bom_amount
				row.making_charge = doc.making_charge
				row.bom_rate = doc.total_bom_amount
				row.rate = doc.total_bom_amount
		elif not row.bom and frappe.db.exists("BOM", row.quotation_bom):
			row.bom = row.quotation_bom
			
			data_to_be_updated = {
				"bom_type": "Sales Order",
				"custom_creation_doctype": "Sales Order",
				"custom_creation_docname": self.name,
				"gold_rate_with_gst": self.gold_rate_with_gst,
			}
			frappe.db.set_value("BOM", row.quotation_bom, data_to_be_updated)
			doc = frappe.get_doc("BOM",row.quotation_bom)
			row.gold_bom_rate = doc.gold_bom_amount
			row.diamond_bom_rate = doc.diamond_bom_amount
			row.gemstone_bom_rate = doc.gemstone_bom_amount
			row.other_bom_rate = doc.other_bom_amount
			row.making_charge = doc.making_charge
			row.bom_rate = doc.total_bom_amount
			row.rate = doc.total_bom_amount
			# create_sales_order_bom(self, row, diamond_grade_data)


def create_serial_no_bom(self, row):
	if row.bom:
		return
	serial_no_bom = frappe.db.get_value("Serial No", row.serial_no, "custom_bom_no")
	if not serial_no_bom:
		return
	bom_doc = frappe.get_doc("BOM", serial_no_bom)
	if self.customer != bom_doc.customer:
		doc = frappe.copy_doc(bom_doc)
		doc.customer = self.customer
		doc.gold_rate_with_gst = self.gold_rate_with_gst
		doc.save(ignore_permissions=True)
		row.bom = doc.name


def create_sales_order_bom(self, row, diamond_grade_data):
	doc = frappe.copy_doc(frappe.get_doc("BOM", row.quotation_bom))
	# doc = get_mapped_doc(
	# 	"BOM",
	# 	row.quotation_bom,
	# 	{
	# 		"BOM": {
	# 			"doctype": "BOM",
	# 		}
	# 	},
	# 	ignore_permissions=True,
	# )
	try:
		doc.custom_creation_doctype = self.doctype
		doc.is_default = 0
		doc.is_active = 1
		doc.bom_type = "Sales Order"
		doc.gold_rate_with_gst = self.gold_rate_with_gst
		doc.customer = self.customer
		doc.selling_price_list = self.selling_price_list
		doc.reference_doctype = "Sales Order"
		doc.reference_docname = self.name
		doc.custom_creation_docname = None
		# doc.save(ignore_permissions=True)
		for diamond in doc.diamond_detail:
			if row.diamond_grade:
				diamond.diamond_grade = row.diamond_grade
			else:
				if not diamond_grade_data.get(row.diamond_quality):
					diamond_grade_data[row.diamond_quality] = frappe.db.get_value(
						"Customer Diamond Grade",
						{"parent": doc.customer, "diamond_quality": row.diamond_quality},
						"diamond_grade_1",
					)

				diamond.diamond_grade = diamond_grade_data.get(row.diamond_quality)
			if row.diamond_quality:
				diamond.quality = row.diamond_quality

		# This Save will Call before_save and validate method in BOM and Rates Will be Calculated as diamond_quality is calculated too
		doc.save(ignore_permissions=True)
		doc.db_set("custom_creation_docname", self.name)
		row.bom = doc.name
		row.gold_bom_rate = doc.gold_bom_amount
		row.diamond_bom_rate = doc.diamond_bom_amount
		row.gemstone_bom_rate = doc.gemstone_bom_amount
		row.other_bom_rate = doc.other_bom_amount
		row.making_charge = doc.making_charge
		row.bom_rate = doc.total_bom_amount
		row.rate = doc.total_bom_amount
		self.total = doc.total_bom_amount
	except Exception as e:
		frappe.logger("utils").exception(e)
		frappe.log_error(
			title=f"Error while creating Sales Order from {row.quotation_bom}", message=str(e)
		)
		frappe.throw(_("Row {0} {1}").format(row.idx, e))


def submit_bom(self):
	for row in self.items:
		if row.bom:
			bom_doc = frappe.get_doc("BOM", row.bom)
			if bom_doc.docstatus == 0:
				bom_doc.submit()
			# frappe.enqueue(enqueue_submit_bom, job_name="Submitting SO BOM", bom=row.bom)


# def enqueue_submit_bom(bom):
# 	bom_doc = frappe.get_doc("BOM", bom)
# 	if bom_doc.docstatus == 0:
# 		bom_doc.submit()


def cancel_bom(self):
	for row in self.items:
		if row.bom:
			bom = frappe.get_doc("BOM", row.bom)
			bom.is_active = 0
			row.bom = ""


@frappe.whitelist()
def get_customer_approval_data(customer_approval_data):
	doc = frappe.get_doc("Customer Approval", customer_approval_data)
	return doc


@frappe.whitelist()
def customer_approval_filter(doctype, txt, searchfield, start, page_len, filters):
	CustomerApproval = frappe.qb.DocType("Customer Approval")
	StockEntry = frappe.qb.DocType("Stock Entry")

	query = (
		frappe.qb.from_(CustomerApproval)
		.left_join(StockEntry)
		.on(CustomerApproval.name == StockEntry.custom_customer_approval_reference)
		.select(CustomerApproval.name)
		.where(
			(
				(StockEntry.custom_customer_approval_reference != CustomerApproval.name)
				| (StockEntry.custom_customer_approval_reference.isnull())
			)
			& (CustomerApproval.docstatus == 1)
			& (CustomerApproval[searchfield].like(f"%{txt}%"))
		)
	)

	if filters.get("date"):
		query = query.where(CustomerApproval.date == filters["date"])

	dialoge_filter = query.run(as_dict=True)

	return dialoge_filter
