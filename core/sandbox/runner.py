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
import time
import sys
from pathlib import Path

from core.sandbox.report_style import RZ_MPL_SRC

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
# 异常同时继承 OSError：部分库只按 OSError 降级。matplotlib 的 fontconfig 探测
# （不设 MPL_IGNORE_SYSTEM_FONTS 时会调 `fc-list`）只捕获 OSError/CalledProcessError，
# 纯 RuntimeError 会让它在字体缓存缺失时直接崩掉 —— 2026-10-19 事故：容器重建后
# 缓存文件不在，所有报告卡在 import matplotlib → 这里。
class _SandboxBlocked(RuntimeError, OSError):
    pass


def _block(*args, **kwargs):
    raise _SandboxBlocked('沙箱内网络访问已禁用')
import socket
# socket.socket 不能替换成普通函数：ssl.py 的 `class SSLSocket(socket)` 需要子类化
# 真实 socket 类，换成函数会让 reportlab / python-pptx（模块级 import ssl）整体导入
# 崩溃 → 沙箱内 PDF/PPT 报告永远生成不了。改用真实 socket 的子类，只封锁
# 连接/发送/监听等方法，保留可被 ssl 子类化，网络外联依然全封。
class _NoNetSocket(socket.socket):
    def _deny(self, *args, **kwargs):
        raise _SandboxBlocked('沙箱内网络访问已禁用')
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

# 3. 预载数据集（cwd 内 data.csv）。**容忍没有 data.csv**：workspace（成文/文档类脚本的
# 保留目录）按设计就不放数据集，早先这里硬读会让那些脚本在预载这一行就崩、一行都跑不到
# （2026-09 实测：模型要生成 Word，连撞 4 次同一个 read_csv traceback，最后退回 Markdown）。
try:
    import pandas as pd
    df = pd.read_csv('data.csv')
except FileNotFoundError:
    df = None
    # 口吻必须中性：沙箱里任何"出错/异常"字样都会被模型当成环境故障，进而放弃作业
    print('（本目录无 data.csv：df 未预载，适合成文/文档类脚本）')

# 3.4 预热 WeasyPrint（HTML→PDF 报告依赖）：其底层用 cffi dlopen Pango/Cairo，
# 首次 import 时会触发 `import ctypes.util`。这发生在下面 3.5 给 ctypes 打桩*之前*，
# 让 cffi 拿到真实 ctypes 完成 dlopen；用户代码后续 import weasyprint 时模块已在
# sys.modules 缓存里，不会重新触发 ctypes.util 导入。不装 weasyprint 的环境静默跳过。
#
# **必须静音 stdout**：缺 Pango/Cairo 时 WeasyPrint 会自己往 stdout 打一段多行告警
# （"WeasyPrint could not import some external libraries... troubleshooting"）。
# 每次沙箱执行都是新进程 → **每一次执行**（哪怕只是算个 pandas 均值）的输出里都带着它，
# 模型读到后判断成"执行环境有问题"，于是要点名产出 Word/PDF 时放弃产文件、退回 Markdown
# （2026-09 实测：模型原话「由于 Word 文档生成遇到技术困难，我已生成 Markdown 版本」，
# 而实际 docx 在沙箱里跑得通）。预热本身照旧，只吞掉这段噪音。
try:
    import contextlib as _ctxlib
    import io as _io

    with _ctxlib.redirect_stdout(_io.StringIO()):
        import weasyprint  # noqa: F401
except Exception:
    pass

# 3.5 禁用原生/进程模块：ctypes 可经 libc 的 system/fork/execve 绕过上面所有
# Python 级封锁（CDLL(None).system(...)），multiprocessing 可经 pickle+exec 起子进程。
# 用 sys.modules 打桩而非改 __import__：importlib.import_module('ctypes') 也绕不过
# sys.modules。已 import 的库（pandas/matplotlib/reportlab/weasyprint 等）早已持有
# 真实引用，打桩只影响用户代码的新 import，不破坏报告生成。
class _NoNativeModule:
    def __getattr__(self, name):
        raise ImportError('沙箱禁止导入 ctypes/_ctypes/multiprocessing（可逃逸沙箱）')
for _m in ('ctypes', '_ctypes', 'multiprocessing'):
    sys.modules[_m] = _NoNativeModule()

