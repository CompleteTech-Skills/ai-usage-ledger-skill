"""Recognize agent-session evidence in approved filesystem and archive paths."""

from collections import Counter
from contextlib import contextmanager
import gzip
import json
import os
from pathlib import PurePosixPath
import re
import stat
import tarfile
import zipfile

import source_streams


ARCHIVES = (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tar", ".tgz", ".zip", ".7z")
IMAGES = (".avhdx", ".vhdx", ".vhd", ".qcow2", ".vmdk", ".vdi")
LOG = re.compile(r"\.(?:jsonl|ndjson)(?:\.(?:gz|zst|zstd|bak))?$", re.I)
NAMED_JSON = re.compile(r"(?:session|conversation|transcript|trajectory|chat|turn).*\.json$", re.I)
DATABASE = re.compile(r"(?:session|thread|conversation|state|opencode|coven|agent-host).*\."
                      r"(?:sqlite3?|db)$", re.I)
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
PRIVATE = re.compile(r"(?:^|/)(?:auth\.json|\.claude\.json|\.credentials(?:\.json)?|"
                     r"\.env(?:\.[^/]*)?|credentials|secrets\.json)(?:/|$)", re.I)
FIXTURE = re.compile(r"(?:^|/)(?:fixtures?|testdata|tests?|__tests__|examples|synthetic)(?:/|$)", re.I)
OFFLINE = 0x1000 | 0x40000 | 0x400000
PRUNE = {
    ".git", ".venv", "__pycache__", "node_modules", "site-packages", "code cache",
    "gpucache", "winsxs", "system volume information",
}
STORES = (
    ("claude-desktop", ("/local-agent-mode-sessions/", "/claude-code-sessions/")),
    ("claude-code", ("/.claude/", "/.claude-profiles/")),
    ("codex", ("/.codex/", "/.codex-auths/", "/codex-home/", "/codex-data/")),
    ("copilot-cli", ("/.copilot/", "/copilot-cli/")),
    ("vscode-agent-chat", ("/chatsessions/", "/emptywindowchatsessions/", "/agent-host.db")),
    ("openclaw", ("/.openclaw/", "/openclaw/agents/", "/srv/openclaw/")),
    ("opencode", ("/opencode/", "/.opencode/")),
    ("gemini-cli", ("/.gemini/tmp/",)),
    ("antigravity", ("/.gemini/antigravity/", "/antigravity ide/user/")),
    ("grok-build", ("/.grok/",)),
    ("hermes", ("/.hermes/", "/hermes/sessions/")),
    ("letta", ("/.letta/",)),
    ("paseo", ("/.paseo/", "/paseo-session-archive-")),
    ("dsh", ("/.dsh/",)),
    ("coven", ("/.coven/", "/ai.opencoven.cave/")),
    ("aider", ("/.aider.chat.history.md", "/.aider/")),
    ("cline", ("/.cline/", "/saoudrizwan.claude-dev/")),
    ("roo-code", ("/.roo/", "/rooveterinaryinc.roo-cline/")),
    ("kilo-code", ("/.kilocode/", "/kilocode.kilo-code/")),
    ("goose", ("/goose/sessions/", "/block/goose/")),
    ("cursor", ("/.cursor/", "/cursor/user/")),
    ("windsurf", ("/windsurf/user/", "/.codeium/windsurf/")),
    ("kiro", ("/.kiro/", "/kiro-cli/", "/amazon-q/")),
    ("qwen-code", ("/.qwen/",)),
    ("kimi", ("/.kimi/", "/.kimi-code/")),
    ("mistral-vibe", ("/.vibe/logs/",)),
    ("pi", ("/.pi/agent/sessions/",)),
    ("continue", ("/.continue/sessions/", "/.continue/dev_data/")),
    ("openhands", ("/.openhands/", "/.local/share/openhands/")),
    ("mini-swe-agent", ("/mini-swe-agent/",)),
    ("okcode", ("/.okcode/",)),
    ("t3-code", ("/.t3/",)),
    ("modex", ("/.modex/",)),
    ("omniroute", ("/.omniroute/",)),
    ("herdr", ("/.config/herdr/",)),
    ("lm-studio", ("/.lmstudio/", "/lm studio/")),
    ("amp", ("/.amp/", "/.local/share/amp/")),
    ("droid", ("/.factory/sessions/",)),
    ("codebuff", ("/.codebuff/", "/.config/manicode/")),
)
SESSION_TABLES = frozenset({
    "threads", "thread", "sessions", "session", "sessions_v2", "session_model_usage", "messages",
    "conversations", "conversation_summaries", "projection_threads", "assistant_usage_events",
    "session_state", "session_message",
})


def relative_name(path):
    return "/" + str(path).replace("\\", "/").lstrip("/").casefold()


def source_kind(path):
    """Return the filename's likely format and harness; identification is a separate check."""
    name = relative_name(path)
    leaf = name.rsplit("/", 1)[-1]
    if PRIVATE.search(name):
        return None
    family = next((tool for tool, markers in STORES if any(marker in name for marker in markers)), None)
    if leaf.endswith(IMAGES):
        return "disk image", family
    if leaf.endswith(ARCHIVES):
        return "archive", family
    if leaf.startswith("rollout-") and LOG.search(leaf):
        return "transcript", "codex"
    if DATABASE.search(leaf) or (family and leaf.endswith((".sqlite", ".sqlite3", ".db", ".vscdb"))):
        return "database", family
    if LOG.search(leaf) or NAMED_JSON.search(leaf) or leaf.endswith(".chat.history.md"):
        return "transcript candidate", family
    if family and leaf in {"stats-cache.json", "sessions.json", "summary.json", "session-index.json", "ui_messages.json",
                           "api_conversation_history.json"}:
        return "store metadata", family
    return None


def blocked_file(info):
    """Return an inspection limit without opening linked or offline content."""
    bits = getattr(info, "st_file_attributes", 0)
    if bits & OFFLINE:
        return "offline placeholder"
    if stat.S_ISLNK(info.st_mode) or bits & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return "linked path"
    return None


def readable_file(path):
    for part in reversed(path.parents):
        problem = blocked_file(part.lstat())
        if problem and not (problem == "linked path" and os.path.ismount(part)):
            return problem
    info = path.lstat()
    return blocked_file(info) or ("special file" if not stat.S_ISREG(info.st_mode) else None)


def record_identity(row, family):
    if not isinstance(row, dict):
        return None, None
    kind = row.get("type")
    payload = row.get("payload")
    if kind == "session_meta" and isinstance(payload, dict):
        return "codex", payload.get("id")
    if row.get("sessionId") and kind in {"assistant", "user", "queue-operation", "system", "file-history-snapshot"}:
        return ("claude-desktop" if family == "claude-desktop" else "claude-code"), row["sessionId"]
    if row.get("sessionId") and ("projectHash" in row or "messages" in row):
        return "gemini-cli", row["sessionId"]
    if kind == "session.start" and isinstance(row.get("data"), dict):
        return "copilot-cli", row["data"].get("sessionId")
    if row.get("method") == "_x.ai/session/update" and isinstance(row.get("params"), dict):
        return "grok-build", row["params"].get("sessionId")
    if kind == "session" and family in {"openclaw", "pi"}:
        return family, row.get("id")
    if row.get("kind") == 0 and isinstance(row.get("v"), dict) and "requests" in row["v"]:
        return "vscode-agent-chat", row["v"].get("sessionId")
    return None, None


def native_id(value):
    # Index identifiers only. Arbitrary text in a malformed record has no role here.
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        return value
    return None


def identify_prefix(raw, family):
    # Bounded reads cover JSON envelopes and the beginning of line-oriented logs.
    for sample in [raw] + raw.splitlines()[:24]:
        try:
            row = json.loads(sample)
        except (ValueError, UnicodeError):
            continue
        tool, identifier = record_identity(row, family)
        if tool:
            return tool, native_id(identifier)
    return None, None


def transcript_prefix(path):
    # source_streams can use a native zstd subprocess. Its normal context expects
    # EOF, so use the stdlib reader for bounded prefixes when available.
    name = str(path).lower()
    if name.endswith((".zst", ".zstd")):
        if source_streams.zstd is None:
            raise OSError("bounded Zstandard prefix needs Python 3.14+")
        try:
            with source_streams.zstd.open(path, "rb") as stream:
                return stream.read(65536)
        except source_streams.zstd.ZstdError as error:
            raise OSError("invalid Zstandard transcript") from error
    opener = gzip.open if name.endswith(".gz") else open
    with opener(path, "rb") as stream:
        return stream.read(65536)


def archive_member(name):
    posix = str(name).replace("\\", "/")
    components = PurePosixPath(posix)
    if components.is_absolute() or ".." in components.parts or FIXTURE.search(posix) or PRIVATE.search(posix):
        return None
    match = source_kind(components)
    if not match or match[0] in {"archive", "disk image"}:
        return None
    kind, family = match
    if not family and relative_name(components).startswith("/transcripts/") and LOG.search(posix):
        return "paseo", kind
    return (family, kind) if family else None


@contextmanager
def open_archive(path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as opened:
            members = [(entry.filename, entry) for entry in opened.infolist() if not entry.is_dir()]
            yield members, opened.open
    else:
        with tarfile.open(path, "r:*") as opened:
            members = [(entry.name, entry) for entry in opened.getmembers() if entry.isfile()]
            yield members, opened.extractfile


def archive_index(path):
    """List one container without exporting or extracting any member."""
    with open_archive(path) as (members, read):
        matches = Counter()
        for name, _ in members:
            hit = archive_member(name)
            if hit:
                matches[hit[0]] += 1
        return {"members": len(members), "session_candidates_by_harness": dict(matches)}
