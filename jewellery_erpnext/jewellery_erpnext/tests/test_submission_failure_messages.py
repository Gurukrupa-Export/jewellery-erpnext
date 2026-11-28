"""Unit tests for the Submission Queue failure messages (pure: no database access).

The fixtures are stored tracebacks in the two formats frappe writes to
`Submission Queue.exception`: standard (production, chained) and traceback-with-variables
(developer_mode sites). Frames, files and line numbers follow the real 16 Nov paths.
"""

from frappe.core.doctype.submission_queue.submission_queue import format_tb
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.submission_queue.submission_queue import (
	FAILURE_DEADLOCK,
	FAILURE_LOCK_WAIT,
	FAILURE_PERMISSION,
	classify_submission_failure,
	describe_submission_failure,
)

_DRIVER_1205 = """\
Traceback (most recent call last):
  File "apps/frappe/frappe/database/database.py", line 272, in sql
    self.execute_query(query, values)
    ~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^
  File "apps/frappe/frappe/database/database.py", line 372, in execute_query
    return self._cursor.execute(query, values)
           ~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^
  File "env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 179, in execute
    res = self._query(mogrified_query)
  File "env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 330, in _query
    db.query(q)
    ~~~~~~~~^^^
  File "env/lib/python3.14/site-packages/MySQLdb/connections.py", line 280, in query
    _mysql.connection.query(self, query)
    ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^
MySQLdb.OperationalError: (1205, 'Lock wait timeout exceeded; try restarting transaction')

The above exception was the direct cause of the following exception:

"""

_EIR_SUBMIT_FRAMES = """\
Traceback (most recent call last):
  File "apps/frappe/frappe/core/doctype/submission_queue/submission_queue.py", line 95, in background_submission
    getattr(to_be_queued_doc, _action)()
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^
  File "apps/frappe/frappe/model/document.py", line 1352, in submit
    return self._submit()
           ~~~~~~~~~~~~^^
  File "apps/frappe/frappe/model/document.py", line 1333, in _submit
    return self.save()
  File "apps/frappe/frappe/model/document.py", line 558, in save
    return self._save(*args, **kwargs)
  File "apps/frappe/frappe/model/document.py", line 613, in _save
    self.run_post_save_methods()
  File "apps/frappe/frappe/model/document.py", line 1460, in run_post_save_methods
    self.run_method("on_submit")
  File "apps/frappe/frappe/model/document.py", line 1262, in run_method
    out = Document.hook(fn)(self, *args, **kwargs)
  File "apps/frappe/frappe/model/document.py", line 1638, in runner
    add_to_return_value(self, fn(self, *args, **kwargs))
                              ~~^^^^^^^^^^^^^^^^^^^^^^^
  File "apps/frappe/frappe/model/document.py", line 1259, in fn
    return method_object(*args, **kwargs)
  File "apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/doctype/employee_ir/employee_ir.py", line 194, in on_submit
    self.on_submit_receive()
    ~~~~~~~~~~~~~~~~~~~~~~^^
"""

#: 16 Nov, Employee IR receive waiting on the Stock Entry counter pre-lock
#: (lock_order.py:516 -> :305), standard chained format.
LOCK_WAIT_SERIES = (
	_DRIVER_1205
	+ _EIR_SUBMIT_FRAMES
	+ """\
  File "apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/doctype/employee_ir/employee_ir.py", line 586, in on_submit_receive
    preallocate_series_for_docs(
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~^
        *series_stubs(
        ^^^^^^^^^^^^^^
    ...<4 lines>...
        )
        ^
    )
    ^
  File "apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/lock_order.py", line 516, in preallocate_series_for_docs
    preallocate_series(series_prefixes)
    ~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/lock_order.py", line 305, in preallocate_series
    frappe.db.sql(
    ~~~~~~~~~~~~~^
        "SELECT `current` FROM `tabSeries` WHERE name = %s FOR UPDATE",
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
        (prefix,),
        ^^^^^^^^^^
    )
    ^
  File "apps/frappe/frappe/database/database.py", line 281, in sql
    raise frappe.QueryTimeoutError(e) from e
frappe.exceptions.QueryTimeoutError: (1205, 'Lock wait timeout exceeded; try restarting transaction')
"""
)

