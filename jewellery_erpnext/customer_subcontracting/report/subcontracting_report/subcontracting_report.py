import frappe
from frappe.utils import flt


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

		# Produced (child) batches first so a later-consumed conversion child exists.
		for r in get_conversion_produced(filters):
			add_opening(batch_map, r.batch_no, r.customer, r.item_code, r.qty)

		for r in get_conversion_consumed(filters):
			process_conversion_consumed(batch_map, parent_usage, child_usage, r)

		for r in get_usage_data(filters, get_conditions(filters)):
			process_usage_row(batch_map, parent_usage, child_usage, r)

		for r in get_snc_return_data(filters, get_conditions(filters)):
			process_return_row(batch_map, parent_usage, child_usage, r)

	data = []

	if batches_to_include is None:
		batches_to_include = set(batch_map)
	else:
		batches_to_include = set(batches_to_include)

	batch_creation_map, inventory_type_map = get_batch_creation_map(batches_to_include)

	for batch in batch_map:
		batch_map[batch]["inventory_type"] = inventory_type_map.get(batch)

	ordered_batches = sorted(
		[b for b in batch_map if b in batches_to_include],
		key=lambda x: batch_creation_map.get(x),
	)

	for batch in ordered_batches:
		info = batch_map[batch]
		opening = info["opening"]

		if not filters.get("batch_no") and opening == 0:
			continue

		owner = info["owner"]
		item = info["item"]
		inventory_type = info.get("inventory_type")

		parent = parent_usage.get(batch, {})
		used_same = parent.get("used_same", 0)

		total_used_other = 0
		total_return_qty = 0
		other_customers = []
		child_rows = [
			(key, usage) for key, usage in child_usage.items() if key[0] == batch
		]

		for (batch_key, target_customer, item_code), usage in child_rows:
			# Net the borrow by what Create SNC has settled/returned: a fully-settled
			# borrower drops to 0 and its "Other Customer" label clears.
			returned = usage.get("return_qty", 0)
			net_used_other = max(0, usage.get("used_other", 0) - returned)
			total_used_other += net_used_other
			total_return_qty += returned
			if (
				target_customer
				and net_used_other > 1e-6
				and target_customer not in other_customers
			):
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

		if filters.get("inventory_type"):
			if inventory_type != filters.get("inventory_type"):
				continue

		# Netting already restores the returned gold, so no separate received-back term.
		balance = opening - used_same - total_used_other
		pure_qty = get_pure_qty(item, balance)

		data.append(
			[
				batch,
				owner,
				item,
				inventory_type,
				opening,
				used_same,
				total_used_other,
				other_customer,
				balance,
				pure_qty,
				total_return_qty,
			]
		)

	if data:
		data.append(build_total_row(data))

	return get_columns(), data


def add_opening(batch_map, batch_no, customer, item_code, qty):
	if not batch_no:
		return

	if batch_no not in batch_map:
		batch_map[batch_no] = {
			"owner": customer,
			"item": item_code,
			"opening": qty,
			"inventory_type": None,
		}
	else:
		batch_map[batch_no]["opening"] += qty


def build_total_row(data):
	"""Consolidated Total row: sum the numeric columns (Opening, Used Same, Used
	Other, Balance, Pure Qty, Return Qty); leave label/link/text columns blank."""
	numeric_cols = (4, 5, 6, 8, 9, 10)
	totals = {i: 0 for i in numeric_cols}
	for row in data:
		for i in numeric_cols:
			totals[i] += flt(row[i])

	total_row = [""] * len(get_columns())
	total_row[0] = "Total"
	for i in numeric_cols:
		total_row[i] = flt(totals[i], 3)
	return total_row


def get_pure_qty(item_code, balance):
	"""Convert a balance qty into pure metal weight using the item's karat purity.

	The purity percentage is carried in the gold item code right after the karat
	segment (e.g. ``F-G-18KT-75.4-...`` -> 75.4), matching the item's Metal Purity
	attribute and the ``custom_pure_qty`` the SNC flow records. 24KT is treated as
	already pure (no change); non-metal items (no karat segment) pass through
	unchanged."""
	parts = (item_code or "").split("-")
	for i, seg in enumerate(parts):
		if not seg.endswith("KT"):
			continue
		if seg == "24KT":
			return flt(balance, 3)
		if i + 1 < len(parts):
			try:
				return flt(flt(balance) * flt(parts[i + 1]) / 100, 3)
			except (TypeError, ValueError):
				break
		break
	return flt(balance, 3)


def process_usage_row(batch_map, parent_usage, child_usage, row):
	batch_no = row.batch_no
	if batch_no not in batch_map:
		return

	owner = batch_map[batch_no]["owner"]
	target_customer = row.target_customer
	qty = row.qty or 0

	if owner == target_customer:
		parent_usage.setdefault(batch_no, {"used_same": 0})
		parent_usage[batch_no]["used_same"] += qty
	else:
		child_usage.setdefault(
			(batch_no, target_customer, row.item_code),
			{"used_other": 0, "return_qty": 0},
		)
		child_usage[(batch_no, target_customer, row.item_code)]["used_other"] += qty


def process_return_row(batch_map, parent_usage, child_usage, row):
	"""Credit a Create-SNC settlement: the 'Material Receive (WORK ORDER)' entry that
	returns the borrowed gold batch to its owner. Netted against ``used_other`` at
	render time so it clears the borrower once fully settled."""
	batch_no = row.batch_no
	if batch_no not in batch_map:
		return

	owner = batch_map[batch_no]["owner"]
	target_customer = row.target_customer
	qty = row.qty or 0

	# A settlement only reverses a borrow by another customer; the owner's own gold
	# is never "returned" to itself.
	if owner == target_customer:
		return

	child_usage.setdefault(
		(batch_no, target_customer, row.item_code),
		{"used_other": 0, "return_qty": 0},
	)
	child_usage[(batch_no, target_customer, row.item_code)]["return_qty"] += qty


