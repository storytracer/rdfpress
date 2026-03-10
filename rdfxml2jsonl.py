# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rdflib>=7.0",
#     "click>=8.0",
#     "tqdm>=4.0",
# ]
# ///
"""
rdfxml2jsonl — Bulk-convert RDF/XML to queryable JSONL or JSON-LD.

Parses RDF/XML files using rdflib and outputs either simplified JSON
for efficient bulk processing, or standards-compliant JSON-LD suitable
for use in RDF pipelines.

OUTPUT MODES
============

Simplified JSON (default):
    Optimised for analytical querying in tools like DuckDB, jq, Pandas,
    or Polars. The flat @graph array is restructured so that each RDF
    class becomes a top-level key, and JSON-LD wrappers are stripped
    to produce compact, directly queryable output.

    This is a one-way transformation — the result is NOT valid JSON-LD
    and cannot be round-tripped back to RDF without the information
    that was removed. Specifically:

      - @context is dropped (prefixed keys are baked into property names).
      - @type is removed from nodes (already encoded as the grouping key).
      - {"@id": "http://..."} references are unwrapped to plain strings.
      - {"@value": 42, "@type": "xsd:long"} typed literals are unwrapped
        to their native JSON types.
      - Language-tagged strings are preserved as-is because the tag
        carries information that would otherwise be lost:
          {"@value": "example", "@language": "en"} -> (unchanged)

JSON-LD (--jsonld):
    Standards-compliant JSON-LD that can be loaded by any JSON-LD
    processor and round-tripped back to RDF. The @context, @type,
    @id references, and typed value wrappers are all preserved exactly
    as rdflib serialises them. The compact context is derived from each
    file's own xmlns namespace declarations.

Usage:
    uv run rdfxml2jsonl.py single input.xml
    uv run rdfxml2jsonl.py single input.xml --jsonld
    uv run rdfxml2jsonl.py batch  input_dir/ -o output.jsonl.gz
    uv run rdfxml2jsonl.py batch  archive.zip -o output.jsonl.gz --jsonld
    uv run rdfxml2jsonl.py batch  zip_folder/ -o out_dir/ -w 8
"""

import gzip
import json
import logging
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Generator

import click
from rdflib import Graph
from tqdm import tqdm

# rdflib logs noisy tracebacks for certain literal type conversions that fail
# (e.g. values that don't match their declared XSD datatype). The original
# lexical form is always preserved — no data is lost — so we silence these.
logging.getLogger("rdflib.term").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Namespace / context helpers
# ---------------------------------------------------------------------------

def context_from_graph(g: Graph) -> dict:
    """
    Build a JSON-LD context dict from the namespace bindings that rdflib
    discovered while parsing. This produces compact prefixed property
    names without requiring a hardcoded context.
    """
    return {
        prefix: str(uri)
        for prefix, uri in g.namespace_manager.namespaces()
        if prefix  # skip the default empty prefix
    }


# ---------------------------------------------------------------------------
# Value simplification (used only in simplified JSON mode)
# ---------------------------------------------------------------------------

def simplify_value(v: Any) -> Any:
    """
    Recursively unwrap JSON-LD value containers where the unwrapping is
    lossless.

    Unwrapped (no information lost):
      {"@id": "http://..."}                    ->  "http://..."
      {"@value": 42, "@type": "xsd:long"}      ->  42
      {"@value": "hello"}                       ->  "hello"

    Kept as-is (language tag carries information):
      {"@value": "example", "@language": "en"}  ->  unchanged

    All other dicts and lists are recursed into.
    """
    if isinstance(v, dict):
        keys = set(v.keys())

        # Pure URI reference
        if keys == {"@id"}:
            return v["@id"]

        # Typed or plain literal without a language tag
        if "@value" in keys and "@language" not in keys:
            return v["@value"]

        # Anything else: recurse
        return {k: simplify_value(val) for k, val in v.items()}

    if isinstance(v, list):
        return [simplify_value(item) for item in v]

    return v


def simplify_node(node: dict) -> dict:
    """
    Simplify a single graph node: strip @type (it is redundant because
    it already serves as the grouping key) and unwrap all nested values.
    """
    return {
        k: simplify_value(v)
        for k, v in node.items()
        if k != "@type"
    }


# ---------------------------------------------------------------------------
# Graph restructuring (used only in simplified JSON mode)
# ---------------------------------------------------------------------------

