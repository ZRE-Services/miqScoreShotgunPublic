"""Table editor for a new lot's Genomic expected values, shown next to the defaults and recent lots.

Sheet holds the editor state and key actions without terminal code; run() shows it as a prompt_toolkit app.
The same Sheet can be run again, so the user returns to their values and cursor.
"""

import math
import re
from dataclasses import dataclass

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.styles import Style

import lotstore

MAX_COMPARED_LOTS = 3
LOTS = "_lots"  # row key; organism keys never start with "_"
SAVE = "save"  # run() results
LINK = "link"
LABEL_W = 26
NEW_W = 14
COL_W = 13

STYLE = Style.from_dict({
    "title": "bold",
    "header": "bold underline",
    "new": "bg:#303030 #ffffff",
    "changed": "bg:#303030 #ffd75f",
    "fixed": "#8a8a8a",
    "compare": "#8a8a8a",
    "cursor": "reverse bold",
    "editing": "bg:#005f87 #ffffff bold",
    "error": "#ff5f5f bold",
    "note": "#5fd787",
    "help": "#8a8a8a italic",
})

HELP = ("←↑↓→ move · type to overwrite a New cell · Enter edit/confirm · Del clear\n"
        "Paste a column of values (one per line, or a tab-separated row) to fill New downwards from the cursor\n"
        "On another set: Enter on a value copies it, Enter on its Lot number row links your lot to that set,\n"
        "Shift-C copies the whole column\n"
        "Ctrl-S save · Ctrl-R rescale to 100 · Esc cancel edit / quit\n"
        "Values in %, each > 0, together 100. '12,5' and '12.5 %' are accepted.")


@dataclass
class Column:
    title: str
    lots: list
    values: dict
    index: int


def comparison_columns(store, product, count=MAX_COMPARED_LOTS):
    """The base reference values (set 0), then the most recently changed value sets."""
    base = lotstore.load_base_reference(product)["expectedValues"]["Genomic"]
    sets = [s for s in store.value_sets() if s.index != lotstore.DEFAULT_INDEX and s.product == product]
    columns = [Column(f"Set {lotstore.DEFAULT_INDEX}", ["default"], base, lotstore.DEFAULT_INDEX)]  # the full label is too wide
    return columns + [Column(s.label, s.lot_numbers, s.genomic, s.index) for s in store.newest_first(sets)[:count]]


def fmt(value):
    return "" if value is None else f"{value:g}"


def parse_value(text):
    """Returns (value, None) or (None, problem)."""
    cleaned = text.strip().rstrip("%").strip().replace(",", ".")
    if not cleaned:
        return None, "missing"
    try:
        value = float(cleaned)
    except ValueError:
        return None, "not a number"
    if not math.isfinite(value):
        return None, "not a number"
    if value <= 0:
        return None, "must be > 0"
    return value, None


