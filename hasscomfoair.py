#!/usr/bin/env python3
#
# Copyright (c) 2020 Erik Simko
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

import argparse
import asyncio
import logging
from datetime import datetime
import sys 
import os
import json
from comfoair.asyncio import ComfoAir
from asyncio_mqtt import Client, MqttError


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# MQTT Topics
# Publish
TOPIC_TEMP_OUTSIDE = "comfoair/temp/outside"
TOPIC_AIRFLOW_EXHAUST = 'comfoair/airflow/exhaust'
TOPIC_AIRFLOW_SUPPLY = 'comfoair/airflow/supply'
TOPIC_FAN_SPEED = 'comfoair/fan/speed'
TOPIC_FAN_STATE = 'comfoair/fan/state'
TOPIC_AVAILABILITY = 'comfoair/LWT'
# Subscribe
TOPIC_SET_FAN_SPEED = "comfoair/fan/speed/set"
TOPIC_SET_FAN_STATE = 'comfoair/fan/state/set'

def decode_display_data(data):
    cc_segments = (
        ('Sa', 'Su', 'Mo', 'Tu', 'We', 'Th', 'Fri', ':'),
        # 1-2: Hour, e.g 3, 13 or 23
        ('1ADEG', '1B', '1C', 'AUTO', 'MANUAL', 'FILTER', 'I', 'E'),
        ('2A', '2B', '2C', '2D', '2E', '2F', '2G', 'Ventilation'),
        # 3-4: Minutes, e.g 52
        ('3A', '3B', '3C', '3D', '3E', '3F', '3G', 'Extractor hood'),
        ('4A', '4B', '4C', '4D', '4E', '4F', '4G', 'Pre-heater'),
        # 5: Stufe, 1, 2, 3 or A
        ('5A', '5B', '5C', '5D', '5E', '5F', '5G', 'Frost'),
        # 6-9: Comfort temperature, e.g. 12.0°C
        ('6A', '6B', '6C', '6D', '6E', '6F', '6G', 'EWT'),
        ('7A', '7B', '7C', '7D', '7E', '7F', '7G', 'Post-heater'),
        ('8A', '8B', '8C', '8D', '8E', '8F', '8G', '.'),
        ('°', 'Bypass', '9AEF', '9G', '9D', 'House', 'Supply air', 'Exhaust air'),
    )

    ssd_chr = {
        0b0000000: ' ',
        0b0111111: '0',
        0b0000110: '1',
        0b1011011: '2',
        0b1001111: '3',
        0b1100110: '4',
        0b1101101: '5',
        0b1111101: '6',
        0b0000111: '7',
        0b1111111: '8',
        0b1101111: '9',
        0b1110111: 'A',
        0b1111100: 'B',
        0b0111001: 'C',
        0b1011110: 'D',
        0b1111001: 'E',
        0b1110001: 'F',
    }

    if len(cc_segments) == len(data):
        segments = []
        for pos, val in enumerate(data):
            if pos == 1:
                digit = val & 6
                if val & 1:
                    digit |= 0b1011001
                assert digit in ssd_chr
                segments.append(ssd_chr[digit])
                offset = 3
            elif 2 <= pos <= 8:
                digit = val & 0x7f
                assert digit in ssd_chr
                segments.append(ssd_chr[digit])
                offset = 7
            elif pos == 9:
                digit = 0
                if val & 4:
                    digit |= 0b0110001
                if val & 8:
                    digit |= 0b1000000
                if val & 0x10:
                    digit |= 0b0001000
                assert digit in ssd_chr
                segments.append(ssd_chr[digit])
                for i in (0, 1, 5, 6, 7):
                    if val & (1 << i):
                        segments.append(cc_segments[pos][i])
                offset = 8
            else:
                offset = 0

            for i in range(offset, 8):
                if val & (1 << i):
                    segments.append(cc_segments[pos][i])

        logger.info('Segments: [%s]', '|'.join(segments))



