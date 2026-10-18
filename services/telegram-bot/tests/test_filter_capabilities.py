import pytest

from vpars_bot.db import Search
from vpars_bot.dialogs import new_draft, skip_unavailable, transition
from vpars_bot.domain import STEPS, Filters, UserError


@pytest.mark.parametrize("user,region,brand", [(101, "FR", "Amiri"), (202, "DE", "Stone Island")])
@pytest.mark.parametrize(
    "field,value,label",
    [
        ("sizes", "12 months", "Розмір"),
        ("colors", "Black", "Колір"),
        ("categories", "Jeans", "Категорія"),
    ],
)
async def test_save_rejects_unverifiable_filter_without_mutating_legacy(
    services, user, region, brand, field, value, label
):
    catalog, control, _ = services
    await catalog.register(user, user, f"user{user}", "Test", 1, start=True)
    values = {"sources": {"vinted": [region]}, "brands": [brand]}
    item = await catalog.save(user, Filters(**values), "valid")
    with pytest.raises(UserError, match=label):
        await catalog.save(
            user, Filters(**values, **{field: [value]}), "bad", search_id=item.id, version=1
        )
    assert (await catalog.get(user, item.id)).version == 1
    async with catalog.sessions.begin() as session:
        saved = await session.get(Search, item.id)
        saved.filters = {**saved.filters, field: [value]}
    legacy = (await catalog.get(user, item.id)).filters
    with pytest.raises(UserError, match=label):
        await control.start(user, "start")
    assert (await catalog.get(user, item.id)).filters == legacy


def test_unavailable_steps_hidden_without_erasing_saved_values(registry):
    draft = new_draft({"sources": {"vinted": ["FR"]}})
    draft["step"] = STEPS.index("colors")
    assert STEPS[skip_unavailable(draft, registry)["step"]] == "brands"
    draft["values"]["colors"] = ["Black"]
    assert skip_unavailable(draft, registry) == draft
    cleared = transition(draft, "skip", registry)
    assert "colors" not in cleared["values"]
    assert draft["values"]["colors"] == ["Black"]
    assert STEPS[transition(cleared, "back", registry)["step"]] == "conditions"
