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
from collections.abc import Sequence
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


class GridSheet(TypedDict):
    name: str
    rows: list[list[GridCell]]
    #: A column's Excel number format by its letter.
    formats: NotRequired[dict[str, str]]
    slices: NotRequired[list[GridSlice]]
    rules: NotRequired[list[GridRule]]


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
    'or filled, a date is written "2026-11-21", and "color" is red, yellow, green, blue, purple or gray.'
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


def normalize_grid(value: object) -> Grid | None:
    """A grid from JSON anyone wrote: every sheet with a name of its own and rows of plain cells, and its slices and
    rules with every test and color checked; None when it holds no sheet."""
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
        slices, rules = _slices(raw.get("slices")), _rules(raw.get("rules"))
        if slices:
            sheet["slices"] = slices
        if rules:
            sheet["rules"] = rules
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
