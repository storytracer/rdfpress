# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Single-file Python CLI tool (`rdfpress.py`) that bulk-converts RDF/XML files to queryable JSONL or standards-compliant JSON-LD using rdflib. Uses Click for CLI, tqdm for progress, and `ProcessPoolExecutor` for parallelism.

## Running

Requires [uv](https://docs.astral.sh/uv/) — no manual dependency installation needed:

```bash
uv run rdfpress.py single input.xml           # single file → simplified JSON
uv run rdfpress.py single input.xml --jsonld   # single file → JSON-LD
uv run rdfpress.py batch input_dir/ -o out.jsonl.gz        # directory/zip → gzipped JSONL
uv run rdfpress.py batch zip_folder/ -o out_dir/ -w 8      # folder of zips, 8 workers
```

## Architecture

The entire codebase is a single script with inline PEP 723 script metadata (dependencies declared in the `# /// script` header). It's also installable as a package via `pyproject.toml` (hatchling backend).

**Processing pipeline:** RDF/XML → rdflib Graph → JSON-LD serialization → optional simplification → output

Key functions:
- `parse_rdfxml(source, jsonld)` — core parser, accepts file path or raw bytes
- `rekey_by_type(data)` — restructures `@graph` array into dict keyed by `@type` (simplified mode only)
- `simplify_value(v)` / `simplify_node(node)` — unwrap JSON-LD value containers losslessly
- `_parse_chunk(zip_path, entry_names, jsonld)` — worker function that opens zip independently per process (avoids pickling bytes across process boundaries)
- `_batch_from_zips(...)` — processes zips sequentially through a shared worker pool with adaptive chunking
- `_batch_single(...)` — parallel processing for directory/single-zip inputs

**Parallelism model:** For zip inputs, adaptive chunking (`_adaptive_chunk_size`) targets `workers*8` tasks per zip. Workers re-open the zip file independently to avoid serializing file contents across processes. Memory is bounded to O(1 zip) regardless of batch size.

## Key conventions

- Python ≥ 3.11 required (uses `str | bytes` union syntax, etc.)
- No test suite currently exists
- `rdflib.term` logger is silenced at ERROR level (noisy but harmless warnings about XSD type conversions)
- Batch output uses atomic write pattern: write to `.tmp`, rename on success
- `--resume` flag enables crash recovery by skipping zips with existing output files
