"""Spreadsheet bots: what a page's save hands them, the rows their slices pass, and the message a run starts with."""
from __future__ import annotations

import html
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal, override

import pytest

from agent.api.core.agent_schedules import AgentScheduler, Cadence
from agent.api.core.grid import Grid, GridBot, GridCell, column_types, normalize_grid, row_matches, shown_value
from agent.api.core.grid_bots import (
    PageSheet, bot_prompt, bot_rows, changes, event_bots, held_rows, page_sheets, rows_table, saved, start_due,
)

GRID: Grid = {
    "title": "Outings",
    "sheets": [{
        "name": "Plans",
        "formats": {"C": "$#,##0.00", "D": "yyyy-mm-dd"},
        "rows": [
            ["Name", "Kind", "Cost", "Date", "With tax"],
            ["Delta Center", "Show", 85, 46350, "=C2*1.1"],
            ["Liberty Park", "Park", 0, 46351, "=C3*1.1"],
            ["Ballet West", "Show", 60, 46353, "=C4*1.1"],
        ],
        "slices": [{"name": "Shows", "where": [{"column": "Kind", "op": "=", "value": "Show"}]},
                   {"name": "Dear", "where": [{"column": "With tax", "op": ">", "value": 70}]}],
        "bots": [{"name": "New plan", "on": "added", "ask": "Find tickets for it."},
                 {"name": "Show changed", "on": "changed", "slice": "Shows", "ask": "Tell me what changed."},
                 {"name": "Morning", "schedule": {"every": "day", "time": "08:00", "timezone": "America/Denver"},
                  "ask": "Tell me what's this week."}],
    }],
}

SHOWN = [
    ["Name", "Kind", "Cost", "Date", "With tax"],
    ["Delta Center", "Show", "$85.00", "2026-11-24", "93.5"],
    ["Liberty Park", "Park", "$0.00", "2026-11-25", "0"],
    ["Ballet West", "Show", "$60.00", "2026-11-27", "66"],
]


def block(grid: Grid, shown: list[list[str]] | None) -> str:
    """The editor's grid block as a page saves it (GridBlock.tsx's renderHTML)."""
    tables = "".join(
        f"<table><caption>{html.escape(sheet['name'])}</caption><tbody>"
        + "".join("<tr>" + "".join(f"<{'th' if r == 0 else 'td'}>{html.escape(cell)}</{'th' if r == 0 else 'td'}>" for cell in row) + "</tr>"
                  for r, row in enumerate(shown or []))
        + "</tbody></table>"
        for sheet in grid["sheets"])
    calculated = " data-calculated=\"\"" if shown is not None else ""
    return f'<p>Plans</p><grid-block data-grid="{html.escape(json.dumps(grid), quote=True)}"{calculated}>{tables}</grid-block>'


def with_rows(grid: Grid, rows: list[list[GridCell]]) -> Grid:
    sheet = {**grid["sheets"][0], "rows": rows}
    normalized = normalize_grid({**grid, "sheets": [sheet]})
    assert normalized is not None
    return normalized


def test_normalize_keeps_bots_with_an_event_or_a_valid_schedule_and_drops_the_rest():
    grid = normalize_grid({"title": "t", "sheets": [{"name": "S", "rows": [["A"]], "bots": [
        {"name": "Adds", "on": "added", "ask": "go", "slice": "Hot"},
        {"name": "adds", "on": "changed", "ask": "same name"},
        {"name": "No ask", "on": "added", "ask": " "},
        {"name": "Bad event", "on": "deleted", "ask": "go"},
        {"name": "Daily", "schedule": {"every": "day", "time": "07:30", "cron": "*"}, "ask": "go"},
        {"name": "Bad schedule", "schedule": {"every": "month"}, "ask": "go"},
    ]}]})
    assert grid is not None
    assert grid["sheets"][0].get("bots") == [
        {"name": "Adds", "ask": "go", "on": "added", "slice": "Hot"},
        {"name": "Daily", "ask": "go", "schedule": {"every": "day", "hours": 24, "time": "07:30", "weekday": 0, "timezone": "UTC"}},
    ]


def test_a_page_hands_over_each_sheet_with_what_its_cells_show():
    [sheet] = page_sheets(block(GRID, SHOWN))
    assert (sheet.title, sheet.sheet["name"], sheet.shown) == ("Outings", "Plans", SHOWN)
    # Before a spreadsheet has calculated it, a cell shows as written.
    [plain] = page_sheets(block(GRID, None))
    assert plain.shown[1] == ["Delta Center", "Show", "85", "46350", "=C2*1.1"]
    assert page_sheets("<p>No grid</p><grid-block data-grid=\"nope\"></grid-block>") == []


