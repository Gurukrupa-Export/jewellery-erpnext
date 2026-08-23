
# import frappe


# # def add_item_bom_to_kggk(doc, method=None):

# #     import requests


# #     def make_json_serializable(data):

# #         return frappe.parse_json(
# #             frappe.as_json(data)
# #         )

# #     items = []
# #     boms = []

# #     # =====================================================
# #     # PREPARE ALL ITEMS + BOMS
# #     # =====================================================

# #     for row in doc.manufacturing_plan_table:

# #         item_code = row.item_code
# #         bom_name = row.bom

# #         if not item_code:
# #             continue

# #         # =================================================
# #         # ITEM
# #         # =================================================

# #         item_doc = frappe.get_doc(
# #             "Item",
# #             item_code
# #         )

# #         item_data = make_json_serializable(
# #                 item_doc.as_dict()
# #         )

# #         items.append({
# #             "item": item_code,
# #             "item_data": item_data
# #         })

# #         # =================================================
# #         # BOM
# #         # =================================================

# #         if bom_name:

# #             bom_doc = frappe.get_doc(
# #                 "BOM",
# #                 bom_name
# #             )

# #             bom_data = make_json_serializable(
# #                 bom_doc.as_dict()
# #             )

# #             boms.append({
# #                 "bom_name": bom_name,
# #                 "bom_data": bom_data
# #             })

# #     # =====================================================
# #     # NOTHING TO SYNC
# #     # =====================================================

# #     if not items and not boms:
# #         return

# #     # =====================================================
# #     # SEND ONE REQUEST TO KGGK
# #     # =====================================================

# #     response = requests.post(
# #         "https://gkexport-dummy-v16.m.frappe.cloud//api/method/add_item_and_bom",
# #         json={
# #             "items": items,
# #             "boms": boms
# #         },
# #         timeout=300
# #     )

# #     # =====================================================
# #     # CHECK RESPONSE
# #     # =====================================================

# #     if response.status_code not in [200, 201]:

# #         frappe.throw(
# #             "KGGK Item/BOM Sync Failed\n\n"
# #             + response.text
# #         )

# #     response_data = response.json()

# #     # =====================================================
# #     # CHECK KGGK RESPONSE
# #     # =====================================================

# #     message = response_data.get("message")

# #     item_result = message.get("items", {})

# #     success_items = item_result.get(
# #         "success_records",
# #         []
# #     )

# #     failed_items = item_result.get(
# #         "failed_records",
# #         []
# #     )


# #     # =====================================================
# #     # MARK SUCCESSFULLY SYNCED ITEMS
# #     # =====================================================

# #     for item_record in success_items:

# #         item_name = item_record.get("item")

# #         if not item_name:
# #             continue

# #         frappe.db.set_value(
# #             "Item",
# #             item_name,
# #             "custom_is_sync",
# #             1
# #         )


# #     if not message:
# #         frappe.throw(
# #             "KGGK returned invalid response\n\n"
# #             + str(response_data)
# #         )

# #     # Agar KGGK mein failures aaye
# #     item_failed = message.get("items", {}).get(
# #         "failed_records", []
# #     )

# #     bom_failed = message.get("boms", {}).get(
# #         "failed_records", []
# #     )

# #     if item_failed or bom_failed:

# #         error_message = []

# #         if item_failed:
# #             error_message.append(
# #                 "ITEM ERRORS:\n"
# #                 + "\n".join(
# #                     str(x)
# #                     for x in item_failed
# #                 )
# #             )

# #         if bom_failed:
# #             error_message.append(
# #                 "BOM ERRORS:\n"
# #                 + "\n".join(
# #                     str(x)
# #                     for x in bom_failed
# #                 )
# #             )

# #         frappe.throw(
# #             "\n\n".join(error_message)
# #         )


# KGGK_ITEM_BOM_SYNC_URL = (
#     "https://kggk-prod.frappe.cloud/api/method/add_item_and_bom"
# )
# KGGK_SYNC_TIMEOUT = 300
# KGGK_SETTINGS_DOCTYPE = "Item Migration in KGGK"


# def is_kggk_item_bom_sync_enabled():
#     value = frappe.db.get_single_value(
#         KGGK_SETTINGS_DOCTYPE,
#         "enable_item_bom_sync_on_manufacturing_plan_submit",
#     )
#     if value is None:
#         return True
#     return frappe.utils.cint(value)


# def make_json_serializable(data):
#     return frappe.parse_json(frappe.as_json(data))


# def prepare_items_and_boms_from_table(table_rows, plan_name):
#     items = []
#     boms = []
#     processed_items = set()
#     processed_boms = set()

#     for row in table_rows:
#         item_code = row.item_code
#         bom_name = row.bom

#         if not item_code:
#             continue

#         if item_code not in processed_items:
#             processed_items.add(item_code)

