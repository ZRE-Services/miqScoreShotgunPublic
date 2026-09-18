# MIQ Score for shotgun sequencing of the ZymoBIOMICS standard

Computes a MIQ (Measurement Integrity Quotient) score for a shotgun metagenomic sequencing run of the
ZymoBIOMICS Microbial Community Standard. Reads are aligned to the standard's reference genome
([BWA](https://github.com/lh3/bwa) for Illumina, [minimap2](https://github.com/lh3/minimap2) for long
reads), and the observed composition is scored against the expected one with the
[NGS MIQ Score Calculator](https://github.com/Zymo-Research/miqScoreNGSReadCountPublic). The result is an
HTML report and a JSON file per sample.

The repository has two parts:

- **The Docker image** (`analyzeStandardReads.py`, built by the `Dockerfile`) does the analysis for one
  sample. It is configured only through environment variables.
- **The wrapper** (`run_miqscore.py`) runs the image on every sample in a folder of paired FASTQ files,
  with the **expected values for the lot** of standard you sequenced. The expected composition differs
  from lot to lot; the wrapper keeps a library of expected values in `lots/` and hands them to the
  unchanged image at run time, so you never build a separate image for a lot.

Contents: [Setup](#setup) · [Running a batch](#running-a-batch-with-the-wrapper) ·
[Expected values per lot](#choosing-the-expected-values) · [The lot library](#the-lot-library-lots) ·
[Output](#output) · [Running the image directly](#running-the-image-directly) ·
[Troubleshooting](#troubleshooting) · [Development](#development)

## Setup

1. **Clone with submodules.** Two sibling repositories hold most of the analysis code and are checked
   out as git submodules; without them the image does not build.
   ```bash
   git clone https://github.com/ZRE-Services/miqScoreShotgunPublic.git
   cd miqScoreShotgunPublic
   git config submodule.miqScoreNGSReadCountPublic.url https://github.com/Zymo-Research/miqScoreNGSReadCountPublic.git
   git submodule update --init --recursive
   ```
   The `git config` line is needed before the first `submodule update`: `miqScoreNGSReadCountPublic` is
   registered over SSH, which fails with `Host key verification failed` unless you have a GitHub SSH key.
   The line is a local-only override (it does not touch the committed `.gitmodules`) that points it at
   HTTPS instead; both submodules are public.

   Do the override first, not as a retry. If `submodule update --init --recursive` ran once without it,
   the failed clone can leave the other submodule (`miqScoreShotgunPublicSupport`) empty too, and
   rerunning does not fix it. Force it to re-clone:
   ```bash
   git submodule deinit -f miqScoreShotgunPublicSupport
   git submodule update --init miqScoreShotgunPublicSupport
   ```
2. **Build the image** (Docker must be installed and you must be allowed to run it):
   ```bash
   docker build -t miqscoreshotgun .
   ```
   BWA, minimap2 and samtools are compiled inside the image and the reference genome is indexed, so the
   first build takes a while.
3. **Install the wrapper's Python packages** (Python 3.8 or newer):
   ```bash
   pip install -r requirements.txt
   ```
4. **Optional:** [seqtk](https://github.com/lh3/seqtk) for subsampling (`sudo apt-get install seqtk`).
   Without it, subsampling is turned off.

If Docker needs `sudo` on your machine, the wrapper asks for your password once and reuses it for every
sample.

## Running a batch with the wrapper

```bash
cd /where/your/fastq/folders/are
python /path/to/miqScoreShotgunPublic/run_miqscore.py
```

The wrapper asks you, one question at a time:

1. **The FASTQ folder.** The wrapper lists the folders that contain FASTQ pairs, with the number of
   samples in each: the current directory and its subfolders, then the folders you used recently. Pick
   one, or choose **Type a path...** to enter any other folder.

   In the path prompt, **Up/Down** bring back your recent folders so you can edit one (for example a
   sibling of a folder you used before) instead of typing the whole path. **Tab** works as in a Linux
   shell: it completes as far as the folder name is unambiguous, and a second Tab lists the matching
   folders. When you press Enter, a folder without FASTQ pairs is rejected, and an empty answer goes back
   to the list.

   The wrapper then shows the samples it will run and any it will skip, and asks you to confirm.

   ```
   ? Folder containing your FASTQ files:
     -- In this directory --
    » run_2025-09_fc3                     (12 samples)
     -- Recent --
      ~/runs/2025-08/run_2025-08_fc1      (8 samples)

      Type a path...

     Found 12 samples to run in /home/me/runs/2025-09/run_2025-09_fc3
       S01   S02   S03   ...
     Will be skipped, incomplete pair: S13 (no R2)
   ? Use these 12 samples? (Y/n)
   ```

   The last 10 confirmed folders are kept in `~/.config/miqscore/recent_input_folders.json` (under
   `$XDG_CONFIG_HOME` if set), one list per user and not in the repository. Folders that no longer exist
   or no longer contain FASTQ pairs are not shown. Delete the file to clear the list.
2. **The expected values:** pick a saved value set, or enter the lot number of the standard (see
   [Choosing the expected values](#choosing-the-expected-values)).
3. **Whether to subsample**, and how many reads per file (default 1,000,000).
4. **Where the results go.** `results/` in the repository is offered first; press Enter to use it. Below
   it are the output folders you used recently, and **Type a path...** for any other folder. The typed
   path works like the FASTQ one (Tab, Up/Down for recent output folders, empty goes back). The folder
   is created if it doesn't exist; a path you can't write to, or one containing `:` (Docker can't mount
   it), is rejected.

   Custom output folders are remembered in `~/.config/miqscore/recent_output_folders.json`; the default
   `results/` is not added to that list, since it is always offered first.

It then processes every sample, prints the Docker output as it goes, and ends with a summary table:

```
=====================================
Sample     MIQ  Status
-------------------------------------
even-B      96  ok
skewedA     81  ok
=====================================
13:06:35 - 2/2 samples succeeded. Summary written to .../miqscore_260916_run_2025-09_fc3_1M_lot270011_summary.csv
```

Press `Ctrl+C` at any question to cancel.

### Command-line options

Any option you give skips the matching question, so runs can be scripted:

| Option | Meaning |
|---|---|
| `--folder PATH` | Folder containing the FASTQ files. The sample list is printed without asking for confirmation; the wrapper stops if the folder has no FASTQ pairs. |
| `--lot LOT` | Lot number of the standard. If the lot is known, you only confirm its values; if not, you are asked how to set it up. |
| `--set N` | Use saved value set N (`lots/setNNN.json`) directly, without a lot number and without confirming. `--set 0` uses the default values. Cannot be combined with `--lot`. |
| `--subsample N` | Subsample each file to `N` reads; `0` turns subsampling off |
| `--output PATH` | Folder to create the run folder in (default: `results/` in the repository). Created if missing. |
| `--image NAME` | Docker image to run (default `miqscoreshotgun`) |

```bash
python run_miqscore.py --folder ~/runs/2025-09-flowcell3 --lot 270011 --subsample 1000000
python run_miqscore.py --folder ~/runs/2025-09-flowcell3 --set 0 --subsample 0
```

### Input files

The folder must contain gzipped paired-end files named `<sample>_R1.fastq.gz` and `<sample>_R2.fastq.gz`.
The part before `_R1`/`_R2` becomes the sample name.

- Samples with only one of the two files are skipped.
- Sample names must start with a letter or digit and may contain only letters, digits, `.`, `_`, `-` and
  spaces. The image rejects other names, so the wrapper skips those samples.

Both types of skipped sample are listed in the summary.

The wrapper supports only paired-end Illumina data with the standard (non-HMW) product. For single-end or
long reads, or the HMW standard, run the image directly (see
[Running the image directly](#running-the-image-directly)).

## Choosing the expected values

Value sets have no names. Each one has a number (its index), and wherever a set is shown, the lots it
covers are shown with it. The wrapper first lists the saved value sets (one line each), followed by
**Enter a lot number**:

```
? Expected values to use:
 » Set 0 (default)  lots -                       P.aer 12 | E.col 12 | S.ent 12 | ...
   Set 1            lots 261689, 238717, 252193  P.aer 10.47 | E.col 12.69 | ...
   Set 2            lots 220318, 216503          P.aer 9.29 | E.col 14.4 | ...
   Enter a lot number
```

**Pick a value set** to use it as it is, without typing a lot number. You see its full table and confirm
it. The run is labelled with the set's number (`_set2`), and the `lot_number` column of the summary stays
empty.

**Enter a lot number** to use the values that belong to your lot. The wrapper looks the lot up in `lots/`.

**Known lot:** the wrapper shows its values, and you confirm them.

```
  Set 1 (lots: 261689, 238717, 252193)
    P. aeruginosa     10.47     S. aureus         11.34
    E. coli           12.69     L. monocytogenes   12.1
    ...
? Use these values for lot 252193? (Y/n)
```

**New lot:** you choose what to do.

```
  Lot 280042 is not known yet.
? What now?
 » Link to an existing value set
   Enter new expected values
   Enter a different lot number
```

- **Link to an existing value set.** Use this when the new lot has the same values as a lot you already
  have. Browse the saved value sets (one line each), look at the full table, and confirm. The lot number
  is added to that value set, so you don't have to type the values again.
- **Enter new expected values.** A table opens where you enter the **Genomic** percentage for each of
  the 10 organisms from the lot's certificate (see [Entering new values](#entering-new-values)).
- **Enter a different lot number.** Go back to the list, for example after a typo.

### Entering new values

The table shows the new values next to the standard's defaults and the three most recently changed
value sets (set 0 and, by last commit in `lots/`, the newest other sets; files that were never committed
count as newest), so you can compare them while you type. The lots of each compared set are listed under
the table:

```
  New expected values (Genomic, %) for lot 270001

                            New                  Set 0        Set 1        Set 2
  Lot number                      270001       default  261689, 23…  220318, 21…

  P. aeruginosa                     10.9            12        10.47
  E. coli                                           12        12.69  <- missing
  S. enterica                         12            12        11.72
  ...

  Set 1: lots 261689, 238717, 252193
  Set 2: lots 220318, 216503
```

- The **New** column starts with the default values; values you change are highlighted.
- Move with the arrow keys (Tab/Shift-Tab also move down/up). You can go back to any cell at any time.
- In the New column, start typing to overwrite a cell, or press **Enter** to edit the current value.
  **Enter** confirms and moves down, **Esc** drops the edit, **Del** clears the cell.
- **Paste** several values at once, for example the column copied from the certificate (one value per
  line) or a row copied from Excel (tab-separated). They fill the New column downwards from the cursor,
  starting at P. aeruginosa if the cursor is on the Lot number row. Values that don't fit are left out,
  with a message. A single pasted value goes into the current cell like typing.
  This also works over SSH, where the paste arrives as if typed line by line: keys that arrive together
  are treated as one paste, following the same rules.
- On another set's column, **Enter** on a value copies it into New, and **Shift-C** copies the whole
  column. This helps when a new lot differs from a saved one in only a few values.
- If a saved set already has your lot's values, go to that set's **Lot number** row and press **Enter** to
  link your lot to it instead. You see the set's full table and confirm; if you say no, you are back in
  the table with your values.
- **Ctrl-S** checks everything. Problems are shown next to the row and the cursor jumps to the first
  one. If only the total is off, **Ctrl-R** rescales the values to 100. **Esc** (or Ctrl-C) leaves the
  table without saving.
- If a saved set has exactly the values you entered (every value equal, also sets not shown in the
  table), the wrapper offers to link your lot to it instead of saving a second set with the same values.
  You can still save a new set, or edit the values again.
- After the check you see the full table and choose **Save**, **Edit the values again** (back to the
  table with your values and cursor) or **Cancel**.

### Rules for expected values

- Every value must be greater than 0. A value of 0 would silently remove that organism from the score.
- The values must add up to **100**. The score compares them with the observed percentages as entered,
  without rescaling them. If your numbers don't add up (for example because of rounding on the
  certificate), press Ctrl-R in the table to rescale them to 100, or correct them.
- You only enter the Genomic values. The bacteria-only values, used by the image's `BACTERIAONLY` option,
  are calculated by leaving out the two yeasts and rescaling the eight bacteria to 100.

### What the score measures

The image converts the reads aligned to the standard into percentages per organism (they always add up
to 100), divides each by the expected percentage, and takes the root mean square of the deviations from
100%; the MIQ score is 100 minus that. Shotgun read fraction is proportional to DNA mass fraction, so the
**Genomic** column of the lot's certificate is the right comparator. Because the deviation is relative,
the two yeasts at about 2% weigh as much as any bacterium: a sample that reproduces lot 261689 exactly
scores only about 90 against the built-in 12%/2% defaults, and 100 against its own lot's values. The lot
values are Zymo's measurement of that lot, so a score of 100 means "as good as Zymo's own measurement".

## The lot library (`lots/`)

Each file in `lots/` is one **value set**: one set of expected values that one or more lot numbers share.
Sets have no names, only a number (`index`), and the file is named after it: set 2 is `lots/set002.json`.

```json
{
  "index": 2,
  "lot_numbers": ["238717", "252193"],
  "product": "standard",
  "expectedValues": {
    "Genomic":             { "paeruginosa": 12, "ecoli": 12, "...": 0 },
    "GenomicBacteriaOnly": { "paeruginosa": 12.5, "...": 0 }
  }
}
```

- The file name must match `index` (three digits, zero-padded). Lot numbers may contain only letters,
  digits, `.`, `_` and `-`, and must start with a letter or digit.
- New sets get the next number: 1 + the highest number that ever existed in `lots/`, including files
  deleted in earlier commits. A number is never reused, so `set 2` in an old summary always means the same
  values.
- A lot number can belong to only one value set.
- Set 0 (`lots/set000.json`) holds the values built into the Docker image (12% for each bacterium, 2% for
  each yeast). It has no lot numbers until you link some to it.
- The files are tracked in git. New or linked lots stay on your computer until you upload them (see
  below).

You can edit the files by hand. Run `python check_lots.py` after editing (see below). The wrapper checks
every file when it starts, and skips files it cannot use, with a warning. So a broken or invalid file is
never used for a run.

### Checking the library (`check_lots.py`)

```bash
python check_lots.py           # check, and offer to fix what can be fixed
python check_lots.py --check   # only report; exit code 1 if anything is wrong
```

The script checks each file in `lots/` separately and lists every problem:

- the file cannot be read, or its `index` does not match the file name
- lot numbers with characters that are not allowed, or a lot number listed twice in one file
- Genomic values that are missing, not greater than 0, or do not add up to 100
- `GenomicBacteriaOnly` values that do not match the Genomic values. The script offers to recalculate
  them.
- **a lot number that belongs to more than one value set.** This happens when two computers add the same
  lot and both upload it. For each such lot, the script shows the value sets and asks how to fix it:
  - If the value sets hold the same values, **merge** them into the one you pick. All lot numbers move to
    that file, and the other files are deleted. Their numbers are not reused.
  - If the values differ, **keep the lot in one value set** and remove it from the others. Check the lot's
    certificate to see which values are right.
  - **Skip** leaves the files unchanged, and the lot is still reported at the end.

Only files that pass the checks are checked for duplicate lot numbers, so fix broken files first and run
the script again. Without a terminal (for example in a script), the script only reports, like `--check`.
`python -m pytest tests` runs the same check on `lots/`.

### Uploading lots (push to GitHub)

There is no automatic sync. The wrapper only writes to `lots/` in your local copy of the repository. To
share a new or changed value set, commit it and push it to GitHub
(`https://github.com/ZRE-Services/miqScoreShotgunPublic`). You need write access to that repository.

```bash
git switch master              # upload to the branch everyone runs from
git pull                       # get the lots others uploaded first
python check_lots.py           # fix duplicate lot numbers and other problems
git status lots/               # see what changed
git add lots/
git commit -m "Add lot 280042"
git push
```

- **Pull before you add a lot**, too. Then the wrapper already knows the lots others uploaded and offers
  to link to them, instead of creating a second value set for the same lot.
- If `git push` is rejected because someone else pushed first, run `git pull`, then
  `python check_lots.py` again, and push again.
- If both of you created a new set at the same time, you both got the same number, and `git pull`
  reports a merge conflict in that file. If the values are the same, keep them and the lot numbers from
  both versions. If they differ, keep the other person's version, then re-add your lot with the wrapper
  after the pull (it gets the next free number). Then `git add lots/`, run `python check_lots.py`, and
  `git commit`.
- Other computers get the new lots when they run `git pull`.

## Output

Each batch creates a run folder inside the output folder you chose, by default `results/` in the
repository. `results/` is ignored by git, so run output never shows up as changes to commit. While the
batch runs, the folder also holds the temporary FASTQ copies, the working files and the alignments; when
the batch ends, only these are kept:

```
miqscore_<YYMMDD>_<fastq folder>_<reads>_lot<LOT>/   (or ..._set<N>/ for a value set picked without a lot)
├── <run folder name>_summary.csv   one row per sample
├── run_info.json                   date, input folder, lot, the exact values used, subsampling, image
├── reference_set<NNN>.json         the reference file the container read
└── output/
    ├── <sample>.html               MIQ report
    └── <sample>.json               detailed results
```

`<reads>` is the subsample size (e.g. `1M`, `500K`) or `fullReads`. Running the same folder with the same
lot (or value set) and read count on the same day reuses the run folder and overwrites earlier reports.
The summary only includes reports written by the current run. A copy of the summary CSV is also placed in
the repository's `results/` folder, whatever output folder you chose.

### Summary CSV (`<run folder name>_summary.csv`)

The file carries the run folder's name (date, FASTQ folder, reads, lot or value set), so it can be
copied elsewhere without losing that information.

| Column | Content |
|---|---|
| `sample` | Sample name |
| `miq_score` | MIQ score rounded as in the HTML report |
| `raw_miq_score` | MIQ score to two decimals |
| `status` | `ok`, `skipped: missing R1/R2`, `skipped: invalid sample name`, `failed: fastq preparation`, `failed: docker` or `failed: no report` |
| `lot_number` | Lot entered for the run (empty if a value set was picked directly) |
| `value_set` | Number of the value set used for the run (`0` = default) |
| `reference_file` | Path to the reference file the container read (the exact expected values) |
| `html_report` | Path to the HTML report |

### Reports

The HTML report is meant to be read in a browser and gives an overview of the sample: score, read fates,
composition against the expected one, and radar plots next to a good and a biased example. The JSON file
holds the same numbers in a form meant for scripts: read counts per organism, percentages, percent of
expected, and the plots as base64 PNG. See `exampleReport.html` and `exampleReport.json`.

### How the wrapper drives the image

The image reads its reference data from the file named in the `REFERENCEDATAFILE` environment variable.
For each batch, the wrapper:

1. copies the image's built-in `reference/zrCommunityStandard.json`,
2. replaces its `Genomic` and `GenomicBacteriaOnly` expected values with the lot's values,
3. writes the result to `working/reference_set<NNN>.json` in the run folder, and
4. starts each sample with
   ```bash
   docker container run --rm -v <run folder>:/data \
     -e SAMPLENAME=<sample> \
     -e REFERENCEDATAFILE=/data/working/reference_set<NNN>.json \
     miqscoreshotgun
   ```

Everything else in the reference file stays as it is. With set 0, the scores are identical to running the
image without the wrapper.

## Running the image directly

The image analyses one sample per run and is driven entirely by environment variables. Create a folder
with this layout (any name; `dataMountDirectory` below) and put the reads in place:

```
dataMountDirectory/
├── input/sequence/
│   ├── standard_submitted_R1.fastq      (may be gzipped)
│   └── standard_submitted_R2.fastq      (PE mode only)
├── working/
└── output/
```

Then run:

```bash
docker container run -v [pathTo]/dataMountDirectory:/data -e SAMPLENAME=My_Sample_Name miqscoreshotgun
```

The report (`<SAMPLENAME>.html`), the results (`<SAMPLENAME>.json`), the alignments and a log are written to
`output/`.

| Variable | Type | Default | Description |
|---|:-:|:-:|---|
| `SAMPLENAME` | string | **required** | Name for the sample; used in file names |
| `MODE` | string | `PE` | `PE` paired-end Illumina, `SE` single-end Illumina, `LONG` Nanopore/long reads. PE and SE share the aligner but not the analysis; SE and LONG share the analysis but not the aligner. |
| `BACTERIAONLY` | bool | `FALSE` | Score the eight bacteria only, leaving out the two yeasts |
| `REFERENCEDATAFILE` | string | `reference/zrCommunityStandard.json` (HMW file in `LONG` mode) | Expected composition and organism names; this is what the wrapper replaces per lot |
| `MAXREADCOUNT` | integer | `0` | Stop if more reads than this are provided; less than 1 means no limit |
| `FORWARDREADS` | string | `/data/input/sequence/standard_submitted_R1.fastq` | Forward reads (path inside the container) |
| `REVERSEREADS` | string | `/data/input/sequence/standard_submitted_R2.fastq` | Reverse reads (PE mode) |
| `SEQUENCEFOLDER` | string | `/data/input/sequence` | Folder containing the input sequences |
| `WORKINGFOLDER` | string | `/data/working` | Folder for temporary files |
| `OUTPUTFOLDER` | string | `/data/output` | Folder for the reports |
| `REFERENCEGENOME` | string | `reference/zrCommunityStandard.fa` | Reference genome of the standard (indexed at build time) |
| `FILENAMINGSTANDARD` | string | `ZYMO` | How sequence files are named; the other option is `illumina` |

## Troubleshooting

| Message | Fix |
|---|---|
| `Docker image 'miqscoreshotgun' not found` | Build the image (see [Setup](#setup)), or pass `--image` with the name of your image. |
| `seqtk not found; subsampling disabled` | Install seqtk, or run without subsampling. |
| `No *_R1.fastq.gz / *_R2.fastq.gz files in this folder` | Check the folder and the file names (`.fastq.gz`, `_R1`/`_R2` just before the extension). |
| `Lot number(s) ... already belong to Set ...` | That lot is already linked to another value set. Use it, or fix the files in `lots/`. |
| `... has index ...; it must be named setNNN.json` | A file in `lots/` was renamed or its `index` edited by hand. Make the file name and `index` match. |
| `Skipping lots/...: ...` | That file cannot be used (see the reason). Run `python check_lots.py` and fix the file. |
| `Lot ... is listed in several value sets` | The same lot was added twice, usually on two computers. Run `python check_lots.py` to consolidate. |
| `failed: docker` in the summary | Look at the Docker output above the summary. Rerun the sample without the wrapper to keep the container's log (see [Running the image directly](#running-the-image-directly)). |
| `Host key verification failed` during `git submodule update` | See [Setup](#setup): point the submodule at HTTPS first. |

## Development

```bash
pip install -r requirements.txt
python -m pytest tests            # wrapper, lot store and value table
python check_lots.py --check      # validate lots/
```

- `lotstore.py` holds the lot library, validation and reference merging, with no UI code.
- `lot_editor.py` is the value table (prompt_toolkit); its `Sheet` class holds the state and key actions
  without terminal code and is tested directly.
- `run_miqscore.py` holds the prompts, FASTQ handling, Docker calls and summary.
- `check_lots.py` checks and repairs `lots/`.
- `analyzeStandardReads.py` is the image's entry point; `defaults/` holds its default paths and constants,
  `reference/` the reference genome, the expected-composition JSON files, the example reports used in the
  charts and the HTML template. The analysis code itself lives in the two submodules.
- `requirements.txt` is for the wrapper on the host; `requirements-image.txt` pins the packages the
  Docker image installs (Python 3.7) and is only used by the `Dockerfile`.

There is no CI. The tests cover the host-side wrapper only; the image is tested by running it.

## Authors and license

Original image and analysis: Michael M. Weinstein (project lead), Aishani Prem, Mingda Jin, Shuiquan
Tang, Jeffrey Bhasin, Ryan Kemp, Brian Janssen and Christopher E. Mason, at
[Zymo Research](https://github.com/Zymo-Research/miqScoreShotgunPublic). The upstream project intends to
license under the GNU GPLv3; see [LICENSE](LICENSE).
