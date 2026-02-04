# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.utils import get_link_to_form


class SketchOrder(Document):
    def validate(self):
        populate_child_table(self)
        rows_remove = []
        for r in self.final_sketch_hold:
            if r.is_approved:
                self.append(
                    "final_sketch_approval_cmo",
                    {
                        "designer": r.designer,
                        "sketch_image": r.sketch_image,
                        "designer_name": r.designer_name,
                        "qc_person": r.qc_person,
                        "diamond_wt_approx": r.diamond_wt_approx,
                        "setting_type": r.setting_type,
                        "sub_category": r.sub_category,
                        "category": r.category,
                        "image_rough": r.image_rough,
                        "final_image": r.final_image,
                    },
                )
                rows_remove.append(r)
                for r in rows_remove:
                    self.final_sketch_hold.remove(r)

                for s in self.final_sketch_approval:
                    s.approved = len(self.final_sketch_approval_cmo)
                    s.hold = len(self.final_sketch_hold)
                frappe.msgprint("Hold Image is approved")
        rows_to_remove = []
        for r in self.final_sketch_rejected:
            if r.is_approved:
                self.append(
                    "final_sketch_approval_cmo",
                    {
                        "designer": r.designer,
                        "sketch_image": r.sketch_image,
                        "designer_name": r.designer_name,
                        "qc_person": r.qc_person,
                        "diamond_wt_approx": r.diamond_wt_approx,
                        "setting_type": r.setting_type,
                        "sub_category": r.sub_category,
                        "category": r.category,
                        "image_rough": r.image_rough,
                        "final_image": r.final_image,
                    },
                )
                rows_to_remove.append(r)
                for r in rows_to_remove:
                    self.final_sketch_rejected.remove(r)

                for s in self.final_sketch_approval:
                    s.approved = len(self.final_sketch_approval_cmo)
                    s.reject = len(self.final_sketch_rejected)
                frappe.msgprint("Rejected image is approved ")

    def on_submit(self):
        self.make_items()

    def make_items(self):
        if self.order_type != "Purchase":
            for row in self.final_sketch_approval_cmo:
                item_template = create_item_template_from_sketch_order(self, row.name)
                update_item_template(self, item_template)
                frappe.db.set_value(row.doctype, row.name, "item", item_template)
                frappe.msgprint(
                    _("New Item Created: {0}").format(
                        get_link_to_form("Item", item_template)
                    )
                )
        if self.order_type == "Purchase":
            item_template = create_item_for_po(self, self.name)
            update_item_template(self, item_template)
            frappe.db.set_value("Sketch Order", self.name, "item_code", item_template)
            frappe.msgprint(
                _("New Item Created: {0}").format(
                    get_link_to_form("Item", item_template)
                )
            )


def update_item_template(self, item_template):
    frappe.db.set_value(
        "Item", item_template, {"is_design_code": 0, "item_code": item_template}
    )


def populate_child_table(self):
    if self.workflow_state == "Assigned":
        self.rough_sketch_approval = []
        self.final_sketch_approval = []
        self.final_sketch_approval_cmo = []
        rough_sketch_approval = []
        final_sketch_approval = []
        final_sketch_approval_cmo = []
        for designer in self.designer_assignment:
            r_s_row = self.get(
                "rough_sketch_approvalz",
                {
                    "designer": designer.designer,
                    "designer_name": designer.designer_name,
                },
            )
            if not r_s_row:
                rough_sketch_approval.append(
                    {
                        "designer": designer.designer,
                        "designer_name": designer.designer_name,
                    },
                )
            final_sketch_approval.append(
                {
                    "designer": designer.designer,
                    "designer_name": designer.designer_name,
                },
            )
        for row in rough_sketch_approval:
            self.append("rough_sketch_approval", row)
        for row in final_sketch_approval:
            self.append("final_sketch_approval", row)
        for row in final_sketch_approval_cmo:
            self.append("final_sketch_approval_cmo", row)
    if self.workflow_state == "Requires Update":
        total_approved = 0
        designer_with_approved_qty = []
        final_sketch_approval_cmo = []

        for i in self.final_sketch_approval:
            total_approved += i.approved
            designer_with_approved_qty.append(
                {"designer": i.designer, "qty": i.approved},
            )

        designer = []
        for j in designer_with_approved_qty:
            if j["designer"] in designer:
                continue
            for k in range(j["qty"]):
                count = check_count(self, j["designer"])
                if count == j["qty"]:
                    continue
                self.append(
                    "final_sketch_approval_cmo",
                    {
                        "designer": j["designer"],
                        "designer_name": frappe.db.get_value(
                            "Employee", j["designer"], "employee_name"
                        ),
                        "category": self.category,
                    },
                )
            designer.append(j["designer"])


def check_count(self, designer):
    count = 0
    if self.final_sketch_approval_cmo:
        for i in self.final_sketch_approval_cmo:
            if designer == i.designer:
                count += 1

    return count


def create_item_template_from_sketch_order(self, source_name, target_doc=None):
    def post_process(source, target):
        sub_category, designer = frappe.db.get_value(
            "Final Sketch Approval CMO", source_name, ["sub_category", "designer"]
        )

        target.update(
            {
                "is_design_code": 1,
                "has_variants": 1,
                "india": self.india,
                "india_states": self.india_states,
                "usa": self.usa,
                "usa_states": self.usa_states,
                "custom_sketch_order_id": self.name,
                "custom_sketch_order_form_id": self.sketch_order_form,
                "item_group": f"{sub_category} - T",
                "designer": designer,
                "subcategory": sub_category,
                "item_subcategory": sub_category,
            }
        )

    doc = get_mapped_doc(
        "Final Sketch Approval CMO",
        source_name,
        {
            "Final Sketch Approval CMO": {
                "doctype": "Item",
                "field_map": {
                    "category": "item_category",
                    "sub_category": "item_subcategory",
                },
            }
        },
        target_doc,
        post_process,
    )
    doc.save()
    return doc.name


def create_item_for_po(self, source_name, target_doc=None):
    def post_process(source, target):
        target.update(
            {
                "is_design_code": 1,
                "has_variants": 1,
                "india": self.india,
                "india_states": self.india_states,
                "usa": self.usa,
                "usa_states": self.usa_states,
                "designer": frappe.db.get_value(
                    "Employee", {"user_id": frappe.session.user}, "name"
                )
                or frappe.session.user,
                "custom_sketch_order_id": self.name,
                "custom_sketch_order_form_id": self.sketch_order_form,
                "item_group": f"{self.subcategory} - T",
                "item_category": self.category,
                "item_subcategory": self.subcategory,
            }
        )

    doc = get_mapped_doc(
        "Sketch Order",
        self.name,
        {
            "Sketch Order": {
                "doctype": "Item",
                "field_map": {
                    "category": "item_category",
                },
            }
        },
        target_doc,
        post_process,
    )
    doc.save()
    return doc.name
