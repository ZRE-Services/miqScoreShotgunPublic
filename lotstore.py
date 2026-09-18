"""Storage and reference-file generation for lot-specific expected values."""

import copy
import json
import math
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
LOTS_DIR = REPO_ROOT / "lots"
BASE_REFERENCES = {"standard": REPO_ROOT / "reference" / "zrCommunityStandard.json"}
YEASTS = ("scerevisiae", "cneoformans")
SUM_TOLERANCE = 0.01
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SET_FILE_PATTERN = re.compile(r"^set(\d{3,})$")
DEFAULT_INDEX = 0  # set 0 holds the base reference values and covers no lot
BACTERIA_ONLY_MISMATCH = "GenomicBacteriaOnly does not match the values derived from Genomic."


class LotError(ValueError):
    pass


def load_base_reference(product: str = "standard") -> dict:
    if product not in BASE_REFERENCES:
        raise LotError(f"Unknown product '{product}'. Known: {', '.join(BASE_REFERENCES)}")
    with open(BASE_REFERENCES[product]) as handle:
        return json.load(handle)


def organisms(product: str = "standard") -> list:
    return list(load_base_reference(product)["expectedValues"]["Genomic"])


def print_names(product: str = "standard") -> dict:
    return load_base_reference(product)["printNames"]


def check_name(value: str, what: str) -> str:
    value = value.strip()
    if not NAME_PATTERN.match(value):
        raise LotError(f"Invalid {what} '{value}': use letters, digits, '.', '_' or '-', starting with a letter or digit.")
    return value


def set_file_stem(index: int) -> str:
    return f"set{index:03d}"


def set_label(index: int) -> str:
    return "Set 0 (default)" if index == DEFAULT_INDEX else f"Set {index}"


def genomic_sum(values: dict) -> float:
    return sum(values.values())


def rescale_to_100(values: dict, digits: int = 2, exact: bool = False) -> dict:
    total = genomic_sum(values)
    rescaled = {key: round(value / total * 100, digits) for key, value in values.items()}
    if exact:
        largest = max(rescaled, key=rescaled.get)
        rescaled[largest] = round(rescaled[largest] + 100 - genomic_sum(rescaled), digits)
    return rescaled


def validate_genomic(values: dict, product: str = "standard") -> None:
    if not isinstance(values, dict):
        raise LotError(f"Genomic expected values must be an object mapping organisms to numbers, got {values!r}.")
    expected = organisms(product)
    if set(values) != set(expected):
        missing = sorted(set(expected) - set(values))
        extra = sorted(set(values) - set(expected))
        raise LotError(f"Organisms do not match the {product} standard. Missing: {missing}. Unexpected: {extra}.")
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            # 0 or null would silently drop the organism from the MIQ calculation.
            raise LotError(f"Expected value for {key} must be a number greater than 0, got {value!r}.")
    total = genomic_sum(values)
    if abs(total - 100) > SUM_TOLERANCE:
        raise LotError(f"Genomic expected values must add up to 100 (the MIQ calculation does not rescale them), got {total:g}.")


def derive_bacteria_only(genomic: dict) -> dict:
    return rescale_to_100({key: value for key, value in genomic.items() if key not in YEASTS})