#             if not frappe.db.exists("Item", item_code):
#                 frappe.log_error(
#                     f"Item not found: {item_code}\nManufacturing Plan: {plan_name}",
#                     "KGGK Item Sync",
#                 )
#             elif not frappe.db.get_value("Item", item_code, "custom_is_sync"):
#                 try:
#                     item_doc = frappe.get_doc("Item", item_code)
#                     items.append({
#                         "item": item_code,
#                         "item_data": make_json_serializable(item_doc.as_dict()),
#                     })
#                 except Exception as e:
#                     frappe.log_error(
#                         f"Item: {item_code}\n\n{str(e)}",
#                         "KGGK Item Prepare Failed",
#                     )

#         if not bom_name or bom_name in processed_boms:
#             continue

#         processed_boms.add(bom_name)

#         if not frappe.db.exists("BOM", bom_name):
#             frappe.log_error(
#                 f"BOM not found: {bom_name}\n"
#                 f"Item: {item_code}\n"
#                 f"Manufacturing Plan: {plan_name}",
#                 "KGGK BOM Sync",
#             )
#             continue

#         try:
#             bom_doc = frappe.get_doc("BOM", bom_name)
#             boms.append({
#                 "bom_name": bom_name,
#                 "bom_data": make_json_serializable(bom_doc.as_dict()),
#             })
#         except Exception as e:
#             frappe.log_error(
#                 f"BOM: {bom_name}\n\n{str(e)}",
#                 "KGGK BOM Prepare Failed",
#             )

#     return items, boms


# def send_items_and_boms_to_kggk(items, boms):
#     import requests

#     try:
#         response = requests.post(
#             KGGK_ITEM_BOM_SYNC_URL,
#             json={"items": items, "boms": boms},
#             timeout=KGGK_SYNC_TIMEOUT,
#         )
#     except Exception as e:
#         frappe.log_error(str(e), "KGGK Item/BOM API Connection Failed")
#         return None

#     if response.status_code not in (200, 201):
#         frappe.log_error(response.text, "KGGK Item/BOM Sync API Failed")
#         return None

#     try:
#         response_data = response.json()
#     except Exception:
#         frappe.log_error(response.text, "KGGK Invalid JSON Response")
#         return None

#     message = response_data.get("message")
#     if not message:
#         frappe.log_error(str(response_data), "KGGK Invalid Sync Response")
#         return None

#     return message


# def mark_synced_items(success_items):
#     synced_count = 0

#     for item_record in success_items:
#         item_name = item_record.get("item")
#         if not item_name or not frappe.db.exists("Item", item_name):
#             continue

#         frappe.db.set_value(
#             "Item",
#             item_name,
#             "custom_is_sync",
#             1,
#             update_modified=False,
#         )
#         synced_count += 1

#     return synced_count


# def log_kggk_sync_summary(items, boms, message):
#     item_result = message.get("items", {})
#     bom_result = message.get("boms", {})

#     success_items = item_result.get("success_records", [])
#     failed_items = item_result.get("failed_records", [])
#     failed_boms = bom_result.get("failed_records", [])

#     frappe.logger().info(
#         f"KGGK ITEM/BOM SYNC COMPLETED | "
#         f"Items sent: {len(items)} | "
#         f"Items synced: {len(success_items)} | "
#         f"Items failed: {len(failed_items)} | "
#         f"BOMs sent: {len(boms)} | "
#         f"BOMs failed: {len(failed_boms)}"
#     )

#     if failed_items:
#         frappe.log_error(
#             frappe.as_json(failed_items, indent=2),
#             "KGGK Item Sync Failed Items",
#         )

#     if failed_boms:
#         frappe.log_error(
#             frappe.as_json(failed_boms, indent=2),
#             "KGGK BOM Sync Failed BOMs",
#         )


# def add_item_bom_to_kggk(doc, method=None):
#     if not is_kggk_item_bom_sync_enabled():
#         frappe.logger().info(
#             "KGGK Item Sync: Skipped (disabled in Item Migration in KGGK)"
#         )
#         return

#     items, boms = prepare_items_and_boms_from_table(
#         doc.manufacturing_plan_table,
#         doc.name,
#     )

#     if not items and not boms:
#         frappe.logger().info(
#             "KGGK Item Sync: Nothing to sync from Manufacturing Plan Table"
#         )
#         return

#     message = send_items_and_boms_to_kggk(items, boms)
#     if not message:
#         return

#     item_result = message.get("items", {})
#     success_items = item_result.get("success_records", [])
#     mark_synced_items(success_items)
#     log_kggk_sync_summary(items, boms, message)



# def add_item_bom_to_kggk_by_schedule():

#     import requests

#     if not is_kggk_item_bom_sync_enabled():
#         return {
#             "status": "skipped",
#             "message": "Item/BOM sync is disabled in Data Migration in KGGK",
#         }

