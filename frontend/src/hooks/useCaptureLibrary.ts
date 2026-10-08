import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import type { Capture, CaptureSelection } from '../types';

interface CaptureLibraryOptions {
  open: boolean;
  busy: boolean;
  runAction: (work: () => Promise<void>) => Promise<void>;
  refreshStatus: () => Promise<void>;
  showNotice: (message: string) => void;
  onError: (message: string) => void;
}

export function useCaptureLibrary({ open, busy, runAction, refreshStatus, showNotice, onError }: CaptureLibraryOptions) {
  const [captures, setCaptures] = useState<Capture[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const generation = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  const busyRef = useRef(busy);
  busyRef.current = busy;

  const cancelRead = useCallback(() => {
    generation.current += 1;
    controllerRef.current?.abort();
  }, []);

  const load = useCallback(async (showLoading: boolean) => {
    cancelRead();
    const version = generation.current;
    const controller = new AbortController();
    controllerRef.current = controller;
    if (showLoading) setLoading(true);
    try {
      const result = await api.captures(controller.signal);
      if (version === generation.current) { setCaptures(result.captures); setLoaded(true); }
    } finally {
      if (version === generation.current) {
        setLoading(false);
        controllerRef.current = null;
      }
    }
  }, [cancelRead]);

  useEffect(() => {
    if (!open) { setLoading(false); return; }
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async (initial: boolean) => {
      try { if (initial || (!busyRef.current && !document.hidden)) await load(initial); }
      catch (cause) {
        if (!disposed && !(cause instanceof DOMException && cause.name === 'AbortError')) {
          onError(cause instanceof Error ? cause.message : String(cause));
        }
      } finally { if (!disposed) timer = setTimeout(() => void refresh(false), 5000); }
    };
    void refresh(true);
    return () => { disposed = true; cancelRead(); clearTimeout(timer); };
  }, [open, load, cancelRead, onError]);

  const refresh = useCallback(() => runAction(async () => {
    await load(true);
    await refreshStatus();
  }), [runAction, load, refreshStatus]);

  const download = useCallback((selection: CaptureSelection) => runAction(async () => {
    const archive = await api.prepareCaptureDownload(selection);
    const url = new URL(archive.download_url, window.location.href);
    if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/captures/download/')) {
      throw new Error('The server returned an invalid capture download link.');
    }
    // Let the browser stream large RAW archives directly to its download manager.
    const link = document.createElement('a');
    link.href = url.href;
    link.download = archive.filename;
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    showNotice(`ZIP download ready: ${archive.count} ${archive.count === 1 ? 'capture' : 'captures'} with metadata.`);
  }), [runAction, showNotice]);

  const remove = useCallback((selection: CaptureSelection) => runAction(async () => {
    cancelRead();
    try {
      const result = await api.deleteCaptures(selection);
      setCaptures(result.captures); setLoaded(true);
      showNotice(`${result.deleted.length} ${result.deleted.length === 1 ? 'capture' : 'captures'} permanently deleted from the host.`);
    } catch (cause) {
      if (cause instanceof ApiError && cause.body.captures) {
        setCaptures(cause.body.captures); setLoaded(true);
      } else { await load(false).catch(() => {}); }
      await refreshStatus().catch(() => {});
      throw cause;
    }
    await refreshStatus().catch(() => {});
  }), [runAction, cancelRead, load, refreshStatus, showNotice]);

  return { captures, loading, loaded, refresh, download, remove };
}
