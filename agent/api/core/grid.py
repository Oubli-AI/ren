"""Spreadsheets in Documents and Squad's DataGrid outcome.

A grid is what agents write and read: JSON with sheets of rows, in a ```grid fence (``GRID_MARKDOWN``). A page keeps
it as the editor's grid block, ``<grid-block data-grid="{…}">`` with a table per sheet, which is what prints and what
a reader without the editor sees (``src/lib/grid.ts`` reads and writes the same block). Once the editor's spreadsheet
has calculated a grid, the block is marked ``data-calculated`` and its tables hold what each cell shows; once the grid
has been changed there, the block also carries the workbook the spreadsheet keeps (``data-workbook``). Agents still
read and write the grid.
"""

from __future__ import annotations

import html
import io
import json
import math
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, NotRequired, TypedDict, TypeIs, get_args

GridCell = str | int | float | bool | None


#: How a condition tests a cell.
ConditionOp = Literal["=", "!=", ">", ">=", "<", "<=", "contains", "empty", "filled"]
RuleColor = Literal["red", "yellow", "green", "blue", "purple", "gray"]
_OPS: Final[tuple[ConditionOp, ...]] = get_args(ConditionOp)
_COLORS: Final[tuple[RuleColor, ...]] = get_args(RuleColor)


class GridCondition(TypedDict):
    """A test on a row's cell in the column with that header: ``value`` as the cell keeps it, a date as "2026-11-21"."""

    column: str
    op: ConditionOp
    value: NotRequired[str | int | float | bool]


class GridSlice(TypedDict):
    """A named part of a sheet's rows: those that pass every test (AppSheet's slice)."""

    name: str
    where: list[GridCondition]


class GridRule(TypedDict):
    """The color, and bold, of the cells in ``columns`` (every column when empty) of a row that passes every test
    (AppSheet's format rule)."""

    where: list[GridCondition]
    columns: list[str]
    color: RuleColor
    bold: NotRequired[bool]


#: What starts a bot that isn't scheduled: rows added, changed, or either (AppSheet's data change event).
BotEvent = Literal["added", "changed", "added or changed"]
_EVENTS: Final[tuple[BotEvent, ...]] = get_args(BotEvent)
_EVERY: Final[tuple[str, ...]] = ("hours", "day", "weekdays", "week")


class GridBot(TypedDict):
    """A sheet's bot (AppSheet's bot): on ``on``, or on ``schedule`` (a scheduled agent's cadence), Ren does ``ask``
    with the rows, those of ``slice`` when it names one (``grid_bots.py``)."""

    name: str
    ask: str
    on: NotRequired[BotEvent]
    schedule: NotRequired[dict[str, str | int]]
    slice: NotRequired[str]


class GridSheet(TypedDict):
    name: str
    rows: list[list[GridCell]]
    #: A column's Excel number format by its letter.
    formats: NotRequired[dict[str, str]]
    slices: NotRequired[list[GridSlice]]
    rules: NotRequired[list[GridRule]]
    bots: NotRequired[list[GridBot]]


class Grid(TypedDict):
    title: str
    sheets: list[GridSheet]


#: A grid's JSON, as agents are told to write it.
GRID_JSON: Final[str] = (
    '{"title": ..., "sheets": [{"name": ..., "rows": [[...], ...], "formats": {"B": "#,##0"}}]}. The first row of a '
    'sheet is its header; a cell is a number, true or false, text, or a formula as text starting with "=" in A1 '
    'notation ("=SUM(B2:B9)", "=Sheet2!B4*0.2"); "formats" is optional and gives a column\'s Excel number format by '
    'its letter. "slices" is optional and names parts of the rows, each the rows that pass every test: [{"name": '
    '"Shows", "where": [{"column": "Kind", "op": "=", "value": "Show"}]}]; "rules" is optional and colors the cells of '
    '"columns" (every column when empty) in a row that passes every test: [{"where": [{"column": "Cost", "op": ">", '
    '"value": 50}], "columns": ["Cost"], "color": "red", "bold": true}]; "op" is =, !=, >, >=, <, <=, contains, empty '
    'or filled, a date is written "2026-11-21", and "color" is red, yellow, green, blue, purple or gray. "bots" is '
    'optional and is set by the person in the spreadsheet: keep it as it is.'
)
#: How agents write a spreadsheet (the editor's "/" Spreadsheet): read back the same way by ``html_to_markdown``.
GRID_MARKDOWN: Final[str] = (
    f"A spreadsheet is one ```grid fence holding JSON: {GRID_JSON} Use it for data to sort, filter and calculate with; "
    "a small table of text stays a Markdown table."
)

