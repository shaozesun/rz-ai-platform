import echartsSrc from 'echarts/dist/echarts.min.js?raw';
import client from '../api/client';

const HTML_EXT = ['html', 'htm'];

function extOf(name: string): string {
  const i = name.lastIndexOf('.');
  return i >= 0 ? name.slice(i + 1).toLowerCase() : '';
}

/** 下载产物（带 token）；.html 产物前置内联 echarts，避免下载后图表空白 */
export async function downloadArtifact(rel: string, name: string): Promise<void> {
  const resp = await client.get(rel, { responseType: 'blob' });
  let blob: Blob = resp.data;
  if (HTML_EXT.includes(extOf(name))) {
    const html = await resp.data.text();
    blob = new Blob([`<script>${echartsSrc}</script>\n${html}`], { type: 'text/html' });
  }
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objectUrl;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}