#: 16 Nov, Employee IR issue waiting on a child-row INSERT of its own submit: a lock wait
#: that is not on a naming counter.
LOCK_WAIT_CHILD_INSERT = (
	_DRIVER_1205
	+ """\
Traceback (most recent call last):
  File "apps/frappe/frappe/core/doctype/submission_queue/submission_queue.py", line 95, in background_submission
    getattr(to_be_queued_doc, _action)()
  File "apps/frappe/frappe/model/document.py", line 1352, in submit
    return self._submit()
  File "apps/frappe/frappe/model/document.py", line 1333, in _submit
    return self.save()
  File "apps/frappe/frappe/model/document.py", line 558, in save
    return self._save(*args, **kwargs)
  File "apps/frappe/frappe/model/document.py", line 611, in _save
    self.update_children()
  File "apps/frappe/frappe/model/document.py", line 656, in update_children
    self.update_child_table(df.fieldname, df)
  File "apps/frappe/frappe/model/document.py", line 690, in update_child_table
    d.db_update()
  File "apps/frappe/frappe/model/base_document.py", line 806, in db_update
    self.db_insert()
  File "apps/frappe/frappe/model/base_document.py", line 766, in db_insert
    frappe.db.sql(
  File "apps/frappe/frappe/database/database.py", line 281, in sql
    raise frappe.QueryTimeoutError(e) from e
frappe.exceptions.QueryTimeoutError: (1205, 'Lock wait timeout exceeded; try restarting transaction')
"""
)

#: Refining Stock Entry insert naming itself through frappe's getseries.
LOCK_WAIT_GETSERIES = (
	_DRIVER_1205
	+ """\
Traceback (most recent call last):
  File "apps/frappe/frappe/core/doctype/submission_queue/submission_queue.py", line 95, in background_submission
    getattr(to_be_queued_doc, _action)()
  File "apps/jewellery_erpnext/jewellery_erpnext/refining/doctype/refining_entry/refining_entry.py", line 2711, in create_material_transfer_se
    se.insert()
  File "apps/frappe/frappe/model/document.py", line 470, in insert
    self.set_new_name(set_name=set_name, set_child_names=set_child_names)
  File "apps/frappe/frappe/model/naming.py", line 175, in make_autoname
    return parse_naming_series(key, doctype=doctype, doc=doc)
  File "apps/frappe/frappe/model/naming.py", line 412, in getseries
    current = (frappe.qb.from_(series).where(series.name == key).for_update().select("current")).run()
  File "apps/frappe/frappe/database/database.py", line 281, in sql
    raise frappe.QueryTimeoutError(e) from e
frappe.exceptions.QueryTimeoutError: (1205, 'Lock wait timeout exceeded; try restarting transaction')
"""
)

#: The same lock wait as LOCK_WAIT_SERIES on a developer_mode site: only the outermost
#: exception, every frame followed by its locals. The locals mention 1205 and
#: QueryTimeoutError too; they are indented and must not matter.
LOCK_WAIT_SERIES_DEV_MODE = """\
Traceback with variables (most recent call last):
  File "apps/frappe/frappe/core/doctype/submission_queue/submission_queue.py", line 95, in background_submission
    getattr(to_be_queued_doc, _action)()
      self = <CustomSubmissionQueue: 4f1c2a9e0b>
      to_be_queued_doc = <EmployeeIR: EMP-IR-Labh-2026-61931>
      action_for_queuing = 'Submit'
      _action = 'submit'
  File "apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/lock_order.py", line 516, in preallocate_series_for_docs
    preallocate_series(series_prefixes)
      docs = (<StockEntry: new-stock-entry-1>,)
      series_prefixes = ['MAT-STE-']
  File "apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/lock_order.py", line 305, in preallocate_series
    frappe.db.sql(
      prefixes = ['MAT-STE-']
      prefix = 'MAT-STE-'
  File "apps/frappe/frappe/database/database.py", line 281, in sql
    raise frappe.QueryTimeoutError(e) from e
      self = <frappe.database.mariadb.database.MariaDBDatabase object at 0x7f>
      query = 'SELECT `current` FROM `tabSeries` WHERE name = %s FOR UPDATE'
      e = OperationalError(1205, 'Lock wait timeout exceeded; try restarting transaction')
frappe.exceptions.QueryTimeoutError: (1205, 'Lock wait timeout exceeded; try restarting transaction')
"""

