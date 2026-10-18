from types import SimpleNamespace

import pytest
from test_monitoring import Bot, Parser, due, lot, rows
from vpars_bot.domain import Filters
from vpars_notifier.service import Notifier

from vpars_worker.db import Delivery, Job, SentLot
from vpars_worker.matching import excluded_match
from vpars_worker.monitor import Monitor


@pytest.mark.parametrize(
    "title,description,excluded,expected",
    [
        ("Nike FAKE hoodie", "", ["fake"], True),
        ("Nike hoodie", "Small hole on sleeve", ["hole"], True),
        ("Nike hoodie", "Junior, kids", ["kids"], True),
        ("Nike hoodie", "ＤＡＭＡＧＥＤ sleeve", ["damaged"], True),
        ("Nike hoodie", "Small-hole on sleeve", ["small hole"], True),
        ("Nike hoodie", "Small stain, no hole", ["small hole"], False),
        ("Nike hoodie", "Wholesale", ["hole"], False),
        ("Nike hoodie", "Excellent", [], False),
        ("Nike hoodie", None, ["fake"], False),
    ],
)
def test_exclusions_match_any_whole_word_or_phrase(title, description, excluded, expected):
    assert excluded_match(lot(title=title, description=description), excluded) == expected


async def setup_filter(environment, excluded, key="filter"):
    store, catalog = environment
    await catalog.save(
        101,
        Filters(
            keywords=["Stone Island"],
            keywords_all=True,
            excluded_keywords=excluded,
            sources={"vinted_fr": ["FR"]},
        ),
        key,
    )
    await store.control.start(101, "start-" + key)
    await Monitor(store, Parser(items=[lot("999", published_at=None)])).once(region="FR")
    await due(store, Job)


async def test_excluded_title_is_not_queued_but_good_new_item_is_sent(environment):
    store, _ = environment
    await setup_filter(environment, ["fake", "kids"])
    await Monitor(
        store,
        Parser(
            items=[
                lot("10001", title="Stone Island fake hoodie"),
                lot("10002", description="Excellent condition"),
            ]
        ),
    ).once(region="FR")
    assert [row.item["source_item_id"] for row in await rows(store, Delivery)] == ["10002"]
    bot = Bot()
    await Notifier(store, bot).once()
    assert len(bot.calls) == 1
    assert len(await rows(store, SentLot)) == 1


@pytest.mark.parametrize(
    "description,expected",
    [("small hole", "excluded"), ("Good condition", "sent"), ("", "sent"), (None, "excluded")],
)
async def test_description_is_loaded_and_checked_before_delivery(
    environment, description, expected
):
    store, _ = environment
    await setup_filter(environment, ["hole"])
    await Monitor(store, Parser(items=[lot()])).once(region="FR")

    class Details:
        async def details(self, item):
            return SimpleNamespace(
                photo_urls=item.photo_urls,
                photos_complete=True,
                published_at=None,
                description=description,
            )

    bot = Bot()
    notifier = Notifier(store, bot, parser=Details())
    await notifier.once()
    assert not bot.calls
    await notifier.once()
    assert (await rows(store, Delivery))[0].state == expected
    assert len(bot.calls) == (1 if expected == "sent" else 0)


async def test_exclusions_are_per_filter_and_do_not_block_another_matching_filter(environment):
    store, _ = environment
    await setup_filter(environment, ["hole"])
    await setup_filter(environment, [], key="unrestricted")
    await Monitor(store, Parser(items=[lot(description="small hole")])).once(region="FR")
    bot = Bot()
    await Notifier(store, bot).once()
    assert len(bot.calls) == 1
    assert len((await rows(store, Delivery))[0].candidates) == 1
