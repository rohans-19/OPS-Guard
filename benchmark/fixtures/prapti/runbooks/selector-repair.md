# Service routing repair

## Incident
Read the Service and the backend named in the operator's confirmed task before
changing a selector. The task states the Service and the complete selector map.
Replace the whole map in one request and record the gateway response.

## Verification
Confirm that the Service has ready endpoints and that responses come from the
intended backend. Other Services keep their selectors. Escalate when the
intended backend cannot be established.
