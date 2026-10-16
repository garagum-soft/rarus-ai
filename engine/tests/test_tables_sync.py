"""Table apps: a change in an outside source reaches the table when it syncs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from raven.tables.store import SOURCE_FIELD, Source, TableError, TableStore
from raven.tables.sync import TableSyncPass, due, sync_table

NOW = datetime(2026, 10, 13, 12, 0, 0)


def _csv(path: Path, rows: list[str]) -> None:
    path.write_text("\n".join(["id,name,stock", *rows]) + "\n", encoding="utf-8")


@pytest.fixture
def store(tmp_path: Path) -> TableStore:
    return TableStore(tmp_path / "tables")


async def test_a_change_in_the_source_reaches_the_table_on_sync(store: TableStore, tmp_path: Path) -> None:
    feed = tmp_path / "stock.csv"
    _csv(feed, ["1,bolt,10", "2,nut,5", "3,gear,0"])
    store.create("stock", "id", ["name", "stock"])
    store.add_source("stock", Source(name="warehouse", kind="csv", location=str(feed)))

    first = (await sync_table(store, "stock", now=NOW))[0]
    _csv(feed, ["1,bolt,7", "2,nut,5", "4,washer,40"])
    second = (await sync_table(store, "stock", now=NOW + timedelta(minutes=5)))[0]

    table = store.load("stock")
    assert first.added == ["1", "2", "3"]
    assert second.changed == ["1"] and second.added == ["4"] and second.removed == ["3"] and second.unchanged == 1
    assert table.row("1")["stock"] == "7" and table.row("3") is None and table.row("4")["name"] == "washer"
    assert table.source("warehouse").last_synced == (NOW + timedelta(minutes=5)).isoformat(timespec="seconds")


async def test_rows_entered_here_or_by_another_source_are_never_touched(store: TableStore, tmp_path: Path) -> None:
    feed = tmp_path / "a.csv"
    _csv(feed, ["1,bolt,10", "9,shim,1"])
    store.create("stock", "id")
    store.set_cell("stock", "9", "name", "my shim")
    store.add_source("stock", Source(name="a", kind="csv", location=str(feed)))

    result = (await sync_table(store, "stock", now=NOW))[0]

    assert result.conflicts == ["9"] and result.added == ["1"]
    assert store.load("stock").row("9") == {"id": "9", SOURCE_FIELD: None, "name": "my shim"}


async def test_a_local_edit_to_a_source_row_is_overwritten_and_reported(store: TableStore, tmp_path: Path) -> None:
    feed = tmp_path / "a.csv"
    _csv(feed, ["1,bolt,10"])
    store.create("stock", "id")
    store.add_source("stock", Source(name="a", kind="csv", location=str(feed)))
    await sync_table(store, "stock", now=NOW)
    store.set_cell("stock", "1", "stock", "999")
    store.set_cell("stock", "1", "note", "kept: not a source column")

    result = (await sync_table(store, "stock", now=NOW))[0]

    row = store.load("stock").row("1")
    assert result.changed == ["1"] and row["stock"] == "10" and row["note"] == "kept: not a source column"


async def test_json_sources_read_a_nested_list_and_map_fields_to_columns(store: TableStore, tmp_path: Path) -> None:
    feed = tmp_path / "items.json"
    feed.write_text(
        json.dumps({"data": {"items": [{"sku": "A1", "title": "Alpha"}, {"title": "no key"}]}}), encoding="utf-8"
    )
    store.create("catalog", "id")
    store.add_source(
        "catalog",
        Source(name="shop", kind="json", location=str(feed), path="data.items", mapping={"id": "sku", "name": "title"}),
    )

    result = (await sync_table(store, "catalog", now=NOW))[0]

    assert result.added == ["A1"] and result.skipped == 1
    assert store.load("catalog").row("A1") == {"id": "A1", "name": "Alpha", SOURCE_FIELD: "shop"}


async def test_an_http_source_is_read_over_the_network(store: TableStore) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    body = {"rows": [{"id": "1", "price": "3.50"}]}

    async def handler(request: web.Request) -> web.Response:
        return web.json_response(body)

    app = web.Application()
    app.router.add_get("/feed.json", handler)
    async with TestServer(app, host="127.0.0.1") as server:
        url = str(server.make_url("/feed.json"))
        store.create("prices", "id")
        store.add_source("prices", Source(name="api", kind="json", location=url, path="rows"))

        await sync_table(store, "prices", now=NOW)
        body["rows"][0]["price"] = "4.00"
        result = (await sync_table(store, "prices", now=NOW))[0]

    assert result.changed == ["1"] and store.load("prices").row("1")["price"] == "4.00"


async def test_a_source_that_cannot_be_read_leaves_the_table_as_it_was(store: TableStore, tmp_path: Path) -> None:
    feed = tmp_path / "a.csv"
    _csv(feed, ["1,bolt,10"])
    store.create("stock", "id")
    store.add_source("stock", Source(name="a", kind="csv", location=str(feed)))
    await sync_table(store, "stock", now=NOW)
    feed.unlink()

    result = (await sync_table(store, "stock", now=NOW))[0]

    table = store.load("stock")
    assert result.error and "cannot read" in result.error
    assert table.row("1")["stock"] == "10" and [e["error"] is None for e in table.log] == [True, False]


async def test_the_gateway_pass_syncs_only_sources_that_are_due(store: TableStore, tmp_path: Path) -> None:
    fast, slow = tmp_path / "fast.csv", tmp_path / "slow.csv"
    _csv(fast, ["1,a,1"])
    _csv(slow, ["2,b,2"])
    store.create("t", "id")
    store.add_source("t", Source(name="fast", kind="csv", location=str(fast), every=60))
    store.add_source("t", Source(name="slow", kind="csv", location=str(slow), every=3600))
    store.add_source("t", Source(name="manual", kind="csv", location=str(slow)))
    sync_pass = TableSyncPass(store=lambda: store)

    first = await sync_pass.run_once(NOW)
    _csv(fast, ["1,a,9"])
    second = await sync_pass.run_once(NOW + timedelta(seconds=90))

    assert sorted(r.source for r in first) == ["fast", "slow"]
    assert [r.source for r in second] == ["fast"] and second[0].changed == ["1"]
    assert due(store.load("t"), NOW + timedelta(hours=2)) == ["fast", "slow"]


def test_the_gateway_builds_the_pass_only_when_it_is_switched_on() -> None:
    from raven.config.schema import Config
    from raven.core.proactive_stack import build_table_sync

    off = Config()
    on = Config.model_validate({"gateway": {"tableSync": {"enabled": True, "intervalS": 30}}})

    assert build_table_sync(off) is None
    assert build_table_sync(on).interval_s == 30


def test_bad_names_and_kinds_are_refused(store: TableStore) -> None:
    with pytest.raises(TableError):
        store.create("../escape", "id")
    with pytest.raises(TableError):
        Source(name="x", kind="xml", location="f")
    store.create("t", "id")
    with pytest.raises(TableError):
        store.create("t", "id")
