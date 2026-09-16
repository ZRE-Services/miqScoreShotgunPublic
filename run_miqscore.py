#!/usr/bin/env python3
"""
Run MIQScore on a folder of paired FASTQ files with lot-specific expected values.

1. Choose the folder containing *_R1.fastq.gz / *_R2.fastq.gz pairs
2. Pick a saved value set, or enter the standard's lot number (link it to a value set or enter new values)
3. Optionally subsample with seqtk
4. Run the miqscoreshotgun docker image for every sample and write a summary CSV
"""

import argparse
import csv
import getpass
import gzip
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import questionary

import lotstore

DOCKER_IMAGE = "miqscoreshotgun"
CONTAINER_DATA = "/data"
DEFAULT_SUBSAMPLE_READS = 1_000_000
VALID_SAMPLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\- ]*$")
RESULTS_DIR = lotstore.REPO_ROOT / "results"
ENTER_LOT = "_enter_lot"  # value set names must start with a letter or digit, so this cannot clash

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def parse_arguments():
    parser = argparse.ArgumentParser(description="Run MIQScore with lot-specific expected values")
    parser.add_argument("--folder", type=Path, help="Folder containing *_R1.fastq.gz / *_R2.fastq.gz pairs")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--lot", help="Lot number of the microbial community standard")
    choice.add_argument("--value-set", help="Name of a saved value set in lots/ to use without a lot number")
    parser.add_argument("--subsample", type=int, help="Reads to subsample per file (0 disables subsampling)")
    parser.add_argument("--image", default=DOCKER_IMAGE, help=f"Docker image to run (default: {DOCKER_IMAGE})")
    return parser.parse_args()


def ask(question):
    answer = question.ask()
    if answer is None:
        logger.info("Cancelled.")
        sys.exit(1)
    return answer


# ---------- value set display ----------

def values_table(value_set):
    names = lotstore.print_names(value_set.product)
    rows = [(names.get(key, key), value) for key, value in value_set.genomic.items()]
    half = (len(rows) + 1) // 2
    width = max(len(name) for name, _ in rows)
    lines = []
    for i in range(half):
        left = f"{rows[i][0]:<{width}} {rows[i][1]:>6g}"
        right = f"{rows[i + half][0]:<{width}} {rows[i + half][1]:>6g}" if i + half < len(rows) else ""
        lines.append(f"    {left}     {right}")
    lots = ", ".join(value_set.lot_numbers) or "none"
    return f"  Value set '{value_set.name}' (lots: {lots})\n" + "\n".join(lines)


def values_summary(value_set):
    names = lotstore.print_names(value_set.product)
    short = " | ".join(f"{names.get(k, k).replace(' ', '')[:5]} {v:g}" for k, v in value_set.genomic.items())
    return f"{value_set.name:<24} {short}"


def show(value_set):
    print("\n" + values_table(value_set) + "\n")


# ---------- lot selection ----------

def pick_value_set(store, message):
    sets = store.value_sets()
    if not sets:
        print("  No value sets saved yet.")
        return None
    choices = [questionary.Choice(values_summary(s), value=s.name) for s in sets]
    choices.append(questionary.Choice("<- Back", value=None))
    name = ask(questionary.select(message, choices=choices))
    return store.get(name) if name else None


def enter_genomic_values(product, defaults):
    names = lotstore.print_names(product)

    def is_positive_number(text):
        try:
            return float(text) > 0 or "Must be greater than 0"
        except ValueError:
            return "Enter a number"

    while True:
        print("\n  Enter the Genomic expected values (%) from the lot's certificate.")
        values = {}
        for key in lotstore.organisms(product):
            answer = ask(questionary.text(f"{names.get(key, key)}:", default=f"{defaults.get(key, ''):g}" if key in defaults else "", validate=is_positive_number))
            values[key] = float(answer)
        total = lotstore.genomic_sum(values)
        if abs(total - 100) <= lotstore.SUM_TOLERANCE:
            return values
        action = ask(questionary.select(
            f"The values add up to {total:g}, but must add up to 100. What now?",
            choices=[
                questionary.Choice("Rescale proportionally to 100", value="rescale"),
                questionary.Choice("Re-enter the values", value="retry"),
                questionary.Choice("Cancel", value="cancel"),
            ],
        ))
        if action == "rescale":
            return lotstore.rescale_to_100(values, exact=True)
        if action == "retry":
            defaults = values
        else:
            return None


