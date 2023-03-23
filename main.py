import _thread
import sys
from sx1262 import SX1262
import network
import time
import machine
from machine import Timer
import socket
import usocket
import struct
import config
import uos
import ubinascii
import ujson
import errno
import _thread
from picogateway import PicoGateway

"""
    CONSTANTS DEFINITION
"""
PROTOCOL_VERSION = const(2)

PUSH_DATA = const(0)
PUSH_ACK = const(1)
PULL_DATA = const(2)
PULL_ACK = const(4)
PULL_RESP = const(3)
TX_ACK = const(5)

TX_ERR_NONE = 'NONE'
TX_ERR_TOO_LATE = 'TOO_LATE'
TX_ERR_TOO_EARLY = 'TOO_EARLY'
TX_ERR_COLLISION_PACKET = 'COLLISION_PACKET'
TX_ERR_COLLISION_BEACON = 'COLLISION_BEACON'
TX_ERR_TX_FREQ = 'TX_FREQ'
TX_ERR_TX_POWER = 'TX_POWER'
TX_ERR_GPS_UNLOCKED = 'GPS_UNLOCKED'

UDP_THREAD_CYCLE_MS = const(20)

STAT_PK = {
    'stat': {
        'time': '',
        'lati': 0,
        'long': 0,
        'alti': 0,
        'rxnb': 0,
        'rxok': 0,
        'rxfw': 0,
        'ackr': 100.0,
        'dwnb': 0,
        'txnb': 0
    }
}

RX_PK = {
    'rxpk': [{
        'time': '',
        'tmst': 0,
        'chan': 0,
        'rfch': 0,
        'freq': 0,
        'stat': 1,
        'modu': 'LORA',
        'datr': '',
        'codr': '4/5',
        'rssi': 0,
        'lsnr': 0,
        'size': 0,
        'data': ''
    }]
}

TX_ACK_PK = {
    'txpk_ack': {
        'error': ''
    }
}

"""
    VARIABLE DEFINITION
"""
id = config.GATEWAY_ID
server = config.SERVER
port = config.PORT
frequency = 868.1
ssid = config.WIFI_SSID
password = config.WIFI_PASS
ntp_server = config.NTP
ntp_period = 3600

server_ip = None

rxnb = 0
rxok = 0
rxfw = 0
dwnb = 0
txnb = 0

sf = 7
bw = 125
cr = 5

rtc_alarm = None
stat_alarm = None
pull_alarm = None
uplink_alarm = None

wlan = None
sock = None
udp_lock = _thread.allocate_lock()

lora = None
udp_stop = False
stop_all = False

rtc = machine.RTC()

def log(message, *args):
    """
    Outputs a log message to stdout.
    """
    print('[{:>10.3f}] {}'.format(
        time.ticks_ms() / 1000,
        str(message).format(*args)
        ))

def connect_to_wifi(ssid, password):
    global wlan
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(ssid, password)
    log('...connecting to : {}', ssid)
    while not wlan.isconnected():
        time.sleep_ms(50)
    log('Connected')

def set_time(ntp_server):
    global rtc
    NTP_QUERY = bytearray(48)
    NTP_QUERY[0] = 0x1B
    addr = usocket.getaddrinfo(ntp_server, 123)[0][-1]
    synced = False
    while not synced:
        s = usocket.socket(usocket.AF_INET, usocket.SOCK_DGRAM)
        s.setsockopt(usocket.SOL_SOCKET, usocket.SO_REUSEADDR, 1)
        try:
            s.settimeout(10)
            res = s.sendto(NTP_QUERY, addr)
            msg = s.recv(48)
            synced = True
        except OSError as e:
            log('Failed to sync, querying again')
            synced = False
            time.sleep_ms(1000)
        finally:
            s.close()
            time.sleep_ms(5000)
            
    val = struct.unpack("!I", msg[40:44])[0]
    t = val - config.NTP_DELTA    
    tm = time.gmtime(t)
    #(y m d weekday h m s subseconds)
    rtc.datetime((tm[0], tm[1], tm[2], 0, tm[3], tm[4], tm[5], 0)) #weekday doesn't seem to work, don't really matters though
    log('Current time is: {}', rtc.datetime())

