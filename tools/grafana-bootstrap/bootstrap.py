#!/usr/bin/env python3
"""Idempotently provision Grafana OSS multi-tenancy from config/tenants.yaml.

Grafana OSS cannot provision orgs / teams / users (and can only provision datasources
into an org that already exists) from files, so we drive the HTTP API instead.

For each tenant this creates:
  - a Grafana Organization
  - Mimir / Loki / Tempo datasources in that org, each injecting the tenant's
    X-Scope-OrgID header (this is what scopes queries to the tenant's data)
  - one Team + one Folder per team, with the team granted Edit on its folder

Then it creates the users from tenants.yaml, assigns org roles (role "*" == every tenant
org, used for the cross-tenant `auditor`), sets server-admin where requested, and adds
users to their team.

Re-runnable: every create is guarded by a lookup first.

Env:
  GRAFANA_URL       (default http://localhost:3000)
  GRAFANA_USER      (default admin)
  GRAFANA_PASSWORD  (default admin)
  TENANTS_FILE      (default /config/tenants.yaml)
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

# Grafana folder/dashboard permission levels.
PERM_VIEW, PERM_EDIT, PERM_ADMIN = 1, 2, 4

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
    r = session.request(method, f"{BASE}{path}", timeout=30, **kw)
    return r


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
    # Race / already exists -> look up again.
    r = api("GET", f"/api/orgs/name/{name}")
    r.raise_for_status()
    return r.json()["id"]


def ensure_admin_in_org(org_id):
    # The admin must be a member of an org to switch into it and manage it.
    api("POST", f"/api/orgs/{org_id}/users",
        json={"loginOrEmail": USER, "role": "Admin"})
    r = api("POST", f"/api/user/using/{org_id}")
    r.raise_for_status()


# ---- Datasources ------------------------------------------------------------
def ensure_datasource(name, ds_type, url, org_id_header_value, is_default=False):
    payload = {
        "name": name,
        "type": ds_type,
        "access": "proxy",
        "url": url,
        "isDefault": is_default,
        "jsonData": {"httpHeaderName1": "X-Scope-OrgID"},
        "secureJsonData": {"httpHeaderValue1": org_id_header_value},
    }
    existing = api("GET", f"/api/datasources/name/{name}")
    if existing.ok:
        uid = existing.json()["uid"]
        r = api("PUT", f"/api/datasources/uid/{uid}", json=payload)
        r.raise_for_status()
        log(f"    updated datasource {name}")
    else:
        r = api("POST", "/api/datasources", json=payload)
        r.raise_for_status()
        log(f"    created datasource {name}")


# ---- Teams & folders --------------------------------------------------------
def ensure_team(name):
    r = api("GET", "/api/teams/search", params={"name": name})
    if r.ok and r.json().get("teams"):
        for t in r.json()["teams"]:
            if t["name"] == name:
                return t["id"]
    r = api("POST", "/api/teams", json={"name": name})
    if r.ok:
        tid = r.json()["teamId"]
        log(f"    created team '{name}' (id={tid})")
        return tid
    r = api("GET", "/api/teams/search", params={"name": name})
    r.raise_for_status()
    return r.json()["teams"][0]["id"]


def ensure_folder(title, uid):
    r = api("GET", f"/api/folders/{uid}")
    if not r.ok:
        r = api("POST", "/api/folders", json={"uid": uid, "title": title})
        if r.ok:
            log(f"    created folder '{title}'")
    return uid


def set_folder_team_permission(uid, team_id, permission):
    api("POST", f"/api/folders/{uid}/permissions",
        json={"items": [{"teamId": team_id, "permission": permission}]})


# ---- Users ------------------------------------------------------------------
def ensure_user(login, name, password):
    r = api("GET", "/api/users/lookup", params={"loginOrEmail": login})
    if r.ok:
        return r.json()["id"]
    r = api("POST", "/api/admin/users",
            json={"name": name, "login": login, "password": password})
    if r.ok:
        uid = r.json()["id"]
        log(f"  created user '{login}' (id={uid})")
        return uid
    r = api("GET", "/api/users/lookup", params={"loginOrEmail": login})
    r.raise_for_status()
    return r.json()["id"]


def set_server_admin(user_id, is_admin):
    api("PUT", f"/api/admin/users/{user_id}/permissions",
        json={"isGrafanaAdmin": is_admin})


def add_user_to_org(org_id, login, role):
    # Switch admin into the org first (org-scoped endpoint).
    api("POST", f"/api/user/using/{org_id}")
    r = api("POST", f"/api/orgs/{org_id}/users",
            json={"loginOrEmail": login, "role": role})
    if r.status_code == 409:  # already a member -> update role
        api("PATCH", f"/api/orgs/{org_id}/users",
            json={"loginOrEmail": login, "role": role})


def add_user_to_team(org_id, team_id, user_id):
    api("POST", f"/api/user/using/{org_id}")
    api("POST", f"/api/teams/{team_id}/members", json={"userId": user_id})


# ---- Main -------------------------------------------------------------------
def main():
    with open(TENANTS_FILE) as f:
        cfg = yaml.safe_load(f)

    wait_ready()

    # org name -> org id ; (org id, team name) -> team id
    org_ids = {}
    team_ids = {}

    log("== Provisioning tenants (orgs, datasources, teams, folders) ==")
    for tenant in cfg["tenants"]:
        org_name = tenant["grafanaOrg"]
        org_val = tenant["orgId"]
        oid = ensure_org(org_name)
        org_ids[org_name] = oid
        ensure_admin_in_org(oid)
        log(f"  org '{org_name}' -> X-Scope-OrgID '{org_val}'")

        ensure_datasource("Mimir", "prometheus", MIMIR_URL, org_val, is_default=True)
        ensure_datasource("Loki", "loki", LOKI_URL, org_val)
        ensure_datasource("Tempo", "tempo", TEMPO_URL, org_val)

        for team in tenant["teams"]:
            tname = team["displayName"]
            tid = ensure_team(tname)
            team_ids[(oid, tname)] = tid
            folder_uid = f"{tenant['name']}-{team['name']}"
            ensure_folder(tname, folder_uid)
            set_folder_team_permission(folder_uid, tid, PERM_EDIT)

    log("== Provisioning users (roles, server-admin, team membership) ==")
    tenant_orgs = [t["grafanaOrg"] for t in cfg["tenants"]]
    for u in cfg.get("users", []):
        uid = ensure_user(u["login"], u["name"], u["password"])
        set_server_admin(uid, bool(u.get("serverAdmin", False)))
        for entry in u.get("orgs", []):
            targets = tenant_orgs if entry["org"] == "*" else [entry["org"]]
            for org_name in targets:
                oid = org_ids[org_name]
                add_user_to_org(oid, u["login"], entry["role"])
                if entry.get("team"):
                    tid = team_ids.get((oid, entry["team"]))
                    if tid:
                        add_user_to_team(oid, tid, uid)
        log(f"  user '{u['login']}' configured")

    log("Grafana bootstrap complete.")


if __name__ == "__main__":
    main()
