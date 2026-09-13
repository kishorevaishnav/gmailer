from backend import store
from main import _trace_item_for_message


def test_trace_item_from_cache():
    store.save_message({"id": "m1", "sender_email": "a@b.com", "sender_name": "A",
                        "subject": "hi", "promo": 1, "category": "Offer/Deal",
                        "label_ids": ["CATEGORY_PROMOTIONS"]})
    it = _trace_item_for_message(None, "m1")
    assert it["sender_email"] == "a@b.com"
    assert it["promo"] is True
    assert it["category"] == "Offer/Deal"