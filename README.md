# komtrol-config

Everything that runs inside a cluster, for every environment and every
customer. ArgoCD reads this repository and applies it. Nothing here is ever
installed by hand: `helm install` against a cluster is reverted by selfHeal at
the next sync.

The companion is `lexplosion-eks-terraform`, which owns every AWS resource. If
a change is not in one of those two repositories, it will be reverted.

> This is a worked sample built to match the **Application Deployment Runbook,
> Section 6**. Reconcile the account IDs, hostnames, and repository URLs with
> your own before using it.

## One path rule

```text
values/<environment>/<tenant>/<application>.yaml        services Lexplosion builds
components/<environment>/<tenant>/<component>.yaml      software Lexplosion runs
```

Two trees, one rule. They are separate for exactly one reason: where the chart
comes from. `values/` renders a chart in this repository. `components/`
renders a chart published by Percona, Weaviate, or ONLYOFFICE. Everything else
about them is identical, down to the generator.

Either way the values file names its own chart, in a `chart:` block the
ApplicationSet reads:

```yaml
chart: { name: komtrol-service }                    # values/, first party
chart: { repoURL: ..., name: weaviate, version: ..., namespace: data }
```

That is what lets one pair of ApplicationSets cover both `komtrol-service` and
`komtrol-platform` without an ApplicationSet per chart.

Three levels, no exceptions, and two environments that never mix.

- **`staging/`** is everything Lexplosion runs for itself. Two tenants today:
  `shared`, the cluster the whole company uses, and `stand-in`, the throwaway
  account one team uses to rehearse an onboarding.
- **`production/`** is real paying customers, one directory each. Nothing else
  is ever in there, which is what makes a sweep over `values/production/*` safe
  to reason about.

Staging is not a special case in any other way. Each of its tenants gets a
directory like every customer, a cluster secret with the same three labels, and
the same ApplicationSet.

```text
komtrol-config/
├── charts/
│   ├── komtrol-service/          one generic chart, Java and Python alike
│   │   ├── Chart.yaml
│   │   ├── values.yaml           layer 1, chart defaults
│   │   └── templates/
│   └── komtrol-platform/         cluster objects ArgoCD owns
│       └── templates/            EC2NodeClass, NodePools, PriorityClasses
├── values/
│   ├── common/                   layer 2, one file per application
│   │   ├── komtrol-api.yaml
│   │   ├── komtrol-indexer.yaml
│   │   └── komtrol-platform.yaml
│   ├── staging/                  Lexplosion's own clusters
│   │   ├── shared/               layer 3
│   │   │   ├── komtrol-api.yaml
│   │   │   ├── komtrol-indexer.yaml
│   │   │   └── komtrol-platform.yaml
│   │   └── stand-in/             production shaped values, see below
│   │       └── (the same three)
│   └── production/               real customers only
│       └── customer-a/
│           └── (the same three)
├── components/                   same three levels, vendor charts
│   ├── common/
│   │   ├── mongodb-operator.yaml
│   │   ├── mongodb.yaml
│   │   ├── weaviate.yaml
│   │   └── onlyoffice.yaml
│   ├── staging/
│   │   ├── shared/               all four
│   │   └── stand-in/             all four
│   └── production/
│       └── customer-a/           all four
├── argocd/
│   ├── applicationset-automatic.yaml              services, waves 0 to 3
│   ├── applicationset-manual.yaml                 services, manual wave
│   ├── applicationset-components-automatic.yaml   components, waves 0 to 3
│   ├── applicationset-components-manual.yaml      components, manual wave
│   └── cluster-secret-*.yaml                      written by Terragrunt
├── release-waves.yaml
└── scripts/validate.py
```

**The stand-in is the one place where the directory and the values disagree,
and it is deliberate.** It sits under `staging/` because the account belongs to
Lexplosion. Everything inside it is production shaped: production sizing,
production replica counts, `LOG_LEVEL: INFO`, production secret paths, and the
same set of files a real customer gets. A rehearsal against a cheaper cluster,
or against a shorter list of files, proves nothing.

Values live **outside** the chart directory on purpose. The chart is the code
and the values are the configuration, so onboarding a customer adds a directory
and touches nothing shared. ArgoCD refuses a `valueFiles` path outside the
chart path, which is why each ApplicationSet lists the repository twice and
references the second as `$values`.

## Three layers, merged left to right

Helm merges values files in order, so the later file wins.

