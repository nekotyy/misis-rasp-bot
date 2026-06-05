from __future__ import annotations

import argparse
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


SEMVER_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
TRASH_SUBJECTS = {
    "development",
    "pre release",
    "prerelease",
    "release",
    "merge",
    "fix",
    "фикс",
    "v1",
    "в1",
    "test",
    "тест",
}
TRASH_PREFIXES = (
    "merge branch",
    "merge pull request",
    "co-authored-by:",
    "чиню ",
    "исправляю ",
    "внес правки ",
    "test commit",
    "тестовый коммит",
)
SECURITY_PATTERNS = (
    r"\bsecurity\b",
    r"\brate[ -]?limit\b",
    r"\bcsrf\b",
    r"\bбезопас",
    r"\bсесс",
    r"\bлогин",
    r"\bаудит вход",
    r"\bхарднинг",
)
INFRA_PATTERNS = (
    r"\bci\b",
    r"\bcd\b",
    r"\bdocker\b",
    r"\bworkflow\b",
    r"\brelease\b",
    r"\bregistry\b",
    r"\bghcr\b",
    r"\bsmoke\b",
    r"\bcompose\b",
    r"\bдеплой",
    r"\bрелиз",
    r"\bреестр",
)
DOCS_PATTERNS = (
    r"\breadme\b",
    r"\bдокум",
    r"\bреадми",
    r"\broadmap\b",
    r"\bdocstring\b",
    r"\bdocs?:\b",
)
FIX_PATTERNS = (
    r"\bпочинил",
    r"\bисправ",
    r"\bfix\b",
    r"\bbug\b",
    r"\bошиб",
    r"\bretry\b",
    r"\bретра",
    r"\bфиксанул",
    r"\bфикс",
    r"\brevert\b",
)


@dataclass(slots=True)
class CommitEntry:
    subject: str
    category: str


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def normalize_tag(version: str) -> str:
    return version if version.startswith("v") else f"v{version}"


def parse_semver_tag(tag: str) -> tuple[int, int, int] | None:
    match = SEMVER_TAG_RE.fullmatch(tag.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def find_previous_tag(target_version: str) -> str | None:
    target_tuple = parse_semver_tag(target_version)
    if target_tuple is None:
        return None

    tags = git("tag", "--list").splitlines()
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for tag in tags:
        parsed = parse_semver_tag(tag)
        if parsed is None or parsed >= target_tuple:
            continue
        candidates.append((parsed, tag))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def cleanup_subject(subject: str) -> str | None:
    cleaned = " ".join(subject.strip().split()).strip(" .")
    if not cleaned:
        return None

    lowered = cleaned.lower()
    if lowered in TRASH_SUBJECTS:
        return None
    if any(lowered.startswith(prefix) for prefix in TRASH_PREFIXES):
        return None
    return cleaned[0].upper() + cleaned[1:] if cleaned else None


def matches_any(subject: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, subject) for pattern in patterns)


def categorize(subject: str) -> str:
    lowered = subject.lower()
    if matches_any(lowered, SECURITY_PATTERNS):
        return "security"
    if matches_any(lowered, INFRA_PATTERNS):
        return "infra"
    if matches_any(lowered, DOCS_PATTERNS):
        return "docs"
    if matches_any(lowered, FIX_PATTERNS):
        return "fixes"
    return "features"


def collect_entries(target_tag: str) -> tuple[str | None, list[CommitEntry]]:
    previous_tag = find_previous_tag(target_tag)
    revision_range = f"{previous_tag}..HEAD" if previous_tag else "HEAD"
    subjects = git("log", "--no-merges", "--format=%s", revision_range).splitlines()

    entries: list[CommitEntry] = []
    seen: set[str] = set()
    for raw_subject in subjects:
        cleaned = cleanup_subject(raw_subject)
        if cleaned is None or cleaned in seen:
            continue
        seen.add(cleaned)
        entries.append(CommitEntry(subject=cleaned, category=categorize(cleaned)))
    return previous_tag, entries


def render_section(title: str, entries: list[CommitEntry]) -> str:
    if not entries:
        return ""
    lines = [f"## {title}"]
    lines.extend(f"- {entry.subject}" for entry in entries)
    return "\n".join(lines)


def build_release_notes(version: str, repository: str) -> str:
    target_tag = normalize_tag(version)
    previous_tag, entries = collect_entries(target_tag)

    grouped: dict[str, list[CommitEntry]] = {
        "features": [],
        "fixes": [],
        "security": [],
        "infra": [],
        "docs": [],
    }
    for entry in entries:
        grouped[entry.category].append(entry)

    parts = [
        "## Что вошло в релиз",
        "Автоматически собранный релиз по коммитам с момента предыдущего тега.",
    ]

    for title, key in (
        ("Новые возможности", "features"),
        ("Исправления", "fixes"),
        ("Безопасность", "security"),
        ("Инфраструктура", "infra"),
        ("Документация", "docs"),
    ):
        section = render_section(title, grouped[key])
        if section:
            parts.append("")
            parts.append(section)

    parts.extend(
        [
            "",
            "## Как обновиться",
            "```bash",
            "git pull",
            "docker compose up -d --build",
            "```",
        ]
    )

    if previous_tag:
        compare_url = f"https://github.com/{repository}/compare/{previous_tag}...{target_tag}"
        parts.extend(
            [
                "",
                "## Полезные ссылки",
                f"- Сравнение изменений: [{previous_tag}...{target_tag}]({compare_url})",
            ]
        )

    return "\n".join(parts).strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    repository = os.environ.get("GITHUB_REPOSITORY", "nekotyy/misis-rasp-bot")
    body = build_release_notes(args.version, repository)
    Path(args.output).write_text(body, encoding="utf-8")


if __name__ == "__main__":
    main()
