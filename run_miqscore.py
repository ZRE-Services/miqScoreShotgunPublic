#!/usr/bin/env python3
"""
Run MIQScore on a folder of paired FASTQ files with lot-specific expected values.

1. Choose the folder containing *_R1.fastq.gz / *_R2.fastq.gz pairs
2. Pick a saved value set, or enter the standard's lot number (link it to a value set or enter new values)
3. Optionally subsample with seqtk
4. Choose where the run folder goes (results/ in the repository by default)
5. Run the miqscoreshotgun docker image for every sample and write a summary CSV
"""

import argparse
import csv
import getpass
import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import questionary
from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.lexers import SimpleLexer
from prompt_toolkit.validation import ValidationError, Validator
from questionary.constants import DEFAULT_QUESTION_PREFIX
from questionary.styles import merge_styles_default

import lot_editor
import lotstore

DOCKER_IMAGE = "miqscoreshotgun"
CONTAINER_DATA = "/data"
DEFAULT_SUBSAMPLE_READS = 1_000_000
VALID_SAMPLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\- ]*$")
RESULTS_DIR = lotstore.REPO_ROOT / "results"
RECENTS_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "miqscore"
MAX_RECENTS = 10
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
    parser.add_argument("--output", type=Path, help=f"Folder to create the run folder in (default: {RESULTS_DIR})")
    parser.add_argument("--image", default=DOCKER_IMAGE, help=f"Docker image to run (default: {DOCKER_IMAGE})")
    return parser.parse_args()


def ask(question, erase=False):
    """erase=True leaves nothing on screen once answered, for steps whose answer is confirmed by a later prompt."""
    question.application.erase_when_done = erase
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


def create_value_set(store, lot_number):
    product = "standard"
    name, values = lot_number, None
    while True:
        edited = lot_editor.edit_values(store, product, lot_number, name, values)
        if edited is None:
            return None
        name, values = edited
        show(lotstore.ValueSet(name=name, genomic=values, lot_numbers=[lot_number], product=product))
        action = ask(questionary.select(f"Save value set '{name}' for lot {lot_number}?", choices=[
            questionary.Choice("Save", value="save"),
            questionary.Choice("Edit the values again", value="edit"),
            questionary.Choice("Cancel", value="cancel"),
        ]))
        if action == "cancel":
            return None
        if action == "save":
            try:
                return store.create(name, lot_number, values, product)
            except lotstore.LotError as err:
                logger.error(err)


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
            if store.path_for(preset_value_set).exists():
                logger.error(f"Value set '{preset_value_set}' cannot be used; run python check_lots.py to see why.")
            else:
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


def with_sample_counts(folders):
    found = []
    for folder in folders:
        try:
            count = len(runnable_samples(find_fastq_pairs(folder)))
        except OSError:
            continue
        if count:
            found.append((folder, count))
    return found


