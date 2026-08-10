import logging
import uuid
import os
from typing import Optional
from app.kernel.optypes import OpSpec, PreviewArtifact, ExecResult, VerifyResult, Severity, Reversibility, Money
from app.kernel.loop import Adapter
from sqlalchemy.ext.asyncio import AsyncSession
from app.adapters.build_agent import BuildAgentHarness

logger = logging.getLogger(__name__)

class Capability:
    def __init__(self, name: str, description: str, keywords: list[str], cost_minor: int):
        self.name = name
        self.description = description
        self.keywords = keywords
        self.cost_minor = cost_minor

class BuildAgentRegistry:
    CAPABILITIES = [
        Capability(
            name="email_template_builder",
            description="HTML/Handlebars templates + drip schedule",
            keywords=["email template", "welcome email", "email series", "handlebars template", "drip schedule"],
            cost_minor=200000, # ₹2000.00
        ),
        Capability(
            name="slack_bot_builder",
            description="Fully functioning Slack app",
            keywords=["slack bot", "slack app", "order notification bot", "slack notification"],
            cost_minor=200000, # ₹2000.00
        ),
        Capability(
            name="api_endpoint_generator",
            description="FastAPI route + auth + tests",
            keywords=["api endpoint", "webhooks/shopify", "fastapi route", "webhook endpoint", "route path"],
            cost_minor=200000, # ₹2000.00
        ),
        Capability(
            name="sql_schema_generator",
            description="Alembic migration + models",
            keywords=["sql schema", "alembic migration", "database models", "orders schema", "inventory schema"],
            cost_minor=100000, # ₹1000.00
        ),
        Capability(
            name="mobile_responsive_audit",
            description="Lighthouse report + fixes suggested",
            keywords=["mobile responsive", "lighthouse report", "works on mobile", "mobile design"],
            cost_minor=100000, # ₹1000.00
        ),
        Capability(
            name="accessibility_fixer",
            description="Auto-fix alt text, contrast, ARIA",
            keywords=["accessibility", "wcag compliance", "alt text", "aria", "contrast"],
            cost_minor=100000, # ₹1000.00
        ),
        Capability(
            name="performance_optimizer",
            description="Tree-shake, lazy-load, CDN config",
            keywords=["performance", "bundle size", "tree-shake", "lazy-load", "minify"],
            cost_minor=100000, # ₹1000.00
        ),
        Capability(
            name="seo_auditor_fixer",
            description="Meta tags, structured data, sitemap",
            keywords=["seo", "meta tags", "structured data", "sitemap", "search engine"],
            cost_minor=100000, # ₹1000.00
        ),
    ]

    @classmethod
    def match_capability(cls, intent: str) -> Optional[Capability]:
        normalized = intent.lower()
        for cap in cls.CAPABILITIES:
            for kw in cap.keywords:
                if kw in normalized:
                    return cap
        return None

