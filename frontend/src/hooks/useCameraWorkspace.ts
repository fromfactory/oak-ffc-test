import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { api } from '../api';
import type { CameraConfig, CameraControls, CameraStatus, CaptureFormat } from '../types';
import { resolutionOrder, resolutionWarning, sortedCameras, socketLabel } from '../utils';

interface WorkspaceState {
  status: CameraStatus | null;
  selected: Set<string>;
  configs: Record<string, CameraConfig>;
  activeSocket: string;
  rawEnabled: boolean;
  streamEpoch: number;
}
type WorkspaceAction =
  | { type: 'status'; status: CameraStatus }
  | { type: 'select'; selected: Set<string> }
  | { type: 'config'; socket: string; patch: Partial<CameraConfig> }
  | { type: 'active'; socket: string }
  | { type: 'raw'; enabled: boolean }
  | { type: 'match' };

function reducer(state: WorkspaceState, action: WorkspaceAction): WorkspaceState {
  switch (action.type) {
    case 'status': {
      const status = action.status;
      const cameras = sortedCameras(status.cameras);
      const detected = new Set(cameras.map(camera => camera.socket));
      const selected = new Set([...state.selected].filter(socket => detected.has(socket)));
      const started = status.running && !state.status?.running;
      if (!state.status || (!state.status.cameras.length && cameras.length) || started) {
        const running = cameras.filter(camera => camera.active === true || status.active_sockets?.includes(camera.socket));
        if (status.running && running.length) {
          selected.clear();
          running.forEach(camera => selected.add(camera.socket));
        } else if (!state.status || !state.status.cameras.length) {
          cameras.slice(0, 2).forEach(camera => selected.add(camera.socket));
        }
      }
      const configs = { ...state.configs };
      cameras.forEach(camera => {
        const active = camera.active === true || status.active_sockets?.includes(camera.socket);
        if (!configs[camera.socket] || (started && active)) configs[camera.socket] = {
          resolution: camera.resolution || '1080p', fps: camera.requested_fps || 10,
        };
      });
      const active = status.running
        ? cameras.filter(camera => typeof camera.active === 'boolean'
          ? camera.active : status.active_sockets?.includes(camera.socket) ?? selected.has(camera.socket)) : [];
      return {
        ...state, status, selected, configs,
        activeSocket: active.some(camera => camera.socket === state.activeSocket) ? state.activeSocket : active[0]?.socket || '',
        rawEnabled: started ? Boolean(status.raw_enabled) : state.rawEnabled,
        streamEpoch: state.streamEpoch + (started ? 1 : 0),
      };
    }
    case 'select': return { ...state, selected: action.selected };
    case 'config': return { ...state, configs: { ...state.configs, [action.socket]: { ...state.configs[action.socket], ...action.patch } } };
    case 'active': return { ...state, activeSocket: action.socket };
    case 'raw': return { ...state, rawEnabled: action.enabled };
    case 'match': {
      const highest = [...resolutionOrder].reverse().find(mode => [...state.selected].some(socket => state.configs[socket]?.resolution === mode));
      if (!highest) return state;
      const configs = { ...state.configs };
      state.selected.forEach(socket => { configs[socket] = { ...configs[socket], resolution: highest }; });
      return { ...state, configs };
    }
  }
}

