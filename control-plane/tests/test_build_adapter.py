import pytest
import os
import tempfile
import shutil
from app.adapters.build import BuildAdapter
from app.kernel.optypes import OpSpec, Severity, Reversibility, Money
from app.adapters.build_agent import BuildAgentHarness

@pytest.fixture
def adapter():
    return BuildAdapter()

@pytest.fixture
def build_op(temp_git_remote):
    return OpSpec(
        id="op_build_123",
        tenant_id="t1",
        brand_id="b1",
        domain="build",
        action="build.deliver",
        params={
            "intent": "change hero color to blue",
            "branch_name": "aos-build-test",
            "repo": temp_git_remote
        },
        severity=Severity(impact=2, reversibility=Reversibility.REVERSIBLE),
        cost_estimate=Money(amount_minor=1000, currency="INR"),
    )

def test_build_adapter_plan(adapter):
    ops = adapter.plan("change hero color to blue", "t1", "b1")
    assert len(ops) == 1
    op = ops[0]
    assert op.action == "build.deliver"
    assert "intent" in op.params
    assert op.params["intent"] == "change hero color to blue"
    assert "branch_name" in op.params
    assert op.params["repo"] is None
    assert op.cost_estimate.amount_minor == 100000  # Default is ₹1000.00 (100000 paise)

def test_build_adapter_plan_with_capability(adapter):
    # Match email_template_builder (200000 paise)
    ops = adapter.plan("create welcome email series for ecommerce", "t1", "b1")
    assert len(ops) == 1
    op = ops[0]
    assert op.action == "build.deliver"
    assert op.params["capability"] == "email_template_builder"
    assert op.cost_estimate.amount_minor == 200000  # ₹2000.00
    
    # Match seo_auditor_fixer (100000 paise)
    ops2 = adapter.plan("optimize for seo", "t1", "b1")
    assert len(ops2) == 1
    assert ops2[0].params["capability"] == "seo_auditor_fixer"
    assert ops2[0].cost_estimate.amount_minor == 100000  # ₹1000.00

def test_build_adapter_preview(adapter, build_op):
    preview_art = adapter.preview(build_op)
    assert preview_art.kind == "build_preview"
    assert "Staging Preview" in preview_art.summary
    assert "diff" in preview_art.detail
    assert preview_art.detail["branch"] == "aos-build-test"


def test_build_adapter_preview_smoke_failure(adapter, temp_git_remote):
    fail_op = OpSpec(
        id="op_build_fail",
        tenant_id="t1",
        brand_id="b1",
        domain="build",
        action="build.deliver",
        params={
            "intent": "change hero color to blue",
            "branch_name": "aos-build-fail-smoke",
            "repo": temp_git_remote
        },
        severity=Severity(impact=2, reversibility=Reversibility.REVERSIBLE),
        cost_estimate=Money(amount_minor=1000, currency="INR"),
    )
    preview_art = adapter.preview(fail_op)
    assert preview_art.kind == "build_error"
    assert "Staging smoke tests failed" in preview_art.summary


async def test_build_adapter_execute(adapter, build_op, temp_git_remote, run_git):
    branch_name = build_op.params["branch_name"]
    # 1. Prepare: Run harness to create and push the branch
    with BuildAgentHarness(repo_url=temp_git_remote, branch_name=branch_name) as harness:
        assert harness.clone_and_checkout() is True
        assert harness.apply_edits("some intent") is True
        assert harness.commit_and_push() is True
        
    # 2. Act: Execute merge
    res = await adapter.execute(build_op, "idem_build_123")
    
    # 3. Assert
    assert res.ok is True
    assert "prod_url" in res.detail
    
    # Verify it was merged on remote
    temp_dir = tempfile.mkdtemp()
    try:
        run_git(["clone", temp_git_remote, temp_dir], check=True, capture_output=True)
        with open(os.path.join(temp_dir, "src/App.js"), "r") as f:
            content = f.read()
        assert "color=\"blue\"" in content
    finally:
        shutil.rmtree(temp_dir)

@pytest.mark.asyncio
async def test_build_adapter_verify(adapter, build_op):
    res = await adapter.verify(build_op)
    assert res.ok is True
    assert res.checks["http_ok"] is True
    assert res.checks["version_match"] is True

def test_build_adapter_compensate(adapter, build_op):
    compensations = adapter.compensate(build_op)
    assert len(compensations) == 1
    comp = compensations[0]
    assert comp.action == "build.rollback"
    assert comp.parent_op_id == build_op.id
    assert comp.params["revert_branch"] == build_op.params["branch_name"]
    assert comp.params["repo"] == build_op.params["repo"]

