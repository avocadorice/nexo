variable "name" {
  description = "Globally unique lowercase project prefix, for example nexo-yourname."
  type        = string
  validation {
    condition     = can(regex("^nexo-[a-z0-9-]{3,35}$", var.name))
    error_message = "Use nexo- followed by 3–35 lowercase letters, digits, or hyphens."
  }
}

variable "region" {
  type        = string
  description = "Keep the cluster, database, and Spaces bucket in the same region."
  default     = "nyc3"
}

variable "kubernetes_version" {
  type        = string
  description = "Exact supported slug from doctl kubernetes options versions; pin before apply."
  validation {
    condition     = can(regex("^1\\.[0-9]+\\.[0-9]+-do\\.[0-9]+$", var.kubernetes_version))
    error_message = "Supply an exact DigitalOcean Kubernetes version, not latest."
  }
}

variable "admin_cidrs" {
  type        = list(string)
  description = "Trusted administrator public IPv4/IPv6 CIDRs for the Kubernetes control plane."
  validation {
    condition     = length(var.admin_cidrs) > 0 && alltrue([for c in var.admin_cidrs : can(cidrhost(c, 0)) && !contains(["0.0.0.0/0", "::/0"], c)])
    error_message = "Supply at least one specific trusted CIDR; a public /0 is not allowed."
  }
}

variable "worker_count" {
  type        = number
  default     = 2
  description = "Each 2-vCPU/4-GiB worker adds about $24/month; no automatic scale-out."
  validation {
    condition     = var.worker_count >= 2 && var.worker_count <= 4 && floor(var.worker_count) == var.worker_count
    error_message = "Use 2–4 workers; review larger cost/capacity changes explicitly."
  }
}