XLSX_MEDIA_TYPE: Final[str] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: What Excel refuses in a sheet's name, which is 31 characters at most; runs of spaces read as one.
_SHEET_NAME_CHARS: Final[re.Pattern[str]] = re.compile(r"[\[\]:*?/\\\s]+")
_COLUMN: Final[re.Pattern[str]] = re.compile(r"[A-Z]+")
_GRID_FENCE: Final[re.Pattern[str]] = re.compile(r'<pre><code class="language-grid">(.*?)</code></pre>', re.DOTALL)


def column_letter(index: int) -> str:
    """"A" for 0, "Z" for 25, "AA" for 26."""
    letters = ""
    while index >= 0:
        letters = chr(65 + index % 26) + letters
        index = index // 26 - 1
    return letters


def _cell(value: object) -> GridCell:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if isinstance(value, (str, int, float, bool)) else None


def _is_record(value: object) -> TypeIs[dict[str, object]]:
    """A JSON object, whose keys are text."""
    return isinstance(value, dict)


def _is_list(value: object) -> TypeIs[list[object]]:
    return isinstance(value, list)


def _items(value: object) -> list[object]:
    """A JSON array's items; none for anything else."""
    return value if _is_list(value) else []


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _conditions(value: object) -> list[GridCondition]:
    kept: list[GridCondition] = []
    for raw in _items(value):
        if not _is_record(raw):
            continue
        op, column = raw.get("op"), _text(raw.get("column"))
        if not isinstance(op, str) or op not in _OPS or not column:
            continue
        condition: GridCondition = {"column": column, "op": op}
        tested = _cell(raw.get("value"))
        if tested is not None:
            condition["value"] = tested
        kept.append(condition)
    return kept


def _slices(value: object) -> list[GridSlice]:
    """A sheet's slices, each with a name of its own."""
    slices: list[GridSlice] = []
    taken: set[str] = set()
    for raw in _items(value):
        name = _text(raw.get("name"))[:60].strip() if _is_record(raw) else ""
        if not _is_record(raw) or not name or name.lower() in taken:
            continue
        taken.add(name.lower())
        slices.append({"name": name, "where": _conditions(raw.get("where"))})
    return slices


def _rules(value: object) -> list[GridRule]:
    rules: list[GridRule] = []
    for raw in _items(value):
        if not _is_record(raw):
            continue
        color = raw.get("color")
        if not isinstance(color, str) or color not in _COLORS:
            continue
        columns = [name for each in _items(raw.get("columns")) if (name := _text(each))]
        rule: GridRule = {"where": _conditions(raw.get("where")), "columns": columns, "color": color}
        if raw.get("bold") is True:
            rule["bold"] = True
        rules.append(rule)
    return rules


