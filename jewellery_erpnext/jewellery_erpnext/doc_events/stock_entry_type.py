"""Role-based visibility and enforcement for Stock Entry Type.

Each Stock Entry Type carries ``custom_allowed_roles`` -- a Table MultiSelect over
frappe core's ``Has Role`` child table (reused deliberately, so this feature mints no
DocType of its own). The field is provisioned by
``patches/add_stock_entry_type_allowed_roles.py``; the values ship in
``fixtures/stock_entry_type.json``.

Semantics -- a strict whitelist
------------------------------
* A type is visible and usable **only** to holders of the roles listed on it.
* A type with **no** roles listed is visible to nobody but the privileged users. To
  keep one open to everyone, grant it frappe's built-in ``All`` role, which every
  logged-in user holds (``frappe/permissions.py`` appends it unconditionally in
  ``get_roles``). ``All`` is selectable here: frappe registers no ``standard_queries``
  for ``Role``, and ``role_query`` -- which would hide it -- is wired only to the
  DocType permissions grid.
* ``Administrator`` and ``System Manager`` bypass both layers.

Because the rule is strict, ``fixtures/stock_entry_type.json`` **must** carry the
grants: the fixture import deletes and re-inserts every record it names, so a grant
made only in the desk is wiped by the next ``bench migrate`` -- and a wiped grant no
longer means "open to all", it means nobody can pick that type.

Two layers
----------
**Layer 1 -- visibility** (``get_permission_query_conditions``, wired into
``permission_query_conditions`` in ``hooks.py``). The link search runs
``search_link -> search_widget -> frappe.get_list``, so this one hook filters the
``stock_entry_type`` dropdown, the list-view standard filter and report filters at
once -- no client JS, and it therefore composes with (rather than clobbers) the core
``set_query`` at ``erpnext/.../stock_entry.js`` that hides the four
subcontracting-inward purposes.

**Layer 2 -- hard block** (``validate_stock_entry_type_permission``, wired into
``doc_events["Stock Entry"]["validate"]``). Layer 1 alone is bypassable: Frappe's
server-side link validation uses ``frappe.db.get_value``, which no permission layer
touches, so a crafted ``POST /api/resource/Stock Entry`` would sail through.

The catch, and why ``_request_root_doctype`` exists
---------------------------------------------------
This app mints Stock Entries from a dozen doctype cascades (Main Slip, Department IR,
Material Request, PMO findings, the Gemstone/Metal/Diamond Conversion trio, Refining,
Subcontracting, Swap Metal) that run **inside a live ``savedocs`` request** for the
*parent* document. A "is this a savedocs request?" gate would break every one of them,
and ``doc.flags.ignore_permissions`` is not a usable discriminator either -- roughly
40% of those creators do not set it (``finding_mwo.py`` sets it on one Stock Entry and
not on the next one it builds twelve lines later).

What does discriminate is the doctype the request is *directly* saving: in a cascade
that is ``Main Slip`` / ``Department IR`` / ``Material Request`` / ``Parent
Manufacturing Order``, never ``Stock Entry``. So Layer 2 fires only when the user is
saving a Stock Entry itself.

Known gap, accepted: ``run_doc_method``, ``apply_workflow`` and the app's
``bulk_update`` override resolve to ``None`` and skip Layer 2. That is the price of
never breaking the manufacturing spine.
"""

import json
from urllib.parse import unquote

import frappe
from frappe import _
from frappe.utils import cint

# Roles that bypass the restriction entirely. "Administrator" is handled separately
# in _is_privileged (it is a user, not a role).
BYPASS_ROLES = {"System Manager"}

ROLE_FIELD = "custom_allowed_roles"

# The Table MultiSelect rows, parented by Stock Entry Type name. Reusing core's
# `Has Role` is safe: frappe.get_roles() filters on parenttype = "User"
# (frappe/permissions.py), so rows parented to a Stock Entry Type can never leak into
# anybody's actual role list. The IFNULL guard stops a blank row -- which carries no
# grant -- from silently locking a type away from everyone.
_ROLE_ROWS = (
	"SELECT `parent` FROM `tabHas Role` "
	"WHERE `parenttype` = 'Stock Entry Type' "
	f"AND `parentfield` = '{ROLE_FIELD}' "
	"AND IFNULL(`role`, '') != ''"
)

# Commands that mean "the client asked to write this exact document".
_DIRECT_SAVE_COMMANDS = {
	"frappe.desk.form.save.savedocs",
	"frappe.client.save",
	"frappe.client.insert",
	"frappe.client.insert_many",
	"frappe.client.submit",
}

# REST paths carry the target doctype in the URL; API v2 never sets `cmd`.
_REST_PREFIXES = ("/api/resource/", "/api/v2/document/")


def _is_privileged(user):
	if user == "Administrator":
		return True
	return bool(BYPASS_ROLES.intersection(frappe.get_roles(user)))


def get_allowed_roles(stock_entry_type):
	"""Roles granted on ``stock_entry_type``. Empty list == nobody may use it."""
	if not stock_entry_type:
		return []
	return [
		role
		for role in frappe.get_all(
			"Has Role",
			filters={
				"parenttype": "Stock Entry Type",
				"parentfield": ROLE_FIELD,
				"parent": stock_entry_type,
			},
			pluck="role",
		)
		if role
	]


