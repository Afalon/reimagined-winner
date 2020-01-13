"""
StatsD client to be used in the application to log various metrics

Based on the code in http://www.monkinetic.com/2011/02/statsd.html (pystatsd client)

"""

# statsd.py

# Steve Ivy <steveivy@gmail.com>
# http://monkinetic.com

import logging
import socket
import random

from pystatsd import Client

from infogami import config

l = logging.getLogger("openlibrary.pystats")

def create_stats_client(cfg = config):
    "Create the client which can be used for logging statistics"
    logger = logging.getLogger("pystatsd.client")
    logger.addHandler(logging.StreamHandler())
    try:
        stats_server = cfg.get("admin", {}).get("statsd_server",None)
        if stats_server:
            host, port = stats_server.rsplit(":", 1)
            return Client(host, port)
        else:
            logger.critical("Couldn't find statsd_server section in config")
            return False
    except Exception as e:
        logger.critical("Couldn't create stats client - %s", e, exc_info = True)
        return False

def put(key, value):
    "Records this ``value`` with the given ``key``. It is stored as a millisecond count"
    global client
    if client:
        l.debug("Putting %s as %s"%(value, key))
        client.timing(key, value)

def increment(key, n=1):
    "Increments the value of ``key`` by ``n``"
    global client
    if client:
        l.debug("Incrementing %s"% key)
        for i in range(n):
            client.increment(key)


client = create_stats_client()