# 3.5b HTML→PDF 兜底：把 weasyprint.HTML 换成**自带本地资源加载器**的子类。
# 为什么必须兜底（实测三次翻车）：提示词里连写"用 render_pdf / 别自己调 WeasyPrint"，
# 模型三轮回回都自己写 —— `HTML(string=.., base_url='.')`、`<img src="file://charts/x.png">`；
# 而沙箱禁网把 urllib 一起禁掉，WeasyPrint 读不到本地图片与字体 → **图静默全丢**（不报错）。
# 换成子类后，模型怎么写都能读到本地图（相对路径按 cwd 解析），也不再受 base_url 写法影响。
def _rz_url_response(url, file_obj, mime):
    """构造 url_fetcher 的返回值：WeasyPrint <70 只认 dict；>=70 要求
    URLFetcherResponse（70.0 删掉了 dict 兼容转换，返回 dict 会断言失败）。"""
    try:
        from weasyprint.urls import URLFetcherResponse
    except ImportError:
        return {'file_obj': file_obj, 'mime_type': mime, 'redirected_url': url}
    return URLFetcherResponse(
        url=url, body=file_obj, headers={'Content-Type': mime})


def _rz_local_fetch(url, *args, **kwargs):
    import mimetypes
    from pathlib import Path as _P
    raw = str(url)
    if raw.startswith('http://') or raw.startswith('https://'):
        raise RuntimeError('沙箱内不允许联网加载资源')
    p = _P(raw[7:] if raw.startswith('file://') else raw)
    if not p.is_absolute():
        p = _P.cwd() / p
    mime = mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
    return _rz_url_response(p.as_uri(), p.open('rb'), mime)


try:
    import weasyprint as _wp
    class _RzHTML(_wp.HTML):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('url_fetcher', _rz_local_fetch)
            super().__init__(*args, **kwargs)
    _wp.HTML = _RzHTML
except Exception:
    pass


# 3.6 平台提供的 HTML→PDF（报告脚本请用它，别自己调 WeasyPrint）
#
# 为什么必须用它：**WeasyPrint 默认用 urllib 取资源（图片/字体），而沙箱为了防外泄把
# urllib 禁了** → 本地图片也读不到，PDF 里图表**静默全丢**（实测：只报
# "Failed to load image ... 沙箱内网络访问已禁用"，文字照常、看不出缺图）。
# 这里给它一个自己的 url_fetcher：**只认本地文件（纯 open 读）**，http/https 一律拒绝 ——
# 既能读图，又不会绕过禁网（真正的网络出口 socket 依然是封的）。顺带把两种"写法坑"也治了：
#   ① `base_url='.'` 这类相对基准 → 统一按 HTML 所在目录解析
#   ② `<img src="file://charts/x.png">`（非法 URL）→ 取其路径部分再按 ① 解析
# 用法：render_pdf('report.html', '事件工单分析报告.pdf')；需要独立 CSS 时传 css='...'
def render_pdf(html_path, pdf_path, css=None):
    import mimetypes
    import re as _re
    from pathlib import Path as _P
    from weasyprint import HTML as _HTML, CSS as _CSS

    _html_file = _P(html_path).resolve()
    _base = _html_file.parent

    # 先把 HTML 里"畸形 file:// URL"（`file://charts/x.png` 这种，host 被当成 charts，
    # 路径会变 `//charts/x.png`）掰正成相对路径；再把相对路径统一锚到 HTML 所在目录。
    _div = open(_html_file, encoding='utf-8').read()
    _div = _re.sub(r'(?<!:)file://(?![/])', '', _div)

    def _fetch(url, *args, **kwargs):
        raw = str(url)
        if raw.startswith('http://') or raw.startswith('https://'):
            raise RuntimeError('沙箱内不允许联网加载资源')
        path = raw[7:] if raw.startswith('file://') else raw
        p = _P(path)
        if not p.is_absolute():
            p = _base / p
        mime = mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
        # 直接给文件对象：绕开 urllib 与二次路径解析；url 用真实绝对路径
        return _rz_url_response(p.as_uri(), p.open('rb'), mime)

    stylesheets = [_CSS(string=css)] if css else None
    _HTML(string=_div, base_url=_base.as_uri() + '/', url_fetcher=_fetch).write_pdf(
        str(pdf_path), stylesheets=stylesheets)
    return str(pdf_path)


