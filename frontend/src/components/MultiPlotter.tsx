/**
 * Multi plotter — overlays two npz line plots (e.g. calibration temperature
 * vs. actual on-site temperature) on the same axes. Supports an Actual /
 * Residuals toggle that swaps the second trace for the difference between
 * the two.
 */

import { useEffect, useState } from "react"
import Plot from "../utils/plotComponent"
import { openDataFile } from "../utils/dataLoader"
import type { Data } from "../utils/dataLoader"
import { withBaseUrl } from "../utils/baseURL"
import MultiSelector from "./MultiSelector"

export type MultiPlotInput = {
  type: "multi"
  title: string
  filePath1: string
  filePath2: string
  name1?: string
  name2?: string
  axisx?: string
  axisy?: string
}

type MultiPlotProps = {
  input: MultiPlotInput
}

function MultiPlotter({ input }: MultiPlotProps) {
  const [data1, setData1] = useState<Data | null>(null)
  const [data2, setData2] = useState<Data | null>(null)
  const [displayMode, setDisplayMode] = useState("Actual Values")
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
     
    setError(null)
    Promise.all([
      openDataFile(withBaseUrl(input.filePath1)),
      openDataFile(withBaseUrl(input.filePath2)),
    ])
      .then(([d1, d2]) => {
        if (cancelled) return
        setData1(d1)
        setData2(d2)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [input.filePath1, input.filePath2])

  if (error) return <div className="text-danger">Error: {error}</div>
  if (!data1 || !data2) return <div>Loading multi plot...</div>

  // Align to the same x-axis (use data1's x; data2 is expected to be
  // a horizontal line with the same x array). If x arrays differ, fall
  // back to whatever data1 provides.
  const x = data1.x
  const name1 = input.name1 || "Trace 1"
  const name2 = input.name2 || "Trace 2"

  const trace1 = { x, y: data1.y, type: "scatter" as const, mode: "lines" as const, name: name1 }
  let trace2
  if (displayMode === "Residuals" && data1.y.length === data2.y.length) {
    const diff = data1.y.map((v, i) => v - data2.y[i])
    trace2 = { x, y: diff, type: "scatter" as const, mode: "lines" as const, name: `${name1} − ${name2}` }
  } else {
    trace2 = { x, y: data2.y, type: "scatter" as const, mode: "lines" as const, name: name2 }
  }

  return (
    <div className="plot-wrapper d-flex flex-column">
      <div className="d-flex flex-column w-100 align-items-start">
        <h2 className="p-2">{input.title}</h2>
        <MultiSelector onSelectChange={setDisplayMode} />
      </div>
      <Plot
        data={[trace1, trace2]}
        layout={{
          autosize: true,
          height: 300,
          margin: { l: 50, r: 20, t: 40, b: 100 },
          showlegend: true,
          xaxis: { title: { text: input.axisx || "Frequency [MHz]" } },
          yaxis: { title: { text: input.axisy || "Y [arb. units]" } },
        }}
        useResizeHandler={true}
        style={{ width: "90%" }}
      />
    </div>
  )
}

export default MultiPlotter
