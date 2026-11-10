"""红外质量检测自检（纯 assert、不连库、不调服务）。

覆盖：标签→判定（含 fail-closed）、分批不丢数、文件名映射（重复名消歧 +
按位置回退）、SSE 解析、结果表定位、列声明与行键一致、计数。
跑法：`python -m service.infrared_quality.tests.test_infrared_quality`
"""

import os
import tempfile
from datetime import datetime, timezone

from pymongo import UpdateOne

from config.settings import settings
from service.infrared_quality import cache, columns, predict, verdict, workflow


def test_verdict_pass():
  assert verdict.classify('sharp') == (verdict.PASS, '')
  assert verdict.classify(' Sharp ') == (verdict.PASS, '')


def test_verdict_review():
  assert verdict.classify('borderline') == (verdict.REVIEW, '')


def test_verdict_blurry():
  for label in ('defocused', 'Defocused'):
    assert verdict.classify(label) == (verdict.BAD, ''), label


def test_verdict_unknown_is_failed():
  # fail-closed：认不出的标签一律检测失败，绝不默认合格
  for label in ('uncertain', '无法确定', '未知', '', '  '):
    judgement, _ = verdict.classify(label)
    assert judgement == verdict.FAILED, label


def test_verdict_old_chinese_labels_now_failed():
  # 接口改版后只认新英文标签：旧缓存里的中文标签下次重检判检测失败（用户口径）
  for label in ('清晰', '临界状态', '模糊或失焦'):
    assert verdict.classify(label)[0] == verdict.FAILED, label


def test_verdict_error_wins():
  assert verdict.classify('sharp', 'boom')[0] == verdict.FAILED


def test_photo_devices_filter():
  # 没图片的是设备台账、未参与本次巡检：不出行、也不算检测失败
  devices = [
    {'device_id': 'a', 'picture': 'http://x/a.jpg'},
    {'device_id': 'b', 'picture': ''},
    {'device_id': 'c', 'picture': None},
  ]
  assert [d['device_id'] for d in workflow.photo_devices(devices)] == ['a']
  assert workflow.photo_devices([]) == []


def test_backfill_lists_cache_hits_missing_locally():
  """缓存命中的设备本地缺图 → 检测时要顺带补图（老任务重跑一次即可补齐证据）。"""
  orig = settings.INFRARED_QUALITY_IMAGE_DIR
  with tempfile.TemporaryDirectory() as tmp:
    settings.INFRARED_QUALITY_IMAGE_DIR = tmp
    try:
      devs = [{'device_id': 'd1', 'picture': 'http://x/a.jpg'},
              {'device_id': 'd2', 'picture': 'http://x/b.png'}]
      assert [d['device_id'] for d in workflow._missing_local('t1', devs)] == ['d1', 'd2']
      os.makedirs(os.path.join(tmp, 't1'))
      open(os.path.join(tmp, 't1', 'd1.jpg'), 'w').close()
      assert [d['device_id'] for d in workflow._missing_local('t1', devs)] == ['d2']
    finally:
      settings.INFRARED_QUALITY_IMAGE_DIR = orig


def test_local_file_reused_not_downloaded():
  """本地已有同名文件 → image_file 直接给出路径，下载阶段复用、不重复下载。"""
  orig = settings.INFRARED_QUALITY_IMAGE_DIR
  with tempfile.TemporaryDirectory() as tmp:
    settings.INFRARED_QUALITY_IMAGE_DIR = tmp
    try:
      assert workflow.image_file('t1', 'd1.jpg') == ''
      os.makedirs(os.path.join(tmp, 't1'))
      dest = os.path.join(tmp, 't1', 'd1.jpg')
      open(dest, 'w').close()
      assert workflow.image_file('t1', 'd1.jpg') == dest
      assert workflow._missing_local(
        't1', [{'device_id': 'd1', 'picture': 'http://x/a.jpg'}]) == []
      # 文件名的路径穿越一律拒绝
      assert workflow.image_file('t1', '../d1.jpg') == ''
    finally:
      settings.INFRARED_QUALITY_IMAGE_DIR = orig


def test_cache_ops_are_pymongo_operations():
  """bulk_write 只认 UpdateOne：mongosh 风格的 dict 会报 not a valid request（缓存曾因此从未写入）。"""
  now = datetime.now(timezone.utc)
  ops = cache._bulk_ops('t1', [
    {'device_id': 'd1', 'label': 'sharp', 'score': '97.9%'},
    {'device_id': '', 'label': 'skip'},
  ], now)
  assert len(ops) == 1 and isinstance(ops[0], UpdateOne)
  assert ops[0]._filter == {'_id': 't1:d1'}
  assert ops[0]._upsert is True
  assert ops[0]._doc == {'$set': {'label': 'sharp', 'score': '97.9%', 'at': now}}


def test_chunks_no_loss():
  parts = predict.chunks(list(range(47)), 20)
  assert [len(p) for p in parts] == [20, 20, 7]
  assert sum(len(p) for p in parts) == 47
  assert predict.chunks([], 20) == []


