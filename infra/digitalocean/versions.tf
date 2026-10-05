terraform {
  required_version = ">= 1.6, < 2.0"
  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "2.103.0"
    }
  }
}

# Credentials come from DIGITALOCEAN_TOKEN and SPACES_ACCESS_KEY_ID /
# SPACES_SECRET_ACCESS_KEY in the user's shell, never a checked-in file.
provider "digitalocean" {}
