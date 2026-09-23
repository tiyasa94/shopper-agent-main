# Bamboo Build - sydent-ibm-wxo-agents

`bamboo_build.sh` is the single CI entry point for the sydent-ibm-wxo-agents project. It installs
from `uv.lock`, **auto-fixes code quality issues**, runs Ruff lint and format checks, executes the
deterministic test suite with a **100% branch coverage gate**, writes JUnit and Cobertura artifacts
under `artifacts/`, and invokes `sonar-scanner`.

## Build Process

The build script performs the following steps in order:

1. **Dependency Installation**: Installs locked dependencies from `uv.lock` using Python 3.13
2. **Auto-Fix Code Quality**: Automatically fixes linting issues and formats all Python files
3. **Verification**: Verifies that all code passes linting and formatting checks
4. **Test Execution**: Runs the full test suite with 100% branch coverage enforcement
5. **Artifact Generation**: Creates coverage.xml, junit.xml, and HTML coverage reports
6. **SonarQube Analysis**: Runs sonar-scanner (if enabled)

## Code Quality

The build automatically handles code quality issues:

- **Auto-fix linting**: Runs `ruff check --fix` to automatically fix fixable issues
- **Auto-format**: Runs `ruff format` to format all Python files
- **Verification**: Ensures all code passes final quality checks

This means developers don't need to manually fix formatting issues before pushing - the CI will
handle it automatically. However, if there are linting issues that cannot be auto-fixed, the build
will fail with clear error messages.

## Coverage Requirements

The build enforces **100% branch coverage** for all code in `src/`. This ensures:
- Every conditional branch (if/else) is tested
- All code paths are exercised by tests
- High code quality and reliability

Branch coverage is configured in `pyproject.toml` under `[tool.coverage.run]` and
`[tool.coverage.report]`.

## Local Testing

Set `RUN_SONAR_SCANNER=0` for local reproduction when Sonar credentials are unavailable:

```bash
RUN_SONAR_SCANNER=0 ./ci_cd_scripts/bamboo_build.sh
```

The build will fail if branch coverage falls below 100%. To see detailed coverage reports:

```bash
uv run pytest --cov=src --cov-branch --cov-report=html
open htmlcov/index.html
```

## Troubleshooting

If the build fails:

1. **Linting errors**: The build auto-fixes most issues. If it still fails, check the error message
   for issues that require manual fixes (e.g., unused imports, undefined variables)
2. **Coverage below 100%**: Add tests to cover the missing branches shown in the error output
3. **Test failures**: Fix the failing tests - the build shows which tests failed and why
