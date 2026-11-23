import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  cellDay, columnLetter, gridColumns, gridDisplay, gridFromJson, gridMarkdown, gridValue, gridWorkbook, normalizeGrid, readGrid, rowViews, sheetCsv,
  starterGrid, temporalSerial, temporalText,
  type Grid, type GridColumn, type GridSheet,
} from './grid.ts'

const GRID: Grid = {
  title: 'Fleet costs',
  sheets: [
    {
      name: 'Fleet',
      formats: { C: '$#,##0.00', D: '$#,##0.00' },
      rows: [
        ['Vehicle', 'Units', 'Unit cost', 'Total', 'Electric'],
        ['Van', 3, 1500, '=B2*C2', true],
        ['Truck', 1.5, 2041.67, '=B3*C3', false],
        [null, '=SUM(B2:B3)', null, '=SUM(D2:D3)'],
      ],
    },
    { name: 'Sources', rows: [['Number', 'Title', 'URL'], [1, 'Fleet report', 'https://example.com/fleet']] },
  ],
}

test('column letters', () => {
  assert.deepEqual([0, 25, 26, 27, 701, 702].map(columnLetter), ['A', 'Z', 'AA', 'AB', 'ZZ', 'AAA'])
})

test('a grid is normalized to named sheets of plain cells, as the server does', () => {
  const grid = normalizeGrid({
    title: '  Costs  ',
    sheets: [
      { name: 'Costs', rows: [['a', 1, 2.5, true, null], 'not a row', [Number.NaN, { x: 1 }, [1]]], formats: { B: '#,##0', b: '0', C: '', D: 3 } },
      'not a sheet',
      { name: 'costs', rows: [] },
      { name: ' Q1: plan / [draft]  and a name that is far too long for Excel ' },
      { rows: [[1]] },
    ],
  })
  assert.equal(grid?.title, 'Costs')
  assert.deepEqual(grid?.sheets.map((sheet) => sheet.name), ['Costs', 'Sheet2', 'Q1 plan draft and a name that i', 'Sheet4'])
  assert.deepEqual(grid?.sheets[0], { name: 'Costs', rows: [['a', 1, 2.5, true, null], [null, null, null]], formats: { B: '#,##0' } })
  assert.deepEqual(normalizeGrid(grid), grid)
  assert.deepEqual(normalizeGrid({ sheets: [{ name: 'sheet2' }, {}] })?.sheets.map((sheet) => sheet.name), ['sheet2', 'Sheet3'])
  assert.equal(normalizeGrid({ title: 'x', sheets: [] }), null)
  assert.equal(normalizeGrid([1]), null)
  assert.deepEqual(normalizeGrid({ sheets: [{ rows: [] }] }), { title: 'Spreadsheet', sheets: [{ name: 'Sheet1', rows: [] }] })
  assert.deepEqual(normalizeGrid(starterGrid()), starterGrid())
})

test('a grid reads as a fence with a row a line and parses back', () => {
  const fence = gridMarkdown(GRID)
  assert.ok(fence.startsWith('```grid\n{"title": "Fleet costs", "sheets": [\n    {"name": "Fleet", "formats": {'))
  assert.ok(fence.includes('\n      ["Van",3,1500,"=B2*C2",true],\n'))
  assert.deepEqual(gridFromJson(fence.slice('```grid\n'.length, -'\n```'.length)), GRID)
  assert.equal(gridFromJson('{not json'), null)
  assert.equal(gridFromJson('3'), null)
})

test('before a spreadsheet calculates it, a grid shows each cell as written', () => {
  assert.deepEqual(gridDisplay(GRID)[0]?.rows, [
    ['Vehicle', 'Units', 'Unit cost', 'Total', 'Electric'],
    ['Van', '3', '1500', '=B2*C2', 'TRUE'],
    ['Truck', '1.5', '2041.67', '=B3*C3', 'FALSE'],
    ['', '=SUM(B2:B3)', '', '=SUM(D2:D3)', ''],
  ])
})

test('a grid opens as a workbook: formulas, a bold frozen header, column formats on the data cells', () => {
  const workbook = gridWorkbook(GRID)
  assert.deepEqual(workbook.sheetOrder, ['sheet-1', 'sheet-2'])
  const fleet = workbook.sheets?.['sheet-1']
  assert.equal(fleet?.name, 'Fleet')
  assert.deepEqual(fleet?.freeze, { xSplit: 0, ySplit: 1, startRow: 1, startColumn: -1 })
  assert.deepEqual(fleet?.cellData?.[0]?.[2], { v: 'Unit cost', s: 'header' })
  assert.deepEqual(fleet?.cellData?.[1]?.[2], { v: 1500, s: 'format-$#,##0.00' })
  assert.deepEqual(fleet?.cellData?.[1]?.[3], { f: '=B2*C2', s: 'format-$#,##0.00' })
  assert.deepEqual(fleet?.cellData?.[1]?.[4], { v: true })
  assert.equal(fleet?.cellData?.[3]?.[0], undefined)
  assert.deepEqual(workbook.styles, { header: { bl: 1 }, 'format-$#,##0.00': { n: { pattern: '$#,##0.00' } } })
  assert.equal(fleet?.rowCount, 54)
  assert.equal(fleet?.columnCount, 11)
  // A column is as wide as its longest text: a formula's text before a spreadsheet has calculated it, its result after.
  assert.equal(fleet?.columnData?.[3]?.w, 112)
  const shown = gridDisplay(GRID).map((sheet) => ({ ...sheet, rows: sheet.rows.map((row) => row.map((cell) => (cell.startsWith('=') ? '$7,562.51' : cell))) }))
  assert.equal(gridWorkbook(GRID, shown).sheets?.['sheet-1']?.columnData?.[3]?.w, 96)
})

