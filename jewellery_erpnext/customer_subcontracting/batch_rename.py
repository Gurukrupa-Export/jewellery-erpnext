import string
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.report.subcontracting_report.subcontracting_report import (
	execute as get_report_data,
)
from jewellery_erpnext.customer_subcontracting.report.subcontracting_report.subcontracting_report import (
	get_linked_batches,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	CUSTOMER_INVENTORY_TYPES,
)

#: Stock Entry Types that have always minted customer parent batches. Kept as a literal
#: list because they are not interchangeable with the configured receipt type:
#: "Subcontracting Repack" has purpose `Repack`, while the settings validator requires the
#: configured type to have purpose `Material Receipt`
#: (``subcontracting_settings.RECEIPT_PURPOSE``). It can therefore never BE the configured
#: type, and replacing this list with the configured one alone would silently kill the
#: Subcontracting Repack leg.
_LEGACY_PARENT_BATCH_TYPES = (
	"Customer Goods Received",
	"Subcontracting Repack",
)


def _customer_gold_config():
	"""``(configured_receipt_type, configured_items)`` for the customer gold flow.

	``(None, None)`` when the flow is off or unconfigured, which is every site today —
	so the legacy behaviour below is reached unchanged unless someone has deliberately
	configured this.

	Imported inside the function, as ``_pure_qty_excluded_types`` does, to keep this
	module importable when the settings doctype has not been synced.
	"""
	from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
		get_allowed_customer_gold_items,
		get_customer_gold_settings,
		is_customer_gold_enabled,
	)

	if not is_customer_gold_enabled():
		return None, None

	settings = get_customer_gold_settings()
	return (
		settings.get("customer_goods_stock_entry_type"),
		get_allowed_customer_gold_items(settings),
	)


def _is_eligible_item(item_code, configured_items):
	"""Whether this row's item should be minted a customer parent batch.

	Two independent tests, either of which qualifies:

	* it is ONE OF the configured receipt items — resolved from Settings; and
	* it carries the ``24KT`` token — the historical rule.

	The token test is retained rather than replaced. Settings is unconfigured on every
	site today, so removing it would stop minting batches for the existing flow
	entirely. Adding identity is what lets a correctly configured item whose code
	happens not to contain ``24KT`` work at all — the C05 gap.

	Membership, not equality, since a customer may hand over more than one purity. The token
	test alone would not cover them: an additional 99.5 item need not carry ``24KT`` in its
	code, and without a batch the metal has no custody identity at all.
	"""
	# A bare string is normalised rather than trusted. ``"X" in "PREFIX-X-SUFFIX"`` is a
	# SUBSTRING test, so a caller passing one item code as a string -- which every caller did
	# before this took a list -- would silently match unrelated items whose codes contain it.
	if isinstance(configured_items, str):
		configured_items = [configured_items]

	if configured_items and item_code in configured_items:
		return True
	return "24KT" in item_code


def create_parent_batches(doc, method=None):
	configured_type, configured_items = _customer_gold_config()

	if doc.doctype == "Stock Entry":
		accepted = _LEGACY_PARENT_BATCH_TYPES + (
			(configured_type,) if configured_type else ()
		)
		if getattr(doc, "stock_entry_type", None) not in accepted:
			return

	elif doc.doctype == "Purchase Receipt":
		if getattr(doc, "purchase_type", None) != "Subcontracting":
			return

	else:
		return

	for row in doc.items:
		if not row.item_code:
			continue

		if not _is_eligible_item(row.item_code, configured_items):
			continue

		if row.batch_no:
			continue

		customer = getattr(row, "customer", None) or getattr(doc, "_customer", None)

		if not customer:
			continue

		year_code = get_year_code()
		month = datetime.today().strftime("%m")

		item_code = row.item_code

		serial = get_next_serial(customer, year_code, month)

		batch_name = f"{customer}-{year_code}{month}-{item_code}-{serial}"

		while frappe.db.exists("Batch", batch_name):
			serial = str(int(serial) + 1).zfill(2)
			batch_name = f"{customer}-{year_code}{month}-{item_code}-{serial}"

		previous_autoname_flag = frappe.flags.is_batch_autoname
		frappe.flags.is_batch_autoname = True

		try:
			batch = frappe.new_doc("Batch")
			batch.batch_id = batch_name
			batch.item = item_code
			batch.reference_doctype = doc.doctype
			batch.reference_name = doc.name
			batch.custom_voucher_detail_no = row.name
			# doc._customer is set by the customer-subcontracting orchestration on the
			# Stock Entry leg only; on a Purchase Receipt the owning customer arrives on
			# the row, resolved from the supplier's Party Link by
			# purchase_receipt/doc_events/utils.py::update_inventory_type.
			# getattr, not bare attribute access: ``_customer`` is a Custom Field on
			# Stock Entry only. Purchase Receipt has no such field, so the bare form
			# raised AttributeError on the Purchase Receipt leg (frappe's Document
			# defines no __getattr__ fallback). That crash is pre-existing for any
			# ``24KT`` item on a Subcontracting receipt; configuring a customer item
			# whose code lacks the token newly routes THOSE rows here too, turning a
			# row that used to be skipped into a hard submit failure.
			# Precedence is deliberately unchanged -- header first, then the row value
			# already resolved above -- because which customer owns a batch on a mixed
			# voucher is an ownership decision, not a crash fix. See HANDOFF.md C7.
			batch.custom_customer = getattr(doc, "_customer", None) or customer
			batch.custom_inventory_type = "Customer Goods"
			batch.custom_customer_voucher_type = "Customer Subcontracting"
			batch.custom_metal_rate = _source_row_rate(doc, row)
			batch.insert(ignore_permissions=True)
		finally:
			frappe.flags.is_batch_autoname = previous_autoname_flag

		row.batch_no = batch_name


