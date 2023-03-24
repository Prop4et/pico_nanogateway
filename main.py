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

udp_sock = usocket.socket(usocket.AF_INET, usocket.SOCK_DGRAM) #SOCK_DGRAM automatically sets to udp 
udp_sock.setsockopt(usocket.SOL_SOCKET, usocket.SO_REUSEADDR, 1)
udp_sock.setblocking(False)

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
    global txnb
    if events & SX1262.TX_DONE:
        txnb += 1
        log('TX done')

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

def push_data(data, id, server_ip):
    global udp_lock, udp_sock
    log('push data')
    token = uos.urandom(2)
    packet = bytes([PROTOCOL_VERSION]) + token + bytes([PUSH_DATA]) + ubinascii.unhexlify(id) + data
    with udp_lock:
        try:
            udp_sock.sendto(packet, server_ip)
        except Exception as ex:
            log('Filed to push uplink packet to server: {}', ex)

def pull_data(id, server_ip):
    global udp_lock, udp_sock
    log('pull data')
    token = uos.urandom(2)
    packet = bytes([PROTOCOL_VERSION]) + token + bytes([PULL_DATA]) + ubinascii.unhexlify(id)
    with udp_lock:
        try:
            udp_sock.sendto(packet, server_ip)
        except Exception as ex:
            log('Failed to pull downlink packets from server: {}', ex)

def stop(rtc_alarm, stat_alarm, pull_alarm, wlan):
        log('Stopping...')
        global udp_stop, stop_all, udp_sock
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

def send_down_link(data, tmst, datr, freq):
    lora.send(data)
    log('Sent downlink packet scheduled on {:.3f}: {}', tmst/1000000, data)

def send_down_link_c(data):
    lora.send(data)
    log('Sent class c downlink packet: {}', data)

def ack_pull_rsp(token, error, id, server_ip):
    global udp_lock, udp_sock
    TX_ACK_PK["txpk_ack"]["error"] = error
    resp = ujson.dumps(TX_ACK_PK)
    packet = bytes([PROTOCOL_VERSION]) + token + bytes([TX_ACK]) + ubinascii.unhexlify(id) + resp
    with udp_lock:
        try:
            udp_sock.sendto(packet, server_ip)
        except Exception as ex:
            log('PULL RSP ACK exception: {}', ex)

def make_node_packet(rx_data, rx_time, tmst, rssi, snr):
    RX_PK["rxpk"][0]["time"] = "%d-%02d-%02dT%02d:%02d:%02d.%dZ" % (rx_time[0], rx_time[1], rx_time[2], rx_time[3], rx_time[4], rx_time[5], rx_time[6])
    RX_PK["rxpk"][0]["tmst"] = tmst
    RX_PK["rxpk"][0]["freq"] = 868.1
    RX_PK["rxpk"][0]["datr"] = 'SF7BW125'
    RX_PK["rxpk"][0]["rssi"] = rssi
    RX_PK["rxpk"][0]["lsnr"] = snr
    RX_PK["rxpk"][0]["data"] = ubinascii.b2a_base64(rx_data)[:-1]
    RX_PK["rxpk"][0]["size"] = len(rx_data)
    return ujson.dumps(RX_PK)

def lora_thread():
    print("Starting lora thread")
    global rxnb, rxok, rtc, rxfw, stop_all, id, server_ip
    while not stop_all:
        msg, err = lora.recv()
        if len(msg) > 0:
            rxnb += 1
            rxok += 1
            error = SX1262.STATUS[err]
            log('--rx data-- {}, rxnb: {} rxok: {}', msg, rxnb, rxok)
            
            packet = make_node_packet(msg, rtc.datetime(), 0, 0, lora.getSNR())
            push_data(packet, id, server_ip)
            log('sent packet: {}', packet)
            rxfw += 1
            error = SX1262.STATUS[err]
            print(msg)
            print(error)
    log('lora thread stopped')

if True:
    log('Initializing gateway')
    log('Starting LoRa pico forwarder with id: {}', id)
    connect_to_wifi(ssid, password)
    log('Syncing time with {} ...', ntp_server)
    set_time(ntp_server)
    rtc_alarm = Timer(mode=Timer.PERIODIC, period = ntp_period*1000, callback = lambda t: set_time(ntp_server))

    server_ip = usocket.getaddrinfo(server, port)[0][-1]
    log('Opening UDP socket to {} ({}) port {}...', server, server_ip[0], server_ip[1])
    
    
    lora = SX1262(spi_bus=1, clk=10, mosi=11, miso=12, cs=3, irq=20, rst=15, gpio=2)

    push_data(make_stat_packet(rtc), id, server_ip)
    stat_alarm = Timer(mode=Timer.PERIODIC, period=30000, callback = lambda t: push_data(make_stat_packet(rtc), id, server_ip))
    #this one could be avoided i think
    pull_alarm = Timer(mode=Timer.PERIODIC, period=60500, callback = lambda x : pull_data(id, server_ip))
    


    lora.begin(freq=868.1, bw=125.0, sf=7, cr=5, syncWord=0x34,
                    power=-5, currentLimit=60.0, preambleLength=8,
                    implicit=False, implicitLen=0xFF,
                    crcOn=True, txIq=False, rxIq=False,
                    tcxoVoltage=1.7, useRegulatorLDO=False, blocking=True)

    _thread.start_new_thread(lora_thread, ())
    lora.setBlockingCallback(True, lora_cb)
    try:
        while not udp_stop:
            try:
                data = udp_sock.recv(1024)
                _token = data[1:3]
                _type = data[3]
                if _type == PUSH_ACK:
                    log('Push ack')
                elif _type == PULL_ACK:
                    log('Pull ack')
                elif _type == PULL_RESP:
                    log('Pull resp')
                    dwnb += 1
                    ack_error = TX_ERR_NONE
                    tx_pk = ujson.loads(data[4:])
                    log('--tx_pk-- {}', tx_pk)
                    if "tmst" in tx_pk['txpk']:
                        tmst = tx_pk["txpk"]["tmst"]
                        t_us = tmst - time.ticks_cpu() - 15000
                        if t_us < 0:
                            t_us += 0xFFFFFFFF
                        if t_us < 20000000:
                            uplink_alarm = Timer(mode=Timer.ONE_SHOT, period= t_us/1000, callback = lambda x: send_down_link(ubinascii.a2b_base64(tx_pk["txpk"]["data"]), tx_pk["txpk"]["tmst"] - 50, tx_pk["txpk"]["datr"], int(tx_pk["txpk"]["freq"] * 1000) * 1000))
                        else:
                            ack_error = TX_ERR_TOO_LATE
                            log('Downlink timestamp error!, t_us: {}', t_us)
                    else:
                        send_down_link_c(ubinascii.a2b_base64(tx_pk["txpk"]["data"]))
                        ack_pull_rsp(_token, ack_error, id, server_ip)
                        log('Pull resp')
            except OSError as ex:
                if ex.args[0] == errno.ETIMEDOUT:
                    pass
                if ex.args[0] != errno.EAGAIN:
                    log('UDP recv OSError Exception: {}', ex)
            except Exception as ex:
                log('UDP recv Exception: {}', ex)
            time.sleep_ms(UDP_THREAD_CYCLE_MS)
    except KeyboardInterrupt as ki:
        log('UDP caught interrupt') 
    finally:
        udp_stop = False
        stop_all = True
        log('UDP thread stopped')
        stop(rtc_alarm, stat_alarm, pull_alarm, wlan)
    
