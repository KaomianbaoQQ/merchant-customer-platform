"""售后模块 —— 售后客服（Dify 工作流 + cs.order 增删改查）。

模块：
    config     读取 admin.json 管理员配置
    auth       权限校验（管理员判定、订单归属校验）
    user       cs.user     用户登录校验 / 信息查询
    order      cs.order    订单增删改查 + 下单结算 + cs.operate_log 操作日志
    address    cs.address_book 用户地址簿（保存 / 复用 / 增删改 / 默认地址）
    chat       cs.chat_log 全部对话记录
    workflow   调用「售后客服」Dify 工作流
    service    售后主流程的 Web 封装（handle_after_sales）
    session_state 售后会话临时状态（待选订单 / 是否已转人工）
    returns    退换货流程（校验物流 / 生成新物流单号 / 我已发出 / 换货重新发货）
    cli        命令行对话流程（run_cli）
"""
