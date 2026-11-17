from dataclasses import replace

import pytest

from retail_agent.auth import ALL_BRANDS, Principal
from retail_agent.errors import AgentError, ErrorCode
from retail_agent.models import Report
from retail_agent.reports import ReportStore


def actor(name="a", brands=frozenset({"Brand A"})):
    return Principal(name, frozenset({"reports:read", "reports:write", "reports:delete"}), brands)


@pytest.fixture
def store(tmp_path):
    value = ReportStore(tmp_path / "reports.sqlite3")
    yield value
    value.close()


def save(store, who=None, conversation="session", title="Client X report"):
    return store.save(
        who or actor(), conversation, Report(title=title, summary="Safe aggregate report.")
    )


def test_owner_and_brand_isolation_and_ceo_not_owner(store):
    save(store)
    save(store, actor("b"))
    assert len(store.list(actor())) == 1
    assert not store.list(actor(brands=frozenset({"Brand B"})))
    ceo = replace(actor("ceo"), scopes=actor().scopes | {ALL_BRANDS})
    assert not store.list(ceo)
    assert not store.preview_delete(ceo).reports


def test_preview_confirmation_exact_snapshot_and_replay(store):
    original = save(store)
    preview = store.preview_delete(actor(), mention="Client X")
    newer = save(store)
    with pytest.raises(AgentError):
        store.confirm_delete("yes", actor)
    assert len(store.list(actor())) == 2
    assert store.confirm_delete("DELETE " + preview.challenge, actor) == 1
    assert [r.id for r in store.list(actor())] == [newer.id]
    assert original.id != newer.id
    with pytest.raises(AgentError):
        store.confirm_delete("DELETE " + preview.challenge, actor)


def test_wrong_user_scope_and_expired_challenge(store):
    save(store)
    preview = store.preview_delete(actor())
    for bad_actor in [
        actor("b"),
        actor(brands=frozenset({"Brand A", "Brand B"})),
        replace(actor(), scopes=frozenset({"reports:read"})),
    ]:
        with pytest.raises(AgentError):
            store.confirm_delete("DELETE " + preview.challenge, lambda a=bad_actor: a)
    store.clock = lambda: preview.expires_at
    with pytest.raises(AgentError):
        store.confirm_delete("DELETE " + preview.challenge, actor)
    assert len(store.list(actor())) == 1


@pytest.mark.parametrize(
    "column,value",
    [
        ("body", "Changed body"),
        ("title", "Changed title"),
        ("digest", "wrong"),
        ("conversation_id", "different"),
        ("brands", '["Brand B"]'),
        ("all_brands", 1),
    ],
)
def test_stale_preview_rolls_back_all_deletions(store, column, value):
    save(store)
    modified = save(store)
    preview = store.preview_delete(actor())
    store.db.execute(f"UPDATE reports SET {column}=? WHERE id=?", (value, modified.id))
    with pytest.raises(AgentError):
        store.confirm_delete("DELETE " + preview.challenge, actor)
    assert store.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 2


def test_expired_jwt_checked_again_inside_transaction(store):
    save(store)
    preview = store.preview_delete(actor())

    def expired():
        raise AgentError(ErrorCode.AUTH)

    with pytest.raises(AgentError):
        store.confirm_delete("DELETE " + preview.challenge, expired)
    assert len(store.list(actor())) == 1


def test_conversation_filter_and_literal_search(store):
    save(store, conversation="one", title="Client X 100%")
    save(store, conversation="two", title="Client X 200")
    assert len(store.list(actor(), mention="%")) == 1
    assert not store.list(actor(), mention="' OR 1=1 --")
    preview = store.preview_delete(actor(), conversation_id="one")
    assert len(preview.reports) == 1
    store.confirm_delete("DELETE " + preview.challenge, actor)
    assert store.list(actor())[0].conversation_id == "two"


def test_persistence_and_hard_delete_has_no_body_copy(tmp_path):
    path = tmp_path / "reports.sqlite3"
    with_body = "UNIQUE_REPORT_BODY_76125"
    first = ReportStore(path)
    first.save(actor(), "session", Report(title="Unique", summary=with_body))
    first.close()
    second = ReportStore(path)
    preview = second.preview_delete(actor())
    assert with_body not in second.db.execute("SELECT snapshot FROM challenges").fetchone()[0]
    second.confirm_delete("DELETE " + preview.challenge, actor)
    second.close()
    assert with_body.encode() not in path.read_bytes()
    assert not path.with_name(path.name + "-wal").exists()


def test_oversized_preview_requires_narrow_selection(store):
    for i in range(101):
        save(store, title=f"Report {i}")
    with pytest.raises(AgentError) as error:
        store.preview_delete(actor())
    assert error.value.code == ErrorCode.BUDGET
    assert store.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 101


@pytest.mark.parametrize(
    "confirmation", ["DELETE x", "DELETE " + "x" * 101, "DELETE " + "x" * 20 + " "]
)
def test_malformed_confirmation_does_not_delete(store, confirmation):
    save(store)
    with pytest.raises(AgentError):
        store.confirm_delete(confirmation, actor)
    assert len(store.list(actor())) == 1


def test_change_of_policy_at_final_check_rolls_back(store):
    save(store)
    preview = store.preview_delete(actor())
    principals = iter([actor(), actor("changed")])
    with pytest.raises(AgentError):
        store.confirm_delete("DELETE " + preview.challenge, lambda: next(principals))
    assert len(store.list(actor())) == 1


def test_deleted_snapshot_record_invalidates_whole_confirmation(store):
    original = save(store)
    save(store)
    preview = store.preview_delete(actor())
    store.db.execute("DELETE FROM reports WHERE id=?", (original.id,))
    with pytest.raises(AgentError):
        store.confirm_delete("DELETE " + preview.challenge, actor)
    assert len(store.list(actor())) == 1


def test_database_failures_are_safe_and_future_schema_rejected(tmp_path, store):
    import sqlite3

    original = save(store)
    store.db.execute("DROP TABLE reports")
    for operation in [
        lambda: save(store),
        lambda: store.get(actor(), original.id),
        lambda: store.list(actor()),
    ]:
        with pytest.raises(AgentError) as caught:
            operation()
        assert caught.value.code == ErrorCode.STORAGE
    path = tmp_path / "future.sqlite3"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version=99")
    db.close()
    with pytest.raises(AgentError) as caught:
        ReportStore(path)
    assert caught.value.code == ErrorCode.STORAGE
