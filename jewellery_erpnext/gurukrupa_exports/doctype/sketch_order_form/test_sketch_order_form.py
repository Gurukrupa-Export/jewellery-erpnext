# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now


class TestSketchOrderForm(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        create_test_data()
        cls.department = frappe.get_value(
            "Department", {"department_name": "Test_Department"}, "name"
        )
        cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

    def test_sketch_order_created(self):
        sk_ord_frm = make_sketch_order_form(
            department=self.department,
            branch=self.branch,
            order_type="Sales",
            design_type="New Design",
        )

        sketch_order = frappe.get_all(
            "Sketch Order",
            filters={"sketch_order_form": sk_ord_frm.name, "docstatus": 0},
        )
        self.assertEqual(len(sketch_order), len(sk_ord_frm.order_details))

    def test_sketch_order_created_mod_design(self):
        item = frappe.db.get_value(
            "Item", {"has_variants": 0}, "name", order_by="creation desc"
        )
        sk_ord_frm = make_sketch_order_form(
            department=self.department,
            branch=self.branch,
            order_type="Sales",
            design_type="Mod",
            design_code=item,
        )

        sketch_order = frappe.get_all(
            "Sketch Order",
            filters={"sketch_order_form": sk_ord_frm.name, "docstatus": 0},
        )
        self.assertEqual(len(sketch_order), len(sk_ord_frm.order_details))

    def test_purchase_order_created(self):
        sk_ord_frm = make_sketch_order_form(
            department=self.department,
            branch=self.branch,
            order_type="Purchase",
            design_type="New Design",
        )

        sketch_order = frappe.get_all(
            "Sketch Order",
            filters={"sketch_order_form": sk_ord_frm.name, "docstatus": 0},
        )
        self.assertEqual(len(sketch_order), len(sk_ord_frm.order_details))

        po = frappe.get_all(
            "Purchase Order",
            filters={"custom_form_id": sk_ord_frm.name, "docstatus": 0},
        )
        self.assertEqual(len(po), 1)


def create_test_data():
    if not frappe.db.exists("Customer", "Test_Customer_External"):
        customer = frappe.get_doc(
            {
                "doctype": "Customer",
                "customer_name": "Test_Customer_External",
                "customer_type": "Individual",
                "custom_sketch_workflow_state": "External",
            }
        )
        customer.append(
            "diamond_grades",
            {
                "diamond_quality": "EF-VVS",
                "diamond_grade_1": "6B",
                "diamond_grade_2": "4",
            },
        )
        customer.save()

    if not frappe.db.exists("Customer", "Test_Customer_Internal"):
        customer = frappe.get_doc(
            {
                "doctype": "Customer",
                "customer_name": "Test_Customer_Internal",
                "customer_type": "Individual",
                "custom_sketch_workflow_state": "Internal",
            }
        )
        customer.append(
            "diamond_grades",
            {
                "diamond_quality": "EF-VVS",
                "diamond_grade_1": "6B",
                "diamond_grade_2": "4",
            },
        )
        customer.save()

    if not frappe.db.exists("Supplier", "Test_Supplier"):
        supplier = frappe.get_doc(
            {"doctype": "Supplier", "supplier_name": "Test_Supplier"}
        )
        supplier.save()

    if not frappe.db.exists("Department", {"department_name": "Test_Department"}):
        dep = frappe.get_doc(
            {
                "doctype": "Department",
                "department_name": "Test_Department",
                "company": "Gurukrupa Export Private Limited",
            }
        )
        dep.save()

    if not frappe.db.exists("Branch", {"branch_name": "Test Branch"}):
        branch = frappe.get_doc(
            {
                "doctype": "Branch",
                "branch": "Test Branch",
                "branch_name": "Test Branch",
                "company": "Gurukrupa Export Private Limited",
            }
        )
        branch.save()

    if not frappe.db.exists("Sales Person", "Test_Sales_Person"):
        salesman = frappe.get_doc(
            {"doctype": "Sales Person", "sales_person_name": "Test_Sales_Person"}
        )
        salesman.save()


def make_sketch_order_form(**args):
    args = frappe._dict(args)
    sketch_order_form = frappe.new_doc("Sketch Order Form")
    sketch_order_form.company = "Gurukrupa Export Private Limited"
    sketch_order_form.customer_code = "Test_Customer_External"
    sketch_order_form.department = args.department
    sketch_order_form.branch = args.branch
    sketch_order_form.salesman_name = "Test_Sales_Person"
    sketch_order_form.order_type = args.order_type
    sketch_order_form.order_date = now()
    sketch_order_form.due_days = 4
    sketch_order_form.delivery_date = add_days(now(), 4)
    sketch_order_form.design_by = "Customer Design"
    if args.order_type == "Purchase":
        sketch_order_form.supplier = "Test_Supplier"

    if args.design_type == "Mod":
        sketch_order_form.append(
            "order_details",
            {
                "design_type": args.design_type,
                "metal_type": "Gold",
                "tag__design_id": args.design_code,
                "budget": 50000,
                "metal_target": 1.1,
                "diamond_target": 1.25,
                "product_size": "10",
                "sizer_type": "Rod",
                "gemstone_type": "Ruby",
                "stone_changeable": "No",
                "length": 10,
                "width": 10,
                "height": 10,
                "diamond_part_length": 10,
                "gemstone_size": "1.60*1.00 MM",
            },
        )

        sketch_order_form.append(
            "order_details",
            {
                "design_type": args.design_type,
                "metal_type": "Gold",
                "tag__design_id": args.design_code,
                "budget": 50000,
                "metal_target": 1.1,
                "diamond_target": 1.5,
                "product_size": "10",
                "sizer_type": "Rod",
                "gemstone_type": "Ruby",
                "stone_changeable": "No",
                "length": 10,
                "width": 10,
                "height": 10,
                "diamond_part_length": 10,
                "gemstone_size": "1.60*1.00 MM",
            },
        )

    else:
        sketch_order_form.append(
            "order_details",
            {
                "design_type": args.design_type,
                "category": "Mugappu",
                "subcategory": "Casual Mugappu",
                "setting_type": "Open",
                "sub_setting_type1": "Close-Open Setting",
                "metal_type": "Gold",
                "metal_color": "Yellow",
                "metal_touch": "18KT",
                "budget": 50000,
                "metal_target": 1.1,
                "diamond_target": 1.25,
                "product_size": "10",
                "sizer_type": "Scale",
                "gemstone_type": "Ruby",
                "stone_changeable": "No",
                "length": 10,
                "width": 10,
                "height": 10,
                "diamond_part_length": 10,
                "gemstone_size": "1.60*1.00 MM",
            },
        )

    sketch_order_form.append("age_group", {"design_attribute": "0-12"})
    sketch_order_form.append("gender", {"design_attribute": "Kids"})
    sketch_order_form.append("occasion", {"design_attribute": "Diwali"})
    sketch_order_form.append("rhodium", {"design_attribute": "Black"})
    sketch_order_form.append("india_states", {"design_attribute": "Gujarat"})

    sketch_order_form.save()
    apply_workflow(sketch_order_form, "Send For Approval")
    apply_workflow(sketch_order_form, "Approve")

    return sketch_order_form
