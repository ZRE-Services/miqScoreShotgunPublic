"""Table editor for a new lot's Genomic expected values, shown next to the defaults and recent lots.

Sheet holds the editor state and key actions without terminal code; edit_values() runs it as a prompt_toolkit app.
"""

import math
from dataclasses import dataclass

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.styles import Style

import lotstore

MAX_COMPARED_LOTS = 3
NAME = "_name"  # row keys; organism keys never start with "_"
LOTS = "_lots"
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
        "On another column: Enter copies the cell, Shift-C copies the whole column\n"
        "Ctrl-S save · Ctrl-R rescale to 100 · Esc cancel edit / quit\n"
        "Values in %, each > 0, together 100. '12,5' and '12.5 %' are accepted.")


@dataclass
class Column:
    title: str
    lots: list
    values: dict


def comparison_columns(store, product, count=MAX_COMPARED_LOTS):
    """The base reference values, then the most recently changed value sets."""
    base = lotstore.load_base_reference(product)["expectedValues"]["Genomic"]
    sets = [s for s in store.value_sets() if s.name != "default" and s.product == product]
    return [Column("default", [], base)] + [Column(s.name, s.lot_numbers, s.genomic) for s in store.newest_first(sets)[:count]]


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

    def __init__(self, store, product, lot_number, name, columns, values=None):
        self.store = store
        self.lot_number = lot_number
        self.columns = columns
        self.keys = lotstore.organisms(product)
        self.labels = dict(lotstore.print_names(product), **{NAME: "Value set name", LOTS: "Lot number"})
        self.rows = [NAME, LOTS] + self.keys
        self.default = columns[0].values
        start = values or self.default
        self.cells = {NAME: name, **{key: fmt(start[key]) for key in self.keys}}
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
        if row == NAME:
            return column.title
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
        if self.editable():
            if self.editing is None:
                self.editing = self.cells[self.current]
            else:
                self.move(1)
        elif self.col > 0 and self.current in self.keys:
            self.cells[self.current] = self.text(self.current, self.col)
            self.note = f"Copied {self.labels[self.current]} from '{self.columns[self.col - 1].title}'"

    def escape(self):
        """Drops an unfinished edit. Returns False if there was none, i.e. the editor should close."""
        if self.editing is None:
            return False
        self.editing = None
        return True

    def copy_column(self):
        column = self.columns[self.col - 1]
        self.cells.update({key: fmt(column.values[key]) for key in self.keys})
        self.note = f"Copied all values from '{column.title}'. Now change the ones that differ."

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
        """Checks everything. Returns (name, values) if they can be saved, else moves the cursor to the first problem."""
        self.commit()
        self.note = ""
        values, self.errors = self.parsed()
        self.problems = []
        try:
            name = lotstore.check_name(self.cells[NAME], "value set name")
            if self.store.path_for(name).exists():
                self.errors[NAME] = f"'{name}' already exists"
        except lotstore.LotError as err:
            self.errors[NAME] = str(err)
        if len(values) == len(self.keys):
            total = lotstore.genomic_sum(values)
            if abs(total - 100) > lotstore.SUM_TOLERANCE:
                self.problems.append(f"The values add up to {total:g}, but must add up to 100. Fix them or press Ctrl-R to rescale.")
        if not self.errors and not self.problems:
            return name, values
        if self.errors:
            self.row, self.col = next(i for i, row in enumerate(self.rows) if row in self.errors), 0
        return None


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
            out += [(style, f"{sheet.text(row, col)[:COL_W - 2]:>{COL_W - 1}}"), ("", " ")]
        if row in sheet.errors:
            out.append(("class:error", f" <- {sheet.errors[row]}"))
        out.append(("", "\n"))
    out.append(("", "\n"))
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
    kb.add("enter")(lambda event: sheet.enter())
    kb.add("backspace")(lambda event: sheet.backspace())
    kb.add("delete")(lambda event: sheet.delete())
    kb.add("c-r")(lambda event: sheet.rescale())
    kb.add("c-c")(lambda event: event.app.exit(result=None))

    @kb.add("c-s")
    def save(event):
        result = sheet.save()
        if result:
            event.app.exit(result=result)

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


def edit_values(store, product, lot_number, name, values=None):
    """Returns (value set name, Genomic values) or None if the user cancelled."""
    sheet = Sheet(store, product, lot_number, name, comparison_columns(store, product), values)
    return build_app(sheet).run()
