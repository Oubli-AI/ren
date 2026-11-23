"""Spreadsheet bots, after AppSheet's (https://support.google.com/appsheet/answer/11998993): a sheet's ``bots``
(``GridBot`` in ``grid.py``) start Ren with the sheet's rows when rows are added or changed, or on a schedule.

A person's save of a page (``PATCH /projects/documents/{id}``) hands ``saved`` the page's grid blocks before and after
it: each grid as written (``data-grid``) and, once the editor's spreadsheet has calculated it, what each cell shows (the
block's tables). Ren's own saves (``workspace_tools.py``) start no bots, so a bot's run never starts itself again.
``saved`` compares each sheet before and after the save. A row whose cells, as written, match no row before it is new:
changed when a row that is gone had the same first cell (the row's name, as its card shows it), added otherwise. A bot
whose event and slice those rows meet starts one run with them. The open spreadsheet hands the page its changes when it
closes, so the rows a person adds or changes there reach the bots together.

A scheduled bot is kept in ``grid_bots`` (``supabase/migrations/025_grid_bots.sql``) with its next due time, for the
user who saved the page last. ``start_due`` (on the scheduler's loop) claims each due bot as the scheduler claims a
schedule, reads its page with the service-role key once that user can still open it, and starts the bot with its
sheet's rows; a bot the page no longer holds is let go.

A run is the scheduler's (``AgentScheduler.run``): a panel session with Ren and an ``agent_runs`` row the Context
widget notifies about.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from bs4 import BeautifulSoup

from agent.api.core.agent_schedules import RUN_PROMPT, AgentScheduler, Cadence, cadence_label, next_run
from agent.api.core.grid import (
    GridBot, GridCell, GridSheet, column_types, grid_from_json, row_matches, shown_value,
)

logger = logging.getLogger(__name__)

BOTS: Final = "grid_bots"
#: Rows one run is handed at most; the prompt says how many the sheet has.
ROWS_SHOWN: Final = 200
#: Due bots one check starts at most; the rest are due on the next check.
DUE_BATCH: Final = 20

@dataclass(frozen=True)
class PageSheet:
    """A sheet of a page's grid block: the grid's title, the sheet as written and what each of its cells shows."""

    title: str
    sheet: GridSheet
    shown: list[list[str]]


def _as_written(cell: GridCell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, bool):
        return "TRUE" if cell else "FALSE"
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell)


def page_sheets(content: str) -> list[PageSheet]:
    """Every sheet of every grid block in a page's HTML, with what its cells show (as written before a spreadsheet
    has calculated it)."""
    sheets: list[PageSheet] = []
    for block in BeautifulSoup(content or "", "html.parser").find_all("grid-block"):
        grid = grid_from_json(str(block.get("data-grid") or ""))
        if grid is None:
            continue
        tables = block.find_all("table")
        calculated = block.has_attr("data-calculated") and len(tables) == len(grid["sheets"])
        for index, sheet in enumerate(grid["sheets"]):
            shown = ([[cell.get_text() for cell in row.find_all(["th", "td"])] for row in tables[index].find_all("tr")]
                     if calculated
                     else [[_as_written(cell) for cell in row] for row in sheet["rows"]])
            sheets.append(PageSheet(grid["title"], sheet, shown))
    return sheets


def held_rows(page: PageSheet) -> list[list[GridCell]]:
    """The sheet's cells as a test reads them: as written, a formula as the result its cell shows."""
    return [[shown_value(page.shown[r][c] if r < len(page.shown) and c < len(page.shown[r]) else "")
             if isinstance(cell, str) and cell.startswith("=") else cell for c, cell in enumerate(row)]
            for r, row in enumerate(page.sheet["rows"])]


def _key(row: Sequence[GridCell]) -> str:
    cells = list(row)
    while cells and cells[-1] in (None, ""):
        del cells[-1]
    return json.dumps(cells, ensure_ascii=False)


def _first(row: Sequence[GridCell]) -> str | None:
    return json.dumps(row[0], ensure_ascii=False) if row and row[0] not in (None, "") else None


def changes(before: Sequence[Sequence[GridCell]], after: Sequence[Sequence[GridCell]]) -> tuple[list[int], list[int]]:
    """The rows of ``after``, past its header, that ``before`` didn't have, as indices: (added, changed)."""
    remaining = Counter(_key(row) for row in before[1:])
    fresh: list[int] = []
    for index, row in enumerate(after[1:], start=1):
        if remaining[_key(row)] > 0:
            remaining[_key(row)] -= 1
        else:
            fresh.append(index)
    gone: Counter[str] = Counter()
    for row in before[1:]:
        if remaining[_key(row)] > 0:
            remaining[_key(row)] -= 1
            if (first := _first(row)) is not None:
                gone[first] += 1
    added: list[int] = []
    changed: list[int] = []
    for index in fresh:
        first = _first(after[index])
        if first is not None and gone[first] > 0:
            gone[first] -= 1
            changed.append(index)
        else:
            added.append(index)
    return added, changed


