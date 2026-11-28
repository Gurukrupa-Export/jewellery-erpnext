import ast
import contextlib
import html
import re
from typing import NamedTuple
from urllib.parse import quote

import frappe
from frappe import _
from frappe.core.doctype.submission_queue.submission_queue import SubmissionQueue
from frappe.desk.doctype.notification_log.notification_log import (
	enqueue_create_notification,
)
from frappe.model.document import Document
from frappe.utils import cint, escape_html, now, strip_html, time_diff_in_seconds

# Submissions that outrun frappe's 600 s Submission Queue default: run on the long queue
# with a 4500 s budget, one job per document.
LONG_SUBMISSION_DOCTYPES = (
	"Employee IR",
	"Department IR",
	"Product Certification",
	"Stock Entry",
	"Main Slip",
	# Scrap refining transfers run 7k-12k batch lines (10-40 min); at 600 s
	# RFN-SCP-26-00022 was killed mid-submit on every attempt.
	"Refining Entry",
)

# Submissions checked against the work order's current operation before they are queued.
CURRENT_OPERATION_PREFLIGHT_DOCTYPES = ("Employee IR", "Department IR")

# Failure messages
# ----------------
# frappe tells the user only "Action Submit failed on <doc>. View it here", and the form
# banner shows the last traceback line. On 16 Nov users re-submitted Employee IRs up to
# six times after that message while another posting held the Stock Entry numbering row.
# The three failures below get one plain line instead, which says what happened, whether
# anything was changed and what to do next. Every other failure keeps frappe's message
# and its stored traceback untouched.
#
# The failure is read from the traceback frappe stored in `exception`, after frappe's
# rollback. Two formats are stored:
# - standard (production): every exception of the chain, each after its own
#   "Traceback (most recent call last):";
# - traceback-with-variables (developer_mode sites, including local benches): only the
#   outermost exception, with each frame's locals. A lock wait or permission error is still
#   recognised there, but on the reconnect path the outermost exception is the broken
#   connection, so that failure keeps frappe's generic message on developer_mode sites.

FAILURE_LOCK_WAIT = "lock wait"
FAILURE_DEADLOCK = "deadlock"
FAILURE_PERMISSION = "permission"

_TRACEBACK_STARTS = (
	"Traceback (most recent call last):",
	"Traceback with variables (most recent call last):",
)
_FRAME_LINE = re.compile(r'^\s+File "(?P<path>[^"]*)", line \d+, in (?P<func>.+?)\s*$')
_EXCEPTION_LINE = re.compile(r"^(?P<cls>[A-Za-z_][\w.]*)(?::\s?(?P<message>.*))?$")
_CARET_LINE = re.compile(r"^[\s~^]+$")
_ERRNO = re.compile(r"^\((?P<errno>\d+),")
_STRING_LOCAL = re.compile(r"^(?P<name>\w+) = (?P<value>'[^']*'|\"[^\"]*\")$")
_PERMISSION_CALL = re.compile(
	r"(?<![\w.])(?P<qualifier>(?:\w+\.)*)(?P<name>require_permission|has_permission)\s*\("
)

#: Errors the connection itself raises (gone away, lost, out of sync) while frappe is
#: already handling the real failure, e.g. its rollback after an RQ timeout mid-query.
_CONNECTION_ERRNOS = (2006, 2013, 2014)
_DB_ERROR_CLASSES = (
	"OperationalError",
	"InternalError",
	"ProgrammingError",
	"QueryTimeoutError",
	"QueryDeadlockError",
)
_FRAPPE_PERMISSION_ERRORS = (
	"frappe.exceptions.PermissionError",
	"frappe.PermissionError",
)


class _Frame(NamedTuple):
	path: str
	func: str
	lines: list  # source, caret and (developer_mode) locals lines, stripped


class _Raised(NamedTuple):
	cls: str
	message: str
	frames: list