def rekey_by_type(data: dict) -> dict:
    """
    Restructure a JSON-LD document by replacing the flat @graph array
    with a dict keyed by each node's @type.

    - Single-occurrence types  -> stored as an object
    - Multi-occurrence types   -> stored as an array
    - Nodes without @type      -> collected under "_untyped"
    - Nodes with multiple types appear under each type key

    The @context key is dropped from the output since prefixed keys are
    already baked into the property names.
    """
    graph = data.get("@graph", [])
    if not graph:
        return data

    grouped: dict[str, list[dict]] = {}

    for node in graph:
        raw_type = node.get("@type")

        if raw_type is None:
            types = ["_untyped"]
        elif isinstance(raw_type, list):
            types = raw_type
        else:
            types = [raw_type]

        for t in types:
            grouped.setdefault(t, []).append(node)

    result: dict[str, Any] = {}

    for type_key, nodes in grouped.items():
        nodes = [simplify_node(n) for n in nodes]
        result[type_key] = nodes[0] if len(nodes) == 1 else nodes

    return result


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_rdfxml(source: str | bytes, jsonld: bool = False) -> dict:
    """
    Parse an RDF/XML source and return a dict.

    Args:
        source: A filesystem path (str) or raw XML bytes.
        jsonld: If True, return valid JSON-LD with all wrappers intact.
                If False (default), return simplified JSON keyed by @type.
    """
    g = Graph()

    if isinstance(source, bytes):
        g.parse(BytesIO(source), format="xml")
    else:
        g.parse(source, format="xml")

    context = context_from_graph(g)
    jsonld_str = g.serialize(format="json-ld", indent=None, context=context)
    data = json.loads(jsonld_str)

    if jsonld:
        return data

    return rekey_by_type(data)


# ---------------------------------------------------------------------------
# File iterators (directory and zip)
# ---------------------------------------------------------------------------

def iter_xml_from_dir(
    dir_path: Path, pattern: str
) -> Generator[tuple[str, str | bytes], None, None]:
    """Yield (filename, filepath) for files matching *pattern* in a directory."""
    for f in sorted(dir_path.glob(pattern)):
        yield f.name, str(f)


def iter_xml_from_zip(
    zip_path: Path, pattern: str
) -> Generator[tuple[str, str | bytes], None, None]:
    """Yield (filename, xml_bytes) for matching entries inside a zip archive."""
    suffix = pattern.replace("*", "")  # e.g. "*.xml" -> ".xml"
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = sorted(
            n for n in zf.namelist()
            if n.endswith(suffix) and not n.startswith("__MACOSX")
        )
        for name in members:
            yield Path(name).name, zf.read(name)


# ---------------------------------------------------------------------------
# Parallel processing workers
# ---------------------------------------------------------------------------

_CHUNK_SIZE = 100  # entries per worker task — balances IPC overhead vs utilisation


def _init_worker():
    """Silence noisy rdflib warnings in worker processes."""
    logging.getLogger("rdflib.term").setLevel(logging.ERROR)


def _parse_one(
    name: str, source: str | bytes, jsonld: bool,
) -> tuple[str, str | None, str | None]:
    """Worker: parse one RDF/XML file, return (name, json_line, error_msg)."""
    try:
        data = parse_rdfxml(source, jsonld=jsonld)
        data["_source_file"] = name
        line = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return (name, line, None)
    except Exception as e:
        return (name, None, f"{name} — {e}")


def _parse_chunk(
    zip_path: str, entry_names: list[str], jsonld: bool,
) -> list[tuple[str, str | None, str | None]]:
    """Worker: open a zip, read + parse a chunk of entries.

    Each worker opens the zip independently so no bytes are pickled across
    process boundaries.  Returns [(source_name, json_line | None, error | None), ...].
    """
    results: list[tuple[str, str | None, str | None]] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for entry_name in entry_names:
            source_name = Path(entry_name).name
            try:
                xml_bytes = zf.read(entry_name)
                data = parse_rdfxml(xml_bytes, jsonld=jsonld)
                data["_source_file"] = source_name
                line = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                results.append((source_name, line, None))
            except Exception as e:
                results.append((source_name, None, f"{source_name} — {e}"))
    return results


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------

