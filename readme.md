# DO2i Dashboard – Current Working Snapshot

## What this is
This repository contains a snapshot of my current working DO2i (oxygen delivery index)
plotting code. This version runs and produces output, but has known limitations and
technical debt.

## What works
- Live data ingest (serial or file)
- DO2i plotting
- Threshold visualization (e.g. <270 mL/min/m²)
- Basic AUC calculation

## Known issues / limitations
- Architecture needs refactoring
- AUC logic is coupled to plotting loop
- Other issues to be documented

## Intent
This snapshot is preserved as a baseline before refactoring.

## How to run
python3 do2progress.py
