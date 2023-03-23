from picogateway import PicoGateway
import _thread
import time

stop = False
def udp_thread():
    global stop
    counter = 0
    while not stop:
        counter += 1
        print('counter', counter)
        time.sleep_ms(10000)
    stop = False
    print('thread exited')
if True:
    _thread.start_new_thread(udp_thread, ())
    counter = 0
    try:
        while True:
            counter += 1
            print('main thread', counter)
            time.sleep_ms(5000)
    except KeyboardInterrupt as ki:
        print('keyboard interrupt', ki)
        stop = True
        while stop:
            time.sleep_ms(50)
        print('main thread exited')
    