| Layer | File | Holds | Changes |
|---|---|---|---|
| 1 | `charts/komtrol-service/values.yaml` | Chart defaults, no service named | When the chart changes |
| 2 | `values/common/<application>.yaml` | Facts about the service: image repository, ports, probe paths, metrics path, migration command | Rarely |
| 3 | `values/<env>/<tenant>/<app>.yaml` | Facts about this deployment: digest, hostname, sizing, config, secret paths | Every release |

The split is enforced, not merely documented. `image.repository` is `required`
and only ever set in layer 2. `image.digest` is `required` and only ever set in
layer 3, written by CI. `scripts/validate.py` fails a pull request that puts
either in the wrong file.

## Why the chart is not called komtrol-api

It renders every Komtrol service. The **release name is the application name**,
taken by the ApplicationSet from the values file's basename, and every object
the chart creates is named from it:

```text
values/production/customer-a/komtrol-indexer.yaml
  -> release name  komtrol-indexer
  -> Deployment, Service, ServiceAccount, ConfigMap ... all komtrol-indexer
```

That is why `_helpers.tpl` never falls back to `.Chart.Name`. Two services from
one generic chart in one namespace would otherwise share a name and a selector,
and each Service would route to both sets of pods.

Nothing in `templates/` differs between the Java service and the Python one.
The whole of that difference is `values/common/komtrol-indexer.yaml`.

## The rules the chart enforces for you

| Rule | What happens if you try |
|---|---|
| Image by digest, never by tag | `image.digest` is `required`, the render fails |
| Repository named once, in layer 2 | `image.repository` is `required`, the render fails |
| Every public service names its host | `ingress.host` is `required`, the render fails |
| Memory limit equals request | Checked by `scripts/validate.py` |
| No CPU limit unless measured | Not set by the chart at all |
| `replicas` never fights the HPA | Omitted from the Deployment when autoscaling is on |
| PDB below the replica floor | Checked by `scripts/validate.py` |
| Restricted Pod Security | Full context on every pod and on the migration Job |
| A config change rolls the pods | `checksum/config` annotation on the pod template |
| Two services never share a selector | Checked by `scripts/validate.py` |
| Nothing selects a node pool its cluster lacks | Checked by `scripts/validate.py` |
| AMIs pinned, never an `@latest` alias | `nodeClass.amiSelectorAlias` is `required` |
| IMDSv2 only, one hop | Set on the EC2NodeClass, so no container can borrow the node role |

## Before you push

```bash
python3 scripts/validate.py
```

It walks both trees, checks the layout, renders all nine first-party
releases and statically asserts the twelve component releases. 402 checks in
total. Among them:

- Every tenant directory has a cluster secret and every cluster secret has a
  tenant directory, so config that nothing applies cannot sit unnoticed
- Every tenant runs all four components and exactly one `komtrol-platform`
  release, so a half-built data plane is caught before the application is
  pointed at a datastore that is not there
- Nothing here claims to be applied by something else
- Nothing selects a node pool its cluster does not have. Shared staging has no
  critical pool, so its datastores fall back to the default pool
- No two tenants share a backup bucket
- Weaviate never allows anonymous access and never uses a third-party
  vectoriser, in any environment
- MongoDB backups carry no static AWS key Run it in CI on every pull request. `helm lint` proves
the chart parses. This proves the repository still obeys the path rule and
still does what Section 6 says it does, which is the part that breaks quietly
when someone edits a template.

To look at one rendering by hand, pass both layers in order:

```bash
helm template komtrol-api charts/komtrol-service -f values/common/komtrol-api.yaml -f values/production/customer-a/komtrol-api.yaml --namespace komtrol | less
```

Against a real cluster, add the check that matters most:

```bash
helm template komtrol-api charts/komtrol-service -f values/common/komtrol-api.yaml -f values/production/customer-a/komtrol-api.yaml --namespace komtrol | kubectl --context customer-a apply --dry-run=server -f -
```

`--dry-run=server` validates against the real admission controllers, so it
catches a Pod Security violation. `--dry-run=client` does not, and will happily
pass a manifest the cluster would refuse.

## Adding a customer

1. Reserve a CIDR in `lexplosion-eks-terraform/docs/cidr-allocations.csv`.
2. Copy the nearest existing tenant directory in **both** trees, to
   `values/production/<customer>/` and `components/production/<customer>/`, and
   change every value. Sizing comes from a comparable customer's real usage,
   not from the chart defaults. Give the customer their own backup buckets:
   validation fails if two tenants share one.
3. Add the customer to `release-waves.yaml`. New customers start in the last
   automatic wave, never the first, unless their contract puts them on manual.
