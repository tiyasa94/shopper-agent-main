#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# bamboo_build.sh — Canonical Bamboo CI entrypoint for shopper-agent
#
# Runs the established test suite, enforces 100% branch coverage gate, and
# exports coverage XML for SonarQube. Any failure immediately stops the build.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
ROOT_DIR="${SCRIPT_DIR}/.."
ARTIFACTS_DIR="${ROOT_DIR}/artifacts"

export UV_PYTHON="${UV_PYTHON:-3.13}"

cd "${ROOT_DIR}"
mkdir -p "${ARTIFACTS_DIR}"

echo "═══ sydent-ibm-wxo-agents ═══"

# Verify uv is installed, install if missing
if ! command -v uv &> /dev/null; then
    echo "⚠ uv is not installed, installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --no-modify-path
    
    # Add uv to PATH for current session
    export PATH="$HOME/.local/bin:$PATH"
    
    # Verify installation
    if ! command -v uv &> /dev/null; then
        echo "✗ uv installation failed" >&2
        exit 1
    fi
    echo "✓ uv installed successfully"
else
    echo "✓ uv is already installed"
fi

echo "Installing the locked Python 3.13 environment..."
uv sync --frozen

echo "Checking Python style with Ruff..."
uv run ruff check .
uv run ruff format --check .

echo "Running deterministic tests with 100% branch coverage gate..."
uv run pytest \
  --cov=src \
  --cov-branch \
  --cov-report="xml:${ARTIFACTS_DIR}/coverage.xml" \
  --cov-report=term-missing \
  --cov-fail-under=100 \
  --junitxml="${ARTIFACTS_DIR}/junit.xml"

echo "sydent-ibm-wxo-agents: tests passed, coverage exported."

# ── SonarQube Scanner (Runs if available, or delegated to Bamboo Task) ───────
if [[ "${RUN_SONAR_SCANNER:-0}" == "1" ]]; then
  # Check if sonar-scanner is available
  if ! command -v sonar-scanner >/dev/null 2>&1; then
    echo "sonar-scanner not found, attempting to locate or install..."
    
    # Common installation paths
    COMMON_PATHS=(
      "/opt/sonar-scanner/bin"
      "/usr/local/sonar-scanner/bin"
      "/usr/local/bin"
      "$HOME/sonar-scanner/bin"
      "$HOME/.sonar/sonar-scanner-*/bin"
    )
    
    FOUND=false
    for path in "${COMMON_PATHS[@]}"; do
      for expanded_path in $path; do
        if [ -d "$expanded_path" ] && [ -f "$expanded_path/sonar-scanner" ]; then
          echo "Found sonar-scanner at: $expanded_path"
          export PATH="$expanded_path:$PATH"
          FOUND=true
          break 2
        fi
      done
    done
    
    if [ "$FOUND" = false ]; then
      # Auto-install sonar-scanner
      echo "Installing sonar-scanner..."
      
      # Detect OS
      OS_TYPE=$(uname -s | tr '[:upper:]' '[:lower:]')
      
      SONAR_VERSION="6.2.1.4610"
      SONAR_DIR="$HOME/.sonar"
      
      # Determine the correct sonar-scanner package based on OS
      if [[ "$OS_TYPE" == "darwin" ]]; then
        # macOS
        SONAR_ZIP="sonar-scanner-cli-${SONAR_VERSION}-macosx-x64.zip"
        SONAR_EXTRACT_DIR="sonar-scanner-${SONAR_VERSION}-macosx-x64"
      elif [[ "$OS_TYPE" == "linux" ]]; then
        # Linux
        SONAR_ZIP="sonar-scanner-cli-${SONAR_VERSION}-linux-x64.zip"
        SONAR_EXTRACT_DIR="sonar-scanner-${SONAR_VERSION}-linux-x64"
      else
        echo "Error: Unsupported OS: $OS_TYPE" >&2
        echo "Please install sonar-scanner manually or set RUN_SONAR_SCANNER=0" >&2
        exit 1
      fi
      
      SONAR_URL="https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/${SONAR_ZIP}"
      
      mkdir -p "$SONAR_DIR"
      cd "$SONAR_DIR"
      
      # Download
      if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$SONAR_URL" -o "${SONAR_ZIP}" || { echo "Failed to download sonar-scanner" >&2; exit 1; }
      elif command -v wget >/dev/null 2>&1; then
        wget -q "$SONAR_URL" -O "${SONAR_ZIP}" || { echo "Failed to download sonar-scanner" >&2; exit 1; }
      else
        echo "Error: Neither curl nor wget found" >&2
        exit 1
      fi
      
      # Extract
      if command -v unzip >/dev/null 2>&1; then
        unzip -q "${SONAR_ZIP}" || { echo "Failed to extract sonar-scanner" >&2; exit 1; }
        rm "${SONAR_ZIP}"
      else
        echo "Error: unzip command not found" >&2
        exit 1
      fi
      
      # Add to PATH
      SONAR_BIN_DIR="$SONAR_DIR/${SONAR_EXTRACT_DIR}/bin"
      export PATH="$SONAR_BIN_DIR:$PATH"
      
      cd "$ROOT_DIR"
      
      if command -v sonar-scanner >/dev/null 2>&1; then
        echo "sonar-scanner installed successfully"
      else
        echo "Error: Failed to install sonar-scanner" >&2
        exit 1
      fi
    fi
  fi
  
  # Verify sonar-scanner is available
  if ! command -v sonar-scanner >/dev/null 2>&1; then
    echo "Error: sonar-scanner is required but not available" >&2
    exit 1
  fi
  
  echo ""
  echo "═══ Running SonarQube Scanner ═══"
  
  # Detect if running in CI environment
  if [[ -n "${BAMBOO_BUILD_NUMBER:-}" ]] || [[ -n "${CI:-}" ]] || [[ -n "${JENKINS_HOME:-}" ]]; then
    # In CI: SonarQube failure should fail the build
    sonar-scanner
  else
    # Local development: SonarQube failure is just a warning
    if sonar-scanner; then
      echo "SonarQube analysis completed successfully"
    else
      echo "Warning: SonarQube analysis failed (non-fatal in local development)"
      echo "This is expected if you're not connected to the corporate network"
    fi
  fi
fi

echo ""
echo "═══ Build complete ═══"
echo "Coverage artifacts:"
ls -1 "${ARTIFACTS_DIR}"/*.xml
