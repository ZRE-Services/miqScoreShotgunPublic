# MIQ Score batch wrapper with lot management

`run_miqscore.py` runs the MIQ Score Docker image on every sample in a folder of paired FASTQ files, using
the **expected values for the lot** of ZymoBIOMICS standard you sequenced.

The expected composition of the standard differs from lot to lot. The wrapper keeps a library of expected
values in `lots/` and gives them to the unchanged Docker image at run time, so you never need to build a
separate image for a lot.

## Requirements

- Python 3.8 or newer
- Docker, with the image built from this repository:
  ```bash
  git clone https://github.com/ZRE-Services/miqScoreShotgunPublic.git
  cd miqScoreShotgunPublic
  git config submodule.miqScoreNGSReadCountPublic.url https://github.com/Zymo-Research/miqScoreNGSReadCountPublic.git
  git submodule update --init --recursive
  docker build -t miqscoreshotgun .
  ```
  The `git config` line is required before the first `submodule update`: `miqScoreNGSReadCountPublic` is
  registered over SSH, which fails with `Host key verification failed` unless you have a GitHub SSH key
  configured. That line is a local-only override (it does not touch the committed `.gitmodules`) that
  points it at HTTPS instead, since both submodules are public.

  Do the override first, not as a retry after a failure — if `submodule update --init --recursive` is run
  once without it, the failed clone can leave the *other* submodule (`miqScoreShotgunPublicSupport`) stuck
  empty too, even though the log shows it cloning, and simply rerunning the command afterwards will not
  fix it. If you already hit this, force it to re-clone:
  ```bash
  git submodule deinit -f miqScoreShotgunPublicSupport
  git submodule update --init miqScoreShotgunPublicSupport
  ```
- The wrapper's Python packages:
  ```bash
  pip install -r requirements-wrapper.txt
  ```
