import pytest

from vpars_bot.domain import Filters, conflict_notice, keyword_conflicts
from vpars_bot.formatting import filter_summary
from vpars_bot.keyword_dialog import render_draft


async def test_many_conflicts_keep_status_diagnostics_bounded(services):
    from vpars_bot.db import Search

    catalog, control, _ = services
    await catalog.register(101, 101, "person", "Test", 1, start=True)
    filters = Filters(keywords=["Fake"], excluded_keywords=["Fake"], sources={"vinted_fr": ["FR"]})
    async with catalog.sessions.begin() as session:
        session.add_all(
            Search(id=f"conflict-{number}", user_id=101, filters=filters.model_dump(mode="json"))
            for number in range(12)
        )
    assert len(await control.problems(101)) == 6


@pytest.mark.parametrize("word", ["Fake", "Vintage", "STONE island"])
@pytest.mark.parametrize("grouped", [False, True])
def test_conflict_notice_works_for_old_and_new_filters_without_rewriting(registry, word, grouped):
    filters = Filters(
        keywords=[word],
        keywords_all=grouped,
        excluded_keywords=[word.lower(), "hole"],
        sources={"vinted_fr": ["FR"]},
    )
    saved = filters.model_dump(mode="json")
    assert keyword_conflicts(filters) == [word]
    assert "Є і в пошуку" in filter_summary(filters, registry)
    assert word in conflict_notice(saved)
    assert filters.model_dump(mode="json") == saved


def test_whole_phrase_warning_does_not_confuse_substrings():
    assert not keyword_conflicts(
        {"keywords": ["Vintage", "Kidswear"], "excluded_keywords": ["age", "kids"]}
    )
    assert keyword_conflicts({"keywords": ["OFF-DYE"], "excluded_keywords": ["Off Dye"]}) == [
        "OFF-DYE"
    ]


def test_new_draft_warns_before_saving_with_escaped_user_text(registry):
    draft = {"keywords": ["<Vintage>"], "excluded_keywords": ["Vintage"], "step": "price"}
    prompt, _ = render_draft(draft, registry)
    assert "Є і в пошуку" in prompt and "&lt;Vintage&gt;" in prompt


@pytest.mark.parametrize("user,word", [(101, "Fake"), (202, "Vintage")])
async def test_saved_conflicts_appear_without_source_error_and_do_not_change_other_users(
    services, user, word
):
    catalog, control, _ = services
    await catalog.register(user, user, "person", "Test", 1, start=True)
    saved = await catalog.save(
        user,
        Filters(
            keywords=[word],
            keywords_all=True,
            excluded_keywords=[word],
            sources={"vinted_fr": ["FR"]},
        ),
        "save",
    )
    assert any("Є і в пошуку" in problem for problem in await control.problems(user))
    assert not await control.problems(303)
    assert (await catalog.get(user, saved.id)).filters == saved.filters