def _whole(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _schedule(value: object) -> dict[str, str | int] | None:
    """A bot's schedule from JSON anyone wrote: a scheduled agent's cadence, with what it leaves out as a new one has
    it (``defaultCadence``); ``Cadence`` checks it when the bot is kept to run."""
    if not _is_record(value):
        return None
    every, time, timezone = value.get("every"), value.get("time"), value.get("timezone")
    if not isinstance(every, str) or every not in _EVERY:
        return None
    return {"every": every, "hours": _whole(value.get("hours"), 24), "time": time if isinstance(time, str) and time else "08:00",
            "weekday": _whole(value.get("weekday"), 0), "timezone": timezone if isinstance(timezone, str) and timezone else "UTC"}


def _bots(value: object) -> list[GridBot]:
    """A sheet's bots, each with a name of its own, something to do, and an event or a schedule."""
    bots: list[GridBot] = []
    taken: set[str] = set()
    for raw in _items(value):
        if not _is_record(raw):
            continue
        name, ask, on = _text(raw.get("name"))[:60].strip(), _text(raw.get("ask"))[:2000], raw.get("on")
        schedule = _schedule(raw.get("schedule"))
        if not name or not ask or name.lower() in taken or (schedule is None and (not isinstance(on, str) or on not in _EVENTS)):
            continue
        taken.add(name.lower())
        bot: GridBot = {"name": name, "ask": ask}
        if schedule is not None:
            bot["schedule"] = schedule
        elif isinstance(on, str) and on in _EVENTS:
            bot["on"] = on
        if part := _text(raw.get("slice")):
            bot["slice"] = part
        bots.append(bot)
    return bots


def normalize_grid(value: object) -> Grid | None:
    """A grid from JSON anyone wrote: every sheet with a name of its own and rows of plain cells, and its slices,
    rules and bots with every test, color and event checked; None when it holds no sheet."""
    if not _is_record(value):
        return None
    sheets: list[GridSheet] = []
    taken: set[str] = set()
    for raw in _items(value.get("sheets")):
        if not _is_record(raw):
            continue
        name = _SHEET_NAME_CHARS.sub(" ", _text(raw.get("name"))).strip()[:31].strip()
        number = len(sheets) + 1
        while not name or name.lower() in taken:
            name, number = f"Sheet{number}", number + 1
        taken.add(name.lower())
        sheet: GridSheet = {
            "name": name,
            "rows": [[_cell(cell) for cell in row] for row in _items(raw.get("rows")) if _is_list(row)],
        }
        formats = raw.get("formats")
        kept = {
            letter: pattern for letter, pattern in formats.items()
            if _COLUMN.fullmatch(letter) and isinstance(pattern, str) and pattern
        } if _is_record(formats) else {}
        if kept:
            sheet["formats"] = kept
        slices, rules, bots = _slices(raw.get("slices")), _rules(raw.get("rules")), _bots(raw.get("bots"))
        if slices:
            sheet["slices"] = slices
        if rules:
            sheet["rules"] = rules
        if bots:
            sheet["bots"] = bots
        sheets.append(sheet)
    if not sheets:
        return None
    return {"title": _text(value.get("title"))[:120] or "Spreadsheet", "sheets": sheets}


def grid_from_json(text: str) -> Grid | None:
    """The grid a ```grid fence, a grid block's ``data-grid`` or a run's grid.json holds, or None when it isn't one."""
    try:
        return normalize_grid(json.loads(text))
    except ValueError:
        return None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def grid_markdown(grid: Grid) -> str:
    """A grid as its ```grid fence: a row a line, so a sheet reads as a table."""
    sheets = ",\n".join(
        f'    {{"name": {_json(sheet["name"])}'
        + (f', "formats": {_json(sheet["formats"])}' if "formats" in sheet else "")
        + (f', "slices": {_json(sheet["slices"])}' if "slices" in sheet else "")
        + (f', "rules": {_json(sheet["rules"])}' if "rules" in sheet else "")
        + (f', "bots": {_json(sheet["bots"])}' if "bots" in sheet else "")
        + ', "rows": [\n'
        + ",\n".join(f"      {_json(row)}" for row in sheet["rows"])
        + "\n    ]}"
        for sheet in grid["sheets"]
    )
    return f'```grid\n{{"title": {_json(grid["title"])}, "sheets": [\n{sheets}\n]}}\n```'


def _shown(cell: GridCell) -> str:
    """A cell as it reads before a spreadsheet has calculated it: as written, a formula as its text."""
    if cell is None:
        return ""
    if isinstance(cell, bool):
        return "TRUE" if cell else "FALSE"
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell)


def grid_block_html(grid: Grid) -> str:
    """The editor's grid block for a grid no spreadsheet has calculated yet: the grid, and a table per sheet with each
    cell as written."""
    tables: list[str] = []
    for sheet in grid["sheets"]:
        width = max((len(row) for row in sheet["rows"]), default=0)
        rows = "".join(
            "<tr>"
            + "".join(
                f"<{tag}>{html.escape(_shown(cell))}</{tag}>" for cell in [*row, *[None] * (width - len(row))]
            )
            + "</tr>"
            for tag, row in (("td" if r else "th", row) for r, row in enumerate(sheet["rows"]))
        )
        tables.append(f"<table><caption>{html.escape(sheet['name'])}</caption><tbody>{rows}</tbody></table>")
    return f'<grid-block data-grid="{html.escape(_json(grid), quote=True)}">{"".join(tables)}</grid-block>'


