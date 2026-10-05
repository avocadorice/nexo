# Cloud deployment proposal

Status: the user approved DigitalOcean with the small managed database. Infrastructure code is prepared; no cloud resources have been created by this work. Prices checked on 5 October 2026, in US dollars before tax. This is the small learning deployment, not a claim that these instance sizes can handle bank-scale traffic.

Selected: **DigitalOcean Kubernetes (DOKS), managed PostgreSQL, and Spaces** for the first deployment: approximately **$90/month** with authenticated port-forward access, or **$105/month** with a later public load balancer, with a single database node and a non-HA control plane recorded as availability simplifications. It keeps the Kubernetes lesson and the chargeback invariants visible without requiring a large cloud platform configuration. If automatic database failover and private worker subnets are required immediately, prefer the AWS option below and budget approximately **$300/month**. The user has confirmed the provider and managed-stateful-service trade-off; account sign-in and an account-specific Terraform plan remain before provisioning.

## Proposed topology

| Component | Kubernetes or managed object |
| --- | --- |
| Customer API, plain UI, operations UI | API `Deployment` and `Service`; two replicas when cloud capacity permits |
| Delivery and acknowledgement polling | Worker `Deployment`; replica count bounded by database connections and recipient capacity |
| Four daily export slots | `CronJob`, `0 */6 * * *`, explicit UTC time zone; each run creates a bounded `Job` |
| Schema migration | One-off `Job` |
| Durable chargebacks, batch assignments, work state, attempts | Managed PostgreSQL; local PostgreSQL `StatefulSet` |
| Generated CSV objects | Private Spaces bucket or S3 bucket; local S3-compatible server |
| Visa/Mastercard simulator | Separate namespace, SFTP (SSH File Transfer Protocol) server `Deployment`, persistent volume, internal `Service` |
| Configuration and credentials | `ConfigMap` plus `Secret` references populated outside version control |

The proposed design uses PostgreSQL work tables rather than an additional broker. Kubernetes Jobs and worker retries still use durable database state; a scheduler replay must never create another assignment for the same chargeback. A small operations view reports batch state, attempt history, backlog, and failures; structured logs go to standard output.

## DigitalOcean estimate

Use one region with all services available, provisionally NYC3. Region choice remains part of approval.

| Resource | Small monthly estimate |
| --- | ---: |
| Two Basic workers, each 2 virtual CPUs and 4 GiB memory | $48.00 |
| Standard PostgreSQL, 2 GiB, one primary | $30.45 |
| Optional regional load balancer (excluded initially) | $12.00 |
| Spaces subscription | $5.00 |
| Basic container registry | $5.00 |
| 10 GiB simulator volume | $1.00 |
| **Initial total without public load balancer** | **$89.45** |
| **Total with optional load balancer** | **$101.45** |

