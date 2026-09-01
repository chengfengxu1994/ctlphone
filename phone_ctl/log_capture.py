"""Relevant, bounded and redacted Android application log capture."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .adb import ADBError, Phone

PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*$")
PRIORITIES = {"V", "D", "I", "W", "E", "F", "S"}
THREADTIME_RE = re.compile(
    r"^(?P<date>\d\d-\d\d)\s+(?P<time>\d\d:\d\d:\d\d\.\d+)\s+"
    r"(?P<pid>\d+)\s+(?P<tid>\d+)\s+(?P<priority>[VDIWEFS])\s+"
    r"(?P<tag>[^:]*):\s?(?P<message>.*)$"
)
SECRET_RE = re.compile(
    r"(?i)\b(authorization|token|api[_-]?key|password|passwd|secret|cookie)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*(?:#[^\s]*)?", re.I)

CATEGORY_PATTERNS = (
    ("crash", re.compile(r"FATAL EXCEPTION|AndroidRuntime|Fatal signal|SIGSEGV|signal 11|native crash", re.I)),
    ("anr", re.compile(r"\bANR in\b|Application Not Responding|Input dispatching timed out", re.I)),
    ("script_error", re.compile(r"SCRIPT ERROR|GDScript.*(?:ERROR|Error)|Parse Error|Invalid (?:call|get|set)|stack traceback", re.I)),
    ("resource_error", re.compile(r"Error loading resource|Failed (?:to )?load(?:ing)? resource|Couldn't load|res://\S+.*not found|resource.*missing", re.I)),
)


def _redact(text: str, device_ids: list[str] | None = None) -> str:
    value = BEARER_RE.sub("Bearer [REDACTED]", text)
    value = JWT_RE.sub("[REDACTED_JWT]", value)
    value = SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)
    value = URL_QUERY_RE.sub(r"\1?[REDACTED]", value)
    for device_id in device_ids or []:
        if device_id:
            value = value.replace(device_id, "[DEVICE]")
    return value[:4000]


def _category(priority: str, text: str) -> str | None:
    for name, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return name
    if priority in {"E", "F"}:
        return "error"
    if priority == "W":
        return "warning"
    return None


def _parse_lines(raw: str, device_ids: list[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for source_line in raw.splitlines():
        match = THREADTIME_RE.match(source_line)
        if not match:
            continue
        item: dict[str, Any] = match.groupdict()
        item["pid"] = int(item["pid"])
        item["tid"] = int(item["tid"])
        item["tag"] = item["tag"].strip()
        item["message"] = _redact(item["message"], device_ids)
        item["category"] = _category(item["priority"], f"{item['tag']}: {item['message']}")
        entries.append(item)
    return entries


def _package_crash_blocks(raw: str, package: str, pids: set[int]) -> str:
    """Keep only crash-buffer blocks with explicit package or owned PID evidence."""
    lines = raw.splitlines()
    owned = set(pids)
    for line in lines:
        match = THREADTIME_RE.match(line)
        if match and package in line:
            owned.add(int(match.group("pid")))
    selected: list[str] = []
    keep_continuation = False
    for line in lines:
        match = THREADTIME_RE.match(line)
        if match:
            keep_continuation = int(match.group("pid")) in owned or package in line
        if keep_continuation:
            selected.append(line)
    return "\n".join(selected)


def capture_app_logs(phone: Phone, package: str, *, limit: int = 500,
                     min_priority: str = "V", include_crash: bool = True) -> dict[str, Any]:
    if not PACKAGE_RE.fullmatch(package):
        raise ADBError("package must be a valid Android package identifier")
    count = max(20, min(2000, int(limit)))
    priority = str(min_priority).upper()
    if priority not in PRIORITIES - {"S"}:
        raise ADBError("min_priority must be one of V, D, I, W, E, F")
    devices = phone.devices()
    if len(devices) != 1:
        raise ADBError(f"exactly one authorized device required, found {len(devices)}")
    pid_output = phone.shell(f"pidof {package}").strip()
    pids = {int(value) for value in pid_output.split() if value.isdigit()}
    raw_parts: list[str] = []
    for pid in sorted(pids):
        raw_parts.append(phone.shell(
            f"logcat -d -v threadtime --pid={pid} '*:{priority}' | tail -n {count}", timeout=30
        ))
    if include_crash:
        crash_raw = phone.shell(
            f"logcat -b crash -d -v threadtime '*:{priority}' | tail -n 2000", timeout=30
        )
        raw_parts.append(_package_crash_blocks(crash_raw, package, pids))
    entries = _parse_lines("\n".join(raw_parts), devices)
    # Multiple buffers/PIDs can overlap; keep stable order while deduplicating.
    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in entries:
        key = tuple(item.get(field) for field in ("date", "time", "pid", "tid", "priority", "tag", "message"))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    total_before_limit = len(unique)
    unique = unique[-count:]
    counts: dict[str, int] = {}
    findings = []
    for item in unique:
        category = item.get("category")
        if category:
            counts[category] = counts.get(category, 0) + 1
            findings.append(item)
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "package": package,
        "pids": sorted(pids),
        "process_running": bool(pids),
        "min_priority": priority,
        "requested_limit": count,
        "truncated": total_before_limit > len(unique),
        "line_count": len(unique),
        "finding_count": len(findings),
        "counts": counts,
        "findings": findings,
        "logs": unique,
        "note": ("process is running but has no matching buffered log lines"
                 if pids and not unique else
                 "process is not running and no package-owned crash lines were found"
                 if not pids and not unique else ""),
    }
