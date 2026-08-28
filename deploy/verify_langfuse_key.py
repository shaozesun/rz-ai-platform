"""验证 Langfuse API key 是否有效（用 key 调 langfuse 的 /api/public/projects）。

用法（在服务器）：
  docker cp /opt/rz-ai/deploy/verify_langfuse_key.py rz-backend:/tmp/verify_langfuse_key.py
  docker exec rz-backend python /tmp/verify_langfuse_key.py

判断：status 200 = key 有效；401 = key 无效/项目不匹配。
"""
import base64
import urllib.request
import urllib.error

KEY = 'pk-lf-ff1bb743-bdb2-49a9-8159-3fda6f659a02:sk-lf-6637df77-8def-4c22-b396-5ec1408d64de'
auth = 'Basic ' + base64.b64encode(KEY.encode()).decode()
req = urllib.request.Request(
    'http://langfuse:3000/api/public/projects',
    headers={'Authorization': auth},
)
try:
    resp = urllib.request.urlopen(req, timeout=5)
    print('status:', resp.status)
except urllib.error.HTTPError as e:
    print('status:', e.code, e.reason)
except Exception as e:
    print('error:', e)