#     # =====================================================
#     # JSON SERIALIZER
#     # =====================================================

#     def make_json_serializable(data):

#         return frappe.parse_json(
#             frappe.as_json(data)
#         )


#     today = frappe.utils.getdate()

#     # Current date se 3 din piche
#     sync_date = frappe.utils.add_days(
#         today,
#         -3
#     )

#     from_date = str(sync_date)
#     to_date = str(sync_date)

#     # =====================================================
#     # VALIDATE DATES
#     # =====================================================

#     if not from_date:
#         frappe.throw("From Date is required")

#     if not to_date:
#         frappe.throw("To Date is required")


#     # =====================================================
#     # GET MANUFACTURING PLANS
#     # BASED ON CREATION DATE
#     # =====================================================

#     manufacturing_plans = frappe.get_all(
#         "Manufacturing Plan",
#         filters=[
#             [
#                 "Manufacturing Plan",
#                 "creation",
#                 ">=",
#                 from_date + " 00:00:00"
#             ],
#             [
#                 "Manufacturing Plan",
#                 "creation",
#                 "<=",
#                 to_date + " 23:59:59"
#             ]
#         ],
#         pluck="name"
#     )


#     # =====================================================
#     # NO MANUFACTURING PLAN
#     # =====================================================

#     if not manufacturing_plans:

#         return {
#             "status": "completed",
#             "from_date": from_date,
#             "to_date": to_date,
#             "manufacturing_plans": 0,
#             "items_sent": 0,
#             "boms_sent": 0
#         }


#     items = []
#     boms = []

#     # Duplicate avoid karne ke liye
#     processed_items = set()
#     processed_boms = set()


#     # =====================================================
#     # PROCESS MANUFACTURING PLANS
#     # =====================================================

#     for plan_name in manufacturing_plans:

#         try:

#             plan = frappe.get_doc(
#                 "Manufacturing Plan",
#                 plan_name
#             )


#             # =================================================
#             # CHILD TABLE
#             # =================================================

#             for row in plan.manufacturing_plan_table:

#                 item_code = row.item_code
#                 bom_name = row.bom


#                 # =============================================
#                 # ITEM CODE REQUIRED
#                 # =============================================

#                 if not item_code:
#                     continue


#                 # =============================================
#                 # CHECK ITEM EXISTS
#                 # =============================================

#                 if not frappe.db.exists(
#                     "Item",
#                     item_code
#                 ):

#                     frappe.log_error(
#                         f"Item not found: {item_code}\n"
#                         f"Manufacturing Plan: {plan_name}",
#                         "KGGK Item Sync"
#                     )

#                     continue


#                 # =============================================
#                 # CHECK custom_is_sync
#                 # =============================================

#                 is_sync = frappe.db.get_value(
#                     "Item",
#                     item_code,
#                     "custom_is_sync"
#                 )


#                 # =============================================
#                 # ALREADY SYNCED -> SKIP
#                 # =============================================

#                 if is_sync:

#                     continue


#                 # =============================================
#                 # DUPLICATE ITEM -> SKIP
#                 # =============================================

#                 if item_code in processed_items:

#                     # BOM phir bhi check kar sakte hain
#                     # lekin Item dobara nahi bhejna

#                     pass

#                 else:

#                     processed_items.add(
#                         item_code
#                     )


#                     # =========================================
#                     # GET ITEM
#                     # =========================================

#                     item_doc = frappe.get_doc(
#                         "Item",
#                         item_code
#                     )


#                     item_data = make_json_serializable(
#                         item_doc.as_dict()
#                     )


#                     items.append({

#                         "item": item_code,

#                         "item_data": item_data

#                     })


#                 # =============================================
#                 # BOM
#                 # =============================================

#                 if not bom_name:

#                     continue


#                 # =============================================
#                 # DUPLICATE BOM
#                 # =============================================

#                 if bom_name in processed_boms:

#                     continue


#                 processed_boms.add(
#                     bom_name
#                 )


#                 # =============================================
#                 # CHECK BOM EXISTS IN GK
#                 # =============================================

#                 if not frappe.db.exists(
#                     "BOM",
#                     bom_name
#                 ):

#                     frappe.log_error(
#                         f"BOM not found: {bom_name}\n"
#                         f"Item: {item_code}\n"
#                         f"Manufacturing Plan: {plan_name}",
#                         "KGGK BOM Sync"
#                     )

#                     continue


#                 # =============================================
#                 # GET BOM
#                 # =============================================

#                 bom_doc = frappe.get_doc(
#                     "BOM",
#                     bom_name
#                 )


#                 bom_data = make_json_serializable(
#                     bom_doc.as_dict()
#                 )


#                 boms.append({

#                     "bom_name": bom_name,

