#!/usr/bin/env python3
"""
Check every value set in lots/ before committing it.

- Each file must load, have a name matching the file name, and hold valid Genomic values.
- GenomicBacteriaOnly must match the values derived from Genomic (offers to recalculate it).
- A lot number may belong to only one value set (offers to consolidate).

With --check (or without a terminal) nothing is changed and the exit code is 1 if anything is wrong.
"""

import argparse
import sys

import questionary

import lotstore
from run_miqscore import ask, show

SKIP = "_skip"  # value set names must start with a letter or digit, so this cannot clash


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Only report problems, never ask or change files")
    return parser.parse_args()


def report(store):
    sets, problems = store.check()
    duplicates = store.duplicate_lots(sets)
    for file_name, problem in problems:
        print(f"  {file_name}: {problem}")
    for lot, names in duplicates.items():
        print(f"  Lot {lot} is listed in several value sets: {', '.join(names)}")
    return sets, problems, duplicates


def recalculate_bacteria_only(store, sets, problems):
    mismatched = {file_name for file_name, problem in problems if problem == lotstore.BACTERIA_ONLY_MISMATCH}
    for value_set in sets:
        if store.path_for(value_set.name).name in mismatched and ask(questionary.confirm(
                f"Recalculate GenomicBacteriaOnly in {value_set.name}.json from its Genomic values?", default=True)):
            store.write(value_set)


def consolidate(store, lot, sets):
    print(f"\n  Lot {lot} is listed in {len(sets)} value sets:")
    for value_set in sets:
        show(value_set)
    names = [s.name for s in sets]
    same_values = all(s.genomic == sets[0].genomic and s.product == sets[0].product for s in sets)
    if same_values:
        print("  They hold the same values, so they can be merged into one value set.")
        choices = [questionary.Choice(f"Merge into '{name}' (the other files are deleted)", value=name) for name in names]
    else:
        print("  Their values differ. Check the lot's certificate to see which one is right.")
        choices = [questionary.Choice(f"Keep lot {lot} only in '{name}'", value=name) for name in names]
    choices.append(questionary.Choice("Skip", value=SKIP))
    keep = ask(questionary.select(f"How should lot {lot} be consolidated?", choices=choices))
    if keep == SKIP:
        return
    target = next(s for s in sets if s.name == keep)
    others = [s for s in sets if s.name != keep]
    if same_values:
        lots = list(target.lot_numbers)
        lots += [n for other in others for n in other.lot_numbers if n not in lots]
        store.write(lotstore.ValueSet(name=target.name, genomic=target.genomic, lot_numbers=lots, product=target.product))
        for other in others:
            store.path_for(other.name).unlink()
        print(f"  Merged {', '.join(o.name for o in others)} into '{target.name}'.")
    else:
        for other in others:
            store.remove_lot(other, lot)
        print(f"  Lot {lot} now belongs only to '{target.name}'.")


def main():
    args = parse_args()
    interactive = not args.check and sys.stdin.isatty()
    store = lotstore.LotStore()
    print(f"Checking {store.lots_dir}")
    sets, problems, duplicates = report(store)

    if interactive and (problems or duplicates):
        recalculate_bacteria_only(store, sets, problems)
        skipped = set()
        while True:
            sets, _ = store.check()
            pending = {lot: names for lot, names in store.duplicate_lots(sets).items() if lot not in skipped}
            if not pending:
                break
            lot, names = next(iter(pending.items()))
            consolidate(store, lot, [s for s in sets if s.name in names])
            skipped.add(lot)  # asked once; a skipped lot is reported again below
        print("\nChecking again")
        sets, problems, duplicates = report(store)

    if problems or duplicates:
        print(f"{len(problems) + len(duplicates)} problem(s) left; fix the files in lots/ by hand.")
        sys.exit(1)
    print(f"All {len(sets)} value sets are valid.")


if __name__ == "__main__":
    main()
