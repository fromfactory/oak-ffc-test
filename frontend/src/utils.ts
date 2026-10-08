import type { Camera, Resolution } from './types';

export const resolutionLabels: Record<Resolution, string> = {
  '1080p': '1920 × 1080', '4k': '3840 × 2160', '12mp': '12 MP sensor mode',
};
export const resolutionOrder: Resolution[] = ['1080p', '4k', '12mp'];
export const resolutionWarning = 'CAM_A and CAM_D need matching sensor resolutions with this DepthAI version. Choose the same mode before starting.';
export const socketLabel = (socket: string) => socket === 'CAM_AA' ? 'CAM_A' : socket;
export function sortedCameras(cameras: Camera[]): Camera[] {
  const order: Record<string, number> = { CAM_A: 0, CAM_AA: 0, CAM_D: 1, CAM_B: 2, CAM_C: 3 };
  return [...cameras].sort((a, b) => (order[a.socket] ?? 9) - (order[b.socket] ?? 9));
}

export function metadataText(camera: Camera): string[] {
  const metadata = camera.metadata || {};
  const parts: string[] = [];
  const exposure = metadata.exposure_us ?? metadata.exposure_time_us;
  if (typeof exposure === 'number') parts.push(`${(exposure / 1000).toLocaleString(undefined, { maximumFractionDigits: 2 })} ms`);
  if (typeof metadata.iso === 'number') parts.push(`ISO ${metadata.iso}`);
  const wb = metadata.white_balance_kelvin ?? metadata.color_temperature;
  if (typeof wb === 'number') parts.push(`${wb} K`);
  const focus = metadata.focus ?? metadata.lens_position;
  if (typeof focus === 'number') parts.push(`Lens ${focus}`);
  return parts;
}