#: A Stock Entry submit chosen as the deadlock victim while updating a Bin.
DEADLOCK = """\
Traceback (most recent call last):
  File "apps/frappe/frappe/database/database.py", line 272, in sql
    self.execute_query(query, values)
  File "env/lib/python3.14/site-packages/MySQLdb/connections.py", line 280, in query
    _mysql.connection.query(self, query)
MySQLdb.OperationalError: (1213, 'Deadlock found when trying to get lock; try restarting transaction')

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "apps/frappe/frappe/core/doctype/submission_queue/submission_queue.py", line 95, in background_submission
    getattr(to_be_queued_doc, _action)()
  File "apps/erpnext/erpnext/stock/doctype/stock_entry/stock_entry.py", line 637, in on_submit
    self.update_stock_ledger()
  File "apps/erpnext/erpnext/stock/stock_ledger.py", line 2171, in update_bin_qty
    frappe.db.set_value("Bin", bin_name, values_to_update)
  File "apps/frappe/frappe/database/database.py", line 278, in sql
    raise frappe.QueryDeadlockError(e) from e
frappe.exceptions.QueryDeadlockError: (1213, 'Deadlock found when trying to get lock; try restarting transaction')
"""

#: 16 Nov / 31 Oct: external Refining Entry; the refining Purchase Order picks the GST
#: template and ERPNext's require_permission(..., "select") refuses it.
PERMISSION_TAX_TEMPLATE = """\
Traceback (most recent call last):
  File "apps/frappe/frappe/core/doctype/submission_queue/submission_queue.py", line 95, in background_submission
    getattr(to_be_queued_doc, _action)()
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^
  File "apps/frappe/frappe/model/document.py", line 1352, in submit
    return self._submit()
  File "apps/frappe/frappe/model/document.py", line 1333, in _submit
    return self.save()
  File "apps/frappe/frappe/model/document.py", line 558, in save
    return self._save(*args, **kwargs)
  File "apps/frappe/frappe/model/document.py", line 595, in _save
    self.run_before_save_methods()
  File "apps/frappe/frappe/model/document.py", line 1418, in run_before_save_methods
    self.run_method("before_submit")
  File "apps/frappe/frappe/model/document.py", line 1259, in fn
    return method_object(*args, **kwargs)
  File "apps/jewellery_erpnext/jewellery_erpnext/refining/doctype/refining_entry/refining_entry.py", line 131, in before_submit
    self.before_submit_external()
  File "apps/jewellery_erpnext/jewellery_erpnext/refining/doctype/refining_entry/refining_entry.py", line 300, in before_submit_external
    self.create_external_refining_po()
  File "apps/jewellery_erpnext/jewellery_erpnext/refining/doctype/refining_entry/refining_entry.py", line 565, in create_external_refining_po
    po.insert(ignore_permissions=True)
    ~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^
  File "apps/frappe/frappe/model/document.py", line 487, in insert
    self.run_before_save_methods()
  File "apps/frappe/frappe/model/document.py", line 1414, in run_before_save_methods
    self.run_method("validate")
  File "apps/frappe/frappe/model/document.py", line 1259, in fn
    return method_object(*args, **kwargs)
  File "apps/erpnext/erpnext/buying/doctype/purchase_order/purchase_order.py", line 200, in validate
    super().validate()
  File "apps/erpnext/erpnext/controllers/buying_controller.py", line 43, in validate
    super().validate()
  File "apps/erpnext/erpnext/controllers/stock_controller.py", line 109, in validate
    super().validate()
  File "apps/erpnext/erpnext/controllers/accounts_controller.py", line 310, in validate
    self.set_missing_values(for_validate=True)
  File "apps/erpnext/erpnext/controllers/buying_controller.py", line 253, in set_missing_values
    _get_party_details(
    ~~~~~~~~~~~~~~~~~~^
        self.supplier,
        ^^^^^^^^^^^^^^
        party_type="Supplier",
        ^^^^^^^^^^^^^^^^^^^^^^
    ...<7 lines>...
    )
    ^
  File "apps/erpnext/erpnext/accounts/party.py", line 148, in _get_party_details
    party_address, shipping_address = set_address_details(
  File "apps/erpnext/erpnext/accounts/party.py", line 304, in set_address_details
    get_regional_address_details(party_details, doctype, company)
  File "apps/erpnext/erpnext/__init__.py", line 150, in caller
    return fn(*args, **kwargs)
  File "apps/india_compliance/india_compliance/gst_india/overrides/transaction.py", line 794, in update_party_details
    party_details.update(get_gst_details(party_details, doctype, company, update_place_of_supply=True))
  File "apps/india_compliance/india_compliance/gst_india/overrides/transaction.py", line 942, in get_gst_details
    gst_details.taxes = get_taxes_and_charges(master_doctype, default_tax)
                        ~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "apps/erpnext/erpnext/controllers/accounts_controller.py", line 3648, in get_taxes_and_charges
    require_permission(master_doctype, master_name, "select")
    ~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "apps/erpnext/erpnext/__init__.py", line 232, in require_permission
    _refuse()
    ~~~~~~~^^
  File "apps/erpnext/erpnext/__init__.py", line 258, in _refuse
    frappe.throw_permission_error()
  File "apps/frappe/frappe/utils/messages.py", line 173, in throw_permission_error
    throw(_("Not permitted"), frappe.PermissionError)
  File "apps/frappe/frappe/utils/messages.py", line 159, in throw
    msgprint(
  File "apps/frappe/frappe/utils/messages.py", line 118, in msgprint
    _raise_exception()
  File "apps/frappe/frappe/utils/messages.py", line 59, in _raise_exception
    raise exc
frappe.exceptions.PermissionError: Not permitted
"""

