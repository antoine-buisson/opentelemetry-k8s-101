#!/usr/bin/env python3
"""Generate the Keycloak realm import JSON from config/tenants.yaml.

Keeps tenants.yaml the single source of truth: the same users (login/password/orgs/roles)
that drive the Grafana bootstrap also drive the Keycloak users and their group membership.

Group taxonomy (bare names; Grafana maps them to org+role via org_mapping):
    tenant-a-admins / -editors / -viewers   -> org "Tenant A" as Admin / Editor / Viewer
    tenant-b-admins / -editors / -viewers   -> org "Tenant B" as ...
    auditors                                 -> Viewer in every tenant org   (org "*")
    platform-admins                          -> Admin everywhere + GrafanaAdmin (server admin)

Derivation from a user's orgs[] entries:
    {org: "Tenant A", role: "Editor"}          -> tenant-a-editors
    {org: "*", role: "Viewer"}                  -> auditors
    {org: "*", role: "Admin"} + serverAdmin    -> platform-admins

Usage: generate_realm.py <tenants.yaml> [output.json]   (stdout if output omitted)
"""
import json
import os
import sys

import yaml

REALM = "otel-101"
CLIENT_ID = "grafana"
CLIENT_SECRET = "grafana-demo-secret"


def grafana_redirects():
    """Grafana OIDC redirect URIs. localhost:3000 covers `make grafana-forward`; if the
    ingress host is known (GRAFANA_INGRESS_HOST), register it too so SSO works via ingress."""
    uris = ["http://localhost:3000/login/generic_oauth"]
    host = os.environ.get("GRAFANA_INGRESS_HOST")
    if host:
        uris.append(f"http://{host}/login/generic_oauth")
    return uris


def build_realm(cfg):
    teams = cfg["teams"]

    # Group taxonomy = one group per team, one per tenant, plus the two cross-cutting groups.
    # Grafana's org_mapping (in the Grafana values) maps each of these to one or more orgs.
    group_names = [t["group"] for t in teams]                         # payments, onboarding, ...
    group_names += sorted({t["tenant"] for t in teams})               # tenant-a, tenant-b
    group_names += ["auditors", "platform-admins"]
    groups = [{"name": g, "path": f"/{g}"} for g in group_names]

    users = []
    for u in cfg.get("users", []):
        member_of = [f"/{g}" for g in u.get("groups", [])]
        # Keycloak's default user profile rejects names with characters like ( ) /, which
        # would dynamically trigger a VERIFY_PROFILE action ("Account is not fully set up").
        # Use only the clean leading part of the display name (before any parenthetical).
        clean = u["name"].split("(")[0].strip()
        first, _, last = clean.partition(" ")
        users.append({
            "username": u["login"],
            "enabled": True,
            "email": f"{u['login']}@otel-101.local",
            "emailVerified": True,
            "firstName": first or u["login"],
            "lastName": last or "Demo",
            "credentials": [
                {"type": "password", "value": u["password"], "temporary": False}],
            "groups": member_of,
        })

    client = {
        "clientId": CLIENT_ID,
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "secret": CLIENT_SECRET,
        "standardFlowEnabled": True,
        # Enabled so the demo can fetch a token headlessly (password grant) to verify groups.
        "directAccessGrantsEnabled": True,
        "redirectUris": grafana_redirects(),
        "webOrigins": ["+"],
        "protocolMappers": [{
            "name": "groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "config": {
                "claim.name": "groups",
                "full.path": "false",          # bare names, e.g. "tenant-a-editors"
                "id.token.claim": "true",
                "access.token.claim": "true",
                "userinfo.token.claim": "true",
            },
        }],
    }

    return {
        "realm": REALM,
        "enabled": True,
        # Node IP (non-localhost) is plain HTTP in this demo; don't force TLS.
        "sslRequired": "none",
        "groups": groups,
        "clients": [client],
        "users": users,
    }


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: generate_realm.py <tenants.yaml> [output.json]")
    with open(sys.argv[1]) as f:
        cfg = yaml.safe_load(f)
    realm = build_realm(cfg)
    out = json.dumps(realm, indent=2)
    if len(sys.argv) >= 3:
        with open(sys.argv[2], "w") as f:
            f.write(out)
    else:
        print(out)


if __name__ == "__main__":
    main()
