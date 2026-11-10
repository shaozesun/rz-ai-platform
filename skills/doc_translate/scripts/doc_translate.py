"""保格式文档翻译（XML 层）：**只改文字节点**，其余一个字节都不动。

本脚本**无命令行参数**——沙箱是 `python -I -c <代码>` 跑的，`sys.argv` 拿不到参数。
改为读**同目录（会话 workspace）下的任务文件** `doc_translate_task.json`：

    第一趟（抽取）：
    {"cmd": "extract", "src": "原件.docx", "out": "segments.json"}

    取待翻译文本（分页，防 stdout 上限截断）：
    {"cmd": "show", "out": "segments.json", "from": 1, "to": 60}

    最后一趟（模型翻译完，写回）：
    {"cmd": "apply", "src": "原件.docx", "seg": "segments.json",
     "trans": "translation.json", "dst": "English.docx", "clean_cjk_indent": true}

三段式（沙箱禁网 → 翻译必须由模型在沙箱外做）：
  1) extract：按文档真实顺序抽出「编号 + 定位 + 原文」→ JSON
  2) 模型翻译 → 产出 {"编号": "译文"} 的 JSON
  3) apply：**逐段回校验**（当前原文 == 记录原文）后把译文写进原文件的对应位置 → 另存新文件

## 为什么在 XML 层做，而不是 python-docx 的对象模型

对象模型上连着踩过四个坑，换层后**结构性地消失**：

| 对象模型的坑 | XML 层为什么没这个问题 |
|---|---|
| `doc.paragraphs` 看不到 `w:sdt`，整份**目录**抽不到 | `root.iter(w:p)` 天然覆盖 sdt / 文本框 / 表格 |
| `run.text = x` 会清空该 run 的**全部内容**，连带删掉图片/流程图 | 只改 `w:t` 文本节点，**不碰 `w:r`**，drawing/fldChar 零风险 |
| `id(element)` 去重（lxml 代理会被 GC 后复用 id）误跳整张表 | 不做 id 去重，按 part + 段落下标定位 |
| apply 按「第 N 个非空段落」硬写 → **静默串位**（旧 segments 112 条 vs 新遍历 160 段） | 每段先校验原文一致，不符即整体中止（见下） |

收尾的「克隆式改写」原则来自业界（Anthropic 官方 docx 技能、kimi-docx）：
**clone and modify, never recreate**——zip 里除被翻译的 part 外全部原样复制。

## 已知取舍（如实告知用户）

- **段内混合格式**（如句中某个加粗词）会退化成该段首个文字 run 的格式；
  单格式段落（绝大多数）完全无损。
- **目录条目 / 页码 / 日期等「域」内容不翻**（`w:fldChar` / `w:fldSimple` / `w:instrText`）。
  它们是 Word 自动生成的：静态改写会把页码写死或写乱，且域内文本一带上页码
  （`1.1 总则…3`）会让翻译本身失真。英文目录的正确做法是**在 Word 里选中目录按 F9 更新**，
  页码会按译文重新排。脚本会在 extract 里报出跳过了多少段。
"""

import json
import re
import sys
import zipfile
from pathlib import Path

TASK_FILE = 'doc_translate_task.json'

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'

# 正文 part 固定排第一（id 从正文开头连续编号）；其余 part 按名字排序，保证遍历顺序确定
_MAIN_PART = 'word/document.xml'
# 页眉页脚 + 脚注/尾注/批注（都在 word/ 下，各自是独立 part，part 内同样是 w:p 容器）
_EXTRA_PREFIXES = ('header', 'footer')
_EXTRA_EXACT = ('footnotes.xml', 'endnotes.xml', 'comments.xml')

_TASK_DOC = (
  f'未找到任务文件 {TASK_FILE}。请先用 analysis_write_file 在 workspace 落一个，内容形如：\n'
  '  {"cmd": "extract", "src": "原件.docx", "out": "segments.json"}\n'
  '  {"cmd": "show", "out": "segments.json", "from": 1, "to": 60}\n'
  '  {"cmd": "apply", "src": "原件.docx", "seg": "segments.json", '
  '"trans": "translation.json", "dst": "English.docx", "clean_cjk_indent": true}'
)


def _w(tag: str) -> str:
  return f'{{{W}}}{tag}'


