# OpenTelemetry on Kubernetes 101

A self-contained, from-scratch introduction to OpenTelemetry on Kubernetes. Everything runs
on minikube, deploys with a few `make` commands, and tears down cleanly. It demonstrates the
full path from an auto-instrumented workload to a queryable dashboard, with clear multi-tenant
and multi-team separation.

## What you get

- **OpenTelemetry Operator** managing collectors and **auto-instrumenting** Python workloads
  (no OTEL code in the app; the SDK is injected via a pod annotation).
- **Grafana LGTM backends**: **Mimir** (metrics), **Loki** (logs), **Tempo** (traces).
- **RustFS** as the S3-compatible object storage behind all three backends.
- **Grafana OSS** as the single frontend.
- **2 tenants x 2 teams** with hard per-tenant data isolation and role-based access
  (`platform-admin`, `auditor`, team leads).
- **Automated fake telemetry**: a tiny self-driving Python service per team that generates
  traffic in a loop, so signals flow continuously with zero interaction.

## Architecture

```
  tenant-a-team-1        tenant-a-team-2      tenant-b-team-1     tenant-b-team-2
  (payments)             (onboarding)         (trading)           (reporting)
      |  Python app auto-instrumented by the OTEL Operator (pod annotation)
      |  OTLP                    |                   |                  |
      v                          v                   v                  v
  [ gateway-tenant-a collector ]           [ gateway-tenant-b collector ]
      |  stamps X-Scope-OrgID: tenant-a         |  stamps X-Scope-OrgID: tenant-b
      |                                         |
      +----> Tempo (traces) ----+              +----> Tempo
      +----> Mimir (metrics) ---+--- S3 --->   +----> Mimir  --- S3 --->  [ RustFS ]
      +----> Loki  (logs) ------+              +----> Loki                (buckets:
                                                                           mimir/loki/tempo)
  [ Grafana OSS ]
    Org "Tenant A"  -> datasources send X-Scope-OrgID: tenant-a  (sees only tenant-a data)
    Org "Tenant B"  -> datasources send X-Scope-OrgID: tenant-b  (sees only tenant-b data)
```

Every signal for a tenant carries that tenant's `X-Scope-OrgID`, so Mimir/Loki/Tempo store and
serve each tenant's data separately. Grafana reproduces the boundary: one Organization per
tenant, whose datasources inject the matching header.

## Tenancy and access model

| Concept | Implemented as | Isolation |
|---|---|---|
| **Tenant** (`tenant-a`, `tenant-b`) | Mimir/Loki/Tempo tenant via `X-Scope-OrgID` + a Grafana Organization | **Hard** (storage-level) |
| **Team** (2 per tenant) | Its own namespace + a Grafana Team + Folder; workloads tagged with `team`/`tenant` resource attributes | **Soft** (shares tenant data, scoped by folder/label) |
| **Roles** | Grafana org roles, plus `auditor` (Viewer in both orgs) and `platform-admin` (server admin) | Grafana-native |

Everything is driven by one file: [`config/tenants.yaml`](config/tenants.yaml). It defines the
tenants, teams, roles, and demo users. The collectors, workloads, and the Grafana bootstrap all
follow it.

**Honest limitation.** Grafana OSS has no per-query enforcement inside an org, so the two teams
in a tenant share the same datasource and their separation is by folder and by the `team` label,
not by query. To make a team a *hard* boundary, give it its own `X-Scope-OrgID`. The config is
data-driven, so that is a small, local change.

## Prerequisites

- `minikube`, `kubectl`, `helm` (v3), `docker`. Run `make check-tools` to verify; it prints
  install hints for anything missing.
- Suggested resources: **6 CPU / 12 GiB** for minikube. Override with
  `make up MINIKUBE_CPUS=4 MINIKUBE_MEMORY=8192` on smaller machines.
- Internet egress from the cluster (pulls images; the Grafana bootstrap Job pip-installs
  `requests` and `pyyaml`).

## Quick start

```bash
make up               # minikube + storage + backends + operator + collectors + grafana + workloads
make status           # watch everything become Ready (give it a few minutes)
make smoke            # validate RustFS S3 + backend readiness
make grafana-forward  # Grafana on http://localhost:3000
```

Log in to Grafana as `admin` / `admin` (server admin: switch orgs via the org menu), or use the
per-role demo users in `config/tenants.yaml` (`platform-admin`, `auditor`, `alice`, `bob`,
`carol`). In each org open **Explore** and query Mimir (metrics), Loki (logs), and Tempo
(traces). Data from the synthetic apps appears within a minute or two.