def _source_row_rate(doc, row):
	"""Batch Rate for a batch this module builds by hand.

	These batches are inserted under ``frappe.flags.is_batch_autoname``, which makes
	``Batch.validate`` return before ``update_inventory_dimentions`` -- so the shared
	rate stamping in ``customization/batch/doc_events/utils.py`` never runs for them
	and they were created with no Batch Rate at all. Read it straight off the row
	that is minting the batch instead: the Stock Entry Detail's maintained rate
	falling back to ``basic_rate``, or the Purchase Receipt Item's ``rate``.

	Metal and finding rows only (F8). ``create_child_batches`` also mints the finished piece's
	batch on a customer order, and that row's rate is the whole piece -- customer gold, company
	alloy and diamond, production cost. Stamped as a metal rate it read as Rs.8,01,654.99 per
	"gram" on KLHGX62F1119's batch, and anything that trusts Batch Rate over ``basic_rate`` took
	the company diamond as metal. Any other item gets no Batch Rate. A metal row's value always
	belongs on ``custom_metal_rate`` -- never ``custom_alloy_rate``.
	"""
	if _variant_of(row) not in METAL_TEMPLATES:
		return 0.0

	if doc.doctype == "Stock Entry":
		return flt(row.get("custom_metal_rate")) or flt(row.get("basic_rate"))

	return flt(row.get("rate"))


#: Item templates whose batches carry a metal Batch Rate: metal and findings.
METAL_TEMPLATES = ("M", "F")


def _variant_of(row):
	if row.get("custom_variant_of"):
		return row.get("custom_variant_of")
	item_code = row.get("item_code")
	return (
		frappe.get_cached_value("Item", item_code, "variant_of") if item_code else None
	)


def get_year_code():
	year_dict = {
		"1": "A",
		"2": "B",
		"3": "C",
		"4": "D",
		"5": "E",
		"6": "F",
		"7": "G",
		"8": "H",
		"9": "I",
		"0": "J",
	}

	year = datetime.today().year
	last_two = str(year)[-2:]

	return last_two[0] + year_dict[last_two[1]]


def get_next_serial(customer, year_code, month):
	prefix = f"{customer}-{year_code}{month}"

	batch = frappe.db.sql(
		"""
        SELECT name
        FROM `tabBatch`
        WHERE name LIKE %s
        ORDER BY name DESC
        LIMIT 1
        """,
		(prefix + "%",),
		as_dict=True,
	)

	if batch:
		last_serial = batch[0].name.split("-")[-1]

		if last_serial.isdigit():
			next_serial = int(last_serial) + 1
			return str(next_serial).zfill(2)

	return "01"


def _row_lane_key(row):
	"""Ownership key of a Stock Entry row: ``(inventory_type, customer)``.

	An empty ``inventory_type`` means company stock, so it normalises to
	"Regular Stock" rather than becoming a third kind of ownership.
	"""
	return (
		getattr(row, "inventory_type", None) or "Regular Stock",
		getattr(row, "customer", None) or None,
	)


def _lane_parent_batches(doc):
	"""``{lane key: first source batch of that lane}``, in row order."""
	parents = {}
	for row in doc.items:
		if row.s_warehouse and row.batch_no:
			parents.setdefault(_row_lane_key(row), row.batch_no)
	return parents


