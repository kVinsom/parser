from datetime import timedelta

import pytest
from sqlalchemy import func, select

from vpars_bot.application import Catalog
from vpars_bot.db import ControlCommand, RoleEvent, SearchRun, SearchVersion, utcnow
from vpars_bot.domain import Filters, Source, SourceRegistry, UserError
from vpars_bot.search_control import DevelopmentSearchControl, WorkToken


async def register(catalog, user_id, name, update_id):
    return await catalog.register(user_id, user_id, name, name, update_id, start=True)


async def token_for(sessions, user_id=101, source_id="vinted_fr"):
    async with sessions() as session:
        commands = (
            await session.scalars(
                select(ControlCommand)
                .where(ControlCommand.user_id == user_id, ControlCommand.kind == "start")
                .order_by(ControlCommand.created_at.desc())
            )
        ).all()
        return next(
            WorkToken(**c.payload["token"])
            for c in commands
            if c.payload["token"]["source_id"] == source_id
        )


async def test_save_idempotency_history_restart_and_paid_quota(services, db, registry):
    catalog, control, _ = services
    await register(catalog, 101, "alice", 1)
    filters = Filters(models=["Nike"], sources={"vinted": ["FR", "GB"]})
    one = await catalog.save(101, filters, "draft1")
    replay = await catalog.save(101, filters, "draft1")
    assert one.id == replay.id
    for i in range(9):
        await catalog.save(101, filters, f"add-{i}")
    with pytest.raises(UserError, match="до 10"):
        await catalog.save(101, filters, "too-many")
    assert await control.start(101, "start-all") == 10
    reopened = Catalog(db[1], registry)
    assert (await reopened.get(101, one.id)).filters == registry.validate(filters).model_dump(
        mode="json"
    )
    changed = await catalog.save(
        101,
        filters.model_copy(update={"models": ["Adidas"]}),
        "edit-1",
        search_id=one.id,
        version=one.version,
    )
    assert changed.version == 2
    with pytest.raises(UserError, match="змінилася"):
        await catalog.save(101, filters, "old-edit", search_id=one.id, version=1)
    async with db[1]() as session:
        history = (
            await session.scalars(
                select(SearchVersion)
                .where(SearchVersion.search_id == one.id)
                .order_by(SearchVersion.version)
            )
        ).all()
    assert [v.filters["models"] for v in history] == [["Nike"], ["Adidas"]]


async def test_stop_generation_lease_stale_ack_and_isolation(services, db):
    catalog, control, _ = services
    for uid, name in [(101, "alice"), (202, "bob")]:
        await register(catalog, uid, name, uid)
        await catalog.save(uid, Filters(models=["Nike"], sources={"vinted": ["FR"]}), "save")
        await control.start(uid, "start")
    a, b = await token_for(db[1]), await token_for(db[1], 202)
    assert not await control.can_deliver(a)
    assert await control.acknowledge(a)
    assert await control.acknowledge(b)
    assert await control.can_deliver(a)
    await control.start(101, "second-click")
    assert await token_for(db[1]) == a
    epoch = await control.stop(101, "stop")
    assert await control.stop(101, "stop") == epoch
    assert not await control.can_deliver(a)
    assert not await control.acknowledge(a)
    assert await control.can_deliver(b)
    await control.start(101, "new-start")
    new = await token_for(db[1])
    assert new.generation > a.generation and new.delivery_epoch > a.delivery_epoch
    assert not await control.can_deliver(a)
    assert await control.acknowledge(new)
    async with db[1].begin() as session:
        run = await session.get(SearchRun, (new.search_id, new.source_id))
        run.active_until = utcnow() - timedelta(seconds=1)
    assert not await control.can_deliver(new)
    assert (await control.status(101))[new.search_id] == "requested"


async def test_edit_and_archive_invalidate_work(services, db):
    catalog, control, _ = services
    await register(catalog, 101, "alice", 1)
    f = Filters(models=["Nike"], sources={"vinted": ["FR"]})
    item = await catalog.save(101, f, "save")
    await control.start(101, "start")
    old = await token_for(db[1])
    await control.acknowledge(old)
    updated = await catalog.save(101, f, "edit", search_id=item.id, version=1)
    assert not await control.acknowledge(old)
    assert not await control.can_deliver(old)
    await control.start(101, "again")
    current = await token_for(db[1])
    await catalog.archive(101, item.id, True, updated.version, "archive")
    await catalog.archive(101, item.id, True, updated.version, "archive")
    assert not await control.acknowledge(current)
    archived = await catalog.get(101, item.id)
    await catalog.archive(101, item.id, False, archived.version, "restore")
    assert (await control.status(101))[item.id] == "stopped"


