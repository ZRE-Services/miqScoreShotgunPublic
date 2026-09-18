import os
import sys
from pathlib import Path

import pytest
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lot_editor  # noqa: E402
import lotstore  # noqa: E402

BASE = lotstore.load_base_reference()["expectedValues"]["Genomic"]
KEYS = list(BASE)
OTHER = dict(BASE, paeruginosa=11, ecoli=13)


@pytest.fixture
def store(tmp_path):
    return lotstore.LotStore(tmp_path / "lots")


@pytest.fixture
def sheet(store):
    columns = [lot_editor.Column("Set 0", ["default"], BASE), lot_editor.Column("Set 1", ["111"], OTHER)]
    return lot_editor.Sheet("standard", "999", columns)


def type_text(sheet, text):
    for char in text:
        sheet.type(char)


def test_starts_on_first_organism_with_default_values(sheet):
    assert sheet.current == KEYS[0]
    assert sheet.save() == {key: float(value) for key, value in BASE.items()}


def test_typing_overwrites_and_enter_moves_down(sheet):
    type_text(sheet, "11,5")
    sheet.enter()
    assert sheet.cells[KEYS[0]] == "11,5"
    assert sheet.current == KEYS[1]


def test_escape_drops_unfinished_edit(sheet):
    type_text(sheet, "7")
    assert sheet.escape()
    assert sheet.cells[KEYS[0]] == "12"
    assert not sheet.escape()


def test_values_can_be_corrected_after_moving_on(sheet):
    type_text(sheet, "99")
    sheet.move(rows=1)
    sheet.move(rows=-1)
    sheet.enter()
    sheet.backspace()
    sheet.enter()
    assert sheet.cells[KEYS[0]] == "9"


def test_lot_row_is_not_editable(sheet):
    sheet.move(rows=-1)
    assert sheet.current == lot_editor.LOTS
    type_text(sheet, "5")
    assert sheet.editing is None


def test_copy_cell_and_column_from_other_lot(sheet):
    sheet.move(cols=5)
    assert sheet.col == 2  # stops at the last column
    sheet.enter()
    assert sheet.cells[KEYS[0]] == "11"
    sheet.move(rows=1)
    sheet.type("C")
    assert {key: float(sheet.cells[key]) for key in KEYS} == OTHER


def test_save_reports_bad_rows_and_moves_cursor(sheet):
    sheet.move(rows=2)
    sheet.delete()
    sheet.move(rows=1)
    type_text(sheet, "0")
    sheet.move(rows=-3)
    assert sheet.save() is None
    assert sheet.errors == {KEYS[2]: "missing", KEYS[3]: "must be > 0"}
    assert sheet.current == KEYS[2]


def test_save_reports_wrong_sum_and_rescale_fixes_it(sheet):
    type_text(sheet, "22")
    assert sheet.save() is None
    assert "add up to 110" in sheet.problems[0]
    sheet.rescale()
    values = sheet.save()
    assert abs(lotstore.genomic_sum(values) - 100) <= lotstore.SUM_TOLERANCE


def test_parse_value_accepts_comma_and_percent():
    assert lot_editor.parse_value(" 12,5 % ") == (12.5, None)
    assert lot_editor.parse_value("nan") == (None, "not a number")


def test_comparison_columns_show_newest_lots_first(store):
    store.write(lotstore.ValueSet(index=0, genomic=dict(BASE)))
    for lot in ["a", "b", "c", "d"]:
        created = store.create(lot, BASE)
        os.utime(store.path_for(created.index), (1000 + created.index, 1000 + created.index))
    os.utime(store.path_for(2), (5000, 5000))
    titles = [column.title for column in lot_editor.comparison_columns(store, "standard")]
    assert titles == ["Set 0", "Set 2", "Set 4", "Set 3"]


def test_app_runs_with_keystrokes(store):
    sheet = lot_editor.Sheet("standard", "999", lot_editor.comparison_columns(store, "standard"))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("13\r")      # P. aeruginosa: 13, then Enter confirms and moves down
        pipe.send_text("11\r")      # E. coli: 11
        pipe.send_text("\x13")      # Ctrl-S
        result = lot_editor.build_app(sheet).run()
    assert result == {key: float(value) for key, value in dict(BASE, paeruginosa=13, ecoli=11).items()}
