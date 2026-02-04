# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.gurukrupa_exports.doctype.sketch_order_form.test_sketch_order_form import (
    make_sketch_order_form,
)


class TestSketchOrder(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.department = frappe.get_value(
            "Department", {"department_name": "Test_Department"}, "name"
        )
        cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

    def test_sketch_order_purchase(self):
        sketch_order_form = make_sketch_order_form(
            department=self.department,
            branch=self.branch,
            order_type="Purchase",
            supplier="Test_Supplier",
            design_type="New Design",
        )

        sketch_order = frappe.get_doc(
            "Sketch Order",
            frappe.get_value(
                "Sketch Order", filters={"sketch_order_form": sketch_order_form.name}
            ),
        )

        sketch_order.sketch_image = "https://www.chidambaramcovering.in/image/cache/catalog/Mogappu%20Chain/mchn510-gold-plated-jewellery-mugappu-design-without-stone-5-425x500.jpg.webp"
        sketch_order.save()
        apply_workflow(sketch_order, "Update")
        sketch_order.reload()
        sketch_order.final_sketch_image = sketch_order.sketch_image
        sketch_order.save()
        apply_workflow(sketch_order, "Update")
        apply_workflow(sketch_order, "Send to QC")
        apply_workflow(sketch_order, "Approve")
        self.assertEqual(
            sketch_order.name,
            frappe.get_value("Item", sketch_order.item_code, "custom_sketch_order_id"),
        )

    def test_sketch_order_sales(self):
        sketch_order_form = make_sketch_order_form(
            department=self.department,
            branch=self.branch,
            order_type="Sales",
            design_type="New Design",
        )

        sketch_order = frappe.get_doc(
            "Sketch Order",
            frappe.get_value(
                "Sketch Order", filters={"sketch_order_form": sketch_order_form.name}
            ),
        )
        sketch_order.append(
            "designer_assignment", {"designer": "GEPL - 00202", "count_1": 1}
        )
        sketch_order.save()

        apply_workflow(sketch_order, "Assigned")
        apply_workflow(sketch_order, "Complete")
        for row in sketch_order.rough_sketch_approval:
            row.approved = 1
        sketch_order.save()

        apply_workflow(sketch_order, "Approve")
        qty = 0
        self.assertEqual(
            len(sketch_order.designer_assignment),
            len(sketch_order.rough_sketch_approval),
        )
        for row in sketch_order.final_sketch_approval:
            row.approved = 1
            qty += 1
        sketch_order.save()

        apply_workflow(sketch_order, "Customer Approval")
        apply_workflow(sketch_order, "Approve")
        self.assertEqual(
            len(sketch_order.rough_sketch_approval),
            len(sketch_order.final_sketch_approval),
        )
        for row in sketch_order.final_sketch_approval_cmo:
            row.sketch_image = "https://www.chidambaramcovering.in/image/cache/catalog/Mogappu%20Chain/mchn510-gold-plated-jewellery-mugappu-design-without-stone-5-425x500.jpg.webp"
            row.sub_category = "Casual Mugappu"
            row.setting_type = "Open"
            row.gold_wt_approx = 10
            row.diamond_wt_approx = 10
        sketch_order.save()

        apply_workflow(sketch_order, "Update")
        self.assertEqual(qty, len(sketch_order.final_sketch_approval_cmo))
        apply_workflow(sketch_order, "Update")
        sketch_order.reload()
        for row in sketch_order.final_sketch_approval_cmo:
            self.assertEqual(
                sketch_order.name,
                frappe.get_value("Item", row.item, "custom_sketch_order_id"),
            )
        apply_workflow(sketch_order, "Update")
        apply_workflow(sketch_order, "Send to QC")
        apply_workflow(sketch_order, "Approve")

    def tearDown(self):
        frappe.db.rollback()
