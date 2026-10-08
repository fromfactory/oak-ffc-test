import { useEffect, useRef, useState } from 'react';
import { CameraViewer, CaptureFormatSelect } from './components/CameraViewer';
import { CapturesDialog } from './components/CapturesDialog';
import { ControlsPanel } from './components/ControlsPanel';
import { SetupDialog } from './components/SetupDialog';
import { useCameraWorkspace } from './hooks/useCameraWorkspace';
import type { CaptureFormat } from './types';
import { socketLabel } from './utils';

export default function App() {
  const workspace = useCameraWorkspace();
  const [dialog, setDialog] = useState<'setup' | 'captures' | null>(null);
  const [format, setFormat] = useState<CaptureFormat>('jpeg');
  const [drafts, setDrafts] = useState<string[]>([]);
  const initialSetupOpened = useRef(false);
  const running = Boolean(workspace.status?.running);
  const rawAvailable = running && Boolean(workspace.status?.raw_enabled);
  const effectiveFormat = format === 'raw' && !rawAvailable ? 'jpeg' : format;
  const device = workspace.status?.device;
  const usbLabels: Record<string, string> = {
    SUPER: 'USB 3 · SuperSpeed', SUPER_PLUS: 'USB 3 · SuperSpeed+', HIGH: 'USB 2 · High Speed',
    FULL: 'USB · Full Speed', LOW: 'USB · Low Speed', DEMO: 'Simulated',
  };
  const showStart = workspace.streamAction ? workspace.streamAction === 'start' : !running;
  const canStart = !workspace.busy && !running && Boolean(workspace.selected.size) && !workspace.hasResolutionConflict;
  const actionableWarnings = workspace.warnings.filter(warning => !warning.toLowerCase().startsWith('demo mode:'));

  useEffect(() => {
    if (workspace.initialized && !initialSetupOpened.current) {
      initialSetupOpened.current = true;
      if (!running) setDialog('setup');
    }
  }, [workspace.initialized, running]);
  useEffect(() => {
    document.body.classList.toggle('busy', workspace.busy);
    return () => document.body.classList.remove('busy');
  }, [workspace.busy]);
  useEffect(() => { if (!rawAvailable) setFormat(value => value === 'raw' ? 'jpeg' : value); }, [rawAvailable]);

  // The workspace records request failures; event handlers consume rejections.
  const invoke = (work: () => Promise<void>) => { void work().catch(() => {}); };
  const start = () => invoke(async () => { await workspace.start(); setDialog(null); });
  const feedback = <div className="feedback-stack" aria-live="polite">
    <div className="notice" id="notice" role="status" hidden={!workspace.notice}>{workspace.notice}</div>
    <div className="alert alert-error" id="error-banner" role="alert" hidden={!workspace.error}>
      <span id="error-text">{workspace.error}</span><button type="button" className="icon-button" id="dismiss-error"
        aria-label="Dismiss error" onClick={workspace.dismissError}>×</button>
    </div>
  </div>;

  return <div className="app-shell">
    <header className="masthead">
      <a className="brand" href="/" aria-label="OAK FFC TEST home"><span className="brand-symbol" aria-hidden="true"><i /><i /><i /><i /></span><span>OAK <strong>FFC TEST</strong></span></a>
      <span className="hardware-label">Camera workspace</span>
      <div className={`connection-pill ${workspace.connected ? workspace.cameras.length ? 'live' : '' : workspace.initialized ? 'error' : ''}`} id="connection-pill">
        <span className="status-dot" /><span id="connection-text">{!workspace.connected ? workspace.initialized ? 'Server unavailable' : 'Connecting'
          : workspace.status?.demo ? 'Demo device' : device?.id || workspace.cameras.length ? 'Device connected' : 'No device'}</span>
      </div>
      <nav className="header-actions" aria-label="Workspace actions">
        <button type="button" className="button button-header" id="open-setup" aria-haspopup="dialog" onClick={() => setDialog('setup')}>
          <svg className="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 17h16M8 4v6m8 4v6" /></svg>Setup
        </button>
        <button type="button" className="button button-header" id="open-captures" aria-haspopup="dialog" onClick={() => setDialog('captures')}>
          <svg className="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h7l2-3h9v16H3z" /></svg>Captures <span className="count-badge" id="capture-count">{workspace.status?.captures.length || 0}</span>
        </button>
        <button type="button" className="button button-stop" id="stop-button" hidden={showStart} disabled={workspace.busy || !running}
          onClick={() => invoke(workspace.stop)}><span aria-hidden="true">■</span><span>{workspace.streamAction === 'stop' ? 'Stopping…' : 'Stop'}</span></button>
        <button type="button" className="button button-primary" id="start-button" hidden={!showStart} disabled={!canStart}
          onClick={start}><span aria-hidden="true">▶</span><span>{workspace.streamAction === 'start' ? 'Starting…' : 'Start'}</span></button>
      </nav>
    </header>
    <div className="status-bar" aria-label="Device status">
      <span className="pipeline-value"><span className={`status-dot ${running ? 'live' : ''}`} id="pipeline-dot" /><strong id="pipeline-state">{running ? 'Running' : 'Stopped'}</strong></span>
      <span id="device-name">{device?.name || (device?.id ? 'OAK device' : 'No device connected')}</span>
      <span className="status-divider" aria-hidden="true">/</span><span id="usb-speed">{device?.usb_speed ? usbLabels[device.usb_speed] || device.usb_speed : '—'}</span>
      <span className="status-divider" aria-hidden="true">/</span><span id="stream-count">{running ? `${workspace.streams.length} ${workspace.streams.length === 1 ? 'stream' : 'streams'} active` : 'Streams stopped'}</span>
      <span className="status-extra" id="pipeline-detail">{running ? workspace.status?.raw_enabled ? 'Preview + RAW enabled' : 'Live preview enabled' : 'Configure cameras to begin'}</span>
      <button type="button" id="workspace-warning" className="warning-link" hidden={!actionableWarnings.length} aria-haspopup="dialog"
        title={actionableWarnings.join('\n')} onClick={() => setDialog('setup')}>
        {workspace.hasResolutionConflict ? 'Match resolutions in Setup' : `Setup: ${actionableWarnings.length} ${actionableWarnings.length === 1 ? 'notice' : 'notices'}`}
      </button>
    </div>
    <div className="demo-banner" id="demo-banner" hidden={!workspace.status?.demo}><strong>DEMO</strong><span>Simulated cameras and images</span></div>
    <main className="workspace" aria-label="Camera workspace">
      <CameraViewer streams={workspace.streams} activeSocket={workspace.activeSocket} busy={workspace.busy}
        epoch={workspace.streamEpoch} format={effectiveFormat} rawAvailable={rawAvailable} drafts={drafts}
        onSelect={workspace.onSelectCamera} onSetup={() => setDialog('setup')} onError={workspace.setError}
        onCapture={(socket, value) => invoke(() => workspace.capture([socket], value))} />
      <ControlsPanel cameras={workspace.streams} activeSocket={workspace.activeSocket} running={running} busy={workspace.busy}
        onSelectCamera={workspace.onSelectCamera} onApply={workspace.onApply} onAutofocus={workspace.onAutofocus} onDraftsChange={setDrafts} />
    </main>
    <footer className="capture-dock" aria-label="Capture actions">
      <div className="capture-dock-info"><strong>Capture</strong><span id="capture-hint">{workspace.activeSocket ? `${socketLabel(workspace.activeSocket)} selected` : 'Choose a running camera'}</span></div>
      <div className="capture-toolbar">
        <CaptureFormatSelect id="capture-format" label="Capture format" value={effectiveFormat} disabled={workspace.busy || !running}
          rawAvailable={rawAvailable} onChange={setFormat} />
        <button type="button" id="capture-selected" className="button button-primary" disabled={workspace.busy || !workspace.activeSocket}
          aria-label={workspace.activeSocket ? `Capture selected camera ${socketLabel(workspace.activeSocket)}` : 'Capture selected camera'}
          onClick={() => invoke(() => workspace.capture([workspace.activeSocket], effectiveFormat))}>
          <svg className="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h4l2-3h6l2 3h4v13H3z" /><circle cx="12" cy="13" r="4" /></svg>Capture selected
        </button>
        <button type="button" id="capture-all" className="button" disabled={workspace.busy || !workspace.streams.length}
          onClick={() => invoke(() => workspace.capture(workspace.streams.map(camera => camera.socket), effectiveFormat))}>Capture all</button>
      </div>
      <span className="capture-sequence-note">Saved on the host · All cameras capture sequentially</span>
    </footer>
    {!dialog && feedback}
    <SetupDialog open={dialog === 'setup'} onClose={() => setDialog(null)} cameras={workspace.cameras} configs={workspace.configs}
      selected={workspace.selected} rawEnabled={workspace.rawEnabled} busy={workspace.busy} running={running}
      streamAction={workspace.streamAction} deviceId={device?.id} warnings={workspace.warnings} hasResolutionConflict={workspace.hasResolutionConflict}
      onSelect={workspace.onSelect} onConfigChange={workspace.onConfigChange} onPreset={workspace.onPreset} onRawChange={workspace.onRawChange}
      onMatchResolutions={workspace.onMatchResolutions} onScan={() => invoke(workspace.scan)} onStart={start} onStop={() => invoke(workspace.stop)}
      feedback={dialog === 'setup' ? feedback : undefined} />
    <CapturesDialog open={dialog === 'captures'} onClose={() => setDialog(null)} captures={workspace.status?.captures || []}
      captureDirectory={workspace.status?.capture_directory} feedback={dialog === 'captures' ? feedback : undefined} />
  </div>;
}
