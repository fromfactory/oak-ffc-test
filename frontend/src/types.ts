export type Resolution = '1080p' | '4k' | '12mp';
export type CaptureFormat = 'jpeg' | 'png' | 'tiff' | 'bmp' | 'raw';

export interface CameraConfig {
  resolution: Resolution;
  fps: number;
}

export interface CameraControls {
  exposure_mode: 'auto' | 'manual';
  exposure_us: number;
  iso: number;
  exposure_lock: boolean;
  auto_exposure_limit_us: number;
  white_balance_mode: 'auto' | 'manual' | 'incandescent' | 'fluorescent' | 'warm_fluorescent' | 'daylight' | 'cloudy' | 'twilight' | 'shade';
  white_balance_kelvin: number;
  white_balance_lock: boolean;
  focus_mode: 'continuous' | 'auto' | 'manual';
  focus: number;
  exposure_compensation: number;
  brightness: number;
  contrast: number;
  saturation: number;
  sharpness: number;
  luma_denoise: number;
  chroma_denoise: number;
  anti_banding: 'auto' | 'off' | '50hz' | '60hz';
  effect_mode: 'off' | 'mono' | 'negative' | 'sepia';
}

export interface Camera {
  socket: string;
  sensor?: string;
  label?: string;
  autofocus?: boolean;
  active?: boolean;
  resolution?: Resolution;
  requested_fps?: number;
  frames?: number;
  fps?: number;
  last_frame_age?: number | null;
  controls?: Partial<CameraControls>;
  metadata?: Record<string, number | string | boolean | null>;
}

export interface Capture {
  id: string;
  socket: string;
  format: CaptureFormat;
  created_at: string;
  files: { name: string; size: number; url: string }[];
  metadata?: Record<string, unknown>;
}

export type CaptureSelection = { ids: string[] } | { all: true };
export interface CaptureCollection { captures: Capture[] }
export interface CaptureDeleteResult extends CaptureCollection {
  deleted: string[];
  capture_count: number;
}
export interface CaptureArchive {
  download_url: string;
  filename: string;
  count: number;
}

export interface CameraStatus {
  running: boolean;
  demo: boolean;
  raw_enabled: boolean;
  cameras: Camera[];
  active_sockets?: string[];
  device?: { id?: string; name?: string; usb_speed?: string } | null;
  warnings?: string[];
  error?: string | null;
  captures: Capture[];
  capture_count?: number;
  capture_directory?: string;
  version?: string;
}
