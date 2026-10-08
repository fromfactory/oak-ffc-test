import { useEffect, useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent } from 'react';
import type { Camera, CameraControls } from '../types';
import { socketLabel } from '../utils';

interface ControlsPanelProps {
  cameras: Camera[];
  activeSocket: string;
  running: boolean;
  busy: boolean;
  onSelectCamera: (socket: string) => void;
  onApply: (socket: string, changes: Partial<CameraControls>) => Promise<void>;
  onAutofocus: (socket: string) => Promise<void>;
  onDraftsChange?: (sockets: string[]) => void;
}

const DEFAULT_CONTROLS: CameraControls = {
  exposure_mode: 'auto',
  exposure_us: 10000,
  iso: 400,
  exposure_lock: false,
  auto_exposure_limit_us: 0,
  white_balance_mode: 'auto',
  white_balance_kelvin: 4500,
  white_balance_lock: false,
  focus_mode: 'continuous',
  focus: 128,
  exposure_compensation: 0,
  brightness: 0,
  contrast: 0,
  saturation: 0,
  sharpness: 1,
  luma_denoise: 1,
  chroma_denoise: 1,
  anti_banding: 'auto',
  effect_mode: 'off',
};

const TABS = ['exposure', 'color', 'focus', 'image'] as const;
type ControlTab = (typeof TABS)[number];
type AdvancedTab = Exclude<ControlTab, 'focus'>;
type ControlKey = keyof CameraControls;
type NumericKey = {
  [Key in ControlKey]: CameraControls[Key] extends number ? Key : never;
}[ControlKey];
type FormValue = string | boolean;
type FormValues = Record<ControlKey, FormValue>;
type UpdateField = (name: ControlKey, value: FormValue) => void;

interface ControlDraft {
  values: FormValues;
  baseline: CameraControls;
}

function cameraLabel(camera: Camera) {
  return socketLabel(camera.socket);
}

function formValues(controls: CameraControls, maxExposure: number): FormValues {
  const entries = Object.entries(controls).map(([name, value]) => [
    name,
    typeof value === 'boolean' ? value : String(value),
  ]);
  const values = Object.fromEntries(entries) as FormValues;
  values.exposure_us = String(Math.min(controls.exposure_us, maxExposure));
  if (values.exposure_mode === 'manual') values.exposure_lock = false;
  if (values.white_balance_mode !== 'auto') values.white_balance_lock = false;
  return values;
}

interface NumericFieldProps {
  id: string;
  name: NumericKey;
  label: string;
  help?: string;
  min: number;
  max: number;
  step?: number;
  value: FormValue;
  category: ControlTab;
  advanced?: AdvancedTab;
  disabled?: boolean;
  onChange: UpdateField;
}

function NumericField({
  id, name, label, help, min, max, step = 1, value, category, advanced,
  disabled, onChange,
}: NumericFieldProps) {
  return <>
    <label htmlFor={id}>{label}{help && <small>{help}</small>}</label>
    <input
      type="number" id={id} name={name} min={min} max={max} step={step}
      required value={String(value)} disabled={disabled}
      data-control-category={category} data-control-advanced={advanced}
      onChange={(event) => onChange(name, event.currentTarget.value)}
    />
  </>;
}

interface SliderFieldProps extends Omit<NumericFieldProps, 'advanced'> {
  numberId: string;
  numberLabel: string;
  unit: string;
  temperature?: boolean;
}

function SliderField({
  id, numberId, name, label, numberLabel, unit, min, max, step = 1,
  value, category, disabled, help, temperature, onChange,
}: SliderFieldProps) {
  // Keep invalid number edits editable while the range retains a valid position.
  const numericValue = Number(value);
  const rangeValue = Number.isFinite(numericValue)
    ? Math.min(max, Math.max(min, numericValue)) : min;
  return <div className="slider-field">
    <label htmlFor={id}>{label} <span>{unit}</span></label>
    <div className="slider-inputs">
      <input
        type="range" id={id} name={name} min={min} max={max} step={step}
        value={rangeValue} disabled={disabled} data-pair={numberId}
        data-control-category={category}
        onChange={(event) => onChange(name, event.currentTarget.value)}
      />
      <input
        type="number" id={numberId} min={min} max={max} step={step}
        required value={String(value)} disabled={disabled} aria-label={numberLabel}
        data-pair={id} data-control-category={category}
        onChange={(event) => onChange(name, event.currentTarget.value)}
      />
    </div>
    {help && <span className="field-help" id="exposure-limit">{help}</span>}
    {temperature && <div className="temperature-scale" aria-hidden="true">
      <span>Warm</span><span>Cool</span>
    </div>}
  </div>;
}

