"""Storage and reference-file generation for lot-specific expected values."""

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
LOTS_DIR = REPO_ROOT / "lots"
BASE_REFERENCES = {"standard": REPO_ROOT / "reference" / "zrCommunityStandard.json"}
YEASTS = ("scerevisiae", "cneoformans")
SUM_TOLERANCE = 0.01
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
    expected = organisms(product)
    if set(values) != set(expected):
        missing = sorted(set(expected) - set(values))
        extra = sorted(set(values) - set(expected))
        raise LotError(f"Organisms do not match the {product} standard. Missing: {missing}. Unexpected: {extra}.")
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            # 0 or null would silently drop the organism from the MIQ calculation.
            raise LotError(f"Expected value for {key} must be a number greater than 0, got {value!r}.")
    total = genomic_sum(values)
    if abs(total - 100) > SUM_TOLERANCE:
        raise LotError(f"Genomic expected values must add up to 100 (the MIQ calculation does not rescale them), got {total:g}.")


def derive_bacteria_only(genomic: dict) -> dict:
    return rescale_to_100({key: value for key, value in genomic.items() if key not in YEASTS})


@dataclass
class ValueSet:
    name: str
    genomic: dict
    lot_numbers: list = field(default_factory=list)
    product: str = "standard"

    @property
    def bacteria_only(self) -> dict:
        return derive_bacteria_only(self.genomic)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lot_numbers": self.lot_numbers,
            "product": self.product,
            "expectedValues": {"Genomic": self.genomic, "GenomicBacteriaOnly": self.bacteria_only},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ValueSet":
        return cls(
            name=data["name"],
            genomic=data["expectedValues"]["Genomic"],
            lot_numbers=[str(lot) for lot in data.get("lot_numbers", [])],
            product=data.get("product", "standard"),
        )

    def validate(self) -> None:
        check_name(self.name, "value set name")
        for lot in self.lot_numbers:
            check_name(lot, "lot number")
        if len(set(self.lot_numbers)) != len(self.lot_numbers):
            raise LotError(f"Value set '{self.name}' lists a lot number more than once.")
        validate_genomic(self.genomic, self.product)

    def build_reference(self) -> dict:
        reference = copy.deepcopy(load_base_reference(self.product))
        reference["expectedValues"]["Genomic"] = dict(self.genomic)
        reference["expectedValues"]["GenomicBacteriaOnly"] = self.bacteria_only
        return reference

    def write_reference(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"reference_{self.name}.json"
        with open(path, "w") as handle:
            json.dump(self.build_reference(), handle, indent=4)
        return path


class LotStore:
    def __init__(self, lots_dir: Path = LOTS_DIR):
        self.lots_dir = Path(lots_dir)

    def path_for(self, name: str) -> Path:
        return self.lots_dir / f"{name}.json"

    def value_sets(self) -> list:
        sets = []
        for path in sorted(self.lots_dir.glob("*.json")):
            with open(path) as handle:
                value_set = ValueSet.from_dict(json.load(handle))
            if value_set.name != path.stem:
                raise LotError(f"{path} has name '{value_set.name}'; the name must match the file name.")
            sets.append(value_set)
        return sets

    def get(self, name: str):
        return next((s for s in self.value_sets() if s.name == name), None)

    def find_by_lot(self, lot_number: str):
        lot_number = lot_number.strip()
        matches = [s for s in self.value_sets() if lot_number in s.lot_numbers]
        if len(matches) > 1:
            raise LotError(f"Lot {lot_number} is listed in several value sets: {', '.join(s.name for s in matches)}.")
        return matches[0] if matches else None

    def save(self, value_set: ValueSet, overwrite: bool = False) -> Path:
        value_set.validate()
        path = self.path_for(value_set.name)
        if path.exists() and not overwrite:
            raise LotError(f"A value set named '{value_set.name}' already exists.")
        for other in self.value_sets():
            if other.name == value_set.name:
                continue
            shared = set(other.lot_numbers) & set(value_set.lot_numbers)
            if shared:
                raise LotError(f"Lot number(s) {', '.join(sorted(shared))} already belong to value set '{other.name}'.")
        self.lots_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as handle:
            json.dump(value_set.to_dict(), handle, indent=2)
            handle.write("\n")
        return path

    def create(self, name: str, lot_number: str, genomic: dict, product: str = "standard") -> ValueSet:
        value_set = ValueSet(name=check_name(name, "value set name"), genomic=dict(genomic), lot_numbers=[check_name(lot_number, "lot number")], product=product)
        self.save(value_set)
        return value_set

    def link_lot(self, value_set: ValueSet, lot_number: str) -> ValueSet:
        lot_number = check_name(lot_number, "lot number")
        if lot_number in value_set.lot_numbers:
            return value_set
        updated = ValueSet(name=value_set.name, genomic=value_set.genomic, lot_numbers=value_set.lot_numbers + [lot_number], product=value_set.product)
        self.save(updated, overwrite=True)
        return updated
