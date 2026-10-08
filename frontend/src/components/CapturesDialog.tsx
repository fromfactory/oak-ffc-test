import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { Capture, CaptureSelection } from '../types';
import { socketLabel } from '../utils';
import { Dialog } from './Dialog';
import { Icon } from './Icon';
import './captures.css';

export interface CapturesDialogProps {
  open: boolean;
  onClose: () => void;
  captures: Capture[];
  captureDirectory?: string;
  loading?: boolean;
  busy?: boolean;
  onDownload: (selection: CaptureSelection) => Promise<void>;
  onDelete: (selection: CaptureSelection) => Promise<void>;
  onRefresh: () => Promise<void>;
  feedback?: ReactNode;
}

type GroupBy = 'date' | 'format' | 'category' | 'camera';
type PendingAction = 'download' | 'delete' | 'refresh' | null;
interface CaptureGroup { key: string; label: string; captures: Capture[] }
const pageSize = 48;

function safeDownloadURL(url: string) {
  try {
    const parsed = new URL(url, window.location.href);
    return parsed.origin === window.location.origin && ['http:', 'https:'].includes(parsed.protocol)
      ? parsed.href : null;
  } catch { return null; }
}

function fileSize(size?: number) {
  if (size === undefined || !Number.isFinite(size)) return '';
  return size >= 1073741824 ? `${(size / 1073741824).toFixed(1)} GB`
    : size >= 1048576 ? `${(size / 1048576).toFixed(1)} MB`
    : size >= 1024 ? `${Math.round(size / 1024)} KB` : `${size} B`;
}

function captureSize(capture: Capture) {
  return (capture.files || []).reduce((total, file) =>
    total + (Number.isFinite(file.size) ? file.size : 0), 0);
}

