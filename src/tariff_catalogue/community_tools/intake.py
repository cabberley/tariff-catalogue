"""Validate community plan submissions and convert issues into review PRs."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import yaml
from tariff_core import PlanVersion, parse_plan, to_dict, validate_plan, version_id

from tariff_catalogue.checks.synthetic import CheckFinding, check_version
from tariff_catalogue.harvest.au_cdr.run import _pricing_hash

_SECTIONS = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_SLUG = re.compile(r"[^a-z0-9]+")
_PERSONAL_DATA_PATTERNS = {
    "Australian NMI": re.compile(
        r"\b(?:NMI\s*[:#]?\s*[A-Z0-9]{10,11}|(?:\d{10,11}|[A-HJ-NP-Z]{2}\d{8,9}))\b",
        re.IGNORECASE,
    ),
    "UK MPAN": re.compile(
        r"\b(?:MPAN\s*[:#]?\s*)?(?:\d{13}|\d{2}\s+\d{4}\s+\d{4}\s+\d{3})\b",
        re.IGNORECASE,
    ),
    "UK MPRN": re.compile(r"\b(?:MPRN\s*[:#]?\s*)?\d{6,10}\b", re.IGNORECASE),
    "email address": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    "phone number": re.compile(
        r"(?<!\w)(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)\d{3,4}[\s.-]?\d{3,4}(?!\w)"
    ),
    "street address": re.compile(
        r"\b\d{1,6}\s+(?:[A-Z0-9.'-]+\s+){0,4}"
        r"(?:street|st|road|rd|avenue|ave|drive|dr|lane|ln|"
        r"crescent|cres|close|court|ct|terrace|parade|highway|hwy)\b",
        re.IGNORECASE,
    ),
}


@dataclass(frozen=True, slots=True)
class Submission:
    country: str
    supplier: str
    plan_name: str
    commodity: str
    source_link: str
    from_my_bill: bool
    plan_yaml: str


@dataclass(slots=True)
class SubmissionResult:
    errors: list[str] = field(default_factory=list)
    plan: PlanVersion | None = None
    file_path: Path | None = None
    duplicate_path: Path | None = None
    id_conflict_path: Path | None = None
    duplicate_url: str | None = None
    findings: list[CheckFinding] = field(default_factory=list)


def _slug(value: str) -> str:
    return _SLUG.sub("-", value.casefold()).strip("-")[:80].strip("-")


def _section_values(body: str) -> dict[str, str]:
    matches = list(_SECTIONS.finditer(body))
    values: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        values[match.group(1).strip().casefold()] = body[match.end() : end].strip()
    return values


def _unwrap_yaml(value: str) -> str:
    match = re.fullmatch(r"\s*```(?:yaml)?\s*\n(.*?)\n```\s*", value, re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else value


def parse_issue_body(body: str) -> Submission:
    """Parse the stable field headings emitted by the GitHub issue form."""
    sections = _section_values(body)
    bill_section = sections.get("source", "")
    return Submission(
        country=sections.get("country", "").strip(),
        supplier=sections.get("supplier", "").strip(),
        plan_name=sections.get("plan name", "").strip(),
        commodity=sections.get("commodity", "").strip().casefold(),
        source_link=sections.get("source link", "").strip(),
        from_my_bill=bool(re.search(r"\[[xX]\].*from my bill", bill_section, re.IGNORECASE)),
        plan_yaml=_unwrap_yaml(sections.get("plan yaml", "")),
    )


def scan_personal_data(value: str) -> list[str]:
    """Return generic personal-data categories without returning matched content."""
    return [label for label, pattern in _PERSONAL_DATA_PATTERNS.items() if pattern.search(value)]


def _plan_path(country: str, supplier: str, plan_name: str) -> Path:
    return Path("community") / country / _slug(supplier) / f"{_slug(plan_name)}.yaml"


def _source_url_is_valid(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def _existing_plan_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for directory in ("community", "network", "formula"):
        paths.extend(
            path
            for suffix in ("*.yaml", "*.yml")
            for path in (root / directory).rglob(suffix)
            if path.is_file()
        )
    published = root / "dist" / "v1" / "plans"
    if published.is_dir():
        paths.extend(path for path in published.rglob("*.json") if path.is_file())
    return sorted(set(paths))


def _country(plan: PlanVersion) -> str | None:
    return plan.region.country.casefold() if plan.region and plan.region.country else None


def _same_pricing_market(left: PlanVersion, right: PlanVersion) -> bool:
    return (
        left.commodity == right.commodity
        and left.currency == right.currency
        and _country(left) == _country(right)
    )


def find_duplicate(plan: PlanVersion, root: Path, exclude: Path | None = None) -> Path | None:
    """Find a plan with identical pricing in checked-in or built catalogue data."""
    target_hash = _pricing_hash(plan)
    for path in _existing_plan_files(root):
        if exclude is not None and path.resolve() == exclude.resolve():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            value = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
            if not isinstance(value, dict):
                continue
            existing = parse_plan(value)
            if _pricing_hash(existing) == target_hash and _same_pricing_market(plan, existing):
                return path.relative_to(root)
        except Exception:
            continue
    return None


def find_plan_id_conflict(plan_id: str, root: Path, exclude: Path | None = None) -> Path | None:
    """Find an existing plan with this ID even when its pricing differs."""
    for path in _existing_plan_files(root):
        if exclude is not None and path.resolve() == exclude.resolve():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            value = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
            if not isinstance(value, dict):
                continue
            if parse_plan(value).plan_id == plan_id:
                return path.relative_to(root)
        except Exception:
            continue
    return None


def _catalogue_base_url() -> str:
    base_url = os.environ.get("CATALOGUE_BASE_URL", "").rstrip("/")
    bucket = os.environ.get("R2_BUCKET", "")
    return base_url or (f"https://{bucket}.r2.dev" if bucket else "")


def find_published_duplicate(plan: PlanVersion) -> str | None:
    """Find matching pricing in the published official catalogue, if configured."""
    base_url = _catalogue_base_url()
    if not base_url:
        return None
    country = plan.region.country.casefold() if plan.region and plan.region.country else ""
    if not country:
        return None

    target_hash = _pricing_hash(plan)
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        response = client.get(f"{base_url}/v1/{quote(country, safe='')}/index.json")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        country_index = response.json()
        if not isinstance(country_index, dict) or not isinstance(
            country_index.get("regions"), list
        ):
            raise ValueError("Invalid published country index")
        regions = country_index["regions"]
        for region in regions:
            if not isinstance(region, dict) or not isinstance(region.get("region"), str):
                continue
            region_name = quote(region["region"], safe="")
            response = client.get(
                f"{base_url}/v1/{quote(country, safe='')}/{region_name}/index.json"
            )
            response.raise_for_status()
            region_index = response.json()
            if not isinstance(region_index, dict) or not isinstance(
                region_index.get("plans"), list
            ):
                raise ValueError("Invalid published region index")
            plans = region_index["plans"]
            for summary in plans:
                if (
                    isinstance(summary, dict)
                    and summary.get("equivalence_group") == target_hash
                    and isinstance(summary.get("plan_id"), str)
                    and summary.get("commodity") == plan.commodity.value
                ):
                    currency = summary.get("currency")
                    if currency is None:
                        version_hash = summary.get("latest_version") or summary.get("version_hash")
                        if not isinstance(version_hash, str):
                            continue
                        version_url = (
                            f"{base_url}/v1/plans/{quote(summary['plan_id'], safe='')}/"
                            f"{quote(version_hash, safe='')}.json"
                        )
                        version_response = client.get(version_url)
                        version_response.raise_for_status()
                        version_data = version_response.json()
                        if not isinstance(version_data, dict):
                            raise ValueError("Invalid published plan version")
                        currency = version_data.get("currency")
                    if currency != plan.currency:
                        continue
                    encoded_plan_id = quote(summary["plan_id"], safe="")
                    return f"{base_url}/v1/plans/{encoded_plan_id}/index.json"
    return None


def _synthetic_findings(plan: PlanVersion) -> list[CheckFinding]:
    try:
        return check_version(plan)
    except Exception:
        return [
            CheckFinding(
                "synthetic_check_error",
                "review",
                "Synthetic bill checks could not be completed.",
            )
        ]


def validate_submission(submission: Submission, root: Path) -> SubmissionResult:
    """Validate form metadata and plan YAML without exposing submitted text."""
    result = SubmissionResult()
    country = submission.country.casefold()
    supplier_slug = _slug(submission.supplier)
    plan_slug = _slug(submission.plan_name)
    if not re.fullmatch(r"[a-z]{2}", country):
        result.errors.append("Country must be a two-letter ISO country code.")
    if not submission.supplier or not supplier_slug:
        result.errors.append("Supplier is required.")
    if not submission.plan_name or not plan_slug:
        result.errors.append("Plan name must contain letters or numbers.")
    if submission.commodity not in {"electricity", "gas", "water"}:
        result.errors.append("Commodity must be electricity, gas, or water.")
    if not submission.source_link and not submission.from_my_bill:
        result.errors.append("Provide a public source link or select the bill source option.")
    if submission.source_link and not _source_url_is_valid(submission.source_link):
        result.errors.append("Source link must be an absolute HTTP(S) URL.")
    if not submission.plan_yaml:
        result.errors.append("Plan YAML is required.")

    try:
        submitted_data = yaml.safe_load(submission.plan_yaml)
    except yaml.YAMLError:
        result.errors.append("Plan YAML could not be parsed.")
        return result
    if not isinstance(submitted_data, dict):
        result.errors.append("Plan YAML must contain a mapping.")
        return result

    data = dict(submitted_data)
    data["display_name"] = submission.plan_name
    data["supplier"] = {"id": supplier_slug, "name": submission.supplier}
    if re.fullmatch(r"[a-z]{2}", country):
        expected_plan_id = f"{country}:community:{plan_slug}"
        if data.get("plan_id") not in (None, expected_plan_id):
            result.errors.append("Plan ID must match its country, community source, and plan slug.")
        data["plan_id"] = expected_plan_id
        region = data.get("region")
        region = dict(region) if isinstance(region, dict) else {}
        region["country"] = country.upper()
        data["region"] = region
    if data.get("commodity") not in (None, submission.commodity):
        result.errors.append("Plan commodity must match the selected commodity.")
    data["commodity"] = submission.commodity

    source = data.get("source")
    source = dict(source) if isinstance(source, dict) else {}
    if source.get("type") not in (None, "community"):
        result.errors.append("Plan source.type must be community.")
    source["type"] = "community"
    if submission.source_link:
        source["url"] = submission.source_link
    else:
        source.pop("url", None)
    data["source"] = source
    if data.get("confidence") not in (None, "unverified"):
        result.errors.append("Plan confidence must be unverified.")
    data["confidence"] = "unverified"

    try:
        parsed = parse_plan(data)
        data = to_dict(parsed)
        data["id"] = version_id(parsed)
        plan = parse_plan(data)
    except (TypeError, ValueError, KeyError):
        result.errors.append("Plan YAML does not match the tariff plan schema.")
        return result

    serialized = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    personal_data = scan_personal_data(serialized)
    if personal_data:
        result.errors.append(
            "Plan contains personal information; remove meter IDs, contact details, and addresses."
        )
    validation_issues = validate_plan(plan)
    if validation_issues:
        result.errors.extend(
            f"Tariff validation failed at {issue.path}." for issue in validation_issues
        )
    if result.errors:
        return result

    result.plan = plan
    result.file_path = _plan_path(country, supplier_slug, submission.plan_name)
    result.findings = _synthetic_findings(plan)
    result.duplicate_path = find_duplicate(plan, root, result.file_path)
    if result.duplicate_path is None and plan.plan_id is not None:
        result.id_conflict_path = find_plan_id_conflict(plan.plan_id, root, result.file_path)
    if result.duplicate_path is None and result.id_conflict_path is None:
        try:
            result.duplicate_url = find_published_duplicate(plan)
        except (httpx.HTTPError, ValueError):
            result.errors.append("Could not check the published catalogue for duplicate plans.")
    return result


def build_pr_body(issue_url: str, findings: list[CheckFinding]) -> str:
    """Build a review-required PR body from the issue and synthetic-check findings."""
    issue_number = issue_url.rstrip("/").rsplit("/", 1)[-1]
    lines = [
        f"Closes #{issue_number}",
        "",
        f"Source issue: {issue_url}",
        "",
        (
            "Automated plan validation passed. This submission requires human review and will not "
            "be merged automatically."
        ),
        "",
        "### Synthetic bill checks",
    ]
    if findings:
        lines.extend(f"- **{finding.code}**: {finding.message}" for finding in findings)
    else:
        lines.append("- No findings.")
    return "\n".join(lines) + "\n"


def _issue_comment(
    errors: list[str],
    *,
    duplicate_url: str | None = None,
    conflict_url: str | None = None,
) -> str:
    if duplicate_url is not None:
        return (
            "This plan has the same pricing as an existing catalogue plan. "
            f"[View the matching plan]({duplicate_url})."
        )
    if conflict_url is not None:
        return (
            "The generated plan ID is already used by a different catalogue plan. "
            f"[View the existing plan]({conflict_url}) and submit a plan with a distinct name."
        )
    if errors:
        return "The submission was not converted into a PR:\n\n" + "\n".join(
            f"- {error}" for error in errors
        )
    return "The submission passed automated checks and a review PR has been opened."


def _pr_comment(pr_url: str, findings: list[CheckFinding]) -> str:
    lines = [
        f"The submission passed automated checks and a review PR has been opened: {pr_url}",
        "",
        "Synthetic bill checks:",
    ]
    if findings:
        lines.extend(f"- **{finding.code}**: {finding.message}" for finding in findings)
    else:
        lines.append("- No findings.")
    return "\n".join(lines)


def _plan_link(repository: str, branch: str, path: Path, root: Path) -> str:
    if path.parts[:2] == ("dist", "v1"):
        try:
            value = json.loads((root / path).read_text(encoding="utf-8"))
            plan_id = value.get("plan_id") if isinstance(value, dict) else None
            base_url = _catalogue_base_url()
            if isinstance(plan_id, str) and base_url:
                return f"{base_url}/v1/plans/{quote(plan_id, safe='')}/index.json"
        except (OSError, UnicodeError, ValueError):
            pass
    return f"https://github.com/{repository}/blob/{branch}/{path.as_posix()}"


def _run(arguments: list[str], root: Path) -> str:
    completed = subprocess.run(
        arguments,
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _process_event(root: Path, event_path: Path, base_branch: str) -> int:
    event = json.loads(event_path.read_text(encoding="utf-8"))
    issue = event.get("issue", {})
    labels = {label.get("name") for label in issue.get("labels", []) if isinstance(label, dict)}
    is_submission = "plan-submission" in labels or (
        event.get("action") == "opened" and issue.get("title", "").startswith("[Plan submission]")
    )
    if not is_submission:
        return 0

    repository = event["repository"]["full_name"]
    issue_number = int(issue["number"])
    issue_url = issue["html_url"]
    result = validate_submission(parse_issue_body(issue.get("body", "")), root)
    gh = ["gh", "issue", "comment", str(issue_number), "--body"]
    if result.errors:
        _run([*gh, _issue_comment(result.errors)], root)
        return 0
    if result.duplicate_path is not None:
        duplicate_url = _plan_link(repository, base_branch, result.duplicate_path, root)
        _run([*gh, _issue_comment([], duplicate_url=duplicate_url)], root)
        return 0
    if result.id_conflict_path is not None:
        conflict_url = _plan_link(repository, base_branch, result.id_conflict_path, root)
        _run([*gh, _issue_comment([], conflict_url=conflict_url)], root)
        return 0
    if result.duplicate_url is not None:
        _run([*gh, _issue_comment([], duplicate_url=result.duplicate_url)], root)
        return 0
    if result.plan is None or result.file_path is None:
        return 1

    branch = f"community/issue-{issue_number}"
    existing_pr = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url",
            "--jq",
            ".[0].url // empty",
        ],
        root,
    )
    if existing_pr:
        _run(
            [
                *gh,
                _pr_comment(existing_pr, result.findings),
            ],
            root,
        )
        return 0

    destination = root / result.file_path
    plan_content = yaml.safe_dump(to_dict(result.plan), sort_keys=False, allow_unicode=True)
    remote_branch = _run(["git", "ls-remote", "--heads", "origin", branch], root)
    if remote_branch:
        _run(["git", "fetch", "origin", branch], root)
        _run(["git", "checkout", "-b", branch, "FETCH_HEAD"], root)
        changed_files = _run(
            ["git", "diff", "--name-only", f"origin/{base_branch}...HEAD"], root
        ).splitlines()
        if changed_files != [result.file_path.as_posix()]:
            _run(
                [
                    *gh,
                    "A previous submission branch contains unexpected files; a maintainer must "
                    "reconcile it before another PR can be opened.",
                ],
                root,
            )
            return 0
        if not destination.is_file() or destination.read_text(encoding="utf-8") != plan_content:
            _run(
                [
                    *gh,
                    "A previous submission branch has different content; a maintainer must "
                    "reconcile it before another PR can be opened.",
                ],
                root,
            )
            return 0
    else:
        if destination.exists():
            _run([*gh, _issue_comment(["A plan already exists at the generated path."])], root)
            return 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(plan_content, encoding="utf-8")
        if _validate_community(root, [destination], check_published=False):
            destination.unlink()
            _run(
                [
                    *gh,
                    "The generated plan failed the final community checks; no PR was opened.",
                ],
                root,
            )
            return 0
        _run(["git", "checkout", "-b", branch], root)
        _run(["git", "add", "--", result.file_path.as_posix()], root)
        _run(["git", "config", "user.name", "github-actions[bot]"], root)
        _run(
            [
                "git",
                "config",
                "user.email",
                "41898282+github-actions[bot]@users.noreply.github.com",
            ],
            root,
        )
        _run(["git", "commit", "-m", "Add community plan submission"], root)
        _run(["git", "push", "--set-upstream", "origin", branch], root)
    pr_url = _run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base_branch,
            "--title",
            "Community plan submission",
            "--body",
            build_pr_body(issue_url, result.findings),
        ],
        root,
    )
    _run(
        [
            "gh",
            "issue",
            "comment",
            str(issue_number),
            "--body",
            _pr_comment(pr_url, result.findings),
        ],
        root,
    )
    return 0


def _validate_community(
    root: Path,
    paths: list[Path] | None = None,
    *,
    check_published: bool = True,
) -> int:
    failed = False
    community_files = paths or [
        *root.joinpath("community").rglob("*.yaml"),
        *root.joinpath("community").rglob("*.yml"),
    ]
    for path in sorted(community_files):
        raw = path.read_text(encoding="utf-8")
        if scan_personal_data(raw):
            print(f"{path.relative_to(root)}: personal information detected.")
            failed = True
            continue
        try:
            data = yaml.safe_load(raw)
            if not isinstance(data, dict):
                raise ValueError
            plan = parse_plan(data)
        except (yaml.YAMLError, ValueError, TypeError, KeyError):
            print(f"{path.relative_to(root)}: plan could not be parsed.")
            failed = True
            continue
        issues = validate_plan(plan)
        expected_path = _plan_path(
            (plan.region.country or "").casefold() if plan.region else "",
            plan.supplier.id if plan.supplier else "",
            plan.display_name or "",
        )
        expected_plan_id = (
            f"{plan.region.country.casefold()}:community:{_slug(plan.display_name or '')}"
            if plan.region and plan.region.country
            else ""
        )
        try:
            valid_version_id = plan.id == version_id(plan)
        except ValueError:
            valid_version_id = False
        if (
            issues
            or plan.source is None
            or plan.source.type is None
            or plan.source.type.value != "community"
            or plan.confidence is None
            or plan.confidence.value != "unverified"
            or plan.plan_id != expected_plan_id
            or not valid_version_id
            or path.relative_to(root) != expected_path
        ):
            print(f"{path.relative_to(root)}: metadata or tariff validation failed.")
            failed = True
            continue
        findings = _synthetic_findings(plan)
        for finding in findings:
            print(f"{path.relative_to(root)}: {finding.code}: {finding.message}")
        duplicate = find_duplicate(plan, root, path)
        if duplicate is not None:
            print(f"{path.relative_to(root)}: duplicate pricing found in {duplicate}.")
            failed = True
            continue
        if plan.plan_id is not None and find_plan_id_conflict(plan.plan_id, root, path):
            print(f"{path.relative_to(root)}: plan ID is already used by another plan.")
            failed = True
            continue
        if check_published:
            try:
                published_duplicate = find_published_duplicate(plan)
            except (httpx.HTTPError, ValueError):
                print(f"{path.relative_to(root)}: published catalogue duplicate check failed.")
                failed = True
                continue
            if published_duplicate is not None:
                print(
                    f"{path.relative_to(root)}: duplicate pricing found in the published catalogue."
                )
                failed = True
    return int(failed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    process = commands.add_parser("process", help="Process the GitHub issue event.")
    process.add_argument(
        "--event", type=Path, default=Path(os.environ.get("GITHUB_EVENT_PATH", "event.json"))
    )
    process.add_argument(
        "--base-branch",
        default=os.environ.get("GITHUB_BASE_BRANCH", "main"),
    )
    validate = commands.add_parser(
        "validate-community", help="Validate checked-in community plans."
    )
    validate.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    if args.command == "process":
        try:
            return _process_event(Path.cwd(), args.event, args.base_branch)
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
            print(f"Community intake failed: {type(error).__name__}.", file=sys.stderr)
            return 1
    return _validate_community(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
