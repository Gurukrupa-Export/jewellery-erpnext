"""Add ``Stock Entry.custom_department_process`` on sites that already ran
``add_process_control_fields`` (Day 1). Same idempotent installer, re-run."""

from jewellery_erpnext.patches.add_process_control_fields import execute as _install


def execute():
	_install()
