import os
import yaml
import pytest

def test_github_actions_workflow_syntax_and_security():
    workflow_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.github/workflows/deploy.yml"))
    assert os.path.exists(workflow_path), f"CD workflow file does not exist at {workflow_path}"
    
    with open(workflow_path, "r") as f:
        workflow = yaml.safe_load(f)
        
    assert workflow is not None
    assert workflow["name"] == "Cloud Run CD"
    
    # Verify trigger
    triggers = workflow.get("on") or workflow.get(True) or {}
    assert "push" in triggers
    assert "main" in triggers["push"].get("branches", [])
    
    # Verify permissions for Workload Identity Federation
    jobs = workflow.get("jobs", {})
    assert "deploy" in jobs
    permissions = jobs["deploy"].get("permissions", {})
    assert permissions.get("id-token") == "write", "id-token: write permission is required for WIF authentication!"
    assert permissions.get("contents") == "read"
    
    # Verify key CD steps are present
    steps = jobs["deploy"].get("steps", [])
    assert len(steps) > 0
    
    uses_clauses = [step.get("uses") for step in steps if "uses" in step]
    assert any(u.startswith("actions/checkout") for u in uses_clauses)
    assert any(u.startswith("google-github-actions/auth") for u in uses_clauses), "google-github-actions/auth is required for WIF!"

    # Deploy is performed via the gcloud CLI (mirrors the proven deploy.sh): setup-gcloud
    # provides the CLI and a run step issues `gcloud run services update` with the full
    # production env vars, Secret Manager bindings, and Cloud SQL instance.
    assert any(u.startswith("google-github-actions/setup-gcloud") for u in uses_clauses), \
        "google-github-actions/setup-gcloud is required to provide the gcloud CLI!"
    run_blocks = "\n".join(step.get("run", "") for step in steps if "run" in step)
    assert "gcloud run services update" in run_blocks or "gcloud run deploy" in run_blocks, \
        "A step must deploy to Cloud Run via `gcloud run services update` (or `gcloud run deploy`)!"
    assert "gcloud secrets versions access" not in run_blocks, \
        "The deploy workflow must not read production secret payloads directly on the GitHub runner!"
    assert 'gcloud run jobs deploy "${MIGRATION_JOB}"' in run_blocks, \
        "Database migrations must deploy a one-off Cloud Run job!"
    assert 'gcloud run jobs execute "${MIGRATION_JOB}"' in run_blocks, \
        "Database migrations must execute the same one-off Cloud Run job!"
    assert run_blocks.index('gcloud run jobs deploy "${MIGRATION_JOB}"') < run_blocks.index('gcloud run jobs execute "${MIGRATION_JOB}"'), \
        "Database migrations must run inside a one-off Cloud Run job that uses the service's secret bindings."


def test_github_actions_ci_workflow():
    workflow_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.github/workflows/ci.yml"))
    assert os.path.exists(workflow_path), f"CI workflow file does not exist at {workflow_path}"
    
    with open(workflow_path, "r") as f:
        workflow = yaml.safe_load(f)
        
    assert workflow is not None
    assert workflow["name"] == "ci"
    
    jobs = workflow.get("jobs", {})
    assert "tests" in jobs
    steps = jobs["tests"].get("steps", [])
    
    # Check that it runs pytest with coverage and NOT in quiet mode (-q)
    pytest_step = None
    for step in steps:
        if step.get("name") == "Kernel test suite":
            pytest_step = step
            break
            
    assert pytest_step is not None, "Kernel test suite step must be present!"
    run_cmd = pytest_step.get("run", "")
    assert "pytest" in run_cmd
    assert "-q" not in run_cmd, "The quiet flag (-q) must be removed to output the full coverage table in CI logs!"


def test_no_committed_db_files():
    import subprocess
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    try:
        output = subprocess.check_output(["git", "ls-files"], cwd=repo_root).decode("utf-8")
        files = output.splitlines()
        db_files = [f for f in files if f.endswith(".db") or f.endswith(".sqlite")]
        assert len(db_files) == 0, f"Found committed database files: {db_files}"
    except subprocess.CalledProcessError:
        # Pass if git CLI is not functional (e.g. running outside git checkout)
        pass