def create_child_batches(doc, method=None):
	if doc.doctype != "Stock Entry":
		return

	# create_child_batches mints "Customer Goods" child batches for the customer-
	# subcontracting orchestration, which signals itself with doc._customer. A Metal
	# Conversion that draws across mixed ownership has no single owning customer on the
	# header, so a row-level customer also opens the gate. SEs outside either case (e.g.
	# auto-created "Process Loss") must not mint child batches -- their batch-tracked
	# produce items auto-create a batch on submit.
	header_customer = getattr(doc, "_customer", None)
	if not header_customer and not any(
		getattr(row, "customer", None) for row in doc.items
	):
		return

	parents = _lane_parent_batches(doc)

	# A voucher whose rows are all one ownership is handled exactly as before: one
	# parent batch for the whole entry, every batch-less produce row minted from it.
	# Every pre-existing caller (SNC's create_repack_metal_conversion, Customer Goods
	# Received, Subcontracting Repack) builds such a voucher, so their behaviour is
	# unchanged by the lane handling below -- which matters because SNC reads its target
	# batch back off Stock Entry Detail and throws if nothing was minted.
	single_lane = len(parents) <= 1

	if not parents:
		return

	for row in doc.items:
		if row.s_warehouse or row.batch_no:
			continue

		if not row.t_warehouse:
			continue

		lane_key = _row_lane_key(row)

		if single_lane:
			parent_batch = next(iter(parents.values()))
			customer = getattr(row, "customer", None) or header_customer
		else:
			# Mixed ownership: only customer-owned produce rows get a customer child
			# batch. A Regular Stock row is left with an empty batch_no on purpose so
			# the Serial-and-Batch path mints it -- that is the only path that stamps
			# ownership from the row itself and runs the Customer-Goods item guard.
			if lane_key[0] not in CUSTOMER_INVENTORY_TYPES:
				continue

			parent_batch = parents.get(lane_key)
			if not parent_batch:
				continue

			customer = lane_key[1] or header_customer

		parts = parent_batch.split("-")
		if len(parts) < 4:
			# The parent was not named by this module (e.g. a Customer Goods batch
			# created by a customer Purchase Receipt has only three segments), so there
			# is no serial to extend. Skip this row and let the Serial-and-Batch path
			# mint it -- historically this aborted the whole voucher.
			continue

		item_code = row.item_code
		parent_serial = parts[-1]

		if customer:
			prefix = f"{customer}-{parts[1]}"
		else:
			prefix = f"{parts[0]}-{parts[1]}"
		base_name = f"{prefix}-{item_code}-{parent_serial}"

		batches = frappe.db.sql(
			"""
            SELECT name
            FROM `tabBatch`
            WHERE name LIKE %s
            ORDER BY name DESC
            """,
			(base_name + "-%",),
			as_dict=True,
		)

		alphabet = "A"
		if batches:
			last_alpha = batches[0].name.split("-")[-1]
			if last_alpha in string.ascii_uppercase:
				idx = string.ascii_uppercase.index(last_alpha)
				if idx + 1 >= len(string.ascii_uppercase):
					frappe.throw(
						_(
							"Cannot create child batch for {0}: parent batch {1} "
							"already uses all 26 child suffixes (A-Z)."
						).format(base_name, parent_batch)
					)
				alphabet = string.ascii_uppercase[idx + 1]

		batch_name = f"{base_name}-{alphabet}"

		while frappe.db.exists("Batch", batch_name):
			if alphabet == "Z":
				frappe.throw(
					_(
						"Cannot create a unique child batch for {0}: all 26 child "
						"suffixes (A-Z) are already used."
					).format(base_name)
				)

			alphabet = chr(ord(alphabet) + 1)
			batch_name = f"{base_name}-{alphabet}"

		previous_autoname_flag = frappe.flags.is_batch_autoname
		frappe.flags.is_batch_autoname = True

		try:
			batch = frappe.new_doc("Batch")
			batch.batch_id = batch_name
			batch.item = item_code
			batch.reference_doctype = doc.doctype
			batch.reference_name = doc.name
			batch.custom_voucher_detail_no = row.name
			# The owning customer comes from the ROW on a mixed voucher: the header
			# describes at most one lane, and stamping it everywhere is what would
			# mislabel another lane's target batch.
			batch.custom_customer = customer or header_customer
			batch.custom_inventory_type = "Customer Goods"
			batch.custom_customer_voucher_type = "Customer Subcontracting"
			batch.custom_metal_rate = _source_row_rate(doc, row)
			batch.insert(ignore_permissions=True)
		finally:
			frappe.flags.is_batch_autoname = previous_autoname_flag

		row.batch_no = batch_name


