# -*- coding: utf-8 -*-
# pylint:disable=C0114,C0115,C0116,W3101,C0412,W0718,I1101,E0611
"""
Created on Thu May 14 09:10:23 2026

@author: Godsix
"""
import re
import time
import struct


def split_packets(data: bytes | bytearray) -> list[bytes]:
    # 以 AA55 为分隔符split，过滤空块
    parts = re.split(b'(?=\xaa\x55)', data)
    return [x for x in parts if len(x) >= 2]


def byte_to_point_str(value: int) -> str:
    """
    byteToPointStr: 一个字节转成 "高4位.低4位" 格式的版本号
    例: 0x23 → "2.3"，0x10 → "1.0"
    """
    value &= 0xff        # 转无符号
    major = value >> 4   # 高4位
    minor = value & 0xF  # 低4位
    return f"{major}.{minor}"


def parse_data_param(content: bytes) -> dict:
    spo2, pr, pi, flags, bat_raw = struct.unpack_from("<BHBBB", content)
    return {
        "spo2": spo2,
        "pr": pr,
        "pi": pi,
        "is_probe_off": bool((flags & 0x02) >> 1),
        "is_pulse_searching": bool((flags & 0x04) >> 2),
        "battery_level": (bat_raw & 0xC0) >> 6,   # 0~3级
    }


def parse_data_wave(content: bytes) -> dict:
    wave = bytes(x & 0x7f for x in content[:5])
    return {"wave": wave,
            "wave_int_data": list(wave),
            "wave_rev_data": [0x7f - x for x in wave]}


def parse_ir_red_freq(content: bytes) -> dict:
    if len(content) >= 8:
        # 小端 2个uint32
        ir_frq, red_frq = struct.unpack_from("<II", content)
    else:
        # 小端 2个uint16，注意顺序和>=8时相反
        red_frq, ir_frq = struct.unpack_from("<HH", content)
    return {"ir_frq": ir_frq, "red_frq": red_frq}


def parse_string(content: bytes) -> dict:
    return content.decode("latin-1")


def parse_string_utf8(content: bytes) -> str:
    return content.decode("utf-8").strip()


def parse_device_info(content: bytes) -> dict:
    """
    parse device info content
    content[0:1] = softwareV
    content[2] = hardwareV
    content[3:] = deviceName (bytes → string)
    """
    software_v = f'{byte_to_point_str(content[0])}.{byte_to_point_str(content[1])}'
    hardware_v = byte_to_point_str(content[2])
    device_name = parse_string(content[3:])

    return {
        "software_version": software_v,
        "hardware_version": hardware_v,
        "device_name": device_name,
    }


def parse_device_info_0f(content: bytes) -> dict:
    """
    parse device info content
    content[0] = softwareV
    content[1] = hardwareV
    content[2:] = deviceName (bytes → string)
    """
    software_v = byte_to_point_str(content[0])
    hardware_v = byte_to_point_str(content[1])
    device_name = parse_string(content[2:])

    return {
        "software_version": software_v,
        "hardware_version": hardware_v,
        "device_name": device_name,
    }


def parse_int_little(content: bytes) -> int:
    return int.from_bytes(content[:4], "little")


def parse_bool(content: bytes) -> True:
    return parse_int_little(content) == 1


def pipeline_work_mode(data):
    mode = data['mode']
    if mode == 1:
        print("Spot mode")
        step = data['step']
        if step == 2:
            if data['para1'] == 0:
                print("Stop Measuring")
            else:
                print("Start Measuring")
    elif mode == 2:
        print("Continuous Mode")
    elif mode == 3:
        print("Stop mea")
    else:
        return


def pipeline_battery(data):
    level = data['battery_level']
    if level == 0:
        power = 25
    elif level == 1:
        power = 50
    elif level == 2:
        power = 75
    elif level == 3:
        power = 100
    else:
        return
    return power


def pipeline_data_param(data):
    if data['is_probe_off']:
        result = {'spo2': '--', 'pr': '--', 'pi': '--'}
    else:
        result = {'spo2': data['spo2'], 'pr': data['pr'], 'pi': data['pi']}
    pipeline_battery(data)
    return result


PARSER_INFO = {
    240: {
        1: {'name': 'TYPE_DEVICE_INFO', 'length': 3, 'func': parse_device_info},
        2: {'name': 'TYPE_DEVICE_SN', 'func': parse_string_utf8},
        3: {'name': 'EventPC60FwBattery', 'struct': ("<B", ("battery_level", ))},
        33: {'name': 'MSG_GET_DEVICE_MAC', 'length': 4, 'func': None},
        34: {'name': 'MSG_SET_DEVICE_MAC', 'func': parse_bool},
        35: {'name': 'MSG_SET_DEVICE_SN', 'func': parse_bool},
        65: {'name': 'MSG_GET_CODE', 'func': parse_string},
        66: {'name': 'MSG_SET_CODE', 'func': parse_bool},
    },
    241: {
        33: {'name': 'MSG_GET_DEVICE_CMEI', 'length': 4, 'func': parse_string_utf8},
        34: {'name': 'MSG_SET_DEVICE_CMEI', 'func': parse_bool},
    },
    15: {
        0: {'name': 'MSG_HEARTBEAT', 'func': None},
        1: {'name': 'EventPC60FwRtDataParam', 'length': 6, 'func': parse_data_param},
        2: {'name': 'EventPC60FwRtDataWave', 'length': 5, 'func': parse_data_wave},
        3: {'name': 'MSG_GET_DEVICE_INFO_0F', 'length': 2, 'func': parse_device_info_0f},
        4: {'name': 'MSG_ENABLE_PARAM', 'func': None},
        5: {'name': 'MSG_ENABLE_WAVE', 'func': None},
        32: {'name': 'MSG_IR_RED_FREQ', 'length': 4, 'func': parse_ir_red_freq},
        33: {'name': 'WORK_STATUS_DATA', 'length': 4,
             'struct': ("<BBBB", ("mode", "step", "para1", "para2"))},
    }
}


def parse_protocol(data: bytes | bytearray):
    if len(data) < 5:
        print('response size error', data.hex())
        return None
    result = None
    mv = memoryview(data)
    timestamp = time.time()
    token, length, type_ = struct.unpack_from("<xxBBB", mv)
    content = mv[5:-1]
    # print({'token': token, 'length': length, 'type': type_, 'content': content})
    if (token_parser := PARSER_INFO.get(token)) is not None:
        if (type_parser := token_parser.get(type_)) is not None:
            length = type_parser.get('length')
            if not content or (length and len(content) < length):
                print('response size error', len(content))
                return
            if st := type_parser.get('struct'):
                ret = dict(zip(st[1], struct.unpack_from(st[0], content)))
            elif func := type_parser.get('func'):
                ret = func(content)
            else:
                print(type_parser['name'])
                return
            if ret is not None:
                if isinstance(ret, dict):
                    result = {'name': type_parser['name'],
                              'timestamp': timestamp,
                              **ret}
                else:
                    result = {'name': type_parser['name'],
                              'timestamp': timestamp,
                              'value': ret}
        else:
            print('Unknown Type', (token, type_))
    else:
        print('Unknown Token', (token, type_))
    return result
