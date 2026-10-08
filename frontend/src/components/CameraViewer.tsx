import { useEffect, useRef, useState } from 'react';
import type { Camera, CaptureFormat } from '../types';
import { metadataText, resolutionLabels, socketLabel } from '../utils';
import { Icon } from './Icon';

export function CaptureFormatSelect({ id, className, value, disabled, rawAvailable, label, onChange }: {
  id?: string; className?: string; value: CaptureFormat; disabled: boolean; rawAvailable: boolean;
  label: string; onChange: (format: CaptureFormat) => void;
}) {
  return <select id={id} className={className} value={value} disabled={disabled} aria-label={label}
    onChange={event => onChange(event.target.value as CaptureFormat)}>
    {(['jpeg', 'png', 'tiff', 'bmp', 'raw'] as const).map(format =>
      <option key={format} value={format} disabled={format === 'raw' && !rawAvailable}>{format.toUpperCase()}</option>)}
  </select>;
}

function CameraCard({ camera, selected, busy, epoch, format, rawAvailable, onSelect, onCapture, onError }: {
  camera: Camera; selected: boolean; busy: boolean; epoch: number; format: CaptureFormat;
  rawAvailable: boolean; onSelect: () => void; onCapture: (format: CaptureFormat) => void; onError: (message: string) => void;
}) {
  const previewRef = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [cameraFormat, setCameraFormat] = useState(format);
  const frames = camera.frames || 0;
  const age = camera.last_frame_age;
  const stale = frames > 0 && (age === null || (typeof age === 'number' && age > 3));
  const healthy = frames > 0 && !stale;
  useEffect(() => { setCameraFormat(format); }, [format]);
  useEffect(() => {
    if (!failed || !healthy) return;
    const timer = setTimeout(() => { setRetry(value => value + 1); setFailed(false); }, 2500);
    return () => clearTimeout(timer);
  }, [failed, healthy]);
  const effectiveFormat = cameraFormat === 'raw' && !rawAvailable ? 'jpeg' : cameraFormat;
  return <article className={`camera-card ${selected ? 'active' : ''}`} data-socket={camera.socket}>
    <div className="camera-card-heading"><div>
      <span className={`status-dot camera-dot ${healthy ? 'live' : stale ? 'error' : ''}`} />
      <h3 className="camera-label" title={camera.socket}>{socketLabel(camera.socket)}</h3>
      <span className="camera-sensor">{camera.sensor}</span>
    </div><button type="button" className="button button-quiet button-small tune-button" disabled={busy}
      aria-label={`Adjust ${socketLabel(camera.socket)} settings`} aria-pressed={selected} onClick={onSelect}>
      {selected ? 'Selected' : 'Select'}
    </button></div>
    <div className={`preview ${frames > 0 && !failed ? 'has-frame' : ''}`} ref={previewRef}
      onClick={() => { if (!busy) onSelect(); }}>
      <img className="camera-image" alt={`${socketLabel(camera.socket)} live preview`}
        src={`/stream/${encodeURIComponent(camera.socket)}?session=${epoch}&retry=${retry}`}
        onError={() => setFailed(true)} onLoad={() => setFailed(false)} />
      <div className="preview-placeholder"><Icon name="camera" className="icon-placeholder" />
        <span className="preview-message">{failed ? 'Preview disconnected. Waiting to reconnect…' : 'Waiting for first frame'}</span>
      </div>
      <span className="preview-resolution" title="Sensor mode; the live preview is downscaled">
        {camera.resolution ? resolutionLabels[camera.resolution] : ''}
      </span>
      <button type="button" className="fullscreen-button" aria-label={`View ${socketLabel(camera.socket)} fullscreen`}
        title="View fullscreen" onClick={event => {
          event.stopPropagation();
          if (!previewRef.current?.requestFullscreen) { onError('Fullscreen is not supported by this browser.'); return; }
          void previewRef.current.requestFullscreen().catch(cause => onError(`Could not open fullscreen: ${String(cause)}`));
        }}><Icon name="fullscreen" /></button>
    </div>
    <div className="camera-telemetry">
      <span className={`frame-state ${stale ? 'stale' : ''}`}>{!frames ? 'Waiting for frames' : stale
        ? `Stale${typeof age === 'number' ? ` · ${age.toFixed(1)}s ago` : ''}` : 'Receiving frames'}</span>
      <span><strong className="camera-fps">{typeof camera.fps === 'number' ? camera.fps.toFixed(1) : '—'}</strong> fps</span>
      <span><strong className="camera-frames">{frames.toLocaleString()}</strong> frames</span>
    </div>
    <div className="camera-metadata">{metadataText(camera).map(text => <span key={text}>{text}</span>)}</div>
    <div className="camera-card-footer">
      <CaptureFormatSelect className="camera-format" label={`${socketLabel(camera.socket)} capture format`}
        value={effectiveFormat} disabled={busy} rawAvailable={rawAvailable} onChange={setCameraFormat} />
      <button type="button" className="button button-capture capture-button" disabled={busy}
        aria-label={`Capture image from ${socketLabel(camera.socket)}`} onClick={() => onCapture(effectiveFormat)}>Capture image <Icon name="download" /></button>
    </div>
  </article>;
}

