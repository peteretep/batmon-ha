"""

https://github.com/jblance/mpp-solar
https://github.com/jblance/jkbms
https://github.com/sshoecraft/jktool/blob/main/jk_info.c
https://github.com/syssi/esphome-jk-bms
https://github.com/PurpleAlien/jk-bms_grafana


fix connection abort:
- https://github.com/hbldh/bleak/issues/631 (use bluetoothctl !)
- https://github.com/hbldh/bleak/issues/666

"""
import asyncio
from typing import Dict

from bms import BmsSample
from bt import BtBms


def calc_crc(message_bytes):
    return sum(message_bytes) & 0xFF


def to_hex_str(data):
    return " ".join(map(lambda b: hex(b)[2:], data))


def _jk_command(address, value, length):
    frame = bytes([0xAA, 0x55, 0x90, 0xEB, address, length,
                   value[0], value[1], value[2], value[3]] + [0] * 9)
    frame += bytes([calc_crc(frame)])
    return frame


MIN_RESPONSE_SIZE = 300


class JKBt(BtBms):
    UUID_RX = "0000ffe1-0000-1000-8000-00805f9b34fb"
    UUID_TX = '0000ffe1-0000-1000-8000-00805f9b34fb'

    TIMEOUT = 8

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._fetch_futures: Dict[int, asyncio.Future] = {}
        self._resp_table = {}

    def _notification_handler(self, sender, data):

        if data[0:4] == bytes([0x55, 0xAA, 0xEB, 0x90]):  # and len(self._buffer)
            self.logger.debug("preamble, clear buf %s", self._buffer)
            self._buffer.clear()

        self._buffer += data

        self.logger.debug(
            "bms msg({2}) (buf {3}) {0}: {1}\n".format(sender, to_hex_str(data), len(data), len(self._buffer)))

        if len(self._buffer) >= MIN_RESPONSE_SIZE:
            crc_comp = calc_crc(self._buffer[0:MIN_RESPONSE_SIZE - 1])
            crc_expected = self._buffer[MIN_RESPONSE_SIZE - 1]
            if crc_comp != crc_expected:
                self.logger.error("crc check failed, %s != %s, %s", crc_comp, crc_expected, self._buffer)
            else:
                self._decode_msg(bytearray(self._buffer))
            self._buffer.clear()

    def _decode_msg(self, buf):
        resp_type = buf[4]
        self.logger.debug('got response %d (len%d)', resp_type, len(buf))

        self._resp_table[resp_type] = buf

        fut = self._fetch_futures.pop(resp_type, None)
        if fut:
            fut.set_result(self._buffer[:])

    async def connect(self, timeout=20):
        """
        Connecting JK with bluetooth appears to require a prior bluetooth scan and discovery, otherwise the connectiong fails with
        `[org.bluez.Error.Failed] Software caused connection abort`. Maybe the scan triggers some wake up?
        :param timeout:
        :return:
        """
        import bleak
        scanner = bleak.BleakScanner()
        self.logger.debug("starting scan")
        await scanner.start()

        attempt = 1
        while True:
            try:
                discovered = set(b.address for b in scanner.discovered_devices)
                if self.client.address not in discovered:
                    raise Exception('Device %s not discovered (%s)' % (self.client.address, discovered))

                self.logger.info("connect attempt %d", attempt)
                await super().connect(timeout=timeout)
                break
            except Exception as e:
                await self.client.disconnect()
                if attempt < 8:
                    self.logger.info('retry after error %s', e)
                    await asyncio.sleep(0.2 * (1.5 ** attempt))
                    attempt += 1
                else:
                    await scanner.stop()
                    raise

        await scanner.stop()

        await self.client.start_notify(self.UUID_RX, self._notification_handler)

        await self._q(cmd=0x97, resp=0x03)  # device info
        await self._q(cmd=0x96, resp=0x02)  # device state
        # after these 2 commands the bms will continuously send 0x02-type messages

        buf = self._resp_table[0x01]
        self.num_cells = buf[114]
        assert 0 < self.num_cells <= 48, "num_cells unexpected %s" % self.num_cells
        self.capacity = int.from_bytes(buf[130:134], byteorder='little', signed=False) * 0.001

    async def disconnect(self):
        self.logger.info("disconnect jk")
        await self.client.stop_notify(self.UUID_RX)
        self._fetch_futures.clear()
        await super().disconnect()

    async def _q(self, cmd, resp):
        assert cmd not in self._fetch_futures, "%s already waiting" % cmd
        self._fetch_futures[resp] = asyncio.Future()
        frame = _jk_command(cmd, bytes([0, 0, 0, 0]), 0)
        self.logger.info("write %s", frame)
        await self.client.write_gatt_char(self.UUID_TX, data=frame)
        res = await asyncio.wait_for(self._fetch_futures[resp], self.TIMEOUT)
        # print('cmd', cmd, 'result', res)
        return res

    async def fetch(self, wait=True) -> BmsSample:

        """

        Decode JK02

        references
        * https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp#L336
        * https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jk02.py

        :return:
        """

        if wait:
            self._fetch_futures[0x02] = asyncio.Future()
            await asyncio.wait_for(self._fetch_futures[0x02], self.TIMEOUT)

        buf = self._resp_table[0x02]
        i16 = lambda i: int.from_bytes(buf[i:(i + 2)], byteorder='little')
        u32 = lambda i: int.from_bytes(buf[i:(i + 2)], byteorder='little', signed=False)
        f32u = lambda i: int.from_bytes(buf[i:(i + 4)], byteorder='little', signed=False) * 1e-3
        f32s = lambda i: int.from_bytes(buf[i:(i + 4)], byteorder='little', signed=True) * 1e-3

        assert f32u(146) == self.capacity, "capacity mismatch %s != %s" % (f32u(146), self.capacity)

        return BmsSample(
            voltage=f32u(118),
            current=f32s(126),
            charge_full=self.capacity,
            temperatures=[i16(130) / 10, i16(132) / 10],
            mos_temperature=i16(134) / 10,
            balance_current=i16(138) / 1000,
            charge=f32u(142),
            # 146 charge_full (see above)
            num_cycles=u32(150),
        )

        # TODO  154   4   0x3D 0x04 0x00 0x00    Cycle_Capacity       1.0

    async def fetch_voltages(self):
        buf = self._resp_table[0x02]
        voltages = [int.from_bytes(buf[(6 + i * 2):(6 + i * 2 + 2)], byteorder='little') / 1000 for i in
                    range(self.num_cells)]
        return voltages


async def main():
    mac_address = 'C8:47:8C:F7:AD:B4'
    bms = JKBt(mac_address, name='jk')
    async with bms:
        while True:
            s = await bms.fetch(wait=True)
            print(s, 'I_bal=', s.balance_current, await bms.fetch_voltages())


if __name__ == '__main__':
    asyncio.run(main())