def _text_parts(zf: zipfile.ZipFile) -> list:
  """需要处理的 part 名单（顺序固定 → extract 与 apply 的编号才能对上）。"""
  names = set(zf.namelist())
  parts = [_MAIN_PART] if _MAIN_PART in names else []
  extra = []
  for n in names:
    if not n.startswith('word/') or not n.endswith('.xml'):
      continue
    base = n[len('word/'):]
    if base.startswith(_EXTRA_PREFIXES) or base in _EXTRA_EXACT:
      extra.append(n)
  return parts + sorted(extra)


# --------------------------------------------------------------------------
# 读：段落文字与分类
# --------------------------------------------------------------------------

def _in_nested_para(el, p_el) -> bool:
  """el 是否落在 p_el **内部嵌套的另一个段落**里（文本框 w:txbxContent 内的 w:p）。

  必须区分：`p_el.iter(w:t)` 是递归的，会把文本框里的文字也算进外层段落，
  导致外层段落"凭空多出一截文字"。
  """
  node = el.getparent()
  while node is not None and node is not p_el:
    if node.tag == _w('p'):
      return True
    node = node.getparent()
  return False


def _own(p_el, tag: str) -> list:
  """p_el **自己的** tag 元素（排除嵌套段落里的）。"""
  return [e for e in p_el.iter(tag) if not _in_nested_para(e, p_el)]


def _para_text(p_el) -> str:
  """段落文字 = 该段自己的 w:t 按文档顺序拼接（Word 常把一句话切成多个 run/w:t）。"""
  return ''.join((t.text or '') for t in _own(p_el, _w('t'))).strip()


def _is_auto_para(p_el) -> bool:
  """是否是 Word 自动生成的内容（域）：目录条目、页码、日期、交叉引用等。

  判据：段落里出现 `w:fldChar`（复杂域）、`w:fldSimple`（简单域）或 `w:instrText`（域指令）。
  这类段落**整段不翻**——见模块 docstring 的取舍说明。
  """
  for tag in (_w('fldChar'), _w('fldSimple'), _w('instrText')):
    if _own(p_el, tag):
      return True
  return False


def _is_toc_para(p_el) -> bool:
  """目录段落兜底判据（没有域标记的静态目录）：TOC* 样式，或包在「目录」内容控件里。"""
  ppr = p_el.find(_w('pPr'))
  if ppr is not None:
    style = ppr.find(_w('pStyle'))
    if style is not None and re.match(r'^toc', style.get(_w('val')) or '', re.I):
      return True
  node = p_el.getparent()
  while node is not None:
    if node.tag == _w('sdt'):
      for g in node.iter(_w('docPartGallery')):
        if 'table of contents' in (g.get(_w('val')) or '').lower():
          return True
    node = node.getparent()
  return False


# --------------------------------------------------------------------------
# 写：合并同格式相邻 run（官方 merge_runs.py 的等价物）+ 只改 w:t
# --------------------------------------------------------------------------

def _is_plain_run(r_el) -> bool:
  """run 是否**只含文字**（w:rPr 格式 + w:t 文本）。

  含 w:drawing / w:fldChar / w:br / w:tab / w:footnoteReference 的 run 一律不参与合并，
  也不作为写译文的目标——避免把图片、域、换行写没。
  """
  for child in r_el:
    if child.tag not in (_w('t'), _w('rPr')):
      return False
  return True


def _rpr_key(r_el):
  """run 的格式指纹：`w:rPr` 完全相同才允许合并（不同格式不合并 → 加粗/斜体各自保留）。"""
  from lxml import etree

  rpr = r_el.find(_w('rPr'))
  return b'' if rpr is None else etree.tostring(rpr)


def _merge_runs(root) -> int:
  """合并**相邻且 rPr 相同**的纯文字 run（减少 run 碎片，让段落文本是连续的一个 w:t）。

  只在 `w:p` / `w:hyperlink` 的直接子元素之间合并；任何非 run 元素
  （bookmarkStart、proofErr、超链接边界…）都会打断相邻关系 → 不会跨结构合并。
  """
  from copy import deepcopy

  merged = 0
  for parent in root.iter():
    if parent.tag not in (_w('p'), _w('hyperlink')):
      continue
    keep = None
    for child in list(parent):
      if (keep is not None and child.tag == _w('r') and keep.tag == _w('r')
          and _is_plain_run(child) and _is_plain_run(keep)
          and _rpr_key(child) == _rpr_key(keep)):
        src_t = child.find(_w('t'))
        if src_t is not None:
          dst_t = keep.find(_w('t'))
          if dst_t is None:
            keep.append(deepcopy(src_t))
          else:
            dst_t.text = (dst_t.text or '') + (src_t.text or '')
        parent.remove(child)
        merged += 1
      else:
        keep = child
  return merged