# 3.7 报告统一样式（定义 rz_setup/rz_fig/rz_save/RZ_COLORS，并把 render_pdf 的缺省 CSS
# 换成统一版式）。只是**定义**、不 import matplotlib —— 不画图的脚本一分钱不付。
# 源码唯一定义在 core/sandbox/report_style.py，由 _build_wrapper() 注入。
__RZ_STYLE__

# 4. 用户代码
__USER_CODE__

# 5. 结构化图表数据提取：用户把结果赋给 `result`
#    （DataFrame/Series，或 dict/list 且内部可嵌套 DataFrame/Series——支持一次
#      exec 产出多组聚合：result = {'trend': df1, 'pie': df2}）
def __rz_jsonable(_o, _d=0):
    if _d > 6:
        return None
    if isinstance(_o, pd.DataFrame):
        return json.loads(_o.to_json(orient='records', force_ascii=False))
    if isinstance(_o, pd.Series):
        return json.loads(_o.reset_index().to_json(orient='records', force_ascii=False))
    if isinstance(_o, dict):
        return {k: __rz_jsonable(v, _d + 1) for k, v in _o.items()}
    if isinstance(_o, (list, tuple)):
        return [__rz_jsonable(v, _d + 1) for v in _o]
    return _o


__CHART_DATA__ = None
if 'result' in dir():
    _r = result
    if _r is not None:
        try:
            __CHART_DATA__ = __rz_jsonable(_r)
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
    .replace('__RZ_STYLE__', RZ_MPL_SRC)
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

  # 继承父进程环境，注入线程限制 + 中文字体路径 + 动态库搜索路径。
  # 线程限制：沙箱 RLIMIT_AS（默认 512MB）下 OpenBLAS 按核数开线程（多核服务器
  # 12 线程），线程栈 + 线程本地缓冲 + 科学库虚拟地址空间占用会顶破地址空间上限，
  # pthread_create 报 EAGAIN（Resource temporarily unavailable）。压成单线程后
  # 虚拟地址空间占用降一个数量级，且 pandas 聚合本就轻量，无性能损失。
  child_env = dict(os.environ)
  child_env['OPENBLAS_NUM_THREADS'] = '1'
  child_env['OMP_NUM_THREADS'] = '1'
  child_env['MKL_NUM_THREADS'] = '1'
  child_env['NUMEXPR_NUM_THREADS'] = '1'
  # 无 DISPLAY 环境下 matplotlib 必须用 Agg：模型即使没调 rz_setup() 也不会因为
  # 选中交互后端而崩。挂环境变量而不是在 wrapper 里 import（不画图的脚本零代价）。
  child_env['MPLBACKEND'] = 'Agg'
  if cjk_font_path:
    # 字体文件是构建不变式（随仓库提交、镜像里也在），不存在说明构建坏了：早报错，
    # 否则脚本里 matplotlib.addfont 会抛难懂的 FileNotFoundError、WeasyPrint 直接静默丢汉字
    if not Path(str(cjk_font_path)).is_file():
      raise RuntimeError(
        f'中文字体文件不存在: {cjk_font_path}（报告出图会缺字）；'
        'assets/fonts/wqy-microhei.ttc 需提交进 git 并重建镜像'
      )
    child_env['RZ_CJK_FONT'] = str(cjk_font_path)
  if extra_lib_path:
    # WeasyPrint 依赖的 Pango/Cairo 在非系统路径时（如 conda env），注入搜索路径。
    # 只作用于本子进程，不影响父进程（后端）加载。
    lib_var = 'DYLD_LIBRARY_PATH' if sys.platform == 'darwin' else 'LD_LIBRARY_PATH'
    existing = child_env.get(lib_var, '')
    child_env[lib_var] = (
      str(extra_lib_path) + (os.pathsep + existing if existing else '')
    )

  run_start = time.monotonic()
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
    'analysis sandbox: rc=%s timed_out=%s elapsed=%.2fs stdout=%dB stderr=%dB chart=%s artifacts=%d',
    proc.returncode, timed_out, time.monotonic() - run_start,
    len(stdout), len(stderr),
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
