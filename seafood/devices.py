"""本地设备适配器：模拟水温/溶氧探头与电子秤。

真实项目中这里走串口/Modbus/MQTT；演示环境用带轻微抖动的虚拟读数。
每个读数生成唯一 reading_token，服务层据此做称重去重。
"""
from __future__ import annotations

import itertools
import random
import time
from dataclasses import dataclass

_token_counter = itertools.count(1)


def make_reading_token(device_id: str) -> str:
    # 设备编号 + 单调序号 + 时间戳，保证全局唯一
    return f"{device_id}-{next(_token_counter):08d}-{int(time.time() * 1000) % 100000}"


@dataclass
class ProbeReading:
    temp_c: float
    oxygen_mg_l: float


class WaterProbeAdapter:
    """水温/溶氧探头模拟器。

    scenario 可注入异常：'normal' / 'heat'（水温异常）/ 'low_oxygen'（缺氧）。
    """

    def __init__(self, tank_code: str, seed: int = 7,
                 base_temp: float = 18.5, base_oxygen: float = 6.8):
        self.tank_code = tank_code
        self.base_temp = base_temp
        self.base_oxygen = base_oxygen
        self.rng = random.Random(seed + hash(tank_code) % 9973)
        self.scenario = "normal"
        self._tick = 0

    def set_scenario(self, scenario: str) -> None:
        self.scenario = scenario
        self._tick = 0

    def read(self) -> ProbeReading:
        self._tick += 1
        jitter_t = self.rng.uniform(-0.2, 0.2)
        jitter_o = self.rng.uniform(-0.1, 0.1)
        if self.scenario == "heat":
            temp = self.base_temp + 7.5 + jitter_t + min(self._tick * 0.2, 4)  # 持续升温
            oxygen = self.base_oxygen + jitter_o
        elif self.scenario == "low_oxygen":
            temp = self.base_temp + jitter_t
            oxygen = 3.0 + jitter_o - min(self._tick * 0.1, 1)  # 持续降氧
        else:
            temp = self.base_temp + jitter_t
            oxygen = self.base_oxygen + jitter_o
        return ProbeReading(temp_c=round(temp, 1), oxygen_mg_l=round(oxygen, 1))


class ScaleAdapter:
    """电子秤模拟器：在基准重量上加传感器噪声；也支持指定读数（重放去重测试）。"""

    def __init__(self, device_id: str = "SCALE-01", precision: float = 0.005):
        self.device_id = device_id
        self.precision = precision
        self.rng = random.Random(20260929)

    def weigh(self, expected_kg: float) -> tuple[float, str, str]:
        """返回 (重量, 设备编号, reading_token)。"""
        value = round(expected_kg + self.rng.uniform(-self.precision, self.precision), 3)
        return value, self.device_id, make_reading_token(self.device_id)

    def exact(self, weight_kg: float) -> tuple[float, str, str]:
        """固定读数（入池表单/样例脚本使用）。"""
        return round(weight_kg, 3), self.device_id, make_reading_token(self.device_id)