class ComfoAirHandler():
    SPEED_VALUES = ["auto", "off", "low", "medium", "high"]
    STATE_VALUES = ["on", "off"]

    def __init__(self, mqtt):
        self._mqtt = mqtt
        self._cache = {}

    def invalidate_cache(self):
        self._cache = {}

    async def event(self, ev):
        cmd, data = ev
        if self._cache.get(cmd) == data:
            return
        self._cache[cmd] = data

        logger.info('Msg %#x: [%s]' % (cmd, data.hex()))

        if cmd == 0x3c:
            decode_display_data(data)

    async def temp_outside_event(self, attribute, value):
        logger.info('Outside temperature: %s', value)
        logger.info('Publish outside temperature: {%s/%s}', TOPIC_TEMP_OUTSIDE, value)
        await self._mqtt.publish(TOPIC_TEMP_OUTSIDE, value)

    async def airflow_exhaust(self, attribute, value):
        logger.info('Airflow exhaust: %s', value)
        logger.info('Publish airflow exhaust: {%s/%s}', TOPIC_AIRFLOW_EXHAUST, value)
        await self._mqtt.publish(TOPIC_AIRFLOW_EXHAUST, value)

    async def airflow_supply(self, attribute, value):
        logger.info('Airflow supply: %s', value)
        logger.info('Publish airflow supply: {%s/%s}', TOPIC_AIRFLOW_SUPPLY, value)
        await self._mqtt.publish(TOPIC_AIRFLOW_SUPPLY, value)

    async def fan_speed_mode(self, attribute, value):
        logger.info('Fan speed: %s', value)
        speed = self.SPEED_VALUES[value]
        if speed == "off":
            logger.info('Publish fan state: {%s/%s}', TOPIC_FAN_STATE, "off")
            self._cache["state"] = "off"
            await self._mqtt.publish(TOPIC_FAN_STATE, payload="off", retain=True)
        elif speed != "auto":    
            if not self._cache.get("speed") or self._cache.get("speed") == "off": # the previous state was OFF
                logger.info('Publish fan state: {%s/%s}', TOPIC_FAN_STATE, "on")
                self._cache["state"] = "on"
                await self._mqtt.publish(TOPIC_FAN_STATE, payload="on", retain=True)
            logger.info('Publish fan state: {%s/%s}', TOPIC_FAN_SPEED, speed)
            await self._mqtt.publish(TOPIC_FAN_SPEED, speed)
        self._cache["speed"] = speed

    async def cooked_event(self, attribute, value):
        logger.info('Attribute %s: %s', attribute, value)

    async def handle_set_speed(self, value, ca):
        if value not in self.SPEED_VALUES:
            logger.info(f"Invalid fan speed value reveived: {value}")
            return
        pos = self.SPEED_VALUES.index(value)
        if pos >= 1 and pos <= 4:
            await ca.set_speed(pos)

    async def handle_set_fan_state(self, value, ca):
        if value not in self.STATE_VALUES:
            logger.info(f"Invalid fan state value reveived: {value}")
            return
        if value == "off":
            await ca.set_speed(1)
        else:
            speed = self._cache.get("speed")
            if not speed or speed == "off":
                await ca.set_speed(2)

async def send_ha_config(mqtt):
    device = {
        "identifiers": [
        "ComfoAir350"
        ],
        "name": "ComfoAir 350",
        "manufacturer": "Zehnder"
    }
    fan = {
            "name": "ComfoAir Fan",
            "device": device,
            "unique_id": "ComfoAir350_fan",
            "state_topic": TOPIC_SET_FAN_STATE,
            "command_topic": TOPIC_SET_FAN_STATE,
            "speed_state_topic": TOPIC_FAN_SPEED,
            "speed_command_topic": TOPIC_SET_FAN_SPEED,
            "availability_topic": TOPIC_AVAILABILITY,
            "payload_available": "Online",
            "payload_not_available": "Offline",
            "qos": 0,
            "payload_on": "on",
            "payload_off": "off",
            "payload_low_speed": "low",
            "payload_medium_speed": "medium",
            "payload_high_speed": "high",
            "speeds": ["off", "low", "medium", "high"],
            "platform": "mqtt"
            }

    temp_outside_sensor = {
        "platform": "mqtt",
        "name": "Outside Temperature",
        "icon": "mdi:thermometer",
        "device": device,
        "unique_id": "ComfoAir350_outside_temp",
        "state_topic": TOPIC_TEMP_OUTSIDE,
        "availability_topic": TOPIC_AVAILABILITY,
        "payload_available": "Online",
        "payload_not_available": "Offline",
        "unit_of_measurement": "C"
    }
    airflow_supply = {
        "platform": "mqtt",
        "name": "Supply Airflow",
        "icon": "mdi:fan",
        "device": device,
        "unique_id": "ComfoAir350_airflow_supply",
        "state_topic": TOPIC_AIRFLOW_SUPPLY,
        "availability_topic": TOPIC_AVAILABILITY,
        "payload_available": "Online",
        "payload_not_available": "Offline",
        "unit_of_measurement": "%"
    }
    airflow_exhaust = {
        "platform": "mqtt",
        "name": "Exhaust Airflow",
        "icon": "mdi:fan",
        "device": device,
        "unique_id": "ComfoAir350_airflow_exhaust",
        "state_topic": TOPIC_AIRFLOW_EXHAUST,
        "availability_topic": TOPIC_AVAILABILITY,
        "payload_available": "Online",
        "payload_not_available": "Offline",
        "unit_of_measurement": "%"
    }

    await mqtt.publish("homeassistant/fan/ComfoAir/config", json.dumps(fan))
    await mqtt.publish("homeassistant/sensor/ComfoAir_temp/config", json.dumps(temp_outside_sensor))
    await mqtt.publish("homeassistant/sensor/ComfoAir_airflow_supply/config", json.dumps(airflow_supply))
    await mqtt.publish("homeassistant/sensor/ComfoAir_airflow_exhaust/config", json.dumps(airflow_exhaust))


