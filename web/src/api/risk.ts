import client from './client';
import type {
  CheckResult,
  BatchCheckResult,
  FireSafetyRequest,
  FireSafetyResult,
  RiskHistoryItem,
  RiskHistoryDetail,
  FireSafetyHistoryItem,
  FireSafetyHistoryDetail,
} from '../types';

// ── 下载辅助：直接 form submit，token 放 URL query param ──
// 原生 form 提交不会被 Chrome 拦截，后端返回 Content-Disposition: attachment 时页面不跳转

function submitDownload(path: string, fields: Record<string, string>) {
  const token = sessionStorage.getItem('access_token');
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = `/api/v1${path}${token ? `?token=${encodeURIComponent(token)}` : ''}`;

  for (const [name, value] of Object.entries(fields)) {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = name;
    input.value = value;
    form.appendChild(input);
  }

  document.body.appendChild(form);
  form.submit();
  document.body.removeChild(form);
}

export async function checkImage(
  file: File,
  description?: string,
): Promise<CheckResult> {
  const form = new FormData();
  form.append('file', file);
  const params = description ? `?description=${encodeURIComponent(description)}` : '';
  const { data } = await client.post(`/risk/check${params}`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 60000,
  });
  return data;
}

export async function batchCheckImages(
  files: File[],
  description?: string,
): Promise<BatchCheckResult> {
  const form = new FormData();
  files.forEach((f) => form.append('files', f));
  const params = description ? `?description=${encodeURIComponent(description)}` : '';
  const { data } = await client.post(`/risk/batch-check${params}`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 120000,
  });
  return data;
}

export async function fireSafetyRecommend(
  request: FireSafetyRequest,
): Promise<FireSafetyResult> {
  const { data } = await client.post('/risk/fire-safety', request, { timeout: 130000 });
  return data;
}

export function downloadFireSafetyReport(result: FireSafetyResult) {
  submitDownload('/risk/fire-safety/report', {
    data: JSON.stringify(result),
  });
}

export async function generateReport(
  results: CheckResult[],
  title?: string,
  format: string = 'json',
): Promise<{ ok: boolean; markdown?: string }> {
  const checkIds = results.map((r) => r.check_id);
  const { data } = await client.post('/risk/report', {
    check_ids: checkIds,
    results,
    title,
    format,
  });
  return data;
}

export function downloadReportDocx(
  results: CheckResult[],
  title: string = '安全隐患检测报告',
) {
  const checkIds = results.map((r) => r.check_id);
  submitDownload('/risk/report', {
    data: JSON.stringify({ check_ids: checkIds, results, title, format: 'docx' }),
  });
}

export async function getRiskHistory(
  limit: number = 10,
  offset: number = 0,
): Promise<RiskHistoryItem[]> {
  const { data } = await client.get('/risk/history', { params: { limit, offset } });
  return data.records ?? [];
}

export async function getRiskCheckDetail(checkId: string): Promise<RiskHistoryDetail> {
  const { data } = await client.get(`/risk/history/${checkId}`);
  return data.detail;
}

export async function deleteRiskCheck(checkId: string): Promise<boolean> {
  const { data } = await client.delete(`/risk/history/${checkId}`);
  return data.ok === true;
}

export async function getFireSafetyHistory(
  limit: number = 10,
  offset: number = 0,
): Promise<FireSafetyHistoryItem[]> {
  const { data } = await client.get('/risk/fire-safety/history', { params: { limit, offset } });
  return data.records ?? [];
}

export async function getFireSafetyDetail(
  recordId: string,
): Promise<FireSafetyHistoryDetail> {
  const { data } = await client.get(`/risk/fire-safety/history/${recordId}`);
  return data.detail;
}

export async function deleteFireSafetyRecord(recordId: string): Promise<boolean> {
  const { data } = await client.delete(`/risk/fire-safety/history/${recordId}`);
  return data.ok === true;
}