def create_value_set(store, lot_number):
    def name_ok(text):
        try:
            name = lotstore.check_name(text, "value set name")
        except lotstore.LotError as err:
            return str(err)
        return f"'{name}' already exists" if store.path_for(name).exists() else True

    name = ask(questionary.text("Name for the new value set:", default=lot_number, validate=name_ok)).strip()
    product = "standard"
    defaults = lotstore.load_base_reference(product)["expectedValues"]["Genomic"]
    values = enter_genomic_values(product, defaults)
    if values is None:
        return None
    preview = lotstore.ValueSet(name=name, genomic=values, lot_numbers=[lot_number], product=product)
    show(preview)
    if not ask(questionary.confirm(f"Save value set '{name}' for lot {lot_number}?", default=True)):
        return None
    try:
        return store.create(name, lot_number, values, product)
    except lotstore.LotError as err:
        logger.error(err)
        return None


def resolve_lot(store, lot_number):
    """Returns the value set for lot_number, or None to start over."""
    value_set = store.find_by_lot(lot_number)
    if value_set:
        show(value_set)
        return value_set if ask(questionary.confirm(f"Use these values for lot {lot_number}?", default=True)) else None

    print(f"\n  Lot {lot_number} is not known yet.")
    while True:
        action = ask(questionary.select("What now?", choices=[
            questionary.Choice("Link to an existing value set", value="link"),
            questionary.Choice("Enter new expected values", value="new"),
            questionary.Choice("Enter a different lot number", value="back"),
        ]))
        if action == "back":
            return None
        if action == "new":
            created = create_value_set(store, lot_number)
            if created:
                return created
            continue
        candidate = pick_value_set(store, "Pick a value set to link:")
        if not candidate:
            continue
        show(candidate)
        if ask(questionary.confirm(f"Link lot {lot_number} to '{candidate.name}'?", default=True)):
            return store.link_lot(candidate, lot_number)


def choose_value_set_or_lot(store):
    """Returns (None, value_set) for a directly picked value set, or (lot_number, None) to look up a lot."""
    choices = [questionary.Choice(values_summary(s), value=s.name) for s in store.value_sets()]
    choices.append(questionary.Choice("Enter a lot number", value=ENTER_LOT))
    name = ask(questionary.select("Expected values to use:", choices=choices))
    if name == ENTER_LOT:
        return ask(questionary.text("Lot number of the standard:", validate=lambda t: bool(t.strip()) or "Enter a lot number")).strip(), None
    value_set = store.get(name)
    show(value_set)
    return None, value_set if ask(questionary.confirm(f"Use value set '{name}'?", default=True)) else None


def choose_lot(store, preset_lot, preset_value_set=None):
    """Returns (lot_number, value_set); lot_number is None when a value set was picked without a lot."""
    if preset_value_set is not None:
        value_set = store.get(preset_value_set)
        if value_set is None:
            logger.error(f"Unknown value set '{preset_value_set}'. Known: {', '.join(s.name for s in store.value_sets())}")
            sys.exit(1)
        logger.info(f"Using value set '{value_set.name}'")
        return None, value_set
    lot_number = preset_lot
    while True:
        if lot_number is None:
            lot_number, value_set = choose_value_set_or_lot(store)
            if value_set:
                logger.info(f"Using value set '{value_set.name}'")
                return None, value_set
            if lot_number is None:
                continue
        try:
            lot_number = lotstore.check_name(lot_number, "lot number")
            value_set = resolve_lot(store, lot_number)
        except lotstore.LotError as err:
            logger.error(err)
            value_set = None
        if value_set:
            logger.info(f"Using value set '{value_set.name}' for lot {lot_number}")
            return lot_number, value_set
        lot_number = None


