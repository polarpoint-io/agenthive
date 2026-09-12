# AgentHive — local build and validation, same shape as the rest of
# polarpoint-io (see helm-mirofish).

SHELL         := /bin/bash
.DEFAULT_GOAL := help

CHART         := helm/agenthive
CHART_NAME    := agenthive

REGISTRY      ?= ghcr.io
OWNER         ?= polarpoint-io
TAG           ?= dev
IMAGE         := $(REGISTRY)/$(OWNER)/agenthive:$(TAG)

NAMESPACE     ?= agenthive
RELEASE       ?= agenthive
KUBE_VERSIONS := 1.29.0 1.31.0

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------- test --
.PHONY: test
test: ## Run the pytest suite against SQLite
	pip install -r requirements.txt
	pytest -v

# ------------------------------------------------------------------ image --
.PHONY: image
image: ## Build the image locally
	docker build -t $(IMAGE) .

.PHONY: push
push: ## Push the image
	docker push $(IMAGE)

# ------------------------------------------------------------------- hero --
.PHONY: hero
hero: ## Regenerate static/hero.svg
	python3 hack/build_hero.py

.PHONY: diagrams
diagrams: ## Regenerate the C4 diagrams under docs/diagrams/ (needs Java + Graphviz)
	java -DGRAPHVIZ_DOT="$$(command -v dot)" -jar plantuml.jar -tsvg docs/diagrams/*.puml

# ------------------------------------------------------------------- chart --
.PHONY: lint
lint: ## helm lint (strict)
	helm lint $(CHART) --strict

.PHONY: template
template: ## Render the chart with default values
	helm template $(RELEASE) $(CHART)

.PHONY: validate
validate: ## Render every ci/ scenario and validate against Kubernetes schemas
	@set -euo pipefail; fail=0; \
	for f in $(CHART)/ci/*-values.yaml $(CHART)/values-postgres-example.yaml; do \
	  for kube in $(KUBE_VERSIONS); do \
	    printf '%-40s k8s %-8s ' "$$(basename $$f)" "$$kube"; \
	    helm template ci-test $(CHART) -f "$$f" \
	      | kubeconform -strict -summary -ignore-missing-schemas -kubernetes-version "$$kube" - \
	      || fail=1; \
	  done; \
	done; exit $$fail

.PHONY: validate-negative
validate-negative: ## Confirm the chart rejects values it cannot support
	@set -uo pipefail; \
	for args in "--set replicaCount=3" \
	            "--set database.type=postgres" \
	            "--set autoscaling.enabled=true" \
	            "--set tls.enabled=true" \
	            "--set ingress.enabled=true --set ingress.tls.enabled=true"; do \
	  if helm template bad $(CHART) $$args >/dev/null 2>&1; then \
	    echo "FAIL: expected '$$args' to be rejected"; exit 1; \
	  fi; \
	  echo "OK: rejected $$args"; \
	done

.PHONY: check
check: lint validate validate-negative ## Everything CI runs for the chart

.PHONY: package
package: ## Package the chart into ./dist
	helm package $(CHART) --destination dist

# --------------------------------------------------------------- lifecycle --
.PHONY: install
install: ## Install the release (override with NAMESPACE=, RELEASE=, VALUES=)
	helm upgrade --install $(RELEASE) $(CHART) \
	  --namespace $(NAMESPACE) --create-namespace \
	  $(if $(VALUES),-f $(VALUES),) \
	  --wait --timeout 10m

.PHONY: uninstall
uninstall: ## Remove the release
	helm uninstall $(RELEASE) --namespace $(NAMESPACE)

.PHONY: logs
logs: ## Tail the AgentHive logs
	kubectl logs -n $(NAMESPACE) -l app.kubernetes.io/name=$(CHART_NAME) -f --tail=200

.PHONY: clean
clean: ## Remove build scratch
	rm -rf dist
