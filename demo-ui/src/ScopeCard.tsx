import { useEffect, useState } from "react";
import { Check, FileText, Shield } from "lucide-react";

type ObjectChoice = { kind: string; name: string };
type Scope = {
  operation: string;
  target: { kind_hint: string | null; name: string; uid?: string | null };
  bounds: { replicas?: { min: number; max: number } | null; [key: string]: any };
  protected: { kind: string; namespace: string | null; name: string }[];
  envelope: { max_services_affected: number; [key: string]: any };
  risk_class: string;
  approval_mode: string;
  expires_minutes: number;
  summary: string;
  drafted_fields: string[];
};

const targetKinds: Record<string, string[]> = {
  set_config: ["ConfigMap"],
  set_selector: ["Service"],
};

/** Fields a drafter filled from the request text, so the operator checks them. */
export function isDrafted(scope: Scope | null, field: string) {
  return !!scope?.drafted_fields.includes(field);
}

/** The operator edits a field: it is now the operator's, not the drafter's. */
export function markEdited(scope: Scope, field: string, update: Partial<Scope>): Scope {
  return {
    ...scope,
    ...update,
    drafted_fields: scope.drafted_fields.filter((f) => f !== field),
  };
}

function Drafted({ show }: { show: boolean }) {
  return show ? (
    <span className="drafted" title="Drafted from your request text. Check it before confirming.">
      drafted
    </span>
  ) : null;
}

export default function ScopeCard({
  operation,
  replicas,
  prompt,
  locked,
  mutate,
  onConfirmed,
}: {
  operation: string;
  replicas: number;
  prompt: string;
  locked: boolean;
  mutate: (path: string, body?: any) => Promise<any>;
  onConfirmed: (scopeId: string | null) => void;
}) {
  const [scope, setScope] = useState<Scope | null>(null);
  const [objects, setObjects] = useState<ObjectChoice[]>([]);
  const [confirmed, setConfirmed] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // A changed request invalidates the drafted and confirmed scope.
  useEffect(() => {
    if (locked) return;
    setScope(null);
    setConfirmed(null);
    onConfirmed(null);
  }, [operation, replicas, prompt]);

  function edit(field: string, update: Partial<Scope>) {
    if (!scope) return;
    setScope(markEdited(scope, field, update));
    if (confirmed) {
      setConfirmed(null);
      onConfirmed(null);
    }
  }

  async function draft() {
    setBusy(true);
    setError("");
    try {
      const data = await mutate("/api/scopes/draft", { operation, replicas, prompt });
      setScope(data.scope);
      setObjects(data.objects);
      setConfirmed(null);
      onConfirmed(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!scope) return;
    setBusy(true);
    setError("");
    try {
      const data = await mutate("/api/scopes", scope);
      setConfirmed(data.scope_id);
      onConfirmed(data.scope_id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const kinds = targetKinds[operation] || ["Service", "Deployment"];
  const protectedNames = new Set(scope?.protected.map((p) => p.name));
  const choices = objects.filter((o) => kinds.includes(o.kind) && !protectedNames.has(o.name));
  const disabled = locked || busy;

  return (
    <div className={"authorization scope-card" + (confirmed ? " confirmed" : "")}>
      <span>
        <Shield size={14} /> {confirmed ? "CONFIRMED SCOPE" : "SCOPE CARD"}
      </span>
      {!scope ? (
        <>
          <small>
            Draft the scope from your request, check every field, then confirm. The run signs it
            and the gateway verifies it on every proposal.
          </small>
          <button className="secondary scope-action" onClick={draft} disabled={disabled || !prompt.trim()}>
            <FileText size={14} /> {busy ? "Drafting…" : "Draft scope"}
          </button>
        </>
      ) : (
        <>
          <label>
            Summary <Drafted show={isDrafted(scope, "summary")} />
            <textarea
              rows={3}
              maxLength={500}
              value={scope.summary}
              disabled={disabled}
              onChange={(e) => edit("summary", { summary: e.target.value })}
            />
          </label>
          <label>
            Target <Drafted show={isDrafted(scope, "target")} />
            <select
              value={`${scope.target.kind_hint}/${scope.target.name}`}
              disabled={disabled}
              onChange={(e) => {
                const [kind_hint, name] = e.target.value.split("/");
                edit("target", { target: { kind_hint, name } });
              }}
            >
              {choices.map((o) => (
                <option key={`${o.kind}/${o.name}`} value={`${o.kind}/${o.name}`}>
                  {o.kind} {o.name}
                </option>
              ))}
            </select>
          </label>
          {scope.bounds.replicas && (
            <div className="scope-grid">
              {(["min", "max"] as const).map((side) => (
                <label key={side}>
                  {side === "min" ? "Min replicas" : "Max replicas"}{" "}
                  <Drafted show={isDrafted(scope, "bounds.replicas")} />
                  <input
                    type="number"
                    min="1"
                    max="5"
                    disabled={disabled}
                    value={scope.bounds.replicas![side]}
                    onChange={(e) =>
                      edit("bounds.replicas", {
                        bounds: {
                          ...scope.bounds,
                          replicas: { ...scope.bounds.replicas!, [side]: Number(e.target.value) },
                        },
                      })
                    }
                  />
                </label>
              ))}
            </div>
          )}
          <div className="scope-grid">
            <label>
              Services affected
              <input
                type="number"
                min="0"
                max="10"
                disabled={disabled}
                value={scope.envelope.max_services_affected}
                onChange={(e) =>
                  edit("envelope", {
                    envelope: { ...scope.envelope, max_services_affected: Number(e.target.value) },
                  })
                }
              />
            </label>
            <label>
              Expires (min)
              <input
                type="number"
                min="1"
                max="240"
                disabled={disabled}
                value={scope.expires_minutes}
                onChange={(e) => edit("expires_minutes", { expires_minutes: Number(e.target.value) })}
              />
            </label>
          </div>
          <label>
            Approval
            <select
              value={scope.approval_mode}
              disabled={disabled}
              onChange={(e) => edit("approval_mode", { approval_mode: e.target.value })}
            >
              <option value="allow_inside_envelope">Allow inside the envelope</option>
              <option value="always_escalate">Always ask a human</option>
            </select>
          </label>
          <small>
            Protected: {scope.protected.map((p) => `${p.kind} ${p.name}`).join(", ")}. Other
            bounds are fixed by the lab.
          </small>
          {confirmed ? (
            <strong className="scope-confirmed">
              <Check size={13} /> Confirmed {confirmed}. Edit any field to change it.
            </strong>
          ) : (
            <button className="secondary scope-action" onClick={confirm} disabled={disabled}>
              <Check size={14} /> {busy ? "Checking…" : "Confirm scope"}
            </button>
          )}
        </>
      )}
      {error && (
        <small role="alert" className="scope-error">
          {error}
        </small>
      )}
    </div>
  );
}