# ---------- input / subsampling ----------

def runnable_samples(pairs):
    return [name for name, files in pairs.items() if files["R1"] and files["R2"] and VALID_SAMPLE_NAME.match(name)]


def folder_problem(folder):
    if not folder.is_dir():
        return "Not a folder"
    if not find_fastq_pairs(folder):
        return "No *_R1.fastq.gz / *_R2.fastq.gz files in this folder"
    return None


def candidate_folders(base):
    folders = [base] + sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
    found = []
    for folder in folders:
        try:
            count = len(runnable_samples(find_fastq_pairs(folder)))
        except OSError:
            continue
        if count:
            found.append((folder, count))
    return found


def type_folder_path():
    def validate(text):
        if not text.strip():
            return True
        return folder_problem(Path(text.strip()).expanduser()) or True

    text = ask(questionary.path("Folder containing your FASTQ files (empty to go back):", only_directories=True, validate=validate)).strip()
    return Path(text).expanduser() if text else None


def pick_folder():
    base = Path.cwd()
    candidates = candidate_folders(base)
    if not candidates:
        return type_folder_path()
    width = max(len(str(folder.relative_to(base))) for folder, _ in candidates)
    choices = [questionary.Choice(f"{'./' if folder == base else str(folder.relative_to(base)):<{width + 2}} ({count} sample{'s' if count != 1 else ''})",
                                  value=str(folder)) for folder, count in candidates]
    choices.append(questionary.Choice("Type a path...", value=""))
    answer = ask(questionary.select("Folder containing your FASTQ files:", choices=choices))
    return Path(answer) if answer else type_folder_path()


