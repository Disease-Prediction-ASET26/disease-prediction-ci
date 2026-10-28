# Disease Prediction CI

This repository contains the shared GitHub Actions workflows for the AI,
backend, and frontend repositories. Each application keeps its own commands in
`.github/workflows/ci.yml`.

## Application pipelines

The pipelines provide these stages: `lint`, `build`, `unit-test`,
`integration-test`, `sonarqube`, `owasp`, `containerize`, and `scan`.
Containerization and image scanning run in the same job.

| Repository | Enabled checks                                                            | Not yet configured                                                   |
| ---------- | ------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| AI         | Workflow lint, Ruff, Python compilation, Dependency-Check, pip-audit      | Unit tests, integration tests, service packaging, Dockerfile         |
| Backend    | Workflow lint, Maven package, Spring context smoke test, Dependency-Check | Java lint, unit tests, Dockerfile                                    |
| Frontend   | Workflow lint and readiness summary                                       | Runtime, dependency manager, application commands, tests, Dockerfile |

The AI build checks Python compilation only. The backend integration test runs
the existing Spring context test. Disabled tests provide no coverage.

## Use the pipelines

1. Create a feature branch in the application repository.
2. Open an internal pull request to `main`.
3. Open the **Actions** tab or the pull request checks.
4. Check the `ci` job summary for results and disabled stages.
5. Merge the pull request after the required checks pass.

The `ci` check fails if an enabled stage fails. Where available, reports are
uploaded as workflow artifacts for three days. Workflows also run after a push
to `main`. Fork pull request workflows are disabled.

## Dependency scanning

The AI and backend pipelines use OWASP Dependency-Check. The AI pipeline also
uses `pip-audit` to check resolved Python dependencies. The frontend scan is
disabled until the frontend stack and dependency files are available. The shared
scan workflow currently supports Python and Maven.

Dependency-Check fails at CVSS 7 or higher. `pip-audit` fails on any advisory.
Scanner errors fail the job. The credential-dependent OSS Index analyzer is
disabled.

The shared updater refreshes the OWASP database every day at `03:17 UTC`. It
publishes a private GHCR snapshot only after the smoke test passes. A failed
update leaves the previous snapshot available. Each application scan uses its
own copy. A missing or more-than-48-hour-old snapshot fails the scan.

To run a normal refresh manually, open **Actions > Refresh OWASP snapshot** in
this repository. Select **Run workflow** on `main`. Leave `bootstrap` disabled
to reuse the existing snapshot.

## Container images

Container stages are disabled until each application provides a `Dockerfile`.
Add a `.dockerignore` file to exclude secrets and local files. After that, set
`CONTAINER_ENABLED` to `true` in the application repository. The workflow builds
one image, scans it with Trivy, and publishes it only if the required checks
pass. Trivy fails on HIGH and CRITICAL findings, including unfixed findings.

| Build       | Private GHCR package                          | Tags                             |
| ----------- | --------------------------------------------- | -------------------------------- |
| Internal PR | `ghcr.io/disease-prediction-aset26/<repo>-pr` | Unique run tag and `pr-<number>` |
| Main        | `ghcr.io/disease-prediction-aset26/<repo>`    | Unique run tag and `main`        |

Use the image digest from the job summary to identify an exact image. Preview
cleanup runs when an internal PR closes and each day at `03:47 UTC`. The preview
package needs **Admin** access for its application repository to delete image
versions. Production packages are not cleaned automatically.

## SonarQube

SonarQube is prepared but disabled. To enable it, set the repository variables
`SONAR_ENABLED`, `SONAR_HOST_URL`, and `SONAR_PROJECT_KEY`. Set `SONAR_ENABLED`
to `true`. Add `SONAR_TOKEN` as a repository secret. Analysis runs on `main` and
waits for the quality gate. Application teams must add coverage generation
before reporting coverage.

## Shared workflows

| Workflow                                                     | Purpose                                         |
| ------------------------------------------------------------ | ----------------------------------------------- |
| [dependency-scan.yml](.github/workflows/dependency-scan.yml) | Scan Python and Maven dependencies.             |
| [owasp-refresh.yml](.github/workflows/owasp-refresh.yml)     | Update and publish the OWASP database snapshot. |
| [container.yml](.github/workflows/container.yml)             | Build, scan, and publish an application image.  |
| [preview-cleanup.yml](.github/workflows/preview-cleanup.yml) | Remove outdated PR preview images.              |
| [validate.yml](.github/workflows/validate.yml)               | Check shared workflows and Python tools.        |

Actions and reusable workflows use full commit SHAs. Dependabot proposes weekly
updates. See [TOOL-VERSIONS.md](TOOL-VERSIONS.md) for the pinned versions.

## Check changes to this repository

Use Python 3.13 and the tool versions in [TOOL-VERSIONS.md](TOOL-VERSIONS.md).
Run these commands before opening a PR:

```sh
ruff check --isolated actions tests
ruff format --check --isolated actions tests
python -m unittest discover -s tests -v
actionlint .github/workflows/*.yml
```
