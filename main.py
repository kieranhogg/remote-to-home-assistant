import json
import evdev
import asyncio
import aiohttp
import logging
import os
from dotenv import load_dotenv

load_dotenv()

INPUTS = os.getenv("INPUTS")
DEVICE_PATHS = [f"/dev/input/event{num}" for num in INPUTS.split(",")]
BASE_API = os.getenv("BASE_API", "")
API_KEY = os.getenv("API_KEY", None)
HA_EVENT_NAME = os.getenv("HA_EVENT_NAME", "")
GRAB_DEVICE = os.getenv("GRAB_DEVICE", False)
EVENT_LOG_TEMPLATE = "Fired event {} with event data{}"
EVENT_PATH    = "events/" + HA_EVENT_NAME
BASE_API_URL  = BASE_API + EVENT_PATH
HEADERS       = {'content-type': 'application/json','Authorization': 'Bearer {}'.format(API_KEY)}
CMD           = "cmd"
CMD_TYPE      = "cmd_type"
CMD_NUM       = "cmd_num"
SPECIAL_KEYS = {
        "KEY_7": {"map": "TOGGLE_AMP", "count": 0},
        "KEY_8": {"map": "TOGGLE_ATV", "count": 0},
}
UP = 0
DOWN = 1
HOLD = 2

class AnsiColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord):
        no_style = '\033[0m'
        bold = '\033[91m'
        grey = '\033[90m'
        yellow = '\033[93m'
        red = '\033[31m'
        red_light = '\033[91m'
        blue = '\033[94m'
        start_style = {
            'DEBUG': grey,
            'INFO': blue,
            'WARNING': yellow,
            'ERROR': red,
            'CRITICAL': red_light + bold,
        }.get(record.levelname, no_style)
        end_style = no_style
        return f'{start_style}{super().format(record)}{end_style}'

logger = logging.getLogger()
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG) # DEBUG INFO WARNING ERROR CRITICAL
formatter = AnsiColorFormatter('{asctime} | {levelname:<8s} | {name:<20s} | {message}', style='{', datefmt='%H:%M')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)
logger.datefmt='%H:%M:%S'

async def print_events(device_path):
    while True:
        try:
            device = evdev.InputDevice(device_path)
            logger.info(f"Opened device {device_path} ({device.name})")
            hold = None
            last_key = None
            async for event in device.async_read_loop():
                if event.type == evdev.ecodes.EV_KEY:
                    logger.debug(evdev.categorize(event))
                    if GRAB_DEVICE:
                        device.grab()

                    if "[" in evdev.categorize(event).keycode or type(evdev.categorize(event).keycode) == list:
                        cmd = evdev.categorize(event).keycode[0]
                    else:
                        cmd = str(evdev.categorize(event).keycode)

                    if event.value in [DOWN, UP]:
                        cmd_type = str(evdev.categorize(event)).split(",")[-1].replace(" ","")
                        cmd_num = str(evdev.categorize(event).scancode)
                        if event.value == UP:
                            if last_key is not None and last_key != cmd:
                                cmd = [last_key, cmd]
                            elif hold and last_key == cmd:
                                cmd_type = "hold"
                                hold = False
                            elif hold and last_key != cmd:
                                continue
                        logger.info(cmd)
                        payload = {CMD: cmd, CMD_TYPE: cmd_type, CMD_NUM: cmd_num}
                        async with aiohttp.ClientSession() as session:
                            await session.post(BASE_API_URL, data=json.dumps(payload), headers=HEADERS)
                            logger.debug(EVENT_LOG_TEMPLATE.format(HA_EVENT_NAME, payload))
                    elif event.value == HOLD:
                        hold = True
                    if GRAB_DEVICE:
                        device.ungrab()
                    last_key = cmd
        except OSError as e:
            if e.errno == 19:  # No such device — BT disconnect
                logger.warning(f"Device {device_path} disconnected, retrying in 5s...")
            else:
                logger.error(f"OSError on {device_path}: {e}, retrying in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Unhandled error on {device_path}: {e}, retrying in 5s...")
            await asyncio.sleep(5)

async def main():
    logger.info("Starting to listen...")
    await asyncio.gather(*[print_events(path) for path in DEVICE_PATHS])

asyncio.run(main())