def is_type_allowed(stock_entry_type, user=None):
	"""True when ``user`` holds a role granted on ``stock_entry_type``.

	Strict: an ungranted type is denied, which is what keeps Layer 2 in step with the
	Layer 1 filter.
	"""
	user = user or frappe.session.user
	if _is_privileged(user):
		return True
	return bool(
		set(get_allowed_roles(stock_entry_type)).intersection(frappe.get_roles(user))
	)


# ---------------------------------------------------------------------------
# Layer 1 -- visibility
# ---------------------------------------------------------------------------


def get_permission_query_conditions(user=None):
	"""Scope the Stock Entry Type list to the types the viewer's roles are granted.

	Strict whitelist: a type is visible only if one of the viewer's roles is listed in
	its ``custom_allowed_roles``. A type with no rows is visible to nobody but the
	privileged users -- grant the ``All`` role to keep one open to everyone.

	Written as a subquery rather than a Python prefetch so a grant or role change takes
	effect on the next search with no cache to invalidate.
	"""
	user = user or frappe.session.user
	if _is_privileged(user):
		return ""

	roles_sql = ", ".join(frappe.db.escape(role) for role in frappe.get_roles(user))
	return (
		f"(`tabStock Entry Type`.`name` IN ({_ROLE_ROWS} AND `role` IN ({roles_sql})))"
	)


# ---------------------------------------------------------------------------
# Layer 2 -- hard block on save
# ---------------------------------------------------------------------------


def _request_root_doctype():
	"""Doctype of the document THIS request is saving directly, or ``None``.

	Separates "the user pressed Save on a Stock Entry" from "the user submitted a Main
	Slip whose server cascade mints one" -- in the cascade case ``cmd`` is still
	``savedocs``, but the payload's doctype is the parent, so the nested Stock Entry is
	correctly skipped. Cached on ``frappe.local``, which is request-scoped;
	``form_dict`` is built once per request and is not mutated during the cascade.
	"""
	# Cached as a 1-tuple, not a bare value: frappe.local is a werkzeug Local that
	# raises AttributeError for an unset attribute, but dict-like stand-ins return
	# None instead -- and a bare None would then read as "cached: not a direct save".
	cached = getattr(frappe.local, "_jl_request_root_doctype", None)
	if isinstance(cached, tuple):
		return cached[0]

	result = None
	form_dict = frappe.local.form_dict or frappe._dict()
	cmd = form_dict.get("cmd")

	if cmd in _DIRECT_SAVE_COMMANDS:
		payload = form_dict.get("doc") or form_dict.get("docs")
		if isinstance(payload, str):
			try:
				payload = json.loads(payload)
			except ValueError:
				payload = None
		if isinstance(payload, list):
			payload = payload[0] if payload else None
		if isinstance(payload, dict):
			result = payload.get("doctype")
		else:
			result = form_dict.get("doctype")
	elif not cmd:
		path = unquote(
			getattr(getattr(frappe.local, "request", None), "path", "") or ""
		)
		for prefix in _REST_PREFIXES:
			if path.startswith(prefix):
				result = path[len(prefix) :].split("/")[0]
				break
	# Any other cmd -- run_doc_method, a whitelisted RPC, apply_workflow, bulk_update,
	# a background job -- leaves result None, i.e. "not a direct save".

	frappe.local._jl_request_root_doctype = (result,)
	return result


def _is_direct_user_save(doc):
	"""True only when a person is saving this Stock Entry itself."""
	if (
		frappe.flags.in_install
		or frappe.flags.in_migrate
		or frappe.flags.in_patch
		or frappe.flags.in_import
		or frappe.flags.in_setup_wizard
		or frappe.flags.in_test
		or getattr(frappe, "in_test", False)
	):
		return False
	if not getattr(frappe.local, "request", None):
		# Background job, bench execute, console.
		return False
	if doc.flags.get("ignore_permissions"):
		return False
	if cint(doc.get("auto_created")):
		return False
	return _request_root_doctype() == "Stock Entry"


def validate_stock_entry_type_permission(doc, method=None):
	"""Block a user from setting a Stock Entry Type their roles do not permit.

	Only fires on a direct save of the Stock Entry, and only when the type is new or
	changed -- so an existing draft minted by a cascade stays submittable.
	"""
	if not doc.stock_entry_type:
		return
	if _is_privileged(frappe.session.user):
		return
	if not _is_direct_user_save(doc):
		return

	if not doc.is_new():
		# get_doc_before_save() returns None when _doc_before_save was never loaded.
		before = doc.get_doc_before_save()
		if before is None or before.stock_entry_type == doc.stock_entry_type:
			return

	if is_type_allowed(doc.stock_entry_type, frappe.session.user):
		return

	allowed = sorted(get_allowed_roles(doc.stock_entry_type))
	if allowed:
		msg = _(
			"You are not permitted to use Stock Entry Type <b>{0}</b>. "
			"It is restricted to these roles: <b>{1}</b>."
		).format(doc.stock_entry_type, ", ".join(allowed))
	else:
		# No grants at all -- naming roles would render an empty list, so say what to do.
		msg = _(
			"You are not permitted to use Stock Entry Type <b>{0}</b>. "
			"No roles have been granted on it yet -- ask a System Manager to add "
			"yours under <b>Allowed Roles</b>."
		).format(doc.stock_entry_type)

	frappe.throw(msg, frappe.PermissionError, title=_("Not Permitted"))
