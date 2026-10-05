#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$PWD/.local/kubeconfig}"
for tool in docker kind kubectl python3 ssh-keygen; do
  command -v "$tool" >/dev/null || { echo "Install $tool, then rerun this command." >&2; exit 1; }
done
docker info >/dev/null
umask 077
mkdir -p .local
python3 - <<'PY'
import json, os, secrets
from pathlib import Path
state = Path('.local/local-secrets.json')
if not state.exists():
    state.write_text(json.dumps({name: secrets.token_hex(24) for name in ('postgres', 's3_key', 's3_secret', 'sftp')}))
    state.chmod(0o600)
values = json.loads(state.read_text())
files = {
    'postgres.env': {'password': values['postgres']},
    'object-storage.env': {'AWS_ACCESS_KEY_ID': values['s3_key'], 'AWS_SECRET_ACCESS_KEY': values['s3_secret']},
    'simulator.env': {'SFTP_USER': 'nexo', 'SFTP_PASSWORD': values['sftp']},
    'migration.env': {'DATABASE_URL': f"postgresql://nexo:{values['postgres']}@postgres.nexo.svc.cluster.local:5432/nexo?sslmode=disable"},
    'app.env': {
        'DATABASE_URL': f"postgresql://nexo:{values['postgres']}@postgres.nexo.svc.cluster.local:5432/nexo?sslmode=disable",
        'AWS_ACCESS_KEY_ID': values['s3_key'], 'AWS_SECRET_ACCESS_KEY': values['s3_secret'],
        'SFTP_PASSWORD': values['sftp'],
    },
}
for filename, data in files.items():
    path = Path('.local') / filename
    path.write_text(''.join(f'{key}={value}\n' for key, value in data.items()))
    path.chmod(0o600)
PY
if [[ ! -f .local/ssh_host_key ]]; then
  ssh-keygen -q -t rsa -b 3072 -m PEM -N '' -f .local/ssh_host_key
fi
awk '{print "[simulator.card-network.svc.cluster.local]:2222 " $1 " " $2}' \
  .local/ssh_host_key.pub > .local/known_hosts
if ! kind get clusters | grep -qx nexo; then
  kind create cluster --config deploy/local/kind.yaml --wait 180s
fi
K=(kubectl --context kind-nexo)
docker build --build-arg SOURCE_ROOT="$PWD" -t nexo:local .
kind load docker-image nexo:local --name nexo
"${K[@]}" apply -f deploy/base/namespaces.yaml
"${K[@]}" -n nexo create secret generic nexo-secrets --from-env-file=.local/app.env --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" -n nexo create secret generic migration-secrets --from-env-file=.local/migration.env --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" -n nexo create secret generic postgres-secrets --from-env-file=.local/postgres.env --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" -n nexo create secret generic object-storage-secrets --from-env-file=.local/object-storage.env --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" -n nexo create secret generic sftp-trust --from-file=.local/known_hosts --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" -n card-network create secret generic simulator-secrets --from-env-file=.local/simulator.env --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" -n card-network create secret generic simulator-host-key --from-file=.local/ssh_host_key --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" apply -f deploy/local/postgres.yaml -f deploy/local/object-storage.yaml
"${K[@]}" -n nexo rollout status statefulset/postgres --timeout=180s
"${K[@]}" -n nexo rollout status statefulset/object-storage --timeout=180s
"${K[@]}" -n nexo delete job migrate --ignore-not-found
"${K[@]}" apply -k deploy/local
"${K[@]}" -n nexo wait --for=condition=complete job/migrate --timeout=180s
"${K[@]}" -n nexo rollout restart deployment/api deployment/worker
"${K[@]}" -n card-network rollout restart deployment/simulator
"${K[@]}" -n nexo rollout status deployment/api --timeout=180s
"${K[@]}" -n nexo rollout status deployment/worker --timeout=180s
"${K[@]}" -n card-network rollout status deployment/simulator --timeout=180s
"${K[@]}" -n nexo exec deployment/api -- python -m nexo.cli seed > .local/seed.json
echo 'Nexo is running. Generated test credentials are in .local/seed.json.'
echo 'Open it with: KUBECONFIG="$PWD/.local/kubeconfig" kubectl --context kind-nexo -n nexo port-forward service/api 8080:80'
echo 'Then visit http://localhost:8080.'