def _raised_exceptions(traceback_text):
	"""The exceptions printed in a stored traceback, innermost cause first.

	Frames, source, carets and locals are indented; an exception line is the first line
	at column 0 after its frames. Lines after it (multi-line messages, chain separators,
	the summary this module appends) are never read as an exception."""
	raised, frames = [], None
	for line in (traceback_text or "").splitlines():
		if line.rstrip() in _TRACEBACK_STARTS:
			frames = []
		elif frames is None or not line.strip():
			continue
		elif line[0].isspace():
			match = _FRAME_LINE.match(line)
			if match:
				frames.append(_Frame(match["path"], match["func"], []))
			elif frames:
				frames[-1].lines.append(line.strip())
		else:
			match = _EXCEPTION_LINE.match(line)
			if match:
				raised.append(
					_Raised(match["cls"], (match["message"] or "").strip(), frames)
				)
			else:
				raised.append(_Raised(line, "", frames))
			frames = None
	return raised


def _errno(exc):
	if not exc.cls.endswith(_DB_ERROR_CLASSES):
		return None
	match = _ERRNO.match(exc.message)
	return int(match["errno"]) if match else None


def _is_connection_error(exc):
	return exc.cls.endswith("InterfaceError") or _errno(exc) in _CONNECTION_ERRNOS


def _deciding_exception(raised):
	"""The exception that ended the attempt. A broken connection raised while that one
	was being handled (the reconnect path) is a consequence and is skipped."""
	while len(raised) > 1 and _is_connection_error(raised[-1]):
		raised = raised[:-1]
	if raised and not _is_connection_error(raised[-1]):
		return raised[-1]
	return None


def _kind(exc):
	if exc is None:
		return None
	errno = _errno(exc)
	if exc.cls.endswith("QueryDeadlockError") or errno in (1213, 1020):
		return FAILURE_DEADLOCK
	if exc.cls.endswith("QueryTimeoutError") or errno == 1205:
		return FAILURE_LOCK_WAIT
	if exc.cls in _FRAPPE_PERMISSION_ERRORS:
		return FAILURE_PERMISSION
	return None


def classify_submission_failure(traceback_text):
	"""FAILURE_LOCK_WAIT, FAILURE_DEADLOCK or FAILURE_PERMISSION for a stored Submission
	Queue traceback, or None for any other failure. Pure: reads only the text.

	Only the exception that ended the attempt counts. A ValidationError raised while
	handling a 1205 (e.g. close_open's "being updated by another user") keeps its own
	message."""
	return _kind(_deciding_exception(_raised_exceptions(traceback_text)))


def _plain(text, limit=300):
	"""One line of plain text that is safe in frappe's banner, which injects the last line
	of `exception` as HTML: tags stripped, entities decoded, then '<', '>' and '&' removed
	and whitespace folded."""
	text = re.sub(r"[<>&]", "", html.unescape(strip_html(text or "")))
	return " ".join(text.split())[:limit]


def _in(frame, path_suffix, func):
	return frame.func == func and frame.path.endswith(path_suffix)


def _lock_wait_cause(frames):
	if any(
		_in(f, "jewellery_erpnext/lock_order.py", "preallocate_series_for_docs")
		for f in frames
	):
		# Every caller pins the counters of the Stock Entries its cascade will mint.
		return _("Stock Entry numbering was busy with another posting.")
	if any(
		_in(f, "jewellery_erpnext/lock_order.py", "preallocate_series")
		or _in(f, "frappe/model/naming.py", "getseries")
		or _in(f, "document_naming_rule/document_naming_rule.py", "apply")
		for f in frames
	):
		return _("document numbering was busy with another posting.")
	return _("another posting was holding records this document needs.")


def _string_locals(frame):
	"""developer_mode only: the frame's string locals, e.g. doctype = 'Item'."""
	values = {}
	for line in frame.lines[1:]:
		match = _STRING_LOCAL.match(line)
		if match:
			with contextlib.suppress(ValueError, SyntaxError):
				values[match["name"]] = ast.literal_eval(match["value"])
	return values


