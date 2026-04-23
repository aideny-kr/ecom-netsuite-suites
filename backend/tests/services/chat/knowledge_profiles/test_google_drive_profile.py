from app.services.chat.knowledge_profiles.loader import load_all_profiles


def test_google_drive_profile_loads():
    profiles = load_all_profiles()
    drive = next((p for p in profiles if p.profile_id == "google_drive"), None)
    assert drive is not None
    assert drive.display_name


def test_google_drive_profile_triggers_on_drive_read_doc():
    profiles = load_all_profiles()
    drive = next(p for p in profiles if p.profile_id == "google_drive")
    assert drive.matches_tools({"drive_read_doc"})


def test_google_drive_profile_triggers_on_sheets_tools():
    # Drive RAG is active whenever Sheets connector is active (shared SA).
    profiles = load_all_profiles()
    drive = next(p for p in profiles if p.profile_id == "google_drive")
    assert drive.matches_tools({"sheets_read_range"})
    assert drive.matches_tools({"sheets_create"})
    assert drive.matches_tools({"sheets_write_range"})


def test_google_drive_profile_does_not_trigger_on_unrelated_tools():
    profiles = load_all_profiles()
    drive = next(p for p in profiles if p.profile_id == "google_drive")
    assert not drive.matches_tools({"netsuite_suiteql"})
    assert not drive.matches_tools({"bigquery_sql"})


def test_google_drive_profile_has_citation_instructions():
    profiles = load_all_profiles()
    drive = next(p for p in profiles if p.profile_id == "google_drive")
    frag = drive.prompt_fragment.lower()
    assert "drive_knowledge" in frag
    assert "[" in frag and "]" in frag  # citation syntax
    assert "source_name" in frag


def test_google_drive_profile_rag_partitions_empty():
    """Drive uses a dedicated retrieval path, not the shared partition mechanism."""
    profiles = load_all_profiles()
    drive = next(p for p in profiles if p.profile_id == "google_drive")
    assert drive.rag_partitions == []
