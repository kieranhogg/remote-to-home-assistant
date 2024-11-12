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
UP = 0
DOWN = 1
HOLD = 2

logging.basicConfig(filename="remote.log",
                    filemode='a',
                    format='%(asctime)s [%(name)s] %(levelname)s %(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.DEBUG)

logger = logging.getLogger('remote-log')
logger.info("Starting to listen...")

async def print_events(device):
    hold = None
    async for event in device.async_read_loop():
        if event.type == evdev.ecodes.EV_KEY:
            if GRAB_DEVICE:
                device.grab()
            if isinstance(type(evdev.categorize(event).keycode), list):
                cmd = evdev.categorize(event).keycode[0]
            else:
                cmd = str(evdev.categorize(event).keycode)
            if event.value == UP:
                cmd_type = str(evdev.categorize(event)).split(",")[-1].replace(" ","")
                cmd_num = str(evdev.categorize(event).scancode)
                if hold:
                    cmd_type = "hold"
                    hold = False
                payload = {CMD: cmd, CMD_TYPE: cmd_type, CMD_NUM: cmd_num}
                async with aiohttp.ClientSession() as session:
                    await session.post(BASE_API_URL, data=json.dumps(payload), headers=HEADERS)
                    logger.info(EVENT_LOG_TEMPLATE.format(HA_EVENT_NAME,payload))
                    await session.close()
            elif event.value == HOLD:
                hold = True
            if GRAB_DEVICE:
                device.ungrab()

for device in DEVICES:
    asyncio.ensure_future(print_events(device))

loop = asyncio.get_event_loop()
loop.run_forever()

