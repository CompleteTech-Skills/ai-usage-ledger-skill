"""Inventory previous installations and read usage from selected local mounts."""

from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

import compile_ai_logs
import safety

try:
    from compression import zstd
except ImportError:  # Python 3.12 is the package baseline.
    zstd = None


def _link(path):
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _selected_status(path):
    try:
        for part in reversed((path, *path.parents)):
            if _link(part) and not os.path.ismount(part):
                return "linked_path"
    except PermissionError:
        return "access_denied"
    except OSError:
        return "unreadable"
    return _directory(path, allow_mount=True)


def _directory(path, *, allow_mount=False):
    try:
        if _link(path) and not (allow_mount and os.path.ismount(path)):
            return "linked_path"
        with os.scandir(path):
            return "readable"
    except (FileNotFoundError, NotADirectoryError):
        return "missing_or_unmounted"
    except PermissionError:
        return "access_denied"
    except OSError:
        return "unreadable"


def windows_volumes():
    command = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$volumes = @(Get-CimInstance Win32_Volume -ErrorAction Stop | Where-Object DriveType -eq 3)
ConvertTo-Json -Compress -InputObject @($volumes | Select-Object DriveLetter,DeviceID,FileSystem,Capacity)
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, check=True, timeout=20)
    return json.loads(result.stdout.decode("utf-8-sig"))


def discover_windows(volumes, active_drive):
    """Read installation markers and user-tree metadata, without opening transcripts."""
    found = []
    for volume in volumes:
        drive = volume.get("DriveLetter")
        if not drive:
            found.append({"path": volume.get("DeviceID"), "status": "unverified_unmounted",
                          "filesystem": volume.get("FileSystem"), "bytes": volume.get("Capacity")})
            continue
        root = Path(drive + "\\")
        candidates = [(root, "alternate_volume")] if drive.casefold() != active_drive.casefold() else []
        candidates.extend([(root / "Windows.old", "retained_windows"),
                           (root / "$SysReset" / "OldOS", "retained_windows")])
        if _directory(root) == "readable":
            with os.scandir(root) as entries:
                candidates.extend((Path(entry.path), "retained_windows") for entry in entries
                                  if re.fullmatch(r"Windows\.old\.\d+", entry.name, re.I))
        for path, kind in candidates:
            status = _directory(path.parent) if path.parent != root and path != root else "readable"
            if status == "readable":
                status = _directory(path)
            if status == "missing_or_unmounted":
                continue
            users = _directory(path / "Users") if status == "readable" else status
            if kind == "alternate_volume" and users == "missing_or_unmounted":
                continue
            found.append({"path": str(path), "kind": kind, "status": "discovered" if status == "readable" else status,
                          "profiles": users, "usage_scanned": False})
    return found