def test_name_map_unique():
  paths = ['/tmp/a/001.jpg', '/tmp/b/002.jpg']
  rows = [['002.jpg', '清晰', 99, ''], ['001.jpg', '临界状态', 60, '']]
  aligned = predict.map_rows(paths, rows)
  assert aligned[0][1] == '临界状态'
  assert aligned[1][1] == '清晰'


def test_name_map_duplicate_falls_back_to_position():
  paths = ['/tmp/x/001.jpg', '/tmp/y/001.jpg']
  rows = [['001.jpg', '清晰', 99, ''], ['001.jpg', '模糊或失焦', 20, '']]
  aligned = predict.map_rows(paths, rows)
  assert aligned[0][1] == '清晰'
  assert aligned[1][1] == '模糊或失焦'


def test_name_map_missing_row():
  aligned = predict.map_rows(['/tmp/a/001.jpg', '/tmp/a/002.jpg'],
                             [['001.jpg', '清晰', 99, '']])
  assert aligned[1] is None


def test_parse_sse_multiline():
  lines = ['event: generating', 'data: null', '',
           'event: complete', 'data: [1,', 'data: 2]', '']
  events = predict.parse_sse(lines)
  assert events[0] == ('generating', 'null')
  assert events[1][0] == 'complete'
  assert events[1][1] == '[1,\n2]'


HEADERS = ['文件名', 'Prediction', 'sharp', 'borderline', 'defocused', '错误']


def test_result_table_found_by_shape():
  table = {'headers': HEADERS,
           'data': [['001.jpg', 'sharp', '97.5%', '2.0%', '0.5%', '']]}
  payload = ['some text', 0, table]
  headers, rows = predict._result_table(payload)
  assert headers == HEADERS
  assert rows and rows[0][1] == 'sharp'


def test_result_table_missing():
  assert predict._result_table(['text', 0]) == ([], [])


def test_parse_new_format():
  rows = [
    ['001.jpg', 'sharp', '97.9%', '2.1%', '0.1%', ''],
    ['002.jpg', 'borderline', '0.9%', '87.4%', '11.7%', ''],
    ['003.jpg', '', '', '', '', '模型超时'],
    ['004.jpg', 'uncertain', '33.3%', '33.3%', '33.4%', ''],
  ]
  paths = ['/tmp/a/001.jpg', '/tmp/a/002.jpg', '/tmp/a/003.jpg', '/tmp/a/004.jpg']
  out = predict.parse_rows(HEADERS, rows, paths)
  # 模型分数取**预测类别那一列**的原值（带 % 的字符串）
  assert out['/tmp/a/001.jpg'] == {'label': 'sharp', 'score': '97.9%', 'error': ''}
  assert out['/tmp/a/002.jpg'] == {'label': 'borderline', 'score': '87.4%', 'error': ''}
  assert out['/tmp/a/003.jpg'] == {'label': '', 'score': None, 'error': '模型超时'}
  assert out['/tmp/a/004.jpg'] == {'label': 'uncertain', 'score': None, 'error': ''}


def test_parse_rows_without_headers():
  # 无表头时回退旧位置（文件名/预测/错误 = 0 / 1 / 3）
  rows = [['001.jpg', 'sharp', '97.9%', '']]
  out = predict.parse_rows([], rows, ['/tmp/001.jpg'])
  assert out['/tmp/001.jpg'] == {'label': 'sharp', 'score': None, 'error': ''}


def test_columns_match_row_keys():
  row = columns.build_row(
    seq=1, company='润泽', dept='A1', task_id='t1', inspector='张三',
    type_name='红外检测PDU', name='1-2APDU', num='1-2A', code='C1',
    site='M4-301', detection_time=1765416129308, max_temp='34', upper='40',
    verdict='合格', score=97.5, note='', picture='http://x/y.jpg',
  )
  assert list(row.keys()) == columns.COLUMNS
  assert row['检测时间'] == '2025-12-11 09:22:09'
  assert row['设备名称'] == '1-2APDU'


def test_columns_empty_time():
  assert columns.fmt_time(0) == ''
  assert columns.fmt_time(None) == ''
  assert columns.fmt_time('bad') == ''


def test_counts_of():
  rows = [{'判定': '合格'}, {'判定': '合格'}, {'判定': '待复核'},
          {'判定': '不合格'}, {'判定': '检测失败'}]
  assert columns.counts_of(rows) == {'合格': 2, '待复核': 1, '不合格': 1, '检测失败': 1}


def test_ext_of():
  assert predict.name_key(r'C:\tmp\a.jpg') == 'a.jpg'
  assert predict.name_key('http://x/y/z.png?token=1') is not None


def test_file_data_shape():
  """Gradio 6 要求 FileData 对象，缺 meta 会被 pydantic 拒（实测 6.31.0）。"""
  fd = predict.file_data('/tmp/local/a.jpg', '/tmp/gradio/abc/a.jpg')
  assert fd['path'] == '/tmp/gradio/abc/a.jpg'
  assert fd['orig_name'] == 'a.jpg'
  assert fd['_type'] == 'gradio.FileData'
  assert fd['meta'] == {'_type': 'gradio.FileData'}


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
