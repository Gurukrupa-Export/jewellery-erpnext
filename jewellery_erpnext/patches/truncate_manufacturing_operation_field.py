import frappe

def execute():
    """
    Truncate 'manufacturing_operation' to 140 characters to allow indexing
    and avoid schema alteration errors.
    """
    # Count affected rows
    count = frappe.db.sql("""
        SELECT COUNT(*) as count FROM `tabStock Entry Detail`
        WHERE CHAR_LENGTH(`manufacturing_operation`) > 140
    """, as_dict=True)[0]["count"]

    print(f"Truncating {count} rows in 'manufacturing_operation' field...")

    # Truncate all rows > 191 chars
    frappe.db.sql("""
        UPDATE `tabStock Entry Detail`
        SET `manufacturing_operation` = LEFT(`manufacturing_operation`, 140)
        WHERE CHAR_LENGTH(`manufacturing_operation`) > 140
    """)

    print("Truncation completed successfully.")
