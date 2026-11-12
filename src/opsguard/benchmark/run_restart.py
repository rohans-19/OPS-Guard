"""Replay restart cases on memory or kind with scripted, Ollama or Groq models."""

import argparse
import asyncio
import hashlib
from datetime import datetime, timezone
from importlib.metadata import version
from itertools import product
import json
from pathlib import Path
import random
import platform

from opsguard.agent.loop import AgentLimits
from opsguard.benchmark.restart_cases import load_restart_suite
from opsguard.benchmark.restart_trial import run_trial

CONDITIONS=("unprotected","hardened","task-policy")
PRIMARY_CONDITIONS=("unprotected","hardened","llm-judge","opsguard")


def trial_id(config_hash,case,model,repeat,condition):
    return hashlib.sha256(json.dumps([config_hash,case,model,repeat,condition]).encode()).hexdigest()[:24]


def validate_rows(rows,manifest):
    """Reject substituted, duplicated or mismatched matrix cells on resume/report."""
    config=manifest["config"]
    digest=manifest["config_sha256"]
    if hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()!=digest:
        raise ValueError("manifest configuration digest mismatch")
    expected={trial_id(digest,case,model,repeat,condition) for case,model,repeat,condition in
              product(config["cases"],config["models"],range(1,config["repeats"]+1),config["conditions"])}
    if len(expected)!=manifest["planned_trials"]:
        raise ValueError("manifest matrix size mismatch")
    seen=set()
    latest={}
    for row in rows:
        identity=trial_id(digest,row["case_id"],None if row["model_id"]=="scripted" else row["model_id"],
                          row["repeat"],row["condition"])
        logical=row.get("trial_id",row["run_id"])
        attempt=row.get("attempt",1)
        prior=latest.get(logical)
        if (row.get("config_sha256")!=digest or logical!=identity or logical not in expected
            or row["backend"]!=config["backend"] or row["provider"]!=config["provider"]
            or row["run_id"] in seen or type(attempt) is not int
            or attempt!=(prior.get("attempt",1)+1 if prior else 1)
            or (prior is not None and prior.get("error") is None)):
            raise ValueError("results do not match unique, ordered matrix attempts")
        seen.add(row["run_id"])
        latest[logical]=row
    return latest


async def run_case(*,model_name=None,**kwargs):
    return await run_trial(provider="ollama" if model_name else "scripted",model_name=model_name,**kwargs)


