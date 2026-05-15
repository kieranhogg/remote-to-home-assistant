# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "aiohttp>=3.13.2",
#     "asyncio>=4.0.0",
#     "evdev>=1.9.2",
#     "python-dotenv>=1.2.1",
#     "sounddevice>=0.5.1",
# ]
# ///

import json
import evdev
import asyncio
import aiohttp
import logging
import os
import sounddevice as sd
from dotenv import load_dotenv

load_dotenv()

INPUTS          = os.getenv("INPUTS")
DEVICES         = [evdev.InputDevice(f"/dev/input/event{num}") for num in INPUTS.split(",")]
BASE_API        = os.getenv("BASE_API")
API_KEY         = os.getenv("API_KEY")
HA_EVENT_NAME   = os.getenv("HA_EVENT_NAME")
GRAB_DEVICE     = os.getenv("GRAB_DEVICE")

# HA_WS_URL   : WebSocket URL e.g. ws://homeassistant.local:8123/api/websocket
# MIC_DEVICE  : sounddevice device index or name substring e.g. "0" or "USB Audio"
#               To list devices: python -c "import sounddevice; print(sounddevice.query_devices())"
# PIPELINE_ID : optional - leave blank to use HA's default/preferred pipeline
# MIC_SAMPLE_RATE: 16000 Hz is required by all HA STT backends
HA_WS_URL       = os.getenv("HA_WS_URL")
MIC_DEVICE_RAW  = os.getenv("MIC_DEVICE")
MIC_DEVICE      = int(MIC_DEVICE_RAW) if MIC_DEVICE_RAW and MIC_DEVICE_RAW.isdigit() else MIC_DEVICE_RAW
MIC_SAMPLE_RATE = int(os.getenv("MIC_SAMPLE_RATE", "16000"))
MIC_CHUNK_MS    = int(os.getenv("MIC_CHUNK_MS", "100"))
PIPELINE_ID     = os.getenv("PIPELINE_ID", None)
TTS_PLAYER      = os.getenv("TTS_PLAYER")      # e.g. media_player.living_room

VOICE_KEY       = "KEY_VOICECOMMAND"
# VOICE_MODE: "hold"    record while button held, send on release
#             "toggle"  first press starts, second press sends
VOICE_MODE      = os.getenv("VOICE_MODE", "hold")

EVENT_LOG_TEMPLATE = "Fired event {} with event data{}"
EVENT_PATH      = "events/" + HA_EVENT_NAME
BASE_API_URL    = BASE_API + EVENT_PATH
HEADERS         = {'content-type': 'application/json', 'Authorization': 'Bearer {}'.format(API_KEY)}
CMD             = "cmd"
CMD_TYPE        = "cmd_type"
CMD_NUM         = "cmd_num"
SPECIAL_KEYS    = {
    "KEY_7": {"map": "TOGGLE_AMP", "count": 0},
    "KEY_8": {"map": "TOGGLE_ATV", "count": 0},
}
UP   = 0
DOWN = 1
HOLD = 2

REPEAT_THROTTLE = 3

class AnsiColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord):
        no_style    = '\033[0m'
        grey        = '\033[90m'
        yellow      = '\033[93m'
        red         = '\033[31m'
        red_light   = '\033[91m'
        blue        = '\033[94m'
        start_style = {
            'DEBUG':    grey,
            'INFO':     blue,
            'WARNING':  yellow,
            'ERROR':    red,
            'CRITICAL': red_light + '\033[91m',
        }.get(record.levelname, no_style)
        return f'{start_style}{super().format(record)}{no_style}'

logger = logging.getLogger()
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
formatter = AnsiColorFormatter('{asctime} | {levelname:<8s} | {name:<20s} | {message}', style='{', datefmt='%H:%M')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)

async def play_tts(url: str, player_entity: str):
    """Call media_player.play_media with the TTS response URL."""
    # HA returns /api/tts_proxy/... build absolute URL from base host only,
    # stripping any trailing /api/ path that BASE_API may already include.
    base_host = BASE_API.rstrip("/")
    if "/api" in base_host:
        base_host = base_host[:base_host.index("/api")]
    if url.startswith("/"):
        url = base_host + url
    payload = {
        "entity_id": player_entity,
        "media_content_id": url,
        "media_content_type": "music",
    }
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            BASE_API + "services/media_player/play_media",
            data=json.dumps(payload),
            headers=HEADERS,
        )
        if resp.status != 200:
            body = await resp.text()
            logger.error(f"TTS playback failed (HTTP {resp.status}): {body}")
        else:
            logger.info(f"TTS playback {player_entity} ({url})")


async def fire_event(payload):
    async with aiohttp.ClientSession() as session:
        await session.post(BASE_API_URL, data=json.dumps(payload), headers=HEADERS)
        logger.debug(EVENT_LOG_TEMPLATE.format(HA_EVENT_NAME, payload))