export function ControlsPanel({
  cameras, activeSocket, running, busy, onSelectCamera, onApply,
  onAutofocus, onDraftsChange,
}: ControlsPanelProps) {
  const [drafts, setDrafts] = useState<Record<string, ControlDraft>>({});
  const [tab, setTab] = useState<ControlTab>('exposure');
  const [advanced, setAdvanced] = useState<Record<AdvancedTab, boolean>>({
    exposure: false, color: false, image: false,
  });
  const formRef = useRef<HTMLFormElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const tabRefs = useRef<Partial<Record<ControlTab, HTMLButtonElement>>>({});
  const draftsChangeRef = useRef(onDraftsChange);
  draftsChangeRef.current = onDraftsChange;

  const camera = cameras.find((item) => item.socket === activeSocket);
  const fps = camera?.requested_fps || 10;
  const maxExposure = Math.max(100, Math.floor(1000000 / fps / 100) * 100);
  const reported: CameraControls = { ...DEFAULT_CONTROLS, ...camera?.controls };
  const draft = drafts[activeSocket];
  const values = draft?.values ?? formValues(reported, maxExposure);
  const disabled = busy || !running || !camera;
  const hasFocus = camera?.autofocus === true;
  const exposureManual = values.exposure_mode === 'manual';
  const whiteBalanceManual = values.white_balance_mode === 'manual';
  const focusManual = values.focus_mode === 'manual';

  useEffect(() => {
    draftsChangeRef.current?.(Object.keys(drafts));
  }, [drafts]);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [tab]);

  function update(name: ControlKey, value: FormValue) {
    if (disabled) return;
    setDrafts((current) => {
      const previous = current[activeSocket];
      const next = { ...(previous?.values ?? formValues(reported, maxExposure)), [name]: value };
      if (name === 'exposure_mode' && value === 'manual') next.exposure_lock = false;
      if (name === 'white_balance_mode' && value !== 'auto') next.white_balance_lock = false;
      return {
        ...current,
        [activeSocket]: { values: next, baseline: previous?.baseline ?? reported },
      };
    });
  }

  function clearDraft(socket: string) {
    setDrafts((current) => {
      const next = { ...current };
      delete next[socket];
      return next;
    });
  }

  function handleTabKey(event: KeyboardEvent<HTMLButtonElement>, name: ControlTab) {
    const current = TABS.indexOf(name);
    const next = event.key === 'ArrowRight' ? (current + 1) % TABS.length
      : event.key === 'ArrowLeft' ? (current + TABS.length - 1) % TABS.length
        : event.key === 'Home' ? 0 : event.key === 'End' ? TABS.length - 1 : null;
    if (next === null) return;
    event.preventDefault();
    const nextTab = TABS[next];
    setTab(nextTab);
    tabRefs.current[nextTab]?.focus();
  }

  function fieldEnabled(name: ControlKey) {
    if (name === 'exposure_us' || name === 'iso') return exposureManual;
    if (name === 'exposure_lock' || name === 'exposure_compensation' || name === 'auto_exposure_limit_us') {
      return !exposureManual;
    }
    if (name === 'white_balance_kelvin') return whiteBalanceManual;
    if (name === 'white_balance_lock') return values.white_balance_mode === 'auto';
    if (name === 'focus_mode') return hasFocus;
    if (name === 'focus') return hasFocus && focusManual;
    return true;
  }

  async function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled) return;
    const invalid = formRef.current?.querySelector<HTMLInputElement | HTMLSelectElement>(
      'input:invalid, select:invalid',
    );
    if (invalid) {
      const invalidTab = invalid.dataset.controlCategory as ControlTab | undefined;
      const invalidAdvanced = invalid.dataset.controlAdvanced as AdvancedTab | undefined;
      if (invalidTab) setTab(invalidTab);
      if (invalidAdvanced) setAdvanced((current) => ({ ...current, [invalidAdvanced]: true }));
      // Allow React to reveal the appropriate tab before native validation focuses it.
      requestAnimationFrame(() => {
        invalid.focus();
        invalid.reportValidity();
      });
      return;
    }
    const changes: Record<string, string | number | boolean> = {};
    const baseline = draft?.baseline ?? reported;
    for (const name of Object.keys(DEFAULT_CONTROLS) as ControlKey[]) {
      if (!fieldEnabled(name)) continue;
      const value = typeof DEFAULT_CONTROLS[name] === 'number' ? Number(values[name]) : values[name];
      if (value !== baseline[name]) changes[name] = value;
    }
    const socket = activeSocket;
    try {
      await onApply(socket, changes as Partial<CameraControls>);
      clearDraft(socket);
    } catch {
      // The parent presents the API error; preserve every edit for a retry.
    }
  }

  async function triggerAutofocus() {
    if (disabled || !hasFocus) return;
    const socket = activeSocket;
    try {
      await onAutofocus(socket);
      setDrafts((current) => {
        const pending = current[socket];
        return pending ? {
          ...current,
          [socket]: { ...pending, baseline: { ...pending.baseline, focus_mode: 'auto' } },
        } : current;
      });
    } catch {
      // The parent handles errors and the existing draft remains available.
    }
  }

  function advancedToggle(name: AdvancedTab, open: boolean) {
    setAdvanced((current) => current[name] === open ? current : { ...current, [name]: open });
  }

  return <aside className="control-panel" aria-labelledby="controls-heading">
    <div className="control-heading">
      <h2 id="controls-heading">Controls</h2>
      <div className="control-camera-row">
        <label className="sr-only" htmlFor="control-camera">Camera to adjust</label>
        <select id="control-camera" value={activeSocket} disabled={disabled}
          onChange={(event) => onSelectCamera(event.currentTarget.value)}>
          {!cameras.length && <option value="">Start a camera to adjust</option>}
          {cameras.map((item) => <option key={item.socket} value={item.socket}>{cameraLabel(item)}</option>)}
        </select>
      </div>
      <span id="control-camera-badge" className="sr-only">{camera ? cameraLabel(camera) : '—'}</span>
    </div>
    <div className="control-tabs" role="tablist" aria-label="Camera control categories">
      {TABS.map((name) => <button
        key={name} type="button" id={`tab-${name}`} role="tab" data-control-tab={name}
        aria-controls={`panel-${name}`} aria-selected={tab === name} tabIndex={tab === name ? 0 : -1}
        ref={(element) => { if (element) tabRefs.current[name] = element; }}
        onClick={() => setTab(name)} onKeyDown={(event) => handleTabKey(event, name)}
      >{name[0].toUpperCase() + name.slice(1)}</button>)}
    </div>
    <form id="controls-form" noValidate ref={formRef} onSubmit={apply}>
      <div className="controls-scroll" ref={scrollRef}>
        <p className="control-help" id="control-help">{camera && running
          ? `${camera.sensor || 'Camera'} · Preview updates after Apply.`
          : 'Start streams to adjust the selected camera.'}</p>
        <fieldset id="control-fields" disabled={disabled}>
          <section id="panel-exposure" className="control-tab-panel" role="tabpanel"
            aria-labelledby="tab-exposure" hidden={tab !== 'exposure'}>
            <div className="control-section-heading">
              <h3>Exposure</h3>
              <select id="exposure-mode" name="exposure_mode" aria-label="Exposure mode"
                value={String(values.exposure_mode)} onChange={(event) => update('exposure_mode', event.currentTarget.value)}>
                <option value="auto">Automatic</option><option value="manual">Manual</option>
              </select>
            </div>
            <div id="manual-exposure-fields" hidden={!exposureManual}>
              <SliderField id="exposure-us" numberId="exposure-number" name="exposure_us"
                label="Shutter" unit="µs" numberLabel="Shutter time in microseconds"
                min={100} max={maxExposure} step={100} value={values.exposure_us} category="exposure"
                disabled={!exposureManual} onChange={update}
                help={`Up to ${(maxExposure / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })} ms at ${fps} fps.`} />
              <SliderField id="iso" numberId="iso-number" name="iso" label="Sensitivity" unit="ISO"
                numberLabel="ISO sensitivity" min={100} max={1600} step={50}
                value={values.iso} category="exposure" disabled={!exposureManual} onChange={update} />
            </div>
            <label className="toggle-field" htmlFor="exposure-lock">
              <span>Lock auto exposure<small>Hold the current automatic exposure</small></span>
              <input type="checkbox" id="exposure-lock" name="exposure_lock"
                checked={values.exposure_lock === true} disabled={exposureManual}
                onChange={(event) => update('exposure_lock', event.currentTarget.checked)} />
            </label>
            <details className="advanced-controls" open={advanced.exposure}
              onToggle={(event) => advancedToggle('exposure', event.currentTarget.open)}>
              <summary>More exposure options</summary>
              <div className="compact-fields">
                <NumericField id="auto-exposure-limit" name="auto_exposure_limit_us"
                  label="Auto shutter limit" help="µs · 0 uses the frame period"
                  min={0} max={Math.floor(1000000 / fps)} value={values.auto_exposure_limit_us}
                  category="exposure" advanced="exposure" disabled={exposureManual} onChange={update} />
                <NumericField id="exposure-compensation" name="exposure_compensation"
                  label="Exposure compensation" help="−9 to +9 · automatic mode"
                  min={-9} max={9} value={values.exposure_compensation}
                  category="exposure" advanced="exposure" disabled={exposureManual} onChange={update} />
                <label htmlFor="anti-banding">Anti-banding</label>
                <select id="anti-banding" name="anti_banding" value={String(values.anti_banding)}
                  onChange={(event) => update('anti_banding', event.currentTarget.value)}>
                  <option value="auto">Automatic</option><option value="off">Off</option>
                  <option value="50hz">50 Hz</option><option value="60hz">60 Hz</option>
                </select>
              </div>
            </details>
          </section>
          <section id="panel-color" className="control-tab-panel" role="tabpanel"
            aria-labelledby="tab-color" hidden={tab !== 'color'}>
            <div className="control-section-heading">
              <h3>White balance</h3>
              <select id="white-balance-mode" name="white_balance_mode" aria-label="White balance mode"
                value={String(values.white_balance_mode)}
                onChange={(event) => update('white_balance_mode', event.currentTarget.value)}>
                <option value="auto">Automatic</option><option value="manual">Manual kelvin</option>
                <option value="incandescent">Incandescent</option><option value="fluorescent">Fluorescent</option>
                <option value="warm_fluorescent">Warm fluorescent</option><option value="daylight">Daylight</option>
                <option value="cloudy">Cloudy</option><option value="twilight">Twilight</option><option value="shade">Shade</option>
              </select>
            </div>
            <div id="manual-white-balance-fields" hidden={!whiteBalanceManual}>
              <SliderField id="white-balance-kelvin" numberId="white-balance-number" name="white_balance_kelvin"
                label="Temperature" unit="K" numberLabel="White balance temperature in kelvin"
                min={1000} max={12000} step={100} value={values.white_balance_kelvin} category="color"
                disabled={!whiteBalanceManual} onChange={update} temperature />
            </div>
            <label className="toggle-field" htmlFor="white-balance-lock">
              <span>Lock auto white balance<small>Hold the current automatic color balance</small></span>
              <input type="checkbox" id="white-balance-lock" name="white_balance_lock"
                checked={values.white_balance_lock === true} disabled={values.white_balance_mode !== 'auto'}
                onChange={(event) => update('white_balance_lock', event.currentTarget.checked)} />
            </label>
            <details className="advanced-controls" open={advanced.color}
              onToggle={(event) => advancedToggle('color', event.currentTarget.open)}>
              <summary>More color options</summary>
              <div className="compact-fields">
                <NumericField id="saturation" name="saturation" label="Saturation" help="−10 to +10"
                  min={-10} max={10} value={values.saturation} category="color" advanced="color" onChange={update} />
              </div>
            </details>
            <p className="field-help">Color adjustments affect previews and processed images. RAW keeps the sensor data.</p>
          </section>
          <section id="panel-focus" className="control-tab-panel" role="tabpanel"
            aria-labelledby="tab-focus" hidden={tab !== 'focus'}>
            <div className="control-section-heading">
              <h3>Lens focus</h3>
              <select id="focus-mode" name="focus_mode" aria-label="Focus mode" disabled={!hasFocus}
                value={String(values.focus_mode)} onChange={(event) => update('focus_mode', event.currentTarget.value)}>
                <option value="continuous">Continuous AF</option><option value="auto">Autofocus once</option>
                <option value="manual">Manual</option>
              </select>
            </div>
            <div id="manual-focus-fields" hidden={!focusManual}>
              <SliderField id="focus" numberId="focus-number" name="focus" label="Lens position" unit="0–255"
                numberLabel="Focus lens position" min={0} max={255} value={values.focus} category="focus"
                disabled={!hasFocus || !focusManual} onChange={update} />
            </div>
            <p className="field-help" id="focus-help">{camera?.autofocus === false
              ? 'This module has a fixed-focus lens. Focus control is unavailable.'
              : camera && !hasFocus ? 'Autofocus capability has not been reported by this module.'
                : 'Low values focus farther away; high values focus nearer.'}</p>
            <button type="button" id="trigger-autofocus" className="button button-quiet"
              hidden={values.focus_mode !== 'auto' || !hasFocus} disabled={disabled || !hasFocus}
              onClick={triggerAutofocus}>Run autofocus once</button>
          </section>
          <section id="panel-image" className="control-tab-panel" role="tabpanel"
            aria-labelledby="tab-image" hidden={tab !== 'image'}>
            <div className="control-section-heading"><h3>Image processing</h3><span className="subtle">Processed images</span></div>
            <div className="compact-fields">
              <label htmlFor="effect-mode">Color effect</label>
              <select id="effect-mode" name="effect_mode" value={String(values.effect_mode)}
                onChange={(event) => update('effect_mode', event.currentTarget.value)}>
                <option value="off">Off</option><option value="mono">Monochrome</option>
                <option value="negative">Negative</option><option value="sepia">Sepia</option>
              </select>
              <NumericField id="brightness" name="brightness" label="Brightness" help="−10 to +10"
                min={-10} max={10} value={values.brightness} category="image" onChange={update} />
              <NumericField id="contrast" name="contrast" label="Contrast" help="−10 to +10"
                min={-10} max={10} value={values.contrast} category="image" onChange={update} />
            </div>
            <details className="advanced-controls" open={advanced.image}
              onToggle={(event) => advancedToggle('image', event.currentTarget.open)}>
              <summary>Sharpness &amp; noise</summary>
              <div className="compact-fields">
                <NumericField id="sharpness" name="sharpness" label="Sharpness" help="0 to 4"
                  min={0} max={4} value={values.sharpness} category="image" advanced="image" onChange={update} />
                <NumericField id="luma-denoise" name="luma_denoise" label="Luma denoise" help="0 to 4"
                  min={0} max={4} value={values.luma_denoise} category="image" advanced="image" onChange={update} />
                <NumericField id="chroma-denoise" name="chroma_denoise" label="Chroma denoise" help="0 to 4"
                  min={0} max={4} value={values.chroma_denoise} category="image" advanced="image" onChange={update} />
              </div>
            </details>
            <p className="field-help">Image adjustments do not change RAW sensor data.</p>
          </section>
        </fieldset>
      </div>
      <div className="control-actions">
        <span id="control-dirty" className={`field-help${draft ? ' dirty' : ''}`} role="status">
          {draft ? 'Unapplied changes · saved in this tab' : 'Settings apply to this camera only.'}
        </span>
        <div>
          <button type="button" className="button button-quiet" id="reset-controls"
            disabled={disabled || !draft} onClick={() => clearDraft(activeSocket)}>Revert</button>
          <button type="submit" className="button button-primary" id="apply-controls"
            disabled={disabled}>Apply settings</button>
        </div>
      </div>
    </form>
  </aside>;
}
