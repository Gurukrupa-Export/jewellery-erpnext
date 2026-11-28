# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Which customer-gold receipt each delivery and raw return drew on, and for how much.

WHY THIS EXISTS
---------------
The settlement Journal Entry releases one amount per customer per document, and the Delivery
event records one total per serial. Neither says which RECEIPT the released gold came from --
``_customer_share`` computed that per component and then threw it away, and the Batch
Component rows it read are replaced on the next production. So a customer with three receipts
at three rates could see the account balance fall without anyone being able to say which receipt
was still open.

One row here is one (disposition event, receipt) pair, written in the same transaction as the
event it decomposes. The rows under one event sum to that event's quantity and value, so one
Journal Entry settling several receipts stays decomposable by receipt.

Immutable, like the ledger: every field is read-only, the doctype is not submittable, and a
cancellation writes a NEW row with ``reversal_of`` pointing at the original.
"""

from frappe.model.document import Document


class CustomerGoldAllocation(Document):
	pass
