#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export PATH="$PWD/.local/bin:$PATH"
if [[ -f .local/cloud.env ]]; then
  set -a
  source .local/cloud.env
  set +a
fi
: "${DIGITALOCEAN_TOKEN:?Sign in and configure .local/cloud.env first}"
export DIGITALOCEAN_ACCESS_TOKEN="$DIGITALOCEAN_TOKEN"
export KUBECONFIG="$PWD/.local/cloud-kubeconfig"
for tool in terraform doctl docker kubectl python3 ssh-keygen; do
  command -v "$tool" >/dev/null || { echo "Install $tool first." >&2; exit 1; }
done
umask 077
mkdir -p .local/cloud/overlay
terraform -chdir=infra/digitalocean output -json > .local/cloud/terraform-output.json
cluster_id=$(terraform -chdir=infra/digitalocean output -raw cluster_id)
registry=$(terraform -chdir=infra/digitalocean output -raw registry)
image_tag="$(git rev-parse --short=12 HEAD)-$(date -u +%Y%m%d%H%M%S)"
export NEXO_IMAGE="$registry/nexo:$image_tag"
doctl kubernetes cluster kubeconfig save "$cluster_id" --alias nexo-cloud
doctl registry login --expiry-seconds 3600
# Cloud workers are amd64 even when the developer's laptop is arm64.
docker buildx build --build-arg SOURCE_ROOT="$PWD" --platform linux/amd64 -t "$NEXO_IMAGE" --push .
if [[ ! -f .local/cloud/ssh_host_key ]]; then
  ssh-keygen -q -t rsa -b 3072 -m PEM -N '' -f .local/cloud/ssh_host_key
fi
awk '{print "[simulator.card-network.svc.cluster.local]:2222 " $1 " " $2}' \
  .local/cloud/ssh_host_key.pub > .local/cloud/known_hosts
python3 - <<'PY'
import json, os, secrets
from pathlib import Path
root = Path('.local/cloud')
values = {key: item['value'] for key, item in json.loads((root/'terraform-output.json').read_text()).items()}
password = root/'sftp-password'
if not password.exists():
    password.write_text(secrets.token_hex(24))
app = values['application_secrets'] | {'SFTP_PASSWORD': password.read_text()}
for name, env in [('app.env', app), ('migration.env', values['migration_secrets']),
                  ('simulator.env', {'SFTP_USER': 'nexo', 'SFTP_PASSWORD': password.read_text()})]:
    (root/name).write_text(''.join(f'{key}={value}\n' for key, value in env.items()))
(root/'overlay/config.env').write_text(''.join(f'{key}={value}\n' for key, value in values['application_config'].items()))
(root/'overlay/ca.crt').write_text(values['database_ca'])
image, tag = os.environ['NEXO_IMAGE'].rsplit(':', 1)
patches = []
for kind, path in [('Deployment', '/spec/template/spec/imagePullSecrets'),
                   ('Job', '/spec/template/spec/imagePullSecrets'),
                   ('CronJob', '/spec/jobTemplate/spec/template/spec/imagePullSecrets')]:
    patches.append({'target': {'kind': kind}, 'patch': json.dumps([
        {'op': 'add', 'path': path, 'value': [{'name': 'nexo-registry'}]}])})
patches.append({'target': {'kind': 'Job', 'name': 'migrate'}, 'patch': json.dumps([
    {'op': 'add', 'path': '/spec/template/spec/containers/0/env',
     'value': [{'name': 'NEXO_APP_DB_ROLE', 'value': 'nexo'}]}])})
manifest = {
    'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
    'resources': ['../../../deploy/digitalocean'],
    'images': [{'name': 'nexo', 'newName': image, 'newTag': tag}],
    'configMapGenerator': [
        {'name': 'nexo-config', 'namespace': 'nexo', 'behavior': 'merge', 'envs': ['config.env']},
        {'name': 'database-ca', 'namespace': 'nexo', 'files': ['ca.crt']}],
    'generatorOptions': {'disableNameSuffixHash': True}, 'patches': patches,
}
(root/'overlay/kustomization.yaml').write_text(json.dumps(manifest, indent=2)+'\n')
PY
K=(kubectl --context nexo-cloud)
"${K[@]}" apply -f deploy/base/namespaces.yaml
for namespace in nexo card-network; do
  doctl registry kubernetes-manifest --namespace "$namespace" --name nexo-registry | "${K[@]}" apply -f -
done
"${K[@]}" -n nexo create secret generic nexo-secrets --from-env-file=.local/cloud/app.env --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" -n nexo create secret generic migration-secrets --from-env-file=.local/cloud/migration.env --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" -n nexo create secret generic sftp-trust --from-file=.local/cloud/known_hosts --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" -n card-network create secret generic simulator-secrets --from-env-file=.local/cloud/simulator.env --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" -n card-network create secret generic simulator-host-key --from-file=.local/cloud/ssh_host_key --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" -n nexo delete job migrate --ignore-not-found
"${K[@]}" apply -k .local/cloud/overlay
"${K[@]}" -n nexo wait --for=condition=complete job/migrate --timeout=300s
"${K[@]}" -n nexo rollout status deployment/api --timeout=300s
"${K[@]}" -n nexo rollout status deployment/worker --timeout=300s
"${K[@]}" -n card-network rollout status deployment/simulator --timeout=300s
if [[ ! -f .local/cloud/seed-cluster-id ]] || [[ "$(cat .local/cloud/seed-cluster-id)" != "$cluster_id" ]]; then
  "${K[@]}" -n nexo exec deployment/api -- python -m nexo.cli seed > .local/cloud/seed.json
  printf '%s' "$cluster_id" > .local/cloud/seed-cluster-id
fi
echo 'Deployed to DigitalOcean. Synthetic test credentials: .local/cloud/seed.json'
echo 'Open with: KUBECONFIG=$PWD/.local/cloud-kubeconfig kubectl -n nexo port-forward service/api 8080:80'
