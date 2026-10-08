import { useEffect, useRef, type ReactNode } from 'react';

export interface DialogProps {
  id: string;
  labelledBy: string;
  open: boolean;
  onClose: () => void;
  className?: string;
  returnFocusId?: string;
  children: ReactNode;
  feedback?: ReactNode;
}

/** Keep native focus trapping and the browser's dialog top layer. */
export function Dialog({
  id,
  labelledBy,
  open,
  onClose,
  className = '',
  returnFocusId,
  children,
  feedback,
}: DialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const backdropPointerDown = useRef(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (open && !dialog.open) {
      const focused = document.activeElement;
      openerRef.current = focused instanceof HTMLElement && focused !== document.body
        ? focused
        : returnFocusId ? document.getElementById(returnFocusId) : null;
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open, returnFocusId]);

  function outsideDialog(clientX: number, clientY: number) {
    const bounds = dialogRef.current?.getBoundingClientRect();
    return !!bounds && (clientX < bounds.left || clientX > bounds.right
      || clientY < bounds.top || clientY > bounds.bottom);
  }

  function restoreFocus() {
    // Switching dialogs should leave focus in the newly opened modal.
    if (document.querySelector('dialog[open]')) return;
    const previous = openerRef.current;
    const opener = previous?.isConnected && previous.getClientRects().length > 0
      && !previous.matches(':disabled')
      ? previous
      : returnFocusId ? document.getElementById(returnFocusId) : null;
    opener?.focus({ preventScroll: true });
  }

  return (
    <dialog
      ref={dialogRef}
      id={id}
      className={`workspace-dialog ${className}`.trim()}
      aria-labelledby={labelledBy}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClose={(event) => {
        restoreFocus();
        if (open && !event.currentTarget.open) onClose();
      }}
      onPointerDown={(event) => {
        backdropPointerDown.current = event.target === event.currentTarget
          && outsideDialog(event.clientX, event.clientY);
      }}
      onClick={(event) => {
        if (backdropPointerDown.current && event.target === event.currentTarget
          && outsideDialog(event.clientX, event.clientY)) onClose();
        backdropPointerDown.current = false;
      }}
    >
      {children}
      {feedback}
    </dialog>
  );
}