def _parse_call(code, start):
	"""The ast.Call that starts at `start` in `code`, or None."""
	for end in range(start, len(code)):
		if code[end] != ")":
			continue
		try:
			node = ast.parse(code[start : end + 1], mode="eval").body
		except (SyntaxError, ValueError, RecursionError):
			continue
		return node if isinstance(node, ast.Call) else None
	return None


def _argument(call, position, keyword):
	for kw in call.keywords:
		if kw.arg == keyword:
			return kw.value
	if len(call.args) > position and not isinstance(call.args[position], ast.Starred):
		return call.args[position]
	return None


def _text(node):
	if isinstance(node, ast.Constant) and isinstance(node.value, str):
		return node.value
	return None


def _permission_call(frame):
	"""(doctype, ptype) from a require_permission(...) / frappe.has_permission(...) call in
	the frame's source line; a part passed as a variable is None. None if there is no call."""
	code = "\n".join(line for line in frame.lines if not _CARET_LINE.match(line))
	for match in _PERMISSION_CALL.finditer(code):
		name, qualifier = match["name"], match["qualifier"]
		if name == "require_permission" and qualifier in ("", "erpnext."):
			# erpnext.require_permission(doctype, name, ptype="read")
			ptype_position = 2
		elif name == "has_permission" and qualifier == "frappe.":
			# frappe.has_permission(doctype, ptype="read", ...)
			ptype_position = 1
		else:
			continue  # e.g. doc.has_permission(ptype): a method with other arguments
		call = _parse_call(code, match.start())
		if call is None:
			continue
		ptype_node = _argument(call, ptype_position, "ptype")
		ptype = "read" if ptype_node is None else _text(ptype_node)
		return _text(_argument(call, 0, "doctype")), ptype
	return None


def _tax_template_doctype(frames):
	"""ERPNext's get_taxes_and_charges(master_doctype, ...) checks the template of the
	document being built: Purchase Taxes and Charges Template under a BuyingController,
	Sales Taxes and Charges Template under a SellingController (innermost one wins)."""
	for frame in reversed(frames):
		if frame.path.endswith("erpnext/controllers/buying_controller.py"):
			return "Purchase Taxes and Charges Template"
		if frame.path.endswith("erpnext/controllers/selling_controller.py"):
			return "Sales Taxes and Charges Template"
	return None


def _permission_target(frames):
	"""(doctype, ptype) of the permission check that failed; either may be None.

	Production tracebacks have no locals, so this reads the innermost permission call in
	the source lines, e.g. require_permission(master_doctype, master_name, "select")."""
	for index in range(len(frames) - 1, -1, -1):
		frame = frames[index]
		if frame.func in (
			"require_permission",
			"has_permission",
			"raise_no_permission_to",
		):
			values = _string_locals(frame)
			doctype = values.get("doctype")
			ptype = values.get("ptype") or values.get("perm_type")
			if doctype or ptype:
				return doctype, ptype
		found = _permission_call(frame)
		if found:
			doctype, ptype = found
			if not doctype and frame.func == "get_taxes_and_charges":
				doctype = _tax_template_doctype(frames[:index])
			return doctype, ptype
	return None, None


def _permission_cause(exc):
	message = _plain(exc.message)
	if message and message not in ("Not permitted", _plain(_("Not permitted"))):
		# A specific message, e.g. a preflight that already names the doctype and the
		# permission type.
		return message if message.endswith((".", "!", "?")) else f"{message}."
	doctype, ptype = _permission_target(exc.frames)
	doctype = _plain(_(doctype), 140) if doctype else None
	ptype = _plain(_(ptype.replace("_", " ").title()), 40) if ptype else None
	if doctype and ptype:
		return _("you do not have {0} permission on {1}.").format(ptype, doctype)
	if doctype:
		return _("you do not have permission on {0}.").format(doctype)
	if ptype:
		return _("you do not have the {0} permission this posting needs.").format(ptype)
	return _("a permission check failed.")