def test_a_formula_is_tested_by_the_result_its_cell_shows():
    assert [shown_value(text) for text in ["$1,200.50", "(12)", "45%", "TRUE", "", "11/5/2026", "Park"]] == [
        1200.5, -12, 0.45, True, None, "2026-11-05", "Park"]
    page = page_sheets(block(GRID, SHOWN))[0]
    held = held_rows(page)
    assert held[1] == ["Delta Center", "Show", 85, 46350, 93.5]
    assert column_types(page.sheet, held) == [("Name", "text"), ("Kind", "text"), ("Cost", "number"), ("Date", "date"),
                                              ("With tax", "number")]
    assert bot_rows(page, {"name": "b", "ask": "a", "slice": "Dear"}) == [1]
    assert bot_rows(page, {"name": "b", "ask": "a", "slice": "Shows"}) == [1, 3]
    assert bot_rows(page, {"name": "b", "ask": "a", "slice": "Gone"}) == []
    assert bot_rows(page, {"name": "b", "ask": "a"}) == [1, 2, 3]


def test_tests_compare_as_the_editor_does():
    columns = [("Name", "text"), ("Date", "date"), ("Done", "yesno"), ("Cost", "number")]
    row: list[str | int | float | bool | None] = ["Café Rio", 46350, True, 12]
    shown = ["Café Rio", "2026-11-24", "TRUE", "$12.00"]
    assert row_matches([{"column": "name", "op": "contains", "value": "CAFE"}], columns, row, shown)
    assert row_matches([{"column": "Date", "op": "<", "value": "2026-11-25"}], columns, row, shown)
    assert row_matches([{"column": "Done", "op": "=", "value": "yes"}], columns, row, shown)
    assert row_matches([{"column": "Cost", "op": ">=", "value": "12"}], columns, row, shown)
    assert not row_matches([{"column": "Missing", "op": "filled"}], columns, row, shown)
    # An empty cell is only ever not the value tested.
    assert row_matches([{"column": "Cost", "op": "!=", "value": 5}], columns, ["", None, None, None], ["", "", "", ""])
    assert not row_matches([{"column": "Cost", "op": "=", "value": 5}], columns, ["", None, None, None], ["", "", "", ""])


def test_a_save_tells_added_rows_from_changed_ones_by_their_first_cell():
    before = GRID["sheets"][0]["rows"]
    after = [before[0], ["Delta Center", "Show", 95, 46350, "=C2*1.1"], before[2], before[3], ["Red Butte", "Park", 14, 46352, "=C5*1.1"]]
    assert changes(before, after) == ([4], [1])
    # Rows that only moved, and a duplicate kept, are neither.
    assert changes(before, [before[0], before[3], before[1], before[2]]) == ([], [])
    assert changes([before[0], before[1]], [before[0], before[1], before[1]]) == ([2], [])
    # A row with an empty first cell is added, never changed.
    assert changes([["A"], ["", 1]], [["A"], ["", 2]]) == ([1], [])


def test_each_bot_gets_the_rows_its_event_and_slice_meet():
    after_grid = with_rows(GRID, [*GRID["sheets"][0]["rows"][:1], ["Delta Center", "Show", 95, 46350, "=C2*1.1"],
                                  ["Liberty Park", "Park", 5, 46351, "=C3*1.1"], GRID["sheets"][0]["rows"][3],
                                  ["Red Butte", "Park", 14, 46352, "=C5*1.1"]])
    after_shown = [SHOWN[0], ["Delta Center", "Show", "$95.00", "2026-11-24", "104.5"],
                   ["Liberty Park", "Park", "$5.00", "2026-11-25", "5.5"], SHOWN[3],
                   ["Red Butte", "Park", "$14.00", "2026-11-26", "15.4"]]
    fired = event_bots(page_sheets(block(GRID, SHOWN)), page_sheets(block(after_grid, after_shown)))
    assert [(bot["name"], rows, what) for _, bot, rows, what in fired] == [
        ("New plan", [4], "Rows added"),
        # Liberty Park changed too, but it isn't a show.
        ("Show changed", [1], "Rows changed"),
    ]
    # A sheet the page didn't have before starts nothing.
    assert event_bots([], page_sheets(block(after_grid, after_shown))) == []


