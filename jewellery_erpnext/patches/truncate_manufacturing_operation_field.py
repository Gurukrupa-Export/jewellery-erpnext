import frappe

def execute():
    """
    Truncate 'manufacturing_operation' to 140 characters to allow indexing
    and avoid schema alteration errors.
    """
    # Count affected rows
    count = frappe.db.count(
        'Stock Entry Detail',
        filters={"manufacturing_operation": ("length", ">", 140)}
    )
    print(f"Truncating {count} rows in 'manufacturing_operation' field...")

    # Truncate all rows > 191 chars
    frappe.db.sql("""
        UPDATE `tabStock Entry Detail`
        SET `manufacturing_operation` = LEFT(`manufacturing_operation`, 140)
        WHERE CHAR_LENGTH(`manufacturing_operation`) > 140
    """)

    print("Truncation completed successfully.")
