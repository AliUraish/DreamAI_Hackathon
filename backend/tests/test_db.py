"""The mirror is a cache, not the store: what was written must come back from the database after a restart."""


def test_writes_survive_a_restart(tmp_path, monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    db.reset_connection()

    db.insert("repos", {"id": "repo_a", "name": "alpha", "local_path": "/a", "validation_commands": [{"name": "tests", "cmd": "npm test"}], "created_at": db.now()})
    db.insert("repos", {"id": "repo_b", "name": "beta", "local_path": "/b", "created_at": db.now()})
    db.update("repos", "repo_a", {"default_branch": "main"})
    db.delete("repos", {"id": "repo_b"})
    for message in ("one", "two", "three"):
        db.insert("events", {"id": db.new_id("evt"), "repo_id": "repo_a", "ts": db.now(), "type": "x", "message": message, "data": {"n": message}})
    assert db.get("repos", "repo_a")["default_branch"] == "main"     # visible at once, before the writer has caught up

    db.reset_connection()                                              # flushes, closes, forgets
    assert [r["id"] for r in db.select("repos")] == ["repo_a"]         # reloaded from the database
    assert db.get("repos", "repo_a")["validation_commands"] == [{"name": "tests", "cmd": "npm test"}]
    events = db.events_after(0, {"repo_id": "repo_a"})
    assert [e["message"] for e in events] == ["one", "two", "three"] and events[0]["data"] == {"n": "one"}
    db.insert("events", {"id": db.new_id("evt"), "repo_id": "repo_a", "ts": db.now(), "type": "x", "message": "four", "data": {}})
    assert db.events_after(events[-1]["seq"])[0]["message"] == "four"  # sequence numbers continue after a reload
    db.reset_connection()