def test_the_run_message_names_the_bot_and_shows_the_rows():
    page = PageSheet("Outings", GRID["sheets"][0], SHOWN)
    message = bot_prompt(page, {"name": "Show changed", "on": "changed", "slice": "Shows", "ask": "Tell me what changed."},
                         [1, 3], why="by the bot \"Show changed\" when rows were saved", what="Rows changed")
    assert message.splitlines()[0] == "Show changed · Outings"
    assert "Tell me what changed.\n\nRows changed on the sheet \"Plans\" of the spreadsheet \"Outings\", in its slice \"Shows\":" in message
    assert "| Name | Kind | Cost | Date | With tax |\n| --- | --- | --- | --- | --- |\n| Delta Center | Show | $85.00 | 2026-11-24 | 93.5 |\n| Ballet West |" in message
    assert "by the bot \"Show changed\" when rows were saved" in message
    many = rows_table(PageSheet("t", {"name": "S", "rows": [["A"], *[[n] for n in range(250)]]}, [["A"]] + [[str(n)] for n in range(250)]),
                      list(range(1, 251)))
    assert many.count("\n| ") == 201 and many.endswith("(The first 200 of 250 rows.)")


Tables = dict[str, list[dict[str, Any]]]


class Query:
    """The part of supabase-py's query builder the bots use, over rows kept in a dict."""

    def __init__(self, tables: Tables, name: str) -> None:
        super().__init__()
        self.rows = tables.setdefault(name, [])
        self.name = name
        self.action: tuple[str, dict[str, Any]] = ("select", {})
        self.tests: list[Callable[[dict[str, Any]], bool]] = []

    def select(self, columns: str) -> Query:
        return self

    def insert(self, values: dict[str, Any]) -> Query:
        self.action = ("insert", values)
        return self

    def update(self, values: dict[str, Any]) -> Query:
        self.action = ("update", values)
        return self

    def delete(self) -> Query:
        self.action = ("delete", {})
        return self

    def eq(self, key: str, value: object) -> Query:
        self.tests.append(lambda row: row.get(key) == value)
        return self

    def lte(self, key: str, value: str) -> Query:
        self.tests.append(lambda row: row.get(key) is not None
                          and datetime.fromisoformat(row[key]) <= datetime.fromisoformat(value))
        return self

    def order(self, key: str) -> Query:
        return self

    def limit(self, count: int) -> Query:
        return self

    async def execute(self) -> SimpleNamespace:
        kind, values = self.action
        if kind == "insert":
            self.rows.append({"id": f"{self.name}-{len(self.rows) + 1}", **values})
            return SimpleNamespace(data=[self.rows[-1]])
        hits = [row for row in self.rows if all(test(row) for test in self.tests)]
        for row in hits:
            if kind == "update":
                row.update(values)
            elif kind == "delete":
                self.rows.remove(row)
        return SimpleNamespace(data=hits)


class Scheduler(AgentScheduler):
    """The scheduler, as its own Supabase client over tables kept in a dict, recording the runs it is asked to start."""

    def __init__(self, tables: Tables) -> None:
        super().__init__(None)
        self.tables = tables
        self.started: list[tuple[str, str | None, str, str]] = []
        self.prompts: list[str] = []

    @override
    async def client(self) -> Any:
        return self

    def table(self, name: str) -> Query:
        return Query(self.tables, name)

    @override
    async def run(self, owner: str, agent_id: str | None, *, task: str = "", cadence: Cadence | None = None,
                  trigger: Literal["schedule", "manual", "bot"] = "manual", bot: str = "",
                  prompt: str = "") -> dict[str, Any]:
        self.started.append((owner, agent_id, bot, trigger))
        self.prompts.append(prompt)
        return {"id": f"run-{len(self.started)}"}


def with_bots(grid: Grid, bots: list[GridBot]) -> Grid:
    normalized = normalize_grid({**grid, "sheets": [{**grid["sheets"][0], "bots": bots}]})
    assert normalized is not None
    return normalized


MORNING: GridBot = {"name": "Morning", "schedule": {"every": "day", "time": "08:00", "timezone": "America/Denver"},
                    "ask": "Tell me what's this week."}
AFTER: list[list[GridCell]] = [["Name", "Kind", "Cost", "Date", "With tax"], ["Delta Center", "Show", 95, 46350, "=C2*1.1"],
         ["Liberty Park", "Park", 0, 46351, "=C3*1.1"], ["Ballet West", "Show", 60, 46353, "=C4*1.1"],
         ["Red Butte", "Park", 14, 46352, "=C5*1.1"]]