test('what the spreadsheet reads back becomes the grid, its formulas\' results and what it shows', () => {
  const money = '$#,##0.00'
  const read = readGrid('Fleet costs', [{
    name: 'Fleet',
    formulas: [['', '', '', '', ''], ['', '', '', '=B2*C2', ''], ['', '', '', '=B3*C3', ''], ['', '=SUM(B2:B3)', '', '=SUM(D2:D3)', ''], ['', '', '', '', '']],
    values: [['Vehicle', 'Units', 'Unit cost', 'Total', ''], ['Van', 3, 1500, 4500, undefined], ['Truck', 1.5, 2041.67, 3062.505, null], ['', 4.5, null, 7562.505, null], [null, null, null, null, null]],
    formats: [['', '', '', '', ''], ['', '', money, money, ''], ['', '', money, money, ''], ['', '', '', money, ''], ['', '', '', '', '']],
    display: [['Vehicle', 'Units', 'Unit cost', 'Total', ''], ['Van', '3', '$1,500.00', '$4,500.00', ''], ['Truck', '1.5', '$2,041.67', '$3,062.51', ''], ['', '4.5', '', '$7,562.51', ''], ['', '', '', '', '']],
  }])
  assert.deepEqual(read.grid, {
    title: 'Fleet costs',
    sheets: [{
      name: 'Fleet',
      rows: [['Vehicle', 'Units', 'Unit cost', 'Total'], ['Van', 3, 1500, '=B2*C2'], ['Truck', 1.5, 2041.67, '=B3*C3'], [null, '=SUM(B2:B3)', null, '=SUM(D2:D3)']],
      formats: { C: money, D: money },
    }],
  })
  assert.deepEqual(read.values, [[['Vehicle', 'Units', 'Unit cost', 'Total'], ['Van', 3, 1500, 4500], ['Truck', 1.5, 2041.67, 3062.505], ['', 4.5, null, 7562.505]]])
  assert.deepEqual(read.display[0]?.rows[3], ['', '4.5', '', '$7,562.51'])
  assert.deepEqual(normalizeGrid(read.grid), read.grid)
  const empty = readGrid('  ', [{ name: 'Sheet1', formulas: [['']], values: [[null]], formats: [['']], display: [['']] }])
  assert.deepEqual(empty.grid, { title: 'Spreadsheet', sheets: [{ name: 'Sheet1', rows: [] }] })
})

test('a block holds a grid, and what it shows and its workbook only as a spreadsheet wrote them', () => {
  const display = gridDisplay(GRID)
  const workbook = { id: 'grid', sheetOrder: ['sheet-1'], sheets: { 'sheet-1': {} } }
  assert.deepEqual(gridValue(GRID, display, workbook), { grid: GRID, display, workbook })
  assert.deepEqual(gridValue(GRID, null, null), { grid: GRID, display: null, workbook: null })
  // What it shows is for other sheets, or isn't sheets of text; the workbook isn't one.
  assert.deepEqual(gridValue(GRID, display.slice(1), { sheets: [] }), { grid: GRID, display: null, workbook: null })
  assert.deepEqual(gridValue(GRID, [{ name: 'Fleet', rows: [[1]] }, { name: 'Sources', rows: [] }], 'workbook'), { grid: GRID, display: null, workbook: null })
  assert.equal(gridValue({ sheets: [] }, display, workbook), null)
  assert.equal(gridValue(null, null, null), null)
})

test('a sheet as CSV quotes what needs it', () => {
  assert.equal(sheetCsv([['Name', 'Note'], ['Van, long', 'said "ok"'], ['Truck', 'line\nbreak']]), 'Name,Note\r\n"Van, long","said ""ok"""\r\nTruck,"line\nbreak"')
})