#: The same refusal on a developer_mode site: require_permission's own locals name it.
PERMISSION_DEV_MODE = """\
Traceback with variables (most recent call last):
  File "apps/frappe/frappe/core/doctype/submission_queue/submission_queue.py", line 95, in background_submission
    getattr(to_be_queued_doc, _action)()
      action_for_queuing = 'Submit'
  File "apps/erpnext/erpnext/controllers/accounts_controller.py", line 3648, in get_taxes_and_charges
    require_permission(master_doctype, master_name, "select")
      master_doctype = 'Purchase Taxes and Charges Template'
      master_name = 'Input GST In-state - KG'
  File "apps/erpnext/erpnext/__init__.py", line 232, in require_permission
    _refuse()
      doctype = 'Purchase Taxes and Charges Template'
      name = 'Input GST In-state - KG'
      ptype = 'select'
  File "apps/frappe/frappe/utils/messages.py", line 59, in _raise_exception
    raise exc
      exc = PermissionError('Not permitted')
frappe.exceptions.PermissionError: Not permitted
"""

#: frappe's own check with literal arguments: the exception line has no message.
PERMISSION_HAS_PERMISSION = """\
Traceback (most recent call last):
  File "apps/frappe/frappe/core/doctype/submission_queue/submission_queue.py", line 95, in background_submission
    getattr(to_be_queued_doc, _action)()
  File "apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/doctype/main_slip/main_slip.py", line 120, in on_submit
    if not frappe.has_permission("Stock Entry", "submit", throw=True):
           ~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "apps/frappe/frappe/__init__.py", line 645, in has_permission
    raise frappe.PermissionError
frappe.exceptions.PermissionError
"""

