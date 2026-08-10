#!/usr/bin/env python3
"""Check the layout, then render every deployment and assert its invariants.

Run it before you push, and in CI on every pull request.

    python3 scripts/validate.py

helm lint proves the chart parses. This proves the repository still obeys the
path rule, and that the rendered output still does what Section 6 says it does,
which is the part that breaks quietly when a template is edited.

Needs helm and PyYAML.
"""

import glob
import os
import subprocess
import sys

import yaml

NS = "komtrol"
ENVIRONMENTS = ("staging", "production")

# The first-party charts. Which one a values file renders is declared in the
# file itself, in a chart.name key, exactly as a component declares its vendor
# chart. The ApplicationSet reads the same key.
SERVICE = "komtrol-service"
PLATFORM = "komtrol-platform"
FIRST_PARTY = (SERVICE, PLATFORM)

# Every tenant that runs the application runs all of these. A data plane
# missing one is a half-built cluster, and the gap shows up as an application
# that starts and then cannot reach its datastore.
REQUIRED_COMPONENTS = ("mongodb-operator", "mongodb", "weaviate", "onlyoffice")
COMPONENT_NAMESPACES = {"mongodb-operator": "data", "mongodb": "data",
                        "weaviate": "data", "onlyoffice": "onlyoffice"}

failures = []
to_pin = []


def check(name, cond, detail=""):
    print(("  pass  " if cond else "  FAIL  ") + name + (("   " + detail) if not cond else ""))
    if not cond:
        failures.append(name)


def deployments():
    """Every values/<environment>/<tenant>/<application>.yaml in the tree."""
    found = []
    for env in ENVIRONMENTS:
        for path in sorted(glob.glob("values/%s/*/*.yaml" % env)):
            _, _, tenant, filename = path.split("/")
            found.append((env, tenant, filename[:-5], filename, path))
    return found


def chart_of(path):
    """Which first-party chart a values file declares."""
    return (load(path).get("chart") or {}).get("name")


def render(env, tenant, app, filename, sets=()):
    chart = chart_of("values/%s/%s/%s" % (env, tenant, filename)) or SERVICE
    cmd = ["helm", "template", app, "charts/%s" % chart,
           "-f", "values/common/%s" % filename,
           "-f", "values/%s/%s/%s" % (env, tenant, filename),
           "--namespace", NS]
    for s in sets:
        cmd += ["--set", s]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def docs(out):
    return [d for d in yaml.safe_load_all(out) if d]


def one(out, kind):
    found = [d for d in docs(out) if d["kind"] == kind]
    return found[0] if found else None


