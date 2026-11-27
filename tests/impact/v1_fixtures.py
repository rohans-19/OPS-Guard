"""Small builders for effect-model v1 tests: wire-format objects in namespace ``shop``."""

from copy import deepcopy

NS = "shop"


def deployment(name, replicas=1, *, labels=None, strategy=None, sa=None, volumes=(), env=(), ready=None, uid=None):
    labels = labels or {"app": name}
    obj = {"kind": "Deployment", "metadata": {"name": name, "namespace": NS, "uid": uid or f"{name}-uid"},
           "spec": {"replicas": replicas, "selector": {"matchLabels": dict(labels)},
                    "template": {"metadata": {"labels": dict(labels)},
                                 "spec": {"containers": [{"name": "app", "image": "shop/app:v1", "env": list(env)}],
                                          "volumes": list(volumes)}}}}
    if strategy is not None:
        obj["spec"]["strategy"] = strategy
    if sa:
        obj["spec"]["template"]["spec"]["serviceAccountName"] = sa
    if ready is not None:
        obj["status"] = {"observedGeneration": 1, **({"readyReplicas": ready} if ready else {})}
    return obj


def replicaset(deploy, suffix="abc"):
    name = f"{deploy['metadata']['name']}-{suffix}"
    return {"kind": "ReplicaSet", "metadata": {"name": name, "namespace": NS, "uid": f"{name}-uid",
                                               "ownerReferences": [{"kind": "Deployment",
                                                                    "name": deploy["metadata"]["name"]}]},
            "spec": {"replicas": deploy["spec"]["replicas"], "template": deepcopy(deploy["spec"]["template"])}}


def pod(rs, index, *, ready=True, deleting=False, volumes=()):
    labels = rs["spec"]["template"]["metadata"]["labels"]
    name = f"{rs['metadata']['name']}-{index}"
    obj = {"kind": "Pod", "metadata": {"name": name, "namespace": NS, "uid": f"{name}-uid", "labels": dict(labels),
                                       "ownerReferences": [{"kind": "ReplicaSet", "name": rs["metadata"]["name"]}]},
           "spec": {"containers": [{"name": "app"}], "volumes": list(volumes)},
           "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}
    if deleting:
        obj["metadata"]["deletionTimestamp"] = "2026-11-27T00:00:00Z"
    return obj


def with_pods(deploy, ready_flags, suffix="abc"):
    rs = replicaset(deploy, suffix)
    return [rs, *(pod(rs, i, ready=flag) for i, flag in enumerate(ready_flags))]


def service(name, selector=None):
    return {"kind": "Service", "metadata": {"name": name, "namespace": NS, "uid": f"{name}-svc"},
            "spec": {"selector": dict(selector if selector is not None else {"app": name}),
                     "ports": [{"port": 8080}]}}


def configmap(name, data):
    return {"kind": "ConfigMap", "metadata": {"name": name, "namespace": NS, "uid": f"{name}-cm"}, "data": dict(data)}


def mount(configmap_name, *, items=None):
    volume = {"name": configmap_name, "configMap": {"name": configmap_name}}
    if items:
        volume["configMap"]["items"] = [{"key": k, "path": k} for k in items]
    return volume


def env_ref(configmap_name, key):
    return {"name": key.upper(), "valueFrom": {"configMapKeyRef": {"name": configmap_name, "key": key}}}


def pdb(name, selector, **budget):
    return {"kind": "PodDisruptionBudget", "metadata": {"name": name, "namespace": NS},
            "spec": {"selector": {"matchLabels": dict(selector)}, **budget}}


def hpa(name, target, low, high):
    return {"kind": "HorizontalPodAutoscaler", "metadata": {"name": name, "namespace": NS, "uid": f"{name}-hpa"},
            "spec": {"scaleTargetRef": {"kind": "Deployment", "name": target}, "minReplicas": low,
                     "maxReplicas": high}}


def ingress(name, *services):
    return {"kind": "Ingress", "metadata": {"name": name, "namespace": NS},
            "spec": {"rules": [{"http": {"paths": [{"path": f"/{s}", "backend": {"service": {"name": s}}}
                                                   for s in services]}}]}}


def netpol(name, selector, ingress_rules):
    return {"kind": "NetworkPolicy", "metadata": {"name": name, "namespace": NS, "uid": f"{name}-np"},
            "spec": {"podSelector": {"matchLabels": dict(selector)}, "policyTypes": ["Ingress"],
                     "ingress": ingress_rules}}


def edited(obj, path, value):
    out = deepcopy(obj)
    node = out
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    return out


def restarted(deploy):
    return edited(deploy, ("spec", "template", "metadata", "annotations"),
                  {"kubectl.kubernetes.io/restartedAt": "2026-11-27T00:00:00Z"})
