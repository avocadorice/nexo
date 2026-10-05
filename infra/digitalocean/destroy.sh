#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export PATH="$PWD/.local/bin:$PATH"
if [[ -f .local/cloud.env ]]; then
  set -a
  source .local/cloud.env
  set +a
fi
: "${DIGITALOCEAN_TOKEN:?Configure .local/cloud.env first}"
export DIGITALOCEAN_ACCESS_TOKEN="$DIGITALOCEAN_TOKEN"
export KUBECONFIG="$PWD/.local/cloud-kubeconfig"
cluster_id=$(terraform -chdir=infra/digitalocean output -raw cluster_id)
echo 'This deletes Nexo cloud workloads, database, object versions, simulator data, registry, and cluster.'
read -r -p 'Type destroy-nexo to continue: ' confirmation
[[ "$confirmation" == destroy-nexo ]] || { echo 'Cancelled.'; exit 1; }
doctl kubernetes cluster kubeconfig save "$cluster_id" --alias nexo-cloud
# Release Kubernetes-owned resources while their cloud controllers still run.
kubectl --context nexo-cloud delete namespaces nexo card-network --ignore-not-found --wait=true --timeout=300s
terraform -chdir=infra/digitalocean destroy
echo 'Check the DigitalOcean project and billing page for retained manual snapshots or resources.'