def _batch_from_zips(
    zip_outputs: list[tuple[Path, Path]],
    jsonld: bool, pattern: str, workers: int,
):
    """Process one or more zip files with fine-grained chunk-based parallelism.

    *zip_outputs* is a list of (zip_path, output_path) pairs.  Each zip
    produces its own .jsonl.gz.  XML entries from **all** zips are chunked
    and distributed across workers so every core stays busy regardless of
    individual zip sizes.
    """
    suffix = pattern.replace("*", "")  # e.g. "*.xml" -> ".xml"

    # --- 1. Enumerate entries (central-directory only, no decompression) ---
    # chunks: [(zip_path_str, output_path_str, [entry_name, …]), …]
    chunks: list[tuple[str, str, list[str]]] = []
    total_files = 0

    for zp, out in zip_outputs:
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zp, "r") as zf:
            members = sorted(
                n for n in zf.namelist()
                if n.endswith(suffix) and not n.startswith("__MACOSX")
            )
        total_files += len(members)
        zp_str, out_str = str(zp), str(out)
        for i in range(0, len(members), _CHUNK_SIZE):
            chunks.append((zp_str, out_str, members[i : i + _CHUNK_SIZE]))

    if total_files == 0:
        click.echo("No matching files found in the provided zip(s).", err=True)
        sys.exit(1)

    # --- 2. Process chunks in parallel ---
    pool_size = min(workers, len(chunks))
    # keyed by output path → {ok, fail}
    stats: dict[str, dict[str, int]] = {
        str(out): {"ok": 0, "fail": 0} for _, out in zip_outputs
    }
    gz_handles: dict[str, gzip.GzipFile] = {}

    try:
        for _, out in zip_outputs:
            gz_handles[str(out)] = gzip.open(
                str(out), "wt", encoding="utf-8", compresslevel=6,
            )

        if pool_size <= 1:
            # Sequential
            for zp_str, out_str, names in tqdm(chunks, desc="Converting", unit="chunk"):
                results = _parse_chunk(zp_str, names, jsonld)
                gz = gz_handles[out_str]
                for _name, line, error in results:
                    if line is not None:
                        gz.write(line)
                        gz.write("\n")
                        stats[out_str]["ok"] += 1
                    else:
                        tqdm.write(f"  FAILED: {error}")
                        stats[out_str]["fail"] += 1
        else:
            with ProcessPoolExecutor(max_workers=pool_size, initializer=_init_worker) as pool:
                futures = {
                    pool.submit(_parse_chunk, zp_str, names, jsonld): out_str
                    for zp_str, out_str, names in chunks
                }
                with tqdm(total=total_files, desc="Converting", unit="file") as pbar:
                    for fut in as_completed(futures):
                        out_str = futures[fut]
                        results = fut.result()
                        gz = gz_handles[out_str]
                        for _name, line, error in results:
                            if line is not None:
                                gz.write(line)
                                gz.write("\n")
                                stats[out_str]["ok"] += 1
                            else:
                                tqdm.write(f"  FAILED: {error}")
                                stats[out_str]["fail"] += 1
                        pbar.update(len(results))
    finally:
        for gz in gz_handles.values():
            gz.close()

    # --- 3. Report ---
    total_ok = sum(s["ok"] for s in stats.values())
    total_fail = sum(s["fail"] for s in stats.values())
    n_zips = len(zip_outputs)

    if n_zips == 1:
        out_path = str(zip_outputs[0][1])
        click.echo(f"\nWrote {out_path}")
        click.echo(f"  {total_ok} converted, {total_fail} failed out of {total_files} files.")
        if total_ok > 0:
            size_mb = Path(out_path).stat().st_size / (1024 * 1024)
            click.echo(f"  File size: {size_mb:.1f} MB")
    else:
        out_dir = zip_outputs[0][1].parent
        click.echo(f"\nWrote {n_zips} .jsonl.gz files to {out_dir}")
        click.echo(f"  {total_ok} records converted, {total_fail} failed across {n_zips} zip files.")


