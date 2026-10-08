import type { ReactNode } from 'react';
import type { Capture } from '../types';
import { Dialog } from './Dialog';

export interface CapturesDialogProps {
  open: boolean;
  onClose: () => void;
  captures: Capture[];
  captureDirectory?: string;
  feedback?: ReactNode;
}

function safeDownloadURL(url: string) {
  try {
    const parsed = new URL(url, window.location.href);
    return parsed.origin === window.location.origin && ['http:', 'https:'].includes(parsed.protocol)
      ? parsed.href : null;
  } catch {
    return null;
  }
}

function fileSize(size?: number) {
  if (size === undefined || !Number.isFinite(size)) return '';
  return size >= 1048576 ? `${(size / 1048576).toFixed(1)} MB`
    : size >= 1024 ? `${Math.round(size / 1024)} KB` : `${size} B`;
}

function captureDate(createdAt: string) {
  const date = new Date(createdAt);
  return Number.isNaN(date.getTime()) ? createdAt : date.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

export function CapturesDialog({ open, onClose, captures, captureDirectory, feedback }: CapturesDialogProps) {
  const recent = captures.slice().sort((a, b) =>
    new Date(b.created_at).getTime() - new Date(a.created_at).getTime()).slice(0, 18);

  return (
    <Dialog id="captures-dialog" labelledBy="captures-heading" className="captures-dialog"
      open={open} onClose={onClose} returnFocusId="open-captures" feedback={feedback}>
      <div className="dialog-heading">
        <div><span className="eyebrow">SAVED IMAGES</span><h2 id="captures-heading">Recent captures</h2></div>
        <button type="button" className="icon-button" id="close-captures" data-close-dialog
          aria-label="Close recent captures" onClick={onClose}>×</button>
      </div>
      <div className="dialog-scroll">
        <p id="capture-directory" className="capture-directory" hidden={!captureDirectory}>
          {captureDirectory ? `Save folder: ${captureDirectory}` : ''}
        </p>
        <p className="capture-note">Download images and metadata below. RAW contains unprocessed sensor data; capture metadata describes its layout.</p>
        <div id="capture-list" className="capture-list">
          {recent.length === 0 && <p className="captures-empty">Your saved images and their metadata will appear here.</p>}
          {recent.map((capture) => <div className="capture-item" key={capture.id}>
            <div className="capture-file-icon" aria-hidden="true">▧</div>
            <div className="min-w-0">
              <div className="capture-item-title">{capture.socket === 'CAM_AA' ? 'CAM_A' : capture.socket} · {(capture.format || 'image').toUpperCase()} capture</div>
              <div className="capture-item-details">{captureDate(capture.created_at)}</div>
            </div>
            <div className="capture-links">
              {(capture.files || []).map((file, fileIndex) => {
                const url = safeDownloadURL(file.url);
                const size = fileSize(file.size);
                return url && <a key={`${file.name}:${fileIndex}`} href={url} download={file.name || ''}>
                  {file.name || 'Download'}{size && <small>{size}</small>}
                </a>;
              })}
            </div>
          </div>)}
          {captures.length > 18 && <p className="captures-empty">Showing the 18 most recent captures of {captures.length}. All files remain saved on the host computer.</p>}
        </div>
      </div>
      <div className="dialog-footer">
        <span>Files remain saved when this window closes.</span>
        <button type="button" className="button" data-close-dialog onClick={onClose}>Done</button>
      </div>
    </Dialog>
  );
}
