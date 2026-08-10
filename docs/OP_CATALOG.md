# Operation (Op) Catalog

This document lists the V1 Ops along with their assigned risk class and compensation semantics, satisfying the Phase 0 requirements of the Production Readiness Runbook.

## Op Primitives

### Provision Pillar
| Action | Risk Class | Reversibility | Description |
|---|---|---|---|
| `provision.recipe.plan` | Low | `REVERSIBLE` | Generates a Terraform plan for a recipe. |
| `provision.recipe.apply` | High | `COMPENSATABLE` | Applies a Terraform recipe (e.g., brand-baseline, web-host). |
| `provision.recipe.destroy` | High | `IRREVERSIBLE` | Tears down provisioned infrastructure. |

### Build Pillar
| Action | Risk Class | Reversibility | Description |
|---|---|---|---|
| `build.branch.create` | Low | `REVERSIBLE` | Creates a feature branch. |
| `build.preview.deploy` | Medium | `COMPENSATABLE` | Deploys a staging preview URL. |
| `build.production.merge` | High | `COMPENSATABLE` | Merges and deploys to production. Reversible via revision rollback. |

### Manage Pillar
| Action | Risk Class | Reversibility | Description |
|---|---|---|---|
| `manage.inventory.sync` | Low | `REVERSIBLE` | Syncs read-only state from external platforms. |
| `manage.snapshot.create` | Medium | `REVERSIBLE` | Creates a point-in-time state snapshot. |
| `manage.infrastructure.redeploy`| High | `COMPENSATABLE` | Redeploys client stack for continuity. |

### Grow Pillar
| Action | Risk Class | Reversibility | Description |
|---|---|---|---|
| `grow.bid.adjust` | Medium | `COMPENSATABLE` | Adjusts bid modifiers for campaigns. |
| `grow.budget.reallocate` | Medium | `COMPENSATABLE` | Shifts budgets between platforms or campaigns. |
| `grow.campaign.pause` | Low | `COMPENSATABLE` | Pauses an active ad campaign. |
| `grow.alert.dispatch` | Low | `IRREVERSIBLE` | Sends an operator or client alert. |
