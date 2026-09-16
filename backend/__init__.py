"""PI P-621.1CD 位移台控制后端。

分层（只允许上层依赖下层）：
    server     FastAPI 路由、SSE 遥测
    scanner    扫描执行器（独立线程，与浏览器无关）
    store      SQLite 持久化
    ccd        CCD / 外部设备联动接口
    pi_stage   设备层：唯一 owner 线程，所有 GCS 访问都排队经过它
    models     REST 数据模型
    config     全部可调参数
"""
