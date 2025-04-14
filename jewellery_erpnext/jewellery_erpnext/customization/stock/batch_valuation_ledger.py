import frappe
from frappe.utils import flt, nowtime
from frappe import qb
from frappe.query_builder.functions import CombineDatetime, Sum
from frappe.query_builder import Criterion

class BatchValuationLedger:
    """
    Repository for batch valuation data in Serial and Batch Bundles.
    - Fetches and stores historical batch data for efficient reuse.
    - Supports in-flight updates during stock transactions.
    """
    def __init__(self):
        self._ledger_data = None

    def initialize(self, sles: list, exclude_voucher_no: str):
        """Initialize ledger with historical batch data for given SLEs."""
        self._ledger_data = self.get_historical_batch_ledger_data(sles, exclude_voucher_no)

    def get_historical_batch_ledger_data(self, sles: list, exclude_voucher_no: str):
        """Fetch historical batch data for outward SLEs."""
        outward_sles = [sle for sle in sles if flt(sle.get("actual_qty", 0)) < 0]
        if not outward_sles:
            return {}

        # Collect batch numbers from SLE bundles or direct batch_no
        batch_nos = set()
        for sle in outward_sles:
            if sle.get("serial_and_batch_bundle"):
                bundles = frappe.get_all(
                    "Serial and Batch Entry",
                    filters={"parent": sle.get("serial_and_batch_bundle")},
                    fields=["batch_no"]
                )
                batch_nos.update(b.batch_no for b in bundles if b.batch_no)
            elif sle.get("batch_no"):
                batch_nos.add(sle.get("batch_no"))

        if not batch_nos:
            return {}

        # Filter batches with batchwise valuation enabled
        batchwise_valuation_batches = set(
            b.name for b in frappe.get_all(
                "Batch",
                filters={"name": ("in", list(batch_nos)), "use_batchwise_valuation": 1},
                fields=["name"]
            )
        )
        if not batchwise_valuation_batches:
            return {}

        # Build query for historical data
        parent = qb.DocType("Serial and Batch Bundle")
        child = qb.DocType("Serial and Batch Entry")
        query = (
            qb.from_(parent)
            .inner_join(child)
            .on(parent.name == child.parent)
            .select(
                parent.warehouse,
                parent.item_code,
                child.batch_no,
                Sum(child.stock_value_difference).as_("incoming_rate"),
                Sum(child.qty).as_("qty"),
            )
            .where(
                (child.batch_no.isin(list(batchwise_valuation_batches)))
                & (parent.docstatus == 1)
                & (parent.is_cancelled == 0)
                & (parent.type_of_transaction.isin(["Inward", "Outward"]))
                & (parent.voucher_type != "Pick List")
                & (parent.voucher_no != exclude_voucher_no)
            )
            .groupby(child.batch_no)
        )

        # Add per-SLE filters (warehouse, item, timestamp)
        conditions = []
        for sle in outward_sles:
            posting_date = sle.get("posting_date")
            posting_time = sle.get("posting_time") or nowtime()
            creation = sle.get("creation")

            timestamp_condition = None
            if posting_date:
                timestamp_condition = CombineDatetime(parent.posting_date, parent.posting_time) < CombineDatetime(
                    posting_date, posting_time
                )
                if creation:
                    timestamp_condition |= (
                        (CombineDatetime(parent.posting_date, parent.posting_time) == CombineDatetime(posting_date, posting_time))
                        & (parent.creation < creation)
                    )

            condition = (
                (parent.warehouse == sle.get("warehouse"))
                & (parent.item_code == sle.get("item_code"))
            )
            if timestamp_condition:
                condition &= timestamp_condition
            if sle.get("voucher_detail_no"):
                condition &= (parent.voucher_detail_no != sle.get("voucher_detail_no"))
            elif sle.get("voucher_no"):
                condition &= (parent.voucher_no != sle.get("voucher_no"))
            conditions.append(condition)

        if conditions:
            query = query.where(Criterion.any(conditions))

        # Execute and format results
        results = query.run(as_dict=True)
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