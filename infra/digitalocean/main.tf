resource "digitalocean_project" "nexo" {
  name        = var.name
  description = "Nexo chargeback system and explicitly labeled network simulator"
  purpose     = "Service or API"
  environment = "Development"
}

resource "digitalocean_tag" "nexo" {
  name = var.name
}

resource "digitalocean_vpc" "nexo" {
  name     = var.name
  region   = var.region
  ip_range = "10.20.0.0/16"
}

resource "digitalocean_kubernetes_cluster" "nexo" {
  name          = var.name
  region        = var.region
  version       = var.kubernetes_version
  vpc_uuid      = digitalocean_vpc.nexo.id
  ha            = false
  auto_upgrade  = true
  surge_upgrade = true
  tags          = [digitalocean_tag.nexo.name]

  control_plane_firewall {
    enabled           = true
    allowed_addresses = var.admin_cidrs
  }
  maintenance_policy {
    day        = "sunday"
    start_time = "08:00"
  }
  node_pool {
    name       = "workloads"
    size       = "s-2vcpu-4gb"
    node_count = var.worker_count
    auto_scale = false
    tags       = [digitalocean_tag.nexo.name]
  }
}

resource "digitalocean_database_cluster" "nexo" {
  name                 = "${var.name}-postgres"
  engine               = "pg"
  version              = "17"
  size                 = "db-s-1vcpu-2gb"
  node_count           = 1
  region               = var.region
  private_network_uuid = digitalocean_vpc.nexo.id
  project_id           = digitalocean_project.nexo.id
  tags                 = [digitalocean_tag.nexo.name]
  maintenance_window {
    day  = "sunday"
    hour = "08:00:00"
  }
}

resource "digitalocean_database_firewall" "nexo" {
  cluster_id = digitalocean_database_cluster.nexo.id
  rule {
    type  = "k8s"
    value = digitalocean_kubernetes_cluster.nexo.id
  }
}

resource "digitalocean_database_db" "nexo" {
  cluster_id = digitalocean_database_cluster.nexo.id
  name       = "nexo"
}

resource "digitalocean_database_user" "nexo" {
  cluster_id = digitalocean_database_cluster.nexo.id
  name       = "nexo"
}

data "digitalocean_database_ca" "nexo" {
  cluster_id = digitalocean_database_cluster.nexo.id
}

resource "digitalocean_spaces_bucket" "files" {
  name   = "${var.name}-files"
  region = var.region
  acl    = "private"
  versioning { enabled = true }
  # This dedicated learning environment has an explicit destructive teardown.
  force_destroy = true
}

resource "digitalocean_spaces_key" "application" {
  name = "${var.name}-application"
  grant {
    bucket     = digitalocean_spaces_bucket.files.name
    permission = "readwrite"
  }
}

resource "digitalocean_container_registry" "nexo" {
  name                   = var.name
  region                 = var.region
  subscription_tier_slug = "basic"
}

resource "digitalocean_project_resources" "nexo" {
  project = digitalocean_project.nexo.id
  resources = [
    digitalocean_kubernetes_cluster.nexo.urn,
    digitalocean_database_cluster.nexo.urn,
    digitalocean_spaces_bucket.files.urn,
  ]
}
