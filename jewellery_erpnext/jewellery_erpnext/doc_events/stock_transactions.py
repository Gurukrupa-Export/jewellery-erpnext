import frappe

def set_batch_certificate_id(doc, method=None):
    for row in doc.items:
        cert_id = row.get("custom_certificate_id")
        if not cert_id:
            continue

        for bundle_field in ("serial_and_batch_bundle", "rejected_serial_and_batch_bundle"):
            bundle_name = row.get(bundle_field)
            if not bundle_name:
                continue

            bundle_type = frappe.db.get_value(
                "Serial and Batch Bundle", bundle_name, "type_of_transaction"
            )
            if bundle_type != "Inward":
                continue

            batch_nos = frappe.get_all(
                "Serial and Batch Entry",
                filters={"parent": bundle_name},
                pluck="batch_no",
            )
            for batch_no in set(filter(None, batch_nos)):
                frappe.db.set_value("Batch", batch_no, "custom_certificate_id", cert_id)