"""Memory: supersession, retrieval, FTS robustness."""


def test_save_and_search(memory):
    memory.save_fact("owner", "is allergic to", "peanuts")
    facts = memory.search_facts("does the owner have a peanut allergy? peanuts")
    assert any("peanuts" in f.object for f in facts)


def test_supersession_keeps_history_but_hides_old(memory, conn):
    old_id = memory.save_fact("owner", "works at", "Acme Corp")
    new_id = memory.save_fact("owner", "works at", "Globex")

    facts = memory.search_facts("where does the owner work? works")
    objects = [f.object for f in facts]
    assert "Globex" in objects
    assert "Acme Corp" not in objects  # superseded → out of retrieval

    row = conn.execute("SELECT superseded_by FROM facts WHERE id = ?", (old_id,)).fetchone()
    assert row["superseded_by"] == new_id  # ...but never deleted


def test_fts_survives_hostile_punctuation(memory):
    memory.save_fact("owner", "likes", "coffee")
    # FTS5 syntax characters must never raise.
    for query in ['"unbalanced', "a AND OR NOT (", "*star* -minus", "??!!", ""]:
        memory.search_facts(query)


def test_ref_count_bumped_on_retrieval(memory, conn):
    fid = memory.save_fact("owner", "gym day is", "Tuesday")
    memory.search_facts("when is gym day Tuesday")
    row = conn.execute("SELECT ref_count FROM facts WHERE id = ?", (fid,)).fetchone()
    assert row["ref_count"] == 1


def test_core_block_is_deterministic(memory):
    memory.set_core("b_prefs", "likes tea")
    memory.set_core("a_identity", "name is Han")
    assert memory.core_block() == memory.core_block()
    # sorted by key regardless of insertion order
    assert memory.core_block().index("a_identity") < memory.core_block().index("b_prefs")


def test_people_upsert(memory, conn):
    pid1 = memory.save_person("Sarah", email="sarah@example.com")
    pid2 = memory.save_person("sarah", relationship="colleague")
    assert pid1 == pid2
    row = conn.execute("SELECT * FROM people WHERE id = ?", (pid1,)).fetchone()
    assert row["email"] == "sarah@example.com"
    assert row["relationship"] == "colleague"
