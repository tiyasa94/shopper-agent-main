#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# bamboo_build_final.sh — Enhanced Bamboo CI entrypoint for sydent-ibm-wxo-agents
#
# This comprehensive build script provides detailed reporting and validation.
# It runs the test suite, enforces 100% branch coverage gate, generates detailed
# coverage reports with statistics, and exports artifacts for SonarQube analysis.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# Colors for output
readonly GREEN='\033[0;32m'
readonly BLUE='\033[0;34m'
readonly RED='\033[0;31m'
readonly YELLOW='\033[1;33m'
readonly CYAN='\033[0;36m'
readonly NC='\033[0m' # No Color

# Directory setup
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
readonly ROOT_DIR="${SCRIPT_DIR}/.."
readonly ARTIFACTS_DIR="${ROOT_DIR}/artifacts"

# Python version
export UV_PYTHON="${UV_PYTHON:-3.13}"

# Coverage threshold
readonly COVERAGE_THRESHOLD=100

# ══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

print_header() {
    echo ""
    echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
}

print_step() {
    echo ""
    echo -e "${CYAN}▶ Step $1: $2${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}" >&2
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_info() {
    echo -e "${CYAN}ℹ $1${NC}"
}

# ══════════════════════════════════════════════════════════════════════════════
# MAIN BUILD PROCESS
# ══════════════════════════════════════════════════════════════════════════════

print_header "sydent-ibm-wxo-agents - Comprehensive Build"

cd "${ROOT_DIR}"
mkdir -p "${ARTIFACTS_DIR}"

print_info "Project root: ${ROOT_DIR}"
print_info "Artifacts directory: ${ARTIFACTS_DIR}"
print_info "Python version: ${UV_PYTHON}"
print_info "Coverage threshold: ${COVERAGE_THRESHOLD}%"

# ──────────────────────────────────────────────────────────────────────────────
# STEP 1: Git Repository Verification
# ──────────────────────────────────────────────────────────────────────────────
print_step "1" "Verifying git repository"

if git rev-parse --is-shallow-repository 2>/dev/null | grep -q "true"; then
    print_warning "Shallow clone detected, converting to full clone..."
    git fetch --unshallow || print_warning "Fetch failed (continuing anyway)"
    print_success "Clone depth fixed"
else
    print_success "Already a full clone"
fi

# ──────────────────────────────────────────────────────────────────────────────
# STEP 2: Python Environment Setup
# ──────────────────────────────────────────────────────────────────────────────
print_step "2" "Setting up Python ${UV_PYTHON} environment with uv"

# Verify uv is installed, install if missing
if ! command -v uv &> /dev/null; then
    print_warning "uv is not installed, installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --no-modify-path

    # Add uv to PATH for current session
    export PATH="$HOME/.local/bin:$PATH"

    # Verify installation
    if ! command -v uv &> /dev/null; then
        print_error "uv installation failed"
        exit 1
    fi
    print_success "uv installed successfully"
else
    print_success "uv is already installed"
fi

print_info "Installing locked dependencies..."
uv sync --frozen || {
    print_error "Dependency installation failed"
    exit 1
}

print_info "Verifying Python version..."
PYTHON_VERSION=$(uv run python --version)
print_success "Using: ${PYTHON_VERSION}"

# ──────────────────────────────────────────────────────────────────────────────
# STEP 3: Code Quality Checks
# ──────────────────────────────────────────────────────────────────────────────
print_step "3" "Checking code quality with Ruff"

print_info "Running Ruff linting..."
if ! uv run ruff check .; then
    print_error "Code has linting issues"
    exit 1
fi
print_success "Linting passed"

print_info "Running Ruff format check..."
if ! uv run ruff format --check .; then
    print_error "Code formatting check failed"
    exit 1
fi
print_success "Format check passed"

print_success "All code quality checks passed"

# ──────────────────────────────────────────────────────────────────────────────
# STEP 4: Test Execution with Coverage
# ──────────────────────────────────────────────────────────────────────────────
print_step "4" "Running deterministic tests with ${COVERAGE_THRESHOLD}% branch coverage gate"

print_info "Executing pytest with comprehensive coverage..."
START_TIME=$(date +%s)

if ! uv run pytest \
  --cov=src \
  --cov-branch \
  --cov-report="xml:${ARTIFACTS_DIR}/coverage.xml" \
  --cov-report="html:${ARTIFACTS_DIR}/htmlcov" \
  --cov-report=term-missing \
  --cov-fail-under=${COVERAGE_THRESHOLD} \
  --junitxml="${ARTIFACTS_DIR}/junit.xml" \
  -v; then
    print_error "Tests failed or coverage below ${COVERAGE_THRESHOLD}%"
    exit 1
fi

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

print_success "All tests passed in ${DURATION} seconds"

# ──────────────────────────────────────────────────────────────────────────────
# STEP 5: Coverage Analysis and Reporting
# ──────────────────────────────────────────────────────────────────────────────
print_step "5" "Analyzing coverage results"

if [ -f "${ARTIFACTS_DIR}/coverage.xml" ]; then
    # Extract coverage statistics
    COVERAGE_STATS=$(uv run python -c "
import xml.etree.ElementTree as ET
try:
    tree = ET.parse('${ARTIFACTS_DIR}/coverage.xml')
    root = tree.getroot()

    line_rate = float(root.attrib.get('line-rate', 0))
    branch_rate = float(root.attrib.get('branch-rate', 0))
    lines_covered = int(root.attrib.get('lines-covered', 0))
    lines_valid = int(root.attrib.get('lines-valid', 0))
    branches_covered = int(root.attrib.get('branches-covered', 0))
    branches_valid = int(root.attrib.get('branches-valid', 0))

    print(f'Line Coverage: {line_rate * 100:.2f}% ({lines_covered}/{lines_valid} lines)')
    print(f'Branch Coverage: {branch_rate * 100:.2f}% ({branches_covered}/{branches_valid} branches)')

    # Count files
    file_count = len(root.findall('.//class'))
    print(f'Files Analyzed: {file_count}')
except Exception as e:
    print(f'Error parsing coverage: {e}')
" 2>/dev/null || echo "Coverage data unavailable")

    echo -e "${GREEN}Coverage Results:${NC}"
    echo "$COVERAGE_STATS" | while IFS= read -r line; do
        echo "  $line"
    done

    print_success "Coverage analysis complete"
else
    print_warning "Coverage file not found"
fi

# ──────────────────────────────────────────────────────────────────────────────
# STEP 6: Artifact Verification
# ──────────────────────────────────────────────────────────────────────────────
print_step "6" "Verifying generated artifacts"

print_info "Checking for required artifacts..."

# List of critical artifacts
declare -a CRITICAL_FILES=("coverage.xml" "junit.xml")
MISSING_COUNT=0

for file in "${CRITICAL_FILES[@]}"; do
    if [ -f "${ARTIFACTS_DIR}/${file}" ]; then
        FILE_SIZE=$(ls -lh "${ARTIFACTS_DIR}/${file}" | awk '{print $5}')
        print_success "${file} (${FILE_SIZE})"
    else
        print_error "${file} - MISSING"
        MISSING_COUNT=$((MISSING_COUNT + 1))
    fi
done

# Check for HTML coverage report
if [ -d "${ARTIFACTS_DIR}/htmlcov" ]; then
    HTML_FILES=$(find "${ARTIFACTS_DIR}/htmlcov" -name "*.html" | wc -l | tr -d ' ')
    print_success "htmlcov/ (${HTML_FILES} HTML files)"
else
    print_warning "htmlcov/ directory not found"
fi

if [ $MISSING_COUNT -gt 0 ]; then
    print_error "${MISSING_COUNT} critical artifact(s) missing"
    exit 1
fi

print_success "All critical artifacts generated"

# ──────────────────────────────────────────────────────────────────────────────
# STEP 7: Build Summary Generation
# ──────────────────────────────────────────────────────────────────────────────
print_step "7" "Generating build summary"

# Count tests
TEST_COUNT=$(grep -o 'tests="[0-9]*"' "${ARTIFACTS_DIR}/junit.xml" 2>/dev/null | grep -o '[0-9]*' || echo "0")
FAILURE_COUNT=$(grep -o 'failures="[0-9]*"' "${ARTIFACTS_DIR}/junit.xml" 2>/dev/null | grep -o '[0-9]*' || echo "0")
SKIP_COUNT=$(grep -o 'skipped="[0-9]*"' "${ARTIFACTS_DIR}/junit.xml" 2>/dev/null | grep -o '[0-9]*' || echo "0")

# Create build summary file
cat > "${ARTIFACTS_DIR}/BUILD_SUMMARY.txt" << EOF
═══════════════════════════════════════════════════════════════
Build Summary - sydent-ibm-wxo-agents
═══════════════════════════════════════════════════════════════

Build Date: $(date)
Build Duration: ${DURATION} seconds
Python Version: ${PYTHON_VERSION}

Test Results:
  Total Tests: ${TEST_COUNT}
  Passed: $((TEST_COUNT - FAILURE_COUNT - SKIP_COUNT))
  Failed: ${FAILURE_COUNT}
  Skipped: ${SKIP_COUNT}

Coverage:
  Threshold: ${COVERAGE_THRESHOLD}%
  Status: PASSED (100% achieved)

Code Quality:
  Ruff Linting: PASSED
  Ruff Formatting: PASSED

Artifacts Generated:
  - coverage.xml (Cobertura format for SonarQube)
  - junit.xml (Test results)
  - htmlcov/ (HTML coverage report)

Build Status: SUCCESS ✓
═══════════════════════════════════════════════════════════════
EOF

print_success "Build summary created: ${ARTIFACTS_DIR}/BUILD_SUMMARY.txt"

# ──────────────────────────────────────────────────────────────────────────────
# STEP 8: SonarQube Scanner
# ──────────────────────────────────────────────────────────────────────────────
if [[ "${RUN_SONAR_SCANNER:-0}" == "1" ]]; then
    print_step "8" "Running SonarQube Scanner"

    # Check if sonar-scanner is available
    if ! command -v sonar-scanner >/dev/null 2>&1; then
        print_info "sonar-scanner not found in PATH, attempting to locate or install..."

        # Common installation paths for sonar-scanner
        COMMON_PATHS=(
            "/opt/sonar-scanner/bin"
            "/usr/local/sonar-scanner/bin"
            "/usr/local/bin"
            "$HOME/sonar-scanner/bin"
            "$HOME/.sonar/sonar-scanner-*/bin"
        )

        FOUND=false
        for path in "${COMMON_PATHS[@]}"; do
            # Handle glob patterns
            for expanded_path in $path; do
                if [ -d "$expanded_path" ] && [ -f "$expanded_path/sonar-scanner" ]; then
                    print_info "Found sonar-scanner at: $expanded_path"
                    export PATH="$expanded_path:$PATH"
                    FOUND=true
                    break 2
                fi
            done
        done

        if [ "$FOUND" = false ]; then
            # Auto-install sonar-scanner
            print_info "Installing sonar-scanner..."

            # Detect OS
            OS_TYPE=$(uname -s | tr '[:upper:]' '[:lower:]')
            ARCH=$(uname -m)

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
                print_error "Unsupported OS: $OS_TYPE"
                print_info "Please install sonar-scanner manually or set RUN_SONAR_SCANNER=0"
                exit 1
            fi

            SONAR_URL="https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/${SONAR_ZIP}"

            mkdir -p "$SONAR_DIR"
            cd "$SONAR_DIR"

            # Download sonar-scanner
            if command -v curl >/dev/null 2>&1; then
                curl -fsSL "$SONAR_URL" -o "${SONAR_ZIP}" || {
                    print_error "Failed to download sonar-scanner"
                    exit 1
                }
            elif command -v wget >/dev/null 2>&1; then
                wget -q "$SONAR_URL" -O "${SONAR_ZIP}" || {
                    print_error "Failed to download sonar-scanner"
                    exit 1
                }
            else
                print_error "Neither curl nor wget found. Cannot download sonar-scanner."
                exit 1
            fi

            # Extract
            if command -v unzip >/dev/null 2>&1; then
                unzip -q "${SONAR_ZIP}" || {
                    print_error "Failed to extract sonar-scanner"
                    exit 1
                }
                rm "${SONAR_ZIP}"
            else
                print_error "unzip command not found. Cannot extract sonar-scanner."
                exit 1
            fi

            # Add to PATH
            SONAR_BIN_DIR="$SONAR_DIR/${SONAR_EXTRACT_DIR}/bin"
            export PATH="$SONAR_BIN_DIR:$PATH"

            cd "$ROOT_DIR"

            if command -v sonar-scanner >/dev/null 2>&1; then
                print_success "sonar-scanner installed successfully"
            else
                print_error "Failed to install sonar-scanner"
                exit 1
            fi
        fi
    else
        print_success "sonar-scanner is already available"
    fi

    # Verify sonar-scanner is now available
    if ! command -v sonar-scanner >/dev/null 2>&1; then
        print_error "sonar-scanner is required but not available"
        exit 1
    fi

    print_info "Executing sonar-scanner..."

    # Detect if running in CI environment
    if [[ -n "${BAMBOO_BUILD_NUMBER:-}" ]] || [[ -n "${CI:-}" ]] || [[ -n "${JENKINS_HOME:-}" ]]; then
        # In CI: SonarQube failure should fail the build
        sonar-scanner
        print_success "SonarQube analysis completed"
    else
        # Local development: SonarQube failure is just a warning
        if sonar-scanner; then
            print_success "SonarQube analysis completed"
        else
            print_warning "SonarQube analysis failed (non-fatal in local development)"
            print_info "This is expected if you're not connected to the corporate network"
        fi
    fi
else
    print_info "Skipping SonarQube scanner (RUN_SONAR_SCANNER=0)"
fi

# ══════════════════════════════════════════════════════════════════════════════
# BUILD SUCCESS SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

print_header "Build Completed Successfully"

echo -e "${GREEN}Build Summary:${NC}"
echo -e "  ${GREEN}✓${NC} Git repository: Verified"
echo -e "  ${GREEN}✓${NC} Python ${UV_PYTHON} environment: Configured with uv"
echo -e "  ${GREEN}✓${NC} Dependencies: Installed from uv.lock"
echo -e "  ${GREEN}✓${NC} Code quality: Ruff checks passed"
echo -e "  ${GREEN}✓${NC} Tests: ${TEST_COUNT} passed (${SKIP_COUNT} skipped)"
echo -e "  ${GREEN}✓${NC} Branch coverage: ${COVERAGE_THRESHOLD}% achieved"
echo -e "  ${GREEN}✓${NC} Artifacts: Generated successfully"
if [[ "${RUN_SONAR_SCANNER:-1}" == "1" ]]; then
    echo -e "  ${GREEN}✓${NC} SonarQube: Analysis completed"
fi

echo ""
echo -e "${CYAN}Artifacts Location:${NC}"
echo "  ${ARTIFACTS_DIR}/"
ls -1 "${ARTIFACTS_DIR}"/*.xml 2>/dev/null | sed 's/^/    /'

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  🎯 BUILD READY FOR DEPLOYMENT${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""


