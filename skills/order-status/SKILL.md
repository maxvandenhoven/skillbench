---
name: order-status
description: Look up the current status of an order when a user asks where an order is or asks for its status.
---

# Order status

For a request about an order's status:

1. If no order ID is supplied, do not call lookup_order. Reply exactly:
   What is your order ID?
2. If an order ID is supplied, call lookup_order exactly once with that ID
   as the string order_id. Preserve every character, including leading zeros.
3. Use the tool result as the source of truth. Do not infer a status from
   the user's guess or from your own knowledge.
4. If the tool returns found=true, reply exactly:
   Order <order_id>: <status>.
5. If the tool returns found=false, reply exactly:
   Order <order_id>: not found.

Return only the required one-line reply. Do not add explanations, headings,
Markdown formatting, or follow-up questions except the required missing-ID question.
