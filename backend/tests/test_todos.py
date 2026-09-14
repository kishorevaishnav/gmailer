from backend import gtasks, store


def _item(mid, subject="Subject here", sender="boss@example.com", name="Boss"):
    return {
        "id": mid,
        "sender_name": name,
        "sender_email": sender,
        "subject": subject,
        "promo": False,
        "category": "Personal",
    }


def test_todo_add_and_list_order():
    store.add_todo(_item("m1"), "2026-09-20")
    store.add_todo(_item("m2"))
    items = store.list_todos()
    assert [(t["id"], t["position"]) for t in items] == [("m1", 1), ("m2", 2)]
    assert items[0]["due_date"] == "2026-09-20"
    assert items[1]["due_date"] is None
    assert items[0]["subject"] == "Subject here"
    assert store.todo_id_set() == {"m1", "m2"}


def test_todo_add_requires_id():
    assert store.add_todo({"subject": "no id"}) is None
    assert store.list_todos() == []


def test_todo_add_duplicate_keeps_position():
    store.add_todo(_item("m1"), "2026-09-20")
    store.add_todo(_item("m2"))
    row = store.add_todo(_item("m1"), "2026-09-25")
    assert row["position"] == 1
    assert row["due_date"] == "2026-09-25"
    assert len(store.list_todos()) == 2


def test_todo_update_due_and_note():
    store.add_todo(_item("m1"))
    row = store.update_todo("m1", due_date="2026-10-01", due_date_set=True, note="call first")
    assert row["due_date"] == "2026-10-01"
    assert row["note"] == "call first"
    row = store.update_todo("m1", due_date="", due_date_set=True)
    assert row["due_date"] is None
    assert store.update_todo("missing", due_date="2026-10-01", due_date_set=True) is None


def test_todo_move_up_down_with_bounds():
    store.add_todo(_item("m1"))
    store.add_todo(_item("m2"))
    store.add_todo(_item("m3"))
    store.move_todo("m3", -1)
    assert [t["id"] for t in store.list_todos()] == ["m1", "m3", "m2"]
    store.move_todo("m3", 1)
    assert [t["id"] for t in store.list_todos()] == ["m1", "m2", "m3"]
    store.move_todo("m1", -1)
    store.move_todo("m3", 1)
    assert [t["id"] for t in store.list_todos()] == ["m1", "m2", "m3"]
    store.move_todo("nope", -1)


def test_todo_reorder():
    store.add_todo(_item("m1"))
    store.add_todo(_item("m2"))
    store.add_todo(_item("m3"))
    store.reorder_todos(["m3", "m1"])
    assert [t["id"] for t in store.list_todos()] == ["m3", "m1", "m2"]
    assert [t["position"] for t in store.list_todos()] == [1, 2, 3]


def test_todo_remove():
    store.add_todo(_item("m1"))
    store.add_todo(_item("m2"))
    store.remove_todo("m1")
    assert [t["id"] for t in store.list_todos()] == ["m2"]
    assert store.get_todo("m1") is None
    assert store.todo_id_set() == {"m2"}
    store.remove_todo("m2")
    assert store.list_todos() == []


def test_gtask_payload_with_due():
    payload = gtasks.build_task_payload(_item("m1"), "2026-09-20")
    assert payload["title"] == "Subject here"
    assert payload["due"] == "2026-09-20T12:00:00.000Z"
    assert "boss@example.com" in payload["notes"]
    assert payload["notes"].endswith("/#inbox/m1")


def test_gtask_payload_without_due():
    payload = gtasks.build_task_payload(_item("m1"))
    assert "due" not in payload
    assert payload["title"] == "Subject here"


def test_gtask_payload_missing_subject():
    payload = gtasks.build_task_payload({"id": "m9"})
    assert payload["title"] == "(no subject)"
    assert payload["notes"].endswith("/#inbox/m9")


def test_gtask_due_format_noon_utc():
    assert gtasks.due_rfc3339("2026-09-20") == "2026-09-20T12:00:00.000Z"