def inventory():
    if os.name != "nt":
        return []
    try:
        return discover_windows(windows_volumes(), os.environ.get("SystemDrive", "C:"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return [{"status": "volume_inventory_unavailable", "usage_scanned": False}]


def selections(values):
    sources = {}
    for value in values:
        name, separator, root = value.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}", name):
            raise ValueError("use --prior-install NAME=ABSOLUTE_PATH")
        host = "prior-" + name.lower()
        path = Path(root)
        if not path.is_absolute() or host in sources:
            raise ValueError("each prior installation needs an absolute path and a unique name")
        safety.check_source_path(root)
        sources[host] = path
    return sources


def _profile_roots(root, problems):
    """Inspect the standard Windows and Linux layouts inside an approved root."""
    def readable(path):
        state = _directory(path)
        if state not in ("readable", "missing_or_unmounted"):
            problems.append({"path": str(path), "status": state})
        return state == "readable"

    if root.name.lower() == ".codex" or any(_directory(root / name) == "readable"
                                            for name in ("sessions", "archived_sessions")):
        yield root
        return
    is_profile_container = root.name.casefold() in ("users", "home")
    profiles = [] if is_profile_container else [root, root / "root"]
    containers = (root,) if is_profile_container else (root / "Users", root / "home")
    for container in containers:
        if readable(container):
            try:
                with os.scandir(container) as entries:
                    profiles.extend(Path(entry.path) for entry in entries
                                    if entry.is_dir(follow_symlinks=False) or entry.is_symlink())
            except OSError:
                problems.append({"path": str(container), "status": "unreadable"})
    for profile in profiles:
        if readable(profile) and readable(profile / ".codex"):
            yield profile / ".codex"


def _rollouts(home, problems):
    result = {}
    for folder in (home / "sessions", home / "archived_sessions"):
        state = _directory(folder)
        if state == "missing_or_unmounted":
            continue
        if state != "readable":
            problems.append({"path": str(folder), "status": state})
            continue

        def denied(error):
            problems.append({"path": str(error.filename or folder), "status": "unreadable"})

        files = result.setdefault(str(folder), [])
        for directory, subdirs, filenames in os.walk(folder, onerror=denied):
            for name in subdirs[:]:
                path = Path(directory) / name
                try:
                    if not _link(path):
                        continue
                    subdirs.remove(name)
                    problems.append({"path": str(path), "status": "linked_path"})
                except OSError as error:
                    subdirs.remove(name)
                    denied(error)
            for name in filenames:
                if not name.startswith("rollout-") or not name.endswith((".jsonl", ".jsonl.gz", ".jsonl.zst")):
                    continue
                path = Path(directory) / name
                try:
                    if _link(path):
                        problems.append({"path": str(path), "status": "linked_path"})
                        continue
                    if not path.is_file() or safety.is_credential_file(str(path)):
                        continue
                except OSError as error:
                    denied(error)
                    continue
                if name.endswith(".zst") and zstd is None:
                    problems.append({"path": str(path), "status": "zstandard_unavailable"})
                else:
                    files.append(str(path))
    return result


@contextmanager
def _open_rollout(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as stream:
            yield stream
    elif path.endswith(".zst") and zstd is not None:
        try:
            with zstd.open(path, "rb") as stream:
                yield stream
        except zstd.ZstdError as error:
            raise OSError("cannot read Zstandard usage log") from error
    else:
        with open(path, "rb") as stream:
            yield stream


def usage(sources, coverage, *, end=None, end_inclusive=True):
    """Yield parsed calls. Coverage records distinguish absent, blocked and partial sources."""
    for host, root in sources.items():
        row = {"host": host, "path": str(root), "harness": "codex", "status": _selected_status(root),
               "problems": [], "events": 0, "files": 0, "first": None, "last": None}
        coverage.append(row)
        if row["status"] != "readable":
            continue
        stats = compile_ai_logs.Stats()
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as output:
            for home in _profile_roots(root, row["problems"]):
                compile_ai_logs.scan_codex(str(home), host, output, stats, [], {},
                                           rollout_paths=_rollouts(home, row["problems"]), open_rollout=_open_rollout)
            row.update(files=stats.files, scanned_events=stats.events, parse_errors=stats.errors, events_after_cutoff=0)
            row["status"] = "partial" if row["problems"] or stats.errors else "scanned" if stats.files else "no_supported_history"
            output.seek(0)
            for line in output:
                event = json.loads(line)
                try:
                    value = event.get("ts") or ""
                    instant = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value)
                    if instant.utcoffset() is None:
                        raise ValueError("missing UTC offset")
                except (ValueError, OverflowError):
                    yield event
                    continue
                if end is not None and (instant > end or (instant == end and not end_inclusive)):
                    row["events_after_cutoff"] += 1
                    continue
                stamp = instant.astimezone(timezone.utc).isoformat(timespec="microseconds")
                row["first"] = min(stamp, row["first"] or stamp)
                row["last"] = max(stamp, row["last"] or stamp)
                row["events"] += 1
                yield event