Worker pricing comes from the [Droplet price table](https://www.digitalocean.com/pricing/droplets); the standard [Kubernetes control plane is included](https://docs.digitalocean.com/products/kubernetes/details/pricing/). The database number uses the live [managed database table](https://www.digitalocean.com/pricing/managed-databases). The other rates are [regional load balancers](https://www.digitalocean.com/pricing/load-balancers), [Spaces](https://docs.digitalocean.com/products/spaces/details/pricing/), [registry](https://docs.digitalocean.com/products/container-registry/details/pricing/), and [volumes](https://docs.digitalocean.com/products/volumes/details/pricing/). Spaces includes 250 GiB storage and 1,024 GiB outbound transfer. Extra retention, traffic, larger databases, taxes, and a domain are additional.

An HA (high availability) control plane adds $40/month. Today a matching 2 GiB database standby adds roughly $30.45/month, bringing that configuration to about $172. **Do not promise that configuration can be recreated indefinitely:** DigitalOcean has announced that new Standard clusters lose standby/read-replica support on 15 October 2026 for accounts that have never created a PostgreSQL or MySQL cluster, and on 30 November for all accounts. Existing clusters are unaffected. Advanced Edition will gain shared-CPU options, whose exact configuration and price need rechecking before a future apply. Current published Advanced pricing begins at $130 per node, so primary plus standby would put this deployment around **$371/month**, including the HA control plane. See the [announced plan changes](https://docs.digitalocean.com/release-notes/upcoming/dbaas-plan-changes/) and [database pricing](https://docs.digitalocean.com/products/databases/postgresql/details/pricing/).

## AWS comparison

Estimate for `us-east-1`, 730 hours/month, no introductory credits or commitments:

| Resource | Small monthly estimate |
| --- | ---: |
| EKS standard-support control plane | $73.00 |
| Two `t3.medium` workers | $61.03 |
| 40 GiB worker disks plus 10 GiB simulator disk | $4.00 |
| RDS PostgreSQL `db.t4g.small`, Single-AZ, 20 GiB gp3 | $25.66 |
| Application load balancer base | $16.43 |
| Four public IPv4 addresses (two workers, two load-balancer zones) | $14.60 |
| Small S3, registry, load-balancer usage and logs allowance | $5–15 |
| **Total, public workers with restrictive security groups** | **$200–210** |

Sources: [EKS](https://aws.amazon.com/eks/pricing/), [T3 instances](https://aws.amazon.com/ec2/instance-types/t3/), [EBS](https://aws.amazon.com/ebs/pricing/), [RDS PostgreSQL](https://aws.amazon.com/rds/postgresql/pricing/), [load balancers](https://aws.amazon.com/elasticloadbalancing/pricing/), [IPv4 and NAT](https://aws.amazon.com/vpc/pricing/), and [S3](https://aws.amazon.com/s3/pricing/). RDS prices were also read directly from the [AWS public us-east-1 price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/us-east-1/index.json): `db.t4g.small` is $0.032/hour Single-AZ or $0.065/hour Multi-AZ; gp3 storage is $0.115 or $0.23 per GB-month respectively. The allowance is an estimate, not a quoted service rate.

For an AWS topology with private workers, two NAT (network address translation) gateways add about $65.70/month before data-processing fees, with public-address charges replacing the workers' addresses. Multi-AZ RDS adds roughly $26.39/month at these sizes. Budget **$295–315/month** plus transfer, CPU credits, retention, and domain costs. Sustained CPU-heavy load should use non-burstable instances after measurement. AWS offers a more explicit multi-zone topology and workload identity integration, with more networking and identity configuration to learn.

## Reproducibility and teardown contract

The [DigitalOcean Terraform configuration](../infra/digitalocean/README.md) defines networking, cluster/node pool, database and its firewall, private bucket, registry, and required cloud identities. Keep application workloads in plain Kustomize manifests shared with kind locally. Give every resource the project/environment tag, pin provider versions, and commit the dependency lock file. Set explicit control-plane HA and node counts so a provider default cannot silently change the estimate. The [DigitalOcean provider](https://docs.digitalocean.com/reference/terraform/reference/resources/) supports the required resource types.

Terraform state can contain credentials: store it outside Git with restricted access and encryption. Populate Kubernetes Secrets through a separate local command; never print secret-bearing Terraform outputs into logs. Cloud access uses the user's own sign-in.

The implementation must expose one teardown command that first removes Kubernetes-created load balancers and volumes, waits for their cleanup, then runs `terraform destroy`. It must make data destruction explicit and inventory retained snapshots, object versions, and registry subscriptions afterward, since those can continue billing. The teardown command is implemented in `infra/digitalocean/destroy.sh`; it has not yet been exercised against a real cloud environment.

Local Kubernetes checks and failure simulations cannot establish cloud availability or backup recovery. Cloud acceptance must include a real TLS (Transport Layer Security) request, a scheduled export and SFTP delivery, an interrupted upload/reconciliation exercise, worker restart, backup restore, and a measured load test. No performance result or cloud deployment is claimed by this proposal.