def _wav_header(sample_rate: int, channels: int = 1, bit_depth: int = 16) -> bytes:
    """
    Build a streaming WAV header with unknown data length (0xFFFFFFFF).
    HA Cloud STT requires a WAV container, raw PCM alone is not accepted.
    """
    import struct
    byte_rate   = sample_rate * channels * bit_depth // 8
    block_align = channels * bit_depth // 8
    # fmt chunk: 16 bytes for PCM
    fmt  = struct.pack('<HHIIHH', 1, channels, sample_rate, byte_rate, block_align, bit_depth)
    # Use 0xFFFFFFFF for both RIFF and data chunk sizes (streaming / unknown length)
    return (
        b'RIFF' + struct.pack('<I', 0xFFFFFFFF) +
        b'WAVE' +
        b'fmt ' + struct.pack('<I', 16) + fmt +
        b'data' + struct.pack('<I', 0xFFFFFFFF)
    )


async def run_assist_pipeline(audio_queue: asyncio.Queue):
    """
    Open a WebSocket to HA, authenticate, start the Assist STT→Intent pipeline,
    stream raw PCM chunks from audio_queue, then log the result.

    Audio format: 16-bit signed PCM, mono, 16 kHz, little-endian — no WAV header.
    Sending an empty bytes frame signals end-of-audio to HA.
    """
    ws_headers = {"Authorization": f"Bearer {API_KEY}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(HA_WS_URL, headers=ws_headers) as ws:

                # Authenticate
                msg = await ws.receive_json()
                if msg.get("type") != "auth_required":
                    logger.error(f"Unexpected WS handshake: {msg}")
                    return
                await ws.send_json({"type": "auth", "access_token": API_KEY})
                msg = await ws.receive_json()
                if msg.get("type") != "auth_ok":
                    logger.error(f"HA WebSocket auth failed: {msg}")
                    return

                # Start the STT to Intent pipeline
                run_msg = {
                    "type": "assist_pipeline/run",
                    "id": 1,
                    "start_stage": "stt",
                    "end_stage": "tts",
                    "input": {"sample_rate": MIC_SAMPLE_RATE},
                }
                if PIPELINE_ID:
                    run_msg["pipeline"] = PIPELINE_ID
                await ws.send_json(run_msg)

                # Wait for HA to signal it's ready to receive audio
                while True:
                    msg = await ws.receive_json()
                    event = msg.get("event", {})
                    if event.get("type") == "stt-start":
                        handler_id = event.get("data", {}).get("runner_data", {}).get("stt_binary_handler_id", 1)
                        handler_prefix = bytes([handler_id])
                        logger.info(f"Assist pipeline ready, streaming audio (handler {handler_id})")
                        break
                    if event.get("type") == "error":
                        logger.error(f"Pipeline start error: {event.get('data')}")
                        return

                # HA Cloud STT expects WAV, not raw PCM - send header first.
                # Every binary frame must be prefixed with the handler ID byte.
                await ws.send_bytes(handler_prefix + _wav_header(MIC_SAMPLE_RATE))

                # Stream PCM chunks until None sentinel (button released)
                while True:
                    chunk = await audio_queue.get()
                    if chunk is None:
                        break
                    await ws.send_bytes(handler_prefix + chunk)

                # Empty bytes frame = end of audio stream
                await ws.send_bytes(handler_prefix)  # empty payload = end of audio
                logger.info("Audio stream ended, waiting for HA response...")

                # Collect and log result events
                async for raw in ws:
                    if raw.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data  = json.loads(raw.data)
                    event = data.get("event", {})
                    etype = event.get("type")

                    if etype == "stt-end":
                        text = event.get("data", {}).get("stt_output", {}).get("text", "")
                        logger.info(f"Recognised: \"{text}\"")

                    elif etype == "intent-end":
                        result = event.get("data", {}).get("intent_output", {})
                        logger.info(f"Intent result: {result}")

                    elif etype == "tts-end":
                        tts_url = event.get("data", {}).get("tts_output", {}).get("url", "")
                        logger.info(f"TTS URL: {tts_url}")
                        if TTS_PLAYER and tts_url:
                            await play_tts(tts_url, TTS_PLAYER)

                    elif etype == "error":
                        logger.error(f"Pipeline error: {event.get('data')}")
                        break

                    elif etype == "run-end":
                        logger.info("Assist pipeline run complete")
                        break

    except Exception as e:
        logger.error(f"Assist pipeline exception: {e}")


def make_audio_callback(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
    """
    sounddevice streams on a C audio thread - RawInputStream gives us plain
    bytes (int16 PCM) directly, so numpy is not required at all.
    """
    def callback(indata: bytes, frames, time_info, status):
        if status:
            logger.warning(f"Mic status: {status}")
        # indata is already raw int16 little-endian bytes -> pass straight through
        asyncio.run_coroutine_threadsafe(queue.put(bytes(indata)), loop)
    return callback


async def handle_voice_command(stop_event: asyncio.Event):
    """
    Record audio and send to HA's assist pipeline.

    hold mode:   mic opens immediately; audio is buffered locally until
                 stop_event fires, then streamed to HA all at once.
                 This avoids the race where UP arrives before the WebSocket
                 is even open (the ~2ms DOWN+UP hardware quirk).

    toggle mode: mic opens and streams live to HA; stop_event fires on
                 the second button press.
    """
    if not HA_WS_URL or not MIC_DEVICE_RAW:
        logger.warning("HA_WS_URL or MIC_DEVICE not configured, skipping voice command")
        return

    loop         = asyncio.get_event_loop()
    audio_queue: asyncio.Queue = asyncio.Queue()
    chunk_frames = int(MIC_SAMPLE_RATE * MIC_CHUNK_MS / 1000)

    stream = sd.RawInputStream(
        device=MIC_DEVICE,
        samplerate=MIC_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=chunk_frames,
        callback=make_audio_callback(loop, audio_queue),
    )

    if VOICE_MODE == "hold":
        # Buffer all audio locally first, then replay into the pipeline queue
        # after the button is released. This means the WebSocket handshake
        # happens after recording — no race condition with fast DOWN→UP keys.
        buffer = []
        with stream:
            logger.info("Recording (hold)")
            await stop_event.wait()
            logger.info("Stopped. sending to HA")

        # Drain anything the callback posted during the tiny window after
        # stop_event fired but before the stream closed
        while not audio_queue.empty():
            buffer.append(await audio_queue.get())

        # Replay buffer through a fresh queue into the pipeline
        replay_queue: asyncio.Queue = asyncio.Queue()
        for chunk in buffer:
            await replay_queue.put(chunk)
        await replay_queue.put(None)
        await run_assist_pipeline(replay_queue)

    else:
        # Toggle mode: stream live while waiting for second press
        with stream:
            pipeline_task = asyncio.ensure_future(run_assist_pipeline(audio_queue))
            await stop_event.wait()
            await audio_queue.put(None)
            logger.info("\U0001f3a4 Recording stopped \u2014 sending to HA")
            await pipeline_task


async def print_events(device):
    hold       = None
    last_key   = None
    hold_tick  = 0

    # Tracks an in-progress voice recording so we can signal it on key-up
    voice_stop_event: asyncio.Event | None = None

    async for event in device.async_read_loop():
        if event.type == evdev.ecodes.EV_KEY:
            logger.debug(evdev.categorize(event))
            if GRAB_DEVICE:
                device.grab()

            keycode = evdev.categorize(event).keycode
            cmd = keycode[0] if ("[" in keycode or type(keycode) == list) else str(keycode)

            if cmd == VOICE_KEY:
                if VOICE_MODE == "hold":
                    if event.value == DOWN and voice_stop_event is None:
                        voice_stop_event = asyncio.Event()
                        asyncio.ensure_future(handle_voice_command(voice_stop_event))
                    elif event.value == UP and voice_stop_event is not None:
                        voice_stop_event.set()
                        voice_stop_event = None
                else:  # toggle
                    if event.value == DOWN:
                        if voice_stop_event is None:
                            logger.info("🎙  Recording started (press again to send)")
                            voice_stop_event = asyncio.Event()
                            asyncio.ensure_future(handle_voice_command(voice_stop_event))
                        else:
                            voice_stop_event.set()
                            voice_stop_event = None
                if GRAB_DEVICE:
                    device.ungrab()
                last_key = cmd
                continue

            if event.value in [DOWN, UP]:
                hold_tick = 0
                cmd_type  = str(evdev.categorize(event)).split(",")[-1].replace(" ", "")
                cmd_num   = str(evdev.categorize(event).scancode)

                if event.value == UP:
                    if last_key is not None and last_key != cmd:
                        cmd = [last_key, cmd]
                    elif hold and last_key == cmd:
                        cmd_type = "hold"
                        hold = False
                    elif hold and last_key != cmd:
                        continue

                logger.info(cmd)
                await fire_event({CMD: cmd, CMD_TYPE: cmd_type, CMD_NUM: cmd_num})

            elif event.value == HOLD:
                hold = True
                hold_tick += 1
                if hold_tick % REPEAT_THROTTLE == 0:
                    cmd_num = str(evdev.categorize(event).scancode)
                    logger.info(f"{cmd} (repeat)")
                    await fire_event({CMD: cmd, CMD_TYPE: "repeat", CMD_NUM: cmd_num})

            if GRAB_DEVICE:
                device.ungrab()
            last_key = cmd


logger.info("Starting to listen...")
for device in DEVICES:
    asyncio.ensure_future(print_events(device))

loop = asyncio.get_event_loop()
loop.run_forever()
