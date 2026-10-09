from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("job", ["ruff", "test"])
def test_lint_workflow_uses_project_dependencies(job: str) -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github" / "workflows" / "lint.yml").read_text())
    steps = workflow["jobs"][job]["steps"]
    setup = next(step for step in steps if step.get("uses", "").startswith("actions/setup-python@"))
    install = next(step for step in steps if "pip install" in step.get("run", ""))

    assert setup["with"]["cache-dependency-path"] == "pyproject.toml"
    assert (root / setup["with"]["cache-dependency-path"]).is_file()
    assert install["run"] == 'python3 -m pip install -e ".[dev]"'