#: A specific permission message (e.g. the C-03 preflight) is passed through, as text.
PERMISSION_SPECIFIC_MESSAGE = """\
Traceback (most recent call last):
  File "apps/jewellery_erpnext/jewellery_erpnext/refining/doctype/refining_entry/refining_entry.py", line 230, in before_submit_external
    frappe.throw(msg, frappe.PermissionError)
  File "apps/frappe/frappe/utils/messages.py", line 59, in _raise_exception
    raise exc
frappe.exceptions.PermissionError: You need the <strong>Select</strong> permission on <strong>Purchase Taxes and Charges Template</strong> Input GST In-state - KG & more
"""

#: E-P14: the receive-delay rule refusing a receive, on a developer_mode site whose locals
#: mention 1205 and QueryTimeoutError (indented, so they must not matter).
VALIDATION_RECEIVE_DELAY = """\
Traceback with variables (most recent call last):
  File "apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/doctype/employee_ir/employee_ir.py", line 166, in before_submit
    validate_employee_ir_receive_delay(self)
      self = <EmployeeIR: EMP-IR-Labh-2026-61985>
      last_error = QueryTimeoutError(1205, 'Lock wait timeout exceeded; try restarting transaction')
  File "apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/doctype/employee_ir/doc_events/validation_utils.py", line 461, in validate_employee_ir_receive_delay
    frappe.throw(
      worst_wait = 3
frappe.exceptions.ValidationError: This Employee IR Receive entry cannot be submitted at this stage. Please try again later.
"""

#: close_open turns a NOWAIT lock error into its own ValidationError. The 1205 is only the
#: context; the user must keep seeing that specific message.
VALIDATION_RAISED_WHILE_HANDLING_LOCK_WAIT = (
	_DRIVER_1205
	+ """\
Traceback (most recent call last):
  File "apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/doctype/parent_manufacturing_order/doc_events/close_open.py", line 686, in _lock_close_open_rows
    locked_mops.update(lock_manufacturing_operations(moved, wait=0))
  File "apps/frappe/frappe/database/database.py", line 281, in sql
    raise frappe.QueryTimeoutError(e) from e
frappe.exceptions.QueryTimeoutError: (1205, 'Lock wait timeout exceeded; try restarting transaction')

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/doctype/parent_manufacturing_order/doc_events/close_open.py", line 695, in _lock_close_open_rows
    frappe.throw(
  File "apps/frappe/frappe/utils/messages.py", line 59, in _raise_exception
    raise exc
frappe.exceptions.ValidationError: <strong>PMO-KG-00042</strong> is being updated by another user right now. Try again in a moment.
"""
)

_RECONNECT_2014 = """
During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "apps/jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/customization/submission_queue/submission_queue.py", line 412, in background_submission
    super().background_submission(to_be_queued_doc, action_for_queuing)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "apps/frappe/frappe/core/doctype/submission_queue/submission_queue.py", line 106, in background_submission
    frappe.db.rollback()
    ~~~~~~~~~~~~~~~~~~^^
  File "apps/frappe/frappe/database/database.py", line 1211, in rollback
    self.sql("rollback")
  File "apps/frappe/frappe/database/database.py", line 272, in sql
    self.execute_query(query, values)
  File "env/lib/python3.14/site-packages/MySQLdb/connections.py", line 280, in query
    _mysql.connection.query(self, query)
MySQLdb.ProgrammingError: (2014, "Commands out of sync; you can't run this command now")
"""

#: The reconnect path: frappe's rollback after the lock wait raised 2014, so the JE
#: override reconnected and stored this chain.
RECONNECT_AFTER_LOCK_WAIT = LOCK_WAIT_SERIES + _RECONNECT_2014

#: The usual reconnect path: the 4500 s RQ timeout landed mid-query.
RECONNECT_AFTER_JOB_TIMEOUT = (
	"""\
Traceback (most recent call last):
  File "apps/frappe/frappe/core/doctype/submission_queue/submission_queue.py", line 95, in background_submission
    getattr(to_be_queued_doc, _action)()
  File "env/lib/python3.14/site-packages/rq/timeouts.py", line 63, in handle_death_penalty
    raise self._exception('Task exceeded maximum timeout value ({0} seconds)'.format(self._timeout))
rq.timeouts.JobTimeoutException: Task exceeded maximum timeout value (4500 seconds)
"""
	+ _RECONNECT_2014
)

