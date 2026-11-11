"""物流模块。

    waybill      退换货单号生成 / 送达计时（普通订单运单号由商家填写）
    status       物流状态动态计算（按不同时间段匹配）
    repository   cs.logistics 物流表 + cs.order 物流字段读写
    tracking_api 外部真实快递查询（非 7 位单号走 uapis.cn）
"""
