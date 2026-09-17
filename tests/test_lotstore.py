import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lotstore  # noqa: E402

BASE = lotstore.load_base_reference()["expectedValues"]["Genomic"]


@pytest.fixture
def store(tmp_path):
    return lotstore.LotStore(tmp_path / "lots")


def test_default_values_reproduce_base_reference():
    value_set = lotstore.ValueSet(name="default", genomic=dict(BASE))
    assert value_set.build_reference() == lotstore.load_base_reference()


def test_committed_default_set_matches_base_reference():
    default = lotstore.LotStore().get("default")
    assert default.build_reference() == lotstore.load_base_reference()


def test_bacteria_only_drops_yeasts_and_rescales():
    assert lotstore.derive_bacteria_only(BASE) == {key: 12.5 for key in BASE if key not in lotstore.YEASTS}


def test_build_reference_does_not_touch_other_methods():
    genomic = dict(BASE, paeruginosa=13, ecoli=11)
    reference = lotstore.ValueSet(name="x", genomic=genomic).build_reference()
    base = lotstore.load_base_reference()
    assert reference["expectedValues"]["Genomic"] == genomic
    assert reference["expectedValues"]["16s"] == base["expectedValues"]["16s"]
    assert reference["printNames"] == base["printNames"]
    assert lotstore.load_base_reference()["expectedValues"]["Genomic"] == BASE


@pytest.mark.parametrize("genomic", [
    dict(BASE, paeruginosa=13),
    dict(BASE, paeruginosa=0, ecoli=24),
    {k: v for k, v in BASE.items() if k != "ecoli"},
    dict(BASE, unknown=0.0),
])
def test_invalid_genomic_values_are_rejected(genomic):
    with pytest.raises(lotstore.LotError):
        lotstore.validate_genomic(genomic)


def test_rescale_exact_sums_to_100():
    values = dict(BASE, paeruginosa=12.3, ecoli=12.1)
    rescaled = lotstore.rescale_to_100(values, exact=True)
    lotstore.validate_genomic(rescaled)


def test_create_and_find_by_any_lot(store):
    store.create("setA", "238717", BASE)
    store.link_lot(store.get("setA"), "252193")
    assert store.find_by_lot("238717").name == "setA"
    assert store.find_by_lot(" 252193 ").name == "setA"
    assert store.find_by_lot("999999") is None
    saved = json.loads(store.path_for("setA").read_text())
    assert saved["lot_numbers"] == ["238717", "252193"]
    assert saved["expectedValues"]["GenomicBacteriaOnly"]["ecoli"] == 12.5


def test_lot_number_cannot_belong_to_two_sets(store):
    store.create("setA", "111", BASE)
    store.create("setB", "222", BASE)
    with pytest.raises(lotstore.LotError):
        store.link_lot(store.get("setB"), "111")
    assert store.get("setB").lot_numbers == ["222"]


def test_duplicate_set_name_is_rejected(store):
    store.create("setA", "111", BASE)
    with pytest.raises(lotstore.LotError):
        store.create("setA", "222", BASE)


def test_linking_existing_lot_is_a_no_op(store):
    store.create("setA", "111", BASE)
    assert store.link_lot(store.get("setA"), "111").lot_numbers == ["111"]


@pytest.mark.parametrize("name", ["", "../evil", "a/b", ".hidden", "with space"])
def test_unsafe_names_are_rejected(store, name):
    with pytest.raises(lotstore.LotError):
        store.create(name, "111", BASE)


def test_write_reference(tmp_path):
    path = lotstore.ValueSet(name="setA", genomic=dict(BASE)).write_reference(tmp_path / "working")
    assert path.name == "reference_setA.json"
    assert json.loads(path.read_text()) == lotstore.load_base_reference()


def test_committed_lot_files_are_valid():
    store = lotstore.LotStore()
    sets, problems = store.check()
    assert problems == []
    assert store.duplicate_lots(sets) == {}


def test_check_reports_each_bad_file(store):
    store.create("good", "1", BASE)
    store.lots_dir.joinpath("broken.json").write_text("{")
    store.lots_dir.joinpath("renamed.json").write_text(store.path_for("good").read_text())
    data = json.loads(store.path_for("good").read_text())
    data["name"] = "zero"
    data["lot_numbers"] = ["2"]
    data["expectedValues"]["Genomic"]["ecoli"] = 0
    store.path_for("zero").write_text(json.dumps(data))
    sets, problems = store.check()
    assert [s.name for s in sets] == ["good"]
    assert [name for name, _ in problems] == ["broken.json", "renamed.json", "zero.json"]


def test_check_reports_stale_bacteria_only(store):
    store.create("a", "1", BASE)
    data = json.loads(store.path_for("a").read_text())
    data["expectedValues"]["Genomic"] = dict(BASE, paeruginosa=13, ecoli=11)
    store.path_for("a").write_text(json.dumps(data))
    sets, problems = store.check()
    assert problems == [("a.json", lotstore.BACTERIA_ONLY_MISMATCH)]
    store.write(sets[0])
    assert store.check()[1] == []


def test_duplicate_lots_and_remove_lot(store):
    store.create("a", "1", BASE)
    store.create("b", "2", BASE)
    data = json.loads(store.path_for("b").read_text())
    data["lot_numbers"] = ["2", "1"]
    store.path_for("b").write_text(json.dumps(data))
    sets, _ = store.check()
    assert store.duplicate_lots(sets) == {"1": ["a", "b"]}
    store.remove_lot(store.get("b"), "1")
    assert store.get("b").lot_numbers == ["2"]
    assert store.duplicate_lots(store.check()[0]) == {}