def candidate_folders(base):
    return with_sample_counts([base] + sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")))


def matching_dirs(text):
    """Directory names completing the last segment of text, like bash (hidden ones only if asked for)."""
    head, tail = os.path.split(os.path.expanduser(text))
    try:
        entries = list(os.scandir(head or "."))
    except OSError:
        return tail, []
    names = [e.name for e in entries
             if e.name.startswith(tail) and (tail.startswith(".") or not e.name.startswith(".")) and e.is_dir()]
    return tail, sorted(names)


def columns(names):
    width = max(len(n) for n in names) + 2
    per_line = max(1, shutil.get_terminal_size().columns // width)
    return "\n".join("".join(f"{n:<{width}}" for n in names[i:i + per_line]).rstrip()
                     for i in range(0, len(names), per_line))


class _PathValidator(Validator):
    def __init__(self, check):
        self.check = check

    def validate(self, document):
        problem = self.check(document.text)
        if problem:
            raise ValidationError(message=problem, cursor_position=len(document.text))


def path_prompt(message, validate, history=(), default=""):
    """A path prompt with bash-style Tab: complete to the common prefix, list the choices on a second Tab."""
    bindings = KeyBindings()

    @bindings.add("tab")
    def complete(event):
        buffer = event.current_buffer
        tail, names = matching_dirs(buffer.document.text_before_cursor)
        if len(names) == 1:
            buffer.insert_text(names[0][len(tail):] + os.sep)
            return
        common = os.path.commonprefix(names)
        if len(common) > len(tail):
            buffer.insert_text(common[len(tail):])
        elif names and event.is_repeat:
            listing = columns([n + os.sep for n in names])
            run_in_terminal(lambda: print(listing))
        else:
            event.app.output.bell()

    session = PromptSession(
        [("class:qmark", DEFAULT_QUESTION_PREFIX), ("class:question", f" {message} ")],
        lexer=SimpleLexer("class:answer"),
        style=merge_styles_default([None]),
        validator=_PathValidator(validate),
        validate_while_typing=False,
        key_bindings=bindings,
        history=InMemoryHistory(list(reversed(history))),
    )
    session.default_buffer.reset(Document(default))
    return questionary.Question(session.app)


def recents_file(kind):
    return RECENTS_DIR / f"recent_{kind}_folders.json"


def load_recent_folders(kind):
    try:
        with open(recents_file(kind)) as handle:
            entries = json.load(handle)
    except (OSError, ValueError):
        return []
    return [Path(entry) for entry in entries if isinstance(entry, str) and Path(entry).is_dir()] if isinstance(entries, list) else []


def remember_folder(kind, folder):
    recents = [folder] + [p for p in load_recent_folders(kind) if p != folder]
    try:
        RECENTS_DIR.mkdir(parents=True, exist_ok=True)
        with open(recents_file(kind), "w") as handle:
            json.dump([str(p) for p in recents[:MAX_RECENTS]], handle, indent=2)
    except OSError as err:
        logger.warning(f"Could not save the recent folders list: {err}")


def display_path(folder):
    try:
        return f"~{os.sep}{folder.relative_to(Path.home())}"
    except ValueError:
        return str(folder)


def type_path(message, check, kind, prefill=None):
    """Returns the typed folder, or None when the answer is empty (go back). A prefill folder starts in the line, ready to edit."""
    def problem(text):
        return check(Path(text.strip()).expanduser()) if text.strip() else None

    recents = [display_path(p) for p in load_recent_folders(kind)]
    default = os.path.join(str(prefill), "") if prefill else ""
    text = ask(path_prompt(f"{message}:", problem, recents, default)).strip()
    return Path(text).expanduser() if text else None


def folder_choices(title, entries):
    if not entries:
        return []
    width = max(len(label) for label, _, _ in entries)
    return [questionary.Separator(title)] + [
        questionary.Choice(f"{label:<{width + 2}} ({count} sample{'s' if count != 1 else ''})", value=str(folder))
        for label, folder, count in entries]


def pick_folder():
    base = Path.cwd()
    nearby = candidate_folders(base)
    nearby_paths = {folder for folder, _ in nearby}
    recent = with_sample_counts([p for p in load_recent_folders("input") if p not in nearby_paths])
    if not nearby and not recent:
        return type_path("FASTQ folder", folder_problem, "input")
    choices = folder_choices("-- In this directory --",
                             [("./" if f == base else str(f.relative_to(base)), f, n) for f, n in nearby])
    choices += folder_choices("-- Recent --", [(display_path(f), f, n) for f, n in recent])
    choices.append(questionary.Separator(" "))
    choices.append(questionary.Choice("Type a path...", value=""))
    answer = ask(questionary.select("Folder containing your FASTQ files:", choices=choices), erase=True)
    return type_path("FASTQ folder", folder_problem, "input", Path(answer) if answer else None)


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
        remember_folder("input", folder)
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
            remember_folder("input", folder)
            return folder, pairs


def output_problem(folder):
    if ":" in str(folder):
        return "Docker cannot mount a path that contains ':'"
    if folder.exists() and not folder.is_dir():
        return "Not a folder"
    existing = next(p for p in [folder, *folder.parents] if p.exists())
    if not existing.is_dir() or not os.access(existing, os.W_OK | os.X_OK):
        return f"No permission to write in {existing}"
    return None


def choose_output(preset):
    """Returns the folder the run folder is created in."""
    if preset is not None:
        folder = preset.expanduser().resolve()
        problem = output_problem(folder)
        if problem:
            logger.error(f"{problem}: {folder}")
            sys.exit(1)
    else:
        folder = None
        while folder is None:
            recent = [p for p in load_recent_folders("output") if p != RESULTS_DIR]
            choices = [questionary.Choice(f"results/ in this repository ({display_path(RESULTS_DIR)})", value=str(RESULTS_DIR))]
            if recent:
                choices.append(questionary.Separator("-- Recent --"))
                choices += [questionary.Choice(display_path(p), value=str(p)) for p in recent]
            choices += [questionary.Separator(" "), questionary.Choice("Type a path...", value="")]
            answer = ask(questionary.select("Where should the results go?", choices=choices), erase=True)
            folder = type_path("Results folder", output_problem, "output", Path(answer) if answer else None)
        folder = folder.resolve()
    if folder != RESULTS_DIR:
        remember_folder("output", folder)
    return folder


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


def clean_up(base_folder, reference_file):
    """Keep only the reports, the summary, run_info.json and the reference file the container read."""
    try:
        reference_file.replace(base_folder / reference_file.name)
        for name in ("input", "working"):
            shutil.rmtree(base_folder / name, ignore_errors=True)
        for path in (base_folder / "output").iterdir():
            if path.suffix not in (".html", ".json"):
                shutil.rmtree(path) if path.is_dir() else path.unlink()
    except OSError as err:
        logger.warning(f"Could not remove all intermediate files: {err}")


def write_summary(base_folder, results):
    summary_path = base_folder / f"{base_folder.name}_summary.csv"
    fields = ["sample", "miq_score", "raw_miq_score", "status", "lot_number", "value_set", "reference_file", "html_report"]
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
    try:
        RESULTS_DIR.mkdir(exist_ok=True)
        shutil.copy2(summary_path, RESULTS_DIR / summary_path.name)
    except OSError as err:
        logger.warning(f"Could not copy the summary to {RESULTS_DIR}: {err}")


# ---------- main ----------

def warn_unusable_lot_files(store):
    unusable = [(name, problem) for name, problem in store.check()[1] if problem != lotstore.BACTERIA_ONLY_MISMATCH]
    for name, problem in unusable:
        logger.warning(f"Skipping lots/{name}: {problem}")
    if unusable:
        logger.warning("Run python check_lots.py to fix the files in lots/.")


def main():
    args = parse_arguments()
    store = lotstore.LotStore()
    warn_unusable_lot_files(store)

    input_folder, fastq_pairs = choose_folder(args.folder)

    lot_number, value_set = choose_lot(store, args.lot, args.value_set)
    num_reads = choose_subsampling(args.subsample)
    output_root = choose_output(args.output)

    sudo_password = get_sudo_password()
    check_docker_image_available(args.image, sudo_password)

    current_date = datetime.now().strftime("%y%m%d")
    values_label = f"lot{lot_number}" if lot_number else f"set{value_set.name}"
    input_label = re.sub(r"[^A-Za-z0-9._-]", "_", input_folder.name)  # ':' would break the docker -v mount
    base_folder = output_root / f"miqscore_{current_date}_{input_label}_{reads_label(num_reads)}_{values_label}"
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
    try:
        for sample_name, files in fastq_pairs.items():
            logger.info(f"Processing sample: {sample_name}")
            result = {"sample": sample_name, "miq_score": "", "raw_miq_score": "", "status": "",
                      "lot_number": lot_number or "", "value_set": value_set.name,
                      "reference_file": str(base_folder / reference_file.name), "html_report": ""}
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

    finally:
        clean_up(base_folder, reference_file)
    write_summary(base_folder, results)


if __name__ == "__main__":
    main()