def with_grid_blocks(page_html: str) -> str:
    """Rendered Markdown with every ```grid fence as the editor's grid block; a fence that holds no grid stays code."""

    def block(match: re.Match[str]) -> str:
        grid = grid_from_json(html.unescape(match.group(1)))
        return grid_block_html(grid) if grid else match.group(0)

    return _GRID_FENCE.sub(block, page_html)


_NUMERIC: Final[re.Pattern[str]] = re.compile(r"-?\d+(?:\.\d+)?")
_US_DATE: Final[re.Pattern[str]] = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_ISO_DATE: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISO_DATETIME: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?")
#: 1970-01-01 as a spreadsheet counts days, from 1899-12-30.
_EPOCH: Final[int] = 25_569


def shown_value(text: str) -> GridCell:
    """A formula's result from what its cell shows: a number without its currency, grouping, sign in brackets or
    percent sign, TRUE or FALSE, a date as "2026-11-21", or the text."""
    shown = text.strip()
    if not shown:
        return None
    if shown in ("TRUE", "FALSE"):
        return shown == "TRUE"
    plain = re.sub(r"[$€£¥,\s]", "", shown)
    negative = plain.startswith("(") and plain.endswith(")")
    plain = plain.strip("()")
    percent = plain.endswith("%")
    plain = plain.removesuffix("%")
    if _NUMERIC.fullmatch(plain):
        number = float(plain) * (-1 if negative else 1) / (100 if percent else 1)
        return int(number) if number.is_integer() else number
    if day := _US_DATE.fullmatch(shown):
        return f"{day[3]}-{int(day[1]):02d}-{int(day[2]):02d}"
    return shown


def fold(text: str) -> str:
    """Text as a search compares it: without case or accents (``fold`` in ``src/lib/grid.ts``)."""
    return "".join(char for char in unicodedata.normalize("NFD", text) if not unicodedata.category(char).startswith("M")).lower()


def cell_day(cell: GridCell) -> str | None:
    """The day a date falls on ("2026-11-21"), from a cell kept as a spreadsheet number or as text (``cellDay``)."""
    if isinstance(cell, str):
        return cell[:10] if _ISO_DATE.match(cell) else None
    if isinstance(cell, bool) or not isinstance(cell, (int, float)):
        return None
    try:
        return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=round((cell - _EPOCH) * 86_400_000))).date().isoformat()
    except OverflowError:
        return None


def _number_type(format_code: str | None) -> str:
    """A column of numbers by its number format, as ``numberType`` reads it; only a date's matters to a test."""
    if not format_code or "%" in format_code:
        return "number"
    codes = re.sub(r'"[^"]*"|\[[^\]]*\]', "", format_code).lower()
    day, hour = re.search(r"[yd]", codes) is not None, re.search(r"[hs]", codes) is not None
    return "datetime" if day and hour else "date" if day else "number"


def column_types(sheet: GridSheet, held: Sequence[Sequence[GridCell]]) -> list[tuple[str, str]]:
    """Each column's header and the type a test compares it by (``gridColumns``): yes or no, a date, a date and
    time, or anything else, from ``held`` (the rows' cells, a formula as its result)."""
    header = sheet["rows"][0] if sheet["rows"] else []
    width = max((len(row) for row in sheet["rows"]), default=0)
    columns: list[tuple[str, str]] = []
    for c in range(width):
        name = _shown(header[c] if c < len(header) else None).strip() or column_letter(c)
        cells = [row[c] for row in held[1:] if c < len(row) and row[c] is not None and row[c] != ""]
        texts = [cell for cell in cells if isinstance(cell, str)]
        if cells and all(isinstance(cell, bool) for cell in cells):
            kind = "yesno"
        elif cells and all(isinstance(cell, (int, float)) and not isinstance(cell, bool) for cell in cells):
            kind = _number_type(sheet.get("formats", {}).get(column_letter(c)))
        elif cells and len(texts) == len(cells) and all(_ISO_DATE.fullmatch(text) for text in texts):
            kind = "date"
        elif cells and len(texts) == len(cells) and all(_ISO_DATETIME.fullmatch(text) for text in texts):
            kind = "datetime"
        else:
            kind = "text"
        columns.append((name, kind))
    return columns


