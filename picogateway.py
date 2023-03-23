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

class PicoGateway:     
    def __init__(self, id, frequency, sf, bw, cr, ssid, password, server, port, ntp_server='pool.ntp.org', ntp_period=3600):
        self.id = id
        self.server = server
        self.port = port
        self.frequency = frequency
        self.ssid = ssid
        self.password = password
        self.ntp_server = ntp_server
        self.ntp_period = ntp_period
        
        self.server_ip = None
        
        self.rxnb = 0
        self.rxok = 0
        self.rxfw = 0
        self.dwnb = 0
        self.txnb = 0
        
        self.sf = sf
        self.bw = bw
        self.cr = cr
        
        self.rtc_alarm = None
        self.stat_alarm = None
        self.pull_alarm = None
        self.uplink_alarm = None
        
        self.wlan = None
        self.sock = None
        self.udp_lock = _thread.allocate_lock()
        
        self.lora = None
        
        self.rtc = machine.RTC()
        
    def start(self, lora_obj):
        self._log('Starting LoRa pico forwarder with id: {}', self.id)
        self.wlan = network.WLAN(network.STA_IF)
        self._connect_to_wifi()
        
        self._log('Syncing time with {} ...', self.ntp_server)
        #set rtc
        self._set_time(None)
        #set periodic alarm to resync the rtc (dunno if needed)
        self.rtc_alarm = Timer(mode=Timer.PERIODIC, period = self.ntp_period*1000, callback=self._set_time)
        
        #set the socket towards the server
        self.server_ip = usocket.getaddrinfo(self.server, self.port)[0][-1]
        self._log('Opening UDP socket to {} ({}) port {}...', self.server, self.server_ip[0], self.server_ip[1])
        self.udp_sock = usocket.socket(usocket.AF_INET, usocket.SOCK_DGRAM) #SOCK_DGRAM automatically sets to udp 
        self.udp_sock.setsockopt(usocket.SOL_SOCKET, usocket.SO_REUSEADDR, 1)
        self.udp_sock.setblocking(False)
        self.lora = lora_obj
        self._push_data(self._make_stat_packet())
        self.stat_alarm = Timer(mode=Timer.PERIODIC, period=30000, callback = lambda t: self._push_data(self._make_stat_packet()))
        #this one could be avoided i think
        self.pull_alarm = Timer(mode=Timer.PERIODIC, period=60500, callback = lambda x : self._pull_data())
        self.udp_stop = False
        self.stop_all = False
        
    def stop(self):
        self._log('Stopping...')
        self.udp_stop = True
        if self.rtc_alarm:
            self.rtc_alarm.deinit()
        if self.stat_alarm:   
            self.stat_alarm.deinit()
        if self.pull_alarm:
            self.pull_alarm.deinit()
        self.udp_sock.close()
        while self.udp_stop and (not self.stop_all):
            time.sleep_ms(50)
        self.stop_all = True
        self.wlan.disconnect()
        self.wlan.deinit()
        self._log('Forwarder stopped')
        
    def _connect_to_wifi(self):
        self.wlan.active(True)
        self.wlan.connect(self.ssid, self.password)
        self._log('...connecting to : {}', self.ssid)
        while not self.wlan.isconnected():
            time.sleep_ms(50)
        self._log('Connected')
        
    def _set_time(self, t):
        NTP_QUERY = bytearray(48)
        NTP_QUERY[0] = 0x1B
        addr = usocket.getaddrinfo(self.ntp_server, 123)[0][-1]
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
                self._log('Failed to sync, querying again')
                synced = False
                time.sleep_ms(1000)
            finally:
                s.close()
                time.sleep_ms(5000)
                
        val = struct.unpack("!I", msg[40:44])[0]
        t = val - config.NTP_DELTA    
        tm = time.gmtime(t)
        #(y m d weekday h m s subseconds)
        self.rtc.datetime((tm[0], tm[1], tm[2], 0, tm[3], tm[4], tm[5], 0)) #weekday doesn't seem to work, don't really matters though
        self._log('Current time is: {}', self.rtc.datetime())
    
    #pushes generic data
    def _push_data(self, data):
        self._log('push data')
        token = uos.urandom(2)
        packet = bytes([PROTOCOL_VERSION]) + token + bytes([PUSH_DATA]) + ubinascii.unhexlify(self.id) + data
        with self.udp_lock:
            try:
                self.udp_sock.sendto(packet, self.server_ip)
            except Exception as ex:
                self._log('Filed to push uplink packet to server: {}', ex)

        
    def _pull_data(self):
        self._log('pull data')
        token = uos.urandom(2)
        packet = bytes([PROTOCOL_VERSION]) + token + bytes([PULL_DATA]) + ubinascii.unhexlify(self.id)
        with self.udp_lock:
            try:
                self.udp_sock.sendto(packet, self.server_ip)
            except Exception as ex:
                self._log('Failed to pull downlink packets from server: {}', ex)

    #function created for the timer callback since it must take the object timer as the argument    
    def _push_data_stat(self, t):
        data = self._make_stat_packet()
        self._log('Data stats')
        token = uos.urandom(2)
        packet = bytes([PROTOCOL_VERSION]) + token + bytes([PUSH_DATA]) + ubinascii.unhexlify(self.id) + data
        with self.udp_lock:
            try:
                self.udp_sock.sendto(packet, self.server_ip)
            except Exception as ex:
                self._log('Filed to push uplink packet to server: {}', ex)
        self._log('Pushed stats {}', packet)
        
    def _make_stat_packet(self):
        now = self.rtc.datetime()
        STAT_PK["stat"]["time"] = "%d-%02d-%02d %02d:%02d:%02d GMT" % (now[0], now[1], now[2], now[4], now[5], now[6])
        STAT_PK["stat"]["rxnb"] = self.rxnb
        STAT_PK["stat"]["rxok"] = self.rxok
        STAT_PK["stat"]["rxfw"] = self.rxfw
        STAT_PK["stat"]["dwnb"] = self.dwnb
        STAT_PK["stat"]["txnb"] = self.txnb
        return ujson.dumps(STAT_PK)
    
    def _make_node_packet(self, rx_data, rx_time, tmst, rssi, snr):
        RX_PK["rxpk"][0]["time"] = "%d-%02d-%02dT%02d:%02d:%02d.%dZ" % (rx_time[0], rx_time[1], rx_time[2], rx_time[3], rx_time[4], rx_time[5], rx_time[6])
        RX_PK["rxpk"][0]["tmst"] = tmst
        RX_PK["rxpk"][0]["freq"] = 868.1
        RX_PK["rxpk"][0]["datr"] = 'SF7BW125'
        RX_PK["rxpk"][0]["rssi"] = rssi
        RX_PK["rxpk"][0]["lsnr"] = snr
        RX_PK["rxpk"][0]["data"] = ubinascii.b2a_base64(rx_data)[:-1]
        RX_PK["rxpk"][0]["size"] = len(rx_data)
        return ujson.dumps(RX_PK)
    
    def udp_thread(self):
        #reads from server
        try:
            while not self.udp_stop:
                try:
                    data = self.udp_sock.recv(1024)
                    _token = data[1:3]
                    _type = data[3]
                    if _type == PUSH_ACK:
                        self._log('Push ack')
                    elif _type == PULL_ACK:
                        self._log('Pull ack')
                    elif _type == PULL_RESP:
                        self._log('Pull resp')
                        self.dwnb += 1
                        ack_error = TX_ERR_NONE
                        tx_pk = ujson.loads(data[4:])
                        self._log('--tx_pk-- {}', tx_pk)
                        if "tmst" in tx_pk['txpk']:
                            tmst = tx_pk["txpk"]["tmst"]
                            t_us = tmst - time.ticks_cpu() - 15000
                            if t_us < 0:
                                t_us += 0xFFFFFFFF
                            if t_us < 20000000:
                                self.uplink_alarm = Timer(mode=Timer.ONE_SHOT, period= t_us/1000, callback = lambda x: self._send_down_link(ubinascii.a2b_base64(tx_pk["txpk"]["data"]), tx_pk["txpk"]["tmst"] - 50, tx_pk["txpk"]["datr"], int(tx_pk["txpk"]["freq"] * 1000) * 1000))
                            else:
                                ack_error = TX_ERR_TOO_LATE
                                self.log('Downlink timestamp error!, t_us: {}', t_us)
                        else:
                            self._send_down_link_c(ubinascii.a2b_base64(tx_pk["txpk"]["data"]))
                            self._ack_pull_rsp(_token, ack_error)
                            self._log('Pull resp')
                except OSError as ex:
                    if ex.args[0] == errno.ETIMEDOUT:
                        pass
                    if ex.args[0] != errno.EAGAIN:
                        print('UDP recv OSError Exception: ', ex)
                except Exception as ex:
                    print('UDP recv Exception: ', ex)
                time.sleep_ms(UDP_THREAD_CYCLE_MS)
        except KeyboardInterrupt as ki:
            self._log('Thread keyboard interrupt {} ', ki) 
        finally:
            self.stop_all = True
            self._log('UDP thread stopped, stop all {}', self.stop_all) 
            self.stop()


    
    def _send_down_link(self, data, tmst, datr, freq):
        self.lora.send(data)
        self._log('Sent downlink packet scheduled on {:.3f}: {}', tmst/1000000, data)
        
    def _send_down_link_c(self, data):
        self.lora.send(data)
        self._log('Sent class c downlink packet: {}', data)
      
    def _ack_pull_rsp(self, token, error):
        TX_ACK_PK["txpk_ack"]["error"] = error
        resp = ujson.dumps(TX_ACK_PK)
        packet = bytes([PROTOCOL_VERSION]) + token + bytes([TX_ACK]) + ubinascii.unhexlify(self.id) + resp
        with self.udp_lock:
            try:
                self.udp_sock.sendto(packet, self.server_ip)
            except Exception as ex:
                self._log('PULL RSP ACK exception: {}', ex)
    
    def get_stop_all(self):
        return self.stop_all

    def _log(self, message, *args):
        """
        Outputs a log message to stdout.
        """

        print('[{:>10.3f}] {}'.format(
            time.ticks_ms() / 1000,
            str(message).format(*args)
            ))