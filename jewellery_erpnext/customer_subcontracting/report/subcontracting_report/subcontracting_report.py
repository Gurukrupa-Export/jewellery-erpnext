import frappe


def execute(filters=None):
	if not filters:
		filters = {}

	conditions = get_conditions(filters)

	cgr_data = get_cgr_data(filters, conditions)
	transfer_data = get_transfer_data(filters, conditions)
	repack_data = get_repack_data(filters)
	pr_data = get_pr_data(filters)

	report = {}

	for r in cgr_data:
		report[r.batch_no] = {
			"owner": r.customer,
			"item": r.item_code,
			"opening": r.qty,
			"used_same": 0,
			"used_other": 0,
		}

	for r in pr_data:
		if r.batch_no not in report:
			report[r.batch_no] = {
				"owner": r.customer,
				"item": r.item_code,
				"opening": r.qty,
				"used_same": 0,
				"used_other": 0,
			}
		else:
			report[r.batch_no]["opening"] += r.qty

	for r in transfer_data:
		if r.batch_no not in report:
			continue

		owner = report[r.batch_no]["owner"]
		target = get_customer_from_maufacturing_work_order(r.manufacturing_work_order)

		if owner == target:
			report[r.batch_no]["used_same"] += r.qty
		else:
			report[r.batch_no]["used_other"] += r.qty

	for r in repack_data:
		parent_batches = (r.get("parent_batches") or "").split("||")
		child_batch = r.get("child_batch")
		qty = r.get("qty")
		customer = r.get("customer")
		item = r.get("item_code")

		for parent_batch in parent_batches:
			if not parent_batch:
				continue

			if parent_batch in report:
				report[parent_batch]["used_other"] += qty

		if child_batch not in report:
			report[child_batch] = {
				"owner": customer,
				"item": item,
				"opening": qty,
				"used_same": 0,
				"used_other": 0,
			}
		else:
			report[child_batch]["opening"] += qty

	if filters.get("customer"):
		report = {
			k: v for k, v in report.items() if v.get("owner") == filters.get("customer")
		}

	data = []

	for batch, d in report.items():
		balance = d["opening"] - d["used_same"] - d["used_other"]
		balance_from_other = d["used_other"]

		data.append(
			[
				batch,
				d["owner"],
				d["item"],
				d["opening"],
				d["used_same"],
				d["used_other"],
				balance,
				balance_from_other,
			]
		)

	item_filter = filters.get("item_code")

	if item_filter and data:
		total_opening = 0
		total_used_same = 0
		total_used_other = 0

		for r in data:
			total_opening += frappe.utils.flt(r[3])
			total_used_same += frappe.utils.flt(r[4])
			total_used_other += frappe.utils.flt(r[5])

		total_balance = total_opening - total_used_same - total_used_other
		total_balance_other = total_used_other

		data.append(
			[
				"Total",
				"",
				item_filter,
				total_opening,
				total_used_same,
				total_used_other,
				total_balance,
				total_balance_other,
			]
		)

	return get_columns(), data


def get_conditions(filters):
	conditions = ""

	if filters.get("batch_no"):
		conditions += " AND sed.batch_no = %(batch_no)s"

	if filters.get("item_code"):
		conditions += " AND sed.item_code = %(item_code)s"

	return conditions


def get_cgr_data(filters, conditions):
	return frappe.db.sql(
		f"""
		SELECT
			sed.batch_no,
			SUM(sed.qty) AS qty,
			sed.customer,
			sed.item_code
		FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name = sed.parent
		WHERE se.stock_entry_type = 'Customer Goods Received'
		{conditions}
		GROUP BY sed.batch_no, sed.customer, sed.item_code
		""",
		filters,
		as_dict=1,
	)


def get_pr_data(filters):
	conditions = ""
	if filters.get("batch_no"):
		conditions += " AND pr_item.batch_no = %(batch_no)s"
	if filters.get("item_code"):
		conditions += " AND pr_item.item_code = %(item_code)s"

	return frappe.db.sql(
		f"""
		SELECT
			pr_item.batch_no,
			pr_item.qty,
			pr_item.item_code,
			pr_item.customer
		FROM `tabPurchase Receipt Item` pr_item
		JOIN `tabPurchase Receipt` pr ON pr.name = pr_item.parent
		WHERE pr.purchase_type = 'Subcontracting'
		{conditions}
		""",
		filters,
		as_dict=1,
	)


def get_transfer_data(filters, conditions):
	return frappe.db.sql(
		f"""
		SELECT
			sed.batch_no,
			sed.qty,
			sed.item_code,
			se.manufacturing_work_order
		FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name = sed.parent
		WHERE se.stock_entry_type = 'Material Transfer (WORK ORDER)'
		{conditions}
		""",
		filters,
		as_dict=1,
	)


def get_repack_data(filters):
	conditions = ""
	if filters.get("batch_no"):
		conditions += " AND (parent_sed.batch_no = %(batch_no)s OR child_sed.batch_no = %(batch_no)s)"
	if filters.get("item_code"):
		conditions += " AND (parent_sed.item_code = %(item_code)s OR child_sed.item_code = %(item_code)s)"

	return frappe.db.sql(
		f"""
		SELECT
			GROUP_CONCAT(DISTINCT parent_sed.batch_no SEPARATOR '||') AS parent_batches,
			child_sed.batch_no AS child_batch,
			SUM(child_sed.qty) AS qty,
			child_sed.customer,
			child_sed.item_code
		FROM `tabStock Entry` se

		JOIN `tabStock Entry Detail` parent_sed
			ON parent_sed.parent = se.name
			AND parent_sed.is_finished_item = 0

		JOIN `tabStock Entry Detail` child_sed
			ON child_sed.parent = se.name
			AND child_sed.is_finished_item = 1

		WHERE se.stock_entry_type = 'Subcontracting Repack'
		{conditions}
		GROUP BY child_sed.parent, child_sed.batch_no, child_sed.customer, child_sed.item_code
		""",
		filters,
		as_dict=1,
	)


def get_customer_from_maufacturing_work_order(mwo):
	if not mwo:
		return None
	if frappe.db.exists("Manufacturing Work Order", mwo):
		return frappe.db.get_value("Manufacturing Work Order", mwo, "customer")
	return None


def get_columns():
	return [
		"Batch No:Link/Batch:350",
		"Owner:Link/Customer:120",
		"Item:Link/Item:160",
		"Opening Qty:Float:110",
		"Used Same:Float:100",
		"Used Other:Float:100",
		"Balance:Float:100",
		"Balance from other:Float:150",
	]
