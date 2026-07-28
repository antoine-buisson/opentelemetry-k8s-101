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
team org(s) they land in, via Grafana OSS `org_mapping` (a group can map to several orgs). The local
`admin/admin` login stays enabled as a fallback.

The realm is **generated from `config/tenants.yaml`** by `tools/keycloak-realm/generate_realm.py`
(same source of truth as everything else), so users and their groups never drift. Group → org
mapping (everyone is Editor; the org is what scopes data):

| Keycloak group | Grafana org(s) |
|---|---|
| `payments` / `onboarding` / `trading` / `reporting` | the matching team org (that team only) |
| `tenant-a` | **Tenant A - Payments** + **Tenant A - Onboarding** |
| `tenant-b` | **Tenant B - Trading** + **Tenant B - Reporting** |
| `auditors` | all four team orgs |
| `platform-admins` | **Grafana server admin** (via `role_attribute_path`) |

Log in: run `make keycloak-info` for the URLs, open Grafana at `http://grafana.<minikube-ip>.nip.io`,
click **Sign in with Keycloak**, and use a demo user (passwords in `config/tenants.yaml`). For
example `alice` sees only the Payments team; `dave` (tenant-a) can switch between both Tenant A team
orgs; `auditor` can switch across all four; `platform-admin` is server admin. (`make grafana-forward`
still works for the local `admin/admin` fallback.)

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
  payments             onboarding           trading              reporting
  (ns tenant-a-team-1) (ns tenant-a-team-2) (ns tenant-b-team-1) (ns tenant-b-team-2)
      |  Python app auto-instrumented by the OTEL Operator (pod annotation)
      |  OTLP                    |                   |                  |
      v                          v                   v                  v
  [ gateway per TEAM ] — each stamps its own X-Scope-OrgID (= its namespace)
      | tenant-a-team-1   | tenant-a-team-2   | tenant-b-team-1   | tenant-b-team-2
      +----> Tempo (traces) ---+
      +----> Mimir (metrics) --+--- S3 --->  [ RustFS ]  (buckets: mimir/loki/tempo)
      +----> Loki  (logs) -----+
  [ Grafana OSS ]  one Org per team, datasource sends only that team's X-Scope-OrgID:
    Org "Tenant A - Payments"   -> tenant-a-team-1   (sees only payments)
    Org "Tenant A - Onboarding" -> tenant-a-team-2   (sees only onboarding)
    Org "Tenant B - Trading"    -> tenant-b-team-1   ...
    Org "Tenant B - Reporting"  -> tenant-b-team-2
```

Every team's signals carry that team's `X-Scope-OrgID`, so Mimir/Loki/Tempo store and serve each
team's data separately. Grafana reproduces the boundary: one Organization per team, whose
datasources inject only that team's header. A "tenant-wide" view is simply membership in both of a
tenant's team orgs.

## Tenancy and access model

The isolation boundary is the **team**. Each team is hard-isolated at the data layer and mirrored
by a Grafana org:

| Concept | Implemented as | Isolation |
|---|---|---|
| **Team** (4: payments, onboarding, trading, reporting) | Its own Mimir/Loki/Tempo tenant (`X-Scope-OrgID = <team>`) **+** its own Grafana Org whose datasources carry only that tenant id | **Hard** (query-level: a member can only ever query their team's data) |
| **Tenant** (Tenant A, Tenant B) | Not a backend tenant here — just "the pair of team orgs". A tenant-level user is a member of both team orgs and switches between them | Grouping |
| **Cross-cutting** | `auditor` (member of all four team orgs), `platform-admin` (Grafana server admin) | Grafana-native |

Everything is driven by one file: [`config/tenants.yaml`](config/tenants.yaml) (teams + users). The
collectors, workloads, Grafana bootstrap, and Keycloak realm all follow it.

**Why team = its own tenant + org (the important bit).** Grafana **OSS has no LBAC and no
fine-grained RBAC** — those are Enterprise/Cloud
([LBAC](https://grafana.com/docs/grafana/latest/administration/data-source-management/teamlbac/),
[RBAC](https://grafana.com/docs/grafana/latest/administration/roles-and-permissions/access-control/)).
In OSS, Teams and folder permissions only gate *saved dashboards*; they do **not** restrict Explore
or ad-hoc queries — a user can query everything in a datasource. So the only way to enforce
per-team query isolation in OSS is at the data layer: a separate backend tenant (`X-Scope-OrgID`)
and a separate Grafana org per team. That is what this repo does. (Org roles Viewer/Editor/Admin
don't scope *data*, so everyone is Editor.)

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

Each team is its own backend tenant, so a query scoped to one team only returns that team. From
inside the cluster (the team's tenant id is its namespace):

```bash
# Payments team only -> only the "payments" service appears.
kubectl -n observability run q --rm -i --restart=Never --image=curlimages/curl --command -- \
  curl -s -G -H 'X-Scope-OrgID: tenant-a-team-1' \
  'http://mimir:8080/prometheus/api/v1/query' --data-urlencode 'query=count by (job) (target_info)' ; echo

# Onboarding team only -> different, non-overlapping result.
kubectl -n observability run q --rm -i --restart=Never --image=curlimages/curl --command -- \
  curl -s -G -H 'X-Scope-OrgID: tenant-a-team-2' \
  'http://mimir:8080/prometheus/api/v1/query' --data-urlencode 'query=count by (job) (target_info)' ; echo
```

In Grafana, log in as `alice` — she is only in the **Tenant A - Payments** org, so Explore shows
only payments data (she can no longer see onboarding). `dave` can switch between both Tenant A team
orgs; `auditor` across all four.

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
- **SSO caveats.** Group → **org** mapping works in Grafana OSS (`org_mapping`, and one group can map
  to several orgs). LBAC / fine-grained RBAC / datasource permissions and group→Grafana-Team sync are
  all Enterprise/Cloud — which is exactly why isolation is enforced by the backend tenant + org
  boundary, not inside Grafana. Ingress hosts (`*.<minikube-ip>.nip.io`) are browser-reachable only on
  **Linux + docker driver**; on macOS/Windows use a tunnel and adjust the OAuth URLs. Keycloak runs in
  dev mode with an **ephemeral H2** store, so the realm JSON (regenerated from `tenants.yaml`) is the
  source of truth and is re-imported on every restart. The client secret and user passwords here are
  **demo values**.
