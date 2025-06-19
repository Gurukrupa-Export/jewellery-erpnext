import frappe
from frappe.utils import flt, nowtime

class BatchValuationLedger:
	"""
	Repository for batch valuation data in Serial and Batch Bundles.
	- Fetches and stores historical batch data for efficient reuse.
	- Supports in-flight updates during stock transactions.
	"""
	def __init__(self):
		self._ledger_data = None

	def initialize(self, sles: list, creation=None, exclude_voucher_no: str = "", exclude_voucher_detail_nos: set = None):
		"""Initialize ledger with historical batch data for given SLEs."""
		self._ledger_data = self.get_historical_batch_ledger_data(sles, creation, exclude_voucher_no, exclude_voucher_detail_nos)

	def get_historical_batch_ledger_data(self, sles: list, creation: str=None, exclude_voucher_no: str = "", exclude_voucher_detail_nos: set = None):
		"""Fetch historical batch data for outward SLEs with precise exclusion and temporal logic."""
		outward_sles = [sle for sle in sles if flt(sle.get("actual_qty", 0)) < 0]
		if not outward_sles:
			return {}

		serial_bundle_ids = set()
		batch_nos = set()
		warehouses = set()
		item_codes = set()

		for sle in outward_sles:
			warehouses.add(sle["warehouse"])
			item_codes.add(sle["item_code"])

			if sle.get("serial_and_batch_bundle"):
				serial_bundle_ids.add(sle["serial_and_batch_bundle"])
			elif sle.get("batch_no"):
				batch_nos.add(sle["batch_no"])

		if serial_bundle_ids:
			bundle_batches = frappe.get_all(
				"Serial and Batch Entry",
				filters={"parent": ["in", list(serial_bundle_ids)]},
				fields=["batch_no"]
			)
			batch_nos.update(b.batch_no for b in bundle_batches if b.batch_no)

		if not batch_nos:
			return {}

		batchwise_batches = frappe.get_all(
			"Batch",
			filters={"name": ["in", list(batch_nos)], "use_batchwise_valuation": 1},
			fields=["name"]
		)
		batchwise_batch_nos = [b.name for b in batchwise_batches]

		if not batchwise_batch_nos:
			return {}

		params = {
			"batch_nos": tuple(batchwise_batch_nos),
			"warehouses": tuple(warehouses),
			"item_codes": tuple(item_codes),
			"exclude_voucher_no": exclude_voucher_no,
		}

		if exclude_voucher_detail_nos:
			params["exclude_voucher_detail_nos"] = tuple(exclude_voucher_detail_nos)
			voucher_exclusion_clause = "AND sb.voucher_detail_no NOT IN %(exclude_voucher_detail_nos)s"
		else:
			voucher_exclusion_clause = "AND sb.voucher_no <> %(exclude_voucher_no)s"

		timestamp_conditions = []
		if outward_sles[0].get("posting_date"):
			params["posting_date"] = outward_sles[0]["posting_date"]
			posting_time = outward_sles[0].get("posting_time") or nowtime()
			params["posting_time"] = posting_time

			timestamp_conditions.append("sb.posting_date < %(posting_date)s")
			timestamp_conditions.append("(sb.posting_date = %(posting_date)s AND sb.posting_time < %(posting_time)s)")

			if creation:
				params["creation"] = creation
				timestamp_conditions.append("(sb.posting_date = %(posting_date)s AND sb.posting_time = %(posting_time)s AND sb.creation < %(creation)s)")

		final_timestamp_filter = f"AND ({' OR '.join(timestamp_conditions)})" if timestamp_conditions else ""

		sql = f"""
			SELECT
				sb.warehouse,
				sb.item_code,
				sbe.batch_no,
				SUM(sbe.stock_value_difference) AS incoming_rate,
				SUM(sbe.qty) AS qty
			FROM `tabSerial and Batch Bundle` sb
			INNER JOIN `tabSerial and Batch Entry` sbe ON sb.name = sbe.parent
			WHERE
				sbe.batch_no IN %(batch_nos)s
				AND sb.warehouse IN %(warehouses)s
				AND sb.item_code IN %(item_codes)s
				AND sb.docstatus = 1
				AND sb.is_cancelled = 0
				AND sb.type_of_transaction IN ('Inward', 'Outward')
				AND sb.voucher_type <> 'Pick List'
				{voucher_exclusion_clause}
				{final_timestamp_filter}
			GROUP BY sb.warehouse, sb.item_code, sbe.batch_no
		"""

		results = frappe.db.sql(sql, params, as_dict=True)

		return {
			(row.warehouse, row.item_code, row.batch_no): {
				"incoming_rate": flt(row.incoming_rate),
				"qty": flt(row.qty)
			}
			for row in results
		}

	def update(self, sle, bundle_entries):
		"""Update ledger with in-flight bundle data from current SLE."""
		if not self._ledger_data or not bundle_entries:
			return

		for entry in bundle_entries:
			key = (sle.warehouse, sle.item_code, entry.batch_no)
			self._ledger_data[key] = {
				"incoming_rate": flt(self._ledger_data.get(key, {}).get("incoming_rate", 0.0)) + flt(entry.stock_value_difference),
				"qty": flt(self._ledger_data.get(key, {}).get("qty", 0.0)) + flt(entry.qty)
			}

	def get_batch_data(self, warehouse, item_code, batch_no):
		"""Retrieve batch data for a specific warehouse, item, and batch."""
		if not self._ledger_data:
			return None
		return self._ledger_data.get((warehouse, item_code, batch_no))

	def clear(self):
		"""Reset ledger data."""
		self._ledger_data = None
