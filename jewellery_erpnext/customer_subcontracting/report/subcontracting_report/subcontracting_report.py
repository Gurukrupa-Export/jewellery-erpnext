import frappe


def execute(filters=None):
	if not filters:
		filters = {}

	batches_to_include = filters.get("linked_batches")

	if filters.get("batch_no") and not batches_to_include:
		batches_to_include = get_linked_batches(filters.get("batch_no"))
		filters["linked_batches"] = batches_to_include

	batch_map = {}
	parent_usage = {}
	child_usage = {}

	with frappe.db.unbuffered_cursor():
		for r in get_cgr_data(filters, get_conditions(filters)):
			add_opening(batch_map, r.batch_no, r.customer, r.item_code, r.qty)

		for r in get_pr_data(filters):
			add_opening(batch_map, r.batch_no, r.customer, r.item_code, r.qty)

		for r in get_repack_data(filters):
			process_repack_row(batch_map, parent_usage, child_usage, r)

		for r in get_usage_data(filters, get_conditions(filters)):
			process_usage_row(batch_map, parent_usage, child_usage, r)

	data = []

	if batches_to_include is None:
		batches_to_include = set(batch_map)
	else:
		batches_to_include = set(batches_to_include)

	ordered_batches = [b for b in sorted(batch_map) if b in batches_to_include]

	for batch in ordered_batches:
		info = batch_map[batch]
		opening = info["opening"]

		if not filters.get("batch_no") and opening == 0:
			continue

		owner = info["owner"]
		item = info["item"]

		parent = parent_usage.get(batch, {})
		used_same = parent.get("used_same", 0)
		received_back = parent.get("received_back", 0)

		total_used_other = 0
		total_return_qty = 0
		other_customers = []
		child_rows = [
			(key, usage) for key, usage in child_usage.items() if key[0] == batch
		]

		for (batch_key, target_customer, item_code), usage in child_rows:
			total_used_other += usage.get("used_other", 0)
			total_return_qty += usage.get("return_qty", 0)
			if target_customer and target_customer not in other_customers:
				other_customers.append(target_customer)

		other_customer = (
			", ".join([str(c) for c in other_customers if c]) if other_customers else ""
		)

		if filters.get("customer"):
			if filters.get("customer") != owner:
				continue

		if filters.get("other_customer"):
			if filters.get("other_customer") not in other_customers:
				continue

		balance = opening - used_same - total_used_other + received_back

		data.append(
			[
				batch,
				owner,
				item,
				opening,
				used_same,
				total_used_other,
				other_customer,
				received_back,
				balance,
				total_return_qty,
			]
		)

	return get_columns(), data


def add_opening(batch_map, batch_no, customer, item_code, qty):
	if not batch_no:
		return

	if batch_no not in batch_map:
		batch_map[batch_no] = {
			"owner": customer,
			"item": item_code,
			"opening": qty,
		}
	else:
		batch_map[batch_no]["opening"] += qty


def process_repack_row(batch_map, parent_usage, child_usage, row):
	parent_batch = row.parent_batch
	parent_qty = row.parent_qty or 0
	child_batch = row.child_batch
	child_qty = row.child_qty or 0
	customer = row.customer
	item = row.item_code
	repack_type = row.repack_type

	if parent_batch:
		if parent_batch not in batch_map:
			add_opening(batch_map, parent_batch, customer, item, 0)

		parent_owner = batch_map[parent_batch]["owner"]

		if parent_owner == customer:
			parent_usage.setdefault(parent_batch, {"used_same": 0, "received_back": 0})
			parent_usage[parent_batch]["used_same"] += parent_qty

		else:
			child_usage.setdefault(
				(parent_batch, customer, item),
				{"used_other": 0, "return_qty": 0},
			)
			child_usage[(parent_batch, customer, item)]["used_other"] += parent_qty

	if repack_type == "Subcontracting Repack":
		source_customer = batch_map.get(parent_batch, {}).get("owner")
		if child_batch:
			if child_batch not in batch_map:
				add_opening(batch_map, child_batch, customer, item, 0)

			parent_usage.setdefault(child_batch, {"used_same": 0, "received_back": 0})
			parent_usage[child_batch]["received_back"] += child_qty

			child_usage.setdefault(
				(child_batch, source_customer, item),
				{"used_other": 0, "return_qty": 0},
			)

		if parent_batch:
			child_usage.setdefault(
				(parent_batch, customer, item),
				{"used_other": 0, "return_qty": 0},
			)
			child_usage[(parent_batch, customer, item)]["return_qty"] += child_qty

	if child_batch:
		if child_batch not in batch_map:
			add_opening(batch_map, child_batch, customer, item, child_qty)


def process_usage_row(batch_map, parent_usage, child_usage, row):
	batch_no = row.batch_no
	if batch_no not in batch_map:
		return

	owner = batch_map[batch_no]["owner"]
	target_customer = row.target_customer
	qty = row.qty or 0

	if owner == target_customer:
		parent_usage.setdefault(batch_no, {"used_same": 0, "received_back": 0})
		parent_usage[batch_no]["used_same"] += qty
	else:
		child_usage.setdefault(
			(batch_no, target_customer, row.item_code),
			{"used_other": 0, "return_qty": 0},
		)
		child_usage[(batch_no, target_customer, row.item_code)]["used_other"] += qty


def get_linked_batches(batch_no):
	batches = {batch_no}

	batch_children = frappe.get_all(
		"Batch MultiSelect",
		filters={"parent": batch_no},
		fields=["batch_no"],
	)
	for row in batch_children:
		if row.batch_no:
			batches.add(row.batch_no)

	repack_children = frappe.db.sql(
		"""
        SELECT DISTINCT child_sed.batch_no
        FROM `tabStock Entry` se
        JOIN `tabStock Entry Detail` parent_sed
            ON parent_sed.parent = se.name AND parent_sed.is_finished_item = 0
        JOIN `tabStock Entry Detail` child_sed
            ON child_sed.parent = se.name AND child_sed.is_finished_item = 1
        WHERE se.stock_entry_type IN ('Repack-Metal Conversion', 'Subcontracting Repack')
        AND parent_sed.batch_no = %s
        """,
		(batch_no,),
		as_dict=True,
	)
	for row in repack_children:
		if row.batch_no:
			batches.add(row.batch_no)

	return list(batches)


def get_conditions(filters):
	conditions = ""

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
	if filters.get("other_customer"):
		conditions += " AND mwo.customer = %(other_customer)s"

	return frappe.db.sql(
		f"""
    SELECT
      sed.batch_no,
      sed.qty,
      sed.item_code,
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
		conditions += " AND (parent_sed.batch_no IN %(linked_batches)s)"

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
		"Other Customer:Link/Customer:140",
		"Received Back:Float:120",
		"Balance:Float:100",
		"Return Qty:Float:100",
	]