def load(path):
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def check_layout(found):
    print("The layout obeys values/<environment>/<tenant>/<application>.yaml")

    stray = [p for p in glob.glob("values/*.yaml")]
    check("no values file sits loose at the top of values/", not stray, " ".join(stray))

    for env in ENVIRONMENTS:
        check("values/%s/ exists" % env, os.path.isdir("values/%s" % env))

    for env, tenant, app, filename, path in found:
        common = "values/common/%s" % filename
        check("%s/%s/%s has a common file" % (env, tenant, app), os.path.exists(common),
              "expected " + common)
        if not os.path.exists(common):
            continue

        tenant_values = load(path)
        chart = (tenant_values.get("chart") or {}).get("name")
        check("%s/%s/%s declares a first-party chart" % (env, tenant, app),
              chart in FIRST_PARTY, str(chart))
        check("%s/%s/%s agrees with the common file on the chart" % (env, tenant, app),
              chart == chart_of(common))
        if chart != SERVICE:
            continue

        check("%s/%s/%s sets a digest" % (env, tenant, app),
              tenant_values.get("image", {}).get("digest"))
        check("%s/%s/%s leaves the repository to the common file" % (env, tenant, app),
              "repository" not in tenant_values.get("image", {}))

    for common in sorted(glob.glob("values/common/*.yaml")):
        app = os.path.basename(common)[:-5]
        values = load(common)
        used = [t for e, t, a, f, p in found if a == app]
        check("common/%s is used by at least one tenant" % app, bool(used))
        if chart_of(common) != SERVICE:
            continue
        check("common/%s names a repository" % app,
              values.get("image", {}).get("repository"))
        check("common/%s carries no digest" % app,
              "digest" not in values.get("image", {}),
              "a digest here would pin every tenant to one build")

    print("\nThe cluster secrets agree with the tree and with release-waves.yaml")
    waves = load("release-waves.yaml")
    in_waves = {t: w for w, tenants in waves.items() for t in (tenants or [])}
    tenants_on_disk = {(e, t) for e, t, a, f, p in found}
    registered = set()

    for path in sorted(glob.glob("argocd/cluster-secret-*.yaml")):
        secret = load(path)
        labels = secret["metadata"]["labels"]
        name = secret["metadata"]["name"]
        env, tenant = labels.get("environment"), labels.get("tenant")

        check("%s: labelled with environment and tenant" % name, bool(env and tenant))
        # The ArgoCD cluster name is what an Application is named after, and
        # the tenant label is what composes its values path. Letting them
        # differ means an Application whose name does not say where its
        # configuration came from.
        check("%s: ArgoCD cluster name equals the tenant label" % name,
              name == tenant and secret["stringData"]["name"] == tenant, tenant)
        check("%s: values/%s/%s/ exists" % (name, env, tenant),
              (env, tenant) in tenants_on_disk)
        registered.add((env, tenant))

        wave = str(labels.get("wave", ""))
        expected = in_waves.get(tenant)
        check("%s: wave label matches release-waves.yaml" % name,
              expected == ("manual" if wave == "manual" else "wave" + wave),
              "label %r, file says %r" % (wave, expected))

        if wave == "manual":
            check("%s: manual wave carries a release window" % name,
                  bool(labels.get("window")))

    for env, tenant in sorted(tenants_on_disk):
        charts = [chart_of(p) for p in
                  sorted(glob.glob("values/%s/%s/*.yaml" % (env, tenant)))]
        check("values/%s/%s/ renders komtrol-platform exactly once" % (env, tenant),
              charts.count(PLATFORM) == 1, "found %d" % charts.count(PLATFORM))

    # The other direction. A tenant directory with no cluster registered is
    # config that nothing applies, and it goes unnoticed for months.
    for env, tenant in sorted(tenants_on_disk):
        check("values/%s/%s/ has a cluster secret" % (env, tenant),
              (env, tenant) in registered)


def service_files(found):
    return [d for d in found if chart_of(d[4]) == SERVICE]


def platform_files(found):
    return [d for d in found if chart_of(d[4]) == PLATFORM]


