"""本地设备适配器:模拟水温传感器、溶氧仪与电子秤。

真实部署时替换为串口/Modbus 实现,接口保持不变。
"""
import itertools
import random
import threading

_lock = threading.Lock()
_scale_seq = itertools.count(1000)


class TemperatureSensor:
    """水温传感器:围绕基准值缓慢漂移,可注入异常。"""

    def __init__(self, base=18.0, drift=0.6):
        self.base = base
        self.drift = drift
        self._offset = 0.0
        self._anomaly = 0.0  # 异常注入量(°C)

    def inject_anomaly(self, delta):
        self._anomaly = delta

    def read(self):
        self._offset = max(-self.drift, min(self.drift,
                           self._offset + random.uniform(-0.25, 0.25)))
        return round(self.base + self._offset + self._anomaly, 1)


class OxygenSensor:
    """溶氧仪:正常 6~9 mg/L,缺氧时 < 4。"""

    def __init__(self, base=7.2):
        self.base = base
        self._anomaly = 0.0

    def inject_anomaly(self, delta):
        self._anomaly = delta

    def read(self):
        return round(max(0.5, self.base + random.uniform(-0.8, 0.8) + self._anomaly), 1)


class Scale:
    """电子秤:每次称重生成 (设备号, 序号) 幂等键,重发同键即去重。"""

    def __init__(self, device_id="SCALE-01"):
        self.device_id = device_id

    def weigh(self, true_weight, jitter=0.05):
        with _lock:
            seq = next(_scale_seq)
        reading = round(true_weight + random.uniform(-jitter, jitter), 2)
        return {
            "weight": reading,
            "device_id": self.device_id,
            "idem_key": f"{self.device_id}-{seq}",
        }


class DeviceHub:
    """按水槽管理的模拟设备集合。"""

    def __init__(self):
        self._sensors = {}
        self._lock = threading.Lock()
        self.scale = Scale("SCALE-01")
        self.out_scale = Scale("SCALE-OUT")

    def sensors_for(self, tank_id, base_temp=18.0):
        with self._lock:
            if tank_id not in self._sensors:
                self._sensors[tank_id] = {
                    "temp": TemperatureSensor(base=base_temp),
                    "oxygen": OxygenSensor(),
                }
            return self._sensors[tank_id]

    def read_tank(self, tank_id, base_temp=18.0):
        s = self.sensors_for(tank_id, base_temp)
        return {"water_temp": s["temp"].read(), "oxygen": s["oxygen"].read()}

    def inject_temp_anomaly(self, tank_id, delta):
        self.sensors_for(tank_id)["temp"].inject_anomaly(delta)

    def inject_oxygen_anomaly(self, tank_id, delta):
        self.sensors_for(tank_id)["oxygen"].inject_anomaly(delta)


hub = DeviceHub()
