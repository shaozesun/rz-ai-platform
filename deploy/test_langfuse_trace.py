"""手动发一条 trace 到 Langfuse，验证 langfuse 4.x 客户端能否正常发送。

用 start_as_current_observation（自动创建 trace 根），而不是 start_observation。
用法（在服务器）：
  docker cp /opt/rz-ai/deploy/test_langfuse_trace.py rz-backend:/tmp/test_langfuse_trace.py
  docker exec rz-backend python /tmp/test_langfuse_trace.py
然后在 Langfuse(http://10.100.17.151:3001) 刷新，看有没有「手动测试trace」。
"""
from langfuse import Langfuse

lf = Langfuse()
with lf.start_as_current_observation(
    name='手动测试trace',
    as_type='generation',
    input={'text': 'hello'},
):
    print('observation 内')
lf.flush()
print('trace 已发送，请到 Langfuse(http://10.100.17.151:3001) 刷新查看')
