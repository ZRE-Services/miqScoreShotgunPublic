import json
import subprocess
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
    value_set = lotstore.ValueSet(index=0, genomic=dict(BASE))
    assert value_set.build_reference() == lotstore.load_base_reference()


def test_committed_default_set_matches_base_reference():
    default = lotstore.LotStore().get(lotstore.DEFAULT_INDEX)
    assert default.lot_numbers == []
    assert default.build_reference() == lotstore.load_base_reference()


def test_bacteria_only_drops_yeasts_and_rescales():
    assert lotstore.derive_bacteria_only(BASE) == {key: 12.5 for key in BASE if key not in lotstore.YEASTS}


def test_build_reference_does_not_touch_other_methods():
    genomic = dict(BASE, paeruginosa=13, ecoli=11)
    reference = lotstore.ValueSet(index=1, genomic=genomic).build_reference()
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
    created = store.create("238717", BASE)
    assert created.index == 1
    store.link_lot(store.get(1), "252193")
    assert store.find_by_lot("238717").index == 1
    assert store.find_by_lot(" 252193 ").index == 1
    assert store.find_by_lot("999999") is None
    saved = json.loads(store.path_for(1).read_text())
    assert store.path_for(1).name == "set001.json"
    assert saved["index"] == 1
    assert saved["lot_numbers"] == ["238717", "252193"]
    assert saved["expectedValues"]["GenomicBacteriaOnly"]["ecoli"] == 12.5


def test_lot_number_cannot_belong_to_two_sets(store):
    store.create("111", BASE)
    store.create("222", BASE)
    with pytest.raises(lotstore.LotError):
        store.link_lot(store.get(2), "111")
    assert store.get(2).lot_numbers == ["222"]


def test_new_sets_get_the_next_index(store):
    store.write(lotstore.ValueSet(index=0, genomic=dict(BASE)))
    assert [store.create(lot, BASE).index for lot in ("111", "222", "333")] == [1, 2, 3]
    store.path_for(2).unlink()
    assert store.create("444", BASE).index == 4


def test_deleted_highest_index_is_not_reused_once_committed(store):
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(store.lots_dir)]
    store.create("111", BASE)
    store.create("222", BASE)
    subprocess.run(git[:-2] + ["init", "-q", str(store.lots_dir)], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "sets"], check=True)
    store.path_for(2).unlink()
    assert store.next_index() == 3


def test_saving_over_an_existing_index_is_rejected(store):
    store.create("111", BASE)
    with pytest.raises(lotstore.LotError):
        store.save(lotstore.ValueSet(index=1, genomic=dict(BASE), lot_numbers=["222"]))


def test_linking_existing_lot_is_a_no_op(store):
    store.create("111", BASE)
    assert store.link_lot(store.get(1), "111").lot_numbers == ["111"]


@pytest.mark.parametrize("lot", ["", "../evil", "a/b", ".hidden", "with space"])
def test_unsafe_lot_numbers_are_rejected(store, lot):
    with pytest.raises(lotstore.LotError):
        store.create(lot, BASE)


@pytest.mark.parametrize("index", [-1, "1", 1.0, True, None])
def test_invalid_indexes_are_rejected(index):
    with pytest.raises(lotstore.LotError):
        lotstore.ValueSet(index=index, genomic=dict(BASE)).validate()


def test_labels():
    assert lotstore.set_label(0) == "Set 0 (default)"
    assert lotstore.ValueSet(index=12, genomic={}).label == "Set 12"


def test_write_reference(tmp_path):
    path = lotstore.ValueSet(index=1, genomic=dict(BASE)).write_reference(tmp_path / "working")
    assert path.name == "reference_set001.json"
    assert json.loads(path.read_text()) == lotstore.load_base_reference()


def test_committed_lot_files_are_valid():
    store = lotstore.LotStore()
    sets, problems = store.check()
    assert problems == []
    assert store.duplicate_lots(sets) == {}


def test_check_reports_each_bad_file(store):
    store.create("1", BASE)
    store.lots_dir.joinpath("broken.json").write_text("{")
    store.lots_dir.joinpath("set009.json").write_text(store.path_for(1).read_text())
    data = json.loads(store.path_for(1).read_text())
    data["index"] = 2
    data["lot_numbers"] = ["2"]
    data["expectedValues"]["Genomic"]["ecoli"] = 0
    store.path_for(2).write_text(json.dumps(data))
    sets, problems = store.check()
    assert [s.index for s in sets] == [1]
    assert [name for name, _ in problems] == ["broken.json", "set002.json", "set009.json"]


def test_check_reports_stale_bacteria_only(store):
    store.create("1", BASE)
    data = json.loads(store.path_for(1).read_text())
    data["expectedValues"]["Genomic"] = dict(BASE, paeruginosa=13, ecoli=11)
    store.path_for(1).write_text(json.dumps(data))
    sets, problems = store.check()
    assert problems == [("set001.json", lotstore.BACTERIA_ONLY_MISMATCH)]
    store.write(sets[0])
    assert store.check()[1] == []


def test_duplicate_lots_and_remove_lot(store):
    store.create("1", BASE)
    store.create("2", BASE)
    data = json.loads(store.path_for(2).read_text())
    data["lot_numbers"] = ["2", "1"]
    store.path_for(2).write_text(json.dumps(data))
    sets, _ = store.check()
    assert store.duplicate_lots(sets) == {"1": [1, 2]}
    store.remove_lot(store.get(2), "1")
    assert store.get(2).lot_numbers == ["2"]
    assert store.duplicate_lots(store.check()[0]) == {}


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_non_finite_values_are_rejected(value):
    with pytest.raises(lotstore.LotError):
        lotstore.validate_genomic(dict(BASE, ecoli=value))


@pytest.mark.parametrize("change", [
    {"expectedValues": {"Genomic": None}},
    {"expectedValues": {"Genomic": 100}},
    {"lot_numbers": "A123"},
    {"expectedValues": {"Genomic": dict(BASE, ecoli=float("nan"))}},
    {"index": "1"},
    {"index": None},
])
def test_check_reports_malformed_fields(store, change):
    store.create("1", BASE)
    data = json.loads(store.path_for(1).read_text())
    data.update(change)
    store.path_for(1).write_text(json.dumps(data))
    sets, problems = store.check()
    assert sets == [] and [name for name, _ in problems] == ["set001.json"]


def test_value_sets_skip_unusable_files(store):
    store.create("1", BASE)
    store.lots_dir.joinpath("broken.json").write_text("{")
    data = json.loads(store.path_for(1).read_text())
    data.update(index=2, lot_numbers=["2"])
    data["expectedValues"]["Genomic"]["ecoli"] = 0
    store.path_for(2).write_text(json.dumps(data))
    assert [s.index for s in store.value_sets()] == [1]
    assert store.get(2) is None
    assert store.find_by_lot("2") is None


def test_write_reference_refuses_invalid_values(tmp_path):
    with pytest.raises(lotstore.LotError):
        lotstore.ValueSet(index=1, genomic=dict(BASE, ecoli=0)).write_reference(tmp_path)
    assert not list(tmp_path.iterdir())
