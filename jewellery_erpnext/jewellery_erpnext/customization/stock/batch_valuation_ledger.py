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

	def initialize(self, sles: list, exclude_voucher_no: str = "", exclude_voucher_detail_nos: set = None):
		"""Initialize ledger with historical batch data for given SLEs."""
		self._ledger_data = self.get_historical_batch_ledger_data(sles, exclude_voucher_no, exclude_voucher_detail_nos)

	def get_historical_batch_ledger_data(self, sles: list, exclude_voucher_no: str = "", exclude_voucher_detail_nos: set = None):
		"""Fetch historical batch data for outward SLEs with precise exclusion and temporal logic."""
		outward_sles = [sle for sle in sles if flt(sle.get("actual_qty", 0)) < 0]
		if not outward_sles:
			return {}

		serial_bundle_ids = set()
		batch_nos = set()

		for sle in outward_sles:
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

		# Build timestamp conditions for all SLEs
		timestamp_conditions = []
		params = {
			"batch_nos": tuple(batchwise_batch_nos),
			"exclude_voucher_no": exclude_voucher_no,
		}

		if exclude_voucher_detail_nos:
			params["exclude_voucher_detail_nos"] = tuple(exclude_voucher_detail_nos)
			voucher_exclusion_clause = "AND sb.voucher_detail_no NOT IN %(exclude_voucher_detail_nos)s"
		else:
			voucher_exclusion_clause = "AND sb.voucher_no <> %(exclude_voucher_no)s"

		for i, sle in enumerate(outward_sles):
			pd = sle.get("posting_date")
			pt = sle.get("posting_time") or nowtime()
			cr = sle.get("creation") or "1900-01-01 00:00:00"

			params[f"pd_{i}"] = pd
			params[f"pt_{i}"] = pt
			params[f"cr_{i}"] = cr

			timestamp_conditions.append(f"""
				(sb.posting_date < %(pd_{i})s)
				OR (sb.posting_date = %(pd_{i})s AND sb.posting_time < %(pt_{i})s)
				OR (sb.posting_date = %(pd_{i})s AND sb.posting_time = %(pt_{i})s AND sb.creation < %(cr_{i})s)
			""")

		final_timestamp_filter = f"AND ({' OR '.join(timestamp_conditions)})"

		sql = f"""
			SELECT
				sb.warehouse,
				sb.item_code,
				sbe.batch_no,
				SUM(sbe.stock_value_difference) AS incoming_rate,
				SUM(sbe.qty) AS qty
			FROM `tabSerial and Batch Bundle` sb
			INNER JOIN `tabSerial and Batch Entry` sbe ON sb.name = sbe.parent
			INNER JOIN `tabBatch` b ON sbe.batch_no = b.name
			WHERE
				b.use_batchwise_valuation = 1
				AND sb.docstatus = 1
				AND sb.is_cancelled = 0
				AND sb.type_of_transaction IN ('Inward', 'Outward')
				AND sb.voucher_type <> 'Pick List'
				AND sbe.batch_no IN %(batch_nos)s
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


# import frappe
# from frappe.utils import flt, nowtime
# from collections import defaultdict

# class BatchValuationLedger:
# 	"""
# 	Efficient in-memory index of historical batch valuation data.
# 	"""

# 	def __init__(self):
# 		self._ledger_data = defaultdict(list)

# 	def initialize(self, sles: list, exclude_voucher_no: str):
# 		# Prefetch all relevant batch ledger entries into memory
# 		entries = self._preload_ledger_data(sles, exclude_voucher_no)
# 		for row in entries:
# 			key = (row.warehouse, row.item_code, row.batch_no)
# 			self._ledger_data[key].append(row)

# 	def _preload_ledger_data(self, sles, exclude_voucher_no):
# 		serial_bundle_ids = {sle["serial_and_batch_bundle"] for sle in sles if sle.get("serial_and_batch_bundle")}
# 		batch_nos = {sle["batch_no"] for sle in sles if sle.get("batch_no")}

