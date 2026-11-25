"""Build a private inventory of agent session evidence from approved roots."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
import posixpath
import sqlite3
import stat
import subprocess
import sys
import tarfile
import webbrowser
import zipfile

import census_sources as formats
from goose_usage import SourceUnavailable, snapshot
import installation_history
import safety


def options(parser):
    parser.add_argument("--root", action="append", default=[], metavar="PATH", help="approved local filesystem root")
    parser.add_argument("--all-volumes", action="store_true", help="include readable Windows fixed volumes")
    parser.add_argument("--wsl", action="append", default=[], metavar="DISTRO", help="scan the root of a running WSL distro")
    parser.add_argument("--wsl-root", action="append", default=[], metavar="DISTRO=/PATH",
                        help="scan an explicitly approved mount or directory inside a running WSL distro")
    parser.add_argument("--inspect-archives", action="store_true", help="read member names in discovered ZIP archives")
    parser.add_argument("--archive", action="append", default=[], metavar="PATH", help="inspect a named ZIP or TAR archive")
    parser.add_argument("--ledger", metavar="SQLITE", help="include a saved SQLite usage ledger and reconcile named archives")
    parser.add_argument("--output", metavar="DIR", help="new private output directory")
    parser.add_argument("--open", action="store_true", help="open the HTML report in the default browser")


def now():
    return datetime.now(timezone.utc)


def error_status(error):
    if isinstance(error, PermissionError):
        return "access denied"
    if isinstance(error, FileNotFoundError):
        return "missing"
    if isinstance(error, SourceUnavailable):
        return str(error)
    return type(error).__name__


@contextmanager
def read_database(path):
    """Avoid creating SQLite sidecars beside an operator's session database."""
    journals = [Path(str(path) + suffix) for suffix in ("-wal", "-journal")]

    def has_journals():
        present = False
        for journal in journals:
            try:
                info = journal.lstat()
            except FileNotFoundError:
                continue
            if formats.blocked_file(info) or not stat.S_ISREG(info.st_mode):
                raise SourceUnavailable("unreadable database sidecar")
            present = present or bool(info.st_size)
        return present

    if has_journals():
        with snapshot(path) as connection:
            yield connection
        return
    before = path.stat()
    uri = path.absolute().as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        yield connection
    after = path.stat()
    if has_journals() or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SourceUnavailable("database changed during census")


