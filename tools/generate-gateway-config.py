#!/usr/bin/env python3
"""
Generate deploy/40-collectors/gateway.yaml from config/tenants.yaml.

Produces a single OpenTelemetryCollector (gateway) that:
  - Receives OTLP (auto-instrumented traces), Jaeger (legacy business spans),
    and Prometheus scrapes (prometheus_client metrics endpoint on :9090).
  - Enriches signals with pod metadata via k8sattributes.
  - Routes signals to per-tenant backends using the routing connector, keyed on
    the resource attributes "tenant" and "team" (set either by the Instrumentation
    CR's resourceAttributes or extracted from pod labels by k8sattributes).
  - Stamps the correct X-Scope-OrgID header on each export so Mimir, Loki, and
    Tempo maintain hard per-team isolation.

Adding a new team to config/tenants.yaml and re-running `make collectors` is
sufficient — no manual YAML editing required.

Usage:
    python tools/generate-gateway-config.py [tenants.yaml] [output.yaml]
"""
import sys
import yaml


def load_tenants(path):
    with open(path) as f:
        return yaml.safe_load(f)


def build_gateway(teams):
    namespaces = [t["namespace"] for t in teams]

    # Per-tenant exporters: 3 backends × N teams.
    exporters = {}
    for t in teams:
        ns = t["namespace"]
        tid = t["tenantId"]
        exporters[f"otlphttp/tempo-{ns}"] = {
            "endpoint": "http://tempo.observability.svc.cluster.local:4318",
            "headers": {"X-Scope-OrgID": tid},
        }
        exporters[f"otlphttp/mimir-{ns}"] = {
            "endpoint": "http://mimir.observability.svc.cluster.local:8080/otlp",
            "headers": {"X-Scope-OrgID": tid},
        }
        exporters[f"otlphttp/loki-{ns}"] = {
            "endpoint": "http://loki.observability.svc.cluster.local:3100/otlp",
            "headers": {"X-Scope-OrgID": tid},
        }
    exporters["debug/unknown"] = {"verbosity": "basic"}

    # Routing connector tables — one per signal type, same routing logic.
    def routing_table(signal):
        return [
            {
                "statement": (
                    f'route() where resource.attributes["tenant"] == "{t["tenant"]}"'
                    f' and resource.attributes["team"] == "{t["team"]}"'
                ),
                "pipelines": [f"{signal}/{t['namespace']}"],
            }
            for t in teams
        ]

    connectors = {
        "routing/traces": {
            "default_pipelines": ["traces/unknown"],
            "error_mode": "ignore",
            "table": routing_table("traces"),
        },
        "routing/metrics": {
            "default_pipelines": ["metrics/unknown"],
            "error_mode": "ignore",
            "table": routing_table("metrics"),
        },
        "routing/logs": {
            "default_pipelines": ["logs/unknown"],
            "error_mode": "ignore",
            "table": routing_table("logs"),
        },
    }

    # Pipelines: 3 input + (3 × N tenant output) + 3 fallback.
    pipelines = {
        "traces/in": {
            "receivers": ["otlp", "jaeger"],
            "processors": ["memory_limiter", "k8sattributes", "batch"],
            "exporters": ["routing/traces"],
        },
        "metrics/in": {
            "receivers": ["otlp", "prometheus"],
            "processors": ["memory_limiter", "k8sattributes", "transform/prometheus_labels", "batch"],
            "exporters": ["routing/metrics"],
        },
        "logs/in": {
            "receivers": ["otlp"],
            "processors": ["memory_limiter", "k8sattributes", "batch"],
            "exporters": ["routing/logs"],
        },
        # Catch-all for signals whose tenant/team attributes are missing or unrecognised.
        "traces/unknown": {"receivers": ["routing/traces"], "exporters": ["debug/unknown"]},
        "metrics/unknown": {"receivers": ["routing/metrics"], "exporters": ["debug/unknown"]},
        "logs/unknown": {"receivers": ["routing/logs"], "exporters": ["debug/unknown"]},
    }
    for t in teams:
        ns = t["namespace"]
        pipelines[f"traces/{ns}"] = {
            "receivers": ["routing/traces"],
            "exporters": [f"otlphttp/tempo-{ns}"],
        }
        pipelines[f"metrics/{ns}"] = {
            "receivers": ["routing/metrics"],
            "exporters": [f"otlphttp/mimir-{ns}"],
        }
        pipelines[f"logs/{ns}"] = {
            "receivers": ["routing/logs"],
            "exporters": [f"otlphttp/loki-{ns}"],
        }

    config = {
        "extensions": {
            "health_check": {},
        },
        "receivers": {
            # 1. OTLP — auto-instrumented traces from Flask/requests via the Instrumentation CR.
            "otlp": {
                "protocols": {
                    "grpc": {"endpoint": "0.0.0.0:4317"},
                    "http": {"endpoint": "0.0.0.0:4318"},
                },
            },
            # 2. Jaeger — legacy business spans emitted by the manual TracerProvider in app.py.
            "jaeger": {
                "protocols": {
                    "thrift_http": {"endpoint": "0.0.0.0:14268"},
                    "grpc": {"endpoint": "0.0.0.0:14250"},
                },
            },
            # 3. Prometheus — scrapes the prometheus_client /metrics endpoint on :9090 in each pod.
            "prometheus": {
                "config": {
                    "scrape_configs": [
                        {
                            "job_name": "synthetic-services",
                            "kubernetes_sd_configs": [
                                {
                                    "role": "pod",
                                    "namespaces": {"names": namespaces},
                                }
                            ],
                            "relabel_configs": [
                                # Keep only pods that opt in to scraping.
                                {
                                    "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_scrape"],
                                    "action": "keep",
                                    "regex": "true",
                                },
                                # Use the annotated metrics path (defaults to /metrics).
                                {
                                    "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_path"],
                                    "action": "replace",
                                    "target_label": "__metrics_path__",
                                    "regex": "(.+)",
                                },
                                # Rewrite the scrape address to use the annotated port.
                                # Use $${1}:$${2} so the OTel collector config expander
                                # treats $$ as an escaped $ and passes ${1}:${2} to Prometheus.
                                {
                                    "source_labels": [
                                        "__address__",
                                        "__meta_kubernetes_pod_annotation_prometheus_io_port",
                                    ],
                                    "action": "replace",
                                    "regex": r"([^:]+)(?::\d+)?;(\d+)",
                                    "replacement": r"$${1}:$${2}",
                                    "target_label": "__address__",
                                },
                                # Carry tenant/team as metric labels so transform/prometheus_labels
                                # can promote them to resource attributes for the routing connector.
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_otel_k8s_101_tenant"],
                                    "target_label": "tenant",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_otel_k8s_101_team"],
                                    "target_label": "team",
                                },
                                # Preserve namespace and pod name for cardinality/debugging.
                                {
                                    "source_labels": ["__meta_kubernetes_namespace"],
                                    "target_label": "k8s_namespace",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_name"],
                                    "target_label": "k8s_pod",
                                },
                            ],
                        }
                    ]
                }
            },
        },
        "processors": {
            # memory_limiter must be first to apply backpressure before any enrichment work.
            "memory_limiter": {
                "check_interval": "5s",
                "limit_percentage": 75,
                "spike_limit_percentage": 25,
            },
            # k8sattributes enriches all signals with pod/deployment metadata and extracts
            # tenant + team from pod labels — the routing connector uses these attributes.
            "k8sattributes": {
                "passthrough": False,
                "auth_type": "serviceAccount",
                "pod_association": [
                    {"sources": [{"from": "resource_attribute", "name": "k8s.pod.ip"}]},
                    {"sources": [{"from": "connection"}]},
                ],
                "extract": {
                    "metadata": [
                        "k8s.pod.name",
                        "k8s.pod.uid",
                        "k8s.deployment.name",
                        "k8s.namespace.name",
                        "k8s.node.name",
                    ],
                    "labels": [
                        {"tag_name": "tenant", "key": "otel-k8s-101/tenant", "from": "pod"},
                        {"tag_name": "team",   "key": "otel-k8s-101/team",   "from": "pod"},
                    ],
                },
            },
            # For Prometheus-scraped metrics, tenant and team arrive as metric-level labels
            # (set via relabeling above). Promote them to resource attributes so the routing
            # connector can match on resource.attributes["tenant"] / resource.attributes["team"].
            "transform/prometheus_labels": {
                "metric_statements": [
                    {
                        "context": "datapoint",
                        "statements": [
                            'set(resource.attributes["tenant"], attributes["tenant"]) where attributes["tenant"] != nil',
                            'set(resource.attributes["team"], attributes["team"]) where attributes["team"] != nil',
                            'delete_key(attributes, "tenant")',
                            'delete_key(attributes, "team")',
                        ],
                    }
                ]
            },
            # batch is last: pack data after all enrichment is done.
            "batch": {},
        },
        "connectors": connectors,
        "exporters": exporters,
        "service": {
            "extensions": ["health_check"],
            "pipelines": pipelines,
        },
    }

    return {
        "apiVersion": "opentelemetry.io/v1beta1",
        "kind": "OpenTelemetryCollector",
        "metadata": {
            "name": "gateway",
            "namespace": "observability",
            "labels": {
                "app.kubernetes.io/component": "opentelemetry-collector",
                "app.kubernetes.io/name": "otel-gateway",
            },
        },
        "spec": {
            "mode": "deployment",
            "replicas": 2,
            "serviceAccount": "otel-gateway",
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"memory": "512Mi"},
            },
            "config": config,
        },
    }


def main():
    tenants_path = sys.argv[1] if len(sys.argv) > 1 else "config/tenants.yaml"
    output_path  = sys.argv[2] if len(sys.argv) > 2 else "deploy/40-collectors/gateway.yaml"

    data  = load_tenants(tenants_path)
    teams = data["teams"]
    cr    = build_gateway(teams)

    header = (
        "# Auto-generated by tools/generate-gateway-config.py from config/tenants.yaml.\n"
        "# Re-run via: make collectors   (or: make gateway-config)\n"
        "#\n"
        "# Single gateway collector: receives OTLP + Jaeger + Prometheus scrape,\n"
        "# routes signals to per-tenant Mimir/Loki/Tempo backends via the routing connector.\n\n"
    )

    with open(output_path, "w") as f:
        f.write(header)
        yaml.dump(cr, f, default_flow_style=False, sort_keys=False, allow_unicode=True, width=10000)

    print(f"Generated {output_path} ({len(teams)} teams, {len(teams) * 3} exporters)")


if __name__ == "__main__":
    main()
