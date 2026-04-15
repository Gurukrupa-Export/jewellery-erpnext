import frappe


def execute(filters=None):
	if not filters:
		filters = {}

	if filters.get("batch_no"):
		linked = get_linked_batches(filters.get("batch_no"))
		filters["linked_batches"] = tuple(linked)

	conditions = get_conditions(filters)

	report = {}
	data = []

	with frappe.db.unbuffered_cursor():
		for r in get_cgr_data(filters, conditions):
			report[r.batch_no] = init_row(r.customer, r.item_code, r.qty)

		for r in get_pr_data(filters):
			if r.batch_no not in report:
				report[r.batch_no] = init_row(r.customer, r.item_code, r.qty)
			else:
				report[r.batch_no]["opening"] += r.qty

		for r in get_repack_data(filters):
			parent_batch = r.parent_batch
			parent_qty = r.parent_qty or 0
			child_batch = r.child_batch
			child_qty = r.child_qty or 0
			customer = r.customer
			item = r.item_code
			repack_type = r.repack_type

			if parent_batch in report:
				parent_owner = report[parent_batch]["owner"]

				if parent_owner == customer:
					report[parent_batch]["used_same"] += parent_qty
				else:
					report[parent_batch]["used_other"] += parent_qty

			if child_batch not in report:
				report[child_batch] = init_row(customer, item, child_qty)

			if repack_type == "Subcontracting Repack" and child_batch in report:
				report[child_batch]["received_back"] += child_qty

		for r in get_usage_data(filters, conditions):
			if r.batch_no not in report:
				continue

			owner = report[r.batch_no]["owner"]
			target = r.target_customer

			if owner == target:
				report[r.batch_no]["used_same"] += r.qty
			else:
				report[r.batch_no]["used_other"] += r.qty

	if filters.get("customer") and not filters.get("batch_no"):
		report = {
			k: v for k, v in report.items() if v.get("owner") == filters.get("customer")
		}

	for batch, d in report.items():
		balance = d["opening"] - d["used_same"] - d["used_other"] + d["received_back"]

		data.append(
			[
				batch,
				d["owner"],
				d["item"],
				d["opening"],
				d["used_same"],
				d["used_other"],
				d["received_back"],
				balance,
			]
		)
	item_filter = filters.get("item_code")
	if item_filter and data:
		total_opening = sum(frappe.utils.flt(r[3]) for r in data)
		total_used_same = sum(frappe.utils.flt(r[4]) for r in data)
		total_used_other = sum(frappe.utils.flt(r[5]) for r in data)
		total_received_back = sum(frappe.utils.flt(r[6]) for r in data)

		total_balance = total_opening - total_used_same - total_used_other

		data.append(
			[
				"Total",
				"",
				item_filter,
				total_opening,
				total_used_same,
				total_used_other,
				total_received_back,
				total_balance,
			]
		)

	return get_columns(), data


def get_linked_batches(batch_no):
	ancestors = set()
	stack = [batch_no]

	while stack:
		current = stack.pop()
		if current in ancestors:
			continue
		ancestors.add(current)

		sources = frappe.get_all(
			"Batch MultiSelect",
			filters={"parent": current},
			fields=["batch_no"],
		)
		for row in sources:
			if row.batch_no and row.batch_no not in ancestors:
				stack.append(row.batch_no)

	linked = set()
	stack = list(ancestors)

	while stack:
		current = stack.pop()
		if current in linked:
			continue
		linked.add(current)

		children = frappe.get_all(
			"Batch MultiSelect",
			filters={"batch_no": current},
			fields=["parent"],
		)
		for row in children:
			if row.parent and row.parent not in linked:
				stack.append(row.parent)

	return linked


def init_row(customer, item, qty):
	return {
		"owner": customer,
		"item": item,
		"opening": qty,
		"used_same": 0,
		"used_other": 0,
		"received_back": 0,
	}


def get_conditions(filters):
	conditions = ""

	if filters.get("linked_batches"):
		conditions += " AND sed.batch_no IN %(linked_batches)s"

	elif filters.get("batch_no"):
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
		as_iterator=True,
	)


def get_pr_data(filters):
	conditions = ""

	if filters.get("linked_batches"):
		conditions += " AND pr_item.batch_no IN %(linked_batches)s"

	elif filters.get("batch_no"):
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
		as_iterator=True,
	)


def get_usage_data(filters, conditions):
	return frappe.db.sql(
		f"""
    SELECT
      sed.batch_no,
      sed.qty,
      mwo.customer AS target_customer
    FROM `tabStock Entry Detail` sed
    JOIN `tabStock Entry` se ON se.name = sed.parent
    LEFT JOIN `tabManufacturing Work Order` mwo
      ON mwo.name = se.manufacturing_work_order
    WHERE se.stock_entry_type = 'Material Transfer (WORK ORDER)'
    {conditions}
    """,
		filters,
		as_dict=1,
		as_iterator=True,
	)


def get_repack_data(filters):
	conditions = ""

	if filters.get("linked_batches"):
		conditions += " AND (parent_sed.batch_no IN %(linked_batches)s OR child_sed.batch_no IN %(linked_batches)s)"

	elif filters.get("batch_no"):
		conditions += " AND (parent_sed.batch_no = %(batch_no)s OR child_sed.batch_no = %(batch_no)s)"

	if filters.get("item_code"):
		conditions += " AND (parent_sed.item_code = %(item_code)s OR child_sed.item_code = %(item_code)s)"

	return frappe.db.sql(
		f"""
    SELECT
      parent_sed.batch_no AS parent_batch,
      parent_sed.qty AS parent_qty,
      child_sed.batch_no AS child_batch,
      child_sed.qty AS child_qty,
      child_sed.customer,
      child_sed.item_code,
      se.stock_entry_type AS repack_type
    FROM `tabStock Entry` se
    JOIN `tabStock Entry Detail` parent_sed
      ON parent_sed.parent = se.name
      AND parent_sed.is_finished_item = 0
    JOIN `tabStock Entry Detail` child_sed
      ON child_sed.parent = se.name
      AND child_sed.is_finished_item = 1
    WHERE se.stock_entry_type IN ('Repack-Metal Conversion', 'Subcontracting Repack')
    {conditions}
    """,
		filters,
		as_dict=1,
		as_iterator=True,
	)


def get_columns():
	return [
		"Batch No:Link/Batch:350",
		"Owner:Link/Customer:120",
		"Item:Link/Item:160",
		"Opening Qty:Float:110",
		"Used Same:Float:100",
		"Used Other:Float:100",
		"Received Back:Float:120",
		"Balance:Float:100",
	]
