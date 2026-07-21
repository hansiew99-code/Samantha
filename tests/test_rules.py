"""Rules engine: deterministic filtering, persistence, core-memory echo."""

from samantha.db import connect
from samantha.memory import Memory
from samantha.rules import RulesEngine


def test_suppress_all_from_source(conn, memory):
    rules = RulesEngine(conn, memory)
    rules.add("clickup", "suppress")
    assert rules.allows("clickup", "anything") is False
    assert rules.allows("gmail", "anything") is True


def test_suppress_scoped(conn, memory):
    rules = RulesEngine(conn, memory)
    rules.add("slack", "suppress", scope="general")
    assert rules.allows("slack", "C-general-channel") is False
    assert rules.allows("slack", "C-urgent") is True


def test_deactivate_restores_flow(conn, memory):
    rules = RulesEngine(conn, memory)
    rid = rules.add("clickup", "suppress")
    assert rules.allows("clickup", "x") is False
    assert rules.deactivate(rid) is True
    assert rules.allows("clickup", "x") is True
    # Deactivated, never deleted.
    row = conn.execute("SELECT active FROM rules WHERE id = ?", (rid,)).fetchone()
    assert row["active"] == 0


def test_vip_matching(conn, memory):
    rules = RulesEngine(conn, memory)
    rules.add("gmail", "vip", scope="boss@corp.com")
    assert rules.is_vip("gmail", "Big Boss <boss@corp.com>") is True
    assert rules.is_vip("gmail", "random@corp.com") is False


def test_rules_echoed_into_core_memory(conn, memory):
    rules = RulesEngine(conn, memory)
    rules.add("clickup", "suppress", detail="owner asked 2026-07-21")
    assert "suppress clickup" in memory.core_block()
    # ...so she *knows* her standing orders (BRIEF §7).


def test_rules_survive_restart(tmp_path):
    db_path = tmp_path / "restart.db"
    conn1 = connect(db_path)
    RulesEngine(conn1, Memory(conn1)).add("clickup", "suppress")
    conn1.close()

    conn2 = connect(db_path)  # fresh process, same disk
    rules2 = RulesEngine(conn2, Memory(conn2))
    assert rules2.allows("clickup", "whatever") is False
    conn2.close()