test('a sheet\'s columns are typed from its header and its cells, as AppSheet reads a sheet', () => {
  const sheet: GridSheet = {
    name: 'Leads',
    formats: { C: '$#,##0.00', D: '0.0%', E: 'yyyy-mm-dd', K: 'h:mm' },
    rows: [
      ['Name', 'Units', 'Price', 'Share', 'Due', 'Email', 'Phone', 'Site', 'Photo', 'Office address', 'Opens', 'Won', 'Stage', 'Notes', 'Total', 'Mixed', null],
      ['Acme', 3, 1500, 0.25, 46347, 'ann@acme.com', '+1 415 555 0100', 'https://acme.com', 'https://acme.com/logo.png', '1 Main St, Springfield', 0.375, true, 'New', 'Met at the fair.', '=B2*C2', 1],
      ['Bolt', 1, 20.5, 0.5, 46348, 'bo@bolt.io', '(415) 555-0101', 'https://bolt.io/about', 'https://bolt.io/a.JPG?x=1', '2 Oak Ave', 0.5, false, 'Won', 'A first line\nand a second', '=B3*C3', 'one'],
      ['Core', 2, 9, 0.1, null, 'c@core.dev', '+44 20 7946 0958', 'http://core.dev', 'https://core.dev/c.webp', '3 Elm Rd', 0.75, true, 'New', '', '=B4*C4', null],
    ],
  }
  const values = sheet.rows.map((row, r) => row.map((cell, c) => (r > 0 && c === 14 ? [4500, 20.5, 18][r - 1] ?? null : cell)))
  const columns = gridColumns(sheet, values)
  assert.deepEqual(columns.map((column) => column.type), [
    'text', 'number', 'price', 'percent', 'date', 'email', 'phone', 'url', 'image', 'address', 'time', 'yesno', 'enum', 'longtext', 'number', 'text', 'text',
  ])
  assert.deepEqual(columns[12], { name: 'Stage', type: 'enum', options: ['New', 'Won'] })
  // A column without a header goes by its letter.
  assert.equal(columns[16]?.name, 'Q')
  // Before a spreadsheet has calculated it, a column of formulas has no values to type it by.
  assert.equal(gridColumns(sheet)[14]?.type, 'text')
  assert.deepEqual(gridColumns({ name: 'Empty', rows: [] }), [])
})

test('dates, times and places written as text are typed too, and a number\'s format tells money from a date', () => {
  const typed = (cells: (string | number)[], format?: string) =>
    gridColumns({ name: 'S', rows: [['Column'], ...cells.map((cell) => [cell])], ...(format ? { formats: { A: format } } : {}) })[0]?.type
  assert.equal(typed(['2026-11-21', '2026-12-01']), 'date')
  assert.equal(typed(['2026-11-21 09:30', '2026-12-01T18:00:05']), 'datetime')
  assert.equal(typed(['9:30', '18:00']), 'time')
  assert.equal(typed(['37.7749, -122.4194', '-33.86,151.21']), 'latlong')
  assert.equal(typed(['Open', 'Open', 'Closed', 'Open']), 'enum')
  assert.equal(typed(['A-1', 'A-2', 'A-3']), 'text')
  assert.equal(typed([46347], '[$-409]d-mmm-yy'), 'date')
  assert.equal(typed([46347.5], 'yyyy-mm-dd hh:mm'), 'datetime')
  assert.equal(typed([12], '#,##0.00 [$€-407]'), 'price')
  assert.equal(typed([12], '#,##0 "days"'), 'number')
})

test('a date and a time go between an input\'s text and the number a spreadsheet keeps', () => {
  assert.equal(temporalSerial('date', '2026-11-21'), 46347)
  assert.equal(temporalText('date', 46347), '2026-11-21')
  assert.equal(temporalSerial('time', '09:00'), 0.375)
  assert.equal(temporalText('time', 0.375), '09:00')
  assert.equal(temporalText('time', 46347.75), '18:00')
  const moment = temporalSerial('datetime', '2026-11-21T09:30') ?? 0
  assert.ok(Math.abs(moment - (46347 + 9.5 / 24)) < 1e-9)
  assert.equal(temporalText('datetime', moment), '2026-11-21T09:30')
  assert.equal(temporalSerial('datetime', '2026-11-21 09:30'), moment)
  assert.equal(temporalSerial('date', 'soon'), null)
})

test('a date cell falls on a day, whether a spreadsheet keeps it as a number or as text', () => {
  assert.equal(cellDay(46347), '2026-11-21')
  assert.equal(cellDay(46347.75), '2026-11-21')
  assert.equal(cellDay('2026-11-21'), '2026-11-21')
  assert.equal(cellDay('2026-11-21 09:30'), '2026-11-21')
  assert.equal(cellDay('soon'), null)
  assert.equal(cellDay(true), null)
  assert.equal(cellDay(null), null)
  assert.equal(cellDay(1e20), null)
})

test('a sheet gets a view for each kind of data it holds, and a dashboard of two or more', () => {
  const columns = (...types: GridColumn['type'][]): GridColumn[] => types.map((type) => ({ name: type, type, options: [] }))
  assert.deepEqual(rowViews(columns('text', 'email', 'phone')), ['cards'])
  assert.deepEqual(rowViews(columns('text', 'price')), ['cards', 'chart'])
  assert.deepEqual(rowViews(columns('text', 'enum', 'date', 'address', 'image')), ['cards', 'chart', 'calendar', 'map', 'gallery', 'dashboard'])
  assert.deepEqual(rowViews(columns('text', 'latlong', 'datetime')), ['cards', 'calendar', 'map', 'dashboard'])
  assert.deepEqual(rowViews(columns('time', 'yesno', 'url', 'longtext')), ['cards'])
})