4. Set `environment`, `tenant`, and `wave` on the cluster secret. They have to
   agree with steps 2 and 3.
5. Run `python3 scripts/validate.py`, then raise the pull request.

No ApplicationSet is edited. The generator is a matrix of clusters and files,
so a new directory is a new set of Applications.

## Adding an application

Add `values/common/<application>.yaml`, then one file of that name in each
tenant directory that should run it. A tenant that has no such file does not
get the application, which is how `values/staging/shared/` and a customer
directory can hold different sets of services.

## Release waves

`release-waves.yaml` records which tenant is in which wave, and the `wave`
label on each cluster secret has to agree with it. `scripts/validate.py` checks
that agreement, since the two drifting apart is silent otherwise.

Wave 0 is shared staging and wave 1 is the stand-in, so a release has cleared
both of Lexplosion's own clusters before any customer sees it. Customer waves 2
and 3 then roll automatically. The **manual** wave has no `automated` block at
all, so the support team releases it by hand inside the window named on the
cluster secret.

Turning automated sync off also turns selfHeal off, so a manual-wave cluster
does not correct drift. Run `argocd app diff` before every hand sync.

See the EKS Platform Build Runbook, Section 12.1.

## Components

MongoDB, Weaviate, and ONLYOFFICE run in **every** tenant, Lexplosion's own
clusters and each customer's data plane alike. Nothing about them differs by
plane except sizing: the customer's copy exists because their documents must
stay inside their own account, and Lexplosion's copies exist so that a chart
upgrade is exercised twice before it reaches them.

| Component | Chart | Namespace | Why it is where it is |
|---|---|---|---|
| `mongodb-operator` | `percona/psmdb-operator` | `data` | Installs the CRD `mongodb` depends on |
| `mongodb` | `percona/psmdb-db` | `data` | Renders a PerconaServerMongoDB replica set |
| `weaviate` | `weaviate/weaviate` | `data` | The vector index over customer documents |
| `onlyoffice` | `onlyoffice/docs` | `onlyoffice` | Baseline Pod Security, so its own namespace |

`charts/komtrol-platform` is the first-party counterpart: one release per
cluster holding the Karpenter `EC2NodeClass`, the `default` and `critical`
NodePools, and the priority classes. It is a chart rather than a Terraform
module because those are Kubernetes objects, and it is not in the Terragrunt
repository's `cluster-bootstrap` unit because it does not have to exist before
ArgoCD connects. See the EKS Platform
Build Runbook, Section 5.

Three things differ from the application ApplicationSets, each for a reason:

- **`prune: false`.** Pruning a PerconaServerMongoDB or a PVC because someone
  deleted a file is not a rollback, it is data loss. The `gp3-encrypted`
  StorageClass sets `reclaimPolicy: Retain` for the same reason.
- **The chart version lives in the tenant file.** It is the component
  equivalent of the image digest, so a chart upgrade rolls through the waves
  like any other change, staging first. Never track the latest chart.
- **`SkipDryRunOnMissingResource=true`.** On a fresh cluster the `mongodb`
  release fails its dry run until `mongodb-operator` has installed the CRD.
  ArgoCD retries and the pair settles without intervention.

**Two things ArgoCD cannot own are not in this repository at all.** The
`gp3-encrypted` StorageClass is immutable and needs a KMS ARN only Terraform
knows. The three namespaces need their Pod Security labels in place before the
first pod is admitted, which is before ArgoCD connects. Both are typed
resources in `lexplosion-eks-terraform/modules/cluster-bootstrap`, applied by
the `cluster-bootstrap` unit.

Ownership follows the apply. They used to sit here in a `bootstrap/` directory
labelled "applied by Terragrunt", which read fine and was a hole: no Terragrunt
unit applied it, so nothing created them at all. Every ApplicationSet still
sets `CreateNamespace=false`, so ArgoCD can never create an unlabelled
namespace behind them.

**Watch the list-versus-map trap.** Helm replaces a list wholesale rather than
merging it, so anything shared through `components/common/` has to be a map. It
is why `replsets` is keyed by name in `mongodb.yaml`: as a list, every shared
setting there would be silently discarded the moment a tenant file set a size.

**Component values are checked but not rendered.** The chart versions in this
sample are placeholders, and `validate.py` lists them at the end of its run
rather than failing. To render one for real, pin its version and then:

```bash
helm template mongodb percona/psmdb-db --version <pinned> -f components/common/mongodb.yaml -f components/production/customer-a/mongodb.yaml --namespace data
```
