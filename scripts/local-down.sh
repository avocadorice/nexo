#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$PWD/.local/kubeconfig}"
echo 'Deleting the nexo kind cluster and all of its local database, object, and simulator data.'
kind delete cluster --name nexo
echo 'Local credentials in .local/ remain for the next local-up; no cloud resources were touched.'
