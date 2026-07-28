#!/usr/bin/env python3
"""Idempotently provision Grafana OSS from config/tenants.yaml.

The isolation boundary is the TEAM. For each team this creates:
  - a Grafana Organization (team.grafanaOrg)
  - Mimir / Loki / Tempo datasources in that org, each injecting ONLY that team's
    X-Scope-OrgID (team.tenantId). Because Grafana OSS has no LBAC/RBAC, this single-tenant
    datasource is what physically scopes a member's queries to their team's data.
  - a small overview dashboard

Then it creates the users, sets server-admin where requested, and adds each user to the orgs
implied by their Keycloak `groups` (so local login mirrors what SSO org_mapping does):
  team group (payments/onboarding/...) -> that team org
  tenant group (tenant-a/tenant-b)     -> both of that tenant's team orgs
  auditors                             -> all team orgs
  platform-admins                      -> none here (granted server admin instead)
Everyone is Editor (OSS org roles do not scope data).

Re-runnable: every create is guarded by a lookup first.

Env: GRAFANA_URL, GRAFANA_USER, GRAFANA_PASSWORD, TENANTS_FILE
"""
import os
import sys
import time

import requests
import yaml

BASE = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
USER = os.environ.get("GRAFANA_USER", "admin")
PASSWORD = os.environ.get("GRAFANA_PASSWORD", "admin")
TENANTS_FILE = os.environ.get("TENANTS_FILE", "/config/tenants.yaml")

session = requests.Session()
session.auth = (USER, PASSWORD)
session.headers.update({"Content-Type": "application/json"})

# Datasource endpoints (in-cluster DNS).
MIMIR_URL = "http://mimir.observability.svc.cluster.local:8080/prometheus"
LOKI_URL = "http://loki.observability.svc.cluster.local:3100"
TEMPO_URL = "http://tempo.observability.svc.cluster.local:3200"


def log(msg):
    print(msg, flush=True)


def api(method, path, **kw):
    return session.request(method, f"{BASE}{path}", timeout=30, **kw)