- Optional: [seqtk](https://github.com/lh3/seqtk) for subsampling (`sudo apt-get install seqtk`). Without it,
  subsampling is turned off.

If Docker needs `sudo` on your machine, the wrapper asks for your password once and reuses it for every
sample.

## Quick start

```bash
cd /where/your/fastq/folders/are
python /path/to/miqScoreShotgunPublic/run_miqscore.py
```

The wrapper asks you, one question at a time:

1. **The FASTQ folder.** The wrapper lists the current directory and its subfolders that contain FASTQ
   pairs, with the number of samples in each. Pick one, or choose **Type a path...** to enter any other
   folder (Tab completes it; a folder without FASTQ pairs is rejected, and an empty answer goes back to
   the list). It then shows the samples it will run and any it will skip, and asks you to confirm.

   ```
   ? Folder containing your FASTQ files:
    » in5081_FC186_1M   (1 sample)
      run_2025-09_fc3   (12 samples)
      Type a path...

     Found 12 samples to run in /home/me/runs/run_2025-09_fc3
       S01   S02   S03   ...
     Will be skipped, incomplete pair: S13 (no R2)
   ? Use these 12 samples? (Y/n)
   ```
2. **The expected values:** pick a saved value set, or enter the lot number of the standard (see
   [Choosing the expected values](#choosing-the-expected-values)).
3. **Whether to subsample**, and how many reads per file (default 1,000,000).

It then processes every sample, prints the Docker output as it goes, and ends with a summary table:

```
=====================================
Sample     MIQ  Status
-------------------------------------
even-B      96  ok
skewedA     81  ok
=====================================
13:06:35 - 2/2 samples succeeded. Summary written to .../260916_run_2025-09_fc3_1M_lot270011_miqscore_summary.csv
```

Press `Ctrl+C` at any question to cancel.

### Command-line options

Any option you give skips the matching question, so runs can be scripted:

| Option | Meaning |
|---|---|
| `--folder PATH` | Folder containing the FASTQ files. The sample list is printed without asking for confirmation; the wrapper stops if the folder has no FASTQ pairs. |
| `--lot LOT` | Lot number of the standard. If the lot is known, you only confirm its values; if not, you are asked how to set it up. |
| `--value-set NAME` | Use the saved value set `lots/NAME.json` directly, without a lot number and without confirming. Cannot be combined with `--lot`. |
| `--subsample N` | Subsample each file to `N` reads; `0` turns subsampling off |
| `--image NAME` | Docker image to run (default `miqscoreshotgun`) |

```bash
python run_miqscore.py --folder ~/runs/2025-09-flowcell3 --lot 270011 --subsample 1000000
python run_miqscore.py --folder ~/runs/2025-09-flowcell3 --value-set default --subsample 0
```

## Input files

The folder must contain gzipped paired-end files named `<sample>_R1.fastq.gz` and `<sample>_R2.fastq.gz`.
The part before `_R1`/`_R2` becomes the sample name.

- Samples with only one of the two files are skipped.
- Sample names must start with a letter or digit and may contain only letters, digits, `.`, `_`, `-` and
  spaces. MIQ Score rejects other names, so the wrapper skips those samples.

Both types of skipped sample are listed in the summary.

Only paired-end Illumina data with the standard (non-HMW) product is supported.

## Choosing the expected values

The wrapper first lists the saved value sets (one line each), followed by **Enter a lot number**:

```
? Expected values to use:
 » default                  P.aer 12 | E.col 12 | S.ent 12 | ...
   Enter a lot number
```

**Pick a value set** to use it as it is, without typing a lot number. You see its full table and confirm
it. The run is labelled with the value set's name, and the `lot_number` column of the summary stays empty.

**Enter a lot number** to use the values that belong to your lot. The wrapper looks the lot up in `lots/`.

**Known lot:** the wrapper shows its values, and you confirm them.

```
  Value set 'default' (lots: 238717, 252193, 270011)
    P. aeruginosa        12     S. aureus            12
    E. coli              12     L. monocytogenes     12
    ...
? Use these values for lot 270011? (Y/n)
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
- **Enter new expected values.** Give the value set a name (the lot number by default), then enter the
  **Genomic** percentage for each of the 10 organisms from the lot's certificate. The standard's default
  values are pre-filled. You see a preview before anything is saved.
- **Enter a different lot number.** Go back to the list, for example after a typo.

### Rules for expected values

- Every value must be greater than 0. A value of 0 would silently remove that organism from the score.
- The values must add up to **100**. MIQ Score compares them with the observed percentages as entered,
  without rescaling them. If your numbers don't add up (for example because of rounding on the
  certificate), you can rescale them to 100 or enter them again.
- You only enter the Genomic values. The bacteria-only values, used by the image's `BACTERIAONLY` option,
  are calculated by leaving out the two yeasts and rescaling the eight bacteria to 100.

## The lot library (`lots/`)

Each file in `lots/` is one **value set**: one set of expected values that one or more lot numbers share.

```json
{
  "name": "238717_252193",
  "lot_numbers": ["238717", "252193"],
  "product": "standard",
  "expectedValues": {
    "Genomic":             { "paeruginosa": 12, "ecoli": 12, "...": 0 },
    "GenomicBacteriaOnly": { "paeruginosa": 12.5, "...": 0 }
  }
}
```

- The file name must match `name`. Names and lot numbers may contain only letters, digits, `.`, `_` and
  `-`, and must start with a letter or digit.
- A lot number can belong to only one value set.
- `lots/default.json` holds the values built into the Docker image (12% for each bacterium, 2% for each
  yeast). It has no lot numbers until you link some to it.
- The files are tracked in git. After adding or linking a lot, **commit the change to `lots/`** so
  everyone else gets it:
  ```bash
  git add lots/
  git commit -m "Add lot 280042"
  ```

You can edit the files by hand. The wrapper checks them when you save a new or linked lot, and
`python -m pytest tests` checks the library code.

## Output

Each run creates a folder inside `results/` in the repository, wherever you start the wrapper from.
`results/` is ignored by git, so run output never shows up as changes to commit:

```
<YYMMDD>_<fastq folder>_<reads>_lot<LOT>_miqscore/   (or ..._set<NAME>_miqscore/ for a picked value set)
├── <run folder name>_summary.csv   one row per sample
├── run_info.json                  date, input folder, lot, the exact values used, subsampling, image
├── input/sequence/                temporary FASTQ copies (emptied when the batch ends)
├── working/
│   └── reference_<set>.json       the reference file the container used
└── output/
    ├── <sample>.html              MIQ report
    ├── <sample>.json              detailed results
    ├── <sample>.bam               alignments
    └── dada2.<timestamp>.log      container log (one per sample)
```

`<reads>` is the subsample size (e.g. `1M`, `500K`) or `fullReads`. Running the same folder with the same
lot (or value set) and read count on the same day reuses the run folder and overwrites earlier reports. The summary only
includes reports written by the current run.

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
| `value_set` | Value set used for the run |
| `html_report` | Path to the HTML report |

## How it works

The Docker image reads its reference data from the file named in the `REFERENCEDATAFILE` environment
variable. For each run, the wrapper:

1. copies the image's built-in `reference/zrCommunityStandard.json`,
2. replaces its `Genomic` and `GenomicBacteriaOnly` expected values with the lot's values,
3. writes the result to `working/reference_<set>.json`, and
4. starts each sample with
   ```bash
   docker container run --rm -v <run folder>:/data \
     -e SAMPLENAME=<sample> \
     -e REFERENCEDATAFILE=/data/working/reference_<set>.json \
     miqscoreshotgun
   ```

Everything else in the reference file stays as it is. The good and bad example charts in the HTML report
are the same for every lot. With `lots/default.json`, the scores are identical to running the image
without the wrapper.

## Troubleshooting

| Message | Fix |
|---|---|
| `Docker image 'miqscoreshotgun' not found` | Build the image (see [Requirements](#requirements)), or pass `--image` with the name of your image. |
| `seqtk not found; subsampling disabled` | Install seqtk, or run without subsampling. |
| `No *_R1.fastq.gz / *_R2.fastq.gz files found!` | Check the folder and the file names (`.fastq.gz`, `_R1`/`_R2` just before the extension). |
| `Lot number(s) ... already belong to value set ...` | That lot is already linked to another value set. Use it, or fix the files in `lots/`. |
| `... has name '...'; the name must match the file name` | A file in `lots/` was renamed by hand. Make its `name` match the file name. |
| `failed: docker` in the summary | Look at the Docker output above the summary and at `output/dada2.*.log`. |

## Development

```bash
pip install -r requirements-wrapper.txt
python -m pytest tests
```

- `lotstore.py` contains the lot library, validation and reference merging. It has no UI code, and all
  tests target it.
- `run_miqscore.py` contains the prompts, FASTQ handling, Docker calls and summary.