AFTER_SHOWN = [SHOWN[0], ["Delta Center", "Show", "$95.00", "2026-11-24", "104.5"], SHOWN[2], SHOWN[3],
               ["Red Butte", "Park", "$14.00", "2026-11-26", "15.4"]]


@pytest.mark.asyncio
async def test_a_save_keeps_its_scheduled_bots_and_starts_ren_for_each_bot_it_meets():
    tables: Tables = {}
    scheduler = Scheduler(tables)
    bots: list[GridBot] = [*GRID["sheets"][0].get("bots", []), {"name": "Either", "on": "added or changed", "ask": "Price it."}]
    before, after = with_bots(GRID, bots), with_bots(with_rows(GRID, AFTER), bots)
    await saved(scheduler, "u1", "doc1", block(before, SHOWN), block(after, AFTER_SHOWN))
    assert [(agent, bot, trigger) for _, agent, bot, trigger in scheduler.started] == [
        (None, "New plan", "bot"), (None, "Show changed", "bot"), (None, "Either", "bot")]
    assert scheduler.prompts[0].startswith("New plan · Outings\n\nFind tickets for it.\n\nRows added on the sheet")
    assert "| Red Butte | Park | $14.00 | 2026-11-26 | 15.4 |" in scheduler.prompts[0]
    [kept] = tables["grid_bots"]
    assert ({key: kept[key] for key in ("owner_id", "document_id", "sheet", "name")}
            == {"owner_id": "u1", "document_id": "doc1", "sheet": "Plans", "name": "Morning"})
    assert kept["cadence"] == Cadence.model_validate(MORNING.get("schedule")).model_dump()
    due = kept["next_run_at"]

    # Another person's save keeps the bot for them; a new time moves its next run; a bot the page dropped goes.
    await saved(scheduler, "u2", "doc1", block(after, AFTER_SHOWN), block(after, AFTER_SHOWN))
    assert (tables["grid_bots"][0]["owner_id"], tables["grid_bots"][0]["next_run_at"]) == ("u2", due)
    later = with_bots(after, [{**MORNING, "schedule": {"every": "day", "time": "09:00", "timezone": "America/Denver"}}])
    await saved(scheduler, "u2", "doc1", block(after, AFTER_SHOWN), block(later, AFTER_SHOWN))
    assert datetime.fromisoformat(tables["grid_bots"][0]["next_run_at"]) - datetime.fromisoformat(due) == timedelta(hours=1)
    await saved(scheduler, "u2", "doc1", block(later, AFTER_SHOWN), block(with_bots(after, []), AFTER_SHOWN))
    assert tables["grid_bots"] == []
    assert len(scheduler.started) == 3


@pytest.mark.asyncio
async def test_a_due_bot_starts_once_with_its_rows_and_is_let_go_when_its_user_cant_open_the_page():
    now = datetime.now(UTC)
    page = {"id": "doc1", "project_id": None, "created_by": "u1",
            "content": block(with_bots(GRID, [{**MORNING, "slice": "Shows"}]), SHOWN)}
    tables: Tables = {
        "grid_bots": [{"id": "b1", "owner_id": "u1", "document_id": "doc1", "sheet": "Plans", "name": "Morning",
                       "cadence": Cadence.model_validate(MORNING.get("schedule")).model_dump(),
                       "next_run_at": (now - timedelta(minutes=1)).isoformat()}],
        "project_documents": [page]}
    scheduler = Scheduler(tables)
    assert await start_due(scheduler) == 1
    assert scheduler.started == [("u1", None, "Morning", "schedule")]
    assert "The rows on the sheet \"Plans\" of the spreadsheet \"Outings\", in its slice \"Shows\"" in scheduler.prompts[0]
    assert "| Ballet West |" in scheduler.prompts[0] and "Liberty Park" not in scheduler.prompts[0]
    assert "by the bot \"Morning\" on its schedule (daily at 08:00)" in scheduler.prompts[0]
    # Claimed: the next run is tomorrow, so another check starts nothing.
    assert datetime.fromisoformat(tables["grid_bots"][0]["next_run_at"]) > now
    assert await start_due(scheduler) == 0
    # A page someone else wrote, outside any project, isn't this user's: the bot is let go.
    tables["grid_bots"][0]["next_run_at"] = (now - timedelta(minutes=1)).isoformat()
    page["created_by"] = "u2"
    assert await start_due(scheduler) == 0
    assert tables["grid_bots"] == [] and len(scheduler.started) == 1
