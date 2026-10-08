import type { CameraControls, CameraStatus, Capture, CaptureFormat, CameraConfig, CaptureArchive, CaptureCollection, CaptureDeleteResult, CaptureSelection } from './types';

interface ErrorBody {
  error?: string;
  captures?: Capture[];
  deleted?: string[];
  capture_count?: number;
}

export class ApiError extends Error {
  constructor(message: string, readonly body: ErrorBody) { super(message); }
}

async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, body === undefined
      ? { cache: 'no-store', signal }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new Error('Cannot reach OAK FFC TEST. Check that the application is running and the host computer is reachable.');
  }
  let data: T & ErrorBody;
  try { data = await response.json(); }
  catch { throw new Error(`The server returned an unreadable response (HTTP ${response.status}).`); }
  if (!response.ok) throw new ApiError(data.error || `The request failed (HTTP ${response.status}).`, data);
  return data;
}

export const api = {
  status: (signal?: AbortSignal) => request<CameraStatus>('/api/status', undefined, signal),
  scan: () => request<CameraStatus>('/api/scan', {}),
  start: (cameras: (CameraConfig & { socket: string })[], rawEnabled: boolean) =>
    request<CameraStatus>('/api/start', { cameras, raw_enabled: rawEnabled }),
  stop: () => request<CameraStatus>('/api/stop', {}),
  controls: (socket: string, changes: Partial<CameraControls>) =>
    request<Partial<CameraControls>>(`/api/controls/${encodeURIComponent(socket)}`, changes),
  capture: (sockets: string[], format: CaptureFormat) =>
    request<{ captures: Capture[] }>('/api/capture', { sockets, format }),
  captures: (signal?: AbortSignal) => request<CaptureCollection>('/api/captures', undefined, signal),
  deleteCaptures: (selection: CaptureSelection) => request<CaptureDeleteResult>('/api/captures/delete', selection),
  prepareCaptureDownload: (selection: CaptureSelection) => request<CaptureArchive>('/api/captures/download?prepare=1', selection),
};
