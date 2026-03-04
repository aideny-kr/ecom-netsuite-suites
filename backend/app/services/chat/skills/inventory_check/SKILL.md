---
Name: Inventory Availability Check
Description: Checks current inventory levels for items across warehouse locations, showing quantity on hand and available.
Triggers:
  - /inventory
  - check inventory
  - stock levels
  - inventory availability
  - how much stock
---

# Inventory Availability Check

You are executing the Inventory Availability Check skill. Follow these exact steps:

1. **Identify Items:**
   - Check if the user specified item names, SKUs, or a product category.
   - If not specified, show top items by quantity available across all locations.

2. **Run the Query:**
   ```sql
   SELECT i.itemid,
          i.displayname,
          BUILTIN.DF(iil.location) as location,
          iil.quantityonhand,
          iil.quantityavailable
   FROM inventoryitemlocations iil
   JOIN item i ON i.id = iil.item
   WHERE iil.quantityavailable > 0
   ORDER BY i.itemid
   FETCH FIRST 100 ROWS ONLY
   ```
   - If user specified an item: add `AND (i.displayname LIKE '%keyword%' OR i.itemid LIKE '%keyword%')`.
   - If query returns 0 rows, retry WITHOUT the `quantityavailable > 0` filter.

3. **Present Results:**
   - Format as a markdown table: Item ID, Name, Location, On Hand, Available.
   - If multiple locations, group by item and show subtotals.
   - Flag any items where available quantity is 0 or negative.
