---
Name: NetSuite CSV Import Template Generator
Description: Generates an empty CSV template with exact mandatory headers for any NetSuite record type to ensure successful CSV Imports.
Triggers:
  - /csv-template
  - create a csv import template
  - generate csv template
  - import template for netsuite
---

# NetSuite CSV Import Template Generator

You are executing the NetSuite CSV Import Template Generator skill. Follow these exact steps sequentially:

1. **Identify the Record Type:**
   - Check if the user has specified a NetSuite record type (e.g., Customer, Sales Order, Inventory Item).
   - If they have not, ask them directly: "Which NetSuite record type are you preparing a CSV import for?"
   - **WAIT** for the user's response before proceeding.

2. **Fetch Record Metadata:**
   - Use `netsuite_get_metadata` to query the specific record type provided by the user.
   - Analyze the returned schema to identify all **mandatory fields** (fields marked as required).

3. **Generate CSV Template:**
   - Use `workspace_propose_patch` to create a CSV file in the workspace.
   - The first row (headers) MUST consist precisely of the mandatory fields identified in the previous step.
   - Include a second row with example placeholder values commented with the expected data type.

4. **Return the Result:**
   - Output a brief markdown table of the mandatory columns included in the template.
   - Explain what data the user needs to provide for each column.
   - Mention the file path where the template was saved.
