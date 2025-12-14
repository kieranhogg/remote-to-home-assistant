# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "aiohttp>=3.13.2",
#     "asyncio>=4.0.0",
#     "evdev>=1.9.2",
#     "python-dotenv>=1.2.1",
# ]
# ///

import json
import evdev
import asyncio
import aiohttp
import logging
import os
from dotenv import load_dotenv

load_dotenv()

INPUTS = os.getenv("INPUTS")
DEVICES = [evdev.InputDevice(f"/dev/input/event{num}") for num in INPUTS.split(",")]
BASE_API = os.getenv("BASE_API")
API_KEY = os.getenv("API_KEY")
HA_EVENT_NAME = os.getenv("HA_EVENT_NAME")
GRAB_DEVICE = os.getenv("GRAB_DEVICE")
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

async def print_events(device):
    hold = None
    last_key = None
    async for event in device.async_read_loop():
        if event.type == evdev.ecodes.EV_KEY:
            #print(f"last key is {last_key}")
            logger.debug(evdev.categorize(event))
            if GRAB_DEVICE:
                device.grab()
            #print(evdev.categorize(event).keycode)
            #print(event.value)
            if "[" in evdev.categorize(event).keycode or type(evdev.categorize(event).keycode) == list:
                cmd = evdev.categorize(event).keycode[0]
            else:
                cmd = str(evdev.categorize(event).keycode)
            #if event.value == DOWN:
            #    last_key = cmd

            # TODO: map()?
            if 1==0 and cmd in SPECIAL_KEYS and event.value == UP:
                SPECIAL_KEYS[cmd]["count"] += 1
                for key in SPECIAL_KEYS.keys():
                    if key != cmd:
                        SPECIAL_KEYS[key]["count"] = 0
                if SPECIAL_KEYS[cmd]["count"] == 3:
                    cmd = SPECIAL_KEYS[cmd]["map"]
                    logger.debug(cmd)
                    SPECIAL_KEYS[cmd]["count"] = 0
            elif 1==0 and event.value == UP:
                for key in SPECIAL_KEYS.keys():
                    SPECIAL_KEYS[key]["count"] = 0

            if event.value in [DOWN, UP]:
                cmd_type = str(evdev.categorize(event)).split(",")[-1].replace(" ","")
                cmd_num = str(evdev.categorize(event).scancode)
                if event.value == UP:
                    if last_key is not None and last_key != cmd:
                        cmd = [last_key, cmd]
                        #print(f"was {last_key}, now {cmd}")
                        #print(cmd + "_" + last_key)
                    elif hold and last_key == cmd:
                        #cmd += "_HOLD"
                        cmd_type = "hold"
                        hold = False
                    elif hold and last_key != cmd:
                        continue
                logger.info(cmd)
                payload = {CMD: cmd, CMD_TYPE: cmd_type, CMD_NUM: cmd_num}
                async with aiohttp.ClientSession() as session:
                    await session.post(BASE_API_URL, data=json.dumps(payload), headers=HEADERS)
                    logger.debug(EVENT_LOG_TEMPLATE.format(HA_EVENT_NAME,payload))
                    await session.close()
            elif event.value == HOLD:
                hold = True
            if GRAB_DEVICE:
                device.ungrab()
            last_key = cmd

logger.info("Starting to listen...")
for device in DEVICES:
    asyncio.ensure_future(print_events(device))

loop = asyncio.get_event_loop()
loop.run_forever()