def _write_para(p_el, new_text: str) -> int:
  """把译文写进该段**第一个 w:t**、其余 w:t 置空。只动文本节点，不删/不改任何 w:r。

  Returns: 清空的 w:t 个数（供统计）。
  """
  targets = _own(p_el, _w('t'))
  if not targets:
    return -1  # 该段没有可安全改写的文字节点（如整段就是一张图）
  first, rest = targets[0], targets[1:]
  first.text = new_text
  if new_text[:1].isspace() or new_text[-1:].isspace():
    first.set(XML_SPACE, 'preserve')
  for t in rest:
    t.text = ''
  return len(rest)


def _clean_cjk_indent(root) -> int:
  """清掉「首行缩进 N 字符」这类东亚段落属性——翻译成英文后这类缩进是多余的。"""
  removed = 0
  for e in root.iter(_w('ind')):
    for attr in ('firstLineChars', 'leftChars'):
      key = _w(attr)
      if e.get(key) is not None:
        del e.attrib[key]
        removed += 1
  return removed


# --------------------------------------------------------------------------
# 包读写
# --------------------------------------------------------------------------

def _load_parts(src: str) -> list:
  """读出全部待处理 part 的 XML 树，返回 [(part 名, root, [该 part 的全部 w:p])]。

  遍历顺序 = 文档顺序（`iter` 是文档序深度优先）：正文 → 表格单元格（含嵌套表）→
  内容控件（目录就在这）→ 文本框；再是页眉页脚/脚注尾注批注。
  """
  from lxml import etree

  with zipfile.ZipFile(src) as zf:
    out = []
    for name in _text_parts(zf):
      root = etree.fromstring(zf.read(name))
      _merge_runs(root)
      out.append((name, root, list(root.iter(_w('p')))))
  return out


def _collect(parts: list) -> tuple:
  """把 parts 折叠成有序的待翻译条目 + 被跳过的自动生成段落。

  `p` = 段落在该 part 内的下标（**含空段一起数**），extract 与 apply 用同一遍历 → 稳定。
  """
  items, skipped = [], []
  for name, _root, paras in parts:
    for idx, p_el in enumerate(paras):
      if _is_auto_para(p_el) or _is_toc_para(p_el):
        text = _para_text(p_el)
        if text:
          skipped.append({'part': name, 'p': idx, 'text': text})
        continue
      text = _para_text(p_el)
      if text:
        items.append({'id': len(items) + 1, 'part': name, 'p': idx, 'text': text})
  return items, skipped


def _save_pkg(src: str, dst: str, replaced: dict) -> None:
  """克隆式另存：zip 成员逐个复制，只有 replaced 里的 part 换成新字节。"""
  with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, 'w') as zout:
    for info in zin.infolist():
      data = replaced.get(info.filename)
      zout.writestr(info, zin.read(info.filename) if data is None else data)


# --------------------------------------------------------------------------
# 三个命令
# --------------------------------------------------------------------------

def cmd_extract(task: dict) -> int:
  src, out = task.get('src'), task.get('out') or 'segments.json'
  if not src:
    print('任务缺 src（源文件名）')
    return 2

  parts = _load_parts(src)
  items, skipped = _collect(parts)
  with open(out, 'w', encoding='utf-8') as f:
    json.dump(items, f, ensure_ascii=False, indent=1)

  print(f'抽出 {len(items)} 段 → {out}')
  dist = [f'{name.split("/")[-1]} {sum(1 for i in items if i["part"] == name)}'
          for name, _r, _p in parts if any(i['part'] == name for i in items)]
  print('（分布：' + '，'.join(dist) + '）')
  if skipped:
    print(f'⚠️ 跳过 {len(skipped)} 段「Word 自动生成内容」（目录条目/页码/日期等域），保持原文：')
    for s in skipped[:5]:
      print(f'   {s["part"]} #{s["p"]}: {s["text"][:60]}')
    if len(skipped) > 5:
      print(f'   …另有 {len(skipped) - 5} 段')
    print('   → 英文目录请在 Word 里选中目录按 F9 更新（页码会按译文重排）；'
          '记得在回复里告知用户这几处未翻')
  print('下一步：把每段 text 翻译后写成 {"编号": 译文} 的 JSON，'
        f'再把任务文件改成 {{"cmd":"apply","src":"{src}","seg":"{out}",'
        '"trans":"<译文文件>","dst":"<输出名>"}} 重跑本脚本')
  return 0


