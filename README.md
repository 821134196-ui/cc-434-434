# 活鲜暂养与损耗结算平台

海鲜档口活鲜暂养管理:档口/水槽/品种批次/入池称重/暂养记录/订单分拣/死亡报损/供应商结算。
纯 Python 标准库实现(零第三方依赖),SQLite 持久化,单命令本地启动。

## 快速开始

```bash
python3 run.py              # 启动并自动生成演示数据(入池/分拣/报损样例)
python3 run.py --fresh      # 清空旧库重建演示数据
python3 run.py --port 8080  # 指定端口
```

浏览器打开 http://127.0.0.1:8000 。右上角可切换用户(管理员/员工/供应商账号),
供应商视角仅能看到本单位结算单与差异。

## 运行测试

```bash
python3 -m unittest discover -s tests -t tests -v
```

覆盖:容量校验、批次并发锁定、称重去重、损耗计算、供应商权限(30 用例)。

## 功能与规则

| 模块 | 规则 |
| --- | --- |
| 批次入池 | 校验水槽容量、品种盐度适配、混养冲突(龙虾×蟹、虾×石斑);秤单幂等键去重 |
| 订单分拣 | 按订单锁定数量,原子扣减批次余量——两名员工同时分拣同一批次只允许一单成功;订单取消自动释放锁定 |
| 死亡报损 | 必须关联原批次;累计损耗率 > 8% 触发"死亡率超阈值"告警与进程内损耗提醒 |
| 补录称重 | 保留原始值与设备号;差异 >0.5kg 只生成待结算差异(挂账),供应商确认前不改账面 |
| 供应商结算 | 结算重量 = 入池 − 损耗;差异须先确认/驳回;确认后批次锁定,不能再次改重量 |
| 异常处理 | 水温异常、缺氧(巡检任务自动判定)、混养冲突、称重差异、订单取消、死亡率超阈值分别落告警 |

## 架构

```
run.py               单条启动命令(建库→演示数据→巡检任务→Web 服务)
app/
  db.py              SQLite schema 与线程级连接
  services.py        业务规则(入池/分拣/损耗/补录/结算/权限)
  devices.py         本地设备适配器:水温传感器、溶氧仪、电子秤(模拟)
  tasks.py           进程内巡检任务:暂养记录采样、异常告警、损耗提醒
  seed.py            演示数据(供应商/客户/金额均为演示数据)
  web.py             标准库 HTTP 服务:JSON API + 静态页面
static/              前端(海蓝/白/番茄红):水槽看板、批次入池、暂养记录、
                     订单分拣、损耗处置(损耗↔原批次连线)、供应商对账
tests/               unittest:容量/锁定/称重去重/损耗/供应商权限
```

## 界面说明

- **水槽看板**:水槽按区域排列,卡片显示容量条、水温、盐度、溶氧与在养批次(重量大号数字);
  异常水槽固定在顶部警示位,不随滚动消失。
- **损耗处置**:左侧损耗记录与右侧原批次以虚线连接,直观呈现报损来源。
- **供应商对账**:待确认差异置顶,确认差异后才能确认结算;金额大号数字展示。

## API 一览

```
GET  /api/meta /api/dashboard /api/batches /api/holding /api/orders
     /api/loss /api/settlements /api/diffs /api/alerts
POST /api/entry                      入池(幂等键 idem_key 去重)
POST /api/batches/<id>/reweigh       补录称重(差异挂账)
POST /api/orders  /api/orders/<id>/cancel
POST /api/sorting                    分拣出池(并发互斥)
POST /api/loss                       死亡报损
POST /api/settlements/generate  /api/settlements/<id>/confirm
POST /api/diffs/<id>/resolve         差异确认/驳回
POST /api/devices/scale/read         模拟电子秤读数
POST /api/devices/anomaly            注入水温/溶氧异常(演示)
POST /api/patrol                     手动触发一次巡检
```

身份通过请求头 `X-User-Id` 传递(演示用);权限校验在服务层执行。