def describe_submission_failure(traceback_text, action="Submit", undone=False):
	"""The one-line message for a failed queued action, or None to keep frappe's message.

	`undone`: frappe's own rollback of this attempt returned and the document is still in
	its pre-action state. Only then does the message say "Nothing was changed"."""
	exc = _deciding_exception(_raised_exceptions(traceback_text))
	kind = _kind(exc)
	if not kind:
		return None
	cancel = (action or "").lower() == "cancel"
	if kind == FAILURE_PERMISSION:
		cause = _permission_cause(exc)
		if cancel:
			retry = _(
				"Ask your System Manager to give you this permission, then cancel it again."
			)
		else:
			retry = _(
				"Ask your System Manager to give you this permission, then submit it again."
			)
	else:
		if kind == FAILURE_DEADLOCK:
			cause = _(
				"this posting clashed with another posting running at the same time."
			)
		else:
			cause = _lock_wait_cause(exc.frames)
		if cancel:
			retry = _("Please cancel it again in a few minutes.")
		else:
			retry = _("Please submit it again in a few minutes.")
	if undone:
		head = _("Not cancelled") if cancel else _("Not saved")
		outcome = "{0} {1}".format(_("Nothing was changed."), retry)
	else:
		head = _("Cancel failed") if cancel else _("Submit failed")
		outcome = _("Reload the document and check its status before trying again.")
	return f"{head}: {cause} {outcome}"


