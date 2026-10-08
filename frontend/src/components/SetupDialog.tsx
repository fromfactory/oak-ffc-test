import type { ReactNode } from 'react';
import type { Camera, CameraConfig } from '../types';
import { Dialog } from './Dialog';

export interface SetupDialogProps {
  open: boolean;
  onClose: () => void;
  cameras: Camera[];
  configs: Record<string, CameraConfig>;
  selected: ReadonlySet<string>;
  rawEnabled: boolean;
  busy: boolean;
  running: boolean;
  streamAction: 'start' | 'stop' | null;
  deviceId?: string;
  warnings: string[];
  hasResolutionConflict: boolean;
  onSelect: (socket: string, selected: boolean) => void;
  onConfigChange: (socket: string, patch: Partial<CameraConfig>) => void;
  onPreset: (count: number) => void;
  onRawChange: (enabled: boolean) => void;
  onMatchResolutions: () => void;
  onScan: () => void;
  onStart: () => void;
  onStop: () => void;
  feedback?: ReactNode;
}

export function SetupDialog({
  open, onClose, cameras, configs, selected, rawEnabled, busy, running,
  streamAction, deviceId, warnings, hasResolutionConflict, onSelect,
  onConfigChange, onPreset, onRawChange, onMatchResolutions, onScan,
  onStart, onStop, feedback,
}: SetupDialogProps) {
  const locked = busy || running;
  const showStart = streamAction ? streamAction === 'start' : !running;
  const selectedModes = new Set(cameras.filter((camera) => selected.has(camera.socket))
    .map((camera) => configs[camera.socket]?.resolution ?? camera.resolution ?? '1080p'));

  return (
    <Dialog id="setup-dialog" labelledBy="setup-heading" open={open} onClose={onClose}
      returnFocusId="open-setup" feedback={feedback}>
      <div className="dialog-heading">
        <div>
          <span className="eyebrow">SETUP</span>
          <h2 id="setup-heading">Camera configuration <span className="subtle" id="selection-count">{selected.size} selected</span></h2>
        </div>
        <button type="button" className="icon-button" id="close-setup" data-close-dialog
          aria-label="Close camera setup" onClick={onClose}>×</button>
      </div>
      <div className="dialog-scroll">
        <div className="device-details">
          <span id="device-id">{deviceId ? `ID ${deviceId}` : 'Connect the OAK device over USB'}</span>
          <span id="camera-count">{cameras.length} detected</span>
          <a href="/api/report" className="diagnostics-link" download>Download diagnostics ↗</a>
        </div>
        <div className="configuration-toolbar">
          <p>Select up to three cameras.</p>
          <button type="button" className="button button-quiet button-small" id="scan-button"
            disabled={locked} onClick={onScan}>↻ Scan cameras</button>
        </div>
        <div className="configuration-tools">
          <div className="configuration-action-row">
            <div className="presets" role="group" aria-label="Camera count presets">
              <span>QUICK SELECT</span>
              {(['One', 'Two', 'Three'] as const).map((label, index) => {
                const count = index + 1;
                return <button type="button" key={label} data-preset={count}
                  className={selected.size === count ? 'active' : ''}
                  aria-pressed={selected.size === count} disabled={locked || cameras.length < count}
                  onClick={() => onPreset(count)}>{label}</button>;
              })}
            </div>
            <button type="button" id="match-resolutions" className="button button-quiet button-small"
              aria-describedby="match-resolution-help" disabled={locked || selectedModes.size < 2}
              onClick={onMatchResolutions}>Match resolutions</button>
          </div>
          <span className="field-help" id="match-resolution-help">Matches all selected cameras to the highest selected mode.</span>
        </div>
        <div className="camera-configs" id="camera-configs">
          {cameras.length === 0 && <div className="discovery-empty">
            <span className="empty-icon" aria-hidden="true">◎</span>
            <p>No cameras discovered. Check power and USB, then scan again.</p>
          </div>}
          {cameras.map((camera) => {
            const isSelected = selected.has(camera.socket);
            const label = camera.socket === 'CAM_AA' ? 'CAM_A' : camera.socket;
            const config = configs[camera.socket] ?? {
              resolution: camera.resolution ?? '1080p', fps: camera.requested_fps ?? 10,
            };
            return (
              <div key={camera.socket} data-socket={camera.socket}
                className={`camera-config min-w-0 ${isSelected ? 'selected' : ''}`}>
                <div className="config-camera-heading">
                  <label className="camera-checkbox">
                    <input type="checkbox" className="camera-select" aria-label={`Select ${label}`}
                      checked={isSelected} disabled={locked}
                      onChange={(event) => onSelect(camera.socket, event.currentTarget.checked)} />
                    <span className="config-icon" aria-hidden="true">◉</span>
                    <span>
                      <strong className="config-label" title={label}>{label}</strong>
                      <small className="config-sensor">{camera.sensor || 'Unknown sensor'} · {camera.autofocus === true
                        ? 'Autofocus' : camera.autofocus === false ? 'Fixed focus' : 'Focus unknown'}</small>
                    </span>
                  </label>
                  <span className="lane-badge">{['CAM_A', 'CAM_AA', 'CAM_D'].includes(camera.socket) ? '4 LANE' : '2 LANE'}</span>
                </div>
                <div className="config-fields">
                  <label>Resolution
                    <select className="resolution-select" aria-label={`${label} resolution`}
                      value={config.resolution} disabled={locked || !isSelected}
                      onChange={(event) => onConfigChange(camera.socket, {
                        resolution: event.currentTarget.value as CameraConfig['resolution'],
                      })}>
                      <option value="1080p">1080p · 1920 × 1080</option>
                      <option value="4k">4K · 3840 × 2160</option>
                      <option value="12mp">12 MP · 4032 × 3040</option>
                    </select>
                  </label>
                  <label className="fps-field">Frame rate
                    <div className="number-suffix">
                      <input className="fps-input" type="number" min="2" max="30" step="1"
                        value={Number.isNaN(config.fps) ? '' : config.fps} aria-label={`${label} frame rate`}
                        disabled={locked || !isSelected} onChange={(event) => onConfigChange(camera.socket,
                          { fps: event.currentTarget.valueAsNumber })} />
                      <span>fps</span>
                    </div>
                  </label>
                </div>
              </div>
            );
          })}
        </div>
        <label className="raw-option">
          <input type="checkbox" id="raw-enabled" checked={rawEnabled} disabled={locked}
            onChange={(event) => onRawChange(event.currentTarget.checked)} />
          <span><strong>Enable RAW capture</strong><small>Uses extra device memory; RAW data transfers when you capture.</small></span>
        </label>
        <p className="setup-footnote" id="setup-footnote">Stop streams to change this configuration. Power off the OAK device before connecting or moving FFC cables.</p>
        <div className="warnings" id="warnings" role="status" hidden={warnings.length === 0}>
          {warnings.map((warning, index) => <p key={`${index}:${warning}`}>{warning}</p>)}
        </div>
      </div>
      <div className="dialog-footer">
        <span id="setup-state-help">{running ? 'Stop streams to edit the configuration.' : 'Changes take effect when streams start.'}</span>
        <div>
          <button type="button" className="button button-quiet" data-close-dialog onClick={onClose}>Close</button>
          <button type="button" className="button button-stop" id="setup-stop-button" hidden={showStart}
            disabled={busy || !running} onClick={onStop}>{streamAction === 'stop' ? 'Stopping…' : 'Stop streams'}</button>
          <button type="button" className="button button-primary" id="setup-start-button" hidden={!showStart}
            disabled={locked || selected.size === 0 || hasResolutionConflict} onClick={onStart}>
            {streamAction === 'start' ? 'Starting…' : 'Start streams'}
          </button>
        </div>
      </div>
    </Dialog>
  );
}
