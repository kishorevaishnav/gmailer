"""Queue builder: fetches unread batch, bundles by sender, orders for triage."""

from __future__ import annotations

from collections import defaultdict

from . import config
from .gmail_service import get_metadata_batch, list_unread_page


def _bundle_key(item: dict) -> str:
    email = (item.get("sender_email") or "").strip().lower()
    if email:
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if domain and ("lists." in domain or domain.startswith("list.") or ".lists." in domain):
            return domain
        return email
    name = (item.get("sender_name") or "").strip().lower()
    return name or "unknown"


def bundle_items(metas: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group flat metadata into (items, bundles_summary) with the queue shape.

    Shared by the fresh Gmail queue and the DB-only cached-first queue so both
    produce byte-identical item shapes.
    """
    if not metas:
        return [], []

    groups: dict[str, list[dict]] = defaultdict(list)
    for m in metas:
        groups[_bundle_key(m)].append(m)

    items: list[dict] = []
    bundles_summary: list[dict] = []
    for key, members in groups.items():
        members.sort(key=lambda m: m.get("internal_date_ms", 0), reverse=True)
        count = len(members)
        name = members[0].get("sender_name") or key
        if count > 1:
            bundles_summary.append(
                {"bundle_key": key, "sender_name": name, "count": count}
            )
        for m in members:
            m["bundle_key"] = key
            m["bundle_count"] = count
            m["bundle_sender_name"] = name
            m["in_bundle"] = count > 1
            items.append(m)

    items.sort(
        key=lambda i: (
            0 if i["bundle_count"] > 1 else 1,
            -i["bundle_count"] if i["bundle_count"] > 1 else 0,
            -i.get("internal_date_ms", 0),
        )
    )
    bundles_summary.sort(key=lambda b: b["count"], reverse=True)
    return items, bundles_summary


def build_queue(client, max_results: int = config.DEFAULT_BATCH, page_token: str | None = None):
    """Return (items, bundles_summary, next_page_token).

    Ordering strategy:
      - Multi-message senders (bundles) first, biggest bundle first —
        so the user can wipe whole mailing lists in one click.
      - Singles after, newest first.
    """
    message_ids, next_page_token = list_unread_page(client, max_results, page_token)
    metas = get_metadata_batch(client, message_ids)
    if not metas:
        return [], [], next_page_token
    items, bundles_summary = bundle_items(metas)
    return items, bundles_summary, next_page_token