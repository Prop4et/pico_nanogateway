from picogateway import PicoGateway
import config
from sx1262 import SX1262
import _thread

def _lora_cb(events, obj):       
    if events & SX1262.RX_DONE:
        obj.rxnb += 1
        obj.rxok += 1
        
        msg, err = lora.recv()
        error = SX1262.STATUS[err]

        obj._log('--rx data-- {}, rxnb: {} rxok: {}', msg, obj.rxnb, obj.rxok)
        
        packet = obj._make_node_packet(msg, obj.rtc.datetime(), 0, lora.getSNR())
        obj._push_data(packet)
        obj._log('sent packet: {}', packet)
        obj.rxfw += 1
    
    if events & SX1262.TX_DONE:
        obj.txnb += 1
        obj._log('TX done')


if True:
    picogw = PicoGateway(
        id = config.GATEWAY_ID,
        frequency = 868.1,
        sf = 7,
        bw = 125,
        cr = 5,
        ssid = config.WIFI_SSID,
        password = config.WIFI_PASS,
        server = config.SERVER,
        port = config.PORT,
        ntp_server = config.NTP
        )
    
    lora = SX1262(spi_bus=1, clk=10, mosi=11, miso=12, cs=3, irq=20, rst=15, gpio=2)
    lora.begin(freq=868.1, bw=125.0, sf=7, cr=5, syncWord=0x34,
                    power=-10, currentLimit=60.0, preambleLength=8,
                    implicit=False, implicitLen=0xFF,
                    crcOn=True, txIq=False, rxIq=False,
                    tcxoVoltage=1.7, useRegulatorLDO=False, blocking=True)
    lora.setBlockingCallback(False, _lora_cb, picogw)
    
    picogw.start(lora)

    #_thread.start_new_thread(picogw._udp_thread, ())
    picogw.udp_thread()
    print('Lora callback handler removed')
    lora.setBlockingCallback(False, None)
