# Asset Inventory

This document tracks all physical and logical assets within Agency OS. It is a requirement for Phase 0 of the Production Readiness Runbook.

## Asset Registry

### 1. Control Plane Assets
| Field | Value |
|---|---|
| **asset_id** | `aos-control-plane-db` |
| **tenant_id** | `SYSTEM` |
| **environment** | `prod` |
| **owner** | Platform Team |
| **data_classification** | Confidential / Restricted |
| **region** | `us-central1` |
| **authentication_method** | IAM / Cloud SQL Auth Proxy |
| **authorized_services** | `aos-control-plane-api`, `aos-outbox-worker` |
| **retention_rule** | 7 years (audit log requirements) |
| **backup_rule** | Daily pg_dump to multi-region GCS bucket |
| **recovery_objective** | RPO: 24h, RTO: 4h |
| **monitoring_rule** | Cloud Monitoring (CPU, Memory, RLS violations) |
| **decommission_rule** | Secure wipe after 7-year retention period |

| Field | Value |
|---|---|
| **asset_id** | `aos-control-plane-api` |
| **tenant_id** | `SYSTEM` |
| **environment** | `prod` |
| **owner** | Platform Team |
| **data_classification** | Internal |
| **region** | `us-central1` |
| **authentication_method** | IAP / JWT |
| **authorized_services** | Web Clients, Webhooks Router |
| **retention_rule** | Logs 30 days |
| **backup_rule** | Stateless (redeployed via CI/CD) |
| **recovery_objective** | RTO: < 5m |
| **monitoring_rule** | Sentry, Cloud Logging, Prometheus metrics |
| **decommission_rule** | Delete Cloud Run service |

### 2. Tenant Shared Tier
| Field | Value |
|---|---|
| **asset_id** | `aos-shared-tier-db` |
| **tenant_id** | `SHARED` |
| **environment** | `prod` |
| **owner** | Platform Team |
| **data_classification** | Confidential |
| **region** | `us-central1` |
| **authentication_method** | IAM |
| **authorized_services** | `shared-tenant-services` |
| **retention_rule** | 30 days post-churn |
| **backup_rule** | Daily snapshot |
| **recovery_objective** | RPO: 24h, RTO: 4h |
| **monitoring_rule** | Cloud Monitoring |
| **decommission_rule** | Drop tenant schemas, secure delete at EOL |

### 3. Third-Party Credentials (Secret Manager)
| Field | Value |
|---|---|
| **asset_id** | `brand-secrets-*` |
| **tenant_id** | `BRAND_SPECIFIC` (Dedicated Project) |
| **environment** | `prod` |
| **owner** | Tenant / Platform Admin |
| **data_classification** | Restricted |
| **region** | `us-central1` |
| **authentication_method** | IAM (Service Account strictly scoped) |
| **authorized_services** | `aos-control-plane-api` (Impersonated) |
| **retention_rule** | Until revoked / rotated |
| **backup_rule** | None (Source of truth in external systems) |
| **recovery_objective** | Re-authenticate via OAuth/Token generation |
| **monitoring_rule** | Secret access logs (metadata only) |
| **decommission_rule** | Immediate destruction on tenant churn |

*(To be expanded via automation scripts polling GCP Asset Inventory)*
