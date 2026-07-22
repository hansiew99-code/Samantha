"""Memory: supersession, retrieval, FTS robustness."""

from samantha.memory import CORE_BLOCK_CHAR_LIMIT, CORE_VALUE_CHAR_LIMIT


def test_save_and_search(memory):
    memory.save_fact("owner", "is allergic to", "peanuts")
    facts = memory.search_facts("does the owner have a peanut allergy? peanuts")
    assert any("peanuts" in f.object for f in facts)


def test_supersession_keeps_history_but_hides_old(memory, conn):
    old_id = memory.save_fact("owner", "works at", "Acme Corp")
    new_id = memory.save_fact(
        "owner", "works at", "Globex", replace_existing=True
    )

    facts = memory.search_facts("where does the owner work? works")
    objects = [f.object for f in facts]
    assert "Globex" in objects
    assert "Acme Corp" not in objects  # superseded → out of retrieval

    row = conn.execute("SELECT superseded_by FROM facts WHERE id = ?", (old_id,)).fetchone()
    assert row["superseded_by"] == new_id  # ...but never deleted


def test_same_predicate_values_coexist_by_default(memory, conn):
    coffee_id = memory.save_fact("owner", "likes", "coffee")
    tea_id = memory.save_fact("owner", "likes", "tea")

    facts = memory.search_facts("what does the owner like coffee tea")
    assert {fact.object for fact in facts} >= {"coffee", "tea"}
    rows = conn.execute(
        "SELECT id, superseded_by FROM facts WHERE id IN (?, ?) ORDER BY id",
        (coffee_id, tea_id),
    ).fetchall()
    assert all(row["superseded_by"] is None for row in rows)


def test_exact_duplicate_save_is_idempotent(memory, conn):
    first_id = memory.save_fact("owner", "likes", "coffee")
    second_id = memory.save_fact("OWNER", "LIKES", "coffee")

    assert second_id == first_id
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM facts WHERE lower(subject) = 'owner' "
        "AND lower(predicate) = 'likes' AND object = 'coffee'"
    ).fetchone()
    assert row["count"] == 1


def test_replacement_with_existing_value_converges_duplicate_state(memory, conn):
    old_id = memory.save_fact("owner", "timezone", "UTC")
    wanted_id = memory.save_fact("owner", "timezone", "Asia/Kuala_Lumpur")

    returned = memory.save_fact(
        " Owner ",
        " timezone ",
        "asia/kuala_lumpur",
        replace_existing=True,
    )

    assert returned == wanted_id
    assert conn.execute(
        "SELECT superseded_by FROM facts WHERE id = ?", (old_id,)
    ).fetchone()["superseded_by"] == wanted_id
    current = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE superseded_by IS NULL"
    ).fetchone()[0]
    assert current == 1


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


def test_durable_conversation_is_searchable_before_consolidation(memory):
    memory.log_message("user", "The Kestrel launch should stay confidential")
    for index in range(30):
        memory.log_message("user", f"unrelated busy-day message {index}")

    rows = memory.search_messages("what did I say about Kestrel?")

    assert len(rows) == 1
    assert "stay confidential" in rows[0]["content"]


def test_core_block_is_deterministic(memory):
    memory.set_core("b_prefs", "likes tea")
    memory.set_core("a_identity", "name is Han")
    assert memory.core_block() == memory.core_block()
    # sorted by key regardless of insertion order
    assert memory.core_block().index("a_identity") < memory.core_block().index("b_prefs")


def test_core_block_has_a_hard_size_cap(memory):
    for index in range(10):
        memory.set_core(f"section_{index}", "x" * 10_000)

    block = memory.core_block()

    assert len(block) <= CORE_BLOCK_CHAR_LIMIT
    stored = memory.conn.execute(
        "SELECT MAX(length(content)) FROM core_memory"
    ).fetchone()[0]
    assert stored <= CORE_VALUE_CHAR_LIMIT


def test_people_upsert(memory, conn):
    pid1 = memory.save_person("Sarah", email="sarah@example.com")
    pid2 = memory.save_person("sarah", relationship="colleague")
    assert pid1 == pid2
    row = conn.execute("SELECT * FROM people WHERE id = ?", (pid1,)).fetchone()
    assert row["email"] == "sarah@example.com"
    assert row["relationship"] == "colleague"