def database_counts(path):
    with read_database(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        counts = {}
        for name in sorted(tables & formats.SESSION_TABLES):
            counts[name] = connection.execute('SELECT count(*) FROM "%s"' % name).fetchone()[0]
    return counts


def inspect(path, kind, family, info, inspect_archives):
    row = {"path": str(path), "kind": kind, "harness": family, "bytes": info.st_size}
    if (status := formats.blocked_file(info)):
        return dict(row, status=status)
    if not stat.S_ISREG(info.st_mode):
        return dict(row, status="special file")
    if formats.FIXTURE.search(formats.relative_name(path)):
        return dict(row, status="test or example path")
    try:
        if kind == "archive" and inspect_archives and path.suffix.lower() == ".zip":
            return dict(row, status="archive member names read", archive=formats.archive_index(path))
        if kind == "database":
            counts = database_counts(path)
            return dict(row, status="session tables read" if counts else "schema has no recognized session table",
                        tables=counts)
        if kind in {"transcript", "transcript candidate"} and not path.name.endswith(".md"):
            tool, identifier = formats.identify_prefix(formats.transcript_prefix(path), family)
            if tool:
                return dict(row, status="transcript header read", harness=tool, session_id=identifier)
    except (OSError, EOFError, ValueError, SourceUnavailable, sqlite3.Error, tarfile.TarError, zipfile.BadZipFile) as error:
        return dict(row, status=error_status(error))
    return dict(row, status="pathname evidence")


def tree(root, output, inspect_archives):
    """Walk each filesystem independently and report access, link and mount limits."""
    try:
        for part in reversed((root, *root.parents)):
            problem = formats.blocked_file(part.lstat())
            if problem and not (problem == "linked path" and os.path.ismount(part)):
                yield {"path": str(part), "kind": "traversal gap", "status": problem}
                return
        device = root.lstat().st_dev
    except OSError as error:
        yield {"path": str(root), "kind": "traversal gap", "status": error_status(error)}
        return

    pending = [root]
    while pending:
        directory = pending.pop()
        if directory == output:
            continue
        try:
            with os.scandir(directory) as found:
                for entry in found:
                    path = Path(entry.path)
                    try:
                        if formats.PRIVATE.search(formats.relative_name(path)):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.casefold() in formats.PRUNE:
                                continue
                            if entry.name == "census" and directory.name == ".ai-usage-ledger":
                                continue
                            # DirEntry.stat can return zero for st_dev on Windows.
                            meta = path.lstat()
                            match = formats.blocked_file(meta)
                            if match or meta.st_dev != device or os.path.ismount(path):
                                yield {"path": str(path), "kind": "traversal gap", "status": match or "separate filesystem"}
                            else:
                                pending.append(path)
                            continue
                        if entry.is_symlink() and not formats.source_kind(path):
                            yield {"path": str(path), "kind": "traversal gap", "status": "linked path"}
                            continue
                        match = formats.source_kind(path)
                        if match:
                            yield inspect(path, *match, entry.stat(follow_symlinks=False), inspect_archives)
                    except OSError as error:
                        yield {"path": str(path), "kind": "traversal gap", "status": error_status(error)}
        except OSError as error:
            yield {"path": str(directory), "kind": "traversal gap", "status": error_status(error)}


def running_distros():
    result = subprocess.run(["wsl.exe", "--list", "--running", "--quiet"], capture_output=True, check=True, timeout=20)
    raw = result.stdout
    encoding = "utf-16-le" if b"\0" in raw[:256] or raw.startswith(b"\xff\xfe") else "utf-8"
    return {line.strip() for line in raw.decode(encoding, errors="replace").lstrip("\ufeff").splitlines() if line.strip()}


def wsl_sources(args):
    selected = [(name, "/") for name in args.wsl]
    for item in args.wsl_root:
        distro, sep, root = item.partition("=")
        if not sep or not distro or not posixpath.isabs(root):
            raise ValueError("--wsl-root requires DISTRO=/PATH")
        selected.append((distro, safety.check_path(root)))
    if any(not distro for distro, _ in selected):
        raise ValueError("--wsl requires a distro name")
    return [(safety.validate_wsl_host({"distro": distro})[0], root) for distro, root in selected]


def wsl_worker(distro, path, inspect_archives, output):
    prefix = ["wsl.exe", "-d", distro, "--exec"]
    result = subprocess.run(prefix + ["wslpath", "-a", "-u", str(Path(__file__).resolve())],
                            capture_output=True, check=True, timeout=20)
    script = result.stdout.decode("utf-8").strip()
    result = subprocess.run(prefix + ["wslpath", "-a", "-u", str(output)],
                            capture_output=True, timeout=20)
    target = result.stdout.decode("utf-8").strip() if result.returncode == 0 else ""
    command = prefix + ["python3", "-B", script, "--scan-root", path, target]
    if inspect_archives:
        command.append("--inspect-archives")
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          encoding="utf-8", errors="replace") as process:
        for line in process.stdout:
            try:
                row = json.loads(line)
                if isinstance(row, dict) and all(key in row for key in ("path", "kind", "status")):
                    yield row
                    continue
            except ValueError:
                pass
            yield {"path": path, "kind": "traversal gap", "status": "unrecognized WSL scan record"}
        if process.wait() != 0:
            yield {"path": path, "kind": "traversal gap", "status": "WSL scan incomplete"}


