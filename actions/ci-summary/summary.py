"""Keep disabled stages explicit without hiding failures or cancellations."""

import json
import os
import sys


def evaluate(needs, disabled, enabled=()):
    failed = []
    lines = ["| Stage | Result |", "| --- | --- |"]
    for name, job in needs.items():
        result = job["result"]
        if result == "skipped" and name in disabled and name not in enabled:
            result = "Not configured: " + disabled[name]
        elif result != "success":
            failed.append(name)
        lines.append(f"| {name} | {result} |")
    lines.append(
        "\nDisabled tests provide no coverage. Only configured stages contribute to this CI result."
    )
    return failed, "\n".join(lines) + "\n"


def main():
    failed, report = evaluate(
        json.loads(os.environ["CI_NEEDS"]),
        json.loads(os.environ["CI_DISABLED"]),
        json.loads(os.environ.get("CI_ENABLED", "[]")),
    )
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(report)
    if failed:
        sys.exit("Unsuccessful enabled stages: " + ", ".join(failed))


if __name__ == "__main__":
    main()
