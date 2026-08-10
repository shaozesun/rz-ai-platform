"""安防平台接入（占位）。

暂无接口文档，本模块只做「让第三个平台能挂进现有 Platform 分层」的骨架：
capabilities 为空、client.call 直接抛异常。接口文档到位后参照 service/query/mgmt/
的接入模式（EndpointSpec 声明式映射）填实，其余分层（retriever/tools_factory/
intent_bindings）无需改动。
"""