def add_ca_listeners(ca, h):
    ca.add_listener(h.event)
    ca.add_cooked_listener(ca.AIRFLOW_EXHAUST, h.airflow_exhaust)
    ca.add_cooked_listener(ca.AIRFLOW_SUPPLY, h.airflow_supply)
    ca.add_cooked_listener(ca.FAN_SPEED_MODE, h.fan_speed_mode)
    ca.add_cooked_listener(ca.TEMP_OUTSIDE, h.temp_outside_event)
    ca.add_cooked_listener(ca.TEMP_COMFORT, h.cooked_event)
    ca.add_cooked_listener(ca.TEMP_RETURN, h.cooked_event)
    ca.add_cooked_listener(ca.TEMP_EXHAUST, h.cooked_event)
    ca.add_cooked_listener(ca.TEMP_SUPPLY, h.cooked_event)

def remove_ca_listeners(ca, h):
    ca.remove_listener(h.event)
    ca.remove_cooked_listener(ca.AIRFLOW_EXHAUST, h.airflow_exhaust)
    ca.remove_cooked_listener(ca.AIRFLOW_SUPPLY, h.airflow_supply)
    ca.remove_cooked_listener(ca.FAN_SPEED_MODE, h.fan_speed_mode)
    ca.remove_cooked_listener(ca.TEMP_OUTSIDE, h.temp_outside_event)
    ca.remove_cooked_listener(ca.TEMP_COMFORT, h.cooked_event)
    ca.remove_cooked_listener(ca.TEMP_RETURN, h.cooked_event)
    ca.remove_cooked_listener(ca.TEMP_EXHAUST, h.cooked_event)
    ca.remove_cooked_listener(ca.TEMP_SUPPLY, h.cooked_event)

async def handle_mqtt_message(mqtt, ca, handlers :dict):
    async with mqtt.unfiltered_messages() as messages:
        for topic in handlers.keys():
            await mqtt.subscribe(topic)
        async for message in messages:
            value = message.payload.decode()
            topic = message.topic
            await handlers[topic](value, ca)

async def mqtt_connection(ca, url, username, password):
    logger.info('Connecting to MQTT...')
    reconnect_interval = 3 #seconds
    mqtt = None
    h = None
    while True:
        try:
            if username and password:
                mqtt = Client(url, username=username, password=password)
            else:
                mqtt = Client(url)

            h = ComfoAirHandler(mqtt)
            # FIXME ugly hack!!!
            # set the LWT message in case of disconnect
            mqtt._client.will_set(TOPIC_AVAILABILITY, payload="Offline", qos=0, retain=True)
            await mqtt.connect()
            await send_ha_config(mqtt)
            await mqtt.publish(TOPIC_AVAILABILITY, "Online", qos=0, retain=True)
            add_ca_listeners(ca, h)
            logger.info('Connected to MQTT')
            await handle_mqtt_message(mqtt, ca, {TOPIC_SET_FAN_SPEED: h.handle_set_speed, TOPIC_SET_FAN_STATE: h.handle_set_fan_state})

        except MqttError as error:
            logger.error(f'Error "{error}". Reconnecting in {reconnect_interval} seconds.')
            remove_ca_listeners(ca, h)
            await asyncio.sleep(reconnect_interval)

async def ca_connection(ca):
    logger.info('Connecting to CA...')
    await ca.connect()
    logger.info('Connected to CA')

async def main(args):
    ca = ComfoAir(args.serial)
    await ca_connection(ca) # needs to have the event loop running
    await mqtt_connection(ca, args.mqtt_url, args.username, args.password) # waits for messages until disconnected

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-V", "--version", help="show program version", action="store_true")
    parser.add_argument("-u", "--username", help="MQTT username")
    parser.add_argument("-p", "--password", help="MQTT password")
    parser.add_argument("-m", "--mqtt-url", help="MQTT url", required=True)
    parser.add_argument("-s", "--serial", help="Serial device", required=True)

    args = parser.parse_args()

    asyncio.run(main(args))
    exit(0)