def cmd_show(task: dict) -> int:
  """打印 segments.json 里的第 from~to 条，供模型**分页**读取待翻译文本。

  为什么要单独一个命令：沙箱 stdout 有上限（4000 字），长文档一次打印会被截断，
  模型就漏翻了。分页取、分页翻，最后合并成一个 translation.json 再 apply。
  """
  path = task.get('out') or 'segments.json'
  with open(path, encoding='utf-8') as f:
    items = json.load(f)
  start = int(task.get('from') or 1)
  end = int(task.get('to') or len(items))
  chunk = [i for i in items if start <= i['id'] <= end]
  for it in chunk:
    print(f"{it['id']}\t{it['text']}")
  print(f'# 本次显示 {len(chunk)} 段（第 {start}~{end} 段，共 {len(items)} 段）')
  return 0


def _load_translation(path: str) -> dict:
  with open(path, encoding='utf-8') as f:
    raw = json.load(f)
  # 兼容两种写法：{"1": "译文"} 或 [{"id": 1, "text": "译文"}]
  if isinstance(raw, list):
    return {str(item['id']): (item.get('text') or '') for item in raw}
  return {str(k): (v or '') for k, v in raw.items()}


def cmd_apply(task: dict) -> int:
  from lxml import etree

  src = task.get('src')
  seg_path = task.get('seg') or 'segments.json'
  trans_path = task.get('trans') or 'translation.json'
  dst = task.get('dst') or 'translated.docx'
  if not src:
    print('任务缺 src（源文件名）')
    return 2
  # 源文件必须原样保留（用户可能还要基于它再翻一版）；dst 与 src 同名会覆盖原件
  if Path(src).resolve() == Path(dst).resolve():
    print(f'❌ dst 不能与 src 同名（{src}）——会覆盖原件。换个输出名，如 translated.docx')
    return 2

  trans = _load_translation(trans_path)

  try:
    with open(seg_path, encoding='utf-8') as f:
      segs = json.load(f)
  except FileNotFoundError:
    print(f'未找到 {seg_path}——apply 必须带着 extract 产出的 segments 一起跑'
          '（它用于逐段校验，防错位）')
    return 2

  # ---- 校验 1：segments 必须是本脚本的格式（旧版遍历产出的是 where 字段）----
  stale = [s for s in segs if 'part' not in s or 'p' not in s]
  if stale:
    print(f'❌ {seg_path} 是**旧格式**（{len(stale)}/{len(segs)} 条没有 part/p 定位字段）。'
          '旧遍历（doc.paragraphs + table[i,j]）与新遍历编号不一致，直接写回必然串位。')
    print('   → 请重新 extract 生成新 segments，并**按新编号重新翻译**。未产出任何文件。')
    return 1

  # ---- 校验 2：逐段比对「当前原文 == 抽取时记录的原文」----
  parts = _load_parts(src)
  items, _skipped = _collect(parts)
  if len(items) != len(segs):
    print(f'❌ segments 与当前文档对不上：segments {len(segs)} 段，文档现有 {len(items)} 段。')
    print(f'   → {seg_path} 已过期（脚本更新过，或换了源文件）。'
          '请重新 extract 并重新翻译。未产出任何文件。')
    return 1

  mismatches = []
  for seg, cur in zip(segs, items):
    if seg.get('text') != cur['text'] or seg.get('part') != cur['part'] or seg.get('p') != cur['p']:
      mismatches.append((cur['id'], seg, cur))
  if mismatches:
    print(f'❌ {len(mismatches)}/{len(items)} 段与 segments 对不上——写回会整体串位，已中止。前几处：')
    for sid, seg, cur in mismatches[:5]:
      print(f'   第 {sid} 段  {seg.get("part")}#{seg.get("p")} 期望「{str(seg.get("text"))[:40]}」'
            f' ≠ 实际「{cur["text"][:40]}」')
    print(f'   → {seg_path} 与源文件不是同一次 extract 的产物，请重新 extract 并重新翻译。'
          '未产出任何文件。')
    return 1

  # ---- 写回：逐段定位，只改文本节点 ----
  para_of = {}
  for name, _root, paras in parts:
    for idx, p_el in enumerate(paras):
      para_of[(name, idx)] = p_el

  replaced = missing = skipped_para = 0
  touched = set()
  for cur in items:
    new_text = trans.get(str(cur['id']))
    if not new_text:
      missing += 1
      continue
    if _write_para(para_of[(cur['part'], cur['p'])], new_text) < 0:
      skipped_para += 1
    else:
      replaced += 1
      touched.add(cur['part'])

  total = replaced + missing + skipped_para
  assert total == len(items), f'内部错误：{total} != {len(items)}'

  removed = 0
  if task.get('clean_cjk_indent'):
    for name, root, _paras in parts:
      if name in touched:
        removed += _clean_cjk_indent(root)

  # 源文件不动：只把**改动过的 part** 换掉，其余 zip 成员字节级复制
  replaced_parts = {
    name: etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
    for name, root, _paras in parts if name in touched
  }
  _save_pkg(src, dst, replaced_parts)

  print(f'替换 {replaced} 段（涉及 {len(replaced_parts)} 个 part）；未给译文而保留原文 {missing} 段')
  if skipped_para:
    print(f'跳过 {skipped_para} 段（该段没有可安全改写的纯文字 run，原文保留）')
  if task.get('clean_cjk_indent'):
    print(f'清理东亚首行缩进属性 {removed} 处')
  print(f'已保存 {dst}（原文件未改动；图片/表格/页眉页脚/样式原样保留）')
  if missing:
    print(f'⚠️ 有 {missing} 段没拿到译文（仍是原文），补齐 {trans_path} 后重跑 apply')

  unknown = set(trans) - {str(i['id']) for i in items}
  if unknown:
    print(f'⚠️ {trans_path} 里有 {len(unknown)} 个编号不在本次 segments 中（已忽略）：'
          f'{sorted(unknown, key=lambda x: int(x) if x.isdigit() else 0)[:10]}')

  if task.get('render_check'):
    _render_check(dst)
  return 0


