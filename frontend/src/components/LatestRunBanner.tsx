/**
 * Page header shown above every data page. Displays:
 *   * which pipeline produced the current data (daemon / user)
 *   * the dates that were used
 *   * the generated-at timestamp
 *   * the save controls (2D checkbox + Save button + result)
 *   * a summary of per-load actual temperatures (with their sampling time)
 * The header sits above all plots; everything inside is stacked vertically
 * so neither the summary nor the save controls sit beside the plots.
 */

import { useState } from "react"
import { useRunState } from "../state/RunContext"
import { BASE_URL } from "../utils/baseURL"
import type { ActualTemperature } from "../types/manifest"

type BannerProps = {
  pageTitle?: string
}

const LOAD_ORDER = ["ambient", "hot", "lna"] as const
const LOAD_LABELS: Record<(typeof LOAD_ORDER)[number], string> = {
  ambient: "Ambient",
  hot: "Hot",
  lna: "LNA",
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return ""
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  // YYYY-MM-DD HH:MM:SS UTC
  const pad = (n: number) => String(n).padStart(2, "0")
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`
}

export default function LatestRunBanner({ pageTitle }: BannerProps) {
  const { latest } = useRunState()
  const [include2D, setInclude2D] = useState(false)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null)

  const sourceLabel = latest.source
    ? latest.source === "daemon"
      ? "Daily daemon run"
      : "Custom user run"
    : "No data yet"

  const dateLine = [
    latest.dates.cal && `cal: ${latest.dates.cal}`,
    latest.dates.s11 && `S11: ${latest.dates.s11}`,
    latest.dates.raw && `raw: ${latest.dates.raw}`,
  ]
    .filter(Boolean)
    .join("  \u00b7  ")

  const temps = latest.actual_temperatures ?? {}
  const tempLines = LOAD_ORDER
    .map((load) => {
      const t = temps[load] as ActualTemperature | undefined
      if (!t) return null
      const tempStr =
        typeof t.temperature_k === "number" ? `${t.temperature_k.toFixed(2)} K` : "n/a"
      const timeStr = formatTime(t.time)
      return `${LOAD_LABELS[load]}: ${tempStr}${timeStr ? `  \u00b7  ${timeStr}` : ""}`
    })
    .filter((line): line is string => Boolean(line))

  const params = latest.parameters ?? {}
  const paramLines = ["tcold", "thot", "tcab", "tload", "tns"]
    .map((name) => {
      const p = params[name]
      if (!p || typeof p.value_k !== "number") return null
      const probeStr = p.source === "probe" && p.probe != null ? `probe ${p.probe}` : "default"
      return `${name} = ${p.value_k.toFixed(2)} K  (${probeStr})`
    })
    .filter((line): line is string => Boolean(line))

  async function handleSave() {
    setSaving(true)
    setMessage(null)
    setDownloadUrl(null)
    try {
      const res = await fetch(`${BASE_URL}/save_outputs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ include_2d: include2D }),
      })
      if (!res.ok) {
        const text = await res.text()
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`)
      }
      const data = (await res.json()) as {
        size_bytes: number
        download_url: string
        label: string
        include_2d: boolean
      }
      setMessage(
        `Saved ${data.label} (${(data.size_bytes / 1024 / 1024).toFixed(2)} MB, ${
          data.include_2d ? "with 2D" : "images only"
        })`,
      )
      setDownloadUrl(data.download_url)
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="latest-run-banner p-3 mb-3 border rounded bg-light d-flex flex-column gap-2">
      <div>
        <strong>{pageTitle ? `${pageTitle} \u2014 ` : ""}{sourceLabel}</strong>
        {latest.generated_at && (
          <div className="text-muted small">
            Generated {new Date(latest.generated_at).toLocaleString()}
          </div>
        )}
        {dateLine && <div className="small">{dateLine}</div>}
      </div>

      {tempLines.length > 0 && (
        <div className="small">
          <div className="text-muted">
            Probe readings from the temperature log at each calibration time:
          </div>
          <ul className="mb-0 ps-3">
            {tempLines.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      )}

      {paramLines.length > 0 && (
        <div className="small">
          <div className="text-muted">Calibration parameters used by the analysis:</div>
          <ul className="mb-0 ps-3">
            {paramLines.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="d-flex align-items-center gap-3">
        <label
          className={`d-flex align-items-center gap-1 small mb-0${latest.has_2d ? "" : " text-muted"}`}
          title={
            latest.has_2d
              ? "Include heatmap (.npz) data in the saved zip"
              : "No heatmap data was produced by this run \u2014 re-run with \u201cInclude 2D heatmaps\u201d checked on the Select page to enable"
          }
        >
          <input
            type="checkbox"
            checked={include2D}
            disabled={!latest.has_2d}
            onChange={(e) => setInclude2D(e.target.checked)}
          />
          Include 2D heatmaps
          {!latest.has_2d && (
            <span className="text-muted">(none on disk)</span>
          )}
        </label>
        <button
          onClick={handleSave}
          disabled={saving}
          className="btn btn-sm btn-primary"
        >
          {saving ? "Saving\u2026" : "Save outputs"}
        </button>
      </div>

      {message && (
        <div className="small">
          {message}
          {downloadUrl && (
            <>
              {" \u2014 "}
              <a href={downloadUrl}>Download zip</a>
            </>
          )}
        </div>
      )}
    </div>
  )
}