function dateKey(createdAt: string) {
  const date = new Date(createdAt);
  if (Number.isNaN(date.getTime())) return 'unknown';
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function captureDate(createdAt: string) {
  const date = new Date(createdAt);
  return Number.isNaN(date.getTime()) ? createdAt : date.toLocaleString(undefined, {
    month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function category(capture: Capture) { return capture.format === 'raw' ? 'raw' : 'processed'; }
function categoryLabel(value: string) { return value === 'raw' ? 'Sensor RAW' : 'Processed images'; }
function formatLabel(capture: Capture) { return (capture.format || 'image').toUpperCase(); }

function groupCaptures(captures: Capture[], groupBy: GroupBy) {
  const groups = new Map<string, CaptureGroup>();
  for (const capture of captures) {
    const key = groupBy === 'date' ? dateKey(capture.created_at)
      : groupBy === 'format' ? capture.format
      : groupBy === 'category' ? category(capture) : capture.socket;
    const label = groupBy === 'date' ? key === 'unknown' ? 'Unknown date'
      : new Date(`${key}T12:00:00`).toLocaleDateString(undefined, {
        weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
      }) : groupBy === 'format' ? formatLabel(capture)
      : groupBy === 'category' ? categoryLabel(key) : socketLabel(key);
    if (!groups.has(key)) groups.set(key, { key, label, captures: [] });
    groups.get(key)!.captures.push(capture);
  }
  return [...groups.values()].sort((a, b) => groupBy === 'date'
    ? b.key.localeCompare(a.key) : a.label.localeCompare(b.label));
}

function CapturePreview({ capture }: { capture: Capture }) {
  const [failed, setFailed] = useState(false);
  const isRaw = capture.format === 'raw';
  return <div className={`capture-preview ${isRaw ? 'capture-preview-raw' : ''}`}>
    {!isRaw && !failed ? <img src={`/captures/${encodeURIComponent(capture.id)}/thumbnail`}
      alt={`${socketLabel(capture.socket)} capture preview`} loading="lazy" decoding="async"
      onError={() => setFailed(true)} /> : <div className="capture-preview-placeholder" aria-hidden="true">
      <Icon name={isRaw ? 'file' : 'image'} />
      <strong>{formatLabel(capture)}</strong><span>{isRaw ? 'Unprocessed sensor data' : 'Preview unavailable'}</span>
    </div>}
    <span className="capture-format-badge">{formatLabel(capture)}</span>
  </div>;
}

export function CapturesDialog({
  open, onClose, captures, captureDirectory, loading = false, busy = false,
  onDownload, onDelete, onRefresh, feedback,
}: CapturesDialogProps) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [dateFilter, setDateFilter] = useState('');
  const [formatFilter, setFormatFilter] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [cameraFilter, setCameraFilter] = useState('');
  const [groupBy, setGroupBy] = useState<GroupBy>('date');
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [page, setPage] = useState(0);
  const [pending, setPending] = useState<PendingAction>(null);
  const [confirmation, setConfirmation] = useState<{ ids: string[]; all: boolean } | null>(null);
  const galleryRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const deleteOpenerRef = useRef<HTMLElement | null>(null);
  const actionRef = useRef(false);
  const locked = loading || busy || pending !== null;

  useEffect(() => {
    const existing = new Set(captures.map((capture) => capture.id));
    setSelected((previous) => [...previous].every((id) => existing.has(id))
      ? previous : new Set([...previous].filter((id) => existing.has(id))));
    // Keep the confirmed set frozen while allowing successful partial deletions to disappear.
    setConfirmation((previous) => {
      if (!previous) return previous;
      const ids = previous.ids.filter((id) => existing.has(id));
      return ids.length === previous.ids.length ? previous
        : ids.length > 0 ? { ...previous, ids } : null;
    });
  }, [captures]);

  useEffect(() => {
    if (!open) { setConfirmation(null); setSelected(new Set()); }
  }, [open]);

  useEffect(() => { if (confirmation) cancelRef.current?.focus(); }, [confirmation]);
  useEffect(() => {
    if (confirmation || !open || locked || !deleteOpenerRef.current) return;
    const opener = deleteOpenerRef.current;
    const target = opener.isConnected && !opener.matches(':disabled')
      ? opener : document.getElementById('refresh-captures');
    target?.focus({ preventScroll: true });
    deleteOpenerRef.current = null;
  }, [confirmation, open, locked]);
  useEffect(() => { setPage(0); }, [dateFilter, formatFilter, categoryFilter, cameraFilter, groupBy]);

  const sorted = useMemo(() => [...captures].sort((a, b) => {
    const aTime = new Date(a.created_at).getTime();
    const bTime = new Date(b.created_at).getTime();
    return (Number.isFinite(bTime) ? bTime : 0) - (Number.isFinite(aTime) ? aTime : 0);
  }), [captures]);
  const filtered = useMemo(() => sorted.filter((capture) =>
    (!dateFilter || dateKey(capture.created_at) === dateFilter)
    && (!formatFilter || capture.format === formatFilter)
    && (!categoryFilter || category(capture) === categoryFilter)
    && (!cameraFilter || capture.socket === cameraFilter)),
  [sorted, dateFilter, formatFilter, categoryFilter, cameraFilter]);
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, totalPages - 1);
  const groups = useMemo(() => {
    // Page the grouped collection so each format/category/camera stays together.
    const ordered = groupCaptures(filtered, groupBy).flatMap((group) => group.captures);
    return groupCaptures(ordered.slice(currentPage * pageSize, (currentPage + 1) * pageSize), groupBy);
  }, [filtered, groupBy, currentPage]);
  const cameras = [...new Set(captures.map((capture) => capture.socket))].sort();
  const formats = [...new Set(captures.map((capture) => capture.format))].sort();
  const activeFilters = [dateFilter, formatFilter, categoryFilter, cameraFilter].filter(Boolean).length;
  const totalBytes = captures.reduce((total, capture) => total + captureSize(capture), 0);
  const rawCount = captures.filter((capture) => category(capture) === 'raw').length;
  const filteredIds = new Set(filtered.map((capture) => capture.id));
  const hiddenSelections = [...selected].filter((id) => !filteredIds.has(id)).length;

  useEffect(() => {
    if (galleryRef.current) galleryRef.current.scrollTop = 0;
  }, [currentPage, dateFilter, formatFilter, categoryFilter, cameraFilter, groupBy]);

  function clearFilters() {
    setDateFilter(''); setFormatFilter(''); setCategoryFilter(''); setCameraFilter('');
  }

  function selectCaptures(items: Capture[]) {
    setSelected((previous) => new Set([...previous, ...items.map((capture) => capture.id)]));
  }

  function toggleCapture(id: string, checked: boolean) {
    setSelected((previous) => {
      const next = new Set(previous);
      if (checked) next.add(id); else next.delete(id);
      return next;
    });
  }

  function cancelDelete() {
    if (actionRef.current || busy) return;
    setConfirmation(null);
  }

  function requestClose() {
    if (actionRef.current || busy) return;
    if (confirmation) cancelDelete(); else onClose();
  }

  function requestDelete(all: boolean) {
    if (locked) return;
    const ids = all ? captures.map((capture) => capture.id) : [...selected];
    if (!ids.length) return;
    deleteOpenerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setConfirmation({ ids, all });
  }

  async function runAction(action: Exclude<PendingAction, null>, callback: () => Promise<void>) {
    if (locked || actionRef.current) return;
    actionRef.current = true;
    setPending(action);
    try { await callback(); } catch { /* Parent feedback explains failures; preserve the user's selection. */ }
    finally { actionRef.current = false; setPending(null); }
  }

  async function deleteConfirmed() {
    if (!confirmation) return;
    const ids = [...confirmation.ids];
    await runAction('delete', async () => {
      await onDelete({ ids });
      setSelected((previous) => new Set([...previous].filter((id) => !ids.includes(id))));
      setConfirmation(null);
    });
  }

  return (
    <Dialog id="captures-dialog" labelledBy="captures-heading" className="captures-dialog"
      open={open} onClose={requestClose} returnFocusId="open-captures" feedback={feedback}>
      <div className="capture-library-content" inert={confirmation !== null}>
        <div className="dialog-heading capture-library-heading">
          <div>
            <span className="eyebrow">SAVED IMAGES</span>
            <h2 id="captures-heading">Capture library <span className="capture-library-total">{captures.length}</span></h2>
            <p className="capture-library-description">Browse your captures, download a collection, or free up space.</p>
          </div>
          <button type="button" className="icon-button" id="close-captures" data-close-dialog
            aria-label="Close capture library" disabled={busy || pending !== null} onClick={requestClose}><Icon name="close" /></button>
        </div>

        <div className="capture-library-overview">
          <div className="capture-library-stats" aria-label="Library summary">
            <span><strong>{captures.length - rawCount}</strong> processed</span>
            <span><strong>{rawCount}</strong> sensor RAW</span>
            <span><strong>{fileSize(totalBytes)}</strong> saved</span>
          </div>
          <p id="capture-directory" className="capture-directory" hidden={!captureDirectory}>
            {captureDirectory ? `Save folder: ${captureDirectory}` : ''}
          </p>
        </div>

        <div className="capture-library-organize">
          <label className="capture-group-control" htmlFor="capture-group-by">Group by
            <select id="capture-group-by" value={groupBy} disabled={locked}
              onChange={(event) => setGroupBy(event.currentTarget.value as GroupBy)}>
              <option value="date">Capture date</option><option value="format">File format</option>
              <option value="category">Category</option><option value="camera">Camera</option>
            </select>
          </label>
          <button type="button" className={`button button-quiet ${filtersOpen ? 'capture-filter-active' : ''}`}
            id="toggle-capture-filters" aria-expanded={filtersOpen} aria-controls="capture-library-filters"
            disabled={locked} onClick={() => setFiltersOpen((value) => !value)}>
            <Icon name="filter" />Filters{activeFilters > 0 && <span className="count-badge">{activeFilters}</span>}
          </button>
          <button type="button" className="button button-quiet capture-refresh-button" id="refresh-captures"
            disabled={locked} onClick={() => void runAction('refresh', onRefresh)}>
            <Icon name="refresh" className={loading || pending === 'refresh' ? 'icon-loading' : ''} />
            {loading || pending === 'refresh' ? 'Loading…' : 'Refresh'}
          </button>
        </div>

        <div className="capture-library-browser" ref={galleryRef}>
          <div className="capture-library-filters" id="capture-library-filters" hidden={!filtersOpen}>
            <label htmlFor="capture-date-filter">Capture date
              <input type="date" id="capture-date-filter" value={dateFilter} disabled={locked}
                onChange={(event) => setDateFilter(event.currentTarget.value)} />
            </label>
            <label htmlFor="capture-format-filter">File format
              <select id="capture-format-filter" value={formatFilter} disabled={locked}
                onChange={(event) => setFormatFilter(event.currentTarget.value)}>
                <option value="">All formats</option>
                {formats.map((format) => <option key={format} value={format}>{format.toUpperCase()}</option>)}
              </select>
            </label>
            <label htmlFor="capture-category-filter">Category
              <select id="capture-category-filter" value={categoryFilter} disabled={locked}
                onChange={(event) => setCategoryFilter(event.currentTarget.value)}>
                <option value="">All categories</option><option value="processed">Processed images</option>
                <option value="raw">Sensor RAW</option>
              </select>
            </label>
            <label htmlFor="capture-camera-filter">Camera
              <select id="capture-camera-filter" value={cameraFilter} disabled={locked}
                onChange={(event) => setCameraFilter(event.currentTarget.value)}>
                <option value="">All cameras</option>
                {cameras.map((socket) => <option key={socket} value={socket}>{socketLabel(socket)}</option>)}
              </select>
            </label>
            <button type="button" className="button button-quiet button-small" id="reset-capture-filters"
              disabled={locked || activeFilters === 0} onClick={clearFilters}>Reset filters</button>
            <p>Processed images are ready to view. Sensor RAW contains unprocessed data.</p>
          </div>

          <div className="capture-library-selection">
            <p id="capture-results-count" aria-live="polite">{loading ? 'Loading saved images…'
              : `${filtered.length} of ${captures.length} saved images`}</p>
            <div>
              <button type="button" className="button button-quiet button-small" id="select-shown-captures"
                title="Select all images matching the current filters, including other pages"
                disabled={locked || filtered.length === 0} onClick={() => selectCaptures(filtered)}>Select filtered</button>
              <button type="button" className="button button-quiet button-small" id="select-all-captures"
                disabled={locked || captures.length === 0} onClick={() => selectCaptures(captures)}>Select all saved</button>
              <button type="button" className="button button-quiet button-small" id="clear-capture-selection"
                disabled={locked || selected.size === 0} onClick={() => setSelected(new Set())}>Clear</button>
            </div>
          </div>

          <div id="capture-list" className="capture-library-gallery" aria-busy={loading}>
            {loading && captures.length === 0 ? <div className="capture-library-empty" id="capture-library-loading" role="status">
              <span className="capture-empty-symbol" aria-hidden="true"><Icon name="clock" className="icon-placeholder" /></span>
              <h3>Loading your library</h3><p>Reading saved captures from the host.</p>
            </div> : filtered.length === 0 ? <div className="capture-library-empty">
              <span className="capture-empty-symbol" aria-hidden="true"><Icon name="image" className="icon-placeholder" /></span>
              <h3>{captures.length === 0 ? 'Your library starts here' : 'No images match these filters'}</h3>
              <p>{captures.length === 0 ? 'Capture an image to save it here with its metadata.'
                : 'Choose another date, format, category, or camera.'}</p>
              {activeFilters > 0 && <button type="button" className="button button-quiet" disabled={locked}
                onClick={clearFilters}>Reset filters</button>}
            </div> : groups.map((group) => <section className="capture-library-group" key={group.key} data-group-key={group.key}>
              <div className="capture-group-heading"><h3>{group.label}</h3>
                <span>{group.captures.length} {group.captures.length === 1 ? 'image' : 'images'}</span>
              </div>
              <div className="capture-card-grid">
                {group.captures.map((capture) => <article key={capture.id} data-capture-id={capture.id}
                  className={`capture-card ${selected.has(capture.id) ? 'capture-card-selected' : ''}`}>
                  <CapturePreview capture={capture} />
                  <label className="capture-card-select">
                    <input type="checkbox" checked={selected.has(capture.id)} disabled={locked}
                      aria-label={`Select ${socketLabel(capture.socket)} ${formatLabel(capture)} capture from ${captureDate(capture.created_at)}`}
                      onChange={(event) => toggleCapture(capture.id, event.currentTarget.checked)} />
                  </label>
                  <div className="capture-card-content">
                    <div className="capture-card-title"><strong>{socketLabel(capture.socket)}</strong>
                      <span>{categoryLabel(category(capture))}</span></div>
                    <time dateTime={capture.created_at}>{captureDate(capture.created_at)}</time>
                    <div className="capture-card-details"><span>{fileSize(captureSize(capture))}</span>
                      <span>{capture.files.length} {capture.files.length === 1 ? 'file' : 'files'} · includes metadata</span></div>
                    <div className="capture-card-files">
                      {(capture.files || []).map((file, fileIndex) => {
                        const url = safeDownloadURL(file.url);
                        return url && <a key={`${file.name}:${fileIndex}`} href={url} download={file.name || ''}
                          title={`${file.name || 'Download'}${fileSize(file.size) ? ` · ${fileSize(file.size)}` : ''}`}>
                          <span>{file.name || 'Download'}</span><small>{fileSize(file.size)}</small>
                        </a>;
                      })}
                    </div>
                  </div>
                </article>)}
              </div>
            </section>)}
            {filtered.length > pageSize && <nav className="capture-library-pagination" aria-label="Saved images pages">
              <button type="button" className="button button-small" id="previous-capture-page" disabled={locked || currentPage === 0}
                onClick={() => setPage(currentPage - 1)}>Previous</button>
              <span>Page {currentPage + 1} of {totalPages} · {filtered.length} images</span>
              <button type="button" className="button button-small" id="next-capture-page" disabled={locked || currentPage >= totalPages - 1}
                onClick={() => setPage(currentPage + 1)}>Next</button>
            </nav>}
          </div>
        </div>

        <div className="capture-library-footer">
          <div className="capture-footer-selection">
            <p id="capture-selection-count" aria-live="polite"><strong>{selected.size} selected</strong>
              {hiddenSelections > 0 && <span> · {hiddenSelections} outside current filters</span>}</p>
            <span className="capture-download-note">ZIP downloads include image files and metadata.</span>
          </div>
          <div className="capture-library-actions" aria-label="Capture actions">
            <button type="button" className="button button-primary" id="download-selected-captures"
              disabled={locked || selected.size === 0}
              onClick={() => void runAction('download', () => onDownload({ ids: [...selected] }))}>
              <Icon name="download" className="capture-action-icon" />Download selected</button>
            <button type="button" className="button capture-delete-button" id="delete-selected-captures"
              disabled={locked || selected.size === 0} onClick={() => requestDelete(false)}><Icon name="trash" className="capture-action-icon" />Delete selected</button>
            <button type="button" className="button" id="download-all-captures"
              disabled={locked || captures.length === 0}
              onClick={() => void runAction('download', () => onDownload({ all: true }))}><Icon name="download" className="capture-action-icon" />Download all saved</button>
            <button type="button" className="button capture-delete-button" id="delete-all-captures"
              disabled={locked || captures.length === 0} onClick={() => requestDelete(true)}><Icon name="trash" className="capture-action-icon" />Delete all saved</button>
          </div>
          {pending === 'download' && <p className="capture-action-progress" role="status">Preparing your ZIP download…</p>}
        </div>
      </div>

      {confirmation && <div className="capture-confirmation" id="capture-delete-confirmation"
        role="alertdialog" aria-modal="true" aria-labelledby="capture-delete-heading"
        aria-describedby="capture-delete-description">
        <div className="capture-confirmation-card">
          <span className="capture-confirmation-icon" aria-hidden="true"><Icon name="trash" /></span>
          <h3 id="capture-delete-heading">Permanently delete {confirmation.ids.length} {confirmation.ids.length === 1 ? 'image' : 'images'}?</h3>
          <p id="capture-delete-description">{confirmation.all ? 'The images saved when you chose Delete all'
            : 'These selected images'}
            {' '}and their metadata will be permanently removed from the Raspberry Pi or host computer. This cannot be undone.</p>
          <p className="capture-confirmation-help">Download a copy first if you want to keep these files.</p>
          <div className="capture-confirmation-actions">
            <button type="button" className="button" id="cancel-capture-delete" ref={cancelRef}
              disabled={busy || pending !== null} onClick={cancelDelete}>Cancel</button>
            <button type="button" className="button capture-delete-confirm-button" id="confirm-capture-delete"
              disabled={locked} onClick={() => void deleteConfirmed()}>
              {pending === 'delete' ? 'Deleting…' : 'Delete permanently'}</button>
          </div>
        </div>
      </div>}
    </Dialog>
  );
}
