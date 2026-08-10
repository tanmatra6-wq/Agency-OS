# Data Classification

This document classifies all data handled by Agency OS into four distinct categories as per the Production Readiness Runbook (Phase 0).

## Classification Levels

1. **Public**
   - Data intended for broad, unauthenticated distribution.
   - Examples: Marketing website content, public product docs, public DNS records.

2. **Internal**
   - Data meant for internal operations but lacking sensitive business or technical information.
   - Examples: Architecture documents, runbooks, internal dashboards without financial data.

3. **Confidential**
   - Data that requires protection and authenticated access, but is not critical to system security or privacy.
   - Examples: Brand application code, marketing campaign analytics, standard audit logs (without secrets), tenant configurations, billing metadata, rule/policy parameters, advisory metrics (trust scores).

4. **Restricted**
   - Highly sensitive data that can cause significant harm if exposed, modified, or leaked.
   - Examples:
     - Authentication tokens and OAuth credentials
     - Git credentials
     - Cloud credentials (Service Account Keys)
     - Webhook secrets
     - Advertising account credentials
     - Customer PII (Personally Identifiable Information)
     - Decrypted backups
     - Private keys (SSL, encryption)
     - Database exports
     - Recovery material

## Handling Requirements

| Level | Storage | Transit | Access Control | Logging |
|---|---|---|---|---|
| **Public** | Standard | HTTPS | Open | Standard access logs |
| **Internal** | Standard | HTTPS | Authenticated | Standard access logs |
| **Confidential** | Encrypted at rest | TLS 1.2+ | RLS + RBAC enforced | Audited access |
| **Restricted** | Secret Manager | TLS 1.3 | Strict identity impersonation, short-lived tokens | Metadata only, no values logged |

## Review & Updates
This classification must be reviewed during any major architectural change or at least annually.