def wait_ready(timeout=300):
    log(f"Waiting for Grafana at {BASE} ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = api("GET", "/api/health")
            if r.ok and r.json().get("database") == "ok":
                log("Grafana is ready.")
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    sys.exit("Grafana did not become ready in time.")


# ---- Orgs -------------------------------------------------------------------
def ensure_org(name):
    r = api("GET", f"/api/orgs/name/{name}")
    if r.ok:
        return r.json()["id"]
    r = api("POST", "/api/orgs", json={"name": name})
    if r.ok:
        oid = r.json()["orgId"]
        log(f"  created org '{name}' (id={oid})")
        return oid
    r = api("GET", f"/api/orgs/name/{name}")
    r.raise_for_status()
    return r.json()["id"]


def ensure_admin_in_org(org_id):
    # The admin must be a member of an org to switch into it and manage it.
    api("POST", f"/api/orgs/{org_id}/users", json={"loginOrEmail": USER, "role": "Admin"})
    api("POST", f"/api/user/using/{org_id}").raise_for_status()


# ---- Datasources ------------------------------------------------------------
def ensure_datasource(uid, name, ds_type, url, org_header_value, extra_json=None,
                      is_default=False):
    """Create a datasource with a fixed uid (so datasources can cross-reference each other for
    correlation) that injects a single team's X-Scope-OrgID. Delete-then-create for idempotency."""
    payload = {
        "uid": uid, "name": name, "type": ds_type, "access": "proxy", "url": url,
        "isDefault": is_default,
        "jsonData": {"httpHeaderName1": "X-Scope-OrgID", **(extra_json or {})},
        "secureJsonData": {"httpHeaderValue1": org_header_value},
    }
    if api("GET", f"/api/datasources/name/{name}").ok:
        api("DELETE", f"/api/datasources/name/{name}")
    api("POST", "/api/datasources", json=payload).raise_for_status()
    log(f"    datasource {name} (uid={uid}, X-Scope-OrgID={org_header_value})")


def datasources_for_org(tenant_id):
    """Create the three correlated datasources for a team org, scoped to one tenant id."""
    mimir_uid, loki_uid, tempo_uid = (f"mimir-{tenant_id}", f"loki-{tenant_id}",
                                      f"tempo-{tenant_id}")
    ensure_datasource(mimir_uid, "Mimir", "prometheus", MIMIR_URL, tenant_id, is_default=True,
                      extra_json={"httpMethod": "POST",
                                  "exemplarTraceIdDestinations": [
                                      {"name": "trace_id", "datasourceUid": tempo_uid}]})
    ensure_datasource(loki_uid, "Loki", "loki", LOKI_URL, tenant_id,
                      extra_json={"derivedFields": [{
                          "name": "TraceID", "matcherType": "label", "matcherRegex": "trace_id",
                          "datasourceUid": tempo_uid, "url": "${__value.raw}",
                          "urlDisplayLabel": "View trace"}]})
    ensure_datasource(tempo_uid, "Tempo", "tempo", TEMPO_URL, tenant_id,
                      extra_json={
                          "tracesToLogsV2": {"datasourceUid": loki_uid,
                                             "spanStartTimeShift": "-5m", "spanEndTimeShift": "5m",
                                             "filterByTraceID": False, "filterBySpanID": False,
                                             "tags": [{"key": "service.name", "value": "service_name"}]},
                          "tracesToMetrics": {"datasourceUid": mimir_uid,
                                              "spanStartTimeShift": "-5m", "spanEndTimeShift": "5m",
                                              "queries": [{"name": "Request rate by service",
                                                           "query": "sum by (job) (rate(http_server_duration_count[5m]))"}]},
                          "serviceMap": {"datasourceUid": mimir_uid},
                          "nodeGraph": {"enabled": True}})
    return mimir_uid, loki_uid


# ---- Dashboard --------------------------------------------------------------
def build_dashboard(uid, title, mimir_uid, loki_uid):
    prom = {"type": "prometheus", "uid": mimir_uid}
    loki = {"type": "loki", "uid": loki_uid}

    def ts(pid, ptitle, expr, x, unit):
        return {"id": pid, "type": "timeseries", "title": ptitle, "datasource": prom,
                "gridPos": {"x": x, "y": 0, "w": 8, "h": 9},
                "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
                "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
                "targets": [{"refId": "A", "datasource": prom, "expr": expr,
                             "legendFormat": "{{job}}", "exemplar": True}]}

    panels = [
        ts(1, "Request rate (req/s) by service",
           "sum by (job) (rate(http_server_duration_count[$__rate_interval]))", 0, "reqps"),
        ts(2, "5xx error rate by service",
           'sum by (job) (rate(http_server_duration_count{http_status_code=~"5.."}[$__rate_interval]))',
           8, "reqps"),
        ts(3, "p95 latency (ms) by service",
           "histogram_quantile(0.95, sum by (le, job) (rate(http_server_duration_bucket[$__rate_interval])))",
           16, "ms"),
        {"id": 4, "type": "logs",
         "title": "Logs (open a line, click TraceID to jump to the trace)", "datasource": loki,
         "gridPos": {"x": 0, "y": 9, "w": 24, "h": 11},
         "options": {"showTime": True, "wrapLogMessage": True, "enableLogDetails": True,
                     "sortOrder": "Descending"},
         "targets": [{"refId": "A", "datasource": loki, "expr": '{service_name=~".+"}',
                      "queryType": "range"}]},
    ]
    return {"uid": uid, "title": title, "tags": ["otel-101"], "schemaVersion": 39,
            "time": {"from": "now-15m", "to": "now"}, "refresh": "10s", "panels": panels}


def ensure_dashboard(org_id, uid, title, mimir_uid, loki_uid):
    api("POST", f"/api/user/using/{org_id}")  # dashboards are created in the current org
    dash = build_dashboard(uid, title, mimir_uid, loki_uid)
    r = api("POST", "/api/dashboards/db",
            json={"dashboard": dash, "folderUid": "", "overwrite": True})
    log(f"    dashboard '{title}'" if r.ok
        else f"    dashboard '{title}' FAILED: {r.status_code} {r.text[:200]}")


# ---- Users ------------------------------------------------------------------
def ensure_user(login, name, password, email):
    # Email MUST match the Keycloak user so an SSO login links to this account (with
    # oauth_allow_insecure_email_lookup) instead of creating a duplicate and colliding.
    existing = api("GET", "/api/users/lookup", params={"loginOrEmail": login})
    if existing.ok:
        uid = existing.json()["id"]
        api("PUT", f"/api/users/{uid}", json={"login": login, "name": name, "email": email})
        return uid
    r = api("POST", "/api/admin/users",
            json={"name": name, "login": login, "password": password, "email": email})
    if r.ok:
        uid = r.json()["id"]
        log(f"  created user '{login}' (id={uid})")
        return uid
    r = api("GET", "/api/users/lookup", params={"loginOrEmail": login})
    r.raise_for_status()
    return r.json()["id"]


def set_server_admin(user_id, is_admin):
    api("PUT", f"/api/admin/users/{user_id}/permissions", json={"isGrafanaAdmin": is_admin})


def add_user_to_org(org_id, login, role="Editor"):
    api("POST", f"/api/user/using/{org_id}")
    r = api("POST", f"/api/orgs/{org_id}/users", json={"loginOrEmail": login, "role": role})
    if r.status_code == 409:  # already a member -> update role
        api("PATCH", f"/api/orgs/{org_id}/users", json={"loginOrEmail": login, "role": role})


# ---- Main -------------------------------------------------------------------
def group_to_orgs(teams):
    """Keycloak group -> list of Grafana org names (mirrors org_mapping in grafana values)."""
    g = {}
    all_orgs = []
    for t in teams:
        all_orgs.append(t["grafanaOrg"])
        g.setdefault(t["group"], []).append(t["grafanaOrg"])          # team group -> its org
        g.setdefault(t["tenant"], []).append(t["grafanaOrg"])         # tenant group -> its teams
    g["auditors"] = list(all_orgs)                                    # auditors -> every org
    g["platform-admins"] = []                                         # server admin only
    return g


def main():
    with open(TENANTS_FILE) as f:
        cfg = yaml.safe_load(f)
    teams = cfg["teams"]

    wait_ready()

    log("== Provisioning team orgs (org + single-tenant datasources + dashboard) ==")
    org_ids = {}
    for t in teams:
        org_name, tenant_id = t["grafanaOrg"], t["tenantId"]
        oid = ensure_org(org_name)
        org_ids[org_name] = oid
        ensure_admin_in_org(oid)
        log(f"  org '{org_name}' -> X-Scope-OrgID '{tenant_id}'")
        mimir_uid, loki_uid = datasources_for_org(tenant_id)
        ensure_dashboard(oid, f"overview-{tenant_id}", f"{org_name} - Overview",
                         mimir_uid, loki_uid)

    log("== Provisioning users (server-admin + org membership from groups) ==")
    g2o = group_to_orgs(teams)
    for u in cfg.get("users", []):
        uid = ensure_user(u["login"], u["name"], u["password"], f"{u['login']}@otel-101.local")
        set_server_admin(uid, bool(u.get("serverAdmin", False)))
        orgs = sorted({o for grp in u.get("groups", []) for o in g2o.get(grp, [])})
        for org_name in orgs:
            add_user_to_org(org_ids[org_name], u["login"])
        log(f"  user '{u['login']}' -> orgs {orgs or '[server admin]'}")

    log("Grafana bootstrap complete.")


if __name__ == "__main__":
    main()
