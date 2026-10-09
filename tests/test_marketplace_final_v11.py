"""Final marketplace subject and price-boundary regressions for v11."""

import copy
import json

from pylon.outbox import Outbox
from pylon.tasks.marketplace import classifier, enrich
from pylon.tasks.marketplace.store import Store


def _post(title, body):
    return {
        "source": "v2ex",
        "source_id": "final-v11-fixture",
        "title": title,
        "body": body,
        "body_complete": True,
        "history": 0,
        "url": "https://example.invalid/final-v11-fixture",
    }


def test_laptop_gpu_model_price_conflict_requires_analysis_and_blocks_partial(tmp_path):
    title = "出星舰8 4060笔记本"
    body = "16+1T，7800元"
    post = _post(title, body)
    local = classifier.classify_listing(title, body, "v2ex", True)
    assert len(local) == 1
    assert local[0]["category"] == "computer.laptop"
    assert local[0]["price"] == 7800

    # Exercise the historical failure shape explicitly: the model number was
    # treated as a GPU sale price without any price evidence.
    mistaken = copy.deepcopy(local)
    mistaken[0].update(category="component.gpu", evidence="4060", price=4060)
    mistaken[0]["attributes"].update(price_evidence="4060", price_status="identified")
    assert enrich.needs_analysis(post, mistaken)

    store = Store(Outbox(tmp_path / "queue.sqlite3"))
    with store.box.connect() as db:
        db.execute(
            "INSERT INTO marketplace_posts "
            "(source,source_id,url,title,body,body_complete,posted_at,first_seen,updated_at,items,status,history) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                post["source"],
                post["source_id"],
                post["url"],
                post["title"],
                post["body"],
                1,
                "",
                0,
                0,
                json.dumps(mistaken, ensure_ascii=False),
                "ready",
                0,
            ),
        )
        assert enrich.prepare(db, post, mistaken) is None
        assert db.execute(
            "SELECT status FROM marketplace_posts WHERE source=? AND source_id=?",
            (post["source"], post["source_id"]),
        ).fetchone()[0] == "pending_analysis"


def test_explicit_standalone_gpu_price_does_not_trigger_model_conflict():
    title = "出 RTX4060 独立显卡"
    body = "售价4060元，可邮寄。"
    items = classifier.classify_listing(title, body, "v2ex", True)

    assert len(items) == 1
    assert items[0]["category"] == "component.gpu"
    assert items[0]["price"] == 4060
    assert "4060元" in items[0]["attributes"]["price_evidence"]
    assert not enrich.needs_analysis(_post(title, body), items)


def test_known_and_unknown_sales_keep_their_own_subjects_and_prices():
    title = "两件闲置分别出售"
    body = (
        "1. 出 iPhone 15 Pro 512G，镜头膜一直贴着，售价4498元，可邮寄。\n"
        "2. 出自制旋纹摆件，售价80元，可邮寄。"
    )
    items = classifier.classify_listing(title, body, "v2ex", True)

    assert [(item["category"], item["price"]) for item in items] == [
        ("phone.smartphone", 4498),
        ("unknown", 80),
    ]
    assert "旋纹摆件" in items[1]["attributes"]["subject"]
    assert "旋纹摆件" not in items[0]["text"]
    assert "iPhone" not in items[1]["text"]

