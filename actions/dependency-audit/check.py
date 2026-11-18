"""Enforce shared, narrowly scoped exceptions for dependency audit findings."""

import datetime as dt
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote

ID_PATTERN = re.compile(
    r"\b(?:GHSA-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}|CVE-\d{4}-\d+|PYSEC-\d{4}-\d+)\b",
    re.IGNORECASE,
)
ECOSYSTEMS = {"npm", "python", "maven"}
REPORT_FORMATS = {"npm-audit", "pip-audit", "dependency-check"}


def normalize_package(package, ecosystem):
    package = unquote(str(package)).strip()
    if ecosystem == "python":
        return re.sub(r"[-_.]+", "-", package).lower()
    return package.lower()


def advisory_ids(*values):
    return {
        match.upper()
        for value in values
        if value is not None
        for match in ID_PATTERN.findall(str(value))
    }


def read_policy(path, today=None):
    policy = json.loads(path.read_text())
    if policy.get("version") != 1 or not isinstance(policy.get("exceptions"), list):
        raise ValueError("Unsupported dependency audit exception policy format")

    today = today or dt.datetime.now(dt.UTC).date()
    exceptions = []
    seen = set()
    for exception in policy["exceptions"]:
        required = {"ecosystem", "package", "advisory", "expires", "reason"}
        if not isinstance(exception, dict) or not required.issubset(exception):
            raise ValueError(
                "Each exception must define ecosystem, package, advisory, expires, and reason"
            )
        ecosystem = exception["ecosystem"]
        if ecosystem not in ECOSYSTEMS:
            raise ValueError(f"Unsupported exception ecosystem: {ecosystem}")
        package = normalize_package(exception["package"], ecosystem)
        identifiers = advisory_ids(exception["advisory"])
        if not package or len(identifiers) != 1 or not exception["reason"].strip():
            raise ValueError(
                "Exception package, advisory ID, and reason must be non-empty and valid"
            )
        expires = dt.date.fromisoformat(exception["expires"])
        if expires < today:
            raise ValueError(
                f"Expired dependency audit exception: {exception['advisory']} for {exception['package']}"
            )
        key = (ecosystem, package, next(iter(identifiers)))
        if key in seen:
            raise ValueError(f"Duplicate dependency audit exception: {key[2]}")
        seen.add(key)
        exceptions.append(
            {
                **exception,
                "package_normalized": package,
                "advisory_normalized": next(iter(identifiers)),
            }
        )
    return exceptions


def matches_exception(exceptions, ecosystem, package, identifiers):
    package = normalize_package(package, ecosystem)
    identifiers = {identifier.upper() for identifier in identifiers}
    return next(
        (
            exception
            for exception in exceptions
            if exception["ecosystem"] == ecosystem
            and exception["package_normalized"] == package
            and exception["advisory_normalized"] in identifiers
        ),
        None,
    )


def npm_findings(report, exceptions):
    if not isinstance(report, dict) or report.get("error"):
        raise ValueError("npm audit returned an error or an invalid report")
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise TypeError("npm audit report has no vulnerabilities map")

    blocked = []
    suppressed = []
    packages = {package.lower(): package for package in vulnerabilities}

    def is_excepted(package, active=()):
        vulnerability = vulnerabilities[package]
        severity = str(vulnerability.get("severity", "")).lower()
        if severity not in {"high", "critical"}:
            return False, []
        if package.lower() in active:
            # npm can report peer-dependency cycles among metavulnerable packages.
            # Defer that edge; another cause may still anchor the cycle in a
            # direct advisory. A cycle with no such cause remains unapproved.
            return None, []

        via = vulnerability.get("via") or []
        if not via:
            return False, []
        active = {*active, package.lower()}
        suppressions = {}
        high_causes = 0
        for cause in via:
            if isinstance(cause, dict):
                cause_severity = str(cause.get("severity", severity)).lower()
                if cause_severity not in {"high", "critical"}:
                    continue
                high_causes += 1
                identifiers = advisory_ids(
                    cause.get("id"),
                    cause.get("url"),
                    cause.get("title"),
                    cause.get("name"),
                )
                exception = matches_exception(exceptions, "npm", package, identifiers)
                if not exception:
                    return False, []
                key = (
                    exception["package_normalized"],
                    exception["advisory_normalized"],
                )
                suppressions[key] = exception
            elif isinstance(cause, str):
                target = packages.get(cause.lower())
                if target is None:
                    return False, []
                target_severity = str(
                    vulnerabilities[target].get("severity", "")
                ).lower()
                if target_severity not in {"high", "critical"}:
                    continue
                accepted, target_suppressions = is_excepted(target, active)
                if accepted is None:
                    continue
                if not accepted:
                    return False, []
                high_causes += 1
                for exception in target_suppressions:
                    key = (
                        exception["package_normalized"],
                        exception["advisory_normalized"],
                    )
                    suppressions[key] = exception
            else:
                return False, []
        return (True if high_causes else None), list(suppressions.values())

    for package, vulnerability in vulnerabilities.items():
        severity = str(vulnerability.get("severity", "")).lower()
        if severity not in {"high", "critical"}:
            continue
        accepted, package_suppressions = is_excepted(package)
        if accepted:
            suppressed.extend(
                (exception["package"], exception) for exception in package_suppressions
            )
        else:
            via = vulnerability.get("via") or []
            identifiers = advisory_ids(
                *[
                    value
                    for cause in via
                    if isinstance(cause, dict)
                    for value in (
                        cause.get("id"),
                        cause.get("url"),
                        cause.get("title"),
                        cause.get("name"),
                    )
                ]
            )
            label = ", ".join(sorted(identifiers)) or "unapproved metavulnerability"
            blocked.append(f"{package}: {severity} {label}")
    unique_suppressions = {}
    for package, exception in suppressed:
        key = (
            normalize_package(package, "npm"),
            exception["advisory_normalized"],
        )
        unique_suppressions.setdefault(key, (package, exception))
    return blocked, list(unique_suppressions.values())