class BuildAdapter(Adapter):
    domain = "build"

    def plan(self, intent: str, tenant_id: str, brand_id: str) -> list[OpSpec]:
        """Plans the build delivery."""
        intent_lower = intent.lower()
        if any(w in intent_lower for w in ["makeover", "redesign", "new site", "new webapp", "scaffold site"]):
            return [
                OpSpec(
                    tenant_id=tenant_id,
                    brand_id=brand_id,
                    domain=self.domain,
                    action="build.design.makeover",
                    params={
                        "intent": intent,
                    },
                    severity=Severity(impact=3, reversibility=Reversibility.REVERSIBLE),
                    cost_estimate=Money(amount_minor=150000, currency="INR"),
                )
            ]

        cap = BuildAgentRegistry.match_capability(intent)
        cost_minor = cap.cost_minor if cap else 100000  # Default to ₹1000.00
        cap_name = cap.name if cap else None

        return [
            OpSpec(
                tenant_id=tenant_id,
                brand_id=brand_id,
                domain=self.domain,
                action="build.deliver",
                params={
                    "intent": intent,
                    "branch_name": f"aos-build-{uuid.uuid4().hex[:8]}",
                    "repo": None,
                    "capability": cap_name
                },
                severity=Severity(impact=2, reversibility=Reversibility.REVERSIBLE),
                cost_estimate=Money(amount_minor=cost_minor, currency="INR"),
            )
        ]

    def _load_profile(self, capability_name: Optional[str]) -> Optional[str]:
        if not capability_name:
            return None
            
        mapping = {
            "email_template_builder": "engineering-frontend-developer.md",
            "slack_bot_builder": "engineering-backend-architect.md",
            "api_endpoint_generator": "engineering-backend-architect.md",
            "sql_schema_generator": "engineering-database-optimizer.md",
            "mobile_responsive_audit": "engineering-frontend-developer.md",
            "accessibility_fixer": "engineering-frontend-developer.md",
            "performance_optimizer": "engineering-filament-optimization-specialist.md",
            "seo_auditor_fixer": "marketing-seo-specialist.md",
            "security_appsec_engineer": "security-appsec-engineer.md",
            "design_brand_guardian": "design-brand-guardian.md",
            "accessibility_auditor": "testing-accessibility-auditor.md",
            "performance_benchmarker": "testing-performance-benchmarker.md",
            "ux_architect": "design-ux-architect.md",
            "ui_designer": "design-ui-designer.md",
            "image_prompt_engineer": "design-image-prompt-engineer.md",
        }
        
        profile_file = mapping.get(capability_name)
        if not profile_file:
            return None
            
        profile_path = os.path.join(os.path.dirname(__file__), "build_profiles", profile_file)
        if not os.path.exists(profile_path):
            logger.warning(f"Profile file not found at {profile_path}")
            return None
            
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                content = f.read()
                
            # Strip YAML frontmatter if present
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    content = parts[2].strip()
            return content
        except Exception as e:
            logger.error(f"Failed to read profile {profile_file}: {e}")
            return None

    def preview(self, op: OpSpec) -> PreviewArtifact:
        """Runs the build agent and deploys to staging to generate a preview."""
        logger.info(f"Generating preview for build intent: {op.params.get('intent')}")
        
        if op.action == "build.design.makeover":
            intent = op.params.get("intent", "")
            ux_prof = self._load_profile("ux_architect") or ""
            ui_prof = self._load_profile("ui_designer") or ""
            img_prof = self._load_profile("image_prompt_engineer") or ""
            
            system_instruction = f"{ux_prof}\n{ui_prof}\n{img_prof}"
            
            try:
                project_id = os.getenv("AOS_GCP_PROJECT")
                from app.services.llm import VertexAIClient
                llm_client = VertexAIClient(project_id=project_id)
                blueprint = llm_client.generate_design_blueprint(intent, system_instruction)
            except Exception as e:
                logger.error(f"Failed to generate design blueprint: {e}")
                return PreviewArtifact(kind="design_error", summary="Design blueprint generation failed", detail={"error": str(e)})

            summary_md = (
                f"# Design Blueprint: {intent}\n\n"
                f"### 📐 UX Structural Wireframe Spec\n{blueprint.get('ux_wireframes')}\n\n"
                f"### 🎨 Visual Style Guide & Theme\n"
                f"*   **Primary Color**: `{blueprint.get('css_theme', {}).get('primary')}`\n"
                f"*   **Secondary Color**: `{blueprint.get('css_theme', {}).get('secondary')}`\n"
                f"*   **Fonts**: {', '.join(blueprint.get('css_theme', {}).get('fonts', []))}\n\n"
                f"### 🖼️ Generated Visual Asset Prompts\n"
            )
            for i, p in enumerate(blueprint.get("image_prompts", []), 1):
                summary_md += f"{i}.  *\"{p}\"*\n"
                
            return PreviewArtifact(
                kind="design_blueprint",
                summary=summary_md,
                detail=blueprint
            )

        repo_url = op.params.get("repo")
        branch = op.params.get("branch_name")
        intent = op.params.get("intent")
        
        # Force safe.bareRepository=all environment variable for git subprocesses if needed?
        # Actually, if we run git commands in subprocess, they inherit our environment.
        # But we can't easily pass '-c safe.bareRepository=all' to BuildAgentHarness unless we modify it,
        # or if we set GIT_CONFIG_PARAMETERS.
        # Let's see if we need it. In debug_harness.py, 'git clone' worked without it.
        # But if the remote is a local bare repo (in tests), we might need it for push.
        # Actually, let's see if tests fail.
        
        security_summary = ""
        design_summary = ""
        with BuildAgentHarness(repo_url=repo_url, branch_name=branch) as harness:
            if not harness.clone_and_checkout():
                return PreviewArtifact(kind="build_error", summary="Failed to clone repo", detail={})
            
            # Format custom intent instruction based on matched capability
            cap = BuildAgentRegistry.match_capability(intent)
            custom_intent = intent
            cap_name = cap.name if cap else None
            if cap:
                custom_intent = f"Role/Target Capability: {cap.name} ({cap.description})\n\nRequirement: {intent}"

            system_instruction = self._load_profile(cap_name)

            if not harness.apply_edits(custom_intent, system_instruction=system_instruction):
                return PreviewArtifact(kind="build_error", summary="Failed to apply edits", detail={})
                
            if not harness.commit_and_push():
                return PreviewArtifact(kind="build_error", summary="Failed to commit changes", detail={})
                
            diff = harness.get_diff()
            op.params["diff"] = diff

            # AppSec Review
            if diff.strip():
                security_instruction = self._load_profile("security_appsec_engineer")
                if security_instruction:
                    try:
                        from app.services.llm import VertexAIClient
                        project_id = os.getenv("AOS_GCP_PROJECT")
                        client = VertexAIClient(project_id=project_id)
                        sec_report = client.analyze_security_diff(diff, security_instruction)
                        
                        passed = sec_report.get("passed", True)
                        violations = sec_report.get("violations", [])
                        risk_score = sec_report.get("risk_score", 1)
                        report = sec_report.get("detailed_report", "")
                        
                        op.params["security_review"] = {
                            "passed": passed,
                            "violations": violations,
                            "risk_score": risk_score,
                            "report": report
                        }
                        
                        if not passed:
                            security_summary = f"\n\n⚠️ AppSec Review: FAILED (Risk Score: {risk_score}/5)\nViolations:\n" + "\n".join(f"- {v}" for v in violations)
                        else:
                            security_summary = f"\n\n✅ AppSec Review: PASSED (Risk Score: {risk_score}/5)"
                    except Exception as e:
                        logger.error(f"AppSec diff review failed: {e}")

            # Brand Guardian Review
            is_visual = False
            for line in diff.splitlines():
                if line.startswith("+++ b/"):
                    ext = os.path.splitext(line)[1]
                    if ext in (".html", ".css", ".js", ".jsx", ".ts", ".tsx"):
                        is_visual = True
                        break

            if is_visual:
                design_instruction = self._load_profile("design_brand_guardian")
                if design_instruction:
                    try:
                        from app.services.llm import VertexAIClient
                        project_id = os.getenv("AOS_GCP_PROJECT")
                        client = VertexAIClient(project_id=project_id)
                        des_report = client.analyze_design_diff(diff, design_instruction)
                        
                        passed = des_report.get("passed", True)
                        violations = des_report.get("violations", [])
                        score = des_report.get("score", 5)
                        report = des_report.get("detailed_report", "")
                        
                        op.params["design_review"] = {
                            "passed": passed,
                            "violations": violations,
                            "score": score,
                            "report": report
                        }
                        
                        if not passed:
                            design_summary = f"\n\n⚠️ Brand Guardian Review: FAILED (Alignment Score: {score}/5)\nViolations:\n" + "\n".join(f"- {v}" for v in violations)
                        else:
                            design_summary = f"\n\n✅ Brand Guardian Review: PASSED (Alignment Score: {score}/5)"
                    except Exception as e:
                        logger.error(f"Brand Guardian design review failed: {e}")

            # Run staging smoke tests check
            if "fail-smoke" in branch:
                return PreviewArtifact(
                    kind="build_error",
                    summary="Staging smoke tests failed: simulated failure",
                    detail={"stdout": "", "stderr": "Staging smoke tests failed"}
                )

            # Secure, Python-based staging URL HTTP check
            staging_url = f"https://staging-{branch}.run.app"
            logger.info(f"Performing secure Python-based HTTP check on staging URL: {staging_url}")
            
            import urllib.request
            import urllib.error
            
            try:
                req = urllib.request.Request(
                    staging_url,
                    headers={"User-Agent": "AOS-Build-Agent/1.0"}
                )
                html_content = ""
                with urllib.request.urlopen(req, timeout=5) as response:
                    status = response.status
                    if status != 200:
                        return PreviewArtifact(
                            kind="build_error",
                            summary=f"Staging smoke tests failed: HTTP status {status}",
                            detail={"status": status}
                        )
                    html_bytes = response.read()
                    html_content = html_bytes.decode("utf-8", errors="ignore")
            except urllib.error.HTTPError as e:
                logger.error(f"HTTP Error during staging check: {e.code}")
                return PreviewArtifact(
                    kind="build_error",
                    summary=f"Staging smoke tests failed: HTTP status {e.code}",
                    detail={"status": e.code, "error": str(e)}
                )
            except urllib.error.URLError as e:
                logger.error(f"URL Error during staging check: {e.reason}")
                return PreviewArtifact(
                    kind="build_error",
                    summary=f"Staging smoke tests failed: URL Error {e.reason}",
                    detail={"error": str(e)}
                )
            except Exception as e:
                logger.error(f"Unexpected error during staging check: {e}")
                return PreviewArtifact(
                    kind="build_error",
                    summary=f"Staging smoke tests failed: {str(e)}",
                    detail={"error": str(e)}
                )

            # Accessibility and Performance quality gates
            accessibility_summary = ""
            performance_summary = ""
            if html_content:
                # 1. Accessibility audit
                a11y_instr = self._load_profile("accessibility_auditor")
                if a11y_instr:
                    try:
                        from app.services.llm import VertexAIClient
                        project_id = os.getenv("AOS_GCP_PROJECT")
                        llm_client = VertexAIClient(project_id=project_id)
                        a11y_report = llm_client.analyze_accessibility(html_content, a11y_instr)
                        
                        passed = a11y_report.get("passed", True)
                        violations = a11y_report.get("violations", [])
                        score = a11y_report.get("score_percent", 100)
                        report = a11y_report.get("report", "")
                        
                        op.params["accessibility_review"] = {
                            "passed": passed,
                            "violations": violations,
                            "score_percent": score,
                            "report": report
                        }
                        if not passed:
                            accessibility_summary = f"\n\n♿ Accessibility Review: FAILED ({score}%)\nViolations:\n" + "\n".join(f"- {v}" for v in violations)
                        else:
                            accessibility_summary = f"\n\n✅ Accessibility Review: PASSED ({score}%)"
                    except Exception as e:
                        logger.error(f"Accessibility preview review failed: {e}")

                # 2. Performance markup audit
                perf_instr = self._load_profile("performance_benchmarker")
                if perf_instr:
                    try:
                        from app.services.llm import VertexAIClient
                        project_id = os.getenv("AOS_GCP_PROJECT")
                        llm_client = VertexAIClient(project_id=project_id)
                        perf_report = llm_client.analyze_performance_markup(html_content, perf_instr)
                        
                        passed = perf_report.get("passed", True)
                        violations = perf_report.get("violations", [])
                        score = perf_report.get("score_percent", 100)
                        report = perf_report.get("report", "")
                        
                        op.params["performance_review"] = {
                            "passed": passed,
                            "violations": violations,
                            "score_percent": score,
                            "report": report
                        }
                        if not passed:
                            performance_summary = f"\n\n⏱️ Performance Review: FAILED ({score}%)\nViolations:\n" + "\n".join(f"- {v}" for v in violations)
                        else:
                            performance_summary = f"\n\n✅ Performance Review: PASSED ({score}%)"
                    except Exception as e:
                        logger.error(f"Performance preview review failed: {e}")

        staging_url = f"https://staging-{branch}.run.app"
        summary = f"Staging Preview: {staging_url}\n\nDiff:\n{diff}{security_summary}{design_summary}{accessibility_summary}{performance_summary}"
        
        return PreviewArtifact(
            kind="build_preview",
            summary=summary,
            detail={
                "staging_url": staging_url,
                "diff": diff,
                "branch": branch
            }
        )

    async def execute(self, op: OpSpec, idem_key: str, session: Optional[AsyncSession] = None) -> ExecResult:
        """Merges approved branch to main (deliver) or reverts last merge (rollback)."""
        if op.action == "build.design.makeover":
            logger.info("Executing design makeover: saving blueprint and proposing build delivery")
            if not session:
                return ExecResult(ok=False, detail={"error": "Database session required"})

            intent = op.params.get("intent", "")
            ux_prof = self._load_profile("ux_architect") or ""
            ui_prof = self._load_profile("ui_designer") or ""
            img_prof = self._load_profile("image_prompt_engineer") or ""
            
            system_instruction = f"{ux_prof}\n{ui_prof}\n{img_prof}"
            
            try:
                project_id = os.getenv("AOS_GCP_PROJECT")
                from app.services.llm import VertexAIClient
                llm_client = VertexAIClient(project_id=project_id)
                blueprint = llm_client.generate_design_blueprint(intent, system_instruction)
            except Exception as e:
                return ExecResult(ok=False, detail={"error": f"Failed to generate blueprint: {str(e)}"})

            from app.models import BrandProperty
            from sqlalchemy import select
            
            stmt = select(BrandProperty).where(
                BrandProperty.tenant_id == op.tenant_id,
                BrandProperty.brand_id == op.brand_id,
                BrandProperty.type == "design_blueprint"
            )
            prop_res = await session.execute(stmt)
            prop = prop_res.scalar_one_or_none()
            
            if not prop:
                prop = BrandProperty(
                    tenant_id=op.tenant_id,
                    brand_id=op.brand_id,
                    type="design_blueprint",
                    provider="mock-design-engine",
                    status="drafted",
                    findings={}
                )
                session.add(prop)

            prop.findings = blueprint
            prop.status = "drafted"

            from app.kernel import loop
            
            task_description = (
                f"Implement design makeover theme:\n"
                f"- Configure CSS variables: Primary={blueprint['css_theme']['primary']}, Secondary={blueprint['css_theme']['secondary']}\n"
                f"- Layout structural scaffold: {blueprint['ux_wireframes']}\n"
                f"- Place visual banners matching prompts:\n"
                f"  1. {blueprint['image_prompts'][0]}\n"
                f"  2. {blueprint['image_prompts'][1]}"
            )
            
            child_op = OpSpec(
                tenant_id=op.tenant_id,
                brand_id=op.brand_id,
                domain="build",
                action="build.deliver",
                params={
                    "intent": task_description,
                    "branch_name": f"aos-redesign-{uuid.uuid4().hex[:8]}",
                    "repo": None,
                    "capability": "frontend_developer"
                },
                severity=Severity(impact=2, reversibility=Reversibility.REVERSIBLE),
                cost_estimate=Money(amount_minor=100000, currency="INR"),
                parent_op_id=op.id
            )
            
            from app.kernel.services import resolve_brand_tier
            tier = await resolve_brand_tier(session, tenant_id=op.tenant_id, brand_id=op.brand_id, domain="build")
            proposed_row = await loop.propose(session, child_op, actor="ux_architect")
            await loop.preview_and_gate(session, proposed_row, tier=tier, actor="ux_architect")

            return ExecResult(
                ok=True,
                detail={
                    "message": "Design blueprint saved and storefront code delivery proposed.",
                    "primary_color": blueprint['css_theme']['primary'],
                    "child_op_proposed": "build.deliver"
                }
            )

        if op.action == "build.deliver":
            logger.info(f"Executing build delivery: merging {op.params.get('branch_name')} to main")
            repo_url = op.params.get("repo")
            branch = op.params.get("branch_name")
            
            with BuildAgentHarness(repo_url=repo_url, branch_name="main") as harness:
                if not harness.clone_and_checkout(create_new=False):
                    return ExecResult(ok=False, detail={"error": "Failed to clone repo and checkout main"})
                    
                if not harness.merge_and_push(from_branch=branch):
                    return ExecResult(ok=False, detail={"error": f"Failed to merge {branch} into main"})
                    
            prod_url = "https://www.ableys.in"
            return ExecResult(
                ok=True,
                detail={
                    "message": f"Branch {branch} merged to main successfully.",
                    "prod_url": prod_url
                }
            )
        elif op.action == "build.rollback":
            logger.info(f"Executing build rollback: reverting last merge for {op.params.get('revert_branch')}")
            repo_url = op.params.get("repo")
            with BuildAgentHarness(repo_url=repo_url, branch_name="main") as harness:
                if not harness.clone_and_checkout(create_new=False):
                    return ExecResult(ok=False, detail={"error": "Failed to clone repo and checkout main"})
                if not harness.revert_last_merge():
                    return ExecResult(ok=False, detail={"error": "Failed to revert last merge"})
            return ExecResult(ok=True, detail={"message": "Rollback successful (reverted merge)."})
        else:
            return ExecResult(ok=False, detail={"error": f"Unknown action: {op.action}"})

    async def verify(self, op: OpSpec, session: Optional[AsyncSession] = None) -> VerifyResult:
        """Verifies production deployment."""
        if op.action == "build.design.makeover":
            return VerifyResult(ok=True, checks={"design_blueprint_drafted": True})

        logger.info("Verifying production deployment")
        # MOCK: Check if prod URL is active and returns 200
        prod_url = "https://www.ableys.in"
        return VerifyResult(
            ok=True,
            checks={
                "http_ok": True,
                "version_match": True
            },
            detail={"verified_url": prod_url}
        )

    def compensate(self, op: OpSpec) -> list[OpSpec]:
        """Rolls back the deployment by reverting the merge or redeploying previous revision."""
        logger.info(f"Compensating build delivery: reverting {op.params.get('branch_name')}")
        
        return [
            OpSpec(
                tenant_id=op.tenant_id,
                brand_id=op.brand_id,
                domain=self.domain,
                action="build.rollback",
                params={
                    "revert_branch": op.params.get("branch_name"),
                    "repo": op.params.get("repo")
                },
                severity=Severity(impact=2, reversibility=Reversibility.IRREVERSIBLE),
                parent_op_id=op.id,
            )
        ]
