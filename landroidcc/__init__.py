import json
import logging
import os
import tempfile
import uuid
import base64
from collections import namedtuple
from threading import Event

import OpenSSL
import paho.mqtt.client as mqtt
import requests

logging.basicConfig(format='%(asctime)s %(module)-8s %(funcName)-10s %(levelname)-8s %(message)s',
                    level=logging.INFO,
                    datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger(__name__)


class Landroid(object):
    _username = None
    _api_user = None
    _mqtt_endpoint = None
    _mqtt_topic_in = None
    _mqtt_topic_out = None
    _api_product_items = None
    _api_boards = None
    _api_products = None
    _mower_product = None  # holds the current mower from _api_products
    _api_certificate = None
    _status = None  # type: MowerStatus
    _mqtt_client = None
    _cachedir = None
    _cache = {}

    def __init__(self):
        self._statuscallback = None
        self._eventmessage = Event()
        self._eventconnect = Event()

    def connect(self, username, password):
        """
        Connect to the Cloud REST API. Does not connect to the robot but provides the needed configuration parameters.

        :param username: Username for the cloud login
        :param password: Password for the login
        :return:
        """
        self._username = username
        self._cachedir = os.path.join(tempfile.gettempdir(), "landroidcc", self._username)
        self._initcache()
        self._api_authentificate(username, password)

        self._api_user = self._apicall_rest("users/me")
        self._mqtt_endpoint = self._api_user["mqtt_endpoint"]

        self._api_product_items = self._apicall_rest("product-items")
        self._mqtt_topic_out = self._api_product_items[0]["mqtt_topics"]["command_out"]
        self._mqtt_topic_in = self._api_product_items[0]["mqtt_topics"]["command_in"]
        self._api_boards = self._apicall_rest("boards")
        self._api_products = self._apicall_rest("products")
        product_id = self._api_product_items[0]["product_id"]
        for product in self._api_products:
            if product["id"] == product_id:
                self._mower_product = product
                break

        self._api_certificate = self._apicall_rest("users/certificate")
        self._writecache()
        self._connectmqtt()

    def disconnect(self):
        self._mqtt_client.disconnect()

    def start_mowing(self):
        # if self._status.lastState
        self._send_command('{"cmd": 1}')
        log.info("Command sent: Start Mowing")

    def stop_mowing(self):
        self._send_command('{"cmd": 2}')
        log.info("Command sent: Stop Mowing")

    def go_home(self):
        self._send_command('{"cmd": 3}')
        log.info("Command sent: Go Home")

    def _send_command(self, cmd):
        self._mqtt_client.publish(self._mqtt_topic_in, cmd)

    def _connectmqtt(self):
        # Callback for connect
        def on_connect(client, userdata, flags, rc):
            log.debug("MQTT onnected with result code " + str(rc))
            client.subscribe(self._mqtt_topic_out)
            log.info("Successfully connected to the cloud")
            self._eventconnect.set()

        # The callback for when a PUBLISH message is received from the server.
        def on_message(client, userdata, msg):
            log.debug("MQTT Msg Received: " + msg.topic + " " + str(msg.payload))
            status = MowerStatus()
            status.updatestatus(msg.payload)
            self._status = status
            self._eventmessage.set()
            if self._statuscallback:
                self._statuscallback(status)

        def on_log(client, userdata, level, buf):
            log.debug("MQTT Library Log: {}".format(buf))

        pkcs12 = base64.decodestring(self._api_certificate["pkcs12"].encode())
        p12 = OpenSSL.crypto.load_pkcs12(pkcs12)
        pem_filename = os.path.join(self._cachedir, "auth.pem")
        with open(pem_filename, "w") as f_pem:
            f_pem.write(OpenSSL.crypto.dump_privatekey(OpenSSL.crypto.FILETYPE_PEM, p12.get_privatekey()))
            f_pem.write(OpenSSL.crypto.dump_certificate(OpenSSL.crypto.FILETYPE_PEM, p12.get_certificate()))
            ca = p12.get_ca_certificates()
            if ca is not None:
                for cert in ca:
                    f_pem.write(OpenSSL.crypto.dump_certificate(OpenSSL.crypto.FILETYPE_PEM, cert))

        self._mqtt_client = mqtt.Client(client_id="android-" + str(uuid.uuid4()), userdata=self)
        self._mqtt_client.tls_set(certfile=pem_filename, keyfile=pem_filename)
        self._mqtt_client.on_connect = on_connect
        self._mqtt_client.on_message = on_message
        self._mqtt_client.on_log = on_log
        self._mqtt_client.connect(self._mqtt_endpoint, 8883, keepalive=300)
        self._mqtt_client.loop_start()
        self._eventconnect.wait(30)
        return self._status

    def set_statuscallback(self, func):
        self._statuscallback = func

    def get_status(self, refresh=True):
        if refresh:
            self._apicall_mqtt("{}")
        return self._status

    def _apicall_mqtt(self, content, blocking=True):
        log.debug("MQTT call with: '{}'".format(content))
        self._eventmessage.clear()
        self._mqtt_client.publish(self._mqtt_topic_in, content)
        result = True
        if blocking:
            result = self._eventmessage.wait(10)
        log.debug("MQTT call finished")
        return result

    def _initcache(self):
        cachefilename = os.path.join(self._cachedir, "cache.json")
        if not os.path.isfile(cachefilename):
            return
        with open(cachefilename) as fptr:
            try:
                self._cache = json.load(fptr)
                return
            except ValueError as ve:
                log.debug("Failed to parse cache file: {}".format(ve))

    def _writecache(self):
        cachedir = os.path.join(self._cachedir, self._username)
        if not os.path.isdir(cachedir):
            os.makedirs(cachedir)
        with open(os.path.join(cachedir, "cache.json"), "w") as fptr:
            json.dump(self._cache, fptr)

    def _apicall_rest(self, url, postdata=None, set_headers=True, allow_cached=True):
        if allow_cached and not postdata and url in self._cache:
            log.debug("API Call form Cache: '{}': {}".format(url, self._cache[url]))
            return self._cache[url]

        headers = None
        if set_headers:
            headers = {"Content-Type": "application/json",
                       "Authorization": self._accessTokenType + " " + self._accessToken}
        if postdata:
            response_plain = requests.post('https://api.worxlandroid.com/api/v2/' + url, data=postdata, headers=headers)
        else:
            response_plain = requests.get('https://api.worxlandroid.com/api/v2/' + url, headers=headers)
        response_plain.raise_for_status()
        response = response_plain.json()
        log.debug("API Call '{}': {}".format(url, response))
        self._cache[url] = response
        return response

    def _api_authentificate(self, username, password):
        post_json = {
            "username": username,
            "password": password,
            "grant_type": "password",
            "client_id": 1,
            "type": "app",
            "client_secret": "nCH3A0WvMYn66vGorjSrnGZ2YtjQWDiCvjg7jNxK",
            "scope": "*"
        }
        response = self._apicall_rest('oauth/token', postdata=post_json, set_headers=False)
        self._accessToken = response["access_token"]
        self._accessTokenType = response["token_type"]
        log.info("Successfully logged in")

    def __str__(self):
        if not self._api_user:
            return "API not connected"
        return "landroid info\n" \
               "#############\n" \
               "Name:   {name}\n" \
               "Serial: {serial}\n" \
               "Type:   {code}\n".format(name=self._api_product_items[0]["name"],
                                         serial=self._api_product_items[0]["serial_number"],
                                         code=self._mower_product["code"])


class MowerStatus(object):
    _BatteryStatus = namedtuple("BatteryStatus", "percent,charges,volts,temperature,charging")
    _Orientation = namedtuple("Orientation", "heading,pitch,roll")
    _Statistics = namedtuple("Statistics", "distance,running,mowing")
    _lastStateDict = {
        0: "Idle",
        1: "Home",
        2: "Start sequence",
        3: "Leaving home",
        4: "Follow wire",
        5: "Searching home",
        6: "Searching wire",
        7: "Mowing",
        8: "Lifted",
        9: "Trapped",
        10: "Blade blocked",
        11: "Debug",
        12: "Remote control",
        30: "Going home",
        32: "Border Cut",
        33: "Searching zone",
        34: "Pause"
    }
    _lastErrorDict = {
        0: "No error",
        1: "Trapped",
        2: "Lifted",
        3: "Wire missing",
        4: "Outside wire",
        5: "Raining",
        6: "Close door to mow",
        7: "Close door to go home",
        8: "Blade motor blocked",
        9: "Wheel motor blocked",
        10: "Trapped timeout",
        11: "Upside down",
        12: "Battery low",
        13: "Reverse wire",
        14: "Charge error",
        15: "Timeout finding home"

    }

    battery = None
    orientation = None
    statistics = None

    lastError = None
    lastState = None
    updated = None
    raw = None

    def __init__(self):
        pass

    def updatestatus(self, inputraw):
        self.raw = inputraw
        api_response = json.loads(inputraw)

        self.battery = self._BatteryStatus(api_response["dat"]["bt"]["p"],
                                           api_response["dat"]["bt"]["nr"],
                                           api_response["dat"]["bt"]["v"],
                                           api_response["dat"]["bt"]["t"],
                                           api_response["dat"]["bt"]["c"] != "0")

        self.orientation = self._Orientation(api_response["dat"]["dmp"][2],
                                             api_response["dat"]["dmp"][0],
                                             api_response["dat"]["dmp"][1])

        self.statistics = self._Statistics(api_response["dat"]["st"]["d"],
                                           api_response["dat"]["st"]["wt"],
                                           api_response["dat"]["st"]["b"])

        self.lastState = self._lastStateDict[api_response["dat"]["ls"]]
        self.lastError = self._lastErrorDict[api_response["dat"]["le"]]
        self.updated = api_response["cfg"]["tm"] + " " + api_response["cfg"]["dt"]

    def __str__(self):
        return "landroid status\n" \
               "###############\n" \
               "LastUpdate: {updated}\n" \
               "State:      {state}\n" \
               "Error:      {error}\n" \
               "Battery:    {percent}%/{temp}C/{voltage}v" \
               "".format(updated=self.updated, state=self.lastState, error=self.lastError,
                         percent=self.battery.percent, temp=self.battery.temperature, voltage=self.battery.volts)