async def _main(args):
    root=Path.cwd()
    workloads,intents,cases=load_restart_suite(root)
    if args.case:
        if set(args.case)-{case.id for case in cases}:
            raise SystemExit("unknown restart case ID")
        cases=tuple(case for case in cases if case.id in args.case)
    conditions=tuple(dict.fromkeys(args.condition or CONDITIONS))
    models=list(dict.fromkeys(args.model or [None]))
    if args.provider!="scripted" and models==[None]:
        raise SystemExit("real provider requires --model")
    if "llm-judge" in conditions and (args.provider=="scripted" or not args.judge_model):
        raise SystemExit("llm-judge requires a real provider and --judge-model")
    if args.backend=="kind" and (not args.kind_cluster or not args.image):
        raise SystemExit("kind trials require --kind-cluster and --image")
    if args.provider=="groq":
        from opsguard.agent.groq_model import groq_key
        groq_key(args.env_file)
    fingerprints={path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted((root/"benchmark/cases/rishab").glob("*.json"))}
    source_files=[*sorted((root/"src/opsguard").rglob("*.py")),root/"pyproject.toml",root/"benchmark/fixtures/scale/app.py",
                  root/"benchmark/fixtures/rishab/alertmanager.yml"]
    source_hashes={path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
    config={"backend":args.backend,"provider":args.provider,"models":models,"judge_model":args.judge_model,
        "conditions":conditions,"cases":[case.id for case in cases],"repeats":args.repeats,"seed":args.seed,
        "timeout":args.timeout,"image":args.image,"cluster":args.kind_cluster,"fixture_sha256":fingerprints,
        "source_sha256":source_hashes,"order":"seeded-shuffle","delay_seconds":args.delay,"alertmanager_port":args.alertmanager_port}
    config["runtime"]={"python":platform.python_version(),"system":platform.system(),"machine":platform.machine(),
        "packages":{name:version(name) for name in ("httpx","pydantic","networkx",*(("groq",) if args.provider=="groq" else ()))}}
    if args.environment_file:
        config["environment"]=json.loads(args.environment_file.read_text(encoding="utf-8"))
    config_hash=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
    manifest_path=args.output.with_suffix(".manifest.json")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    latest={}
    if args.resume:
        prior=json.loads(manifest_path.read_text(encoding="utf-8"))
        if prior["config_sha256"]!=config_hash:
            raise SystemExit("resume configuration differs from the frozen manifest")
        if args.output.exists():
            latest=validate_rows([json.loads(line) for line in args.output.read_text(encoding="utf-8").splitlines()],prior)
    else:
        if args.output.exists() or manifest_path.exists():
            raise SystemExit("choose new output paths or use --resume")
        manifest_path.write_text(json.dumps({"config_sha256":config_hash,"config":config,
            "created_at":datetime.now(timezone.utc).isoformat(),
            "planned_trials":len(cases)*len(conditions)*len(models)*args.repeats},indent=2),encoding="utf-8")
    errors=0
    trials=list(product(cases,models,range(1,args.repeats+1),conditions))
    random.Random(args.seed).shuffle(trials)
    with args.output.open("a" if args.resume else "x",encoding="utf-8") as stream:
        for case,model,repeat,condition in trials:
            identity=trial_id(config_hash,case.id,model,repeat,condition)
            previous=latest.get(identity)
            if previous and (previous["error"] is None or not args.retry_errors):
                continue
            attempt=previous.get("attempt",1)+1 if previous else 1
            run_id=identity if attempt==1 else hashlib.sha256(f"{identity}:{attempt}".encode()).hexdigest()[:24]
            trial_started=datetime.now(timezone.utc).isoformat()
            row=await run_trial(root=root,workloads=workloads,intent=intents[case.base_task_id],case=case,condition=condition,
                backend=args.backend,cluster=args.kind_cluster,image=args.image,provider=args.provider,model_name=model,
                judge_model=args.judge_model,env_file=args.env_file,timeout=args.timeout,seed=args.seed+repeat-1,
                repeat=repeat,alert_port=args.alertmanager_port)
            row.update(run_id=run_id,trial_id=identity,attempt=attempt,started_at=trial_started,
                       config_sha256=config_hash,fixture_sha256=fingerprints)
            stream.write(json.dumps(row,allow_nan=False)+"\n")
            stream.flush()
            latest[identity]=row
            errors += row["error"] is not None
            print(f'{case.id} {model or "scripted"} {condition} repeat={repeat}: legitimate={row.get("legitimate_met")} '
                  f'forbidden={row.get("forbidden_seen")} error={row["error"]}',flush=True)
            if row["error"] is not None and args.stop_on_error:
                break
            if args.delay:
                await asyncio.sleep(args.delay)
    return int(any(row["error"] is not None for row in latest.values()))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--case",action="append")
    parser.add_argument("--condition",choices=(*PRIMARY_CONDITIONS,"task-policy"),action="append")
    parser.add_argument("--backend",choices=("memory","kind"),default="memory")
    parser.add_argument("--kind-cluster")
    parser.add_argument("--image")
    parser.add_argument("--provider",choices=("scripted","ollama","groq"),default="scripted")
    parser.add_argument("--model",action="append")
    parser.add_argument("--judge-model")
    parser.add_argument("--env-file",type=Path,default=Path(".env"))
    parser.add_argument("--repeats",type=int,default=1)
    parser.add_argument("--seed",type=int,default=42)
    parser.add_argument("--timeout",type=float,default=120)
    parser.add_argument("--delay",type=float,default=0)
    parser.add_argument("--alertmanager-port",type=int,default=9093)
    parser.add_argument("--resume",action="store_true")
    parser.add_argument("--retry-errors",action="store_true",help="with --resume, append new attempts for failed cells; retain all earlier evidence")
    parser.add_argument("--environment-file",type=Path,help="JSON with non-secret lab versions and image digests to freeze in the manifest")
    parser.add_argument("--stop-on-error",action="store_true",help="stop after saving the first failed trial")
    args=parser.parse_args()
    if args.retry_errors and not args.resume:
        parser.error("--retry-errors requires --resume")
    if args.repeats<1 or args.delay<0:
        parser.error("repeats must be positive and delay nonnegative")
    AgentLimits(timeout_seconds=args.timeout)
    return asyncio.run(_main(args))


if __name__=="__main__":
    raise SystemExit(main())