## Verify the isolation

The point of the demo is that tenant A cannot see tenant B's data. From inside the cluster:

```bash
# Same query, different tenant header -> different data.
kubectl -n observability run q --rm -i --restart=Never --image=curlimages/curl -- \
  curl -s -H 'X-Scope-OrgID: tenant-a' \
  'http://mimir:8080/prometheus/api/v1/query?query=up' ; echo

kubectl -n observability run q --rm -i --restart=Never --image=curlimages/curl -- \
  curl -s -H 'X-Scope-OrgID: tenant-b' \
  'http://loki:3100/loki/api/v1/labels' ; echo
```

In Grafana, log in as `auditor` (read-only in both orgs) versus `alice` (Editor in Tenant A
only) to see role separation.

## Layers (deploy order)

| Dir | What | How |
|---|---|---|
| `deploy/00-namespaces` | Namespaces | manifest |
| `deploy/10-storage` | RustFS + bucket-create Job | Helm + Job |
| `deploy/20-backends` | Mimir (single binary), Loki, Tempo | manifest + Helm |
| `deploy/30-operator` | OpenTelemetry Operator | Helm (self-signed webhook cert) |
| `deploy/40-collectors` | Per-tenant gateway collectors + Instrumentation CRs | manifest |
| `deploy/50-grafana` | Grafana + bootstrap Job | Helm + Job |
| `deploy/60-workloads` | Per-team synthetic apps | templated manifest |
| `apps/synthetic` | The Python app + Dockerfile | built into minikube's docker |
| `tools/grafana-bootstrap` | Idempotent Grafana API provisioner | Python |

Each `make <layer>` target (e.g. `make backends`) runs independently once the cluster is up.

## Versions (pinned)

| Component | Chart / image | Version |
|---|---|---|
| RustFS | `rustfs/rustfs` | 0.11.0 (app 1.0.0-beta.11) |
| Mimir | `grafana/mimir` (single binary) | 3.1.2 |
| Loki | `grafana-community/loki` | 18.7.0 (app 3.7.4) |
| Tempo | `grafana-community/tempo` | 2.2.3 (app 2.10.7) |
| Grafana | `grafana-community/grafana` | 12.10.0 (app 13.1.1) |
| OTEL Operator | `open-telemetry/opentelemetry-operator` | 0.120.0 (app 0.156.0) |

> The Grafana Helm charts moved to the `grafana-community` repo in early 2026; `mimir-distributed`
> stayed in the `grafana` repo. `make repos` adds both.

### Why not the Mimir Helm chart

`mimir-distributed` is microservices-only; even its smallest preset (`small.yaml`) requests
roughly 10+ CPU and 30+ GiB, which does not fit on minikube. For a 101 we run Mimir as a single
binary (`-target=all`) from a small Deployment with a readable config in
[`deploy/20-backends/mimir/mimir.yaml`](deploy/20-backends/mimir/mimir.yaml). Loki and Tempo do
ship genuine single-binary charts, so those stay on Helm.

## Teardown

```bash
make down    # remove the stack, keep the cluster
make nuke    # delete the whole minikube cluster
make reset   # nuke + up (true from-scratch rebuild)
```

## Caveats

- **RustFS is beta** and not officially certified against the Grafana stack. `make smoke`
  validates the S3 pairing first. If it ever blocks, swapping in MinIO is a values-only change
  (same S3 API).
- Credentials in this repo (`otel-demo/...`, Grafana `admin/admin`) are **demo values**. Change
  them for anything beyond a local sandbox.
- Logs and metrics flow via the app's OTLP export (auto-instrumentation), which keeps per-tenant
  routing clean. Cluster/node infra metrics via a DaemonSet collector are a natural next step but
  are intentionally out of scope for the 101.
- **Where's my data in RustFS?** Backends buffer, then flush to object storage on a schedule.
  Tempo and Loki are tuned here to flush within ~1-2 minutes (see the `ingester` blocks in their
  values), so trace blocks and log chunks appear in the `tempo`/`loki` buckets quickly, partitioned
  by tenant (`tenant-a/`, `tenant-b/`). **Mimir ships metrics blocks only every ~2h** by design, so
  the `mimir` bucket holds just a cluster-seed file until then. Data is still queryable immediately
  in Grafana the whole time (it is served from the ingester's memory/WAL before it reaches S3).
- Grafana ships with datasources per org but **no prebuilt dashboards** here. Use **Explore** to
  query Mimir/Loki/Tempo. Provisioning starter dashboards per org is a straightforward extension of
  `tools/grafana-bootstrap`.
