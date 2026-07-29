# =============================================================================
# OpenTelemetry on Kubernetes 101 — orchestration
#
#   make up      spin up the whole stack on minikube (from scratch)
#   make down    remove the stack (keep the minikube cluster)
#   make nuke    delete the minikube cluster entirely
#   make reset   nuke + up
#
# Run `make help` for the full list.
# =============================================================================
SHELL := /bin/bash
.DEFAULT_GOAL := help

# ---- Config (override on the CLI, e.g. `make up MINIKUBE_MEMORY=8192`) ------
MINIKUBE_PROFILE ?= otel-101
MINIKUBE_CPUS    ?= 6
MINIKUBE_MEMORY  ?= 12288
MINIKUBE_DRIVER  ?= docker
K8S_VERSION      ?= stable

NS_OBS      := observability
NS_OPERATOR := opentelemetry-operator-system

# Pinned chart versions (see README "Versions").
VER_RUSTFS   := 0.11.0
VER_LOKI     := 18.7.0
VER_TEMPO    := 2.2.3
VER_GRAFANA  := 12.10.0
VER_OTEL_OP  := 0.120.0

# Keycloak NodePort. Reached at http://$(minikube ip):$(KEYCLOAK_NODEPORT) by both browser and pods.
KEYCLOAK_NODEPORT := 30080

KUBECTL := kubectl
HELM    := helm

# team namespace : service : tenant : team   (mirror of config/tenants.yaml)
TEAMS := \
	tenant-a-team-1:payments:tenant-a:team-1 \
	tenant-a-team-2:onboarding:tenant-a:team-2 \
	tenant-b-team-1:trading:tenant-b:team-1 \
	tenant-b-team-2:reporting:tenant-b:team-2

# =============================================================================
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---- Cluster ----------------------------------------------------------------
check-tools: ## Verify required CLIs are installed
	@for c in $(HELM) $(KUBECTL) docker; do \
		command -v $$c >/dev/null || { echo "ERROR: '$$c' not found in PATH"; exit 1; }; \
	done
	@command -v minikube >/dev/null || { \
		echo "ERROR: 'minikube' not found. Install it:"; \
		echo "  https://minikube.sigs.k8s.io/docs/start/"; \
		echo "  (linux/amd64: curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64 && sudo install minikube-linux-amd64 /usr/local/bin/minikube)"; \
		exit 1; }
	@echo "All tools present."

minikube-start: check-tools ## Start the minikube cluster
	@minikube status -p $(MINIKUBE_PROFILE) >/dev/null 2>&1 && echo "minikube '$(MINIKUBE_PROFILE)' already running." || \
		minikube start -p $(MINIKUBE_PROFILE) \
			--driver=$(MINIKUBE_DRIVER) \
			--cpus=$(MINIKUBE_CPUS) --memory=$(MINIKUBE_MEMORY) \
			--kubernetes-version=$(K8S_VERSION)
	@minikube -p $(MINIKUBE_PROFILE) addons enable metrics-server >/dev/null 2>&1 || true
	@minikube -p $(MINIKUBE_PROFILE) addons enable ingress >/dev/null 2>&1 || true

repos: ## Add/update the Helm repositories
	@$(HELM) repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
	@$(HELM) repo add grafana-community https://grafana-community.github.io/helm-charts >/dev/null 2>&1 || true
	@$(HELM) repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
	@$(HELM) repo add rustfs https://charts.rustfs.com >/dev/null 2>&1 || true
	@$(HELM) repo update >/dev/null

namespaces: ## Create all namespaces
	@$(KUBECTL) apply -f deploy/00-namespaces/namespaces.yaml

# ---- Layers -----------------------------------------------------------------
storage: namespaces ## Deploy RustFS (S3) and create buckets
	@echo ">> RustFS (S3-compatible object storage)"
	@$(KUBECTL) apply -f deploy/10-storage/s3-credentials.yaml
	@$(HELM) upgrade --install rustfs rustfs/rustfs --version $(VER_RUSTFS) \
		-n $(NS_OBS) -f deploy/10-storage/rustfs-values.yaml --wait --timeout 5m
	@echo ">> Creating buckets (mimir, loki, tempo)"
	@$(KUBECTL) -n $(NS_OBS) delete job rustfs-create-buckets --ignore-not-found
	@$(KUBECTL) apply -f deploy/10-storage/bucket-create-job.yaml
	@$(KUBECTL) -n $(NS_OBS) wait --for=condition=complete job/rustfs-create-buckets --timeout=5m

