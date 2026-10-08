const iconPaths = {
  camera: <><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3z" /><circle cx="12" cy="14" r="4" /></>,
  folder: <path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zm0 0h18" />,
  settings: <><path d="M4 6h3m4 0h9M4 12h9m4 0h3M4 18h3m4 0h9" /><circle cx="9" cy="6" r="2" /><circle cx="15" cy="12" r="2" /><circle cx="9" cy="18" r="2" /></>,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  play: <path d="m7 4 14 8-14 8z" />,
  stop: <rect x="5" y="5" width="14" height="14" rx="1" />,
  refresh: <><path d="M20 7V3m0 4h-4M4 17v4m0-4h4" /><path d="M20 7a9 9 0 0 0-15.2-1M4 17a9 9 0 0 0 15.2 1" /></>,
  fullscreen: <path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5" />,
  download: <><path d="M12 3v12m-5-5 5 5 5-5M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4" /></>,
  trash: <><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7" /></>,
  filter: <><path d="M4 7h7m4 0h5M4 17h3m4 0h9" /><circle cx="13" cy="7" r="2" /><circle cx="9" cy="17" r="2" /></>,
  image: <><rect x="3" y="3" width="18" height="18" rx="3" /><circle cx="8.5" cy="8.5" r="1.5" /><path d="m3 17 5-5 4 4 4-6 5 7" /></>,
  file: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 12h8m-8 4h8" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
};

export function Icon({ name, className = '' }: { name: keyof typeof iconPaths; className?: string }) {
  return <svg className={`icon ${className}`.trim()} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    {iconPaths[name]}
  </svg>;
}
