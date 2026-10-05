output "cluster_id" { value = digitalocean_kubernetes_cluster.nexo.id }
output "cluster_name" { value = digitalocean_kubernetes_cluster.nexo.name }
output "registry" { value = digitalocean_container_registry.nexo.endpoint }
output "database_id" { value = digitalocean_database_cluster.nexo.id }

output "application_config" {
  value = {
    S3_BUCKET       = digitalocean_spaces_bucket.files.name
    S3_ENDPOINT_URL = "https://${digitalocean_spaces_bucket.files.endpoint}"
    AWS_REGION      = var.region
  }
}

output "database_ca" {
  value = data.digitalocean_database_ca.nexo.certificate
}

output "application_secrets" {
  sensitive = true
  value = {
    DATABASE_URL = format("postgresql://%s:%s@%s:%s/%s?sslmode=verify-full&sslrootcert=/etc/nexo/db/ca.crt",
      digitalocean_database_user.nexo.name,
      urlencode(digitalocean_database_user.nexo.password),
      digitalocean_database_cluster.nexo.private_host,
      digitalocean_database_cluster.nexo.port,
    digitalocean_database_db.nexo.name)
    AWS_ACCESS_KEY_ID     = digitalocean_spaces_key.application.access_key
    AWS_SECRET_ACCESS_KEY = digitalocean_spaces_key.application.secret_key
  }
}

output "migration_secrets" {
  sensitive = true
  value = {
    DATABASE_URL = format("postgresql://%s:%s@%s:%s/%s?sslmode=verify-full&sslrootcert=/etc/nexo/db/ca.crt",
      digitalocean_database_cluster.nexo.user,
      urlencode(digitalocean_database_cluster.nexo.password),
      digitalocean_database_cluster.nexo.private_host,
      digitalocean_database_cluster.nexo.port,
    digitalocean_database_db.nexo.name)
  }
}