class Sheet:
    """Cursor, cell texts and messages of the editor. Column 0 is the new value set, the others are read-only."""

    def __init__(self, product, lot_number, columns):
        self.lot_number = lot_number
        self.columns = columns
        self.keys = lotstore.organisms(product)
        self.labels = dict(lotstore.print_names(product), **{LOTS: "Lot number"})
        self.rows = [LOTS] + self.keys
        self.default = columns[0].values
        self.cells = {key: fmt(self.default[key]) for key in self.keys}
        self.row, self.col = self.rows.index(self.keys[0]), 0
        self.editing = None
        self.errors = {}  # row key -> problem, shown next to the row
        self.problems = []  # messages that belong to no single row
        self.note = ""

    @property
    def current(self):
        return self.rows[self.row]

    def text(self, row, col):
        if col == 0:
            return self.lot_number if row == LOTS else self.cells[row]
        column = self.columns[col - 1]
        if row == LOTS:
            return ", ".join(column.lots) or "-"
        return fmt(column.values.get(row))

    def editable(self):
        return self.col == 0 and self.current != LOTS

    def commit(self):
        if self.editing is not None:
            self.cells[self.current] = self.editing.strip()
            self.editing = None

    def move(self, rows=0, cols=0):
        self.commit()
        self.row = max(0, min(len(self.rows) - 1, self.row + rows))
        self.col = max(0, min(len(self.columns), self.col + cols))
        self.note = ""

    def type(self, char):
        if self.editable():
            self.editing = (self.editing or "") + char
        elif self.col > 0 and char == "C":
            self.copy_column()

    def backspace(self):
        if self.editable():
            self.editing = (self.cells[self.current] if self.editing is None else self.editing)[:-1]

    def delete(self):
        if self.editable():
            self.editing = None
            self.cells[self.current] = ""

    def enter(self):
        """Returns the index of the set to link the lot to when Enter is pressed on a set's lot number row."""
        if self.col > 0 and self.current == LOTS:
            return self.columns[self.col - 1].index
        if self.editable():
            if self.editing is None:
                self.editing = self.cells[self.current]
            else:
                self.move(1)
        elif self.col > 0 and self.current in self.keys:
            self.cells[self.current] = self.text(self.current, self.col)
            self.note = f"Copied {self.labels[self.current]} from {self.columns[self.col - 1].title}"

    def line_feed(self):
        """A paste the terminal did not mark as one (common over SSH) arrives as typing, with lines ending in CR
        (handled as Enter) or LF. LF confirms like Enter but never opens a cell, so CR LF stays one line end."""
        if self.editing is not None:
            self.move(1)

    def escape(self):
        """Drops an unfinished edit. Returns False if there was none, i.e. the editor should close."""
        if self.editing is None:
            return False
        self.editing = None
        return True

    def paste(self, text):
        """One value is typed into the current cell; several fill the New column downwards from the cursor."""
        values = [part.strip() for part in re.split(r"[\r\n\t;]+", text) if part.strip()]
        if not values:
            return
        if self.col > 0:
            self.note = "Paste into the New column"
            return
        if len(values) == 1 and self.editable():
            self.editing = (self.editing or "") + values[0]
            return
        self.commit()
        start = max(self.row, self.rows.index(self.keys[0]))
        targets = self.rows[start:start + len(values)]
        self.cells.update(zip(targets, values))
        self.row = self.rows.index(targets[-1])
        self.note = f"Pasted {len(targets)} values, {self.labels[targets[0]]} to {self.labels[targets[-1]]}"
        left_out = len(values) - len(targets)
        if left_out:
            self.note += f". {left_out} more did not fit and were left out."

    def copy_column(self):
        column = self.columns[self.col - 1]
        self.cells.update({key: fmt(column.values[key]) for key in self.keys})
        self.note = f"Copied all values from {column.title}. Now change the ones that differ."

    def parsed(self):
        values, errors = {}, {}
        for key in self.keys:
            value, error = parse_value(self.cells[key])
            if error:
                errors[key] = error
            else:
                values[key] = value
        return values, errors

    def rescale(self):
        self.commit()
        values, self.errors = self.parsed()
        if self.errors:
            self.note = ""
            return
        rescaled = lotstore.rescale_to_100(values, exact=True)
        self.cells.update({key: fmt(value) for key, value in rescaled.items()})
        self.problems = []
        self.note = f"Rescaled from {lotstore.genomic_sum(values):g} to 100. Check the values against the certificate."

    def save(self):
        """Checks everything. Returns the values if they can be saved, else moves the cursor to the first problem."""
        self.commit()
        self.note = ""
        values, self.errors = self.parsed()
        self.problems = []
        if not self.errors:
            total = lotstore.genomic_sum(values)
            if abs(total - 100) > lotstore.SUM_TOLERANCE:
                self.problems.append(f"The values add up to {total:g}, but must add up to 100. Fix them or press Ctrl-R to rescale.")
        if not self.errors and not self.problems:
            return values
        if self.errors:
            self.row, self.col = next(i for i, row in enumerate(self.rows) if row in self.errors), 0
        return None


def clip(text, width):
    return text if len(text) <= width else text[:width - 1] + "…"