def process_conversion_consumed(batch_map, parent_usage, child_usage, row):
	"""The batch a metal conversion consumed: Used Same when the owner converted their
	own gold, else Used Other. The produced (child) batch is handled separately as an
	opening, so this only draws down the consumed batch."""
	batch_no = row.batch_no
	qty = row.qty or 0
	customer = row.customer
	item = row.item_code

	if batch_no not in batch_map:
		add_opening(batch_map, batch_no, customer, item, 0)
	owner = batch_map[batch_no]["owner"]

	if owner == customer:
		parent_usage.setdefault(batch_no, {"used_same": 0})
		parent_usage[batch_no]["used_same"] += qty
	else:
		child_usage.setdefault(
			(batch_no, customer, item),
			{"used_other": 0, "return_qty": 0},
		)
		child_usage[(batch_no, customer, item)]["used_other"] += qty


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

	# Kept for the legacy Repack-based consumers of this helper (batch_rename.py,
	# repack.py) that still resolve a batch's repack children. The SNC report itself
	# no longer needs the expansion -- it returns the borrowed batch as itself -- but
	# the extra linked batches are harmless to the report (they surface their own
	# CGR / usage / return rows).
	repack_children = frappe.db.sql(
		"""
        SELECT DISTINCT child_sed.batch_no
        FROM `tabStock Entry` se
        JOIN `tabStock Entry Detail` parent_sed
            ON parent_sed.parent = se.name AND parent_sed.is_finished_item = 0
        JOIN `tabStock Entry Detail` child_sed
            ON child_sed.parent = se.name AND child_sed.is_finished_item = 1
        WHERE se.stock_entry_type IN ('Repack-Metal Conversion', 'Subcontracting Repack')
		AND se.docstatus = 1
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
	AND se.docstatus =1
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
	AND pr.docstatus = 1
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
    WHERE se.stock_entry_type IN ('Material Transfer (WORK ORDER)', 'Material Transfer to Department')
	AND se.docstatus = 1
    {conditions}
    """,
		filters,
		as_dict=1,
		as_iterator=True,
	)


def get_snc_return_data(filters, conditions):
	"""Create-SNC settlements: the 'Material Receive (WORK ORDER)' entries (tagged
	``custom_request_id LIKE 'SNC-%'``) that return the borrowed gold batch to its
	owner. The received (t_warehouse) row carries the borrowed batch; ``mwo.customer``
	is the borrowing customer, matching the Used-Other key it offsets."""
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
    WHERE se.stock_entry_type = 'Material Receive (WORK ORDER)'
	AND se.custom_request_id LIKE 'SNC-%%'
	AND se.docstatus = 1
	AND sed.t_warehouse IS NOT NULL
	AND sed.batch_no IS NOT NULL
    {conditions}
    """,
		filters,
		as_dict=1,
		as_iterator=True,
	)


def _conversion_rows(filters, is_finished_item):
	"""Aggregated Repack-Metal Conversion rows for one side (consumed = 0, produced =
	1), summed per batch so a multi-line conversion SE can't cartesian-double the qty.

	Standalone customer conversions only (e.g. 24KT -> 18KT of a customer's own gold);
	the SNC settlement's own systematic purity swaps carry a ``manufacturing_work_order``
	and are handled through the SNC receive / transfer path instead."""
	conditions = ""
	if filters.get("item_code"):
		conditions += " AND sed.item_code = %(item_code)s"

	return frappe.db.sql(
		f"""
    SELECT
      sed.batch_no,
      SUM(sed.qty) AS qty,
      sed.customer,
      sed.item_code
    FROM `tabStock Entry Detail` sed
    JOIN `tabStock Entry` se ON se.name = sed.parent
    WHERE se.stock_entry_type = 'Repack-Metal Conversion'
	AND se.docstatus = 1
	AND se.manufacturing_work_order IS NULL
	AND sed.is_finished_item = {int(is_finished_item)}
	AND sed.batch_no IS NOT NULL
    {conditions}
    GROUP BY sed.batch_no, sed.customer, sed.item_code
    """,
		filters,
		as_dict=1,
		as_iterator=True,
	)


def get_conversion_produced(filters):
	"""Batches created by a metal conversion (e.g. the 18KT side of a 24KT -> 18KT).
	These have no Customer Goods Received / PR opening of their own, so without this
	they never surface in the report."""
	return _conversion_rows(filters, is_finished_item=1)


def get_conversion_consumed(filters):
	"""Batches drawn down by a metal conversion (e.g. the 24KT side)."""
	return _conversion_rows(filters, is_finished_item=0)


def get_batch_creation_map(batches):
	if not batches:
		return {}, {}

	data = frappe.get_all(
		"Batch",
		filters={"name": ["in", list(batches)]},
		fields=[
			"name",
			"creation",
			"custom_inventory_type",
		],
	)

	creation_map = {d.name: d.creation for d in data}

	inventory_type_map = {d.name: d.custom_inventory_type for d in data}

	return creation_map, inventory_type_map


def get_columns():
	return [
		"Batch No:Link/Batch:350",
		"Owner:Link/Customer:120",
		"Item:Link/Item:160",
		"Inventory Type:Data:140",
		"Opening Qty:Float:110",
		"Used Same:Float:100",
		"Used Other:Float:100",
		"Other Customer:Data/Customer:140",
		"Balance:Float:100",
		"Pure Qty:Float:100",
		"Return Qty:Float:100",
	]