class CustomSubmissionQueue(SubmissionQueue):
	def insert(self, to_be_queued_doc: Document, action: str):
		if (
			self.ref_doctype in CURRENT_OPERATION_PREFLIGHT_DOCTYPES
			and (action or "").lower() == "submit"
		):
			# Lock-free work-order current-operation check in the user's own request: a stale
			# draft is refused here with the real reason and no queue row is created. The
			# worker re-checks authoritatively under locks before any side effect.
			from jewellery_erpnext.jewellery_erpnext.doc_events.current_operation_guard import (
				preflight,
			)

			preflight(to_be_queued_doc)

			if self.ref_doctype == "Employee IR":
				# Same idea for the supplier pure metal limit: refuse here, in the user's request,
				# rather than as a Failed queue row. The worker re-checks under the supplier lock.
				from jewellery_erpnext.jewellery_erpnext.doc_events.supplier_metal_limit import (
					employee_ir_preflight,
				)

				employee_ir_preflight(to_be_queued_doc)

		queue = frappe.db.get_value(
			"Submission Queue",
			{
				"ref_doctype": self.ref_doctype,
				"ref_docname": self.ref_docname,
				"status": ["in", ["Queued", "Finished"]],
			},
		)

		if self.ref_doctype in LONG_SUBMISSION_DOCTYPES and queue:
			frappe.msgprint(
				_("Queued for Submission. You can track the progress over {0}.").format(
					f"<a href='/app/submission-queue/{queue}'><b>here</b></a>"
				),
				indicator="red",
				raise_exception=1,
			)
		else:
			super().insert(to_be_queued_doc, action)

	def after_insert(self):
		if self.ref_doctype in LONG_SUBMISSION_DOCTYPES:
			# deduplicate by target document: if a background submission for this exact
			# (doctype, name) is already queued/running, RQ drops the duplicate instead
			# of running two writers that collide on the same Series/Bin/SRE rows.
			self.queue_action(
				"background_submission",
				to_be_queued_doc=self.queued_doc,
				action_for_queuing=self.action_for_queuing,
				timeout=4500,
				enqueue_after_commit=True,
				queue="long",
				job_id=f"submit::{self.ref_doctype}::{self.ref_docname}",
				deduplicate=True,
			)
		else:
			super().after_insert()

	def background_submission(
		self, to_be_queued_doc: Document, action_for_queuing: str
	):
		# frappe's base reaches notify("Failed") only after its own frappe.db.rollback()
		# returned, so on that path this attempt is confirmed undone. The reconnect path
		# below is not: the server discards the transaction, but nothing confirms it.
		self.flags.rollback_confirmed = True
		try:
			super().background_submission(to_be_queued_doc, action_for_queuing)
		except Exception:
			self.flags.rollback_confirmed = False
			# frappe marks the row Failed only after its own rollback succeeds. An RQ timeout
			# that lands mid-query leaves the connection out of sync (MySQL 2014), so that
			# rollback raises and the row stays "Queued" for good, which also blocks every
			# later submission of the document (queue_submission refuses while one is Queued).
			# Drop the broken connection (the server rolls the transaction back) and record the
			# failure on a fresh one.
			exception = frappe.get_traceback(with_context=True)
			with contextlib.suppress(Exception):
				frappe.db.close()
			frappe.db.connect()
			frappe.db.set_value(
				self.doctype,
				self.name,
				{"status": "Failed", "exception": exception, "ended_at": now()},
				update_modified=False,
			)
			frappe.db.commit()
			self.notify("Failed", action_for_queuing)

	def notify(self, submission_status: str, action: str):
		if submission_status == "Failed":
			exception = frappe.db.get_value(self.doctype, self.name, "exception") or ""
			undone = self._attempt_undone(action)
			try:
				sentence = describe_submission_failure(exception, action, undone)
			except Exception:
				# A wording bug must never cost the recorded failure: raising here would
				# send the job into the reconnect path, which rewrites `exception`.
				sentence = None
			if sentence:
				self._append_failure_summary(exception, sentence)
				self._send_failure_message(sentence)
				return
		super().notify(submission_status, action)

	def _attempt_undone(self, action):
		"""True only after frappe's own rollback of this attempt returned, with the
		document still in its pre-action state (draft for submit, submitted for cancel)."""
		if not self.flags.rollback_confirmed or not self.ref_doctype:
			return False
		expected = 1 if (action or "").lower() == "cancel" else 0
		docstatus = frappe.db.get_value(self.ref_doctype, self.ref_docname, "docstatus")
		return docstatus is not None and cint(docstatus) == expected

	def _append_failure_summary(self, exception, sentence):
		"""Keep the stored traceback byte for byte and add the message as its last line,
		which is what frappe's form banner shows ("Previous Submission (Failed): ...").
		Committed with the job, like frappe's own Failed write."""
		if exception.rstrip().endswith(sentence):
			return
		separator = "\n" if exception.endswith("\n") else "\n\n"
		frappe.db.set_value(
			self.doctype,
			self.name,
			"exception",
			f"{exception}{separator}{sentence}",
			update_modified=False,
		)

	def _send_failure_message(self, sentence):
		"""frappe's channel rule and recipient: a toast within 60 s of queueing, otherwise a
		Notification Log. Both point at this Submission Queue row."""
		label = frappe.bold(escape_html(f"{_(self.ref_doctype)} {self.ref_docname}"))
		message = f"{label}: {sentence}"
		if cint(time_diff_in_seconds(now(), self.created_at)) <= 60:
			route = quote(self.doctype.lower().replace(" ", "-"))
			link = f"<a href='/desk/{route}/{quote(self.name)}'><b>{_('here')}</b></a>"
			frappe.publish_realtime(
				"msgprint",
				{
					"message": "{0} {1}".format(message, _("View it {0}").format(link)),
					"alert": True,
					"indicator": "red",
				},
				user=self.enqueued_by,
			)
		else:
			notify_to = frappe.db.get_value("User", self.enqueued_by, fieldname="email")
			enqueue_create_notification(
				[notify_to],
				{
					"type": "Alert",
					"document_type": self.doctype,
					"document_name": self.name,
					"subject": message,
				},
			)
