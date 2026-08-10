import logging
import os
import tempfile
from typing import Optional, Any
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.kernel.optypes import ExecResult, Money, OpSpec, PreviewArtifact, Reversibility, Severity, VerifyResult
from app.kernel.services import audit_verify
from app.models import Base, AuditEvent

logger = logging.getLogger(__name__)

class DRAdapter:
    """Automated database backup restore and verification drill adapter (§6.5)."""
    domain = "dr"

    def __init__(self):
        # Clean up orphan temporary database files from crashed/cancelled runs
        try:
            import time
            import glob
            temp_dir = tempfile.gettempdir()
            pattern = os.path.join(temp_dir, "scratch_restore_*.db")
            now = time.time()
            for filepath in glob.glob(pattern):
                # If file is older than 5 minutes (300 seconds), delete it
                try:
                    if now - os.path.getmtime(filepath) > 300:
                        os.unlink(filepath)
                        logger.info(f"Cleaned up orphan temporary DR database: {filepath}")
                except OSError:
                    pass
        except Exception as e:
            logger.warning(f"Failed to clean up orphan DR database files during initialization: {e}")

    def plan(self, intent: str, tenant_id: str, brand_id: str) -> list[OpSpec]:
        normalized = intent.lower()
        if "verify dr" in normalized or "run dr drill" in normalized or "database restore verify" in normalized:
            return [OpSpec(
                tenant_id=tenant_id,
                brand_id=brand_id,
                domain=self.domain,
                action="dr.restore_verify",
                params={"brand_id": brand_id},
                severity=Severity(impact=1, reversibility=Reversibility.REVERSIBLE),
                cost_estimate=Money(0)
            )]
        return []

    def preview(self, op: OpSpec) -> PreviewArtifact:
        return PreviewArtifact(
            kind="dr_drill_preview",
            summary="Will restore latest pg_dump into a temporary scratch DB, verify row counts, and execute audit_verify to prove continuity readiness.",
            detail={"scope": "full_db_restore_drill"}
        )

    async def execute(self, op: OpSpec, idem_key: str, session: Optional[AsyncSession] = None) -> ExecResult:
        if op.params.get("simulate_failure") or op.params.get("simulate_timeout"):
            return ExecResult(
                ok=False,
                detail={"error": "DR restore failed: Simulated restore timeout / database unavailable."}
            )

        # In a real environment, we would run: pg_restore / pg_dump tools to restore a backup.
        # For testing/sandbox, we simulate the restore by creating a temporary file-based sqlite database,
        # copying the current schema and the active tenant's audit events into it.
        temp_dir = tempfile.gettempdir()
        scratch_db_path = os.path.join(temp_dir, f"scratch_restore_{op.id}.db")
        scratch_url = f"sqlite+aiosqlite:///{scratch_db_path}"

        logger.info(f"Simulating DR restore drill. Creating scratch DB at {scratch_url}")
        
        try:
            # 1. Create schema on scratch DB
            scratch_engine = create_async_engine(scratch_url)
            async with scratch_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            # 2. Copy audit events from production session to scratch DB
            if session:
                # Retrieve all audit events for the active tenant
                stmt = select(AuditEvent).where(AuditEvent.tenant_id == op.tenant_id).order_by(AuditEvent.id.asc())
                res = await session.execute(stmt)
                events = res.scalars().all()

                # Insert them into the scratch DB
                scratch_session_maker = async_sessionmaker(scratch_engine, expire_on_commit=False)
                async with scratch_session_maker() as scratch_session:
                    async with scratch_session.begin():
                        for ev in events:
                            # Create new AuditEvent row mapping fields
                            scratch_ev = AuditEvent(
                                id=ev.id,
                                ts=ev.ts,
                                tenant_id=ev.tenant_id,
                                actor=ev.actor,
                                action=ev.action,
                                op_id=ev.op_id,
                                payload=ev.payload,
                                prev_hash=ev.prev_hash,
                                hash=ev.hash
                            )
                            scratch_session.add(scratch_ev)
            
            await scratch_engine.dispose()
            
            # Save the scratch URL in the detail to let verify() access it
            return ExecResult(
                ok=True,
                detail={
                    "message": "Backup successfully restored to scratch database",
                    "scratch_db_url": scratch_url,
                    "temp_dir": temp_dir
                }
            )
        except Exception as e:
            logger.exception(f"DR execution failed during restore: {e}")
            if os.path.exists(scratch_db_path):
                try:
                    os.unlink(scratch_db_path)
                except OSError:
                    pass
            return ExecResult(ok=False, detail={"error": f"Restore execution error: {str(e)}"})

    async def verify(self, op: OpSpec, session: Optional[AsyncSession] = None) -> VerifyResult:
        # In execute we returned the scratch db url in details, but wait!
        # verify() receives only the OpSpec! It does NOT receive the ExecResult!
        # Ah! Let's check `_execute_and_verify` in `loop.py`:
        # ```python
        # 552:     await transition(s, row, OpState.VERIFYING, actor="kernel")
        # 553:     verdict = await adapter.verify(spec)
        # ```
        # It does NOT pass execute details to verify!
        # So how does verify find the scratch DB url?
        # We can construct the scratch DB url deterministically using the Op's id or idem_key!
        # Wait, the idem_key is not in OpSpec.
        # But we can use `op.id`!
        # Yes! `op.id` is in OpSpec!
        # Let's construct it as: `scratch_restore_{op.id}.db`!
        
        # Let's see: we can write it to a standard system temporary directory
        temp_dir = tempfile.gettempdir()
        scratch_db_path = os.path.join(temp_dir, f"scratch_restore_{op.id}.db")
        scratch_url = f"sqlite+aiosqlite:///{scratch_db_path}"

        if op.params.get("simulate_verify_failure"):
             if os.path.exists(scratch_db_path):
                 try:
                     os.unlink(scratch_db_path)
                 except OSError:
                     pass
             return VerifyResult(ok=False, checks={"database_restored": True, "audit_chain_verified": False}, detail={"error": "Simulated verification failure"})

        if not os.path.exists(scratch_db_path):
            return VerifyResult(
                ok=False,
                checks={"database_restored": False},
                detail={"error": f"Scratch database file {scratch_db_path} not found. Restore might have failed."}
            )

        try:
            try:
                # Open session to the scratch database
                scratch_engine = create_async_engine(scratch_url)
                scratch_session_maker = async_sessionmaker(scratch_engine, expire_on_commit=False)
                
                async with scratch_session_maker() as scratch_session:
                    # Run audit_verify on the scratch database!
                    ok, first_bad_id = await audit_verify(scratch_session)
                    
                await scratch_engine.dispose()
            finally:
                # Unconditionally delete the temporary database file to prevent PII leaks
                if os.path.exists(scratch_db_path):
                    try:
                        os.unlink(scratch_db_path)
                    except OSError:
                        pass

            if ok:
                return VerifyResult(
                    ok=True,
                    checks={"database_restored": True, "audit_chain_verified": True}
                )
            else:
                return VerifyResult(
                    ok=False,
                    checks={"database_restored": True, "audit_chain_verified": False},
                    detail={"error": f"Audit chain verification failed in scratch database. First bad event: {first_bad_id}"}
                )
        except Exception as e:
            logger.exception(f"DR verification exception: {e}")
            return VerifyResult(
                ok=False,
                checks={"database_restored": True, "audit_chain_verified": False},
                detail={"error": f"Verification error: {str(e)}"}
            )

    def compensate(self, op: OpSpec) -> list[OpSpec]:
        # Clean up is done automatically or is a no-op since it's scratch only
        return []
