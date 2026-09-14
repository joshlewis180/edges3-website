import { useEffect, useState } from "react"
import { Link } from "react-router"
import { BASE_URL } from "../utils/baseURL"
import { useRunState } from "../state/RunContext"
import LatestRunBanner from "../components/LatestRunBanner"

type DaemonStatus = {
  enabled: boolean
  hour: number
  raw_data_root_exists: boolean
  lock: { busy: boolean; holder: string | null }
}

export default function Home() {
  const { latest, bumpRefresh } = useRunState()
  const [status, setStatus] = useState<DaemonStatus | null>(null)
  const [triggering, setTriggering] = useState(false)
  const [triggerMsg, setTriggerMsg] = useState<string | null>(null)

  useEffect(() => {
    fetch(`${BASE_URL}/daemon/status`, { cache: "no-store" })
      .then((r) => r.json())
      .then((d: DaemonStatus) => setStatus(d))
      .catch(() => setStatus(null))
  }, [])

  async function triggerDaemon() {
    setTriggering(true)
    setTriggerMsg(null)
    try {
      const res = await fetch(`${BASE_URL}/daemon/trigger`, { method: "POST" })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = (await res.json()) as { dates: Record<string, string> }
      setTriggerMsg(
        `Daemon run complete (cal=${data.dates.cal}, raw=${data.dates.raw})`,
      )
      bumpRefresh()
    } catch (err: unknown) {
      setTriggerMsg(err instanceof Error ? err.message : String(err))
    } finally {
      setTriggering(false)
    }
  }

  return (
    <div className="d-flex flex-column p-3 gap-3">
      <LatestRunBanner pageTitle="Home" />

      <div className="border rounded p-3">
        <h2>Welcome</h2>
        <p>
          Use <Link to="/Select">Select</Link> to pick dates and parameters, or browse the
          ready-made plots in <Link to="/CalibrationData">Calibration</Link>,{" "}
          <Link to="/RawData">Raw Data</Link>, and <Link to="/CalibratedData">Calibrated Data</Link>.
        </p>
      </div>

      <div className="border rounded p-3">
        <h2>Daemon status</h2>
        {status ? (
          <>
            <p>
              Scheduler enabled: <strong>{status.enabled ? "yes" : "no"}</strong>
              {" · "}
              Target hour: <strong>{status.hour}:00</strong>
              {" · "}
              Raw data root exists: <strong>{status.raw_data_root_exists ? "yes" : "no"}</strong>
            </p>
            <p>
              Pipeline busy: <strong>{status.lock.busy ? `yes (${status.lock.holder})` : "no"}</strong>
            </p>
            <button onClick={triggerDaemon} disabled={triggering} className="btn btn-sm btn-outline-primary">
              {triggering ? "Triggering…" : "Run daemon now"}
            </button>
            {triggerMsg && <div className="small mt-2">{triggerMsg}</div>}
          </>
        ) : (
          <p className="text-muted">Daemon status unavailable (is the backend running?).</p>
        )}
        {latest.source === "daemon" && (
          <p className="text-muted small mt-2">
            The website is currently showing data from the most recent daemon run.
          </p>
        )}
      </div>
    </div>
  )
}