export function useCameraWorkspace() {
  const [state, dispatch] = useReducer(reducer, {
    status: null, selected: new Set<string>(), configs: {}, activeSocket: '', rawEnabled: false, streamEpoch: 0,
  });
  const [busy, setBusy] = useState(true);
  const [initialized, setInitialized] = useState(false);
  const [streamAction, setStreamAction] = useState<'start' | 'stop' | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [connected, setConnected] = useState(false);
  const busyRef = useRef(true);
  const generation = useRef(0);
  const pollController = useRef<AbortController | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showNotice = useCallback((message: string) => {
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    setNotice(message);
    noticeTimer.current = setTimeout(() => setNotice(''), 6500);
  }, []);
  const acceptStatus = useCallback((status: CameraStatus) => {
    dispatch({ type: 'status', status });
    setConnected(true);
    if (status.error) setError(status.error);
  }, []);
  const refresh = useCallback(async () => { acceptStatus(await api.status()); }, [acceptStatus]);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const initialController = new AbortController();
    async function initialize() {
      try {
        let status = await api.status(initialController.signal);
        if (disposed) return;
        if (!status.running && !status.cameras.length) status = await api.scan();
        if (!disposed) acceptStatus(status);
      } catch (cause) {
        if (!disposed) { setConnected(false); setError(cause instanceof Error ? cause.message : String(cause)); }
      } finally {
        if (!disposed) {
          busyRef.current = false; setBusy(false); setInitialized(true);
          timer = setTimeout(poll, 1500);
        }
      }
    }
    async function poll() {
      if (disposed) return;
      if (!busyRef.current && !document.hidden) {
        const currentGeneration = generation.current;
        const controller = new AbortController();
        pollController.current = controller;
        try {
          const status = await api.status(controller.signal);
          if (!disposed && generation.current === currentGeneration) acceptStatus(status);
        } catch (cause) {
          if (!disposed && !controller.signal.aborted && generation.current === currentGeneration) {
            setConnected(false); setError(cause instanceof Error ? cause.message : String(cause));
          }
        } finally { if (pollController.current === controller) pollController.current = null; }
      }
      if (!disposed) timer = setTimeout(poll, 1500);
    }
    void initialize();
    return () => { disposed = true; initialController.abort(); pollController.current?.abort(); clearTimeout(timer); };
  }, [acceptStatus]);

  useEffect(() => () => { if (noticeTimer.current) clearTimeout(noticeTimer.current); }, []);

  const runAction = useCallback(async (work: () => Promise<void>) => {
    if (busyRef.current) throw new Error('Wait for the current camera action to finish.');
    busyRef.current = true;
    generation.current += 1;
    pollController.current?.abort();
    setBusy(true); setError('');
    try { await work(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); throw cause; }
    finally { busyRef.current = false; setBusy(false); }
  }, []);

  const cameras = sortedCameras(state.status?.cameras || []);
  const streams = state.status?.running ? cameras.filter(camera => typeof camera.active === 'boolean'
    ? camera.active : state.status?.active_sockets?.includes(camera.socket) ?? state.selected.has(camera.socket)) : [];
  const socketA = state.selected.has('CAM_A') ? 'CAM_A' : state.selected.has('CAM_AA') ? 'CAM_AA' : '';
  const hasResolutionConflict = Boolean(socketA && state.selected.has('CAM_D') && state.configs[socketA]?.resolution !== state.configs.CAM_D?.resolution);
  const warnings = [...(state.status?.warnings || [])];
  if (hasResolutionConflict) warnings.push(resolutionWarning);
  if (state.selected.has('CAM_B') && state.selected.has('CAM_C')) warnings.push('CAM_B and CAM_C may share I²C camera controls for identical sensors. Settings may affect both cameras.');

  async function start() {
    await runAction(async () => {
      if (hasResolutionConflict) throw new Error(resolutionWarning);
      const configs = cameras.filter(camera => state.selected.has(camera.socket)).map(camera => ({ socket: camera.socket, ...state.configs[camera.socket] }));
      for (const config of configs) if (!Number.isInteger(config.fps) || config.fps < 2 || config.fps > 30) {
        throw new Error(`${config.socket}: enter a whole-number frame rate between 2 and 30 fps.`);
      }
      setStreamAction('start');
      try { acceptStatus(await api.start(configs, state.rawEnabled)); showNotice('Streams started.'); }
      finally { setStreamAction(null); }
    });
  }
  async function stop() {
    await runAction(async () => {
      setStreamAction('stop');
      try { acceptStatus(await api.stop()); showNotice('Streams stopped.'); }
      finally { setStreamAction(null); }
    });
  }
  async function capture(sockets: string[], format: CaptureFormat) {
    await runAction(async () => {
      try {
        const result = await api.capture(sockets, format);
        const count = result.captures.length;
        showNotice(`${count} ${format.toUpperCase()} ${count === 1 ? 'capture' : 'captures'} saved. Open Captures to download.`);
      } catch (cause) {
        // A partial capture may have saved files; keep the original failure visible.
        await refresh().catch(() => {});
        throw cause;
      }
      await refresh();
    });
  }

  return {
    ...state, cameras, streams, busy, initialized, streamAction, error, notice, connected, warnings, hasResolutionConflict,
    start, stop, capture, setError, runAction, refresh, showNotice,
    dismissError: () => setError(''),
    scan: () => runAction(async () => { const status = await api.scan(); acceptStatus(status); showNotice(`${status.cameras.length} ${status.cameras.length === 1 ? 'camera' : 'cameras'} discovered.`); }),
    onSelect: (socket: string, selected: boolean) => {
      if (busy || state.status?.running) return;
      if (selected && state.selected.size >= 3) { setError('Select a maximum of three cameras per session.'); return; }
      const sockets = new Set(state.selected);
      if (selected) sockets.add(socket); else sockets.delete(socket);
      dispatch({ type: 'select', selected: sockets });
    },
    onConfigChange: (socket: string, patch: Partial<CameraConfig>) => dispatch({ type: 'config', socket, patch }),
    onPreset: (count: number) => dispatch({ type: 'select', selected: new Set(cameras.slice(0, count).map(camera => camera.socket)) }),
    onRawChange: (enabled: boolean) => dispatch({ type: 'raw', enabled }),
    onMatchResolutions: () => { dispatch({ type: 'match' }); showNotice('Selected cameras now use the highest selected mode.'); },
    onSelectCamera: (socket: string) => dispatch({ type: 'active', socket }),
    onApply: (socket: string, changes: Partial<CameraControls>) => runAction(async () => {
      if (Object.keys(changes).length) await api.controls(socket, changes);
      await refresh(); showNotice(`Settings applied to ${socketLabel(socket)}.`);
    }),
    onAutofocus: (socket: string) => runAction(async () => {
      await api.controls(socket, { focus_mode: 'auto' }); await refresh(); showNotice(`Autofocus requested for ${socketLabel(socket)}.`);
    }),
  };
}