def render(sheet):
    out = [("class:title", f"  New expected values (Genomic, %) for lot {sheet.lot_number}\n\n"),
           ("", "  " + " " * LABEL_W), ("class:header", "New".ljust(NEW_W - 1)), ("", " ")]
    for column in sheet.columns:
        out += [("class:header", f"{column.title[:COL_W - 2]:>{COL_W - 1}}"), ("", " ")]
    out.append(("", "\n"))
    for index, row in enumerate(sheet.rows):
        if row == sheet.keys[0]:
            out.append(("", "\n"))
        here = index == sheet.row
        out.append(("class:error" if row in sheet.errors else "", f"  {sheet.labels[row]:<{LABEL_W}}"))
        if here and sheet.col == 0 and sheet.editing is not None:
            out.append(("class:editing", f"{(sheet.editing + '▏')[-(NEW_W - 1):]:<{NEW_W - 1}}"))
        else:
            if here and sheet.col == 0:
                style = "class:cursor"
            elif row == LOTS:
                style = "class:fixed"
            elif row in sheet.default and sheet.cells[row] != fmt(sheet.default[row]):
                style = "class:changed"
            else:
                style = "class:new"
            out.append((style, f"{sheet.text(row, 0)[:NEW_W - 2]:>{NEW_W - 2}} "))
        out.append(("", " "))
        for col in range(1, len(sheet.columns) + 1):
            style = "class:cursor" if here and sheet.col == col else "class:compare"
            out += [(style, f"{clip(sheet.text(row, col), COL_W - 2):>{COL_W - 1}}"), ("", " ")]
        if row in sheet.errors:
            out.append(("class:error", f" <- {sheet.errors[row]}"))
        out.append(("", "\n"))
    out.append(("", "\n"))
    out += [("class:compare", f"  {column.title}: lots {', '.join(column.lots)}\n") for column in sheet.columns[1:]]
    out.append(("", "\n"))
    if sheet.col > 0 and sheet.current == LOTS:
        column = sheet.columns[sheet.col - 1]
        out.append(("class:note", f"  Enter: link lot {sheet.lot_number} to {lotstore.set_label(column.index)} instead of saving new values\n"))
    out += [("class:error", f"  {message}\n") for message in sheet.problems]
    if sheet.note:
        out.append(("class:note", f"  {sheet.note}\n"))
    out.append(("class:help", "\n" + "\n".join("  " + line for line in HELP.splitlines())))
    return out


def build_app(sheet):
    kb = KeyBindings()
    kb.add("up")(lambda event: sheet.move(rows=-1))
    kb.add("s-tab")(lambda event: sheet.move(rows=-1))
    kb.add("down")(lambda event: sheet.move(rows=1))
    kb.add("tab")(lambda event: sheet.move(rows=1))
    kb.add("left")(lambda event: sheet.move(cols=-1))
    kb.add("right")(lambda event: sheet.move(cols=1))
    kb.add("backspace")(lambda event: sheet.backspace())
    kb.add("delete")(lambda event: sheet.delete())
    kb.add("c-r")(lambda event: sheet.rescale())
    kb.add("c-c")(lambda event: event.app.exit(result=None))
    kb.add(Keys.BracketedPaste)(lambda event: sheet.paste(event.data))
    kb.add("c-j")(lambda event: sheet.line_feed())

    @kb.add("enter")
    def enter(event):
        link = sheet.enter()
        if link is not None:
            event.app.exit(result=(LINK, link))

    @kb.add("c-s")
    def save(event):
        values = sheet.save()
        if values:
            event.app.exit(result=(SAVE, values))

    @kb.add("escape", eager=True)
    def escape(event):
        if not sheet.escape():
            event.app.exit(result=None)

    @kb.add("<any>")
    def typed(event):
        if len(event.data) == 1 and event.data.isprintable():
            sheet.type(event.data)

    control = FormattedTextControl(lambda: render(sheet), focusable=True, key_bindings=kb, show_cursor=False)
    return Application(layout=Layout(HSplit([Window(control)])), style=STYLE, erase_when_done=True)


def new_sheet(store, product, lot_number):
    return Sheet(product, lot_number, comparison_columns(store, product))


def run(sheet):
    """Returns (SAVE, Genomic values), (LINK, set index), or None if the user cancelled."""
    return build_app(sheet).run()