async def test_unsupported_filters_refuse_save_and_legacy_still_refuses_start(services):
    catalog, control, _ = services
    await register(catalog, 101, "alice", 1)
    with pytest.raises(UserError, match="Колір"):
        await catalog.save(101, Filters(colors=["Black"], sources={"vinted": ["FR"]}), "bad-save")
    item = await catalog.save(101, Filters(sources={"vinted": ["FR"]}), "save")
    async with catalog.sessions.begin() as session:
        from vpars_bot.db import Search

        legacy = await session.get(Search, item.id)
        legacy.filters = {**legacy.filters, "colors": ["Black"]}
    with pytest.raises(UserError, match="Колір"):
        await control.start(101, "start")
    assert (await control.status(101))[item.id] == "stopped"


async def test_multiple_marketplaces_and_demo_do_not_claim_real_work(db):
    registry = SourceRegistry(
        {
            "vinted": Source(label="Vinted", regions=["GB", "FR"]),
            "future-source": Source(label="Test source", regions=["GB"]),
        }
    )
    catalog = Catalog(db[1], registry)
    await register(catalog, 101, "alice", 1)
    item = await catalog.save(
        101, Filters(sources={"vinted": ["FR"], "future-source": ["GB"]}), "save"
    )
    control = DevelopmentSearchControl(db[1], registry, app_env="development")
    await control.start(101, "demo")
    assert (await control.status(101))[item.id] == "demo"
    token = await token_for(db[1], source_id="vinted")
    assert not await control.acknowledge(token)
    assert not await control.can_deliver(token)
    with pytest.raises(ValueError):
        DevelopmentSearchControl(db[1], registry, app_env="production")


async def test_bootstrap_role_revocation_and_restart(services, db, profiles):
    catalog, control, admins = services
    await register(catalog, 101, "kV1nsom", 1)
    await register(catalog, 202, "bob", 2)
    profiles.names = {101: "kv1nsom", 202: "bob"}
    with pytest.raises(UserError):
        await admins.bootstrap(202)
    await admins.bootstrap(101)
    await admins.change(101, 202, "BOB", True, False, "grant")
    await admins.change(101, 202, "bob", True, False, "grant")
    item = await catalog.save(101, Filters(models=["Nike"], sources={"vinted": ["GB"]}), "save")
    await control.start(101, "start")
    before = await token_for(db[1], source_id="vinted_gb")
    await admins.change(202, 101, "@kV1nsom", False, True, "revoke")
    assert (await control.status(101))[item.id] == "requested"
    assert await token_for(db[1], source_id="vinted_gb") == before
    with pytest.raises(UserError):
        await admins.bootstrap(101)
    assert not (await Catalog(db[1], catalog.registry).user(101)).is_admin
    with pytest.raises(UserError, match="адміністратор"):
        await admins.change(101, 202, "bob", False, True, "old-button")
    with pytest.raises(UserError, match="останнього"):
        await admins.change(202, 202, "bob", False, True, "last")
    async with db[1]() as session:
        assert await session.scalar(select(func.count()).select_from(RoleEvent)) == 3


async def test_username_transfer_stale_updates_and_recipient_recheck(services, profiles):
    catalog, _, admins = services
    await register(catalog, 101, "kV1nsom", 1)
    await register(catalog, 202, "bob", 2)
    profiles.names = {101: "kv1nsom", 202: "bob"}
    await admins.bootstrap(101)
    assert (await admins.resolve(101, " @BOB ")).id == 202
    profiles.names[202] = "changed"
    with pytest.raises(UserError):
        await admins.change(101, 202, "bob", True, False, "stale")
    await catalog.register(202, 202, "changed", "Bob", 4)
    await catalog.register(202, 202, "bob", "Bob", 3)
    assert (await catalog.user(202)).username == "changed"
    await catalog.register(202, 202, None, "Bob", 5)
    assert (await catalog.user(202)).username is None
    assert not (await catalog.user(202)).is_admin
    with pytest.raises(UserError):
        await admins.resolve(101, "bob")


async def test_forged_username_text_does_not_bootstrap(chat, services):
    await chat.send("/start")
    await chat.send("@kV1nsom")
    assert not (await services[0].user(101)).is_admin
    assert "Додати адміністратора" not in str(chat.panel().reply_markup)


async def test_admin_handlers_and_stale_confirmation(chat, services, profiles):
    catalog, _, admins = services
    await chat.send("/start", username="kV1nsom")
    await chat.send("/start", user_id=202, username="bob")
    await chat.send("/start", user_id=303, username="charlie")
    profiles.names = {101: "kv1nsom", 202: "bob", 303: "charlie"}
    await admins.bootstrap(101)
    await chat.send("/start", username="kV1nsom")
    await chat.click("Додати адміністратора", username="kV1nsom")
    await chat.send("@bob", username="kV1nsom")
    assert "ID: <code>202" in chat.panel().text
    old = await chat.click("Підтвердити", username="kV1nsom")
    await chat.click(data=old, username="kV1nsom")
    assert (await catalog.user(202)).is_admin
    await chat.click("Додати адміністратора", username="kV1nsom")
    await chat.send("charlie", username="kV1nsom")
    confirmation = chat.button("Підтвердити")
    await admins.change(202, 101, "kv1nsom", False, True, "revoke")
    await chat.click(data=confirmation, username="kV1nsom")
    assert not (await catalog.user(303)).is_admin
