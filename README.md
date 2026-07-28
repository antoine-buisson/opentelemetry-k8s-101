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
- **Correlated signals**: traces, logs, and metrics are linked by trace context, so you can jump
  between them in one click (see below).
- **Keycloak SSO**: OIDC login for Grafana where Keycloak group membership decides which tenant
  (org) and role a user gets, with local `admin/admin` kept as a fallback (see below).

## Correlated telemetry (the OpenTelemetry payoff)

Because the SDK propagates trace context into every signal, the three are cross-linked in Grafana.
All wiring is provisioned by `tools/grafana-bootstrap`; the links are per-org so they stay within a
tenant's data.

| From | To | How | Try it |
|---|---|---|---|
| **Logs → Trace** | Tempo | The SDK stamps `trace_id` on log records; Loki exposes it as a label and a datasource *derived field* turns it into a link | In Explore/dashboard logs, expand a line and click **View trace** |
| **Trace → Logs** | Loki | Tempo `tracesToLogsV2` maps the span's `service.name` to the `service_name` log label | Open a trace, click **Logs for this span** |
| **Metrics → Trace** | Tempo | Metrics carry **exemplars** (a sample tagged with its `trace_id`); Mimir stores them and the datasource links them | On a latency panel, hover an exemplar dot and click the trace link |

The **Overview dashboard** in each org shows RED metrics (request rate, 5xx rate, p95 latency, with
exemplar dots) and a logs panel with the clickable TraceID.

## SSO with Keycloak

Grafana login is backed by **Keycloak** (OIDC). A user's **Keycloak group membership** decides which
tenant org and role they get in Grafana, via Grafana OSS `org_mapping`. The local `admin/admin`
login stays enabled as a fallback.

The realm is **generated from `config/tenants.yaml`** by `tools/keycloak-realm/generate_realm.py`
(same source of truth as everything else), so users and their groups never drift. Group → access
mapping:

| Keycloak group | Grafana result |
|---|---|
| `tenant-a-editors` / `-viewers` / `-admins` | Org **Tenant A** as Editor / Viewer / Admin |
| `tenant-b-editors` / `-viewers` / `-admins` | Org **Tenant B** as ... |
| `auditors` | **Viewer in both** tenant orgs (cross-tenant read) |
| `platform-admins` | **Grafana server admin** (via `role_attribute_path`) |

Log in: run `make keycloak-info` for the URLs, open Grafana at `http://grafana.<minikube-ip>.nip.io`,
click **Sign in with Keycloak**, and use a demo user (passwords in `config/tenants.yaml`). For
example `alice` lands in Tenant A as Editor; `auditor` sees both tenants read-only; `platform-admin`
is server admin. (`make grafana-forward` still works for the local `admin/admin` fallback.)

How the URLs line up (the usual Keycloak-in-k8s snags):
- **Ingress via nip.io.** `make up` enables minikube's ingress addon and creates Ingress for Grafana
  and Keycloak at `grafana.<minikube-ip>.nip.io` / `keycloak.<minikube-ip>.nip.io`. `nip.io` resolves
  those to the minikube IP with no `/etc/hosts` edits, from both the host browser and in-cluster
  pods (Linux + docker driver). No port-forwarding needed.
- **Browser vs server URLs.** Grafana's browser-facing `auth_url` uses the Keycloak ingress host;
  the server-to-server `token_url`/`api_url` use the in-cluster service DNS. `generic_oauth` does not
  validate the issuer, so this split is fine.
- **Identity linking.** The bootstrap creates the local users with emails matching Keycloak
  (`<login>@otel-101.local`), and `oauth_allow_insecure_email_lookup` lets an SSO login attach to
  that existing account (keeping team membership) instead of creating a duplicate. Without this you
  get "User sync failed".

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
make up             # minikube + storage + backends + operator + collectors + keycloak + grafana + ingress + workloads
make status         # watch everything become Ready (give it a few minutes)
make smoke          # validate RustFS S3 + backend readiness
make keycloak-info  # print the Grafana/Keycloak URLs (nip.io ingress) + demo logins
```

Open Grafana at `http://grafana.<minikube-ip>.nip.io` (from `make keycloak-info`). Click **Sign in
with Keycloak** and use a demo user (`alice`, `bob`, `carol`, `auditor`, `platform-admin`; passwords
in `config/tenants.yaml`), or use the local `admin` / `admin` fallback. In each org open **Explore**
and query Mimir (metrics), Loki (logs), and Tempo (traces); data appears within a minute or two.

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
| `deploy/45-keycloak` | Keycloak (OIDC) + realm ConfigMap | manifest + generated realm |
| `deploy/50-grafana` | Grafana (Keycloak SSO) + bootstrap Job | Helm + Job |
| `deploy/60-workloads` | Per-team synthetic apps | templated manifest |
| `deploy/80-ingress` | nip.io Ingress for Grafana + Keycloak | templated manifest |
| `apps/synthetic` | The Python app + Dockerfile | built into minikube's docker |
| `tools/grafana-bootstrap` | Idempotent Grafana API provisioner | Python |
| `tools/keycloak-realm` | Realm generator from `tenants.yaml` | Python |

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
| Keycloak | `quay.io/keycloak/keycloak` | 26.7.0 |

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
- Each org gets an **Overview dashboard** (request rate, 5xx rate, p95 latency, and a logs panel)
  provisioned by `tools/grafana-bootstrap`. **Explore** is still there for ad-hoc queries.
- **SSO caveats.** Group → **org + role** mapping works in Grafana OSS (`org_mapping`); group →
  **Grafana Team** sync is Enterprise-only, so team membership stays bootstrap-driven. The Keycloak
  NodePort URL is reachable by the browser only on **Linux + docker driver** (the minikube node IP
  is host-routable there); on macOS/Windows use a tunnel and adjust the OAuth URLs. Keycloak runs in
  dev mode with an **ephemeral H2** store, so the realm JSON (regenerated from `tenants.yaml`) is the
  source of truth and is re-imported on every restart. The client secret and user passwords here are
  **demo values**.