#                     "bom_data": bom_data

#                 })


#         except Exception as e:

#             frappe.log_error(

#                 f"Manufacturing Plan: {plan_name}\n\n"
#                 f"{str(e)}",

#                 "KGGK Manufacturing Plan Sync Failed"

#             )


#     # =====================================================
#     # NOTHING TO SYNC
#     # =====================================================

#     if not items:

#         return {

#             "status": "completed",

#             "from_date": from_date,

#             "to_date": to_date,

#             "manufacturing_plans": len(
#                 manufacturing_plans
#             ),

#             "items_sent": 0,

#             "boms_sent": 0,

#             "message": "No unsynced items found"

#         }


#     # =====================================================
#     # SEND ONE REQUEST TO KGGK
#     # =====================================================

#     try:

#         response = requests.post(

#             "https://gkexport-dummy-v16.m.frappe.cloud/api/method/add_item_and_bom",

#             json={

#                 "items": items,

#                 "boms": boms

#             },

#             timeout=300

#         )

#     except Exception as e:

#         frappe.log_error(

#             str(e),

#             "KGGK Item BOM API Connection Failed"

#         )

#         return {

#             "status": "failed",

#             "error": str(e)

#         }


#     # =====================================================
#     # HTTP RESPONSE
#     # =====================================================

#     if response.status_code not in [200, 201]:

#         frappe.log_error(

#             response.text,

#             "KGGK Item BOM API Failed"

#         )

#         return {

#             "status": "failed",

#             "error": response.text

#         }


#     # =====================================================
#     # PARSE RESPONSE
#     # =====================================================

#     try:

#         response_data = response.json()

#     except Exception:

#         frappe.log_error(

#             response.text,

#             "KGGK Invalid JSON Response"

#         )

#         return {

#             "status": "failed",

#             "error": "Invalid JSON response"

#         }


#     # =====================================================
#     # GET MESSAGE
#     # =====================================================

#     message = response_data.get(
#         "message"
#     )


#     if not message:

#         frappe.log_error(

#             frappe.as_json(
#                 response_data,
#                 indent=2
#             ),

#             "KGGK Invalid Sync Response"

#         )

#         return {

#             "status": "failed",

#             "error": "Invalid KGGK response"

#         }


#     # =====================================================
#     # ITEM RESPONSE
#     # =====================================================

#     item_result = message.get(
#         "items",
#         {}
#     )


#     success_items = item_result.get(
#         "success_records",
#         []
#     )


#     failed_items = item_result.get(
#         "failed_records",
#         []
#     )


#     # =====================================================
#     # MARK SUCCESSFUL ITEMS AS SYNCED
#     # =====================================================

#     synced_count = 0


#     for item_record in success_items:

#         item_name = item_record.get(
#             "item"
#         )


#         if not item_name:
#             continue


#         if frappe.db.exists(
#             "Item",
#             item_name
#         ):

#             frappe.db.set_value(

#                 "Item",

#                 item_name,

#                 "custom_is_sync",

#                 1,

#                 update_modified=False

#             )

#             synced_count += 1


#     # =====================================================
#     # BOM RESPONSE
#     # =====================================================

#     bom_result = message.get(
#         "boms",
#         {}
#     )


#     success_boms = bom_result.get(
#         "success_records",
#         []
#     )


#     failed_boms = bom_result.get(
#         "failed_records",
#         []
#     )


#     # =====================================================
#     # COMMIT
#     # =====================================================

#     if synced_count:

#         frappe.db.commit()


#     # =====================================================
#     # LOG FAILURES
#     # =====================================================

#     if failed_items:

#         frappe.log_error(

#             frappe.as_json(
#                 failed_items,
#                 indent=2
#             ),

#             "KGGK Failed Items"

#         )


#     if failed_boms:

#         frappe.log_error(

#             frappe.as_json(
#                 failed_boms,
#                 indent=2
#             ),

#             "KGGK Failed BOMs"

#         )


#     # =====================================================
#     # FINAL SUMMARY
#     # =====================================================

#     return {

#         "status": "completed",

#         "from_date": from_date,

#         "to_date": to_date,

#         "manufacturing_plans": len(
#             manufacturing_plans
#         ),

#         "items_found": len(
#             processed_items
#         ),

#         "items_sent": len(
#             items
#         ),

#         "items_synced": synced_count,

#         "items_failed": len(
#             failed_items
#         ),

#         "boms_sent": len(
#             boms
#         ),

#         "boms_success": len(
#             success_boms
#         ),

#         "boms_failed": len(
#             failed_boms
#         )

#     }



# # scheduler_events = {
# #     "cron": {
# #         "0 0 * * *": [
# #             "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.doc_events.add_item_bom_to_kggk.scheduled_item_bom_sync"
# #         ]
# #     }
# # }