# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Dockerized command-line tool that computes a "MIQ" (Measurement Integrity Quotient) score for shotgun
metagenomic sequencing runs against the ZymoBIOMICS mock community standard. It aligns submitted reads
(via BWA for PE/SE Illumina reads, minimap2 for Nanopore/long reads) to the standard reference genome,
analyzes the resulting BAM, and produces an HTML + JSON report scoring how well the sequencing run
reproduces the expected community composition.

The entry point (`analyzeStandardReads.py`) is designed to run as the `CMD` of a Docker container, driven
entirely by environment variables — there is no CLI argument parsing.

## Critical setup: submodules

This repo depends on two sibling repos checked out as **git submodules**, and they contain essentially all
of the actual analysis logic:

- `miqScoreShotgunPublicSupport` — parameter parsing (`parameters.environmentParameterParser`), fastq
  validation (`formatReaders.fastq.fastqHandler`), the BWA/minimap2 wrappers (`alignmentAnalysis.bwaHandler`,
  `alignmentAnalysis.minimap2`), BAM processing (`alignmentAnalysis.alignmentAnalysisPE/SE`), and HTML
  report templating (`reporting`).
- `miqScoreNGSReadCountPublic` — the actual MIQ score calculator (`MiqScoreCalculator`), reference handling
  (`referenceHandler.StandardReference`), and plot generation (read fate chart, radar plots, composition
  bar plot).

These directories are **empty until initialized**:
```bash
git submodule update --init --recursive
```
If either submodule directory is empty, imports in `analyzeStandardReads.py` (`import
miqScoreShotgunPublicSupport`, `import miqScoreNGSReadCountPublic`) will fail — check this first before
debugging import errors. When reasoning about behavior that isn't in this repo (parameter validation rules,
alignment logic, MIQ score math, plotting), look inside these submodules, not in this repo's own files.

`.gitmodules` points `miqScoreNGSReadCountPublic` at an SSH URL (`git@github.com:...`), which fails with
"Host key verification failed" in environments without a configured GitHub SSH key/known_hosts (e.g. this
sandbox). Both repos are public, so if the SSH clone fails, point the submodule at HTTPS locally instead
(this is a local-only override, it does not touch the committed `.gitmodules`):
```bash
git config submodule.miqScoreNGSReadCountPublic.url https://github.com/Zymo-Research/miqScoreNGSReadCountPublic.git
git submodule update --init --recursive
```

## Build & run

```bash
git clone --recursive https://github.com/Zymo-Research/miqScoreShotgunPublic.git
docker build -t miqscoreshotgun .
```

There is no local (non-Docker) run path documented or tested — BWA, minimap2, and samtools are compiled
from source in the Dockerfile and expected on `PATH` at `/opt/...`. The image installs `requirements-image.txt`
(Python 3.7 pins); `requirements.txt` is the host wrapper's. `.dockerignore` keeps the wrapper, tests, `lots/`
and run output out of the image. To run:

```bash
docker container run -v [pathTo]/dataMountDirectory:/data -e SAMPLENAME=My_Sample_Name miqscoreshotgun
```

expecting a mounted directory shaped like:
```
dataMountDirectory/
+-- input/sequence/standard_submitted_R1.fastq   (+ _R2.fastq for PE)
+-- working/
+-- output/
```

There is no CI. The only automated tests cover the host-side wrapper: lot store, lot editor, and the
new-lot flow of `run_miqscore.py` with the prompts replaced by scripted answers (see below):
```bash
pip install -r requirements.txt
python -m pytest tests                      # all
python -m pytest tests -k bacteria_only     # single test
python check_lots.py --check                # validate lots/
```

## Host wrapper and lot-specific expected values

The expected organism percentages differ between lots of the ZymoBIOMICS standard. Instead of building one
image per lot, the host-side wrapper `run_miqscore.py` runs the unchanged `miqscoreshotgun` image. For each
run it writes a merged reference JSON to `<run folder>/working/reference_set<NNN>.json` and passes it in with
`-e REFERENCEDATAFILE=/data/working/...`.

