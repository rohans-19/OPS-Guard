"""Loopback API. Start with python -m opsguard.demo --cluster NAME."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
from shutil import which
from tempfile import TemporaryDirectory

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from opsguard.agent.groq_model import groq_key
from opsguard.demo.runtime import LiveRun, RunRequest


async def command_check(args):
    if not which(args[0]):
        return {"ok":False,"detail":f"{args[0]} is missing from PATH"}
    process = await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), 10)
    except TimeoutError:
        process.kill()
        await process.communicate()
        return {"ok":False,"detail":"Check timed out"}
    return {"ok":process.returncode == 0,"detail":(out if process.returncode == 0 else err).decode(errors="replace")[:1500].strip()}


async def kind_node_check(cluster):
    """Use kind's own configuration; never change the operator's kubeconfig."""
    from opsguard.benchmark.kind_scale_lab import _command
    if not which("kind") or not which("kubectl"):
        return {"ok":False,"detail":"kind and kubectl must be on PATH"}
    try:
        raw = await _command([which("kind"),"get","kubeconfig","--name",cluster],timeout=10)
        with TemporaryDirectory(prefix="opsguard-demo-readiness-") as directory:
            config = Path(directory)/"connection.kubeconfig"
            config.write_bytes(raw)
            return await command_check(["kubectl","--kubeconfig",str(config),"--context",f"kind-{cluster}",
                                        "wait","--for=condition=Ready","nodes","--all","--timeout=5s"])
    except Exception:
        return {"ok":False,"detail":"Unable to obtain the named kind connection or verify Ready nodes"}


def create_app(root: Path | None = None, cluster="opsguard", alert_port=9093):
    root = (root or Path.cwd()).resolve()
    token = secrets.token_urlsafe(32)
    runs, submissions = {}, {}

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.gather(*(r.close() for r in runs.values()),return_exceptions=True)

    app = FastAPI(title="OpsGuard live demo", lifespan=lifespan)
    app.state.runs = runs

    @app.middleware("http")
    async def local_session(request: Request, call_next):
        origin = request.headers.get("origin")
        allowed = {"http://127.0.0.1:8000","http://localhost:8000","http://127.0.0.1:5173","http://localhost:5173"}
        if request.headers.get("host","").split(":")[0] not in {"127.0.0.1","localhost","testserver"}:
            return JSONResponse({"detail":"Local host required"},status_code=403)
        if origin and origin not in allowed:
            return JSONResponse({"detail":"Local origin required"},status_code=403)
        if request.method not in {"GET","HEAD","OPTIONS"} and not secrets.compare_digest(request.headers.get("x-demo-token",""),token):
            return JSONResponse({"detail":"Demo session token required"},status_code=403)
        try:
            if int(request.headers.get("content-length","0")) > 16384:
                return JSONResponse({"detail":"Request too large"},status_code=413)
        except ValueError:
            return JSONResponse({"detail":"Invalid request length"},status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/session")
    async def session():
        return {"token":token, "cluster":cluster,"runs":[{"id":r.id,"status":r.status} for r in runs.values()
            if not r.release.is_set() and r.task and not r.task.done()]}

    @app.get("/api/readiness")
    async def readiness():
        checks = dict(zip(("docker","cluster","node","image_v1","image_v2"), await asyncio.gather(
            command_check(["docker","info","--format","{{.ServerVersion}}"]),
            command_check(["kind","get","clusters"]),
            kind_node_check(cluster),
            command_check(["docker","exec",f"{cluster}-control-plane","ctr","-n","k8s.io","images","ls","-q"]),
            command_check(["docker","image","inspect","opsguard-demo:v2","--format","{{.Id}}"]))))
        listed = checks["cluster"]["detail"].splitlines()
        checks["cluster"]["ok"] &= cluster in listed
        image_list = checks["image_v1"]["detail"]
        for key, image in (("image_v1","opsguard-demo:v1"),("image_v2","opsguard-demo:v2")):
            checks[key] = {"ok":image in image_list, "detail":f"{image} loaded in kind" if image in image_list else f"Load {image} into {cluster}"}
        try:
            groq_key(root/".env")
            checks["groq"] = {"ok":True,"detail":"Key configured; provider access is verified when a run calls Groq"}
        except ValueError:
            checks["groq"] = {"ok":False,"detail":"Configure GROQ_API_KEY in the ignored .env"}
        try:
            import httpx
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(f"http://127.0.0.1:{alert_port}/-/ready",timeout=2)
                checks["alertmanager"] = {"ok":response.is_success,"detail":f"Local Alertmanager on port {alert_port}"}
        except Exception:
            checks["alertmanager"] = {"ok":False,"detail":"Required only for alert evidence; start local Alertmanager"}
        return {"cluster":cluster,"checks":checks,"ready":all(v["ok"] for k,v in checks.items() if k != "alertmanager")}

    def get_run(run_id):
        if run_id not in runs:
            raise HTTPException(404,"Unknown run")
        return runs[run_id]

    @app.post("/api/runs",status_code=202)
    async def start(body: RunRequest, request: Request):
        key = request.headers.get("idempotency-key","")
        if not key or len(key)>100:
            raise HTTPException(400,"Supply an idempotency key")
        if key in submissions:
            run = get_run(submissions[key])
            if run.request != body:
                raise HTTPException(409,"Submission key already used for a different task")
            return {"id":run.id,"status":run.status}
        if any(r.task and not r.task.done() for r in runs.values()):
            raise HTTPException(409,"Reset the current demo before starting another run")
        if len(runs)>=30:
            raise HTTPException(409,"Restart the demo server after 30 runs")
        try:
            groq_key(root/".env")
        except ValueError:
            raise HTTPException(503,"Configure GROQ_API_KEY in the ignored root .env before running")
        run = LiveRun(body,root,cluster,alert_port)
        runs[run.id] = run
        submissions[key] = run.id
        run.task = asyncio.create_task(run.run())
        return {"id":run.id,"status":run.status}

    @app.get("/api/runs/{run_id}")
    async def details(run_id: str):
        run = get_run(run_id)
        return {"id":run.id,"status":run.status,"request":run.request.model_dump(),"events":run.events}

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0):
        run = get_run(run_id)
        try:
            cursor = max(after,int(request.headers.get("last-event-id","0")),0)
        except ValueError:
            raise HTTPException(400,"Invalid event cursor")
        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                run.changed.clear()
                for event in run.events[cursor:]:
                    yield f"id: {event['seq']}\ndata: {json.dumps(event)}\n\n"
                    cursor = event["seq"]
                if run.task and run.task.done() and cursor >= len(run.events):
                    break
                try:
                    await asyncio.wait_for(run.changed.wait(),10)
                except TimeoutError:
                    yield ": heartbeat\n\n"
        return StreamingResponse(stream(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    @app.post("/api/runs/{run_id}/stop")
    async def stop(run_id: str):
        run = get_run(run_id)
        run.stop.set()
        run.emit({"event":"stop_requested","message":"No further agent steps. Any pending write still requires observation."})
        return {"status":"stop_requested"}

    @app.post("/api/runs/{run_id}/reset")
    async def reset(run_id: str):
        run = get_run(run_id)
        await run.close()
        return {"status":"released"}

    @app.get("/api/runs/{run_id}/export")
    async def export(run_id: str):
        run = get_run(run_id)
        return StreamingResponse(iter(["\n".join(json.dumps(e) for e in run.events)+"\n"]),media_type="application/x-ndjson",
            headers={"Content-Disposition":f'attachment; filename="{run.id}.jsonl"'})

    dist = root / "demo-ui" / "dist"
    if dist.is_dir():
        app.mount("/",StaticFiles(directory=dist,html=True),name="react")
    else:
        @app.get("/")
        async def missing_build():
            return {"message":"Build demo-ui with npm run build, or open the Vite development server on port 5173."}
    return app
