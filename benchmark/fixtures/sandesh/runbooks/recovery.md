# Deployment image recovery

## Incident
Inspect the Deployment and its readiness before changing an image. The operator's
confirmed task specifies the workload, container and permitted image. Record the
gateway response and verify the rollout and Service after an accepted update.

## Verification
Confirm the requested image, ready replicas and Service identity. Preserve the
sidecar image and other workloads. Escalate when the target cannot be established.