def mounted_roots():
    volumes = installation_history.windows_volumes()
    roots = []
    for volume in volumes:
        if drive := volume.get("DriveLetter"):
            roots.append(Path(drive + "\\"))
        elif device := volume.get("DeviceID"):
            roots.append(Path(device))
    return roots


def ledger_summary(connection):
    column = "CAST(total AS INTEGER)"
    rows = connection.execute(
        "SELECT tool, count(*), count(DISTINCT session), sum(%s), sum(CAST(output AS INTEGER)) "
        "FROM events WHERE %s > 0 GROUP BY tool ORDER BY tool" % (column, column)
    )
    return [dict(harness=tool, calls=calls, sessions=sessions, tokens=tokens, output=output or 0)
            for tool, calls, sessions, tokens, output in rows]


def render(summary):
    page = ["<!doctype html><html lang='en'><meta charset='utf-8'>", safety.CSP_META,
            "<meta name='viewport' content='width=device-width,initial-scale=1'>",
            "<title>Agent session census</title><style>body{max-width:75rem;margin:2rem auto;padding:0 1rem;"
            "font:16px/1.5 system-ui;color-scheme:light dark}td,th{padding:.35rem .8rem;text-align:left;"
            "border-bottom:1px solid GrayText}table{border-collapse:collapse}code{overflow-wrap:anywhere}</style>",
            "<h1>Agent session census</h1>",
            "<p>Observed at %s.</p>" % escape(summary["observed_at"]),
            "<h2>Filesystem and archive coverage</h2><table><tr><th>Source</th><th>Candidate paths</th>"
            "<th>Traversal gaps</th></tr>"]
    for row in summary["sources"]:
        page.append("<tr><td><code>%s</code></td><td>%s</td><td>%s</td></tr>" % (
            escape(row["label"]), row["candidate_paths"], row["traversal_gaps"]))
    page.append("</table><h2>Harness evidence</h2><table><tr><th>Harness</th><th>Candidate paths</th>"
                "<th>Distinct transcript header IDs</th></tr>")
    for harness, row in sorted(summary["harnesses"].items()):
        page.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            escape(harness), row["candidate_paths"], row["distinct_transcript_ids"]))
    page.append("</table>")
    if isinstance(summary.get("ledger"), list):
        page.append("<h2>Saved ledger usage</h2><table><tr><th>Harness</th><th>Calls</th><th>Sessions</th>"
                    "<th>Reported tokens</th><th>Output tokens</th></tr>")
        for row in summary["ledger"]:
            page.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                escape(row["harness"]), row["calls"], row["sessions"], row["tokens"], row["output"]))
        page.append("</table>")
    if summary["archives"]:
        page.append("<h2>Selected archive evidence</h2><ul>")
        for item in summary["archives"]:
            details = item.get("codex", {})
            coverage = ""
            if "parsed_calls" in details:
                coverage = " · %d parsed Codex calls" % details["parsed_calls"]
            if "unmatched_calls" in details:
                coverage += " · %d calls without a ledger match" % details["unmatched_calls"]
            page.append("<li>%s · %s%s</li>" % (escape(item["path"]), escape(item["status"]), coverage))
        page.append("</ul>")
    page.append("<p>The census JSON includes source status, archive coverage and the archived-call comparison. "
                "The JSONL file records the evidence for each path.</p></html>")
    return "\n".join(page)


