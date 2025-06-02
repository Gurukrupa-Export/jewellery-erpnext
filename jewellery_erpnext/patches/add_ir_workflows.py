import frappe

def execute():
	for doctype in ["Department IR"]:
		if not frappe.db.exists("Workflow", f"{doctype} Workflow"):
			create_ir_workflow(doctype)


def create_ir_workflow(doctype):
	workflow = frappe.get_doc(
		{
			"doctype": "Workflow",
			"workflow_name": f"{doctype} Workflow",
			"document_type": doctype,
			"is_active": 1,
			"workflow_state_field": "workflow_state",
            "states": [
                make_state("Pending MOP Creation", 0, "Warning"),
                make_state("Queued MOP Creation", 0, "Primary"),
                make_state("MOP Failed", 0, "Danger"),
                make_state("Pending Stock Entry Creation", 0, "Warning"),
                make_state("Queued Stock Entry Creation", 0, "Primary"),
                make_state("Stock Entry Created", 1, "Success"),
                make_state("Stock Entry Failed", 0, "Danger"),
                make_state("Cancelled", 2, "Dark"),
                make_state("IR Issued", 1, "Success"),
                make_state("IR Received", 1, "Success"),
            ],
			"transitions": [
				make_transition("Pending MOP Creation", "Create MOP", "Queued MOP Creation"),
				make_transition("MOP Failed", "Retry MOP Creation", "Queued MOP Creation"),
				make_transition("Pending Stock Entry Creation", "Create Stock Entry", "Queued Stock Entry Creation"),
				make_transition("Queued Stock Entry Creation", "Cancel Stock Entry Queue", "Stock Entry Failed"),
				make_transition("Stock Entry Failed", "Retry Stock Entry Creation", "Queued Stock Entry Creation"),
				make_transition("Stock Entry Created", "Cancel", "Cancelled"),
			],
		}
	)
	workflow.insert(ignore_permissions=True)
	frappe.msgprint(f"{doctype} Workflow created.")


def make_state(name, doc_status=0, style=""):
	return {
		"state": name,
		"doc_status": doc_status,
		"allow_edit": "All",
		"style": style,
	}


def make_transition(state, action, next_state):
	return {
		"state": state,
		"action": action,
		"next_state": next_state,
		"allowed": "All",
		"allow_self_approval": 1,
	}
