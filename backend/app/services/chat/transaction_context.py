"""Keep the existing unified agent focused after transaction intent is established.

This is inventory reduction, not routing or authorization. Only tools already
made available by connector/role/policy discovery can survive this filter.
"""


def transaction_tools(definitions):
    from app.services.chat.metabase_context import metabase_tool_names
    from app.services.chat.tool_categories import is_celigo_source
    from app.services.chat.tools import parse_external_tool_name

    metabase = metabase_tool_names(definitions)
    local = {
        "agent_skill",
        "rag_search",
        "web_search",
        "skill_search",
        "skill_load",
        "skills_search",
        "skills_load",
        "drive_read_doc",
        "reference_previous_result",
        "analytics_calculate",
        "escalate_reasoning",
    }
    native = {
        "ns_getRecord",
        "ns_getRecordTypeMetadata",
        "ns_getSuiteQLMetadata",
        "ns_runCustomSuiteQL",
        "ns_updateRecord",
        "ns_createRecord",
        "ns_getAccountingBooks",
        "ns_getAccountingContexts",
        "ns_getNexusIds",
        "ns_getSubsidiaries",
        "ns_runSavedSearch",
        "ns_listSavedSearches",
    }
    result = []
    for tool in definitions:
        name = tool.get("name", "")
        normalized = name.replace(".", "_")
        external = parse_external_tool_name(name)
        if (
            normalized.startswith(("transaction_ops_", "netsuite_", "solidus_"))
            or normalized in local
            or name in metabase
            or is_celigo_source(name)
            or (external and external[1] in native)
        ):
            result.append(tool)
    return result