async def test_build_adapter_rollback_execution(adapter, build_op, temp_git_remote, run_git):
    branch_name = build_op.params["branch_name"]
    # 1. Prepare: Run harness to create, push branch, and merge it
    with BuildAgentHarness(repo_url=temp_git_remote, branch_name=branch_name) as harness:
        assert harness.clone_and_checkout() is True
        assert harness.apply_edits("some intent") is True
        assert harness.commit_and_push() is True
        
    res_deliver = await adapter.execute(build_op, "idem_deliver_123")
    assert res_deliver.ok is True
    
    # Verify it was merged
    temp_dir = tempfile.mkdtemp()
    try:
        run_git(["clone", temp_git_remote, temp_dir], check=True, capture_output=True)
        with open(os.path.join(temp_dir, "src/App.js"), "r") as f:
            assert "color=\"blue\"" in f.read()
    finally:
        shutil.rmtree(temp_dir)
        
    # 2. Act: Get compensation Op and execute it
    compensations = adapter.compensate(build_op)
    rollback_op = compensations[0]
    
    res_rollback = await adapter.execute(rollback_op, "idem_rollback_123")
    assert res_rollback.ok is True
    
    # 3. Assert: Verify it was reverted on remote (color should be back to red)
    temp_dir = tempfile.mkdtemp()
    try:
        run_git(["clone", temp_git_remote, temp_dir], check=True, capture_output=True)
        with open(os.path.join(temp_dir, "src/App.js"), "r") as f:
            content = f.read()
        assert "color=\"red\"" in content
        assert "color=\"blue\"" not in content
    finally:
        shutil.rmtree(temp_dir)

def test_build_adapter_load_profile(adapter):
    # Test valid profile loading and frontmatter stripping
    profile_content = adapter._load_profile("email_template_builder")
    assert profile_content is not None
    assert "Frontend Developer" in profile_content
    assert "---" not in profile_content[:10]
    
    # Test invalid profile
    assert adapter._load_profile("non_existent_capability") is None
    assert adapter._load_profile(None) is None

from unittest.mock import patch

def test_build_adapter_preview_security_passed(adapter, build_op):
    preview_art = adapter.preview(build_op)
    assert preview_art.kind == "build_preview"
    assert "✅ AppSec Review: PASSED" in preview_art.summary
    assert "security_review" in build_op.params
    assert build_op.params["security_review"]["passed"] is True
    assert build_op.params["security_review"]["risk_score"] == 1

def test_build_adapter_preview_security_failed_credentials(adapter, temp_git_remote):
    with patch("app.services.llm.VertexAIClient.generate_edits") as mock_gen:
        mock_gen.return_value = {
            "explanation": "Adding credentials",
            "edits": [
                {
                    "path": "src/App.js",
                    "action": "modify",
                    "content": "const AWS_SECRET_ACCESS_KEY = 'vulnerable_secret';"
                }
            ]
        }
        
        fail_op = OpSpec(
            id="op_build_sec_fail",
            tenant_id="t1",
            brand_id="b1",
            domain="build",
            action="build.deliver",
            params={
                "intent": "leak keys",
                "branch_name": "aos-build-sec-fail",
                "repo": temp_git_remote
            },
            severity=Severity(impact=2, reversibility=Reversibility.REVERSIBLE),
            cost_estimate=Money(amount_minor=1000, currency="INR"),
        )
        
        preview_art = adapter.preview(fail_op)
        assert preview_art.kind == "build_preview"
        assert "⚠️ AppSec Review: FAILED" in preview_art.summary
        assert "Found hardcoded AWS_SECRET_ACCESS_KEY" in preview_art.summary
        assert fail_op.params["security_review"]["passed"] is False
        assert fail_op.params["security_review"]["risk_score"] == 5

def test_build_adapter_preview_security_failed_eval(adapter, temp_git_remote):
    with patch("app.services.llm.VertexAIClient.generate_edits") as mock_gen:
        mock_gen.return_value = {
            "explanation": "Using eval",
            "edits": [
                {
                    "path": "src/App.js",
                    "action": "modify",
                    "content": "eval(untrusted_code);"
                }
            ]
        }
        
        fail_op = OpSpec(
            id="op_build_sec_eval",
            tenant_id="t1",
            brand_id="b1",
            domain="build",
            action="build.deliver",
            params={
                "intent": "use eval",
                "branch_name": "aos-build-sec-eval",
                "repo": temp_git_remote
            },
            severity=Severity(impact=2, reversibility=Reversibility.REVERSIBLE),
            cost_estimate=Money(amount_minor=1000, currency="INR"),
        )
        
        preview_art = adapter.preview(fail_op)
        assert "⚠️ AppSec Review: FAILED" in preview_art.summary
        assert "Found eval() usage in javascript" in preview_art.summary
        assert fail_op.params["security_review"]["passed"] is False
        assert fail_op.params["security_review"]["risk_score"] == 4