def _batch_single(
    entries: list[tuple[str, str | bytes]], output: str,
    jsonld: bool, workers: int,
):
    """Parse a list of (name, source) entries into a single .jsonl.gz."""
    pool_size = min(workers, len(entries))
    success, failed = 0, 0

    with gzip.open(output, "wt", encoding="utf-8", compresslevel=6) as gz:
        if pool_size <= 1:
            for name, source in tqdm(entries, desc="Converting", unit="file"):
                try:
                    data = parse_rdfxml(source, jsonld=jsonld)
                    data["_source_file"] = name
                    gz.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
                    gz.write("\n")
                    success += 1
                except Exception as e:
                    tqdm.write(f"FAILED: {name} — {e}")
                    failed += 1
        else:
            with ProcessPoolExecutor(max_workers=pool_size, initializer=_init_worker) as pool:
                futures = {
                    pool.submit(_parse_one, name, source, jsonld): name
                    for name, source in entries
                }
                with tqdm(total=len(futures), desc="Converting", unit="file") as pbar:
                    for fut in as_completed(futures):
                        name, line, error = fut.result()
                        if line is not None:
                            gz.write(line)
                            gz.write("\n")
                            success += 1
                        else:
                            tqdm.write(f"FAILED: {error}")
                            failed += 1
                        pbar.update(1)

    click.echo(f"\nWrote {output}")
    click.echo(f"  {success} converted, {failed} failed out of {len(entries)} files.")

    if success > 0:
        size_mb = Path(output).stat().st_size / (1024 * 1024)
        click.echo(f"  File size: {size_mb:.1f} MB")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """Convert RDF/XML to simplified JSON or standards-compliant JSON-LD."""


@cli.command()
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", type=click.Path(), default=None, help="Output path. Default: input with .json/.jsonld extension.")
@click.option("--indent", type=int, default=2, help="JSON indent level.")
@click.option("--jsonld", is_flag=True, help="Output valid JSON-LD instead of simplified JSON.")
def single(input_file: str, output: str | None, indent: int, jsonld: bool):
    """Convert a single RDF/XML file to JSON or JSON-LD."""
    data = parse_rdfxml(input_file, jsonld=jsonld)

    if output is None:
        suffix = ".jsonld" if jsonld else ".json"
        output = str(Path(input_file).with_suffix(suffix))

    Path(output).write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")
    click.echo(f"Converted: {input_file} -> {output}")


@cli.command()
@click.argument("input_path", type=click.Path(exists=True))
@click.option("-o", "--output", type=click.Path(), default=None,
              help="Output file (single source) or directory (folder of zips).")
@click.option("--jsonld", is_flag=True, help="Output valid JSON-LD instead of simplified JSON.")
@click.option("--glob", "pattern", type=str, default="*.xml", help="File glob pattern. [default: *.xml]")
@click.option("-w", "--workers", type=int, default=4, help="Parallel workers. [default: 4]")
def batch(input_path: str, output: str | None, jsonld: bool, pattern: str, workers: int):
    """
    Convert many RDF/XML files to gzipped JSONL.

    INPUT_PATH can be a directory of XML files, a .zip archive, or a
    directory of .zip archives.  In the first two cases every input
    file becomes one JSON line in a single output file.  In the last
    case each zip archive produces its own .jsonl.gz file.

    A "_source_file" field is added to every record for traceability.
    Use --jsonld for standards-compliant JSON-LD output.
    """
    src = Path(input_path)
    workers = max(1, workers)

    # --- Directory input: detect folder-of-zips vs XML files ---
    if src.is_dir():
        zip_files = sorted(src.glob("*.zip"))
        entries = list(iter_xml_from_dir(src, pattern))

        if not entries and zip_files:
            # Multi-zip mode — one .jsonl.gz per zip
            out_dir = Path(output) if output else src
            zip_outputs = [
                (zf, out_dir / zf.with_suffix(".jsonl.gz").name)
                for zf in zip_files
            ]
            _batch_from_zips(zip_outputs, jsonld, pattern, workers)
            return

        if not entries:
            click.echo(
                f"Error: no files matching '{pattern}' and no .zip files in {src}",
                err=True,
            )
            sys.exit(1)

        # Directory of XML files — use _batch_single (paths are cheap to pickle)
        if output is None:
            output = str(src.with_suffix(".jsonl.gz"))
        _batch_single(entries, output, jsonld, workers)
        return

    elif src.is_file() and zipfile.is_zipfile(src):
        # Single zip — route through chunk-based parallel processing
        if output is None:
            output = str(src.with_suffix(".jsonl.gz"))
        _batch_from_zips([(src, Path(output))], jsonld, pattern, workers)
        return

    click.echo(f"Error: {input_path} is not a directory or zip file.", err=True)
    sys.exit(1)


if __name__ == "__main__":
    cli()