export function CameraViewer({ streams, activeSocket, busy, epoch, format, rawAvailable, drafts, onSelect, onSetup, onCapture, onError }: {
  streams: Camera[]; activeSocket: string; busy: boolean; epoch: number; format: CaptureFormat; rawAvailable: boolean;
  drafts: string[]; onSelect: (socket: string) => void; onSetup: () => void;
  onCapture: (socket: string, format: CaptureFormat) => void; onError: (message: string) => void;
}) {
  const [selectedView, setSelectedView] = useState(false);
  const [details, setDetails] = useState(false);
  return <section className={`viewer ${details ? 'show-frame-details' : ''}`} aria-labelledby="live-heading">
    <div className="viewer-toolbar">
      <div className="viewer-title"><h1 id="live-heading">Live view</h1></div>
      <div id="camera-tabs" className="camera-tabs" role="group" aria-label="Select camera">
        {!streams.length && <span className="empty-tab-label">No active cameras</span>}
        {streams.map(camera => <button key={camera.socket} type="button" data-camera-socket={camera.socket}
          disabled={busy} aria-pressed={activeSocket === camera.socket}
          title={drafts.includes(camera.socket) ? 'This camera has unapplied changes' : `Select ${socketLabel(camera.socket)}`}
          onClick={() => onSelect(camera.socket)}>{socketLabel(camera.socket)}
          <span className="draft-dot" hidden={!drafts.includes(camera.socket)} aria-hidden="true" />
        </button>)}
      </div>
      <div className="viewer-actions">
        <div className="view-switch" role="group" aria-label="Preview layout">
          <button type="button" id="view-all" aria-pressed={!selectedView} onClick={() => setSelectedView(false)}>All</button>
          <button type="button" id="view-selected" aria-pressed={selectedView} onClick={() => setSelectedView(true)}>Selected</button>
        </div>
        <button type="button" id="toggle-details" className="details-button" aria-pressed={details}
          title="Show frame metadata" onClick={() => setDetails(value => !value)}>
          <Icon name="settings" /><span>Details</span>
        </button>
      </div>
    </div>
    <div className={`camera-gallery ${streams.length <= 1 ? 'single' : ''} ${selectedView ? 'selected-view' : ''}`}
      id="camera-gallery" aria-label="Live camera previews" data-count={streams.length}>
      {!streams.length && <div className="workspace-empty" id="workspace-empty">
        <div className="lens-illustration" aria-hidden="true"><Icon name="camera" className="icon-placeholder" /></div><h2>Ready when you are.</h2>
        <p>Choose up to three cameras in Setup, then start the streams.</p>
        <button type="button" className="button button-primary" data-open-setup onClick={onSetup}>Configure cameras</button>
      </div>}
      {streams.map(camera => <CameraCard key={`${camera.socket}:${epoch}`} camera={camera}
        selected={camera.socket === activeSocket} busy={busy} epoch={epoch} format={format} rawAvailable={rawAvailable}
        onSelect={() => onSelect(camera.socket)} onCapture={value => onCapture(camera.socket, value)} onError={onError} />)}
    </div>
  </section>;
}