backends: repos ## Deploy Mimir, Loki, Tempo
	@echo ">> Mimir (single binary)"
	@$(KUBECTL) create configmap mimir-config -n $(NS_OBS) \
		--from-file=mimir.yaml=deploy/20-backends/mimir/mimir.yaml \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -
	@$(KUBECTL) apply -f deploy/20-backends/mimir/mimir.deployment.yaml
	@$(KUBECTL) -n $(NS_OBS) rollout restart deployment/mimir >/dev/null 2>&1 || true
	@echo ">> Loki (monolithic)"
	@$(HELM) upgrade --install loki grafana-community/loki --version $(VER_LOKI) \
		-n $(NS_OBS) -f deploy/20-backends/loki/values.yaml --wait --timeout 8m
	@echo ">> Tempo (single binary)"
	@$(HELM) upgrade --install tempo grafana-community/tempo --version $(VER_TEMPO) \
		-n $(NS_OBS) -f deploy/20-backends/tempo/values.yaml --wait --timeout 5m
	@$(KUBECTL) -n $(NS_OBS) rollout status deployment/mimir --timeout=5m

operator: repos ## Install the OpenTelemetry Operator
	@echo ">> OpenTelemetry Operator"
	@$(HELM) upgrade --install opentelemetry-operator open-telemetry/opentelemetry-operator \
		--version $(VER_OTEL_OP) -n $(NS_OPERATOR) --create-namespace \
		-f deploy/30-operator/operator-values.yaml --wait --timeout 5m
	@$(KUBECTL) -n $(NS_OPERATOR) rollout status deploy -l app.kubernetes.io/name=opentelemetry-operator --timeout=5m
	@echo "   waiting for the operator webhook to settle..."
	@sleep 15

gateway-config: ## Regenerate deploy/40-collectors/gateway.yaml from config/tenants.yaml
	@echo ">> Generating single-gateway config from tenants.yaml"
	@docker run --rm -v "$$PWD":/w -w /w python:3.12-slim \
		sh -c 'pip install -q pyyaml && python tools/generate-gateway-config.py config/tenants.yaml deploy/40-collectors/gateway.yaml'
	@echo "   Generated deploy/40-collectors/gateway.yaml"

