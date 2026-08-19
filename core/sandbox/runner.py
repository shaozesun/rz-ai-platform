"""受限子进程执行器 — 数据分析沙箱的核心执行边界。

在隔离子进程中运行 LLM 生成的 pandas 代码：
- `python -I` 隔离模式：忽略 PYTHONPATH / user site，环境不泄漏
- cwd 锁定在数据集目录，预载 `df = pd.read_csv('data.csv')`
- 资源限制：RLIMIT_AS 内存 + RLIMIT_CPU 时间
- 禁网络：socket / urllib 置空，防数据外泄与 SSRF
- 禁子进程：subprocess / os.system / os.fork / exec* / pty 置空，
  堵住「子进程内 curl 绕过禁网」的漏洞
- 外部超时：asyncio.wait_for + kill，防无限挂起

安全定位：面向内部可信环境（内部运维数据）的 MVP，代码逃逸面受
子进程 + 资源限制约束；硬化路径为 Docker 容器 + Seccomp（对齐 DifySandbox），
见计划「风险与缓解」。
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 产物扫描时忽略的沙箱内部文件（数据集 + chart 提取物 + 分析脚本）
# .py 是 analysis_write_file 落盘 / analysis_exec(file=...) 执行的中间脚本，非交付产物
_IGNORED_ARTIFACTS = {'data.csv', 'meta.json', '_chart_data.json'}
# 报告产物里，图表 PNG/JPG 是嵌入文档（PDF/PPT/Word/Excel）的中间文件，随文档一起
# 列出属于冗余下载，直接剔除；无文档时（用户只要图）仍正常返回图片。
_DOCUMENT_SUFFIXES = {'.pdf', '.pptx', '.docx', '.xlsx'}
_IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}


def _collect_artifacts(
  cwd: str,
  max_artifact_mb: int = 20,
  max_artifacts: int = 10,
) -> tuple[list[dict], str | None]:
  """扫描 cwd 内的用户产物文件（排除沙箱内部文件），做大小/数量/路径校验。

  Returns:
    (artifacts, note): artifacts 为 [{name, size}] 列表；note 为超限提示，无则 None。
  """
  base = Path(cwd).resolve()
  files: list[Path] = []
  try:
    files = [
      p for p in base.iterdir()
      if p.is_file()
      and p.name not in _IGNORED_ARTIFACTS
      and p.suffix.lower() != '.py'
    ]
  except OSError:
    return [], None

  notes: list[str] = []
  keep: list[dict] = []
  for p in files:
    try:
      if not p.resolve().is_relative_to(base):
        continue  # 防用户代码写绝对路径的产物混入
    except OSError:
      continue
    try:
      size = p.stat().st_size
    except OSError:
      continue
    if max_artifact_mb and size > max_artifact_mb * 1024 * 1024:
      notes.append(f'部分产物超过 {max_artifact_mb}MB 上限已忽略')
      continue
    keep.append({'path': p, 'name': p.name, 'size': size})

  # 报告场景：存在文档产物时剔除同级图片（已嵌入文档，别让用户多下载一份 PNG）
  if keep and any(a['path'].suffix.lower() in _DOCUMENT_SUFFIXES for a in keep):
    keep = [a for a in keep if a['path'].suffix.lower() not in _IMAGE_SUFFIXES]

  if max_artifacts and len(keep) > max_artifacts:
    # 保留最近写入的产物（按 mtime 倒序）
    keep.sort(key=lambda a: a['path'].stat().st_mtime, reverse=True)
    keep = keep[:max_artifacts]
    notes.append(f'产物数量超过 {max_artifacts} 上限，仅保留最近的 {max_artifacts} 个')

  artifacts = [{'name': a['name'], 'size': a['size']} for a in keep]
  return artifacts, ('；'.join(notes) if notes else None)


# 占位符用 __XX__ 哨兵 + replace 注入，避免用户 pandas 代码里的花括号（dict 字面量
# 等）被 str.format 误解析。
_WRAPPER_TEMPLATE = r'''
import os, sys, resource, json
# 1. 资源限制：内存（RLIMIT_AS）+ CPU 时间（RLIMIT_CPU）
def _apply_limits():
    try:
        mem = __MAX_MEMORY_MB__ * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (__TIMEOUT__, __TIMEOUT__))
    except Exception:
        pass
_apply_limits()

# 2. 禁用网络外连（socket / urllib 置为抛异常）
def _block(*args, **kwargs):
    raise RuntimeError('沙箱内网络访问已禁用')
import socket
# socket.socket 不能替换成普通函数：ssl.py 的 `class SSLSocket(socket)` 需要子类化
# 真实 socket 类，换成函数会让 reportlab / python-pptx（模块级 import ssl）整体导入
# 崩溃 → 沙箱内 PDF/PPT 报告永远生成不了。改用真实 socket 的子类，只封锁
# 连接/发送/监听等方法，保留可被 ssl 子类化，网络外联依然全封。
class _NoNetSocket(socket.socket):
    def _deny(self, *args, **kwargs):
        raise RuntimeError('沙箱内网络访问已禁用')
    connect = _deny
    connect_ex = _deny
    send = _deny
    sendall = _deny
    sendto = _deny
    sendmsg = _deny
    bind = _deny
    listen = _deny
socket.socket = _NoNetSocket
socket.create_connection = _block
try:
    import urllib.request
    urllib.request.urlopen = _block
    urllib.request.Request = _block
except Exception:
    pass

# 2.5 禁用子进程创建（subprocess / os.system / os.fork / exec* / pty）
# 堵住已确认的绕过漏洞：沙箱内 subprocess.run(['curl',...]) 可绕开禁网外泄数据。
# 模块级替换对用户后续 import subprocess/os 同样生效（拿到同一模块对象）。
import subprocess
subprocess.Popen = _block
subprocess.run = _block
subprocess.call = _block
subprocess.check_call = _block
subprocess.check_output = _block
subprocess.getoutput = _block
subprocess.getstatusoutput = _block
os.system = _block
os.popen = _block
os.fork = _block
os.forkpty = _block
os.posix_spawn = _block
os.posix_spawnp = _block
for _n in ('spawnl','spawnle','spawnlp','spawnlpe','spawnv','spawnve','spawnvp','spawnvpe',
           'execv','execve','execvp','execvpe'):
    setattr(os, _n, _block)
try:
    import pty
    pty.spawn = _block
    pty.fork = _block
except Exception:
    pass

# 3. 预载数据集（cwd 内 data.csv）
import pandas as pd
df = pd.read_csv('data.csv')

# 3.5 禁用原生/进程模块：ctypes 可经 libc 的 system/fork/execve 绕过上面所有
# Python 级封锁（CDLL(None).system(...)），multiprocessing 可经 pickle+exec 起子进程。
# 用 sys.modules 打桩而非改 __import__：importlib.import_module('ctypes') 也绕不过
# sys.modules。已 import 的库（pandas/matplotlib/reportlab 等）早已持有真实引用，
# 打桩只影响用户代码的新 import，不破坏报告生成。
class _NoNativeModule:
    def __getattr__(self, name):
        raise ImportError('沙箱禁止导入 ctypes/_ctypes/multiprocessing（可逃逸沙箱）')
for _m in ('ctypes', '_ctypes', 'multiprocessing'):
    sys.modules[_m] = _NoNativeModule()

# 4. 用户代码
__USER_CODE__

# 5. 结构化图表数据提取：用户把结果赋给 `result`（DataFrame/Series/list[dict]/dict）
__CHART_DATA__ = None
if 'result' in dir():
    _r = result
    if _r is not None:
        try:
            if isinstance(_r, pd.DataFrame):
                __CHART_DATA__ = json.loads(_r.to_json(orient='records', force_ascii=False))
            elif isinstance(_r, pd.Series):
                __CHART_DATA__ = json.loads(_r.reset_index().to_json(orient='records', force_ascii=False))
            else:
                __CHART_DATA__ = _r
            json.dumps(__CHART_DATA__, ensure_ascii=False)  # 序列化校验
        except Exception:
            __CHART_DATA__ = None
if __CHART_DATA__ is not None:
    try:
        with open('_chart_data.json', 'w', encoding='utf-8') as _f:
            json.dump(__CHART_DATA__, _f, ensure_ascii=False)
    except Exception:
        pass
'''


def _build_wrapper(code: str, *, timeout: int, max_memory_mb: int) -> str:
  """注入用户代码与参数到 wrapper 模板。"""
  return (
    _WRAPPER_TEMPLATE
    .replace('__MAX_MEMORY_MB__', str(max_memory_mb))
    .replace('__TIMEOUT__', str(timeout))
    .replace('__USER_CODE__', code)
  )


async def run_python(
  code: str,
  cwd: str,
  *,
  timeout: int = 30,
  max_output_chars: int = 4000,
  max_memory_mb: int = 512,
  max_artifact_mb: int = 20,
  max_artifacts: int = 10,
  cjk_font_path: str | None = None,
  extra_lib_path: str | None = None,
) -> dict:
  """在受限子进程运行用户 pandas 代码。

  Args:
    code: 用户代码（顶层可访问预载变量 `df`）
    cwd: 数据集目录（含 data.csv），进程工作目录与隔离根
    timeout: 单次执行超时（秒）
    max_output_chars: stdout/stderr 返回上限
    max_memory_mb: RLIMIT_AS 内存上限（MB）
    max_artifact_mb: 单产物大小上限（MB），超限剔除
    max_artifacts: 单次执行产物数量上限，超限保留最近写入的
    cjk_font_path: 沙箱报告中文字体绝对路径；非空时以 RZ_CJK_FONT 环境变量
      注入子进程，供脚本 matplotlib / WeasyPrint 加载（跨平台捆绑字体）
    extra_lib_path: 额外动态库搜索路径（如 conda 环境 lib 目录，WeasyPrint 依赖
      Pango/Cairo）；非空时注入 DYLD_LIBRARY_PATH(macOS) 或 LD_LIBRARY_PATH(Linux)。
      Linux 下 apt 已装系统库则留空即可

  Returns:
    dict: {stdout, stderr, timed_out, error, chart_data, chart_note, artifacts, artifact_note}
      - stdout: 用户代码打印输出（截断）
      - stderr: 子进程错误输出（截断）
      - timed_out: 是否超时被杀
      - error: 非零退出码时的简要说明
      - chart_data: 用户代码 `result` 变量的结构化数据（JSON 可序列化对象）；无则 None
      - chart_note: chart_data 被忽略时的说明（如过大）
      - artifacts: cwd 内用户产物文件 [{name, size}]；无则空列表
      - artifact_note: 产物被忽略/超限时的说明；无则 None
  """
  wrapper = _build_wrapper(code, timeout=timeout, max_memory_mb=max_memory_mb)

  # 继承父进程环境，额外注入中文字体路径与动态库搜索路径。
  # 传 env=os.environ 等价于默认继承行为，仅多这几个变量，无隔离降级。
  child_env = None
  if cjk_font_path or extra_lib_path:
    child_env = dict(os.environ)
    if cjk_font_path:
      child_env['RZ_CJK_FONT'] = str(cjk_font_path)
    if extra_lib_path:
      # WeasyPrint 依赖的 Pango/Cairo 在非系统路径时（如 conda env），注入搜索路径。
      # 只作用于本子进程，不影响父进程（后端）加载。
      lib_var = 'DYLD_LIBRARY_PATH' if sys.platform == 'darwin' else 'LD_LIBRARY_PATH'
      existing = child_env.get(lib_var, '')
      child_env[lib_var] = (
        str(extra_lib_path) + (os.pathsep + existing if existing else '')
      )

  proc = await asyncio.create_subprocess_exec(
    sys.executable, '-I', '-c', wrapper,
    cwd=cwd,
    env=child_env,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
  )

  timed_out = False
  try:
    stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
  except asyncio.TimeoutError:
    timed_out = True
    proc.kill()
    stdout_b, stderr_b = await proc.communicate()

  stdout = stdout_b.decode('utf-8', errors='replace')
  stderr = stderr_b.decode('utf-8', errors='replace')

  # 截断输出（pandas 打印大表时可能超限）
  if len(stdout) > max_output_chars:
    stdout = stdout[:max_output_chars] + '\n…（输出过长已截断）'
  if len(stderr) > max_output_chars:
    stderr = stderr[:max_output_chars] + '\n…（错误输出过长已截断）'

  error = None
  if timed_out:
    error = f'代码执行超时（>{timeout}s），已终止。请缩小数据范围或简化计算。'
  elif proc.returncode != 0:
    error = f'代码执行失败（退出码 {proc.returncode}），详见 stderr。'

  # 读取结构化图表数据（wrapper 写入 _chart_data.json；读后删除防污染）
  chart_data = None
  chart_note = None
  chart_path = Path(cwd) / '_chart_data.json'
  if chart_path.is_file():
    try:
      raw = chart_path.read_text(encoding='utf-8')
      if len(raw) > max_output_chars:
        chart_note = (
          f'chart_data 过大（{len(raw)} 字符）已忽略；请缩小聚合后再赋给 result'
        )
      else:
        chart_data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
      chart_data = None
    finally:
      chart_path.unlink(missing_ok=True)

  # 扫描用户产物文件（排除 data.csv/meta.json/_chart_data.json，校验在 cwd 内）
  artifacts, artifact_note = _collect_artifacts(
    cwd, max_artifact_mb=max_artifact_mb, max_artifacts=max_artifacts,
  )

  logger.info(
    'analysis sandbox: rc=%s timed_out=%s stdout=%dB stderr=%dB chart=%s artifacts=%d',
    proc.returncode, timed_out, len(stdout), len(stderr),
    'yes' if chart_data is not None else 'no',
    len(artifacts),
  )
  return {
    'stdout': stdout,
    'stderr': stderr,
    'timed_out': timed_out,
    'error': error,
    'chart_data': chart_data,
    'chart_note': chart_note,
    'artifacts': artifacts,
    'artifact_note': artifact_note,
  }
