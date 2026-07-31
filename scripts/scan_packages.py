#!/usr/bin/env python3
"""Scan project dependencies to generate a comprehensive LICENSES.md file."""

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent

BACKEND_JSON = SCRIPT_DIR / ".backend.json"
FRONTEND_JSON = SCRIPT_DIR / ".frontend.json"
OUTPUT_PATH = PROJECT_ROOT / "LICENSES.md"

FRONTEND_PACKAGE = PROJECT_ROOT / "frontend" / "package.json"
BACKEND_PYPROJECT = PROJECT_ROOT / "backend" / "pyproject.toml"


@dataclass
class Package:
    name: str
    version: str | None = None
    repository: str | None = None
    description: str | None = None
    license: str | None = None


# ---------------------------------------------------------------------------
# Run external tools
# ---------------------------------------------------------------------------


def run_license_checker():
    """Execute license-checker via pnpm for frontend dependencies.

    https://github.com/davglass/license-checker
    """
    print("🔍 Running license-checker for frontend dependencies...")
    command = f"pnpm dlx license-checker --direct --json --customPath ../scripts/.format.json --out {FRONTEND_JSON}"
    subprocess.run(command, shell=True, cwd=PROJECT_ROOT / "frontend", check=True)
    print("✅ Frontend license data generated successfully")


def run_pip_licenses():
    """Execute pip-licenses via poetry for backend dependencies.

    https://github.com/raimon49/pip-licenses
    """
    print("🔍 Running pip-licenses for backend dependencies...")
    command = f"poetry run pip-licenses --with-urls --with-description --format=json --output-file {BACKEND_JSON}"
    subprocess.run(command, shell=True, cwd=PROJECT_ROOT / "backend", check=True)
    print("✅ Backend license data generated successfully")


# ---------------------------------------------------------------------------
# Parse JSON outputs
# ---------------------------------------------------------------------------


def parse_frontend_json() -> list[Package]:
    """Parse frontend dependencies JSON file."""
    print("📖 Parsing frontend license data...")
    data: dict[str, dict] = json.loads(FRONTEND_JSON.read_text("utf-8"))
    packages: dict[str, Package] = {}
    for pkg in data.values():
        name = pkg["name"]
        packages[name] = Package(
            name=name,
            version=pkg.get("version"),
            repository=pkg.get("repository"),
            description=pkg.get("description"),
            license=pkg.get("licenses"),
        )
    print(f"✅ Found {len(packages)} frontend packages")
    return list(packages.values())


def parse_backend_json() -> list[Package]:
    """Parse backend dependencies JSON file."""
    print("📖 Parsing backend license data...")
    data: list[dict] = json.loads(BACKEND_JSON.read_text("utf-8"))
    packages: dict[str, Package] = {}
    for pkg in data:
        name = pkg["Name"]
        url = pkg.get("URL")
        desc = pkg.get("Description")
        packages[name] = Package(
            name=name,
            version=pkg.get("Version"),
            repository=url if url and url != "UNKNOWN" else None,
            description=desc if desc and desc != "UNKNOWN" else None,
            license=pkg.get("License"),
        )
    print(f"✅ Found {len(packages)} backend packages")
    return list(packages.values())


# ---------------------------------------------------------------------------
# Generate LICENSES.md
# ---------------------------------------------------------------------------

EXCLUDE_NAMES = {"kaloscope-frontend", "kaloscope-backend"}


def generate_table(packages: list[Package], title: str) -> list[str]:
    """Generate a Markdown table for dependency data."""
    pkgs = [p for p in packages if p.name not in EXCLUDE_NAMES]
    rows: list[str] = [
        f"## {title}\n",
        f"> **{len(pkgs)}** packages included\n",
        "| Package | Version | License | Description |",
        "|---------|---------|---------|-------------|",
    ]
    for pkg in pkgs:
        repo = pkg.repository.removeprefix("git+") if pkg.repository else None
        name = f"[{pkg.name}]({repo})" if repo else pkg.name
        version = pkg.version or "-"
        license = pkg.license or "Unknown"
        # escape pipes in description to avoid breaking table structure
        description = (pkg.description or "-").replace("|", "\\|")
        rows.append(f"| {name} | {version} | {license} | {description} |")
    rows.append("")
    return rows


def generate_licenses(frontend_data: list[Package], backend_data: list[Package]):
    """Generate the complete LICENSES.md document."""
    now = datetime.now(timezone.utc).strftime("%B %d, %Y, %I:%M %p %Z")
    lines: list[str] = [
        "# Third-Party License Notices\n",
        f"> This document was automatically generated on {now}\n",
        *generate_table(frontend_data, "Frontend Dependencies"),
        *generate_table(backend_data, "Backend Dependencies"),
    ]
    print("📝 Writing markdown file...")
    OUTPUT_PATH.write_text("\n".join(lines), "utf-8")
    print(f"✅ Markdown file generated: {OUTPUT_PATH}")


# ---------------------------------------------------------------------------
# Update README badges
# ---------------------------------------------------------------------------


def get_xyflow_version() -> str:
    """Get xyflow version from frontend package.json."""
    data = json.loads(FRONTEND_PACKAGE.read_text("utf-8"))
    version = (data.get("dependencies") or {}).get("@xyflow/svelte")
    if not version:
        raise RuntimeError("xyflow version not found in package.json")
    return version.lstrip("^~")


def get_svelte_version() -> str:
    """Get Svelte version from frontend package.json."""
    data = json.loads(FRONTEND_PACKAGE.read_text("utf-8"))
    version = (data.get("devDependencies") or {}).get("svelte")
    if not version:
        raise RuntimeError("Svelte version not found in package.json")
    return version.lstrip("^~")


def get_sanic_version() -> str:
    """Get Sanic version from backend pyproject.toml."""
    content = BACKEND_PYPROJECT.read_text("utf-8")
    match = re.search(r"sanic[^(]*\(>=(\d+\.\d+(?:\.\d+)?)", content)
    if not match:
        raise RuntimeError("Sanic version not found in pyproject.toml")
    return match.group(1)


def update_readme():
    """Update version badges in all README*.md files at the project root."""
    xyflow_version = get_xyflow_version()
    print(f"📋 xyflow version: {xyflow_version}")

    svelte_version = get_svelte_version()
    print(f"📋 Svelte version: {svelte_version}")

    sanic_version = get_sanic_version()
    print(f"📋 Sanic version: {sanic_version}")

    readme_files = sorted(PROJECT_ROOT.glob("README*.md"))
    print(
        f"📖 Found {len(readme_files)} README files: {[f.name for f in readme_files]}"
    )

    for readme_file in readme_files:
        content = readme_file.read_text("utf-8")
        content = re.sub(r"xyflow-v[\d.]+", f"xyflow-v{xyflow_version}", content)
        content = re.sub(r"Svelte-v[\d.]+", f"Svelte-v{svelte_version}", content)
        content = re.sub(r"Sanic-v[\d.]+", f"Sanic-v{sanic_version}", content)
        readme_file.write_text(content, "utf-8")
        print(f"✅ Updated {readme_file.name}")

    print("🎉 All README files updated successfully")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 1. run license-checker
    run_license_checker()
    # 2. run pip-licenses
    run_pip_licenses()
    # 3. parse and generate LICENSES.md
    generate_licenses(parse_frontend_json(), parse_backend_json())
    # 4. update README files
    update_readme()
