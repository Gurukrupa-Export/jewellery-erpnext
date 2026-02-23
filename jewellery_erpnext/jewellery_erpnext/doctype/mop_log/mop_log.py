# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MOPLog(Document):
	def validate(self):
		update_value = {}

		code0 = self.item_code[0]
		qty = self.qty_after_transaction
		pcs = self.pcs_after_transaction
		FIELD_MAP = {
			"M": "net",
			"F": "finding",
			"D": "diamond",
			"G": "gemstone",
			"O": "other",
		}

		prefix = FIELD_MAP.get(code0)

		if not prefix:
			return  # or handle invalid code
		update_value[f"{prefix}_wt"] = qty
		if code0 in ("D", "G"):
			update_value.update(
				{
					f"{prefix}_wt_in_gram": qty * 0.2,
					f"{prefix}_pcs": pcs,
				}
			)
		update_value["gross_wt"] = self.qty_after_transactionmop
		frappe.db.set_value(
			"Manufacturing Operation", self.manufacturing_operation, update_value
		)