def _render_check(dst: str) -> None:
  """尽力把产物转 PDF 做版式自检（沙箱禁子进程 → 安静跳过，不影响产物）。"""
  try:
    import shutil
    import subprocess
    import tempfile

    soffice = shutil.which('soffice') or shutil.which('libreoffice')
    if not soffice:
      print('（未找到 LibreOffice，跳过 PDF 渲染自检）')
      return
    import os

    with tempfile.TemporaryDirectory() as td:
      subprocess.run(
        [soffice, '--headless', '--convert-to', 'pdf', '--outdir', td, dst],
        capture_output=True, timeout=300, check=False,
      )
      pdf = os.path.join(td, os.path.splitext(os.path.basename(dst))[0] + '.pdf')
      if os.path.exists(pdf) and os.path.getsize(pdf) > 0:
        shutil.copy2(pdf, os.path.basename(pdf))
        print(f'渲染自检通过：{os.path.basename(pdf)}（{os.path.getsize(pdf)} 字节）')
      else:
        print('⚠️ 渲染自检未产出 PDF（不影响 docx 产物本身）')
  except Exception as e:  # noqa: BLE001 —— 自检失败不该影响交付
    print(f'（沙箱禁子进程或渲染失败，跳过 PDF 渲染自检：{type(e).__name__}）')


def main() -> int:
  try:
    with open(TASK_FILE, encoding='utf-8') as f:
      task = json.load(f)
  except FileNotFoundError:
    print(_TASK_DOC)
    return 2
  except ValueError as e:
    print(f'{TASK_FILE} 不是合法 JSON：{e}')
    return 2

  cmd = task.get('cmd')
  try:
    if cmd == 'extract':
      return cmd_extract(task)
    if cmd == 'show':
      return cmd_show(task)
    if cmd == 'apply':
      return cmd_apply(task)
    print(f'未知 cmd={cmd!r}（只支持 extract / show / apply）\n{_TASK_DOC}')
    return 2
  except FileNotFoundError as e:
    print(f'文件不存在：{e.filename}')
    return 1
  except zipfile.BadZipFile as e:
    print(f'不是合法的 docx（zip）文件：{e}')
    return 1
  except Exception as e:  # noqa: BLE001 —— 让模型看到原始错误，便于修正
    import traceback
    traceback.print_exc()
    print(f'执行失败：{type(e).__name__}: {e}')
    return 1


if __name__ == '__main__':
  sys.exit(main())