#: The reconnect path on a developer_mode site: only the outermost exception is kept.
RECONNECT_DEV_MODE = """\
Traceback with variables (most recent call last):
  File "apps/frappe/frappe/database/database.py", line 1211, in rollback
    self.sql("rollback")
      self = <frappe.database.mariadb.database.MariaDBDatabase object at 0x7f>
MySQLdb.ProgrammingError: (2014, "Commands out of sync; you can't run this command now")
"""

SERIES_BUSY_SUBMIT = (
	"Not saved: Stock Entry numbering was busy with another posting. "
	"Nothing was changed. Please submit it again in a few minutes."
)
TAX_TEMPLATE_SUBMIT = (
	"Not saved: you do not have Select permission on Purchase Taxes and Charges "
	"Template. Nothing was changed. Ask your System Manager to give you this "
	"permission, then submit it again."
)


class TestClassifySubmissionFailure(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_lock_wait_and_deadlock_tracebacks(self):
		for text in (
			LOCK_WAIT_SERIES,
			LOCK_WAIT_CHILD_INSERT,
			LOCK_WAIT_GETSERIES,
			LOCK_WAIT_SERIES_DEV_MODE,
		):
			self.assertEqual(classify_submission_failure(text), FAILURE_LOCK_WAIT)
		self.assertEqual(classify_submission_failure(DEADLOCK), FAILURE_DEADLOCK)

	def test_permission_tracebacks(self):
		for text in (
			PERMISSION_TAX_TEMPLATE,
			PERMISSION_DEV_MODE,
			PERMISSION_HAS_PERMISSION,
			PERMISSION_SPECIFIC_MESSAGE,
		):
			self.assertEqual(classify_submission_failure(text), FAILURE_PERMISSION)

	def test_the_reconnect_path_reports_the_failure_it_interrupted(self):
		# The 2014 is frappe's rollback failing on the broken connection, not the cause.
		self.assertEqual(
			classify_submission_failure(RECONNECT_AFTER_LOCK_WAIT), FAILURE_LOCK_WAIT
		)

	def test_other_failures_are_not_classified(self):
		for text in (
			VALIDATION_RECEIVE_DELAY,
			VALIDATION_RAISED_WHILE_HANDLING_LOCK_WAIT,
			RECONNECT_AFTER_JOB_TIMEOUT,
			RECONNECT_DEV_MODE,  # developer_mode: only the broken connection was stored
			"",
			None,
			"frappe.exceptions.QueryTimeoutError: (1205, 'no traceback header')",
		):
			self.assertIsNone(classify_submission_failure(text))
			self.assertIsNone(describe_submission_failure(text, "Submit", undone=True))

	def test_a_builtin_permission_error_is_not_a_frappe_permission_error(self):
		text = (
			"Traceback (most recent call last):\n"
			'  File "apps/frappe/frappe/utils/file_manager.py", line 10, in write_file\n'
			"    with open(path, 'wb') as f:\n"
			"PermissionError: [Errno 13] Permission denied: '/home/frappe/x'\n"
		)
		self.assertIsNone(classify_submission_failure(text))

	def test_the_appended_message_does_not_change_the_classification(self):
		stored = f"{LOCK_WAIT_SERIES}\n{SERIES_BUSY_SUBMIT}"
		self.assertEqual(classify_submission_failure(stored), FAILURE_LOCK_WAIT)
		self.assertEqual(
			describe_submission_failure(stored, "Submit", undone=True),
			SERIES_BUSY_SUBMIT,
		)


class TestDescribeSubmissionFailure(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_numbering_busy_after_a_confirmed_rollback(self):
		self.assertEqual(
			describe_submission_failure(LOCK_WAIT_SERIES, "Submit", undone=True),
			SERIES_BUSY_SUBMIT,
		)
		# developer_mode stores a different format; the message is the same.
		self.assertEqual(
			describe_submission_failure(
				LOCK_WAIT_SERIES_DEV_MODE, "Submit", undone=True
			),
			SERIES_BUSY_SUBMIT,
		)

	def test_the_lock_wait_names_what_was_busy(self):
		self.assertEqual(
			describe_submission_failure(LOCK_WAIT_GETSERIES, "Submit", undone=True),
			"Not saved: document numbering was busy with another posting. "
			"Nothing was changed. Please submit it again in a few minutes.",
		)
		self.assertEqual(
			describe_submission_failure(LOCK_WAIT_CHILD_INSERT, "Submit", undone=True),
			"Not saved: another posting was holding records this document needs. "
			"Nothing was changed. Please submit it again in a few minutes.",
		)

	def test_deadlock(self):
		self.assertEqual(
			describe_submission_failure(DEADLOCK, "Submit", undone=True),
			"Not saved: this posting clashed with another posting running at the same "
			"time. Nothing was changed. Please submit it again in a few minutes.",
		)

	def test_permission_names_the_doctype_and_the_permission_type(self):
		self.assertEqual(
			describe_submission_failure(PERMISSION_TAX_TEMPLATE, "Submit", undone=True),
			TAX_TEMPLATE_SUBMIT,
		)
		self.assertEqual(
			describe_submission_failure(PERMISSION_DEV_MODE, "Submit", undone=True),
			TAX_TEMPLATE_SUBMIT,
		)
		self.assertEqual(
			describe_submission_failure(
				PERMISSION_HAS_PERMISSION, "Submit", undone=True
			),
			"Not saved: you do not have Submit permission on Stock Entry. Nothing was "
			"changed. Ask your System Manager to give you this permission, then submit "
			"it again.",
		)

	def test_a_specific_permission_message_is_passed_through_as_plain_text(self):
		self.assertEqual(
			describe_submission_failure(
				PERMISSION_SPECIFIC_MESSAGE, "Submit", undone=True
			),
			"Not saved: You need the Select permission on Purchase Taxes and Charges "
			"Template Input GST In-state - KG more. Nothing was changed. Ask your System "
			"Manager to give you this permission, then submit it again.",
		)

	def test_unconfirmed_rollback_never_claims_nothing_was_changed(self):
		self.assertEqual(
			describe_submission_failure(
				RECONNECT_AFTER_LOCK_WAIT, "Submit", undone=False
			),
			"Submit failed: Stock Entry numbering was busy with another posting. "
			"Reload the document and check its status before trying again.",
		)

	def test_cancel_wording(self):
		self.assertEqual(
			describe_submission_failure(LOCK_WAIT_SERIES, "Cancel", undone=True),
			"Not cancelled: Stock Entry numbering was busy with another posting. "
			"Nothing was changed. Please cancel it again in a few minutes.",
		)
		self.assertEqual(
			describe_submission_failure(
				PERMISSION_TAX_TEMPLATE, "cancel", undone=False
			),
			"Cancel failed: you do not have Select permission on Purchase Taxes and "
			"Charges Template. Reload the document and check its status before trying "
			"again.",
		)

	def test_every_message_is_one_html_safe_line(self):
		for text in (
			LOCK_WAIT_SERIES,
			LOCK_WAIT_CHILD_INSERT,
			LOCK_WAIT_GETSERIES,
			DEADLOCK,
			PERMISSION_TAX_TEMPLATE,
			PERMISSION_HAS_PERMISSION,
			PERMISSION_SPECIFIC_MESSAGE,
			RECONNECT_AFTER_LOCK_WAIT,
		):
			for action in ("Submit", "submit", "Cancel", "Update"):
				for undone in (True, False):
					with self.subTest(action=action, undone=undone):
						message = describe_submission_failure(text, action, undone)
						self.assertTrue(message)
						self.assertNotIn("\n", message)
						for unsafe in "<>&":
							self.assertNotIn(unsafe, message)
						self.assertEqual("Nothing was changed" in message, undone)
						# format_tb is what frappe's form banner shows: the last line.
						self.assertEqual(format_tb(f"{text}\n{message}"), message)