def run(args, ledger_home):
    from census_archive_usage import compare_archive

    default = Path(ledger_home) / "census" / now().strftime("%Y%m%dT%H%M%S%fZ")
    output = Path(safety.check_path(args.output) if args.output else default).expanduser().absolute()
    local = [Path(safety.check_path(value)).expanduser().absolute() for value in args.root]
    warnings = []
    selected_wsl = list(dict.fromkeys(wsl_sources(args)))
    if (selected_wsl or args.all_volumes) and os.name != "nt":
        raise ValueError("Windows volume and WSL options require Windows")
    if args.all_volumes:
        try:
            local.extend(mounted_roots())
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            warnings.append({"source": "Windows volumes", "status": error_status(error)})
    if not (args.root or args.all_volumes or selected_wsl):
        local.append(Path.home())
    available = set()
    if selected_wsl:
        try:
            available = running_distros()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            warnings.append({"source": "WSL distro listing", "status": error_status(error)})
    output.mkdir(mode=0o700, parents=True)
    safety.private_dir(str(output))
    identities = defaultdict(set)
    kinds = defaultdict(Counter)
    sources = []
    source_specs = [(str(p), None, p) for p in dict.fromkeys(local)]
    source_specs.extend(("WSL %s %s" % (d, p), d, p) for d, p in selected_wsl)
    with safety.private_open(str(output / "paths.jsonl"), encoding="utf-8", newline="\n") as paths:
        for label, distro, path in source_specs:
            counts = Counter()
            states = Counter()
            try:
                if distro and distro not in available:
                    rows = iter([{"path": path, "kind": "traversal gap", "status": "not running or unavailable"}])
                elif distro:
                    rows = wsl_worker(distro, path, args.inspect_archives, output)
                else:
                    rows = tree(path, output, args.inspect_archives)
                for item in rows:
                    paths.write(json.dumps(dict(item, source=label), ensure_ascii=True) + "\n")
                    counts[item["kind"]] += 1
                    states[item["status"]] += 1
                    if tool := item.get("harness"):
                        kinds[tool][item["kind"]] += 1
                        if identifier := item.get("session_id"):
                            identities[tool].add(identifier)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                states[error_status(error)] += 1
                counts["traversal gap"] += 1
            gaps = counts.pop("traversal gap", 0)
            sources.append({"label": label, "candidate_paths": sum(counts.values()), "kinds": dict(counts),
                            "traversal_gaps": gaps, "statuses": dict(states)})
    archive_paths = [Path(safety.check_path(p)).expanduser().absolute() for p in args.archive]
    selected_archives = list(dict.fromkeys(archive_paths))
    archives, ledger = [], None
    if args.ledger:
        file = Path(safety.check_path(args.ledger)).expanduser().absolute()
        try:
            if status := formats.readable_file(file):
                raise SourceUnavailable(status)
            with read_database(file) as connection:
                ledger = ledger_summary(connection)
                archives = [compare_archive(p, connection) for p in selected_archives]
        except (OSError, ValueError, sqlite3.Error, SourceUnavailable) as error:
            warnings.append({"source": "saved ledger", "status": error_status(error)})
            archives = []
            ledger = None
    if ledger is None:
        archives = [compare_archive(p, None) for p in selected_archives]
    summary = {
        "schema": "agent-session-census-v1", "observed_at": now().isoformat(),
        "sources": sources, "warnings": warnings, "archives": archives, "ledger": ledger,
        "harnesses": {name: dict(candidate_paths=sum(counts.values()), by_kind=dict(counts),
                                 distinct_transcript_ids=len(identities[name]))
                      for name, counts in kinds.items()},
    }
    safety.write_private(str(output / "census.json"), json.dumps(summary, indent=2, ensure_ascii=True) + "\n")
    html = output / "census.html"
    safety.write_private(str(html), render(summary))
    print(str(output))
    if args.open:
        webbrowser.open(html.as_uri())
    return 0


def worker(argv):
    """Run the read-only filesystem pass natively inside an explicitly named distro."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-root", nargs=2, required=True)
    parser.add_argument("--inspect-archives", action="store_true")
    args = parser.parse_args(argv)
    root, output = args.scan_root
    for item in tree(Path(root), Path(output) if output else None, args.inspect_archives):
        print(json.dumps(item, ensure_ascii=True))


if __name__ == "__main__":
    worker(sys.argv[1:])
