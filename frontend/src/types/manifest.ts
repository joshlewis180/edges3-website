/**
 * Manifest type definitions — must stay in sync with `io_utils.py` on the
 * backend.
 */

export type PlotType = "s11" | "single" | "multi" | "image" | "heatmap"
export type PageName = "calibration" | "calibrated" | "raw"
export type PlotSource = "daemon" | "user"

export interface RunDates {
  cal?: string
  s11?: string
  raw?: string
}

export interface BasePlot {
  id: string
  type: PlotType
  title: string
  page: PageName
  /** Per-plot dates if the backend attached them (optional). */
  dates?: RunDates
  /** Which pipeline produced this plot. */
  source?: PlotSource
  /** X-axis label with units (e.g. "Frequency [MHz]"). */
  axisx?: string
  /** Y-axis label with units (e.g. "Temperature [K]"). */
  axisy?: string
}

export interface S11Plot extends BasePlot {
  type: "s11"
  filePath: string
}

export interface SinglePlot extends BasePlot {
  type: "single"
  filePath: string
  xKey?: string
  yKey?: string
}

export interface MultiPlot extends BasePlot {
  type: "multi"
  filePath1: string
  filePath2: string
  name1?: string
  name2?: string
}

export interface ImagePlot extends BasePlot {
  type: "image"
  filePath: string
}

export interface HeatmapPlot extends BasePlot {
  type: "heatmap"
  filePath: string
  /** Z-axis label with units for the colour bar. */
  axisz?: string
}

export type Plot = S11Plot | SinglePlot | MultiPlot | ImagePlot | HeatmapPlot

export interface Manifest {
  latest_run: string
  /** 'daemon' or 'user' — which pipeline produced this manifest. */
  source?: PlotSource
  dates?: RunDates
  generated_at?: string
  plots: Plot[]
}

export interface ActualTemperature {
  /** ISO-8601 timestamp of when the temperature was sampled. */
  time: string | null
  /** Sampled temperature in Kelvin. */
  temperature_k: number | null
}

export interface CalParameter {
  /** Temperature in Kelvin used by the analysis. */
  value_k: number | null
  /** ``"probe"`` if derived from the temperature log; ``"default"`` otherwise. */
  source: string | null
  /** Probe number used to derive the value (when ``source === "probe"``). */
  probe: number | null
  /** ISO-8601 timestamp of the calibration file used for the lookup. */
  time?: string | null
}

export interface LatestRunInfo {
  source: PlotSource | null
  run_id: string | null
  dates: RunDates
  generated_at: string | null
  /** Per-load actual temperatures (ambient/hot/lna) keyed by load name. */
  actual_temperatures?: Record<string, ActualTemperature>
  /** Calibration parameters used by the analysis (tcold, thot, tcab, tload, tns). */
  parameters?: Record<string, CalParameter>
  /** True if any ``_2d.npz`` heatmap files exist in the current source tree. */
  has_2d?: boolean
}