def lora_cb(events, obj):       
    if events & SX1262.RX_DONE:
        obj.rxnb += 1
        obj.rxok += 1
        
        msg, err = lora.recv()
        error = SX1262.STATUS[err]

        obj._log('--rx data-- {}, rxnb: {} rxok: {}', msg, obj.rxnb, obj.rxok)
        
        packet = obj._make_node_packet(msg, obj.rtc.datetime(), 0, 0, lora.getSNR())
        obj._push_data(packet)
        obj._log('sent packet: {}', packet)
        obj.rxfw += 1
    
    if events & SX1262.TX_DONE:
        obj.txnb += 1
        obj._log('TX done')

def make_stat_packet(rtc):
    global rxnb, rxok, rxfw, dwnb, txnb
    now = rtc.datetime()
    STAT_PK["stat"]["time"] = "%d-%02d-%02d %02d:%02d:%02d GMT" % (now[0], now[1], now[2], now[4], now[5], now[6])
    STAT_PK["stat"]["rxnb"] = rxnb
    STAT_PK["stat"]["rxok"] = rxok
    STAT_PK["stat"]["rxfw"] = rxfw
    STAT_PK["stat"]["dwnb"] = dwnb
    STAT_PK["stat"]["txnb"] = txnb
    return ujson.dumps(STAT_PK)

def push_data(data, id, udp_sock):
    global udp_lock
    log('push data')
    token = uos.urandom(2)
    packet = bytes([PROTOCOL_VERSION]) + token + bytes([PUSH_DATA]) + ubinascii.unhexlify(id) + data
    with udp_lock:
        try:
            udp_sock.sendto(packet, server_ip)
        except Exception as ex:
            log('Filed to push uplink packet to server: {}', ex)

def pull_data(id, server_ip, udp_sock):
    global udp_lock
    log('pull data')
    token = uos.urandom(2)
    packet = bytes([PROTOCOL_VERSION]) + token + bytes([PULL_DATA]) + ubinascii.unhexlify(id)
    with udp_lock:
        try:
            udp_sock.sendto(packet, server_ip)
        except Exception as ex:
            log('Failed to pull downlink packets from server: {}', ex)

def stop(rtc_alarm, stat_alarm, pull_alarm, udp_sock, wlan):
        log('Stopping...')
        global udp_stop, stop_all
        udp_stop = True
        if rtc_alarm:
            rtc_alarm.deinit()
        if stat_alarm:   
            stat_alarm.deinit()
        if pull_alarm:
            pull_alarm.deinit()
        udp_sock.close()
        #while udp_stop and (not stop_all):
            #time.sleep_ms(50)
        stop_all = True
        wlan.disconnect()
        wlan.deinit()
        log('Forwarder stopped')

if True:
    log('Initializing gateway')
    log('Starting LoRa pico forwarder with id: {}', id)
    connect_to_wifi(ssid, password)
    log('Syncing time with {} ...', ntp_server)
    set_time(ntp_server)
    rtc_alarm = Timer(mode=Timer.PERIODIC, period = ntp_period*1000, callback = lambda t: set_time(ntp_server))

    server_ip = usocket.getaddrinfo(server, port)[0][-1]
    log('Opening UDP socket to {} ({}) port {}...', server, server_ip[0], server_ip[1])
    udp_sock = usocket.socket(usocket.AF_INET, usocket.SOCK_DGRAM) #SOCK_DGRAM automatically sets to udp 
    udp_sock.setsockopt(usocket.SOL_SOCKET, usocket.SO_REUSEADDR, 1)
    udp_sock.setblocking(False)
    
    lora = SX1262(spi_bus=1, clk=10, mosi=11, miso=12, cs=3, irq=20, rst=15, gpio=2)

    push_data(make_stat_packet(rtc), id, udp_sock)
    stat_alarm = Timer(mode=Timer.PERIODIC, period=30000, callback = lambda t: push_data(make_stat_packet(rtc), id, udp_sock))
    #this one could be avoided i think
    pull_alarm = Timer(mode=Timer.PERIODIC, period=60500, callback = lambda x : pull_data(id, server_ip, udp_sock))
    


    lora.begin(freq=868.1, bw=125.0, sf=7, cr=5, syncWord=0x34,
                    power=-5, currentLimit=60.0, preambleLength=8,
                    implicit=False, implicitLen=0xFF,
                    crcOn=True, txIq=False, rxIq=False,
                    tcxoVoltage=1.7, useRegulatorLDO=False, blocking=True)
    #lora.setBlockingCallback(False, lora_cb, picogw)
    try:
        while not udp_stop:
            ()#here should go the udp handler
    except KeyboardInterrupt as ki:
        stop(rtc_alarm, stat_alarm, pull_alarm, udp_sock, wlan)
    #start(picogw, lora)