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
  const formEl = document.createElement('form');
  formEl.method = 'POST';
  formEl.action = '/api/v1/risk/fire-safety/report';
  const input = document.createElement('input');
  input.type = 'hidden';
  input.name = 'data';
  input.value = JSON.stringify(result);
  formEl.appendChild(input);
  document.body.appendChild(formEl);
  formEl.submit();
  document.body.removeChild(formEl);
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

export async function downloadReportDocx(
  results: CheckResult[],
  title: string = '安全隐患检测报告',
): Promise<void> {
  const checkIds = results.map((r) => r.check_id);
  const token = sessionStorage.getItem('access_token');
  const response = await fetch('/api/v1/risk/report', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({
      check_ids: checkIds,
      results,
      title,
      format: 'docx',
    }),
  });
  if (!response.ok) {
    throw new Error(`下载失败: ${response.status}`);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = '安全隐患检测报告.docx';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, 200);
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