- `lotstore.py` holds the storage, validation and merge logic and has no UI code. `lot_editor.py` is the
  prompt_toolkit table for entering a new lot's values next to the defaults and the 3 newest value sets
  (`LotStore.newest_first`, by last git commit); its `Sheet` class holds the state and key actions without
  terminal code, so it is tested directly. The wrapper keeps one `Sheet` per new lot and reruns it, so the user
  returns to their values after declining a link. `lot_editor.run()` returns `(SAVE, values)` or `(LINK, index)`
  (Enter on a compared set's lot row); on SAVE the wrapper offers to link instead if `LotStore.find_by_values()`
  finds a set with exactly those values. `run_miqscore.py` is the
  terminal UI (questionary prompts; no GUI/Tkinter). It handles folder choice, lot selection, seqtk
  subsampling, one `docker run` per `*_R1/_R2.fastq.gz` pair, and writes `<run folder name>_summary.csv` and
  `run_info.json` into the run folder `miqscore_<yymmdd>_<input folder>_<reads>_<lot or set>` under the chosen
  results folder (default `results/`, gitignored). After the run only the summary, `run_info.json`, the
  `output/*.html`/`*.json` reports and the reference JSON (moved from `working/` to the run folder, and linked
  from the CSV's `reference_file` column) are kept, and a copy of the summary CSV always goes to the repo's `results/`.
  Recent input/output folders are kept per user in `~/.config/miqscore/recent_{input,output}_folders.json`.
- `lots/setNNN.json` files are tracked in git. Each file is one value set that can cover several lot
  numbers (`lot_numbers`); a lot number may appear in only one file. Sets have no names, only an integer
  `index` that must match the zero-padded file name; the UI refers to them as "Set N" and always shows the
  lots they cover. `LotStore.create()` assigns `next_index()` = 1 + the highest index in `lots/` or anywhere in
  its git history, so indexes are never reused. Only `Genomic` values are entered; `GenomicBacteriaOnly` is
  derived by dropping the two yeasts and rescaling. Set 0 (`lots/set000.json`) holds the repo's original
  12/2 values.
- There is no automatic sync: lots are shared by committing `lots/` and pushing. `check_lots.py` checks each
  file on its own (via `LotStore.check()`), reports lot numbers that are listed in several files (as happens
  after two machines push the same lot), and asks how to consolidate them. `--check` only reports.
  `tests/test_lotstore.py::test_committed_lot_files_are_valid` runs the same check on the real `lots/`.
- `LotStore.load()` validates each file, and `value_sets()`/`get()`/`find_by_lot()` silently leave out files
  that fail (the wrapper warns about them at startup). `ValueSet.write_reference()` validates again, so invalid
  values never reach the image.
- The Genomic values must be > 0 and add up to 100. The MIQ calculator (`calculateObservedPercentOfExpected`)
  uses them as given against observed percentages that always add up to 100, and a value of 0 or null
  silently removes that organism from the score.
- Only the standard product with PE mode is supported. HMW/LONG needs its own base file
  (`zrCommunityStandardHMW.json` has a different organism list); the `product` field and
  `lotstore.BASE_REFERENCES` are the extension point.

## Configuration model

All runtime configuration is via environment variables, parsed by `EnvParameters` (from
`miqScoreShotgunPublicSupport.parameters.environmentParameterParser`) in `analyzeStandardReads.py`. Key
variables: `SAMPLENAME` (required), `MODE` (`PE`/`SE`/`LONG`), `MAXREADCOUNT`, `FORWARDREADS`/
`REVERSEREADS`/`READS`, `SEQUENCEFOLDER`, `WORKINGFOLDER`, `OUTPUTFOLDER`, `REFERENCEGENOME`,
`FILENAMINGSTANDARD`, `BACTERIAONLY`. See README.md for the full table and defaults.

Static defaults (paths, thresholds, DADA2-style filtering params) live in `defaults/`:
- `defaults/environment.py` — default file/folder paths, all derived from `/data`.
- `defaults/standard.py` — analysis constants (`r1MaxEE`, `truncQ`, `minOverlap`, `maxMismatch`, etc.) plus
  re-exports of `environment.py`.
- `defaults/_minReadLengths.py` — minimum read length per amplicon region (legacy from the amplicon
  pipeline this was forked from; largely unused by the shotgun path).

`loadDefaultPackage()` in `analyzeStandardReads.py` loads whichever default package name is given via the
`DEFAULTPACKAGENAME` env var (defaults to `"standard"`), so alternate default modules can be added under
`defaults/` and selected at runtime.

## Application flow (`analyzeStandardReads.py`)

1. `getApplicationMode()` reads `MODE` env var → `PE` | `SE` | `LONG`.
2. `getApplicationParameters{PE,SE,Long}()` builds the full parameter set for that mode, including
   selecting the correct reference/example-MIQ JSON files based on `MODE` and `BACTERIAONLY`:
   - Standard PE/SE reference: `reference/zrCommunityStandard.json` and `good/badMiq[BacteriaOnly].json`
   - LONG (HMW) reference: `reference/zrCommunityStandardHMW.json` and `good/badMiq[BacteriaOnly]HMW.json`
3. Alignment: PE/SE modes call `bwaHandler.bwaAlignPE/SE`; LONG mode calls `minimap2.minimapAlign`. PE and
   LONG/SE each have distinct BAM-processing logic (`alignmentAnalysisPE` vs `alignmentAnalysisSE` — LONG
   reuses the SE processor).
4. `analyzeStandardResult()` feeds the alignment read table into
   `miqScoreNGSReadCountPublic.MiqScoreCalculator`, selecting `analysisMethod` = `"Genomic"` or
   `"GenomicBacteriaOnly"` based on `BACTERIAONLY`, and generates all plots on the result object.
5. `saveResult()` writes `{outputFolder}/{sampleName}.json`.
6. `generateReport()` fills `reference/shotgunReportTemplate.html` with score/plots/tables via
   `miqScoreNGSReadCountPublic.reportGeneration.generateReport` and writes
   `{outputFolder}/{sampleName}.html`.

## Reference data (`reference/`)

- `zrCommunityStandard.fa` (+ `.fai`) — the mock community reference genome, BWA-indexed at Docker build
  time (`bwa index`) for speed.
- `zrCommunityStandard.json` / `zrCommunityStandardHMW.json` — expected composition data for standard vs.
  HMW (long-read) analysis.
- `good/badMiq*.json` — example "good" and "bad" MIQ results (standard, bacteria-only, and HMW variants)
  used as reference points on report charts.
- `shotgunReportTemplate.html` — HTML template with placeholder tokens (e.g. `SAMPLENAME`, `MIQSCORE`,
  `READFATETABLE`, `*RADARPLOT*`) substituted by `generateReportReplacementTable()`.

Editing any reference JSON/template requires keeping the placeholder tokens and the four MIQ variants
(standard/bacteria-only × normal/HMW) in sync with the code paths in `getApplicationParameters*()`.
