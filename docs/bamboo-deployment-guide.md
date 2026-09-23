# Bamboo CI

The build plan has one repository entry point:

```bash
./ci_cd_scripts/bamboo_build.sh
```

The agent requires Python 3.13, `uv`, and `sonar-scanner` on the worker. The script installs from
`uv.lock`, runs Ruff lint/format checks, enforces 85% test coverage, publishes JUnit and Cobertura
artifacts, and invokes SonarQube using `sonar-project.properties`.