collectors: gateway-config ## Deploy single gateway collector + Instrumentation CRs + filelog sidecars
	@echo ">> RBAC for the gateway collector (k8sattributes + Prometheus SD)"
	@$(KUBECTL) apply -f deploy/40-collectors/gateway-rbac.yaml
	@echo ">> Single gateway collector (routing connector, all teams)"
	@$(KUBECTL) apply -f deploy/40-collectors/gateway.yaml
	@echo ">> Per-team Instrumentation CRs + filelog sidecars"
	@for t in $(TEAMS); do \
		ns=$${t%%:*}; rest=$${t#*:}; svc=$${rest%%:*}; rest=$${rest#*:}; tenant=$${rest%%:*}; team=$${rest#*:}; \
		echo "   $$tenant/$$team -> instrumentation + filelog-sidecar (ns=$$ns)"; \
		sed -e "s/__NAMESPACE__/$$ns/g" \
		    -e "s/__TENANT__/$$tenant/g" -e "s/__TEAM__/$$team/g" \
		    deploy/40-collectors/instrumentation.template.yaml | $(KUBECTL) apply -f - ; \
		sed -e "s/__NAMESPACE__/$$ns/g" -e "s/__SERVICE__/$$svc/g" \
		    -e "s/__TENANT__/$$tenant/g" -e "s/__TEAM__/$$team/g" \
		    deploy/40-collectors/sidecar.template.yaml | $(KUBECTL) apply -f - ; \
	done

keycloak: namespaces ## Deploy Keycloak (OIDC) with a realm generated from tenants.yaml
	@echo ">> Keycloak (OIDC identity provider)"
	@mkdir -p .gen
	@IP=$$(minikube -p $(MINIKUBE_PROFILE) ip); \
	 docker run --rm -e GRAFANA_INGRESS_HOST=grafana.$$IP.nip.io -v "$$PWD":/w -w /w python:3.12-slim \
		sh -c 'pip install -q pyyaml && python tools/keycloak-realm/generate_realm.py config/tenants.yaml /w/.gen/otel-101-realm.json'
	@$(KUBECTL) create configmap keycloak-realm -n $(NS_OBS) \
		--from-file=otel-101-realm.json=.gen/otel-101-realm.json \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -
	@$(KUBECTL) apply -f deploy/45-keycloak/keycloak.yaml
	@$(KUBECTL) -n $(NS_OBS) rollout restart deployment/keycloak >/dev/null 2>&1 || true
	@$(KUBECTL) -n $(NS_OBS) rollout status deployment/keycloak --timeout=5m

grafana: repos ## Deploy Grafana (Keycloak SSO) and bootstrap orgs/teams/users/datasources
	@echo ">> Grafana"
	@IP=$$(minikube -p $(MINIKUBE_PROFILE) ip); \
	 GHOST=grafana.$$IP.nip.io; KHOST=keycloak.$$IP.nip.io; \
	 mkdir -p .gen; \
	 printf 'grafana.ini:\n  server:\n    root_url: http://%s/\n  auth.generic_oauth:\n    auth_url: http://%s/realms/otel-101/protocol/openid-connect/auth\n' \
	   "$$GHOST" "$$KHOST" > .gen/grafana-oauth.yaml; \
	 echo "   Grafana root_url: http://$$GHOST | Keycloak auth: http://$$KHOST"; \
	 $(HELM) upgrade --install grafana grafana-community/grafana --version $(VER_GRAFANA) \
		-n $(NS_OBS) -f deploy/50-grafana/values.yaml -f .gen/grafana-oauth.yaml --wait --timeout 5m
	@$(KUBECTL) create configmap grafana-bootstrap-script -n $(NS_OBS) \
		--from-file=bootstrap.py=tools/grafana-bootstrap/bootstrap.py \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -
	@$(KUBECTL) create configmap tenants-config -n $(NS_OBS) \
		--from-file=tenants.yaml=config/tenants.yaml \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -
	@echo ">> Bootstrapping Grafana tenants"
	@$(KUBECTL) -n $(NS_OBS) delete job grafana-bootstrap --ignore-not-found
	@$(KUBECTL) apply -f deploy/50-grafana/bootstrap-job.yaml
	@$(KUBECTL) -n $(NS_OBS) wait --for=condition=complete job/grafana-bootstrap --timeout=5m
	@# org_mapping resolves org names -> IDs at startup, but the bootstrap creates those orgs
	@# only just now, so restart Grafana to re-resolve (else SSO users land in no org).
	@echo ">> Restarting Grafana so org_mapping picks up the new orgs"
	@$(KUBECTL) -n $(NS_OBS) rollout restart deployment/grafana
	@$(KUBECTL) -n $(NS_OBS) rollout status deployment/grafana --timeout=5m

build: ## Build the synthetic app image into minikube's docker
	@echo ">> Building otel-synthetic:latest inside minikube"
	@eval $$(minikube -p $(MINIKUBE_PROFILE) docker-env) && \
		docker build -t otel-synthetic:latest apps/synthetic

workloads: build ## Deploy the per-team synthetic workloads
	@echo ">> Synthetic workloads (auto-instrumented)"
	@for t in $(TEAMS); do \
		ns=$${t%%:*}; rest=$${t#*:}; svc=$${rest%%:*}; rest=$${rest#*:}; tenant=$${rest%%:*}; team=$${rest#*:}; \
		echo "   $$tenant/$$team -> $$svc (ns=$$ns)"; \
		sed -e "s/__NAMESPACE__/$$ns/g" -e "s/__SERVICE__/$$svc/g" \
		    -e "s/__TENANT__/$$tenant/g" -e "s/__TEAM__/$$team/g" \
		    deploy/60-workloads/workload.template.yaml | $(KUBECTL) apply -f - ; \
	done

ingress: ## Create nip.io Ingress for Grafana + Keycloak (no port-forward needed)
	@echo ">> Ingress (nip.io)"
	@echo "   waiting for the ingress-nginx admission webhook to be ready..."
	@$(KUBECTL) -n ingress-nginx wait --for=condition=ready pod \
		-l app.kubernetes.io/component=controller --timeout=180s >/dev/null 2>&1 || true
	@IP=$$(minikube -p $(MINIKUBE_PROFILE) ip); \
	 sed -e "s/__GRAFANA_HOST__/grafana.$$IP.nip.io/g" \
	     -e "s/__KEYCLOAK_HOST__/keycloak.$$IP.nip.io/g" \
		 -e "s/__RUSTFS_HOST__/rustfs.$$IP.nip.io/g" \
	     deploy/80-ingress/ingress.template.yaml | $(KUBECTL) apply -f -; \
	 echo "   Grafana:  http://grafana.$$IP.nip.io"; \
	 echo "   Keycloak: http://keycloak.$$IP.nip.io"; \
	 echo "   RustFS:   http://rustfs.$$IP.nip.io"

# ---- Aggregate --------------------------------------------------------------
up: minikube-start storage backends operator collectors keycloak grafana ingress workloads ## Bring the whole stack up (from scratch)
	@echo ""
	@echo "================================================================"
	@echo " Stack is up. Next:"
	@echo "   make status          # check everything is Ready"
	@echo "   make keycloak-info   # Grafana/Keycloak URLs (nip.io ingress) + demo logins"
	@echo "================================================================"

# ---- Ops --------------------------------------------------------------------
status: ## Show pods across all namespaces + collector CRs
	@echo "== observability =="; $(KUBECTL) -n $(NS_OBS) get pods -o wide 2>/dev/null || true
	@echo "== operator ==";      $(KUBECTL) -n $(NS_OPERATOR) get pods 2>/dev/null || true
	@for t in $(TEAMS); do ns=$${t%%:*}; echo "== $$ns =="; $(KUBECTL) -n $$ns get pods 2>/dev/null || true; done
	@echo "== collectors =="; $(KUBECTL) -n $(NS_OBS) get opentelemetrycollectors 2>/dev/null || true

grafana-forward: ## Port-forward Grafana to http://localhost:3000 (local-admin fallback only)
	@echo "Grafana on http://localhost:3000 (use admin/admin here)."
	@echo "For Keycloak SSO use the ingress URL instead (make keycloak-info) — SSO redirects there."
	@$(KUBECTL) -n $(NS_OBS) port-forward svc/grafana 3000:80

keycloak-info: ## Print the Grafana/Keycloak URLs and demo logins
	@IP=$$(minikube -p $(MINIKUBE_PROFILE) ip); \
	 echo "Grafana:       http://grafana.$$IP.nip.io      (local fallback: admin/admin)"; \
	 echo "Keycloak:      http://keycloak.$$IP.nip.io     (admin console /admin , admin/admin)"; \
	 echo "RustFS:        http://rustfs.$$IP.nip.io        (otel-demo / otel-demo-secret-key-change-me)"; \
	 echo "Realm:         otel-101"; \
	 echo ""; \
	 echo "Each team is its own tenant+org, so a user sees ONLY the team(s) they can access:"; \
	 echo "   alice           -> Payments team only"; \
	 echo "   bob             -> Onboarding team only"; \
	 echo "   carol           -> Trading team only"; \
	 echo "   dave            -> both Tenant A teams (switch orgs)"; \
	 echo "   auditor         -> all four teams (switch orgs)"; \
	 echo "   platform-admin  -> Grafana server admin (every org)"; \
	 echo "Passwords are in config/tenants.yaml (users[].password)."

rustfs-console: ## Port-forward the RustFS console to http://localhost:9001
	@echo "RustFS console on http://localhost:9001 (login: otel-demo / otel-demo-secret-key-change-me)"
	@$(KUBECTL) -n $(NS_OBS) port-forward svc/rustfs-svc 9001:9001

smoke: ## Validate RustFS accepts S3 writes and backends are healthy
	@bash scripts/smoke.sh $(MINIKUBE_PROFILE) $(NS_OBS)

logs-bootstrap: ## Show the Grafana bootstrap job logs
	@$(KUBECTL) -n $(NS_OBS) logs job/grafana-bootstrap

# ---- Teardown ---------------------------------------------------------------
down: ## Remove the stack but keep the minikube cluster
	@echo ">> Removing workloads, collectors, backends, storage, grafana, operator"
	@$(KUBECTL) -n $(NS_OBS) delete ingress grafana keycloak --ignore-not-found 2>/dev/null || true
	@for t in $(TEAMS); do ns=$${t%%:*}; $(KUBECTL) delete ns $$ns --ignore-not-found; done
	@# gateway CR (observability ns) + Instrumentation/sidecar CRs (team namespaces) all removed with their namespaces
	@# ClusterRole + ClusterRoleBinding are cluster-scoped; delete them explicitly.
	@$(KUBECTL) delete clusterrole otel-gateway --ignore-not-found 2>/dev/null || true
	@$(KUBECTL) delete clusterrolebinding otel-gateway --ignore-not-found 2>/dev/null || true
	@$(HELM) uninstall grafana -n $(NS_OBS) 2>/dev/null || true
	@$(KUBECTL) delete -f deploy/45-keycloak/keycloak.yaml --ignore-not-found 2>/dev/null || true
	@$(HELM) uninstall tempo -n $(NS_OBS) 2>/dev/null || true
	@$(HELM) uninstall loki -n $(NS_OBS) 2>/dev/null || true
	@$(KUBECTL) delete -f deploy/20-backends/mimir/mimir.deployment.yaml --ignore-not-found 2>/dev/null || true
	@$(HELM) uninstall rustfs -n $(NS_OBS) 2>/dev/null || true
	@$(HELM) uninstall opentelemetry-operator -n $(NS_OPERATOR) 2>/dev/null || true
	@$(KUBECTL) delete ns $(NS_OBS) $(NS_OPERATOR) --ignore-not-found

nuke: ## Delete the entire minikube cluster
	@minikube delete -p $(MINIKUBE_PROFILE)

reset: nuke up ## Destroy and rebuild everything from scratch

.PHONY: help check-tools minikube-start repos namespaces storage backends operator \
	gateway-config collectors keycloak grafana build workloads ingress up status grafana-forward \
	keycloak-info rustfs-console smoke logs-bootstrap down nuke reset
