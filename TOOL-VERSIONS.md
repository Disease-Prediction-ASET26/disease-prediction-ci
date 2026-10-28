# Tool versions

Resolved from official release tags and registries on 2026-10-28.

| Tool                              | Version | Commit or digest                                                          | Source                                                                              |
| --------------------------------- | ------- | ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| actions/checkout                  | v7.0.1  | `3d3c42e5aac5ba805825da76410c181273ba90b1`                                | [Release](https://github.com/actions/checkout/releases/tag/v7.0.1)                  |
| actions/setup-python              | v7.1.0  | `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0`                                | [Release](https://github.com/actions/setup-python/releases/tag/v7.1.0)              |
| actions/setup-java                | v6.1.0  | `b843ebf9b913146b38da2eaa7aeddf76074ae1ad`                                | [Release](https://github.com/actions/setup-java/releases/tag/v6.1.0)                |
| actions/upload-artifact           | v7.0.2  | `cf430e030ddbb5b0abf93d22962f4752f3646cd9`                                | [Release](https://github.com/actions/upload-artifact/releases/tag/v7.0.2)           |
| actions/download-artifact         | v8.0.2  | `9000827ccba6bdab643e8b6fd33ac0654aef8333`                                | [Release](https://github.com/actions/download-artifact/releases/tag/v8.0.2)         |
| docker/login-action               | v4.7.0  | `81c6f93d4bc991e510b342a00b419b3bb4efc5c6`                                | [Release](https://github.com/docker/login-action/releases/tag/v4.7.0)               |
| docker/setup-buildx-action        | v4.4.1  | `f87e5991a6d7451dcb8d9637bfbc97413f497069`                                | [Release](https://github.com/docker/setup-buildx-action/releases/tag/v4.4.1)        |
| docker/build-push-action          | v7.4.0  | `c3c9e263c25d99ce0380d002d59b67737d91b0dc`                                | [Release](https://github.com/docker/build-push-action/releases/tag/v7.4.0)          |
| aquasecurity/trivy-action         | v0.36.0 | `ed142fd0673e97e23eac54620cfb913e5ce36c25`                                | [Release](https://github.com/aquasecurity/trivy-action/releases/tag/v0.36.0)        |
| SonarSource/sonarqube-scan-action | v8.4.0  | `4d1dc0c91ee400dcf8a6dddf2f705ac36542e304`                                | [Release](https://github.com/SonarSource/sonarqube-scan-action/releases/tag/v8.4.0) |
| rhysd/actionlint                  | v1.7.12 | `8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8`        | [Release](https://github.com/rhysd/actionlint/releases/tag/v1.7.12)                 |
| dependency-check/DependencyCheck  | v13.0.0 | `4aacdb10707fe88a55b873dd8f7235ad4ad3555f`                                | [Release](https://github.com/dependency-check/DependencyCheck/releases/tag/v13.0.0) |
| ruff                              | 0.17.3  | `PyPI release`                                                            | [Release](https://pypi.org/project/ruff/0.17.3/)                                    |
| pip-audit                         | 2.10.1  | `PyPI release`                                                            | [Release](https://pypi.org/project/pip-audit/2.10.1/)                               |
| owasp/dependency-check            | 13.0.0  | `sha256:0e8f69ad5b00d62000937f4e57189812343f6903d9c83ee712012d51542a020a` | [Release](https://hub.docker.com/r/owasp/dependency-check/tags?name=13.0.0)         |

The backend Sonar Maven scanner uses
[5.8.0.7211](https://repo.maven.apache.org/maven2/org/sonarsource/scanner/maven/sonar-maven-plugin/5.8.0.7211/).
The Trivy action explicitly selects CLI v0.70.0, its upstream default at the
pinned action commit. Dependabot proposes weekly updates. Engine/schema upgrades
also require a fresh snapshot and reviewed consumer pins.

Bootstrap uses [actions/cache v6.1.0](https://github.com/actions/cache/releases/tag/v6.1.0) at `55cc8345863c7cc4c66a329aec7e433d2d1c52a9` (verified 2026-10-28).

The [Log4j fixture](https://repo.maven.apache.org/maven2/org/apache/logging/log4j/log4j-core/2.14.1/log4j-core-2.14.1.jar) uses SHA-512 `6e41ff42f12deedb8da06cbed73d0a9a5389660b7ee058436f8fcb6b14a6ab3105faf8e3f2c007d38ccc85af1e704505b84be5a80d8e68a434aae82b54b85f70`. The download matched Maven Central's published SHA-1 before this stronger digest was pinned.