def bot_rows(page: PageSheet, bot: GridBot, among: Sequence[int] | None = None) -> list[int]:
    """The rows (indices past the header) of ``among``, or of the whole sheet, that pass the bot's slice; every one
    of them when it names none. A slice the sheet doesn't have passes none."""
    rows = list(among) if among is not None else list(range(1, len(page.sheet["rows"])))
    if "slice" not in bot:
        return rows
    part = next((each for each in page.sheet.get("slices", []) if each["name"].lower() == bot["slice"].lower()), None)
    if part is None:
        return []
    held = held_rows(page)
    columns = column_types(page.sheet, held)
    return [r for r in rows if row_matches(part["where"], columns, held[r], page.shown[r] if r < len(page.shown) else [])]


def _cell_text(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def rows_table(page: PageSheet, rows: Sequence[int]) -> str:
    """The rows as the sheet shows them, under its header, as a Markdown table."""
    header = page.shown[0] if page.shown else []
    width = max([len(header), *(len(page.shown[r]) for r in rows if r < len(page.shown))], default=0)

    def line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(_cell_text(cells[c]) if c < len(cells) else "" for c in range(width)) + " |"

    shown = [line(page.shown[r]) for r in rows[:ROWS_SHOWN] if r < len(page.shown)]
    more = f"\n\n(The first {ROWS_SHOWN} of {len(rows)} rows.)" if len(rows) > ROWS_SHOWN else ""
    return "\n".join([line(header), "| " + " | ".join(["---"] * width) + " |", *shown]) + more


def bot_prompt(page: PageSheet, bot: GridBot, rows: Sequence[int], *, why: str, what: str) -> str:
    """The durable user message a bot's run starts with; its first line titles the thread."""
    task = (f"{bot['ask']}\n\n{what} on the sheet \"{page.sheet['name']}\" of the spreadsheet \"{page.title}\""
            + (f", in its slice \"{bot['slice']}\"" if "slice" in bot else "") + f":\n\n{rows_table(page, rows)}")
    return RUN_PROMPT.format(title=f"{bot['name']} · {page.title}", task=task, why=why)


def _sheet_of(sheets: Sequence[PageSheet], title: str, name: str) -> PageSheet | None:
    return next((each for each in sheets if each.title == title and each.sheet["name"] == name), None)


def event_bots(before: Sequence[PageSheet], after: Sequence[PageSheet]) -> list[tuple[PageSheet, GridBot, list[int], str]]:
    """The bots a save starts, each with the rows it is handed and what happened to them."""
    fired: list[tuple[PageSheet, GridBot, list[int], str]] = []
    for page in after:
        bots = [bot for bot in page.sheet.get("bots", []) if "on" in bot]
        old = _sheet_of(before, page.title, page.sheet["name"])
        # A sheet new to the page, or renamed, has no rows to compare.
        if not bots or old is None:
            continue
        added, changed = changes(old.sheet["rows"], page.sheet["rows"])
        for bot in bots:
            among = {"added": added, "changed": changed, "added or changed": sorted(added + changed)}[bot["on"]]
            rows = bot_rows(page, bot, among) if among else []
            if rows:
                what = {"added": "Rows added", "changed": "Rows changed", "added or changed": "Rows added or changed"}[bot["on"]]
                fired.append((page, bot, rows, what))
    return fired


async def _start(scheduler: AgentScheduler, owner: str, page: PageSheet, bot: GridBot, rows: Sequence[int], *,
                 why: str, what: str, trigger: Literal["bot", "schedule"]) -> None:
    _ = await scheduler.run(owner, None, bot=bot["name"], prompt=bot_prompt(page, bot, rows, why=why, what=what),
                            trigger=trigger)


async def _sync(scheduler: AgentScheduler, owner: str, document_id: str, sheets: Sequence[PageSheet]) -> None:
    """Keep the page's scheduled bots in ``grid_bots`` for the user who saved it, and let go of the ones it dropped."""
    client = await scheduler.client()
    now = datetime.now(UTC)
    kept = (await client.table(BOTS).select("id,sheet,name,cadence").eq("document_id", document_id).execute()).data
    wanted: dict[tuple[str, str], Cadence] = {}
    for page in sheets:
        for bot in page.sheet.get("bots", []):
            if "schedule" not in bot:
                continue
            try:
                wanted[(page.sheet["name"], bot["name"])] = Cadence.model_validate(bot["schedule"])
            except ValueError as error:
                logger.info("document=<%s>, bot=<%s>, error=<%s> | a bot's schedule is not valid", document_id, bot["name"], error)
    for row in kept:
        cadence = wanted.pop((row["sheet"], row["name"]), None)
        if cadence is None:
            await client.table(BOTS).delete().eq("id", row["id"]).execute()
        elif cadence.model_dump() != row["cadence"]:
            await client.table(BOTS).update({"owner_id": owner, "cadence": cadence.model_dump(),
                                             "next_run_at": next_run(cadence, now).isoformat(),
                                             "updated_at": now.isoformat()}).eq("id", row["id"]).execute()
        else:
            await client.table(BOTS).update({"owner_id": owner, "updated_at": now.isoformat()}).eq("id", row["id"]).execute()
    for (sheet, name), cadence in wanted.items():
        await client.table(BOTS).insert({"owner_id": owner, "document_id": document_id, "sheet": sheet, "name": name,
                                         "cadence": cadence.model_dump(), "next_run_at": next_run(cadence, now).isoformat()}).execute()


async def saved(scheduler: AgentScheduler, owner: str, document_id: str, before: str, after: str) -> None:
    """After ``owner`` saved a page that held ``before``: keep its scheduled bots, and start the bots the save meets."""
    old, new = page_sheets(before), page_sheets(after)
    await _sync(scheduler, owner, document_id, new)
    for page, bot, rows, what in event_bots(old, new):
        try:
            await _start(scheduler, owner, page, bot, rows, why=f"by the bot \"{bot['name']}\" when rows were saved",
                         what=what, trigger="bot")
        except Exception:
            logger.exception("document=<%s>, bot=<%s> | bot did not start", document_id, bot["name"])


async def _can_open(client: Any, owner: str, page: dict[str, Any]) -> bool:
    """Whether ``owner`` may open the page, as its row level security decides (``022_personal_documents.sql``)."""
    if page.get("project_id") is None:
        return page.get("created_by") == owner
    projects = (await client.table("projects").select("id").eq("id", page["project_id"]).eq("user_id", owner).limit(1).execute()).data
    if projects:
        return True
    members = (await client.table("project_members").select("project_id").eq("project_id", page["project_id"])
               .eq("user_id", owner).limit(1).execute()).data
    return bool(members)


async def start_due(scheduler: AgentScheduler) -> int:
    """Start every scheduled bot that is due; returns how many runs started."""
    client = await scheduler.client()
    now = datetime.now(UTC)
    due = (await client.table(BOTS).select("id,owner_id,document_id,sheet,name,cadence,next_run_at")
           .lte("next_run_at", now.isoformat()).order("next_run_at").limit(DUE_BATCH).execute()).data
    started = 0
    for row in due:
        cadence = Cadence.model_validate(row["cadence"])
        following = next_run(cadence, now, datetime.fromisoformat(row["next_run_at"]))
        # Compare-and-set: only the process that moves this exact due time forward starts the occurrence.
        claimed = (await client.table(BOTS).update({"next_run_at": following.isoformat()})
                   .eq("id", row["id"]).eq("next_run_at", row["next_run_at"]).execute()).data
        if not claimed:
            continue
        pages = (await client.table("project_documents").select("id,content,project_id,created_by")
                 .eq("id", row["document_id"]).limit(1).execute()).data
        sheets = page_sheets(pages[0].get("content") or "") if pages and await _can_open(client, row["owner_id"], pages[0]) else []
        found = next(((page, bot) for page in sheets if page.sheet["name"] == row["sheet"]
                      for bot in page.sheet.get("bots", []) if bot["name"] == row["name"] and "schedule" in bot), None)
        if found is None:
            await client.table(BOTS).delete().eq("id", row["id"]).execute()
            logger.info("bot=<%s>, document=<%s> | the page no longer holds this bot for its user; let go", row["name"], row["document_id"])
            continue
        page, bot = found
        try:
            await _start(scheduler, row["owner_id"], page, bot, bot_rows(page, bot),
                         why=f"by the bot \"{bot['name']}\" on its schedule ({cadence_label(cadence)})",
                         what="The rows", trigger="schedule")
            started += 1
        except Exception:
            logger.exception("bot=<%s>, document=<%s> | scheduled bot did not start", row["name"], row["document_id"])
    return started


__all__ = ["PageSheet", "bot_prompt", "bot_rows", "changes", "event_bots", "held_rows", "page_sheets", "rows_table", "saved",
           "start_due"]