def pip_findings(report, exceptions):
    dependencies = report if isinstance(report, list) else report.get("dependencies")
    if not isinstance(dependencies, list):
        raise TypeError("pip-audit report has an invalid dependency list")

    blocked = []
    suppressed = []
    for dependency in dependencies:
        package = dependency.get("name", "")
        for vulnerability in dependency.get("vulns") or []:
            identifiers = advisory_ids(
                vulnerability.get("id"), *(vulnerability.get("aliases") or [])
            )
            exception = matches_exception(exceptions, "python", package, identifiers)
            if exception:
                suppressed.append((package, exception))
            else:
                label = ", ".join(sorted(identifiers)) or "unknown advisory"
                blocked.append(f"{package}: {label}")
    return blocked, suppressed


def dependency_package_names(dependency, ecosystem):
    names = []
    for package in dependency.get("packages", []):
        identifier = package.get("id", "")
        if not identifier.startswith("pkg:"):
            continue
        package_type, _, name = identifier[4:].partition("/")
        if package_type.lower() != {"python": "pypi", "maven": "maven"}.get(ecosystem):
            continue
        name = unquote(name.split("?", 1)[0].split("#", 1)[0])
        name = name.rsplit("@", 1)[0]
        if ecosystem == "maven":
            parts = name.split("/")
            if len(parts) == 2:
                names.append(f"{parts[0]}:{parts[1]}")
        else:
            names.append(name)
    return names


def dependency_check_findings(report, ecosystem, exceptions):
    dependencies = report.get("dependencies") if isinstance(report, dict) else None
    if not isinstance(dependencies, list):
        raise TypeError("Dependency-Check report has an invalid dependency list")

    blocked = []
    suppressed = []
    for dependency in dependencies:
        packages = dependency_package_names(dependency, ecosystem)
        for vulnerability in dependency.get("vulnerabilities") or []:
            cvss = [
                (vulnerability.get("cvssv4") or {}).get("baseScore"),
                (vulnerability.get("cvssv3") or {}).get("baseScore"),
                (vulnerability.get("cvssv2") or {}).get("score"),
            ]
            scores = [float(score) for score in cvss if score is not None]
            if not scores or max(scores) < 7:
                continue
            identifiers = advisory_ids(
                vulnerability.get("name"),
                *[
                    reference.get("name")
                    for reference in vulnerability.get("references") or []
                ],
            )
            match = next(
                (
                    (package, exception)
                    for package in packages
                    if (
                        exception := matches_exception(
                            exceptions, ecosystem, package, identifiers
                        )
                    )
                ),
                None,
            )
            if match:
                suppressed.append(match)
            else:
                package = ", ".join(packages) or dependency.get(
                    "fileName", "unknown package"
                )
                label = ", ".join(sorted(identifiers)) or "unknown advisory"
                blocked.append(f"{package}: CVSS {max(scores):g} {label}")
    return blocked, suppressed


def main():
    ecosystem = os.environ.get("AUDIT_ECOSYSTEM", "")
    report_format = os.environ.get("AUDIT_REPORT_FORMAT", "")
    if ecosystem not in ECOSYSTEMS or report_format not in REPORT_FORMATS:
        raise ValueError("Unsupported ecosystem or audit report format")
    if (report_format, ecosystem) not in {
        ("npm-audit", "npm"),
        ("pip-audit", "python"),
        ("dependency-check", "python"),
        ("dependency-check", "maven"),
    }:
        raise ValueError("Audit report format does not match its ecosystem")

    policy_dir = Path(os.environ["AUDIT_POLICY_DIR"])
    report_path = Path(os.environ["AUDIT_REPORT_PATH"])
    report = json.loads(report_path.read_text())
    exceptions = read_policy(policy_dir / "exceptions.json")
    if report_format == "npm-audit":
        blocked, suppressed = npm_findings(report, exceptions)
    elif report_format == "pip-audit":
        blocked, suppressed = pip_findings(report, exceptions)
    else:
        blocked, suppressed = dependency_check_findings(report, ecosystem, exceptions)

    if suppressed:
        lines = ["### Dependency audit exceptions applied", ""]
        for package, exception in suppressed:
            message = (
                f"- `{package}` / `{exception['advisory']}` through "
                f"{exception['expires']}: {exception['reason']}"
            )
            print(f"::warning::{message}")
            lines.append(message)
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a") as summary:
                summary.write("\n".join(lines) + "\n")

    if blocked:
        for finding in blocked:
            print(f"::error::Unapproved dependency finding: {finding}")
        raise SystemExit(1)
    print("Dependency audit passed at the configured severity threshold.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"::error::Dependency audit policy failed closed: {error}")
        raise SystemExit(1) from error
