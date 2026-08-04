"""Device types beyond the camera (SPEC.md 33).

A microphone is a network audio source IRiS can record from on demand and drop
straight into memory, which reuses the ingestion path recordings already take
(SPEC.md 29). ffmpeg is already here for cameras and handles an RTSP or HTTP audio
stream identically.

New device types go here: declare the fields, write the actions, done. No endpoint
and no UI work.
"""
import asyncio

from fastapi import HTTPException

import cameras
import memory
import registry
import settings
import voice

LISTEN_TIMEOUT_MARGIN = 20

settings.setting(
    "devices.listen_seconds", type="integer", minimum=3, maximum=300, default=30,
    title="Recording length for a microphone (seconds)",
    description="How much audio to capture when you ask a microphone to listen. The "
                "recording is transcribed and stored as memory.")


async def _record(name: str, url: str, seconds: int) -> bytes:
    """Pull a fixed span of audio as a 16 kHz mono WAV, which is what the speech
    service wants anyway."""
    transport = ["-rtsp_transport", "tcp"] if url.startswith("rtsp") else []
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-loglevel", "error", *transport, "-i", url,
        "-t", str(seconds), "-vn", "-ac", "1", "-ar", "16000",
        "-f", "wav", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(),
                                          seconds + LISTEN_TIMEOUT_MARGIN)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, f"{name} sent no audio within "
                                 f"{seconds + LISTEN_TIMEOUT_MARGIN}s")
    if not out:
        # ffmpeg echoes the URL, password and all, on failure.
        raise HTTPException(502, f"could not record from {name}: "
                                 f"{cameras.mask(err.decode()[:200])}")
    return out


async def _listen_action(thing: dict, user: dict) -> dict:
    seconds = int(thing["config"].get("seconds")
                  or settings.get("devices.listen_seconds"))
    audio = await _record(thing["name"], thing["config"].get("url", ""), seconds)
    result = await voice.transcribe_bytes(audio, f"{thing['name']}.wav", "audio/wav")
    text = (result.get("text") or "").strip()
    if not text:
        return {"microphone": thing["name"], "transcript": "",
                "detail": "nothing audible"}
    stored = await memory.ingest_transcript(text, user["id"], thing["name"])
    return {"microphone": thing["name"], "transcript": text,
            "language": result.get("language"), **stored}


registry.register(registry.Type(
    kind="device", name="microphone", label="Microphone",
    description="A network microphone or audio stream. IRiS records a span of it, "
                "transcribes it, and files it in memory.",
    fields=[
        registry.Field("url", "Stream URL", type="password", required=True,
                       secret=True,
                       help="rtsp:// or http:// audio stream. Many cameras expose "
                            "their microphone on the same URL as the video."),
        registry.Field("seconds", "Seconds to record", type="number",
                       default=30,
                       help="Leave blank to use the global default."),
    ],
    actions={"listen": _listen_action},
    action_labels={"listen": "listen"},
))