def show_samples(folder, pairs):
    runnable = runnable_samples(pairs)
    print(f"\n  Found {len(runnable)} sample{'s' if len(runnable) != 1 else ''} to run in {folder}")
    width = max(len(name) for name in pairs)
    per_line = max(1, 100 // (width + 3))
    for i in range(0, len(runnable), per_line):
        print("    " + "   ".join(f"{name:<{width}}" for name in runnable[i:i + per_line]).rstrip())
    missing = [f"{name} (no {'R2' if files['R1'] else 'R1'})" for name, files in pairs.items() if not (files["R1"] and files["R2"])]
    invalid = [name for name in pairs if not VALID_SAMPLE_NAME.match(name)]
    if missing:
        print(f"  Will be skipped, incomplete pair: {', '.join(missing)}")
    if invalid:
        print(f"  Will be skipped, name not accepted by MIQScore: {', '.join(invalid)}")
    print()


def choose_folder(preset):
    """Returns (folder, fastq_pairs)."""
    if preset is not None:
        folder = preset.expanduser().resolve()
        problem = folder_problem(folder)
        if problem:
            logger.error(f"{problem}: {folder}")
            sys.exit(1)
        pairs = find_fastq_pairs(folder)
        show_samples(folder, pairs)
        return folder, pairs
    while True:
        folder = pick_folder()
        if folder is None:
            continue
        folder = folder.resolve()
        pairs = find_fastq_pairs(folder)
        show_samples(folder, pairs)
        count = len(runnable_samples(pairs))
        if count and ask(questionary.confirm(f"Use these {count} sample{'s' if count != 1 else ''}?", default=True)):
            logger.info(f"Selected folder: {folder}")
            return folder, pairs


def check_seqtk_available():
    return shutil.which("seqtk") is not None


def choose_subsampling(preset):
    if preset is not None:
        if preset < 0:
            logger.error("--subsample must be 0 or a positive number")
            sys.exit(1)
        if preset and not check_seqtk_available():
            logger.error("seqtk is required for --subsample. Install it with: sudo apt-get install seqtk")
            sys.exit(1)
        return preset or None
    if not check_seqtk_available():
        logger.warning("seqtk not found; subsampling disabled. Install it with: sudo apt-get install seqtk")
        return None
    if not ask(questionary.confirm("Enable subsampling?", default=True)):
        return None
    reads = ask(questionary.text("Number of reads to subsample:", default=str(DEFAULT_SUBSAMPLE_READS),
                                 validate=lambda t: (t.isdigit() and int(t) > 0) or "Enter a positive whole number"))
    return int(reads)


def find_fastq_pairs(input_folder):
    pairs = {}
    for file_path in sorted(input_folder.glob("*.fastq.gz")):
        match = re.match(r"^(.*)_R([12])\.fastq\.gz$", file_path.name)
        if not match:
            continue
        pairs.setdefault(match.group(1), {"R1": None, "R2": None})[f"R{match.group(2)}"] = file_path
    return pairs


def process_fastq_file(input_file, output_file, num_reads=None):
    if num_reads:
        logger.info(f"Subsampling {input_file.name} to {num_reads} reads -> {output_file.name}")
        with open(output_file, "w") as f_out:
            subprocess.run(["seqtk", "sample", "-s100", str(input_file), str(num_reads)], stdout=f_out, check=True)
    else:
        logger.info(f"Decompressing {input_file.name} -> {output_file.name}")
        with gzip.open(input_file, "rb") as f_in, open(output_file, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)


def reads_label(num_reads):
    if not num_reads:
        return "fullReads"
    if num_reads >= 1_000_000:
        return f"{num_reads // 1_000_000}M"
    if num_reads >= 1_000:
        return f"{num_reads // 1_000}K"
    return str(num_reads)


# ---------- docker ----------

def get_sudo_password():
    try:
        subprocess.run(["docker", "ps"], check=True, capture_output=True)
        return None
    except FileNotFoundError:
        logger.error("docker is not installed or not on PATH.")
        sys.exit(1)
    except subprocess.CalledProcessError:
        return getpass.getpass("Enter sudo password for Docker: ")


def docker_command(args, sudo_password):
    cmd = ["docker"] + args
    return ["sudo", "-S", "-p", ""] + cmd if sudo_password else cmd


def check_docker_image_available(image, sudo_password):
    result = subprocess.run(docker_command(["images", "-q", image], sudo_password), input=(sudo_password + "\n") if sudo_password else None,
                            capture_output=True, text=True)
    if not result.stdout.strip():
        logger.error(f"Docker image '{image}' not found. Build it from this repo with: docker build -t {DOCKER_IMAGE} .")
        sys.exit(1)
    logger.info(f"Docker image '{image}' found.")


def run_docker_miqscore(image, base_folder, sample_name, reference_file, sudo_password):
    container_reference = f"{CONTAINER_DATA}/{reference_file.relative_to(base_folder).as_posix()}"
    cmd = docker_command([
        "container", "run", "--rm",
        "-v", f"{base_folder}:{CONTAINER_DATA}",
        "-e", f"SAMPLENAME={sample_name}",
        "-e", f"REFERENCEDATAFILE={container_reference}",
        image,
    ], sudo_password)
    logger.info(f"Running MIQScore for sample {sample_name}")
    try:
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE if sudo_password else None, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
        if sudo_password:
            process.stdin.write(sudo_password + "\n")
            process.stdin.flush()
        for line in process.stdout:
            print(line.rstrip())
        return process.wait() == 0
    except OSError as err:
        logger.error(f"Error running docker: {err}")
        return False


# ---------- summary ----------

def read_score(output_folder, sample_name, not_before):
    report = output_folder / f"{sample_name}.json"
    # A same-day rerun reuses the folder; ignore reports left over from an earlier run.
    if not report.is_file() or report.stat().st_mtime < not_before:
        return None
    with open(report) as handle:
        return json.load(handle).get("miqScore")


def write_summary(base_folder, results):
    summary_path = base_folder / f"{base_folder.name}_summary.csv"
    fields = ["sample", "miq_score", "raw_miq_score", "status", "lot_number", "value_set", "html_report"]
    with open(summary_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    width = max([len("Sample")] + [len(r["sample"]) for r in results])
    print("\n" + "=" * (width + 30))
    print(f"{'Sample':<{width}}  {'MIQ':>5}  Status")
    print("-" * (width + 30))
    for r in results:
        print(f"{r['sample']:<{width}}  {str(r['miq_score']):>5}  {r['status']}")
    print("=" * (width + 30))
    succeeded = sum(r["status"] == "ok" for r in results)
    logger.info(f"{succeeded}/{len(results)} samples succeeded. Summary written to {summary_path}")


# ---------- main ----------

def main():
    args = parse_arguments()
    store = lotstore.LotStore()

    input_folder, fastq_pairs = choose_folder(args.folder)

    lot_number, value_set = choose_lot(store, args.lot, args.value_set)
    num_reads = choose_subsampling(args.subsample)

    sudo_password = get_sudo_password()
    check_docker_image_available(args.image, sudo_password)

    current_date = datetime.now().strftime("%y%m%d")
    values_label = f"lot{lot_number}" if lot_number else f"set{value_set.name}"
    base_folder = RESULTS_DIR / f"{current_date}_{input_folder.name}_{reads_label(num_reads)}_{values_label}_miqscore"
    logger.info(f"Analysis folder: {base_folder}")
    input_seq = base_folder / "input" / "sequence"
    output_folder = base_folder / "output"
    for folder in (input_seq, base_folder / "working", output_folder):
        folder.mkdir(parents=True, exist_ok=True)

    reference_file = value_set.write_reference(base_folder / "working")
    with open(base_folder / "run_info.json", "w") as handle:
        json.dump({
            "date": datetime.now().isoformat(timespec="seconds"),
            "input_folder": str(input_folder),
            "lot_number": lot_number,
            "value_set": value_set.to_dict(),
            "subsample_reads": num_reads,
            "docker_image": args.image,
        }, handle, indent=2)

    results = []
    for sample_name, files in fastq_pairs.items():
        logger.info(f"Processing sample: {sample_name}")
        result = {"sample": sample_name, "miq_score": "", "raw_miq_score": "", "status": "",
                  "lot_number": lot_number or "", "value_set": value_set.name, "html_report": ""}
        results.append(result)

        if not files["R1"] or not files["R2"]:
            logger.warning(f"Missing R1 or R2 file for {sample_name}, skipping")
            result["status"] = "skipped: missing R1/R2"
            continue
        if not VALID_SAMPLE_NAME.match(sample_name):
            logger.warning(f"Sample name '{sample_name}' is not accepted by MIQScore, skipping")
            result["status"] = "skipped: invalid sample name"
            continue

        for old_file in input_seq.glob("*.fastq"):
            old_file.unlink()
        try:
            for read in ("R1", "R2"):
                process_fastq_file(files[read], input_seq / f"standard_submitted_{read}.fastq", num_reads)
        except (OSError, subprocess.CalledProcessError) as err:
            logger.error(f"Preparing FASTQ files for {sample_name} failed: {err}")
            result["status"] = "failed: fastq preparation"
            continue

        started = time.time() - 1
        if not run_docker_miqscore(args.image, base_folder, sample_name, reference_file, sudo_password):
            logger.error(f"Docker failed for sample {sample_name}")
            result["status"] = "failed: docker"
            continue
        score = read_score(output_folder, sample_name, started)
        if score is None:
            result["status"] = "failed: no report"
            continue
        result.update(miq_score=round(score), raw_miq_score=round(score, 2), status="ok",
                      html_report=str(output_folder / f"{sample_name}.html"))
        logger.info(f"Sample {sample_name}: MIQ score {round(score)}")

    for old_file in input_seq.glob("*.fastq"):
        old_file.unlink()
    write_summary(base_folder, results)


if __name__ == "__main__":
    main()
