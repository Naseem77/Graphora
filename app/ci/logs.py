from __future__ import annotations

import io
import re
import zipfile

import requests

from app.config import Settings, get_settings
from app.github.app import create_installation_token


MAX_LOG_CHARS = 60000
MAX_SUMMARY_LINES = 80

FAILED_TEST_PATTERNS = (
    re.compile(r"FAILED\s+([^\s]+::[^\s]+)"),
    re.compile(r"FAIL:\s+([A-Za-z_][\w.]+)"),
    re.compile(r"--- FAIL:\s+([A-Za-z_][\w./-]+)"),
    re.compile(r"FAIL\s+([^\s]+\.(?:test|spec)\.[jt]sx?)"),
)
PATH_PATTERN = re.compile(r"((?:[\w.-]+/)+[\w.-]+\.(?:py|js|jsx|ts|tsx|go|java|rb|php|rs|cs|kt|swift))")
ERROR_LINE_PATTERN = re.compile(
    r"(FAILED|FAIL|ERROR|Error:|Exception|Traceback|AssertionError|TypeError|ValueError|panic:|npm ERR!|Process completed with exit code)",
    re.IGNORECASE,
)


def fetch_workflow_run_logs(
    installation_id: int,
    logs_url: str,
    settings: Settings | None = None,
) -> str:
    if not logs_url:
        return ""
    settings = settings or get_settings()
    token = create_installation_token(installation_id, settings)
    response = requests.get(
        logs_url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=60,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Could not fetch workflow logs: {response.status_code} {response.text[:500]}")
    return _decode_log_response(response.content, response.headers.get("content-type", ""))


def extract_failed_tests(log_text: str) -> list[str]:
    seen: set[str] = set()
    tests: list[str] = []
    for pattern in FAILED_TEST_PATTERNS:
        for match in pattern.finditer(log_text):
            name = match.group(1).strip()
            if name and name not in seen:
                seen.add(name)
                tests.append(name)
    return tests[:25]


def extract_paths(log_text: str) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for match in PATH_PATTERN.finditer(log_text):
        path = match.group(1)
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths[:50]


def summarize_failure_logs(log_text: str, max_chars: int = MAX_LOG_CHARS) -> str:
    if not log_text:
        return "No workflow logs were available."
    lines = []
    for line in log_text.splitlines():
        stripped = _strip_log_prefix(line).strip()
        if not stripped:
            continue
        if ERROR_LINE_PATTERN.search(stripped) or PATH_PATTERN.search(stripped):
            lines.append(stripped)
        if len(lines) >= MAX_SUMMARY_LINES:
            break
    summary = "\n".join(lines) if lines else "\n".join(log_text.splitlines()[-MAX_SUMMARY_LINES:])
    return summary[:max_chars]


def _decode_log_response(content: bytes, content_type: str) -> str:
    if "zip" not in content_type.lower() and not content.startswith(b"PK"):
        return content.decode("utf-8", errors="replace")

    output: list[str] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in sorted(archive.namelist()):
            if name.endswith("/"):
                continue
            output.append(f"===== {name} =====")
            output.append(archive.read(name).decode("utf-8", errors="replace"))
    return "\n".join(output)


def _strip_log_prefix(line: str) -> str:
    return re.sub(r"^\d{4}-\d{2}-\d{2}T[^\s]+\s+", "", line)
