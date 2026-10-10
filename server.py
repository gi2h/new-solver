import asyncio
import logging
import secrets
import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from config import (
    SOLVER_KEY, MAX_CONCURRENT, QUEUE_SIZE,
    TASK_TTL_OK, TASK_TTL_ERR, SOLVE_TIMEOUT, PORT,
)
from solver import solve_turnstile, shutdown_browser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("server")

app = FastAPI()

TASKS = {}
QUEUE = None
RUNNING = 0
START_TIME = time.time()


async def _schedule_cleanup(task_id, ttl):
    await asyncio.sleep(ttl)
    TASKS.pop(task_id, None)


async def _do_solve(task_id, params):
    global RUNNING
    RUNNING += 1
    TASKS[task_id] = {"status": "processing", "taskId": task_id}
    log.info(f"[{task_id}] START (running={RUNNING})")

    try:
        result = await solve_turnstile(
            domain=params["domain"],
            sitekey=params["sitekey"],
            action=params.get("action"),
            cdata=params.get("cdata"),
            proxy=params.get("proxy"),
            timeout=SOLVE_TIMEOUT,
        )

        if result["success"]:
            TASKS[task_id] = {
                "status": "ready",
                "taskId": task_id,
                "solution": {"token": result["token"]},
            }
            log.info(f"[{task_id}] READY")
            asyncio.create_task(_schedule_cleanup(task_id, TASK_TTL_OK))
        else:
            TASKS[task_id] = {
                "status": "error",
                "taskId": task_id,
                "err": {
                    "usr": "ERROR_CAPTCHA_UNSOLVABLE",
                    "api": result["error"],
                    "msg": "Solver error. Coba lagi.",
                },
            }
            log.warning(f"[{task_id}] ERROR {result['error']}")
            asyncio.create_task(_schedule_cleanup(task_id, TASK_TTL_ERR))

    except Exception as e:
        log.exception(f"[{task_id}] exception")
        TASKS[task_id] = {
            "status": "error",
            "taskId": task_id,
            "err": {
                "usr": "ERROR_CAPTCHA_UNSOLVABLE",
                "api": str(e),
                "msg": "Internal error.",
            },
        }
        asyncio.create_task(_schedule_cleanup(task_id, TASK_TTL_ERR))

    finally:
        RUNNING -= 1
        log.info(f"[{task_id}] DONE (running={RUNNING})")


async def _worker(worker_id):
    log.info(f"Worker {worker_id} started")
    while True:
        try:
            task_id, params = await QUEUE.get()
        except Exception:
            continue
        try:
            await _do_solve(task_id, params)
        except Exception:
            log.exception("worker error")
        finally:
            QUEUE.task_done()


@app.on_event("startup")
async def startup():
    global QUEUE
    QUEUE = asyncio.Queue(maxsize=MAX_CONCURRENT + QUEUE_SIZE)
    for i in range(MAX_CONCURRENT):
        asyncio.create_task(_worker(i))
    log.info(
        f"Server ready MAX_CONCURRENT={MAX_CONCURRENT} "
        f"QUEUE_SIZE={QUEUE_SIZE} TIMEOUT={SOLVE_TIMEOUT}s"
    )


@app.on_event("shutdown")
async def shutdown():
    await shutdown_browser()


@app.get("/")
async def health():
    return {
        "status": "ok",
        "message": "ready",
        "version": "2.0-patchright",
        "uptime": int(time.time() - START_TIME),
        "running": RUNNING,
        "queue_size": QUEUE.qsize() if QUEUE else 0,
        "max_concurrent": MAX_CONCURRENT,
        "queue_capacity": QUEUE_SIZE,
        "tasks_in_memory": len(TASKS),
    }


@app.post("/solve")
async def solve(request: Request):
    key = request.headers.get("key") or request.headers.get("x-api-key")
    if key != SOLVER_KEY:
        return JSONResponse({
            "status": "error",
            "err": {
                "usr": "UNAUTHORIZED",
                "api": "Invalid solver key.",
                "msg": "Kunci solver salah.",
            },
        })

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({
            "status": "error",
            "err": {"usr": "BAD_PARAMETERS", "api": "invalid JSON", "msg": "Bad request."},
        })

    task_id = body.get("taskId")
    if task_id:
        t = TASKS.get(task_id)
        if not t:
            return JSONResponse({
                "status": "error",
                "taskId": task_id,
                "err": {
                    "usr": "TASK_NOT_FOUND",
                    "api": "taskId not found / expired",
                    "msg": "Task tidak ditemukan.",
                },
            })
        return JSONResponse(t)

    if body.get("method") != "CloudflareTask" or not isinstance(body.get("task"), dict):
        return JSONResponse({
            "status": "error",
            "err": {
                "usr": "BAD_PARAMETERS",
                "api": "method harus CloudflareTask & task objek",
                "msg": "Parameter tidak valid.",
            },
        })

    task = body["task"]
    method  = (task.get("type") or "").lower()
    domain  = task.get("websiteUrl")
    sitekey = task.get("websiteKey")
    action  = task.get("action")
    cdata   = task.get("cdata")
    proxy   = task.get("proxy")

    if method != "turnstile":
        return JSONResponse({
            "status": "error",
            "err": {
                "usr": "BAD_PARAMETERS",
                "api": "Hanya turnstile yang didukung",
                "msg": "Tipe tidak didukung.",
            },
        })

    if not domain or not sitekey:
        return JSONResponse({
            "status": "error",
            "err": {
                "usr": "BAD_PARAMETERS",
                "api": "websiteUrl & websiteKey wajib",
                "msg": "Parameter kurang.",
            },
        })

    new_tid = secrets.token_hex(6)
    params = {
        "domain": domain,
        "sitekey": sitekey,
        "action": action,
        "cdata": cdata,
        "proxy": proxy,
    }

    try:
        QUEUE.put_nowait((new_tid, params))
    except asyncio.QueueFull:
        return JSONResponse({
            "status": "error",
            "err": {
                "usr": "SOLVER_FULL",
                "api": f"queue full ({QUEUE.qsize()}/{QUEUE.maxsize})",
                "msg": "Server penuh. Coba lagi nanti.",
                "retry_after": 10,
            },
        })

    TASKS[new_tid] = {"status": "processing", "taskId": new_tid}

    return JSONResponse({
        "taskId": new_tid,
        "status": "processing",
        "msg": f"Queued (running={RUNNING}, queue={QUEUE.qsize()}). Poll taskId.",
    })


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