# 		# Bulk fetch all batch_nos from serial_and_batch_bundles
# 		if serial_bundle_ids:
# 			batch_nos.update(
# 				r["batch_no"] for r in frappe.get_all(
# 					"Serial and Batch Entry",
# 					filters={"parent": ["in", list(serial_bundle_ids)]},
# 					fields=["batch_no"]
# 				) if r["batch_no"]
# 			)

# 		if not batch_nos:
# 			return []

# 		# Run SQL join against `Batch` for only batchwise_valuation = 1 entries
# 		sql = """
# 			SELECT
# 				sb.warehouse,
# 				sb.item_code,
# 				sbe.batch_no,
# 				sb.posting_date,
# 				sb.posting_time,
# 				sb.creation,
# 				sb.voucher_no,
# 				sb.voucher_detail_no,
# 				sbe.stock_value_difference,
# 				sbe.qty
# 			FROM `tabSerial and Batch Bundle` sb
# 			INNER JOIN `tabSerial and Batch Entry` sbe ON sb.name = sbe.parent
# 			INNER JOIN `tabBatch` b ON sbe.batch_no = b.name
# 			WHERE
# 				b.use_batchwise_valuation = 1
# 				AND sbe.batch_no IN %(batch_nos)s
# 				AND sb.docstatus = 1
# 				AND sb.is_cancelled = 0
# 				AND sb.type_of_transaction IN ('Inward', 'Outward')
# 				AND sb.voucher_type <> 'Pick List'
# 				AND sb.voucher_no <> %(exclude_voucher_no)s
# 		"""
# 		return frappe.db.sql(
# 			sql,
# 			{
# 				"batch_nos": tuple(batch_nos),
# 				"exclude_voucher_no": exclude_voucher_no
# 			},
# 			as_dict=True
# 		)

# 	def get_batch_data(self, warehouse, item_code, batch_no, posting_dt=None, creation=None, exclude_voucher_no=None, exclude_voucher_detail_no=None):
# 		key = (warehouse, item_code, batch_no)
# 		entries = self._ledger_data.get(key)
# 		if not entries:
# 			return None

# 		filtered = [
# 			row for row in entries
# 			if (
# 				# Timestamp condition
# 				not posting_dt or
# 				f"{row.posting_date} {row.posting_time}" < posting_dt or
# 				(
# 					f"{row.posting_date} {row.posting_time}" == posting_dt and
# 					creation and row.creation and row.creation < creation
# 				)
# 			)
# 			and not (
# 				exclude_voucher_detail_no and row.voucher_detail_no == exclude_voucher_detail_no or
# 				not exclude_voucher_detail_no and row.voucher_no == exclude_voucher_no
# 			)
# 		]

# 		if not filtered:
# 			return None

# 		return {
# 			"incoming_rate": sum(flt(r.stock_value_difference) for r in filtered),
# 			"qty": sum(flt(r.qty) for r in filtered)
# 		}

# 	def update(self, sle, bundle_entries):
# 		if not bundle_entries:
# 			return
# 		key_template = (sle.warehouse, sle.item_code)
# 		now_time = sle.posting_time or nowtime()
# 		for entry in bundle_entries:
# 			key = (*key_template, entry.batch_no)
# 			self._ledger_data[key].append(frappe._dict({
# 				"warehouse": sle.warehouse,
# 				"item_code": sle.item_code,
# 				"batch_no": entry.batch_no,
# 				"posting_date": sle.posting_date,
# 				"posting_time": now_time,
# 				"creation": sle.creation,
# 				"voucher_no": sle.voucher_no,
# 				"voucher_detail_no": sle.voucher_detail_no,
# 				"stock_value_difference": entry.stock_value_difference,
# 				"qty": entry.qty
# 			}))

# 	def clear(self):
# 		self._ledger_data.clear()