def _compare(kind: str, held: GridCell, tested: str | int | float | bool, shown: str) -> int:
    """Below 0 when the cell is less than the tested value, 0 when they are the same (``compare``)."""
    if kind in ("date", "datetime"):
        day = cell_day(held) or ""
        other = (cell_day(tested) or "") if isinstance(tested, (int, float)) and not isinstance(tested, bool) else str(tested)[:10]
        return (day > other) - (day < other)
    number = (float(tested) if isinstance(tested, (int, float)) and not isinstance(tested, bool)
              else float(str(tested).strip()) if _NUMERIC.fullmatch(str(tested).strip()) else None)
    if isinstance(held, (int, float)) and not isinstance(held, bool) and number is not None:
        return (held > number) - (held < number)
    if kind == "yesno":
        return int(held is True) - int(tested is True or re.fullmatch(r"yes|true", str(tested), re.IGNORECASE) is not None)
    left, right = fold(shown), fold(str(tested))
    return (left > right) - (left < right)


def row_matches(where: Sequence[GridCondition], columns: Sequence[tuple[str, str]], held: Sequence[GridCell],
                shown: Sequence[str]) -> bool:
    """Whether a row passes every test (``rowMatches``): ``held`` gives its cells (a formula as its result),
    ``shown`` what each shows."""
    for condition in where:
        c = next((index for index, (name, _) in enumerate(columns) if fold(name) == fold(condition["column"])), -1)
        if c < 0:
            return False
        cell = held[c] if c < len(held) else None
        text = shown[c] if c < len(shown) else _shown(cell)
        op, tested = condition["op"], condition.get("value")
        empty = cell is None or cell == ""
        if op in ("empty", "filled"):
            passed = empty == (op == "empty")
        elif empty or tested is None:
            passed = op == "!="
        elif op == "contains":
            passed = fold(str(tested)) in fold(text)
        else:
            order = _compare(columns[c][1], cell, tested, text)
            passed = {"=": order == 0, "!=": order != 0, ">": order > 0, ">=": order >= 0, "<": order < 0,
                      "<=": order <= 0}[op]
        if not passed:
            return False
    return True


def grid_xlsx(grid: Grid, values: Sequence[Sequence[Sequence[GridCell]]] = ()) -> bytes:
    """The grid as an Excel workbook: every formula as a formula with the result it had in ``values`` (by sheet, row
    and cell), the header row bold and frozen, each column's number format on its cells, columns as wide as their text.

    XlsxWriter doesn't calculate; a formula written with its result shows it in readers that don't calculate either,
    and Excel calculates again when it opens the file
    (https://xlsxwriter.readthedocs.io/working_with_formulas.html#formula-results). ``use_future_functions`` writes
    the functions Excel added after 2007 (XLOOKUP, IFS, TEXTJOIN) the way the file format wants them.
    """
    import xlsxwriter

    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True, "use_future_functions": True})
    header = workbook.add_format({"bold": True})
    for index, sheet in enumerate(grid["sheets"]):
        worksheet = workbook.add_worksheet(sheet["name"])
        formats = {
            letter: workbook.add_format({"num_format": pattern}) for letter, pattern in sheet.get("formats", {}).items()
        }
        results = values[index] if index < len(values) else ()
        for r, row in enumerate(sheet["rows"]):
            for c, cell in enumerate(row):
                if cell is None or cell == "":
                    continue
                style = header if r == 0 else formats.get(column_letter(c))
                if isinstance(cell, str) and cell.startswith("="):
                    result = results[r][c] if r < len(results) and c < len(results[r]) else None
                    worksheet.write_formula(r, c, cell, style, "" if result is None else result)
                elif worksheet.write(r, c, cell, style) != 0 and isinstance(cell, str):
                    # A link Excel can't keep (longer than it allows, or past a sheet's limit) stays as its text.
                    worksheet.write_string(r, c, cell, style)
        worksheet.freeze_panes(1, 0)
        worksheet.autofit()
    workbook.close()
    return buffer.getvalue()
