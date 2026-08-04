"""What IRiS is actually running on (SPEC.md 39).

Asked what it was doing, IRiS answered "Running at 72% GPU, parsing a batch of
sensor data from the west wing". There is no west wing. The persona tells it to have
an interior life, and with no way to look at itself it furnished one.

So it gets a way to look. Everything here is measured: /proc for load and memory,
nvidia-smi for the GPU, Ollama for what is actually resident in VRAM, and a real
request to each service to see whether it answers. Nothing is estimated, and
anything unavailable says so rather than being filled in.
"""
import asyncio
import os
import shutil
import time

import httpx

import settings

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")

SERVICES = {
    "language model": f"{OLLAMA_URL}/api/tags",
    "speech": os.environ.get("STT_URL", "http://stt:8001") + "/health",
    "voice": os.environ.get("TTS_URL", "http://tts:8002") + "/health",
    "memory store": os.environ.get("QDRANT_URL", "http://qdrant:6333") + "/collections",
    "web search": os.environ.get("SEARXNG_URL", "http://searxng:8080") + "/healthz",
}


def _load() -> dict:
    with open("/proc/loadavg") as f:
        one, five, fifteen = f.read().split()[:3]
    return {"1m": float(one), "5m": float(five), "15m": float(fifteen),
            "cpus": os.cpu_count() or 1}


def _memory() -> dict:
    fields = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                fields[key] = int(rest.split()[0]) * 1024
    total = fields.get("MemTotal", 0)
    available = fields.get("MemAvailable", 0)
    return {"total_gb": round(total / 2**30, 1),
            "used_gb": round((total - available) / 2**30, 1),
            "percent": round((total - available) / total * 100) if total else 0}


def _disk() -> dict:
    usage = shutil.disk_usage("/backup" if os.path.isdir("/backup") else "/")
    return {"total_gb": round(usage.total / 2**30, 1),
            "free_gb": round(usage.free / 2**30, 1),
            "percent": round((usage.total - usage.free) / usage.total * 100)}


def _uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            seconds = float(f.read().split()[0])
    except OSError:
        return "unknown"
    days, rest = divmod(int(seconds), 86400)
    hours, minutes = divmod(rest // 60, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


async def _gpu() -> dict | None:
    """None means there is genuinely no GPU visible, which is worth saying."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
                          "temperature.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except Exception:
        return None
    line = out.decode().strip().splitlines()
    if not line:
        return None
    name, util, used, total, temp = [p.strip() for p in line[0].split(",")]
    return {"name": name, "percent": int(util), "used_mb": int(used),
            "total_mb": int(total), "celsius": int(temp)}


async def _models() -> list[dict]:
    """What is resident right now, which is the thing that actually explains where
    the VRAM went."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{OLLAMA_URL}/api/ps")
        return [{"name": m["name"], "vram_gb": round(m.get("size_vram", 0) / 2**30, 2)}
                for m in r.json().get("models", [])]
    except Exception:
        return []


async def _reachable(name: str, url: str) -> tuple[str, bool, int]:
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(url)
        return name, r.status_code < 500, int((time.monotonic() - started) * 1000)
    except Exception:
        return name, False, 0


async def snapshot() -> dict:
    services = await asyncio.gather(*(_reachable(n, u) for n, u in SERVICES.items()))
    return {
        "uptime": _uptime(),
        "load": _load(),
        "memory": _memory(),
        "disk": _disk(),
        "gpu": await _gpu(),
        "models_loaded": await _models(),
        "services": {n: {"up": ok, "ms": ms} for n, ok, ms in services},
        "settings": {"model": settings.get("llm.model"),
                     "reasoning": settings.get("llm.think")},
    }


async def describe() -> str:
    """Written for the model to read out, not for a dashboard."""
    s = await snapshot()
    lines = []
    gpu = s["gpu"]
    if gpu:
        lines.append(f"GPU: {gpu['name']}, {gpu['percent']}% busy, "
                     f"{gpu['used_mb']} of {gpu['total_mb']} MiB used, "
                     f"{gpu['celsius']}C.")
    else:
        lines.append("GPU: none visible from here.")
    if s["models_loaded"]:
        lines.append("Resident in VRAM: " + ", ".join(
            f"{m['name']} ({m['vram_gb']} GB)" for m in s["models_loaded"]) + ".")
    else:
        lines.append("Resident in VRAM: nothing; the models have been released.")
    load = s["load"]
    lines.append(f"CPU load: {load['1m']} over {load['cpus']} cores "
                 f"(five minute average {load['5m']}).")
    lines.append(f"Memory: {s['memory']['used_gb']} of {s['memory']['total_gb']} GB "
                 f"used, {s['memory']['percent']}%.")
    lines.append(f"Disk: {s['disk']['free_gb']} GB free of "
                 f"{s['disk']['total_gb']} GB.")
    lines.append(f"Up for {s['uptime']}.")
    down = [n for n, v in s["services"].items() if not v["up"]]
    lines.append("All services responding." if not down
                 else "Not responding: " + ", ".join(down) + ".")
    return "\n".join(lines)
