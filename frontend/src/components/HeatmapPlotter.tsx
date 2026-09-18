import { useCallback, useEffect, useState } from "react"
import Plot from "../utils/plotComponent"
import { openHeatmapFile } from "../utils/heatmapLoader"
import type { HeatmapData } from "../utils/heatmapLoader"
import { withBaseUrl } from "../utils/baseURL"
import type { HeatmapPlot } from "../types/manifest"

type Props = {
  input: HeatmapPlot
}

/**
 * Downsample a 2D array to at most maxRows × maxCols by block-averaging.
 * NaN values are ignored when computing the average. This keeps Plotly
 * responsive for very large waterfall data.
 */
function downsample2D(
  x: number[],
  y: number[],
  z: number[][],
  maxRows = 500,
  maxCols = 1000,
): { x: number[]; y: number[]; z: number[][] } {
  if (y.length <= maxRows && x.length <= maxCols) {
    return { x, y, z }
  }
  const rowFactor = Math.ceil(y.length / maxRows)
  const colFactor = Math.ceil(x.length / maxCols)
  const newRows = Math.ceil(y.length / rowFactor)
  const newCols = Math.ceil(x.length / colFactor)

  const newX = Array.from({ length: newCols }, (_, j) => {
    const start = j * colFactor
    const end = Math.min(start + colFactor, x.length)
    let sum = 0
    for (let k = start; k < end; k++) sum += x[k]
    return sum / (end - start)
  })
  const newY = Array.from({ length: newRows }, (_, i) => {
    const start = i * rowFactor
    const end = Math.min(start + rowFactor, y.length)
    let sum = 0
    for (let k = start; k < end; k++) sum += y[k]
    return sum / (end - start)
  })
  const newZ: number[][] = []
  for (let i = 0; i < newRows; i++) {
    const row: number[] = []
    for (let j = 0; j < newCols; j++) {
      let sum = 0
      let count = 0
      const yStart = i * rowFactor
      const yEnd = Math.min(yStart + rowFactor, y.length)
      const xStart = j * colFactor
      const xEnd = Math.min(xStart + colFactor, x.length)
      for (let ii = yStart; ii < yEnd; ii++) {
        const srcRow = z[ii]
        if (!srcRow) continue
        for (let jj = xStart; jj < xEnd; jj++) {
          const val = srcRow[jj]
          if (val !== undefined && Number.isFinite(val)) {
            sum += val
            count++
          }
        }
      }
      row.push(count > 0 ? sum / count : NaN)
    }
    newZ.push(row)
  }
  return { x: newX, y: newY, z: newZ }
}

export default function HeatmapPlotter({ input }: Props) {
  const [data, setData] = useState<HeatmapData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [zmin, setZmin] = useState<number | null>(null)
  const [zmax, setZmax] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
     
    setError(null)
    setData(null)
    openHeatmapFile(withBaseUrl(input.filePath))
      .then((d) => { if (!cancelled) setData(d) })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
    return () => { cancelled = true }
  }, [input.filePath])

  // Robust initial color range (5th / 95th percentiles).
  useEffect(() => {
    if (!data) return
    const allValues: number[] = []
    for (const row of data.z) {
      for (const val of row) {
        if (Number.isFinite(val)) allValues.push(val)
      }
    }
    if (allValues.length > 0) {
      allValues.sort((a, b) => a - b)
       
      setZmin(allValues[Math.floor(allValues.length * 0.05)])
       
      setZmax(allValues[Math.floor(allValues.length * 0.95)])
    } else {
       
      setZmin(null)
       
      setZmax(null)
    }
  }, [data])

  const getRangeIndices = useCallback(
    (arr: number[], min: number, max: number): [number, number] => {
      let start = 0
      let end = arr.length - 1
      while (start < arr.length && arr[start] < min) start++
      while (end >= 0 && arr[end] > max) end--
      return [start, end]
    },
    [],
  )

  const handleRelayout = useCallback(
    (event: Record<string, unknown>) => {
      if (!data) return
      const xMin = event["xaxis.range[0]"] as number | undefined
      const xMax = event["xaxis.range[1]"] as number | undefined
      const yMin = event["yaxis.range[0]"] as number | undefined
      const yMax = event["yaxis.range[1]"] as number | undefined
      if (xMin === undefined || xMax === undefined || yMin === undefined || yMax === undefined) {
        setZmin(null)
        setZmax(null)
        return
      }
      const [xStart, xEnd] = getRangeIndices(data.x, xMin, xMax)
      const [yStart, yEnd] = getRangeIndices(data.y, yMin, yMax)
      const visibleValues: number[] = []
      for (let i = yStart; i <= yEnd; i++) {
        const row = data.z[i]
        if (!row) continue
        for (let j = xStart; j <= xEnd; j++) {
          const val = row[j]
          if (val !== undefined && Number.isFinite(val)) visibleValues.push(val)
        }
      }
      if (visibleValues.length > 0) {
        visibleValues.sort((a, b) => a - b)
        setZmin(visibleValues[Math.floor(visibleValues.length * 0.05)])
        setZmax(visibleValues[Math.floor(visibleValues.length * 0.95)])
      } else {
        setZmin(null)
        setZmax(null)
      }
    },
    [data, getRangeIndices],
  )

  if (error) return <div className="text-danger">Error: {error}</div>
  if (!data) return <div>Loading heatmap…</div>

  const { x, y, z } = downsample2D(data.x, data.y, data.z)

  return (
    <div className="plot-wrapper d-flex flex-column">
      <h2 className="p-2">{input.title}</h2>
      <Plot
        data={[
          {
            x,
            y,
            z,
            type: "heatmap",
            colorscale: "Viridis",
            zmin: zmin ?? undefined,
            zmax: zmax ?? undefined,
            colorbar: input.axisz ? { title: { text: input.axisz } } : undefined,
          },
        ]}
        layout={{
          autosize: true,
          height: 400,
          margin: { l: 60, r: 20, t: 40, b: 80 },
          xaxis: { title: { text: input.axisx || "Frequency [MHz]" } },
          yaxis: { title: { text: input.axisy || "LST [hr]" } },
        }}
        useResizeHandler={true}
        style={{ width: "90%" }}
        onRelayout={handleRelayout}
      />
    </div>
  )
}