def get_purity(item_code):
	item = frappe.get_doc("Item", item_code)
	purity = 100
	for attr in item.attributes:
		if attr.attribute == "Metal Purity":
			purity = flt(attr.attribute_value)
	return purity


def create_repack_for_used_other(doc, method=None):
	if doc.doctype != "Stock Entry":
		return

	if doc.stock_entry_type not in [
		"Customer Goods Received",
		"Customer Goods Transfer",
	]:
		return

	for item in doc.items:
		if not getattr(item, "t_warehouse", "").startswith("Central RM"):
			return

	source_customer = (
		getattr(doc, "_customer", None)
		or getattr(doc, "customer", None)
		or next(
			(row.customer for row in doc.items if getattr(row, "customer", None)), None
		)
	)

	if not source_customer:
		return []

	columns, report_data = get_report_data(filters={"other_customer": source_customer})
	if not report_data:
		return []

	matched_rows = []

	for row in report_data:
		try:
			batch_no = row[0]
			owner = row[1]
			item = row[2]
			used_other = flt(row[5])
			other_customer = row[6]
		except Exception:
			continue

		if not other_customer or used_other <= 0 or not owner:
			continue

		other_customers = [
			c.strip() for c in (other_customer or "").split(",") if c.strip()
		]
		if source_customer in other_customers:
			matched_rows.append(
				{
					"batch_no": batch_no,
					"owner": owner,
					"item": item,
					"used_other": used_other,
				}
			)

	if not matched_rows:
		return []

	matched_rows.sort(key=lambda x: x["batch_no"])

	source_batch = next((d.batch_no for d in doc.items if d.batch_no), None)
	source_warehouse = next((d.s_warehouse for d in doc.items if d.batch_no), None)

	if not source_batch:
		return

	total_source_qty = sum(flt(d.qty) for d in doc.items if d.batch_no)
	source_item = next((d.item_code for d in doc.items if d.batch_no), None)

	source_purity = get_purity(source_item)
	total_source_24kt = total_source_qty * (source_purity / 100)

	remaining_qty = total_source_24kt

	for row in matched_rows:
		if remaining_qty <= 0:
			break

		child_batch = row["batch_no"]
		item_code = row["item"]
		used_other = row["used_other"]
		owner = row["owner"]

		if owner == source_customer:
			continue

		linked_batches = get_linked_batches(child_batch)

		parent_batch = None
		for b in linked_batches:
			item = frappe.get_value("Batch", b, "item")
			if item and "24KT" in item:
				parent_batch = b
				break

		if not parent_batch:
			continue

		parent_item = frappe.get_value("Batch", parent_batch, "item")

		already_repacked = (
			frappe.db.sql(
				"""
			SELECT IFNULL(SUM(sed_source.qty), 0)
			FROM `tabStock Entry` se
			JOIN `tabStock Entry Detail` sed_source
				ON sed_source.parent = se.name AND sed_source.is_finished_item = 0
			JOIN `tabStock Entry Detail` sed_target
				ON sed_target.parent = se.name AND sed_target.is_finished_item = 1
			WHERE se.stock_entry_type = 'Subcontracting Repack'
			AND sed_target.batch_no = %s
			AND sed_source.batch_no = %s
			AND se.docstatus = 1
		""",
				(parent_batch, source_batch),
			)[0][0]
			or 0
		)

		purity = get_purity(item_code)
		used_other_24kt = used_other * (purity / 100)

		pending_qty = used_other_24kt - already_repacked

		if pending_qty <= 0:
			continue

		process_qty = min(pending_qty, remaining_qty)

		purity = get_purity(item_code)
		converted_qty = process_qty * (purity / 100)

		try:
			se = frappe.new_doc("Stock Entry")
			se.stock_entry_type = "Subcontracting Repack"
			se.purpose = "Repack"
			se.company = doc.company

			se.append(
				"items",
				{
					"item_code": parent_item,
					"batch_no": source_batch,
					"qty": converted_qty,
					"s_warehouse": source_warehouse,
					"customer": source_customer,
					"inventory_type": "Regular Stock",
					"is_finished_item": 0,
					"use_serial_batch_fields": 1,
				},
			)

			se.append(
				"items",
				{
					"item_code": parent_item,
					"batch_no": parent_batch,
					"qty": converted_qty,
					"t_warehouse": source_warehouse,
					"customer": owner,
					"inventory_type": "Regular Stock",
					"is_finished_item": 1,
					"use_serial_batch_fields": 1,
				},
			)

			se.insert(ignore_permissions=True)
			se.submit()

		except Exception as e:
			frappe.log_error("Repack Error", str(e))
			continue

		remaining_qty -= process_qty