@dataclass
class ValueSet:
    index: int
    genomic: dict
    lot_numbers: list = field(default_factory=list)
    product: str = "standard"

    @property
    def bacteria_only(self) -> dict:
        return derive_bacteria_only(self.genomic)

    @property
    def label(self) -> str:
        return set_label(self.index)

    @property
    def lots_text(self) -> str:
        return ", ".join(self.lot_numbers) or "none"

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "lot_numbers": self.lot_numbers,
            "product": self.product,
            "expectedValues": {"Genomic": self.genomic, "GenomicBacteriaOnly": self.bacteria_only},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ValueSet":
        lot_numbers = data.get("lot_numbers", [])
        if not isinstance(lot_numbers, list):
            raise LotError(f"lot_numbers must be a list, got {lot_numbers!r}.")
        return cls(
            index=data["index"],
            genomic=data["expectedValues"]["Genomic"],
            lot_numbers=[str(lot) for lot in lot_numbers],
            product=data.get("product", "standard"),
        )

    def validate(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise LotError(f"The set index must be a whole number of 0 or more, got {self.index!r}.")
        for lot in self.lot_numbers:
            check_name(lot, "lot number")
        if len(set(self.lot_numbers)) != len(self.lot_numbers):
            raise LotError(f"{self.label} lists a lot number more than once.")
        validate_genomic(self.genomic, self.product)

    def build_reference(self) -> dict:
        reference = copy.deepcopy(load_base_reference(self.product))
        reference["expectedValues"]["Genomic"] = dict(self.genomic)
        reference["expectedValues"]["GenomicBacteriaOnly"] = self.bacteria_only
        return reference

    def write_reference(self, folder: Path) -> Path:
        self.validate()  # last guard: never hand the image values that would silently skew the score
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"reference_{set_file_stem(self.index)}.json"
        with open(path, "w") as handle:
            json.dump(self.build_reference(), handle, indent=4)
        return path


class LotStore:
    def __init__(self, lots_dir: Path = LOTS_DIR):
        self.lots_dir = Path(lots_dir)

    def path_for(self, index: int) -> Path:
        return self.lots_dir / f"{set_file_stem(index)}.json"

    def files(self) -> list:
        return sorted(self.lots_dir.glob("*.json"))

    def load(self, path: Path) -> ValueSet:
        """Loads and validates one file; raises LotError for anything that cannot be used."""
        try:
            with open(path) as handle:
                value_set = ValueSet.from_dict(json.load(handle))
        except LotError as err:
            raise LotError(f"{path.name}: {err}") from err
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
            raise LotError(f"{path.name} could not be read: {err!r}") from err
        value_set.validate()
        if path.name != self.path_for(value_set.index).name:
            raise LotError(f"{path.name} has index {value_set.index!r}; it must be named {self.path_for(value_set.index).name}.")
        return value_set

    def value_sets(self) -> list:
        """The usable value sets. Files that fail load() are left out; check() reports them."""
        return self.check()[0]

    def check(self):
        """Checks every file on its own. Returns (value sets that passed, [(file name, problem)])."""
        sets, problems = [], []
        for path in self.files():
            try:
                value_set = self.load(path)
            except LotError as err:
                problems.append((path.name, str(err)))
                continue
            with open(path) as handle:
                stored = json.load(handle)["expectedValues"].get("GenomicBacteriaOnly")
            if stored != value_set.bacteria_only:
                problems.append((path.name, BACTERIA_ONLY_MISMATCH))
            sets.append(value_set)
        return sets, problems

    def newest_first(self, sets: list) -> list:
        """Sorts by last commit time; files that were never committed count as new. File times alone say nothing after a clone."""
        try:
            log = subprocess.run(["git", "log", "--format=@%ct", "--name-only", "--", "."], cwd=self.lots_dir,
                                 capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            log = ""
        committed, stamp = {}, 0
        for line in log.splitlines():
            if line.startswith("@"):
                stamp = int(line[1:])
            elif line:
                committed.setdefault(Path(line).name, stamp)

        def changed(value_set):
            path = self.path_for(value_set.index)
            return committed.get(path.name) or path.stat().st_mtime

        return sorted(sets, key=changed, reverse=True)

    def next_index(self) -> int:
        """1 + the highest index ever used, counting files in the git history, so a deleted set's index is never reused."""
        stems = [path.stem for path in self.files()]
        try:
            log = subprocess.run(["git", "log", "--all", "--format=", "--name-only", "--", "."], cwd=self.lots_dir,
                                 capture_output=True, text=True, check=True).stdout
            stems += [Path(line).stem for line in log.splitlines() if line]
        except (OSError, subprocess.CalledProcessError):
            pass
        used = [int(match.group(1)) for match in map(SET_FILE_PATTERN.match, stems) if match]
        return max(used + [DEFAULT_INDEX]) + 1

    @staticmethod
    def duplicate_lots(sets: list) -> dict:
        """Maps each lot number listed in more than one value set to the indexes of those sets."""
        owners = {}
        for value_set in sets:
            for lot in value_set.lot_numbers:
                owners.setdefault(lot, []).append(value_set.index)
        return {lot: indexes for lot, indexes in sorted(owners.items()) if len(indexes) > 1}

    def write(self, value_set: ValueSet) -> Path:
        """Writes a validated value set without the cross-set checks of save(); used to repair the library."""
        value_set.validate()
        path = self.path_for(value_set.index)
        self.lots_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as handle:
            json.dump(value_set.to_dict(), handle, indent=2)
            handle.write("\n")
        return path

    def remove_lot(self, value_set: ValueSet, lot_number: str) -> ValueSet:
        updated = ValueSet(index=value_set.index, genomic=value_set.genomic, lot_numbers=[lot for lot in value_set.lot_numbers if lot != lot_number], product=value_set.product)
        self.write(updated)
        return updated

    def get(self, index: int):
        return next((s for s in self.value_sets() if s.index == index), None)

    def find_by_values(self, genomic: dict, product: str = "standard") -> list:
        """The value sets whose Genomic values are exactly these; a new lot with such values should be linked instead."""
        return [s for s in self.value_sets() if s.product == product and s.genomic == genomic]

    def find_by_lot(self, lot_number: str):
        lot_number = lot_number.strip()
        matches = [s for s in self.value_sets() if lot_number in s.lot_numbers]
        if len(matches) > 1:
            raise LotError(f"Lot {lot_number} is listed in several value sets: {', '.join(s.label for s in matches)}.")
        return matches[0] if matches else None

    def save(self, value_set: ValueSet, overwrite: bool = False) -> Path:
        value_set.validate()
        path = self.path_for(value_set.index)
        if path.exists() and not overwrite:
            raise LotError(f"{value_set.label} already exists.")
        for other in self.value_sets():
            if other.index == value_set.index:
                continue
            shared = set(other.lot_numbers) & set(value_set.lot_numbers)
            if shared:
                raise LotError(f"Lot number(s) {', '.join(sorted(shared))} already belong to {other.label}.")
        return self.write(value_set)

    def create(self, lot_number: str, genomic: dict, product: str = "standard") -> ValueSet:
        """Saves a new value set under the next free index."""
        value_set = ValueSet(index=self.next_index(), genomic=dict(genomic), lot_numbers=[check_name(lot_number, "lot number")], product=product)
        self.save(value_set)
        return value_set

    def link_lot(self, value_set: ValueSet, lot_number: str) -> ValueSet:
        lot_number = check_name(lot_number, "lot number")
        if lot_number in value_set.lot_numbers:
            return value_set
        updated = ValueSet(index=value_set.index, genomic=value_set.genomic, lot_numbers=value_set.lot_numbers + [lot_number], product=value_set.product)
        self.save(updated, overwrite=True)
        return updated