def merge(base, over):
    """What Helm does with -f common.yaml -f tenant.yaml, for maps."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def check_components(found):
    """The upstream charts: MongoDB, Weaviate, ONLYOFFICE.

    These are not rendered here. The chart versions in this sample are
    placeholders, and pulling three vendor repositories would make the check
    depend on the network. Everything below is a static read of the values.
    """
    print("\nComponents obey the same path rule")
    tenants = sorted({(e, t) for e, t, a, f, p in found})
    buckets = {}
    critical = {}
    for env, tenant, app, filename, path in platform_files(found):
        merged = merge(load("values/common/%s" % filename), load(path))
        critical[(env, tenant)] = bool(merged.get("critical", {}).get("enabled"))

    stray = glob.glob("components/*.yaml")
    check("no component file sits loose at the top of components/", not stray,
          " ".join(stray))

    for env, tenant in tenants:
        present = sorted(os.path.basename(p)[:-5]
                         for p in glob.glob("components/%s/%s/*.yaml" % (env, tenant)))
        for want in REQUIRED_COMPONENTS:
            check("%s/%s runs %s" % (env, tenant, want), want in present)

        for name in present:
            tag = "%s/%s/%s" % (env, tenant, name)
            common_path = "components/common/%s.yaml" % name
            check("%s has a common file" % tag, os.path.exists(common_path))
            if not os.path.exists(common_path):
                continue

            common = load(common_path)
            own = load("components/%s/%s/%s.yaml" % (env, tenant, name))
            values = merge(common, own)
            chart = own.get("chart") or {}

            for key in ("repoURL", "name", "version", "namespace"):
                check("%s: chart.%s is set" % (tag, key), bool(chart.get(key)))
            for key in ("repoURL", "name", "namespace"):
                check("%s: chart.%s agrees with the common file" % (tag, key),
                      chart.get(key) == common.get("chart", {}).get(key))
            check("%s: deploys to the %s namespace" % (tag, COMPONENT_NAMESPACES.get(name)),
                  chart.get("namespace") == COMPONENT_NAMESPACES.get(name),
                  str(chart.get("namespace")))
            if "<" in str(chart.get("version", "")):
                to_pin.append("%s  chart version" % tag)

            if name == "mongodb":
                rs = values["replsets"]["rs0"]
                check("%s: volumes use gp3-encrypted" % tag,
                      rs["volumeSpec"]["persistentVolumeClaim"]["storageClassName"]
                      == "gp3-encrypted")
                check("%s: members spread across zones" % tag,
                      rs["affinity"]["antiAffinityTopologyKey"]
                      == "topology.kubernetes.io/zone")
                want = "critical" if critical.get((env, tenant)) else "default"
                check("%s: on the %s pool, the one this cluster has" % (tag, want),
                      rs["nodeSelector"]["karpenter.sh/nodepool"] == want)
                check("%s: exempt from consolidation" % tag,
                      rs["annotations"]["karpenter.sh/do-not-disrupt"] == "true")
                check("%s: backup uses Pod Identity, not a stored key" % tag,
                      values["backup"]["storages"]["s3-data-plane"]["s3"]
                      ["credentialsSecret"] == "")
                check("%s: three members outside shared staging" % tag,
                      rs["size"] == 3 or tenant == "shared",
                      "size %s" % rs["size"])
                buckets.setdefault(
                    values["backup"]["storages"]["s3-data-plane"]["s3"]["bucket"],
                    []).append(tag)

            if name == "weaviate":
                check("%s: anonymous access disabled" % tag,
                      values["authentication"]["anonymous_access"]["enabled"] is False)
                check("%s: no third-party vectoriser" % tag,
                      not values["modules"].get("text2vec-openai", {}).get("enabled")
                      and not values["modules"].get("text2vec-cohere", {}).get("enabled"))
                check("%s: memory request equals limit" % tag,
                      values["resources"]["requests"]["memory"]
                      == values["resources"]["limits"]["memory"])
                check("%s: storage uses gp3-encrypted" % tag,
                      values["storage"]["storageClassName"] == "gp3-encrypted")
                want = "critical" if critical.get((env, tenant)) else "default"
                check("%s: on the %s pool, the one this cluster has" % (tag, want),
                      values["nodeSelector"]["karpenter.sh/nodepool"] == want)
                buckets.setdefault(
                    values["backups"]["s3"]["envconfig"]["BACKUP_S3_BUCKET"],
                    []).append(tag)

            if name == "onlyoffice":
                check("%s: JWT signing enabled" % tag, values["jwt"]["enabled"] is True)
                check("%s: shared cache claim set, required above one replica" % tag,
                      bool(values["persistence"]["existingClaim"]))
                ann = values["ingress"]["annotations"]
                check("%s: load balancer idle timeout raised for WebSockets" % tag,
                      "idle_timeout.timeout_seconds=300" in
                      ann["alb.ingress.kubernetes.io/load-balancer-attributes"])
                check("%s: sticky sessions enabled" % tag,
                      "stickiness.enabled=true" in
                      ann["alb.ingress.kubernetes.io/target-group-attributes"])
                check("%s: pinned to amd64 until arm64 is confirmed" % tag,
                      values["nodeSelector"]["kubernetes.io/arch"] == "amd64")
                check("%s: names its own hostname" % tag, bool(values["ingress"]["host"]))

    # The check that would matter most on the day it fired. Two tenants sharing
    # a backup bucket means one customer's data is written into a location
    # another customer's cluster can also write to.
    for bucket, users in sorted(buckets.items()):
        check("backup bucket %s is used by one tenant only" % bucket,
              len(users) == 1, " and ".join(users))


def check_no_bootstrap():
    """bootstrap/ used to live here, and it was a hole.

    Namespaces and the StorageClass have to exist before ArgoCD connects, so
    ArgoCD cannot apply them and neither can this repository. They are typed
    resources in lexplosion-eks-terraform/modules/cluster-bootstrap now, which
    is where the KMS key ARN and the cluster outputs already are.
    """
    print("\nNothing here claims to be applied by something else")
    check("no bootstrap/ directory", not os.path.isdir("bootstrap"),
          "namespaces and the storage class belong to the Terragrunt repository")


def check_platform(found):
    """charts/komtrol-platform: the cluster objects ArgoCD owns."""
    print("\nCluster objects render, one release per cluster")
    for env, tenant, app, filename, path in platform_files(found):
        tag = "%s/%s" % (env, tenant)
        rc, out, err = render(env, tenant, app, filename)
        check("%s: komtrol-platform renders" % tag, rc == 0,
              err.strip().splitlines()[-1] if err.strip() else "")
        if rc != 0:
            continue

        nc = one(out, "EC2NodeClass")
        pools = {d["metadata"]["name"]: d for d in docs(out) if d["kind"] == "NodePool"}
        classes = [d for d in docs(out) if d["kind"] == "PriorityClass"]
        values = merge(load("values/common/%s" % filename), load(path))

        check("%s: node class discovers by this cluster's tag" % tag,
              nc["spec"]["subnetSelectorTerms"][0]["tags"]["karpenter.sh/discovery"]
              == values["clusterName"])
        check("%s: node class names this cluster's node role" % tag,
              values["clusterName"] in nc["spec"]["role"], nc["spec"]["role"])
        check("%s: AMI pinned, not an @latest alias" % tag,
              "@latest" not in nc["spec"]["amiSelectorTerms"][0]["alias"])
        check("%s: every node volume encrypted" % tag,
              all(m["ebs"]["encrypted"] for m in nc["spec"]["blockDeviceMappings"]))
        check("%s: IMDSv2 required, one hop" % tag,
              nc["spec"]["metadataOptions"]["httpTokens"] == "required"
              and nc["spec"]["metadataOptions"]["httpPutResponseHopLimit"] == 1)
        check("%s: node class syncs before the pools that reference it" % tag,
              int(nc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
              < int(pools["default"]["metadata"]["annotations"]
                    ["argocd.argoproj.io/sync-wave"]))

        check("%s: default pool exists and is capped" % tag,
              "default" in pools and pools["default"]["spec"]["limits"]["cpu"])
        check("%s: default pool is on-demand only" % tag,
              ["on-demand"] == [v for r in pools["default"]["spec"]["template"]["spec"]
                                ["requirements"] if r["key"] == "karpenter.sh/capacity-type"
                                for v in r["values"]])
        check("%s: nodes expire so the fleet stays patched" % tag,
              bool(pools["default"]["spec"]["template"]["spec"].get("expireAfter")))

        # Every tenant that runs a datastore needs the pool those datastores
        # tolerate. Without it the pods sit Pending and the cause is two files
        # away from the symptom.
        selectors = {}
        for c in ("mongodb", "weaviate", "onlyoffice"):
            own = "components/%s/%s/%s.yaml" % (env, tenant, c)
            if not os.path.exists(own):
                continue
            v = merge(load("components/common/%s.yaml" % c), load(own))
            sel = (v["replsets"]["rs0"] if c == "mongodb" else v).get("nodeSelector", {})
            selectors[c] = sel.get("karpenter.sh/nodepool")
        wants_critical = sorted(c for c, p in selectors.items() if p == "critical")
        has_critical = bool(values.get("critical", {}).get("enabled"))

        # The check the platform chart exists to make possible. Pinning a pod
        # to a pool this cluster does not have leaves it Pending forever, and
        # the cause is two files away from the symptom.
        check("%s: nothing selects a pool this cluster lacks" % tag,
              has_critical or not wants_critical, ", ".join(wants_critical))

        if has_critical:
            crit = pools.get("critical")
            check("%s: critical pool exists" % tag, crit is not None)
            if crit:
                taint = crit["spec"]["template"]["spec"]["taints"][0]
                check("%s: critical pool taint matches what workloads tolerate" % tag,
                      taint["key"] == "workload" and taint["value"] == "critical"
                      and taint["effect"] == "NoSchedule")
                check("%s: critical pool consolidates only when empty" % tag,
                      crit["spec"]["disruption"]["consolidationPolicy"] == "WhenEmpty")
        else:
            check("%s: critical pool correctly absent" % tag, "critical" not in pools)
            check("%s: datastores fall back to the default pool" % tag,
                  all(p == "default" for p in selectors.values()),
                  str(selectors))

        check("%s: three priority classes, none of them the cluster default" % tag,
              len(classes) == 3 and not any(c["globalDefault"] for c in classes))


def check_guards(found):
    print("\nGuard rails refuse an incomplete values file")
    env, tenant, app, filename, _ = service_files(found)[0]
    for key, message in (("image.digest", "image.digest is required"),
                         ("image.repository", "image.repository is required")):
        rc, _, err = render(env, tenant, app, filename, ["%s=" % key])
        check("empty %s fails the render" % key, rc != 0 and message in err)

    api = [d for d in found if d[2] == "komtrol-api"][0]
    rc, _, err = render(api[0], api[1], api[2], api[3], ["ingress.host="])
    check("empty ingress.host fails the render", rc != 0 and "ingress.host is required" in err)


def check_render(found):
    print("\nEvery service renders, and holds its invariants")
    seen_names = {}

    for env, tenant, app, filename, path in service_files(found):
        rc, out, err = render(env, tenant, app, filename)
        tag = "%s/%s/%s" % (env, tenant, app)
        check("%s renders" % tag, rc == 0,
              err.strip().splitlines()[-1] if err.strip() else "")
        if rc != 0:
            continue

        dep = one(out, "Deployment")
        pod = dep["spec"]["template"]["spec"]
        c = pod["containers"][0]
        res = c["resources"]

        # Two services from one generic chart in one namespace. If the release
        # name did not reach the selector, they would share it and each
        # Service would route to both.
        key = (env, tenant)
        selector = dep["spec"]["selector"]["matchLabels"]["app"]
        check("%s: selector is the application name" % tag, selector == app, selector)
        check("%s: no other service in this tenant shares the selector" % tag,
              seen_names.get((key, selector)) in (None, app))
        seen_names[(key, selector)] = app

        check("%s: memory limit equals request" % tag,
              res["limits"]["memory"] == res["requests"]["memory"])
        check("%s: no CPU limit" % tag, "cpu" not in res.get("limits", {}))
        check("%s: image pinned by digest" % tag, "@sha256:" in c["image"])
        check("%s: non-root uid 10001" % tag,
              pod["securityContext"]["runAsNonRoot"] is True
              and pod["securityContext"]["runAsUser"] == 10001)
        check("%s: read-only root filesystem" % tag,
              c["securityContext"]["readOnlyRootFilesystem"] is True)
        check("%s: all capabilities dropped" % tag,
              c["securityContext"]["capabilities"]["drop"] == ["ALL"])
        check("%s: seccomp RuntimeDefault" % tag,
              pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault")
        check("%s: /tmp emptyDir mounted" % tag,
              any(m["mountPath"] == "/tmp" for m in c["volumeMounts"]))
        check("%s: all three probes present" % tag,
              all(k in c for k in ("startupProbe", "readinessProbe", "livenessProbe")))
        check("%s: preStop delay present" % tag, "preStop" in c.get("lifecycle", {}))

        pdb = one(out, "PodDisruptionBudget")
        hpa = one(out, "HorizontalPodAutoscaler")
        floor = hpa["spec"]["minReplicas"] if hpa else dep["spec"].get("replicas", 1)
        check("%s: PDB minAvailable below the replica floor" % tag,
              pdb["spec"]["minAvailable"] < floor,
              "%s vs %s" % (pdb["spec"]["minAvailable"], floor))

        if hpa:
            check("%s: replicas omitted while the HPA owns the count" % tag,
                  "replicas" not in dep["spec"])
        else:
            check("%s: replicas set when there is no HPA" % tag, "replicas" in dep["spec"])

        ing = one(out, "Ingress")
        if ing:
            delay = int(ing["metadata"]["annotations"][
                "alb.ingress.kubernetes.io/deregistration-delay.timeout_seconds"])
            check("%s: deregistration delay below the grace period" % tag,
                  delay < pod["terminationGracePeriodSeconds"],
                  "%s vs %s" % (delay, pod["terminationGracePeriodSeconds"]))

        np = one(out, "NetworkPolicy")
        port = np["spec"]["ingress"][0]["ports"][0]["port"]
        check("%s: network policy opens the container port" % tag,
              port == c["ports"][0]["containerPort"], str(port))

        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            for h in c[probe]["httpGet"].get("httpHeaders", []):
                looks_secret = any(w in h["name"].lower()
                                   for w in ("auth", "token", "key", "secret", "password"))
                check("%s: no credential in a %s header" % (tag, probe), not looks_secret,
                      h["name"])


def check_behaviour(found):
    print("\nA config change rolls the pods, and the critical pool is reachable")
    env, tenant, app, filename, _ = service_files(found)[0]
    _, a, _ = render(env, tenant, app, filename)
    _, b, _ = render(env, tenant, app, filename, ["config.LOG_LEVEL=TRACE"])
    ann = lambda o: one(o, "Deployment")["spec"]["template"]["metadata"]["annotations"]
    check("checksum/config changes with the ConfigMap",
          ann(a)["checksum/config"] != ann(b)["checksum/config"])

    _, out, _ = render(env, tenant, app, filename, ["nodePool=critical"])
    pod = one(out, "Deployment")["spec"]["template"]["spec"]
    check("critical pool sets the nodeSelector",
          pod.get("nodeSelector", {}).get("karpenter.sh/nodepool") == "critical")
    check("critical pool sets the toleration",
          any(t["key"] == "workload" for t in pod.get("tolerations", [])))


def main():
    for chart in FIRST_PARTY:
        if not os.path.isdir("charts/%s" % chart):
            print("charts/%s not found. Run this from the repository root." % chart)
            return 1
    found = deployments()
    if not found:
        print("no values/<environment>/<tenant>/<application>.yaml files found.")
        return 1

    check_layout(found)
    check_platform(found)
    check_components(found)
    check_no_bootstrap()
    check_guards(found)
    check_render(found)
    check_behaviour(found)

    print()
    if failures:
        print("%d check(s) failed:" % len(failures))
        for f in failures:
            print("   " + f)
        return 1

    components = len(glob.glob("components/*/*/*.yaml"))
    print("%d first-party releases rendered, %d component releases checked, "
          "all invariants hold" % (len(found), components))
    if to_pin:
        # Not a failure. The sample ships with placeholders, and this is the
        # list to work through before the first real sync.
        print("\n%d placeholder(s) still to fill in:" % len(to_pin))
        for t in to_pin:
            print("   " + t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
