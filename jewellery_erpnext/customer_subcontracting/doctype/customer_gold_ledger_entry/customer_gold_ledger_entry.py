# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Append-only record of what happened to a customer's gold.

WHY THIS DOCTYPE IS HERE AT ALL, AND WHERE IT CAME FROM
-------------------------------------------------------
It already existed on the `gk` site as a ``tabDocType`` row with **no source file in any app**
-- created by a branch that no longer exists, `custom = 0`, 0 rows. Leaving it that way was
the hazard: because `custom = 0`, `bench migrate` treats a same-named app DocType as the
authority, so a field whose *meaning* changed while its *name* stayed would be silently
reinterpreted. Adopting it as real source is what makes that safe, and it is the route the
orphan-schema inventory recommended over deletion.

The schema is reproduced from the live definition verbatim -- all 34 fields, the 15
``cg_event_kind`` options, the 6 ``cg_stage`` options and the three read-only DocPerms
(System Manager / Stock Manager / Accounts Manager) -- because the JSON REPLACES those rows on
import. One field is added: ``serial_no``. The C12 fixture is entirely per-serial ("deliver
Serial 1, leave Serial 2 held") and the inherited schema had nowhere to put one.

WHAT IT IS NOT
--------------
Not a second valuation engine, and not a balance table. Balances are projections over these
events plus standard stock state. Nothing here is mutable: every field is ``read_only``, the
doctype is **not submittable**, and a reversal is a NEW row pointing at the original through
``cg_reversal_of`` -- never an edit or a cancel. That matches CG-T143's "canceled audit rows
need not be deleted".

``cg_event_key`` carries a UNIQUE constraint. That, not an existence check, is what makes a
repeated callback or a concurrent retry produce one effective event (CG-T137, CG-T138).

THE ONLY MONETARY FIELDS are ``cg_carrying_value_delta`` and ``cg_currency``. They are left
NULL unless the configured valuation policy is Nominal. With them empty the row is still a
complete memorandum record -- which is exactly what the zero-value model needs, so the same
writer serves both policies and D01 does not gate the event itself.
"""

from frappe.model.document import Document


class CustomerGoldLedgerEntry(Document):
	pass
