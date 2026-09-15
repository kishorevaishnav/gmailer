from backend import store


def test_remove_messages_deletes_only_given_ids():
    store.save_message({"id": "m1", "sender_email": "a@x.com", "subject": "s", "internal_date_ms": 1})
    store.save_message({"id": "m2", "sender_email": "b@x.com", "subject": "s", "internal_date_ms": 2})
    store.save_message({"id": "m3", "sender_email": "c@x.com", "subject": "s", "internal_date_ms": 3})

    removed = store.remove_messages(["m1", "m3"])

    assert removed == 2
    ids = [m["id"] for m in store.list_messages()]
    assert ids == ["m2"]


def test_remove_messages_missing_ids_ignored():
    store.save_message({"id": "m1", "sender_email": "a@x.com", "subject": "s", "internal_date_ms": 1})
    assert store.remove_messages(["ghost", "m1"]) == 1
    assert store.list_messages() == []


def test_remove_messages_empty_is_noop():
    assert store.remove_messages([]) == 0
