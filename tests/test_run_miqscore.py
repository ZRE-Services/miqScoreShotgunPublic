import sys
from pathlib import Path

import pytest
import questionary

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lot_editor  # noqa: E402
import lotstore  # noqa: E402
import run_miqscore  # noqa: E402

BASE = lotstore.load_base_reference()["expectedValues"]["Genomic"]
OTHER = dict(BASE, paeruginosa=11, ecoli=13)


@pytest.fixture
def store(tmp_path):
    store = lotstore.LotStore(tmp_path / "lots")
    store.create("111", OTHER)
    return store


def script(monkeypatch, editor_results, answers):
    """Replaces the editor and the questionary prompts with the given results, in order."""
    editor_results, answers = list(editor_results), list(answers)
    monkeypatch.setattr(lot_editor, "run", lambda sheet: editor_results.pop(0))
    monkeypatch.setattr(questionary, "select", lambda *args, **kwargs: None)
    monkeypatch.setattr(questionary, "confirm", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_miqscore, "ask", lambda question, erase=False: answers.pop(0))
    return editor_results, answers


def test_link_from_editor(store, monkeypatch):
    script(monkeypatch, [(lot_editor.LINK, 1)], [True])
    assert run_miqscore.create_value_set(store, "999").index == 1
    assert store.get(1).lot_numbers == ["111", "999"]


def test_declined_link_returns_to_editor(store, monkeypatch):
    script(monkeypatch, [(lot_editor.LINK, 1), None], [False])
    assert run_miqscore.create_value_set(store, "999") is None
    assert store.get(1).lot_numbers == ["111"]


def test_identical_values_offer_link(store, monkeypatch):
    script(monkeypatch, [(lot_editor.SAVE, dict(OTHER))], [1])
    assert run_miqscore.create_value_set(store, "999").index == 1
    assert store.find_by_lot("999").index == 1
    assert store.next_index() == 2


def test_identical_values_can_still_be_saved_as_new_set(store, monkeypatch):
    script(monkeypatch, [(lot_editor.SAVE, dict(OTHER))], ["new", "save"])
    assert run_miqscore.create_value_set(store, "999").index == 2


def test_different_values_are_saved_without_asking_about_links(store, monkeypatch):
    _, answers = script(monkeypatch, [(lot_editor.SAVE, dict(BASE))], ["save"])
    assert run_miqscore.create_value_set(store, "999").index == 2
    assert answers == []
