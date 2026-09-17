"""PI P-621.1CD 位移台控制后端。

分层（只允许上层依赖下层）：
    server     FastAPI 路由、SSE 遥测
    scanner    扫描执行器（独立线程，与浏览器无关）
    store      SQLite 持久化
    ccd        CCD / 外部设备联动接口
    stage_api  设备层公共面：能力声明、状态结构、异常类型、契约
    pi_stage   PI E-709 实现 / xmt_stage  芯明天 E53.D1S-H 实现
               各自一个 owner 线程，配置选一台，同时只激活一台
    models     REST 数据模型
    config     全部可调参数
"""