def test_build_adapter_preview_design_passed(adapter, build_op):
    preview_art = adapter.preview(build_op)
    assert preview_art.kind == "build_preview"
    assert "✅ Brand Guardian Review: PASSED" in preview_art.summary
    assert "design_review" in build_op.params
    assert build_op.params["design_review"]["passed"] is True
    assert build_op.params["design_review"]["score"] == 5

def test_build_adapter_preview_design_failed_color(adapter, temp_git_remote):
    with patch("app.services.llm.VertexAIClient.generate_edits") as mock_gen:
        mock_gen.return_value = {
            "explanation": "Changing button to pink",
            "edits": [
                {
                    "path": "src/App.js",
                    "action": "modify",
                    "content": "const Button = () => <button color='pink'>Click</button>;"
                }
            ]
        }
        
        fail_op = OpSpec(
            id="op_build_design_fail",
            tenant_id="t1",
            brand_id="b1",
            domain="build",
            action="build.deliver",
            params={
                "intent": "make it pink",
                "branch_name": "aos-build-design-fail",
                "repo": temp_git_remote
            },
            severity=Severity(impact=2, reversibility=Reversibility.REVERSIBLE),
            cost_estimate=Money(amount_minor=1000, currency="INR"),
        )
        
        preview_art = adapter.preview(fail_op)
        assert preview_art.kind == "build_preview"
        assert "⚠️ Brand Guardian Review: FAILED" in preview_art.summary
        assert "Brand colors only permit primary blue" in preview_art.summary
        assert fail_op.params["design_review"]["passed"] is False
        assert fail_op.params["design_review"]["score"] == 2

def test_build_adapter_preview_design_non_visual_skipped(adapter, temp_git_remote):
    with patch("app.services.llm.VertexAIClient.generate_edits") as mock_gen:
        mock_gen.return_value = {
            "explanation": "Update backend service",
            "edits": [
                {
                    "path": "backend/main.py",
                    "action": "modify",
                    "content": "def run(): print('hello')"
                }
            ]
        }
        
        py_op = OpSpec(
            id="op_build_non_visual",
            tenant_id="t1",
            brand_id="b1",
            domain="build",
            action="build.deliver",
            params={
                "intent": "edit python backend",
                "branch_name": "aos-build-py",
                "repo": temp_git_remote
            },
            severity=Severity(impact=2, reversibility=Reversibility.REVERSIBLE),
            cost_estimate=Money(amount_minor=1000, currency="INR"),
        )
        
        preview_art = adapter.preview(py_op)
        assert preview_art.kind == "build_preview"
        assert "Brand Guardian Review" not in preview_art.summary
        assert "design_review" not in py_op.params

def test_build_adapter_preview_quality_gates_passed(adapter, build_op, mock_urlopen_globally):
    mock_urlopen_globally.return_value.read.return_value = b"<html><div>Normal Page</div></html>"
    
    preview_art = adapter.preview(build_op)
    assert preview_art.kind == "build_preview"
    assert "✅ Accessibility Review: PASSED" in preview_art.summary
    assert "✅ Performance Review: PASSED" in preview_art.summary
    assert "accessibility_review" in build_op.params
    assert "performance_review" in build_op.params
    assert build_op.params["accessibility_review"]["passed"] is True
    assert build_op.params["performance_review"]["passed"] is True

def test_build_adapter_preview_accessibility_failed(adapter, build_op, mock_urlopen_globally):
    mock_urlopen_globally.return_value.read.return_value = b"<html><div class='fail-a11y'>Violating markup</div></html>"
    
    preview_art = adapter.preview(build_op)
    assert preview_art.kind == "build_preview"
    assert "♿ Accessibility Review: FAILED" in preview_art.summary
    assert "Image missing alt text (WCAG 1.1.1)" in preview_art.summary
    assert build_op.params["accessibility_review"]["passed"] is False
    assert build_op.params["accessibility_review"]["score_percent"] == 60

def test_build_adapter_preview_performance_failed(adapter, build_op, mock_urlopen_globally):
    mock_urlopen_globally.return_value.read.return_value = b"<html><div class='fail-perf'>Heavy scripts</div></html>"
    
    preview_art = adapter.preview(build_op)
    assert preview_art.kind == "build_preview"
    assert "⏱️ Performance Review: FAILED" in preview_art.summary
    assert "Render-blocking scripts in head" in preview_art.summary
    assert build_op.params["performance_review"]["passed"] is False
    assert build_op.params["performance_review"]["score_percent"] == 55

